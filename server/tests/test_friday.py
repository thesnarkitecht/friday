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
from friday.cli import FRAME, Segmenter
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

    async def chat(self, system, messages, tools=(), effort="low", max_tokens=4000, on_text=None):
        self.sent.append({"system": system, "messages": list(messages), "tools": list(tools)})
        r = self.replies.pop(0) if self.replies else "ok"
        if isinstance(r, str):
            for word in r.split(" ") if on_text else []:  # stream it word by word, like Claude
                await on_text(word + " ")
            return {"stop": "end_turn", "content": [{"type": "text", "text": r}], "text": r, "tool_uses": []}
        content = [{"type": "tool_use", **u} for u in r["tool_uses"]]
        return {"stop": "tool_use", "content": content, "text": "", "tool_uses": r["tool_uses"]}

    async def decide(self, prompt, schema, effort="low", max_tokens=4000):
        self.sent.append({"prompt": prompt})
        r = self.replies.pop(0) if self.replies else None
        return schema.model_validate(r) if isinstance(r, dict) else r


def until_said(ws):
    """Read a reply: spoken sentence parts, then the full text. Returns (parts, final say message)."""
    parts = []
    while (m := ws.receive_json())["type"] == "say_part":
        parts.append(m)
    assert m["type"] == "say"
    return parts, m


def use(name, **inp):
    return {"tool_uses": [{"id": "t1", "name": name, "input": inp}]}


def settings(tmp_path, **kw):
    s = config.load()
    s.token, s.db, s.owner = "secret", str(tmp_path / "f.db"), "Ryan"
    s.skills_dir, s.plugin_roots = str(tmp_path / "skills"), [str(tmp_path / "plugins")]
    s.quiet_hours = (0, 0)  # tests run at any hour; quiet hours have their own test
    s.greet_after_h = 0  # greetings have their own test
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
    with pytest.raises(ValueError):
        m.forget("a")  # would match nearly everything
    assert m.forget("NEAR(") == ['He said "NEAR(" AND OR *'] and m.profile() == []


# --- addressee ------------------------------------------------------------------
@pytest.mark.parametrize("text,verdict", [
    ("Hey Friday, what's on my calendar", "yes"),
    ("friday turn the lights off", "yes"),
    ("see you on Friday", "no"),
    ("Friday night we're going out", "no"),
    ("pass the salt", "no"),
    ("remind me to call the dentist", "maybe"),  # alone + sounds like a request: Claude decides
    ("what's the latest on that new model", "maybe"),
    ("I think it'll rain", "no"),
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
                parts, say = until_said(ws)
                assert [p["text"] for p in parts] == ["Noted."] and say["text"] == "Noted." and say["spoken"]
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
    mod = module("friday/builtin_plugins/models/plugin.py")
    data = {"data": [{"id": "anthropic/claude-x", "name": "Claude X"}, {"id": "openai/gpt-9:free"},
                     {"id": "randolab/thing"}]}
    assert mod.new_models(data, {"anthropic", "openai"}) == [
        {"key": "anthropic/claude-x", "kind": "models", "text": "New model just showed up: Claude X."}]


def test_hackernews_plugin_matches_whole_words_with_traction():
    mod = module("friday/builtin_plugins/hackernews/plugin.py")
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
    shutil.copytree("friday/builtin_plugins/hermes", tmp_path / "plugins" / "hermes")
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
    seg, f = Segmenter(end_ms=90, min_ms=60), b"\0" * FRAME * 2
    assert seg.push(f, False) is None  # silence before speech is ignored
    out = [seg.push(f, s) for s in (True, True, True, False, False, False)]
    assert out[:-1] == [None] * 5 and len(out[-1]) == 6 * FRAME * 2
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


# --- OpenAI-compatible API + CLI --------------------------------------------------------
def test_openai_clients_can_talk_to_friday(tmp_path):
    llm = FakeLLM("Nothing new from the big labs today.", "Second answer.")
    app = create_app(settings(tmp_path), llm=llm, background=False)
    msgs = [{"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": [{"type": "text", "text": "Did a new model drop?"}]}]
    with TestClient(app) as c:
        assert c.get("/v1/models").status_code == 401
        assert c.get("/v1/models", headers=H).json()["data"][0]["id"] == "friday"
        r = c.post("/v1/chat/completions", headers=H, json={"model": "friday", "messages": msgs}).json()
        assert r["object"] == "chat.completion"
        assert r["choices"][0]["message"] == {"role": "assistant", "content": "Nothing new from the big labs today."}
        asyncio.run(app.state.friday.notify("hn", "Rust 2.0 is out"))
        app.state.friday.memory.learn("hn", liked=False)  # pretend it's held
        stream = c.post("/v1/chat/completions", headers=H, json={"messages": msgs, "stream": True}).text
        events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: {")]
        assert events[0]["choices"][0]["delta"]["content"].startswith("Second answer.")
        assert events[-1]["choices"][0]["finish_reason"] == "stop" and stream.rstrip().endswith("data: [DONE]")
        assert c.post("/v1/chat/completions", headers=H, json={"messages": []}).status_code == 400
    assert llm.sent[0]["messages"][-1]["content"].endswith("Did a new model drop?")


def test_cli_drives_friday(tmp_path, monkeypatch, capsys):
    from friday import cli
    app = create_app(settings(tmp_path), llm=FakeLLM("Two new models today."), background=False)
    with TestClient(app, headers=H) as c:
        monkeypatch.setattr(cli, "client", lambda **kw: c)
        cli.main(["ask", "anything", "new?"])
        assert "Two new models today." in capsys.readouterr().out
        app.state.friday.addressee.last_spoke = 0  # follow-up window closed
        cli.main(["heard", "pass the salt"])
        assert "not for FRIDAY" in capsys.readouterr().out
        app.state.friday.memory.remember("Rust", "interest")
        cli.main(["memory"])
        assert "[interest] Rust" in capsys.readouterr().out
        cli.main(["forget", "Rust"])
        assert "forgot: Rust" in capsys.readouterr().out
        cli.main(["status"])
        out = capsys.readouterr().out
        assert "Situation" in out and "Skills" in out
        with pytest.raises(SystemExit):
            cli.main(["forget", "a"])  # refused by the server, reported, not a traceback


def test_quiet_hours_hold_non_urgent_things_overnight(tmp_path):
    f = create_app(settings(tmp_path, quiet_hours=(22, 8)), llm=FakeLLM(), background=False).state.friday
    late, morning, noon = (datetime(2030, 1, 1, h).timestamp() for h in (23, 7, 12))
    assert f.quiet_hours(late) and f.quiet_hours(morning) and not f.quiet_hours(noon)
    f.settings.quiet_hours = (0, 0)
    assert not f.quiet_hours(late)


@pytest.mark.parametrize("empty_profile_dir", [False, True])
def test_no_claude_credentials_at_all_is_spoken(tmp_path, monkeypatch, empty_profile_dir):
    from friday.llm import Claude, Unavailable
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no `ant auth login` profile
    if empty_profile_dir:  # compose mounted ~/.config/anthropic before anyone signed in
        monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path))
    with pytest.raises(Unavailable, match="not signed in"):
        asyncio.run(Claude().chat("s", [{"role": "user", "content": "hi"}]))



# --- any device with headphones ----------------------------------------------------------
class FakeVoice:
    def __init__(self):
        self.heard = []

    def available(self):
        return True

    def transcribe(self, pcm):
        self.heard.append(len(pcm))
        return "Friday, did a new model drop?"

    def speak(self, text):
        return b"RIFF" + text.encode()


def test_a_headphone_device_talks_and_hears_friday(tmp_path):
    import base64
    voice = FakeVoice()
    app = create_app(settings(tmp_path), llm=FakeLLM("Yes, two this morning."), background=False, voice=voice)
    with TestClient(app) as c:
        with c.websocket_connect("/ws?token=secret") as phone, c.websocket_connect("/ws?token=secret") as laptop:
            phone.receive_json(); laptop.receive_json()
            phone.send_json({"type": "hello", "voice": True})
            assert phone.receive_json() == {"type": "ready", "voice": True}
            phone.send_json({"type": "audio", "pcm": "not base64!!"})  # ignored, connection survives
            phone.send_json({"type": "audio", "pcm": base64.b64encode(b"\0" * 3200).decode()})
            assert phone.receive_json()["text"] == "Friday, did a new model drop?"
            parts, say = until_said(phone)
            assert [base64.b64decode(p["audio"]) for p in parts] == [b"RIFFYes, two this morning."]
            assert "audio" not in say  # already spoken sentence by sentence
            laptop.receive_json()
            assert all("audio" not in p for p in until_said(laptop)[0])  # text-only device gets text
    assert voice.heard == [3200]


def test_server_without_speech_models_says_so(tmp_path):
    class NoVoice(FakeVoice):
        def available(self):
            return False
    app = create_app(settings(tmp_path), llm=FakeLLM(), background=False, voice=NoVoice())
    with TestClient(app) as c, c.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "hello", "voice": True})
        assert ws.receive_json()["voice"] is False


def test_typing_while_listening_always_reaches_friday(tmp_path):
    app = create_app(settings(tmp_path), llm=FakeLLM("On it."), background=False)
    with TestClient(app) as c, c.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "ask", "text": "look that up"})  # no wake word needed when typed
        assert ws.receive_json() == {"type": "heard", "text": "look that up", "addressed": True}
        assert until_said(ws)[1]["text"] == "On it."


def test_friday_knows_what_you_were_just_talking_about(tmp_path):
    llm = FakeLLM("It's a solid pick for the Pi.")
    app = create_app(settings(tmp_path), llm=llm, background=False)
    f = app.state.friday
    f.memory.add_utterance("should we use Qwen 3 or Llama for the Pi", speaker="unknown", ts=time.time() - 60)
    f.memory.add_utterance("something from yesterday", speaker="unknown", ts=time.time() - 86400)
    with TestClient(app) as c:
        c.post("/api/utterance", json={"text": "Friday, what do you think?"}, headers=H)
    ctx = llm.sent[0]["messages"][-1]["content"]
    assert "should we use Qwen 3 or Llama for the Pi" in ctx and "yesterday" not in ctx


def test_login_saves_url_and_token_privately(tmp_path, monkeypatch, capsys):
    import os
    import stat
    from friday import cli
    monkeypatch.setattr(cli, "CONFIG", str(tmp_path / "friday" / "config.json"))
    monkeypatch.delenv("FRIDAY_TOKEN", raising=False)
    monkeypatch.delenv("FRIDAY_URL", raising=False)
    app = create_app(settings(tmp_path), llm=FakeLLM(), background=False)
    with TestClient(app) as tc:
        monkeypatch.setattr(cli.httpx, "Client", lambda **kw: TestClient(app, headers=kw.get("headers")))
        cli.main(["login", "http://testserver", "secret"])
    assert stat.S_IMODE(os.stat(cli.CONFIG).st_mode) == 0o600
    assert cli.login_settings() == ("http://testserver", "secret")
    with pytest.raises(SystemExit):
        cli.main(["login", "http://testserver", "wrong"])  # a bad token is never saved
    assert cli.login_settings()[1] == "secret"


def test_friday_speaks_each_sentence_as_soon_as_it_is_written(tmp_path):
    voice = FakeVoice()
    reply = "Two new models dropped. One is from Anthropic! Want details?"
    app = create_app(settings(tmp_path), llm=FakeLLM(reply), background=False, voice=voice)
    with TestClient(app) as c, c.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "hello", "voice": True}); ws.receive_json()
        ws.send_json({"type": "ask", "text": "anything new?"}); ws.receive_json()
        parts, say = until_said(ws)
    assert [p["text"] for p in parts] == ["Two new models dropped.", "One is from Anthropic!", "Want details?"]
    assert say["text"] == reply


def test_talking_over_friday_stops_it(tmp_path):
    gate = asyncio.Event()

    class Slow(FakeLLM):
        async def chat(self, system, messages, tools=(), effort="low", max_tokens=4000, on_text=None):
            await on_text("First sentence. Sec")
            await gate.wait()  # still writing when the owner cuts in
            await on_text("ond sentence. Third.")
            return {"stop": "end_turn", "content": [], "text": "First sentence. Second sentence. Third.", "tool_uses": []}

    app = create_app(settings(tmp_path), llm=Slow(), background=False)
    f = app.state.friday
    with TestClient(app) as c, c.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "ask", "text": "tell me a story"}); ws.receive_json()
        first = ws.receive_json()
        assert first == {"type": "say_part", "text": "First sentence."}
        ws.send_json({"type": "interrupt"})
        while not f.hush:
            time.sleep(0.01)
        c.portal.call(gate.set)
        parts, say = until_said(ws)
        assert parts == [] and say["text"] == "First sentence. Second sentence. Third."



def test_one_install_runs_friday_right_here(tmp_path, monkeypatch, capsys):
    """No server, no login: `friday ask` starts FRIDAY in-process (real HTTP server thread) and answers."""
    from friday import app as app_module, cli, voice as voice_module
    monkeypatch.setattr(cli, "CONFIG", str(tmp_path / "config.json"))
    for k in ("FRIDAY_URL", "FRIDAY_TOKEN", "FRIDAY_DB", "FRIDAY_SKILLS_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(app_module, "Claude", lambda model: FakeLLM("Hello from your own machine."))
    monkeypatch.setattr(voice_module.Voice, "available", lambda self: True)  # skip the voice download
    cli.main(["ask", "are you there?"])
    assert "Hello from your own machine." in capsys.readouterr().out
    assert (tmp_path / "home" / "friday.db").exists()
    assert (tmp_path / "home" / "skills" / "morning-briefing" / "SKILL.md").exists()  # starter skills seeded
    token = json.load(open(cli.CONFIG))["local_token"]
    assert len(token) > 20 and cli.login_settings() is None  # local mode, private token kept



# --- personality ------------------------------------------------------------------------------
def test_friday_greets_you_when_you_put_headphones_on(tmp_path):
    llm = FakeLLM("Morning. Coffee first, or the news?")
    app = create_app(settings(tmp_path, greet_after_h=4), llm=llm, background=False, voice=FakeVoice())
    f = app.state.friday
    f.memory.hold("models", "New model just showed up: Claude X.")
    with TestClient(app) as c, c.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "hello", "voice": True})
        assert ws.receive_json()["type"] == "ready"
        parts, say = until_said(ws)
        assert say["text"] == "Morning. Coffee first, or the news?" and all(p["audio"] for p in parts)
        catch_up = ws.receive_json()
        assert catch_up["type"] == "notify" and "Claude X" in catch_up["text"]  # then what it held
    assert "just put their headphones on" in llm.sent[0]["messages"][-1]["content"]
    with TestClient(app) as c, c.websocket_connect("/ws?token=secret") as ws:  # back a minute later: no repeat
        ws.receive_json()
        ws.send_json({"type": "hello", "voice": True}); ws.receive_json()
        ws.send_json({"type": "ask", "text": "hi"})
        assert ws.receive_json()["type"] == "heard"


def test_friday_checks_in_on_plans_it_heard_and_shares_patterns(tmp_path):
    m = Memory(str(tmp_path / "m.db"))
    m.add_utterance("big interview tomorrow at 10, kind of nervous", speaker="owner", ts=time.time() - 60)
    llm = FakeLLM({"summary": "Interview prep.", "facts": [], "observation": "You've asked about GPUs five times "
                   "this week; want me to watch for price drops?", "reminders": [
                       {"text": "How did the interview go?", "due": "2030-01-01T12:00:00", "kind": "checkin"}]})
    asyncio.run(m.consolidate(llm))
    assert m.due_reminders(now=1.9e9) == []  # not a plain reminder
    assert m.due_reminders(now=1.9e9, kind="checkin") == ["How did the interview go?"]
    assert m.release() == [("insight", "You've asked about GPUs five times this week; want me to watch for "
                                       "price drops?")]


def test_a_reply_to_a_check_in_has_context(tmp_path):
    llm = FakeLLM("Knew you'd nail it.")
    app = create_app(settings(tmp_path), llm=llm, background=False)
    f = app.state.friday
    with TestClient(app) as c:
        asyncio.run(f.notify("checkin", "How did the interview go?"))
        c.post("/api/utterance", json={"text": "it went great"}, headers=H)  # no wake word: follow-up window
    msgs = llm.sent[0]["messages"]
    assert msgs[0]["role"] == "user"  # the API's rule, kept without losing FRIDAY's question
    assert msgs[-2] == {"role": "assistant", "content": "How did the interview go?"}
