import os
from types import SimpleNamespace


def load():
    """Secrets and settings come from env (one .env file mounted into the container)."""
    e = os.environ.get
    return SimpleNamespace(
        token=e("FRIDAY_TOKEN", ""),
        owner=e("FRIDAY_OWNER_NAME", "the owner"),
        db=e("FRIDAY_DB", "friday.db"),
        skills_dir=e("FRIDAY_SKILLS_DIR", "skills"),
        plugins_dir=e("FRIDAY_PLUGINS_DIR", "plugins"),
        model=e("FRIDAY_MODEL", "claude-opus-5-5"),
        # Raw speech is dropped after the nightly pass unless it scored as worth keeping.
        retain_days=int(e("FRIDAY_RETAIN_DAYS", "1")),
        keep_importance=float(e("FRIDAY_KEEP_IMPORTANCE", "0.5")),
        record_others=e("FRIDAY_RECORD_OTHERS", "0") == "1",
        # Hours (local, 24h) when only urgent things speak, as "start-end". "0-0" turns it off.
        quiet_hours=tuple(int(h) for h in e("FRIDAY_QUIET_HOURS", "22-8").split("-")),
    )
