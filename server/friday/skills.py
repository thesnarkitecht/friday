"""Skills: how-to knowledge FRIDAY loads only when it needs it (progressive disclosure).

Ported in spirit from Hermes Agent (Nous Research, MIT): a skill is a folder holding SKILL.md with
`name` and `description` frontmatter, the same format as Hermes and agentskills.io, so their skills drop
in. Only the one-line index sits in the (cached) system prompt; `skill_view` loads the body on demand.
FRIDAY writes and improves its own skills, mostly in the nightly review of what it did that day."""
import re
from pathlib import Path

from pydantic import BaseModel

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MAX_BODY = 8000

REVIEW = (
    "You are reviewing what a voice assistant did today, to make it better at repeat tasks. Below: its "
    "existing skills (name: description), then today's tool-using tasks. Write a skill only for a "
    "multi-step procedure likely to recur, or to fix an existing skill that led to a wrong or slow result "
    "(reuse its name to replace it). Bodies are short, numbered steps naming the tools to call, plus "
    "pitfalls. Names are kebab-case; descriptions say in one line when to use the skill. Return no skills "
    "if nothing is worth saving.\n\n")


class SkillDraft(BaseModel):
    name: str
    description: str
    body: str


class SkillReview(BaseModel):
    skills: list[SkillDraft]


def _frontmatter(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    meta = dict(re.findall(r"^(name|description):\s*(.+?)\s*$", m.group(1), re.M))
    return {k: v.strip("'\"") for k, v in meta.items()}, m.group(2)


class Skills:
    def __init__(self, root, extra=()):
        """root: where FRIDAY writes its own skills. extra: read-only skill folders (plugins ship these)."""
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.extra = [Path(p) for p in extra]

    def all(self):
        out = {}
        for f in [f for r in (*self.extra, self.root) for f in sorted(r.glob("*/SKILL.md"))]:
            meta, body = _frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            name = meta.get("name") or f.parent.name
            out[name] = {"description": meta.get("description", "")[:160], "body": body, "dir": f.parent}
        return out

    def index(self):
        return "\n".join(f"- {n}: {s['description']}" for n, s in self.all().items()) or "(none yet)"

    def view(self, name):
        s = self.all().get(name)
        return s["body"][:MAX_BODY] if s else None

    def save(self, name, description, body):
        """Create or replace a skill. Names are validated: they become directory names."""
        name = name.strip().lower()
        if not NAME.match(name):
            raise ValueError("skill name must be kebab-case, 1-40 chars")
        description = " ".join(description.split())[:160]
        existing = self.all().get(name)
        folder = existing["dir"] if existing and existing["dir"].parent == self.root else self.root / name
        folder.mkdir(exist_ok=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()[:MAX_BODY]}\n", encoding="utf-8")
        return name

    async def review(self, llm, worklog):
        """The nightly learning pass over the day's tool-using tasks. Returns names written."""
        if not worklog:
            return []
        tasks = "\n\n".join(
            f"Asked: {w['ask'][:300]}\nTools: {'; '.join(w['tools'])[:800]}\nReplied: {w['reply'][:300]}"
            for w in worklog[-20:])
        review = await llm.decide(f"{REVIEW}Skills:\n{self.index()}\n\nToday:\n{tasks}", SkillReview, effort="medium")
        written = []
        for s in review.skills if review else []:
            try:
                written.append(self.save(s.name, s.description, s.body))
            except ValueError:
                pass  # one malformed suggestion shouldn't sink the rest
        return written
