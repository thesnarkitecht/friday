"""Run the bundled Honcho (vendor/honcho) on this machine: API, deriver, Postgres, Redis.

`up` writes Honcho's .env from Friday's own model config the first time, so there is one place
to configure models. It never overwrites an existing .env.
"""

import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from .memory import LOCAL_HONCHO
from .models import spec

HONCHO = Path(__file__).resolve().parent.parent / "vendor" / "honcho"
# Every Honcho feature that calls an LLM; each takes its own model config.
FEATURES = ["DERIVER", "SUMMARY", "DREAM_DEDUCTION", "DREAM_INDUCTION"] + [
    f"DIALECTIC_LEVELS__{level}" for level in ("minimal", "low", "medium", "high", "max")
]


def honcho_env(cfg: dict) -> str:
    """Honcho's .env, pointing all of its LLM work at Friday's fast model."""
    s = spec(cfg, "fast")
    # From inside a container, "localhost" is the container; the host is host.docker.internal.
    base = (s.get("base_url") or "").replace("localhost", "host.docker.internal").replace(
        "127.0.0.1", "host.docker.internal"
    )
    lines = [
        "# Written by `friday up`. Edit freely; it is not overwritten.",
        "AUTH_USE_AUTH=false",
        f"LLM_OPENAI_API_KEY={os.environ.get(s.get('api_key_env', 'OPENAI_API_KEY')) or 'none'}",
    ]
    for feature in FEATURES:
        lines += [f"{feature}_MODEL_CONFIG__TRANSPORT=openai", f"{feature}_MODEL_CONFIG__MODEL={s['model']}"]
        if base:
            lines.append(f"{feature}_MODEL_CONFIG__OVERRIDES__BASE_URL={base}")
    h = cfg["honcho"]
    if h.get("embedding_model"):
        lines.append(f"EMBEDDING_MODEL_CONFIG__MODEL={h['embedding_model']}")
    if h.get("embedding_base_url"):
        lines.append(f"EMBEDDING_MODEL_CONFIG__OVERRIDES__BASE_URL={h['embedding_base_url']}")
    if h.get("embedding_dimensions"):
        lines.append(f"EMBEDDING_VECTOR_DIMENSIONS={h['embedding_dimensions']}")
    return "\n".join(lines) + "\n"


def healthy() -> bool:
    try:
        urllib.request.urlopen(f"{LOCAL_HONCHO}/health", timeout=2)
        return True
    except OSError:
        return False


def up(cfg: dict, say=print) -> bool:
    if not (HONCHO / "docker-compose.yml.example").exists():
        say("vendor/honcho is empty. Run: git submodule update --init")
        return False
    if not shutil.which("docker"):
        say("Docker is not installed; Honcho needs it (Postgres + pgvector, Redis).")
        return False
    compose = HONCHO / "docker-compose.yml"
    if not compose.exists():
        shutil.copy(HONCHO / "docker-compose.yml.example", compose)
    env = HONCHO / ".env"
    if not env.exists():
        env.touch(mode=0o600)  # it holds an API key
        env.write_text(honcho_env(cfg))
        say(f"wrote {env} from your fast model; embeddings use Honcho's default unless [honcho] embedding_* is set")
    if subprocess.run(["docker", "compose", "up", "-d", "--build"], cwd=HONCHO).returncode != 0:
        return False
    for _ in range(90):
        if healthy():
            say(f"Honcho is up at {LOCAL_HONCHO}")
            return True
        time.sleep(2)
    say("Honcho did not become healthy in 3 minutes. See: docker compose logs (in vendor/honcho)")
    return False


def down() -> bool:
    return subprocess.run(["docker", "compose", "down"], cwd=HONCHO).returncode == 0
