import json
import re

import httpx


class LLM:
    """Thin client for any OpenAI-compatible /chat/completions endpoint (vLLM, tt-inference-server, Anthropic's compat API)."""

    def __init__(self, base, key, model, client=None):
        self.base, self.key, self.model = base.rstrip("/"), key, model
        self.client = client or httpx.AsyncClient(timeout=120)

    async def chat(self, messages, tools=None, model=None, max_tokens=800):
        body = {"model": model or self.model, "messages": messages, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
        r = await self.client.post(f"{self.base}/chat/completions", json=body,
                                   headers={"Authorization": f"Bearer {self.key}"})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]

    async def text(self, prompt, model=None, max_tokens=800):
        msg = await self.chat([{"role": "user", "content": prompt}], model=model, max_tokens=max_tokens)
        return msg.get("content") or ""


def parse_json(text):
    """Pull the first JSON object out of a model reply (models like to wrap it in prose or fences)."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
