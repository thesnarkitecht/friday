"""Config: defaults < ~/.friday/config.toml. Secrets stay in env vars, named by api_key_env."""

import copy
import os
import tomllib
from pathlib import Path

HOME = Path(os.environ.get("FRIDAY_HOME", "~/.friday")).expanduser()

DEFAULTS = {
    "user": os.environ.get("USER", "user"),
    # tier -> {model, base_url, api_key_env}. Only "main" is required; fast/deep fall back to it.
    "models": {},
    "s1": {
        "jev_model": "jev-latest",
        "confidence": 0.6,  # below this a Jev decision falls back to the safe default
        "needle_confidence": 0.5,  # below this a Needle tool call escalates to System 2
        "needle_weights": "",  # path to a fine-tuned .cact (see `friday train`)
    },
    "context": {"window": 128_000, "compact_at": 0.7, "keep_recent": 8},
    # "auto" uses the bundled Honcho when `friday up` has it running; "" turns Honcho off.
    "honcho": {"base_url": os.environ.get("HONCHO_BASE_URL", "auto"), "workspace": "friday"},
    "watch": [],
}

EXAMPLE = """\
user = "you"

# Any OpenAI-compatible endpoint works. Only [models.main] is required.
[models.fast]
model = "llama3.2"
base_url = "http://localhost:11434/v1"      # Ollama

[models.main]
model = "your-main-model"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"

[models.deep]
model = "your-strongest-model"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"

[honcho]
base_url = "auto"   # the bundled Honcho once `friday up` has started it; "" to turn it off

# Proactive watchers for `friday watch`. With cmd, fires only when the output changes.
# [[watch]]
# name = "repo"
# every = 300
# cmd = "git -C ~/code/myrepo status --porcelain"
# prompt = "The working tree changed. Tell me if anything needs my attention."
"""


def load() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = HOME / "config.toml"
    if path.exists():
        for key, value in tomllib.loads(path.read_text()).items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg
