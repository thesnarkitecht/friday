"""Skills: SKILL.md folders (the same format Hermes and Claude Code use), picked by System 1.

Three things make this more than a folder of prompts:
- Jev ranks the whole library per request, so no skill index sits in the big model's context.
- Every use is scored (did the turn resolve without escalating?), and a skill that keeps losing
  is retired from selection until it is rewritten.
- After a multi-step turn Jev decides whether the work becomes a new skill or a rewrite of the
  one that was used, and System 2 writes it.
"""

import json
import re
from pathlib import Path

from .config import HOME

REPO_SKILLS = Path(__file__).resolve().parent.parent / "skills"


def dirs() -> list[Path]:
    # Existing Claude Code and Hermes skills work as they are; Friday's own take precedence.
    home = Path.home()
    return [
        REPO_SKILLS,
        home / ".claude" / "skills",
        home / ".hermes" / "skills",
        HOME / "skills",
        Path.cwd() / ".friday" / "skills",
    ]


def load() -> dict[str, tuple[str, Path]]:
    """name -> (description, path). Later directories override earlier ones."""
    found = {}
    for d in dirs():
        for f in sorted(d.rglob("SKILL.md")):
            head = f.read_text(errors="replace")[:2000]
            name = re.search(r"^name:\s*(.+)$", head, re.M)
            desc = re.search(r"^description:\s*(.+)$", head, re.M)
            found[(name.group(1) if name else f.parent.name).strip()] = (
                desc.group(1).strip() if desc else "",
                f,
            )
    return found


def save(text: str) -> Path | None:
    """Write a model-authored SKILL.md under ~/.friday/skills. Returns None if it has no name."""
    text = re.sub(r"^```\w*\n|\n```\s*$", "", text.strip())
    name = re.search(r"^name:\s*(.+)$", text, re.M)
    if not name:
        return None
    # The name came from a model: keep it a single safe path segment.
    slug = re.sub(r"[^a-z0-9]+", "-", name.group(1).lower()).strip("-")[:60]
    if not slug:
        return None
    path = HOME / "skills" / slug / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")
    return path


def scores() -> dict[str, list[int]]:
    """name -> [uses, wins]."""
    path = HOME / "skills" / "scores.json"
    return json.loads(path.read_text()) if path.exists() else {}


def record(names: list[str], won: bool) -> None:
    data = scores()
    for name in names:
        uses, wins = data.get(name, [0, 0])
        data[name] = [uses + 1, wins + int(won)]
    path = HOME / "skills" / "scores.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def healthy(score: list[int] | None) -> bool:
    """A skill that lost at least 3 of its first 4+ uses stops being offered."""
    return not score or score[0] < 4 or score[1] / score[0] >= 0.25


def reset(name: str) -> None:
    """A rewritten skill starts with a clean record."""
    data = scores()
    if data.pop(name, None) is not None:
        (HOME / "skills" / "scores.json").write_text(json.dumps(data))


DISTILL = """\
Turn the work below into a reusable skill so the next similar request is faster and avoids the
same dead ends. Reply with only a SKILL.md: YAML frontmatter with `name` (kebab-case) and a
one-line `description` saying when to use it, then the steps, the pitfalls hit, and how to check
the result. Be specific to what actually worked. {existing}

WORK:
{work}"""
