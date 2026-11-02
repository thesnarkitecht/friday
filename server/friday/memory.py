"""Long-term memory: every transcript lands here, gets an importance score, and a nightly
"sleep" pass turns the day into a summary plus durable facts. Low-value raw text decays."""
import re
import sqlite3
import time
from datetime import datetime

from .llm import parse_json

SCHEMA = """
create table if not exists utterances(id integer primary key, ts real, speaker text, text text,
    place text, importance real, addressed int, ephemeral int, consolidated int default 0);
create virtual table if not exists utterances_fts using fts5(text);
create table if not exists facts(id integer primary key, ts real, category text, text text unique,
    importance real, source text);
create virtual table if not exists facts_fts using fts5(text);
create table if not exists episodes(day text primary key, summary text);
create table if not exists reminders(id integer primary key, due real, text text, done int default 0);
create table if not exists devices(addr text primary key, person text);
create table if not exists seen(key text primary key, ts real);
create table if not exists notifications(id integer primary key, ts real, kind text, text text);
create table if not exists taste(kind text primary key, score real);
create table if not exists held(id integer primary key, ts real, kind text, text text);
"""

# Signals that something is worth remembering. Weights add up; tuned by eye, not science.
_SIGNALS = [
    (r"\b(remind|remember|don'?t forget|promise[ds]?|deadline|appointment|meeting|due|owe|birthday|anniversary)\b", 0.35),
    (r"\b(i'?ll|i will|i'?m going to|we should|let'?s|need to|have to|must|gotta)\b", 0.25),
    (r"\b(tomorrow|tonight|today|monday|tuesday|wednesday|thursday|saturday|sunday|next week|next month|\d{1,2}(:\d\d)?\s?(am|pm))\b", 0.2),
    (r"\b(allergic|favou?rite|i hate|i love|i prefer|my (wife|husband|partner|son|daughter|mom|dad|boss|brother|sister|friend))\b", 0.25),
    (r"\$\d|\b\d{3}[-.\s]\d{3}[-.\s]?\d{4}\b", 0.15),
    (r"\b(password|address|phone number|email|flight|doctor|hospital)\b", 0.2),
]
_FILLER = re.compile(r"^\W*(ok(ay)?|yeah|yep|yes|no|nope|uh+|um+|hmm+|lol|haha|cool|nice|thanks|thank you|hi|hey|bye|sure|right)\W*$", re.I)


def importance(text):
    t = text.strip()
    if not t or _FILLER.match(t):
        return 0.0
    score = 0.1 + min(0.1, len(t.split()) / 200)
    low = t.lower()
    for pattern, weight in _SIGNALS:
        if re.search(pattern, low):
            score += weight
    return round(min(score, 1.0), 2)


CONSOLIDATE = (
    "You are the memory of a personal assistant. Below is part of today's raw transcript. "
    "Return ONLY JSON: {\"summary\": \"2-4 sentence account\", "
    "\"facts\": [{\"text\": \"durable fact about the owner or people in their life\", "
    "\"category\": \"person|preference|health|work|place|commitment|other\", \"importance\": 0.0-1.0}], "
    "\"reminders\": [{\"text\": \"...\", \"due\": \"ISO-8601 local datetime\"}]}. "
    "Only include facts still useful in a month. Skip small talk.\n\n")


def _chunks(lines, size):
    chunk = ""
    for line in lines:
        if chunk and len(chunk) + len(line) > size:
            yield chunk
            chunk = ""
        chunk += line[:size] + "\n"
    if chunk:
        yield chunk


def _fts_query(q):
    words = re.findall(r"\w+", q.lower())
    return " OR ".join(f'"{w}"' for w in words if len(w) > 2)


class Memory:
    def __init__(self, path, retain_days=30, keep_importance=0.5, record_others=False):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.retain_days, self.keep_importance, self.record_others = retain_days, keep_importance, record_others

    # --- transcripts -----------------------------------------------------
    def add_utterance(self, text, speaker="unknown", place="", addressed=False, ts=None):
        # Unenrolled speakers are kept only until the nightly pass summarizes them (consent laws).
        ephemeral = speaker != "owner" and not self.record_others
        cur = self.db.execute(
            "insert into utterances(ts, speaker, text, place, importance, addressed, ephemeral) values(?,?,?,?,?,?,?)",
            (ts or time.time(), speaker, text, place, importance(text), int(addressed), int(ephemeral)))
        self.db.execute("insert into utterances_fts(rowid, text) values(?, ?)", (cur.lastrowid, text))
        self.db.commit()
        return cur.lastrowid

    def recent(self, n=20):
        rows = self.db.execute("select * from utterances order by ts desc limit ?", (n,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    # --- facts -----------------------------------------------------------
    def remember(self, text, category="general", importance_=0.7, source="told"):
        cur = self.db.execute(
            "insert or ignore into facts(ts, category, text, importance, source) values(?,?,?,?,?)",
            (time.time(), category, text.strip(), importance_, source))
        if cur.rowcount:
            self.db.execute("insert into facts_fts(rowid, text) values(?, ?)", (cur.lastrowid, text.strip()))
        self.db.commit()

    def profile(self, n=25):
        rows = self.db.execute("select category, text from facts order by importance desc, ts desc limit ?", (n,))
        return [f"[{r['category']}] {r['text']}" for r in rows]

    def search(self, q, n=6):
        fq = _fts_query(q)
        if not fq:
            return []
        facts = self.db.execute(
            "select f.text, f.category from facts_fts join facts f on f.id = facts_fts.rowid "
            "where facts_fts match ? order by rank limit ?", (fq, n)).fetchall()
        talk = self.db.execute(
            "select u.text, u.ts, u.speaker from utterances_fts join utterances u on u.id = utterances_fts.rowid "
            "where utterances_fts match ? order by rank limit ?", (fq, n)).fetchall()
        eps = self.db.execute("select day, summary from episodes where summary like ? order by day desc limit 3",
                              (f"%{q.split()[0] if q.split() else ''}%",)).fetchall()
        out = [f"fact: {r['text']}" for r in facts]
        out += [f"{datetime.fromtimestamp(r['ts']):%b %d %H:%M} {r['speaker']}: {r['text']}" for r in talk]
        out += [f"day {r['day']}: {r['summary']}" for r in eps]
        return out

    # --- reminders / devices / dedupe ------------------------------------
    def add_reminder(self, text, due_ts):
        self.db.execute("insert into reminders(due, text) values(?, ?)", (due_ts, text))
        self.db.commit()

    def due_reminders(self, now=None):
        now = now or time.time()
        rows = self.db.execute("select id, text from reminders where done = 0 and due <= ?", (now,)).fetchall()
        self.db.executemany("update reminders set done = 1 where id = ?", [(r["id"],) for r in rows])
        self.db.commit()
        return [r["text"] for r in rows]

    def enroll(self, addr, person):
        self.db.execute("insert or replace into devices values(?, ?)", (addr.lower(), person))
        self.db.commit()

    def devices(self):
        return {r["addr"]: r["person"] for r in self.db.execute("select * from devices")}

    def first_time(self, key):
        """True once per key; used to avoid repeating the same alert."""
        cur = self.db.execute("insert or ignore into seen values(?, ?)", (key, time.time()))
        self.db.commit()
        return cur.rowcount == 1

    def log_notification(self, kind, text):
        self.db.execute("insert into notifications(ts, kind, text) values(?,?,?)", (time.time(), kind, text))
        self.db.commit()

    def notifications(self, n=20):
        return [dict(r) for r in self.db.execute("select * from notifications order by ts desc limit ?", (n,))]

    # --- the learning loop: how much the owner wants each kind of interruption --------
    def taste(self, kind):
        row = self.db.execute("select score from taste where kind = ?", (kind,)).fetchone()
        return row["score"] if row else 0.6

    def learn(self, kind, liked, rate=0.3):
        """Move the score toward 1 (engaged) or 0 (pushed back). Returns the new score."""
        score = round(self.taste(kind) + rate * ((1.0 if liked else 0.0) - self.taste(kind)), 3)
        self.db.execute("insert or replace into taste values(?, ?)", (kind, score))
        self.db.commit()
        return score

    def tastes(self):
        return {r["kind"]: r["score"] for r in self.db.execute("select * from taste")}

    def hold(self, kind, text):
        self.db.execute("insert into held(ts, kind, text) values(?,?,?)", (time.time(), kind, text))
        self.db.commit()

    def release(self):
        rows = self.db.execute("select kind, text from held order by ts").fetchall()
        self.db.execute("delete from held")
        self.db.commit()
        return [(r["kind"], r["text"]) for r in rows]

    # --- the nightly "sleep" pass ------------------------------------------
    async def consolidate(self, llm, model=None, now=None, chunk_chars=6000):
        now = now or time.time()
        rows = self.db.execute("select * from utterances where consolidated = 0 and ts < ? order by ts",
                               (now,)).fetchall()
        if not rows:
            return None
        day = datetime.fromtimestamp(rows[0]["ts"]).strftime("%Y-%m-%d")
        lines = [f"{datetime.fromtimestamp(r['ts']):%H:%M} [{r['speaker']}|imp {r['importance']}] {r['text']}"
                 for r in rows]
        data = {"summary": "", "facts": [], "reminders": []}
        for chunk in _chunks(lines, chunk_chars):  # small context windows: digest the day in bites
            part = parse_json(await llm.text(CONSOLIDATE + chunk, model=model, max_tokens=1000)) or {}
            data["summary"] = " ".join(x for x in (data["summary"], part.get("summary") or "") if x)
            data["facts"] += part.get("facts") or []
            data["reminders"] += part.get("reminders") or []
        if data["summary"]:
            self.db.execute("insert or replace into episodes values(?, coalesce((select summary from episodes "
                            "where day = ?) || ' ', '') || ?)", (day, day, data["summary"]))
        for f in data["facts"]:
            if isinstance(f, dict) and f.get("text"):
                self.remember(f["text"], f.get("category", "other"), float(f.get("importance", 0.6)), "consolidated")
        for r in data["reminders"]:
            try:
                self.add_reminder(r["text"], datetime.fromisoformat(r["due"]).timestamp())
            except (KeyError, ValueError, TypeError):
                pass
        ids = [(r["id"],) for r in rows]
        self.db.executemany("update utterances set consolidated = 1 where id = ?", ids)
        self._purge(now)
        self.db.commit()
        return data

    def _purge(self, now):
        cutoff = now - self.retain_days * 86400
        doomed = self.db.execute(
            "select id from utterances where consolidated = 1 and (ephemeral = 1 or (ts < ? and importance < ?))",
            (cutoff, self.keep_importance)).fetchall()
        for (i,) in doomed:
            self.db.execute("delete from utterances where id = ?", (i,))
            self.db.execute("delete from utterances_fts where rowid = ?", (i,))
