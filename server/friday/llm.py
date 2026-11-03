"""FRIDAY's brain: Claude through the official Anthropic SDK (default), or any OpenAI-compatible API.

Sign-in: run `ant auth login` once (the SDK reads that profile), or set ANTHROPIC_API_KEY."""
import json
import re

import anthropic

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class Unavailable(Exception):
    """Claude can't be used right now; str(e) is a sentence FRIDAY can say out loud."""


async def _call(make):
    try:
        return await make()
    except anthropic.AuthenticationError as e:
        raise Unavailable("I can't sign in to Claude. Run ant auth login again, or check the API key.") from e
    except anthropic.RateLimitError as e:
        raise Unavailable("Claude is rate-limiting me. Try me again in a minute.") from e
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        raise Unavailable("I can't reach Claude right now.") from e
    except (TypeError, anthropic.CredentialsError) as e:  # no sign-in profile and no key
        if isinstance(e, TypeError) and "authentication" not in str(e):
            raise
        raise Unavailable("I'm not signed in to Claude yet. Run ant auth login, or set ANTHROPIC_API_KEY.") from e


class Claude:
    def __init__(self, model="claude-opus-5-5", client=None):
        self.model, self._client = model, client

    @property
    def client(self):
        # Created on first use, so a missing sign-in is a spoken answer, not a server that won't start.
        if self._client is None:
            self._client = anthropic.AsyncAnthropic()
        return self._client

    async def chat(self, system, messages, tools=(), effort="low", max_tokens=4000, on_text=None):
        """One Messages API call. Returns text, tool uses, and the raw content to append back unchanged.
        With on_text, the reply streams: on_text(chunk) is awaited as words arrive, so FRIDAY can start
        speaking the first sentence while Claude is still writing the rest."""
        kwargs = dict(
            model=self.model, max_tokens=max_tokens, system=system, messages=messages,
            output_config={"effort": effort},
            cache_control={"type": "ephemeral"},  # system + tools + history are a stable prefix
            betas=[FALLBACK_BETA], fallbacks="default",  # a declined request is re-run server-side
            **({"tools": list(tools)} if tools else {}))

        async def streamed():
            async with self.client.beta.messages.stream(**kwargs) as stream:
                async for chunk in stream.text_stream:
                    await on_text(chunk)
                return await stream.get_final_message()

        r = await _call(streamed if on_text else lambda: self.client.beta.messages.create(**kwargs))
        return {
            "stop": r.stop_reason, "content": r.content,
            "text": "".join(b.text for b in r.content if b.type == "text").strip(),
            "tool_uses": [{"id": b.id, "name": b.name, "input": b.input} for b in r.content if b.type == "tool_use"],
        }

    async def decide(self, prompt, schema, effort="low", max_tokens=4000):
        """A quick, type-safe decision: the reply is validated into `schema` (a Pydantic model).
        Returns None if Claude declined, so callers treat it as "no decision"."""
        r = await _call(lambda: self.client.beta.messages.parse(
            model=self.model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}],
            output_format=schema, output_config={"effort": effort},
            betas=[FALLBACK_BETA], fallbacks="default"))
        return None if r.stop_reason == "refusal" else r.parsed_output


class OpenAICompat:
    """Any OpenAI-compatible chat API: OpenRouter, Ollama, LM Studio, llama.cpp, vLLM, OpenAI, Hermes…
    Speaks the same interface as Claude above, so the brain doesn't care which one it's talking to."""

    def __init__(self, base_url, key, model, http=None):
        import httpx
        self.url, self.model = base_url.rstrip("/") + "/chat/completions", model
        # A model on this machine (Ollama, LM Studio…) is never reached through a network proxy.
        self.http = http or httpx.AsyncClient(timeout=300, trust_env=not is_local(base_url))
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}

    async def _post(self, body):
        import httpx
        try:
            r = await self.http.send(self.http.build_request("POST", self.url, json=body, headers=self.headers),
                                     stream=bool(body.get("stream")))
        except httpx.HTTPError as e:
            raise Unavailable(f"I can't reach the model at {self.url.split('/')[2]}.") from e
        if r.status_code in (401, 403):
            await r.aclose()
            raise Unavailable("The model service refused the key. Check FRIDAY_LLM_KEY.")
        if r.status_code == 429:
            await r.aclose()
            raise Unavailable("The model service is rate-limiting me. Try me again in a minute.")
        if r.status_code >= 500 or (body.get("stream") and r.status_code >= 400):
            await r.aclose()
            raise Unavailable(f"The model service at {self.url.split('/')[2]} isn't answering properly "
                              f"(HTTP {r.status_code}).")
        return r

    async def chat(self, system, messages, tools=(), effort="low", max_tokens=4000, on_text=None):
        body = {"model": self.model, "max_tokens": max_tokens, "stream": bool(on_text),
                "messages": [{"role": "system", "content": system}, *to_openai(messages)]}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
        r = await self._post(body)
        text, calls, finish = "", {}, None
        if on_text:
            async for line in r.aiter_lines():
                if not line.startswith("data:") or line.strip() == "data: [DONE]":
                    continue
                choice = (json.loads(line[5:]).get("choices") or [{}])[0]
                finish = choice.get("finish_reason") or finish
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text += delta["content"]
                    await on_text(delta["content"])
                for tc in delta.get("tool_calls") or []:  # tool calls arrive in pieces, keyed by index
                    c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    c["id"] = tc.get("id") or c["id"]
                    c["name"] += (tc.get("function") or {}).get("name") or ""
                    c["args"] += (tc.get("function") or {}).get("arguments") or ""
        else:
            await r.aread()
            if r.status_code >= 400:
                raise Unavailable("The model service had a problem with that request.")
            choice = r.json()["choices"][0]
            finish, msg = choice.get("finish_reason"), choice["message"]
            text = msg.get("content") or ""
            for i, tc in enumerate(msg.get("tool_calls") or []):
                calls[i] = {"id": tc["id"], "name": tc["function"]["name"], "args": tc["function"]["arguments"]}
        uses = [{"id": c["id"] or f"call_{i}", "name": c["name"], "input": _args(c["args"])}
                for i, c in sorted(calls.items())]
        content = ([{"type": "text", "text": text}] if text else []) + [{"type": "tool_use", **u} for u in uses]
        stop = "refusal" if finish == "content_filter" else "tool_use" if uses else "end_turn"
        return {"stop": stop, "content": content, "text": text.strip(), "tool_uses": uses}

    async def decide(self, prompt, schema, effort="low", max_tokens=4000):
        """Structured output where the server supports it; otherwise ask for JSON and validate it."""
        import pydantic
        fmt = {"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}}
        body = {"model": self.model, "max_tokens": max_tokens, "response_format": fmt,
                "messages": [{"role": "user", "content": prompt}]}
        r = await self._post(body)
        await r.aread()
        if r.status_code == 400:  # no structured outputs here: describe the shape instead
            body.pop("response_format")
            body["messages"][0]["content"] += ("\n\nReply with only a JSON object matching this JSON Schema:\n"
                                               + json.dumps(schema.model_json_schema()))
            r = await self._post(body)
            await r.aread()
        if r.status_code >= 400:
            raise Unavailable("The model service had a problem with that request.")
        raw = r.json()["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            return schema.model_validate_json(m.group(0)) if m else None
        except pydantic.ValidationError:
            return None


def is_local(url):
    from urllib.parse import urlparse
    return urlparse(url).hostname in ("localhost", "127.0.0.1", "::1")


def _args(raw):
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}  # the tool reports what's missing; the model can try again


def to_openai(messages):
    """The brain's Anthropic-shaped history -> OpenAI chat messages."""
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
        elif m["role"] == "user":  # tool results
            out += [{"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]}
                    for b in content if b.get("type") == "tool_result"]
        else:
            blocks = [b if isinstance(b, dict) else b.model_dump() for b in content]
            msg = {"role": "assistant", "content": "".join(b.get("text", "") for b in blocks if b["type"] == "text")}
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                     for b in blocks if b["type"] == "tool_use"]
            out.append({**msg, **({"tool_calls": calls} if calls else {})})
    return out
