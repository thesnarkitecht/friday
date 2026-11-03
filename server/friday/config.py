import os
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).parent
HOME = Path(os.environ.get("FRIDAY_HOME") or Path.home() / ".friday")  # everything FRIDAY keeps lives here


def load_env_file(path=None):
    """~/.friday/.env holds credentials (Claude key, plugin keys). Real env vars win."""
    try:
        lines = Path(path or HOME / ".env").read_text().splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load():
    e = os.environ.get
    home = Path(e("FRIDAY_HOME") or HOME)
    voice = e("FRIDAY_VOICE", "en_GB-jenny_dioco-medium")
    return SimpleNamespace(
        home=home,
        token=e("FRIDAY_TOKEN", ""),
        owner=e("FRIDAY_OWNER_NAME", "the owner"),
        db=e("FRIDAY_DB") or str(home / "friday.db"),
        skills_dir=e("FRIDAY_SKILLS_DIR") or str(home / "skills"),
        # Built-in plugins ship in the package; your own go in ~/.friday/plugins.
        plugin_roots=[str(PACKAGE / "builtin_plugins"), e("FRIDAY_PLUGINS_DIR") or str(home / "plugins")],
        voice=voice,
        voice_path=e("FRIDAY_PIPER_VOICE") or str(home / "voices" / f"{voice}.onnx"),
        model=e("FRIDAY_MODEL", "claude-opus-5-5"),
        # Raw speech is dropped after the nightly pass unless it scored as worth keeping.
        retain_days=int(e("FRIDAY_RETAIN_DAYS", "1")),
        keep_importance=float(e("FRIDAY_KEEP_IMPORTANCE", "0.5")),
        record_others=e("FRIDAY_RECORD_OTHERS", "0") == "1",
        # Hours (local, 24h) when only urgent things speak, as "start-end". "0-0" turns it off.
        # FRIDAY greets you when you put your headphones on after this many hours away. 0 turns it off.
        greet_after_h=float(e("FRIDAY_GREET_AFTER_HOURS", "4")),
        quiet_hours=tuple(int(h) for h in e("FRIDAY_QUIET_HOURS", "22-8").split("-")),
    )
