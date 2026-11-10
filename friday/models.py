"""System 2: slow, generative models. Any OpenAI-compatible endpoint, three capability tiers."""

import os
from functools import lru_cache

from openai import OpenAI

TIERS = ("fast", "main", "deep")


def spec(cfg: dict, tier: str) -> dict:
    models = cfg["models"]
    found = models.get(tier) or models.get("main") or next(iter(models.values()), None)
    if not found or not found.get("model"):
        raise RuntimeError("No model configured. Run `friday init` and edit ~/.friday/config.toml.")
    return found


@lru_cache
def _client(base_url: str | None, key_env: str) -> OpenAI:
    # Local servers need no key, but the SDK insists on a non-empty string.
    return OpenAI(base_url=base_url, api_key=os.environ.get(key_env) or "none")


def chat(cfg: dict, tier: str, messages: list, tools: list | None = None, on_delta=None):
    """One completion. Returns (message, prompt_tokens). `on_delta(text)` streams the reply."""
    s = spec(cfg, tier)
    client = _client(s.get("base_url"), s.get("api_key_env", "OPENAI_API_KEY"))
    kwargs = {"model": s["model"], "messages": messages, **({"tools": tools} if tools else {})}
    if on_delta is None:
        r = client.chat.completions.create(**kwargs)
    else:
        with client.chat.completions.stream(**kwargs, stream_options={"include_usage": True}) as stream:
            for event in stream:
                if event.type == "content.delta":
                    on_delta(event.delta)
            r = stream.get_final_completion()
    return r.choices[0].message, (r.usage.prompt_tokens if r.usage else 0)


def ladder(cfg: dict, start: str) -> list[str]:
    """Tiers to try from `start` upward, skipping ones that resolve to an already-listed model."""
    out, seen = [], set()
    for tier in TIERS[TIERS.index(start) :]:
        s = spec(cfg, tier)
        ident = (s["model"], s.get("base_url"))
        if ident not in seen:
            seen.add(ident)
            out.append(tier)
    return out
