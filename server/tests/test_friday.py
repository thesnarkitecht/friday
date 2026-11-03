import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from friday import config, plugins
from friday.addressee import Addressee
from friday.app import create_app
from friday.ears import FRAME_BYTES, Segmenter
from friday.memory import Memory, importance
from friday.proactive import tick
from friday.skills import Skills

H = {"Authorization": "Bearer secret"}


class FakeLLM:
    """Replays scripted replies; records what it was sent.
    chat(): a str is a final spoken reply; {"tool_uses": [...]} asks for tools.
    decide(): a dict is validated into the schema; None means Claude declined."""
    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    async def chat(self, system, messages, tools=(), effort="low", max_tokens=4000):
        self.sent.append({"system": system, "messages": list(messages), "tools": list(tools)})
        r = self.replies.pop(0) if self.replies else "ok"
        if isinstance(r, str):
            return {"stop": "end_turn", "content": [{"type": "text", "text": r}], "text": r, "tool_uses": []}
        content = [{"type": "tool_use", **u} for u in r["tool_uses"]]
        return {"stop": "tool_use", "content": content, "text": "", "tool_uses": r["tool_uses"]}

    async def decide(self, prompt, schema, effort="low", max_tokens=4000):
        self.sent.append({"prompt": prompt})
        r = self.replies.pop(0) if self.replies else None
        return schema.model_validate(r) if isinstance(r, dict) else r


def use(name, **inp):
    return {"tool_uses": [{"id": "t1", "name": name, "input": inp}]}


def settings(tmp_path, **kw):
    s = config.load()
    s.token, s.db, s.owner = "secret", str(tmp_path / "f.db"), "Ryan"
    s.skills_dir, s.plugins_dir = str(tmp_path / "skills"), str(tmp_path / "plugins")
    s.__dict__.update(kw)
    return s


def module(path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(path.replace("/", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_plugin(root, name, code):
    (root / name).mkdir(parents=True)
    (root / name / "plugin.py").write_text(code)


# --- memory -------------------------------------------------------------------
def test_importance_ranks_commitments_over_filler():
    assert importance("ok") == 0.0
    assert importance("remind me to call mom tomorrow at 5pm") > importance("the weather is kind of grey") > 0


def test_consolidation_keeps_what_matters_and_drops_the_rest(tmp_path):
    m = Memory(str(tmp_path / "m.db"), retain_days=0)
    m.add_utterance("I'm really into Rust", speaker="owner", ts=time.time() - 70)  # kept as an interest fact
    m.add_utterance("Don't forget the dentist appointment tomorrow at 5pm", speaker="owner", ts=time.time() - 60)
    m.add_utterance("uh yeah the thing", speaker="owner", ts=time.time() - 50)
    m.add_utterance("my number is 555 123 4567", speaker="unknown", ts=time.time() - 30)
    llm = FakeLLM({"summary": "Talked tech.", "facts": [
        {"text": "Rust", "category": "interest", "importance": 0.8}],
        "reminders": [{"text": "call mom", "due": "2030-01-01T09:00:00"}]})
    asyncio.run(m.consolidate(llm))
    assert m.interests() == ["Rust"]
    texts = [u["text"] for u in m.recent()]
    assert "Don't forget the dentist appointment tomorrow at 5pm" in texts  # scored worth keeping raw
    assert "uh yeah the thing" not in texts and not any("555" in t for t in texts)  # filler + others: gone
    assert m.due_reminders(now=1.9e9) == ["call mom"]


def test_consolidation_digests_long_days_in_chunks(tmp_path):
    m = Memory(str(tmp_path / "m.db"))
    for i in range(40):
        m.add_utterance(f"note number {i} about the garden project", speaker="owner", ts=time.time() - 100 + i)
    llm = FakeLLM(*[{"summary": f"part {i}.", "facts": [], "reminders": []} for i in range(10)])
    data = asyncio.run(m.consolidate(llm, chunk_chars=500))
    assert len(llm.sent) > 1 and all(len(s["prompt"]) < 2000 for s in llm.sent)
    assert data["summary"].startswith("part 0. part 1.")


def test_search_survives_fts_syntax_and_forget_works(tmp_path):
    m = Memory(str(tmp_path / "m.db"))
    m.remember('He said "NEAR(" AND OR *')
    assert m.search('"NEAR(" AND OR *')  # FTS operators are quoted as plain words, not parsed
    assert m.search("*") == []
    assert m.forget("NEAR(") == ['He said "NEAR(" AND OR *'] and m.profile() == []


# --- addressee ------------------------------------------------------------------
@pytest.mark.parametrize("text,verdict", [
    ("Hey Friday, what's on my calendar", "yes"),
    ("friday turn the lights off", "yes"),
    ("see you on Friday", "no"),
    ("Friday night we're going out", "no"),
    ("pass the salt", "no"),
])
def test_wake_word(text, verdict):
    assert Addressee().check(text, now=1000)[0] == verdict


def test_followup_window():
    a = Addressee(followup_s=10)
    a.spoke(now=1000)
    assert a.check("and tomorrow?", now=1005)[0] == "yes"
    assert a.check("and tomorrow?", others_present=True, now=1005)[0] == "maybe"
    assert a.check("and tomorrow?", now=1020)[0] == "no"


# --- app ----------------------------------------------------------------------------
def test_api_requires_token_and_answers_wake_word(tmp_path):
    llm = FakeLLM("Your 3pm moved to 4.")
    app = create_app(settings(tmp_path), llm=llm, background=False)
    with TestClient(app) as c:
        assert c.post("/api/utterance", json={"text": "hey friday"}).status_code == 401
        assert c.post("/api/utterance", json={"text": "pass the salt"}, headers=H).json() == {
            "addressed": False, "reply": None}
        r = c.post("/api/utterance", json={"text": "Friday, what's my afternoon look like?"}, headers=H).json()
        assert r == {"addressed": True, "reply": "Your 3pm moved to 4."}
    assert "Ryan" in llm.sent[0]["system"]
    assert {t["name"] for t in llm.sent[0]["tools"]} >= {"remember", "recall", "skill_view"}
    assert all("input_schema" in t for t in llm.sent[0]["tools"])  # Anthropic tool format


def test_tool_call_and_reply_reach_every_client(tmp_path):
    llm = FakeLLM(use("remember", text="local LLMs", category="interest"), "Noted.")
    app = create_app(settings(tmp_path), llm=llm, background=False)
    with TestClient(app) as c:
        with c.websocket_connect("/ws?token=secret") as a, c.websocket_connect("/ws?token=secret") as b:
            a.receive_json(); b.receive_json()
            a.send_json({"type": "utterance", "text": "Friday, I'm into local LLMs"})
            for ws in (a, b):
                assert ws.receive_json()["type"] == "heard"
                assert ws.receive_json() == {"type": "say", "text": "Noted."}
    assert app.state.friday.memory.interests() == ["local LLMs"]
    result = llm.sent[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"


def test_prompt_prefix_stays_identical_across_turns(tmp_path):
    llm = FakeLLM("One.", "Two.")
    app = create_app(settings(tmp_path), llm=llm, background=False)
    with TestClient(app) as c:
        c.post("/api/ask", json={"text": "first question"}, headers=H)
        c.post("/api/ask", json={"text": "second question"}, headers=H)
    first, second = llm.sent
    assert first["system"] == second["system"] and first["tools"] == second["tools"]
    assert second["messages"][0] == {"role": "user", "content": "first question"}  # history stays bare text
    assert "[context]" in second["messages"][-1]["content"]
    assert second["messages"][-1]["content"].endswith("second question")


# --- plugins + proactive -----------------------------------------------------------
FEED = '''
def setup(p):
    p.items = []
    @p.feed(every_s=600)
    async def watch():
        return p.items
    @p.tool("Echo.", x=("string", "x"))
    async def echo(x):
        return {"x": x}
'''


def test_plugins_load_tools_feeds_and_skills_and_survive_a_broken_one(tmp_path):
    root = tmp_path / "plugins"
    write_plugin(root, "good", FEED)
    write_plugin(root, "broken", "def setup(p):\n    raise RuntimeError('no key')\n")
    (root / "good" / "skills" / "do-thing").mkdir(parents=True)
    (root / "good" / "skills" / "do-thing" / "SKILL.md").write_text("---\nname: do-thing\ndescription: Does it\n---\n1.")
    app = create_app(settings(tmp_path), llm=FakeLLM(), background=False)
    f = app.state.friday
    assert [p.name for p in f.plugins] == ["good"]
    assert f.plugin_errors == ["broken: RuntimeError: no key"]
    assert asyncio.run(f.brain.tools.run("echo", {"x": "hi"})) == {"x": "hi"}
    assert "- do-thing: Does it" in f.brain.system


def test_feeds_learn_what_exists_first_then_announce_only_new_items(tmp_path):
    write_plugin(tmp_path / "plugins", "watcher", FEED)
    app = create_app(settings(tmp_path), llm=FakeLLM(), background=False)
    f = app.state.friday
    p = f.plugins[0]
    p.items = [{"key": "a", "kind": "models", "text": "Model A"}]
    f.memory.add_reminder("stand up", time.time() - 1)
    night = datetime(2030, 1, 1, 23).timestamp()  # outside question hours
    assert asyncio.run(tick(f, now=night)) == [("reminder", "Reminder: stand up")]  # feed seeded silently
    p.items.append({"key": "b", "kind": "models", "text": "Model B"})
    assert asyncio.run(tick(f, now=night + 300)) == []  # not due yet: every 10 minutes
    assert asyncio.run(tick(f, now=night + 600)) == [("models", "Model B")]
    assert asyncio.run(tick(f, now=night + 1200)) == []  # said once
    p.items += [{"key": f"x{i}", "kind": "models", "text": f"X{i}"} for i in range(5)]
    assert len(asyncio.run(tick(f, now=night + 1800))) == 3  # a burst is capped
    assert asyncio.run(tick(f, now=night + 2400)) == []  # the rest were marked seen, not queued


def test_a_feed_that_is_not_set_up_keeps_its_first_quiet_look(tmp_path):
    write_plugin(tmp_path / "plugins", "watcher", FEED)
    f = create_app(settings(tmp_path), llm=FakeLLM(), background=False).state.friday
    p, night = f.plugins[0], datetime(2030, 1, 1, 23).timestamp()
    p.items = None  # e.g. Hacker News before any interests exist
    asyncio.run(tick(f, now=night))
    p.items = [{"key": "a", "kind": "hn", "text": "A"}]
    assert asyncio.run(tick(f, now=night + 600)) == []  # this is the quiet first look


def test_models_plugin_filters_labs_and_variants():
    mod = module("plugins/models/plugin.py")
    data = {"data": [{"id": "anthropic/claude-x", "name": "Claude X"}, {"id": "openai/gpt-9:free"},
                     {"id": "randolab/thing"}]}
    assert mod.new_models(data, {"anthropic", "openai"}) == [
        {"key": "anthropic/claude-x", "kind": "models", "text": "New model just showed up: Claude X."}]


def test_hackernews_plugin_matches_whole_words_with_traction():
    mod = module("plugins/hackernews/plugin.py")
    hits = [{"objectID": "1", "title": "Rust 2.0 released", "points": 300},
            {"objectID": "2", "title": "Trusting trust revisited", "points": 300},
            {"objectID": "3", "title": "My Rust side project", "points": 4}]
    assert [m["key"] for m in mod.matches(hits, ["rust"])] == ["1"]


def test_hermes_plugin_hands_off_then_reports_back(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_URL", "http://hermes:8642/v1")
    monkeypatch.setenv("HERMES_KEY", "k")
    seen = {}

    def handler(request):
        seen["auth"], seen["body"] = request.headers["authorization"], json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Use uv.\nDetails here."}}]})

    import shutil
    shutil.copytree("plugins/hermes", tmp_path / "plugins" / "hermes")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(settings(tmp_path), llm=FakeLLM(), background=False, http=http)
    f = app.state.friday

    async def go():
        out = await f.brain.tools.run("ask_hermes", {"task": "how should I package this?"})
        await asyncio.gather(*(t for t in asyncio.all_tasks() if t is not asyncio.current_task()))
        return out

    assert asyncio.run(go())["status"].startswith("handed to Hermes")
    assert seen["auth"] == "Bearer k" and seen["body"]["model"] == "hermes-agent"
    assert f.memory.notifications()[0]["text"].startswith("Hermes is back on how should I package this?. Use uv.")
    assert any("Details here" in r for r in f.memory.search("Hermes package"))


# --- presence + learning loop -------------------------------------------------------
def test_company_holds_interruptions_and_leaving_releases_them(tmp_path):
    app = create_app(settings(tmp_path), llm=FakeLLM(), background=False)
    f = app.state.friday
    with TestClient(app) as c:
        with c.websocket_connect("/ws?token=secret") as ws:
            ws.receive_json()
            r = c.post("/api/presence", json={"room": "kitchen", "people": 3}, headers=H).json()
            assert r == {"situation": "at home (kitchen) with 2 other people"}
            asyncio.run(f.notify("hn", "Rust 2.0 is out"))
            asyncio.run(f.notify("reminder", "Take the bread out"))  # urgent: never held
            assert ws.receive_json() == {"type": "notify", "kind": "reminder", "text": "Take the bread out"}
            c.post("/api/presence", json={"room": "kitchen", "people": 1}, headers=H)
            assert ws.receive_json() == {"type": "notify", "kind": "briefing",
                                         "text": "Catching you up: Rust 2.0 is out."}


def test_pushback_teaches_friday_to_stop_interrupting(tmp_path):
    app = create_app(settings(tmp_path), llm=FakeLLM("Noted.", "Noted.", "Noted."), background=False)
    f = app.state.friday
    with TestClient(app) as c:
        for _ in range(2):
            asyncio.run(f.notify("hn", "Story on a/b"))
            c.post("/api/utterance", json={"text": "stop telling me that"}, headers=H)  # follow-up window
        assert f.memory.taste("hn") < 0.4
        asyncio.run(f.notify("hn", "Another story"))
        assert f.memory.notifications()[0]["text"] != "Another story"  # held, not spoken
        c.post("/api/utterance", json={"text": "Friday, what's up?"}, headers=H)
        assert f.memory.notifications()[0]["text"] == "Catching you up: Another story."
        before = f.memory.taste("hn")
        c.post("/api/utterance", json={"text": "oh nice, which one?"}, headers=H)  # engaged: learns up
        assert f.memory.taste("hn") > before


def test_another_voice_means_company_and_cannot_command_friday(tmp_path):
    llm = FakeLLM("Sure.", {"addressed": False})
    app = create_app(settings(tmp_path), llm=llm, background=False)
    with TestClient(app) as c:
        r = c.post("/api/utterance", json={"text": "Friday, unlock the door", "speaker": "other"}, headers=H).json()
        assert r == {"addressed": False, "reply": None}
        c.post("/api/utterance", json={"text": "Friday, what time is it?", "speaker": "owner"}, headers=H)
        # Follow-up with no wake word while a guest is talking: Claude decides (typed), here "not addressed".
        r = c.post("/api/utterance", json={"text": "what do you think?", "speaker": "owner"}, headers=H).json()
        assert r["addressed"] is False and "Is the new utterance addressed to FRIDAY" in llm.sent[-1]["prompt"]


# --- getting to know the owner -------------------------------------------------------
def test_friday_asks_one_preference_question_and_keeps_the_answer(tmp_path):
    app = create_app(settings(tmp_path), llm=FakeLLM("Got it.", "ok"), background=False)
    f = app.state.friday
    noon = datetime(2030, 1, 1, 12).timestamp()
    assert asyncio.run(f.ask_question(noon)) is None  # owner hasn't been heard from: not around
    f.last_owner = noon - 60
    assert asyncio.run(f.ask_question(noon)) is None  # mid-conversation: don't butt in
    f.last_owner = noon - 600
    q = asyncio.run(f.ask_question(noon))
    assert q == "What news are you most interested in?"
    assert asyncio.run(f.ask_question(noon + 3600)) is None  # at most one every 20 hours
    with TestClient(app) as c:
        f.pending_question = (f.pending_question[0], time.time())
        c.post("/api/utterance", json={"text": "AI and space launches"}, headers=H)  # in the follow-up window
    assert any("What news are you most interested in" in p and "AI and space launches" in p for p in f.memory.profile())


def test_questions_never_touch_relationships(tmp_path):
    m = Memory(str(tmp_path / "m.db"))
    llm = FakeLLM({"questions": ["What sci-fi do you like?", "How is your relationship with your sister?",
                                 "Do you have kids?"]})
    asyncio.run(m.think_of_questions(llm))
    assert m.next_question()["text"] == "What sci-fi do you like?" and m.unanswered() == 1


# --- skills + routines ----------------------------------------------------------------
def test_skills_load_on_demand_and_reject_bad_names(tmp_path):
    sk = Skills(tmp_path / "skills")
    sk.save("order-pizza", "Ordering from Gino's", "1. recall Gino's order\n2. confirm total")
    assert sk.index() == "- order-pizza: Ordering from Gino's"
    assert "recall" in sk.view("order-pizza")
    for bad in ("../etc", "Has Space", ""):
        with pytest.raises(ValueError):
            sk.save(bad, "x", "y")
    (tmp_path / "skills" / "obsidian").mkdir()  # Hermes / agentskills.io format drops straight in
    (tmp_path / "skills" / "obsidian" / "SKILL.md").write_text(
        "---\nname: obsidian\ndescription: Read notes.\nversion: 1.0.0\nmetadata:\n  hermes:\n    tags: []\n---\n# Body")
    assert sk.view("obsidian") == "# Body"


def test_skill_index_is_in_the_cached_prefix_and_refreshes_only_on_purpose(tmp_path):
    f = create_app(settings(tmp_path), llm=FakeLLM(), background=False).state.friday
    f.skills.save("morning-brief", "What to cover in the morning", "1. recall today")
    assert "morning-brief" not in f.brain.system  # mid-day save: usable via skill_view, prefix untouched
    f.brain.refresh()
    assert "- morning-brief: What to cover in the morning" in f.brain.system


def test_nightly_review_turns_tool_use_into_skills(tmp_path):
    llm = FakeLLM(use("recall", query="today"), "Nothing today.",
                  {"skills": [{"name": "day-check", "description": "What's on today", "body": "1. recall today"},
                              {"name": "../bad", "description": "x", "body": "x"}]})
    app = create_app(settings(tmp_path), llm=llm, background=False)
    f = app.state.friday
    with TestClient(app) as c:
        c.post("/api/ask", json={"text": "anything today?"}, headers=H)
    assert f.brain.worklog[0]["tools"] == ['recall({"query": "today"})']
    assert asyncio.run(f.skills.review(llm, f.brain.worklog)) == ["day-check"]


def test_routines_run_once_per_day_on_their_days(tmp_path):
    m = Memory(str(tmp_path / "m.db"))
    m.add_routine("07:00", "weekdays", "brief me")
    with pytest.raises(ValueError):
        m.add_routine("7am", "daily", "x")
    mon_6, mon_8, sat_8 = (datetime(2026, 11, d, h).timestamp() for d, h in ((2, 6), (2, 8), (7, 8)))
    assert m.due_routines(mon_6) == []
    assert m.due_routines(mon_8) == ["brief me"]
    assert m.due_routines(mon_8 + 60) == []  # once a day
    assert m.due_routines(sat_8) == []


# --- ears ------------------------------------------------------------------------------
def test_segmenter_cuts_utterances_on_silence_and_drops_blips():
    seg, f = Segmenter(end_ms=90, min_ms=60), b"\0" * FRAME_BYTES
    assert seg.push(f, False) is None  # silence before speech is ignored
    out = [seg.push(f, s) for s in (True, True, True, False, False, False)]
    assert out[:-1] == [None] * 5 and len(out[-1]) == 6 * FRAME_BYTES
    out = [seg.push(f, s) for s in (True, False, False, False)]  # one 30 ms blip
    assert out == [None] * 4


def test_claude_outage_is_spoken_not_a_crash(tmp_path):
    from friday.llm import Unavailable

    class Down(FakeLLM):
        async def chat(self, *a, **k):
            raise Unavailable("I can't reach Claude right now.")

    app = create_app(settings(tmp_path), llm=Down(), background=False)
    with TestClient(app) as c:
        r = c.post("/api/utterance", json={"text": "Friday, hello"}, headers=H).json()
    assert r == {"addressed": True, "reply": "I can't reach Claude right now."}
