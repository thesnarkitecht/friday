"""New model drops, checked every 10 minutes with plain HTTP (no tokens).

OpenRouter's public model list covers every major lab and needs no key. A model id FRIDAY hasn't
seen before is a drop. Set FRIDAY_MODEL_LABS to choose labs (comma-separated id prefixes)."""

LABS = "anthropic,openai,google,meta-llama,mistralai,deepseek,qwen,x-ai,moonshotai,nousresearch,z-ai"


def new_models(data, labs):
    """Pure: OpenRouter /models JSON -> watched models. Variants like ':free' aren't new models."""
    out = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        if ":" in mid or mid.split("/")[0] not in labs:
            continue
        out.append({"key": mid, "kind": "models",
                    "text": f"New model just showed up: {m.get('name') or mid}."})
    return out


def setup(p):
    labs = {x.strip() for x in p.env("FRIDAY_MODEL_LABS", LABS).split(",") if x.strip()}

    @p.feed(every_s=600)
    async def drops():
        r = await p.http.get("https://openrouter.ai/api/v1/models")
        r.raise_for_status()
        return new_models(r.json(), labs)
