"""Long-term memory: every transcript lands here, gets an importance score, and a nightly
"sleep" pass turns the day into a summary plus durable facts. Low-value raw text decays."""
import re
import sqlite3
import time
from datetime import datetime

from pydantic import BaseModel

SCHEMA = """
create table if not exists utterances(id integer primary key, ts real, speaker text, text text,
    place text, importance real, addressed int, ephemeral int, consolidated int default 0);
create virtual table if not exists utterances_fts using fts5(text);
create table if not exists facts(id integer primary key, ts real, category text, text text unique,
    importance real, source text);
create virtual table if not exists facts_fts using fts5(text);
create table if not exists episodes(day text primary key, summary text);
create table if not exists reminders(id integer primary key, due real, text text, done int default 0,
    kind text default 'reminder');
create table if not exists devices(addr text primary key, person text);
create table if not exists seen(key text primary key, ts real);
create table if not exists notifications(id integer primary key, ts real, kind text, text text);
create table if not exists taste(kind text primary key, score real);
create table if not exists routines(id integer primary key, at text, days text, task text, last_day text);
create table if not exists questions(id integer primary key, text text unique, asked real, answer text);
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
    "You are the memory of a personal assistant that talks to its owner all day. Below is part of today's raw "
    "transcript. Summarize it in 2-4 sentences. Pull out durable facts about the owner still useful in a month "
    "(interests, preferences, work, places, commitments, and rapport: running jokes, nicknames, how they like "
    "to be talked to); skip small talk and other people's private matters. List reminders the owner asked for "
    "(kind 'reminder'), and check-ins: things they were planning, waiting on or nervous about that a good "
    "friend would ask about afterwards, phrased as that question, due a little after the event (kind "
    "'checkin', e.g. 'How did the interview go?'). Due times are ISO-8601 local. Finally, if you notice one "
    "pattern worth mentioning (something they keep asking about, a recurring frustration, a habit), write "
    "it as one short, friendly offer of help; otherwise leave it empty.\n\n")


class Fact(BaseModel):
    text: str
    category: str  # interest | preference | work | place | commitment | other
    importance: float


class Reminder(BaseModel):
    text: str
    due: str
    kind: str = "reminder"  # reminder | checkin


class Digest(BaseModel):
    summary: str
    facts: list[Fact]
    reminders: list[Reminder]
    observation: str = ""


QUESTIONS = (
    "You help a voice assistant get to know its owner's tastes. Here is what it knows and the questions it has "
    "already asked. Write 3 short, friendly new questions about preferences only (news topics, tech, hobbies, "
    "how they like answers, routines, food, music). Never ask about relationships, family, friends, health, "
    "money, religion or politics. Don't repeat anything already asked.\n\n")


class Questions(BaseModel):
    questions: list[str]


def _chunks(lines, size):
    chunk = ""
    for line in lines:
        if chunk and len(chunk) + len(line) > size:
            yield chunk
            chunk = ""
        chunk += line[:size] + "\n"
    if chunk:
        yield chunk


# Questions FRIDAY asks are about tastes, never people or private matters.
_PERSONAL = re.compile(r"\b(wife|husband|partner|girlfriend|boyfriend|dating|relationship|married|divorce|family|"
                       r"kids?|children|son|daughter|mom|mother|dad|father|parents?|sibling|brother|sister|friends?|"
                       r"ex|love life|health|medical|weight|salary|income|money|debt|religio\w*|politic\w*)\b", re.I)

SEED_QUESTIONS = [
    "What news are you most interested in?",
    "Which topics should I watch Hacker News for?",
    "Which AI labs or models do you want to hear about first when something new drops?",
    "Are you more of a short-answer or a full-explanation person when you ask me something?",
    "What time of day do you least want to be interrupted?",
    "What kind of music do you like to work to?",
    "Which programming languages and tools do you use most these days?",
]

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _fts_query(q):
    words = re.findall(r"\w+", q.lower())
    return " OR ".join(f'"{w}"' for w in words if len(w) > 2)


class Memory:
    def __init__(self, path, retain_days=30, keep_importance=0.5, record_others=False):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:  # databases from before check-ins existed
            self.db.execute("alter table reminders add column kind text default 'reminder'")
        except sqlite3.OperationalError:
            pass
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

    def forget(self, text):
        """Delete facts containing text. Returns what was forgotten."""
        if len(text.strip()) < 3:
            raise ValueError("say a bit more of what to forget (3+ characters)")  # "a" would match everything
        rows = self.db.execute("select id, text from facts where text like ?", (f"%{text.strip()}%",)).fetchall()
        for r in rows:
            self.db.execute("delete from facts where id = ?", (r["id"],))
            self.db.execute("delete from facts_fts where rowid = ?", (r["id"],))
        self.db.commit()
        return [r["text"] for r in rows]

    def interests(self):
        return [r["text"] for r in self.db.execute("select text from facts where category = 'interest'")]

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
    def add_reminder(self, text, due_ts, kind="reminder"):
        self.db.execute("insert into reminders(due, text, kind) values(?, ?, ?)", (due_ts, text, kind))
        self.db.commit()

    def due_reminders(self, now=None, kind="reminder"):
        """kind "checkin": things FRIDAY heard you planning and will ask about afterwards."""
        now = now or time.time()
        rows = self.db.execute("select id, text from reminders where done = 0 and due <= ? and kind = ?",
                               (now, kind)).fetchall()
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

    # --- routines: tasks the owner scheduled in plain words -----------------
    def add_routine(self, at, days, task):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
            raise ValueError("at must be HH:MM, 24-hour")
        days = days.lower().replace(" ", "")
        if days not in ("daily", "weekdays", "weekends") and not set(days.split(",")) <= set(_DAYS):
            raise ValueError("days: daily, weekdays, weekends, or a list like mon,wed,fri")
        cur = self.db.execute("insert into routines(at, days, task) values(?,?,?)", (at, days, task))
        self.db.commit()
        return cur.lastrowid

    def routines(self):
        return [dict(r) for r in self.db.execute("select id, at, days, task from routines order by at")]

    def remove_routine(self, rid):
        self.db.execute("delete from routines where id = ?", (rid,))
        self.db.commit()

    def due_routines(self, now=None):
        """Routines whose time has come today and haven't run today. Marks them run."""
        t = datetime.fromtimestamp(now or time.time())
        day, today = _DAYS[t.weekday()], t.strftime("%Y-%m-%d")
        due = []
        for r in self.db.execute("select * from routines").fetchall():
            on = {"daily": _DAYS, "weekdays": _DAYS[:5], "weekends": _DAYS[5:]}.get(r["days"], r["days"].split(","))
            if day in on and t.strftime("%H:%M") >= r["at"] and r["last_day"] != today:
                self.db.execute("update routines set last_day = ? where id = ?", (today, r["id"]))
                due.append(r["task"])
        self.db.commit()
        return due

    # --- getting to know the owner: preference questions, one at a time ------
    def add_questions(self, texts):
        for t in texts:
            if t.strip() and not _PERSONAL.search(t):  # preferences only, never relationships
                self.db.execute("insert or ignore into questions(text) values(?)", (t.strip(),))
        self.db.commit()

    def next_question(self):
        row = self.db.execute("select id, text from questions where asked is null order by id limit 1").fetchone()
        return dict(row) if row else None

    def last_asked(self):
        return self.db.execute("select max(asked) from questions").fetchone()[0] or 0

    def mark_asked(self, qid, now=None):
        self.db.execute("update questions set asked = ? where id = ?", (now or time.time(), qid))
        self.db.commit()

    def answer(self, qid, text):
        q = self.db.execute("select text from questions where id = ?", (qid,)).fetchone()
        self.db.execute("update questions set answer = ? where id = ?", (text, qid))
        self.db.commit()
        if q:
            self.remember(f"Asked \"{q['text']}\" they said: {text}", "preference", 0.8, "question")

    def unanswered(self):
        return self.db.execute("select count(*) from questions where asked is null").fetchone()[0]

    async def think_of_questions(self, llm):
        """Nightly: top up the question queue so FRIDAY keeps learning the owner's tastes."""
        if self.unanswered() >= 3:
            return []
        known = "\n".join(self.profile(15)) or "(nothing yet)"
        asked = "\n".join(self.asked_questions()) or "(none)"
        out = await llm.decide(f"{QUESTIONS}Known:\n{known}\n\nAlready asked:\n{asked}", Questions)
        self.add_questions(out.questions if out else [])
        return out.questions if out else []

    def asked_questions(self):
        return [r["text"] for r in self.db.execute("select text from questions")]

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
    async def consolidate(self, llm, now=None, chunk_chars=6000):
        now = now or time.time()
        rows = self.db.execute("select * from utterances where consolidated = 0 and ts < ? order by ts",
                               (now,)).fetchall()
        if not rows:
            return None
        day = datetime.fromtimestamp(rows[0]["ts"]).strftime("%Y-%m-%d")
        lines = [f"{datetime.fromtimestamp(r['ts']):%H:%M} [{r['speaker']}|imp {r['importance']}] {r['text']}"
                 for r in rows]
        data = {"summary": "", "facts": [], "reminders": [], "observations": []}
        for chunk in _chunks(lines, chunk_chars):  # small context windows: digest the day in bites
            part = await llm.decide(CONSOLIDATE + chunk, Digest, effort="medium")
            if part:
                data["summary"] = " ".join(x for x in (data["summary"], part.summary) if x)
                data["facts"] += [f.model_dump() for f in part.facts]
                data["reminders"] += [r.model_dump() for r in part.reminders]
                data["observations"] += [part.observation.strip()] if part.observation.strip() else []
        if data["summary"]:
            self.db.execute("insert or replace into episodes values(?, coalesce((select summary from episodes "
                            "where day = ?) || ' ', '') || ?)", (day, day, data["summary"]))
        for f in data["facts"]:
            if f.get("text"):
                self.remember(f["text"], f.get("category", "other"), float(f.get("importance", 0.6)), "consolidated")
        for r in data["reminders"]:
            try:
                kind = r.get("kind") if r.get("kind") in ("reminder", "checkin") else "reminder"
                self.add_reminder(r["text"], datetime.fromisoformat(r["due"]).timestamp(), kind)
            except (KeyError, ValueError, TypeError):
                pass
        if data["observations"]:
            self.hold("insight", data["observations"][0])  # one pattern a day, at the next good moment
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
