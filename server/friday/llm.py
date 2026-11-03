"""Claude, through the official Anthropic SDK.

Sign-in: run `ant auth login` once (the SDK reads that profile), or set ANTHROPIC_API_KEY."""
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


class Claude:
    def __init__(self, model="claude-opus-5-5", client=None):
        self.model = model
        self.client = client or anthropic.AsyncAnthropic()

    async def chat(self, system, messages, tools=(), effort="low", max_tokens=4000):
        """One Messages API call. Returns text, tool uses, and the raw content to append back unchanged."""
        kwargs = {"tools": list(tools)} if tools else {}
        r = await _call(lambda: self.client.beta.messages.create(
            model=self.model, max_tokens=max_tokens, system=system, messages=messages,
            output_config={"effort": effort},
            cache_control={"type": "ephemeral"},  # system + tools + history are a stable prefix
            betas=[FALLBACK_BETA], fallbacks="default",  # a declined request is re-run server-side
            **kwargs))
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
