"""End-to-end tests. System 2 is a scripted OpenAI-compatible server reached through the real SDK;
System 1 is faked where a test needs a specific decision and real where it can run offline."""

import json
import math
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class FakeLLM(ThreadingHTTPServer):
    """Replies from `script` (a list, or a function of the request body). Records every request."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script, self.requests = [], []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}/v1"


def say(content=None, calls=()):
    return {"content": content, "calls": list(calls)}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.server.requests.append(body)
        script = self.server.script
        reply = script(body) if callable(script) else script.pop(0)
        if reply == 500:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "boom"}}')
            return
        calls = [
            {"index": i, "id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(reply["calls"])
        ]
        usage = {"prompt_tokens": reply.get("tokens", 10), "completion_tokens": 1, "total_tokens": 11}
        base = {"id": "x", "created": 0, "model": body["model"]}
        self.send_response(200)
        if body.get("stream"):
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            deltas = [{"role": "assistant", "content": ""}]
            deltas += [{"content": w} for w in re.findall(r"\S+\s*", reply["content"] or "")]
            deltas += [{"tool_calls": [c]} for c in calls]
            chunks = [{"choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
            chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if calls else "stop"}]})
            chunks.append({"choices": [], "usage": usage})
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps({**base, 'object': 'chat.completion.chunk', **chunk})}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            message = {"role": "assistant", "content": reply["content"], **({"tool_calls": calls} if calls else {})}
            out = {**base, "object": "chat.completion", "usage": usage,
                   "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(out).encode())


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path / "palace"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    from friday import config, skills

    for module in (config, skills):
        monkeypatch.setattr(module, "HOME", tmp_path / "home")
    import friday.agent, friday.bus, friday.proactive

    monkeypatch.setattr(friday.agent, "HOME", tmp_path / "home")
    monkeypatch.setattr(friday.bus, "HOME", tmp_path / "home")
    monkeypatch.setattr(friday.proactive, "HOME", tmp_path / "home")
    monkeypatch.setattr(skills, "dirs", lambda: [tmp_path / "home" / "skills"])
    (tmp_path / "home").mkdir()
    return tmp_path


@pytest.fixture
def llm():
    server = FakeLLM()
    yield server
    server.shutdown()


class NoMemory:
    honcho_on, errors = False, {}

    def __init__(self):
        self.stored = []

    def recall(self, query, limit=4):
        return ""

    def remember(self, speaker, text):
        self.stored.append((speaker, text))

    def wake_up(self):
        return ""

    def flush(self):
        pass


def make_agent(home, llm, decisions=None, act=None, **kw):
    """An agent on the fake model. `decisions` are System 1 answers by question name."""
    from friday import agent as agent_module
    from friday.config import DEFAULTS
    from friday.system1 import Answer

    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["honcho"]["base_url"] = ""
    cfg["models"] = {t: {"model": f"{t}-model", "base_url": llm.url} for t in ("fast", "main", "deep")}
    a = agent_module.Agent.__new__(agent_module.Agent)
    orig = agent_module.Memory
    agent_module.Memory = lambda *_: NoMemory()
    try:
        a.__init__(cfg, yolo=kw.pop("yolo", True), **kw)
    finally:
        agent_module.Memory = orig
    a.out = lambda line: a.lines.append(line)
    a.lines = []
    decisions = {"tier": ("main", 0.9), "web": (False, 0.9), "desktop": (False, 0.9), "agents": (False, 0.9), **(decisions or {})}

    def decide(state, questions, emulate=True):
        return {k: Answer(*decisions[k]) if k in decisions else Answer(q.get("default")) for k, q in questions.items()}

    a.s1.decide = decide
    a.s1.act = act or (lambda text, tools, system=None: ([], 0.0))
    return a


def finish(agent):
    agent._learner.join()


# -- the loop -----------------------------------------------------------------------------


def test_system2_uses_tools_then_answers(home, llm):
    llm.script = [say(calls=[("write_file", {"path": "a.txt", "content": "hi"})]), say("done")]
    agent = make_agent(home, llm)
    assert agent.turn("make a.txt") == "done"
    assert (home / "a.txt").read_text() == "hi"
    assert llm.requests[0]["model"] == "main-model"
    # Tool groups System 1 ruled out never reach the model's context.
    offered = {t["function"]["name"] for t in llm.requests[0]["tools"]}
    assert "shell" in offered and not offered & {"browse", "use_computer", "delegate"}
    finish(agent)


def test_reflex_needs_no_system2(home, llm):
    (home / "notes.txt").write_text("remember the milk")
    act = lambda text, tools, system=None: ([{"name": "read_file", "arguments": {"path": "notes.txt"}}], 0.9)
    agent = make_agent(home, llm, {"tier": ("reflex", 0.95)}, act)
    assert agent.turn("read notes.txt") == "remember the milk"
    assert llm.requests == [] and agent.last_tier == "reflex"
    finish(agent)


def test_unsure_reflex_goes_to_system2(home, llm):
    llm.script = [say("thought about it")]
    act = lambda text, tools, system=None: ([{"name": "shell", "arguments": {"command": "rm -rf /"}}], 0.2)
    agent = make_agent(home, llm, {"tier": ("reflex", 0.95)}, act)
    assert agent.turn("hmm") == "thought about it"
    assert llm.requests[0]["model"] == "fast-model"
    finish(agent)


def test_low_confidence_route_falls_back_to_main(home, llm):
    llm.script = [say("ok")]
    agent = make_agent(home, llm, {"tier": ("deep", 0.3)})
    agent.turn("x")
    assert llm.requests[0]["model"] == "main-model"
    finish(agent)


def test_failing_model_escalates_a_tier(home, llm):
    llm.script = lambda body: 500 if body["model"] == "fast-model" else say("from main")
    agent = make_agent(home, llm, {"tier": ("fast", 0.9)})
    from friday import models

    models._client.cache_clear()
    for tier in ("fast", "main", "deep"):  # no SDK retries, so the test is quick
        s = models.spec(agent.cfg, tier)
        models._client(s.get("base_url"), "OPENAI_API_KEY").max_retries = 0
    assert agent.turn("x") == "from main"
    assert any("escalating" in line for line in agent.lines)
    finish(agent)


def test_writes_are_denied_without_approval(home, llm):
    from friday.tools import DENIED

    llm.script = [say(calls=[("shell", {"command": "touch made"}), ("shell", {"command": "ls"})]), say("ok")]
    agent = make_agent(home, llm, yolo=False)
    agent.turn("x")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert results[0] == DENIED and results[1].startswith("exit 0")  # read-only commands need no approval
    assert not (home / "made").exists()
    finish(agent)


def test_read_only_allowlist_rejects_chained_commands():
    from friday.tools import _is_read_only

    assert _is_read_only("ls -la") and _is_read_only("rg TODO src")
    for sneaky in ("ls; rm -rf x", "cat a > b", "ls && rm x", "cat `rm x`", "rm x", "ls $(rm x)", "ls\nrm x"):
        assert not _is_read_only(sneaky)


def test_streaming_delivers_tokens_and_tool_calls(home, llm):
    llm.script = [say("let me", calls=[("write_file", {"path": "s.txt", "content": "x"})]), say("all done now")]
    agent = make_agent(home, llm)
    seen = []
    agent.stream = seen.append
    assert agent.turn("go") == "all done now"
    assert (home / "s.txt").exists() and "".join(t for t in seen if t) == "let meall done now"
    assert seen.count(None) == 2  # one end-of-message marker per model call
    finish(agent)


def test_compaction_keeps_tool_pairs_and_stores_verbatim(home, llm):
    llm.script = [say("one"), say(calls=[("recall", {"query": "q"})], ), say("summary of earlier work"), say("three")]
    llm.script[1]["tokens"] = 10**6  # the second turn's request is over budget
    agent = make_agent(home, llm)
    agent.cfg["context"]["keep_recent"] = 2
    agent.turn("first")
    finish(agent)
    assert agent.turn("second") == "three"
    assert agent.messages[0]["content"].startswith("[Earlier conversation, compacted")
    assert "summary of earlier work" in agent.messages[0]["content"]
    roles = [m["role"] for m in agent.messages]
    assert roles == ["user", "user", "assistant", "tool", "assistant"]  # the call kept its result
    assert any("[compacted context]" in text and "first" in text for _, text in agent.memory.stored)
    finish(agent)


# -- system 1 -----------------------------------------------------------------------------


def test_jev_answers_become_typed_decisions(home, monkeypatch):
    from types import SimpleNamespace as NS

    from friday.config import DEFAULTS
    from friday.system1 import S1

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    s1 = S1(DEFAULTS)
    sent = {}

    def system_one(state, questions):
        sent.update(questions)
        return NS(answers={
            "tier": NS(type="choice", choice="deep", confidence=0.8, probabilities={"deep": 0.8, "main": 0.2}),
            "web": NS(type="noul", noul=0.95),
            "nope": NS(type="noul", noul=0.1),
        })

    s1._jev = NS(system_one=system_one)
    out = s1.decide("x", {"tier": {"type": "choice", "criteria": {"deep": "", "main": ""}, "default": "main"},
                          "web": {"type": "noul"}, "nope": {"type": "noul"}, "missing": {"type": "noul", "default": True}})
    assert (out["tier"].value, out["tier"].probs["main"]) == ("deep", 0.2)
    assert out["web"].value is True and out["web"].confidence == pytest.approx(0.9)
    assert out["nope"].value is False and out["nope"].confidence == pytest.approx(0.8)
    assert out["missing"].value is True and out["missing"].confidence == 0.0  # unanswered: default
    assert "default" not in sent["tier"]  # Friday's own field never goes on the wire


def test_emulated_decisions_drop_invalid_answers(home):
    from friday.config import DEFAULTS
    from friday.system1 import S1

    s1 = S1(DEFAULTS, llm=lambda prompt: 'Sure: {"tier": "fast", "web": "maybe"}')
    out = s1.decide("x", {"tier": {"type": "choice", "criteria": {"fast": "", "main": ""}}, "web": {"type": "noul", "default": True}})
    assert out["tier"].value == "fast" and out["web"] .confidence == 0.0 and out["web"].value is True


def test_broken_system1_never_breaks_a_turn(home, llm):
    from friday.config import DEFAULTS
    from friday.system1 import S1

    def explode(prompt):
        raise RuntimeError("down")

    out = S1(DEFAULTS, llm=explode).decide("x", {"tier": {"type": "choice", "criteria": {"a": ""}, "default": "main"}})
    assert out["tier"].value == "main"


def test_needle_makes_a_real_tool_call():
    """The real on-device model. Skipped where its weights cannot be fetched."""
    from friday.config import DEFAULTS
    from friday.system1 import S1
    from friday.tools import schemas

    s1 = S1(DEFAULTS)
    calls, conf = s1.act("read the file notes.txt", schemas({"core"}))
    if not calls and s1.last_error:
        pytest.skip(f"Needle unavailable: {s1.last_error}")
    assert calls[0]["name"] == "read_file" and calls[0]["arguments"]["path"] == "notes.txt"
    assert 0 < conf <= 1


# -- skills -------------------------------------------------------------------------------


def write_skill(home, name, description):
    d = home / "home" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\nSteps for {name}.\n")


def test_jev_ranks_skills_and_the_best_ones_reach_the_prompt(home, llm):
    from friday.system1 import Answer

    for name in ("deploy", "release-notes", "unrelated"):
        write_skill(home, name, f"how to {name}")
    llm.script = [say("shipped")]
    agent = make_agent(home, llm)
    ranked = Answer("deploy", 0.7, {"deploy": 0.55, "release-notes": 0.3, "unrelated": 0.05, "none": 0.1})
    base = agent.s1.decide
    agent.s1.decide = lambda state, qs, emulate=True: {**base(state, qs), **({"skills": ranked} if "skills" in qs else {})}
    agent.turn("ship it")
    system = llm.requests[0]["messages"][0]["content"]
    assert "Steps for deploy." in system and "Steps for release-notes." in system
    assert "Steps for unrelated." not in system
    finish(agent)
    from friday import skills

    assert skills.scores() == {"deploy": [1, 1], "release-notes": [1, 1]}


def test_a_skill_that_keeps_losing_is_retired(home):
    from friday import skills

    write_skill(home, "flaky", "x")
    for won in (False, False, False, True):
        skills.record(["flaky"], won)
    assert skills.healthy(skills.scores()["flaky"])
    skills.record(["flaky"], False)  # 1 win in 5
    assert not skills.healthy(skills.scores()["flaky"])
    skills.reset("flaky")
    assert skills.healthy(skills.scores().get("flaky"))


def test_model_written_skill_cannot_escape_the_skills_folder(home):
    from friday import skills

    path = skills.save("```markdown\n---\nname: ../../etc/Evil Skill\ndescription: d\n---\nbody\n```")
    assert path == home / "home" / "skills" / "etc-evil-skill" / "SKILL.md"
    assert skills.save("no frontmatter at all") is None
    assert "etc-evil-skill" not in skills.load() and "../../etc/Evil Skill" in skills.load()


def test_multi_step_work_becomes_a_skill_and_a_lone_call_trains_needle(home, llm):
    three = [("shell", {"command": "ls"})] * 3
    llm.script = [say(calls=three), say("did it"),
                  say("---\nname: list-things\ndescription: when asked to list\n---\n1. ls\n")]
    agent = make_agent(home, llm, {"learn": ("new", 0.9)})
    agent.turn("list things three ways")
    finish(agent)
    assert (home / "home" / "skills" / "list-things" / "SKILL.md").exists()
    assert any("learned skill: list-things" in line for line in agent.lines)

    llm.script = [say(calls=[("shell", {"command": "pwd"})]), say("here")]
    agent.turn("where am I")
    finish(agent)
    rows = [json.loads(line) for line in (home / "home" / "train" / "needle.jsonl").read_text().splitlines()]
    assert rows == [{"text": "where am I", "tool_code": 'shell(command="pwd")'}]


# -- agents talking -----------------------------------------------------------------------


def test_bus_delivers_each_message_once(home):
    from friday.bus import Bus

    bus = Bus(home / "bus.db")
    bus.send("a", "b", "hello")
    bus.send("c", "b", "again")
    assert bus.inbox("b") == [("a", "hello"), ("c", "again")]
    assert bus.inbox("b") == [] and bus.inbox("a") == []


def test_delegate_runs_subagents_in_parallel(home, llm):
    def script(body):
        last = body["messages"][-1]
        if last["role"] == "tool":
            return say("merged")
        if "tools" in body and any(t["function"]["name"] == "delegate" for t in body["tools"]) and last["content"] == "split":
            return say(calls=[("delegate", {"tasks": ["task A", "task B"]})])
        return say(f"report on {last['content']}")

    llm.script = script
    agent = make_agent(home, llm, {"agents": (True, 0.9)})
    assert agent.turn("split") == "merged"
    report = next(m["content"] for m in agent.messages if m["role"] == "tool")
    assert "## friday.1\nreport on task A" in report and "## friday.2\nreport on task B" in report
    finish(agent)


# -- proactive ----------------------------------------------------------------------------


def test_watcher_fires_only_when_command_output_changes(home):
    from friday.proactive import due_events

    target = home / "watched"
    target.write_text("v1")
    watches = [{"name": "f", "every": 0, "cmd": f"cat {target}", "prompt": "changed"}]
    state = {}
    assert due_events(watches, state, 1.0) == []  # first look is the baseline
    assert due_events(watches, state, 2.0) == []
    target.write_text("v2")
    (event,) = due_events(watches, state, 3.0)
    assert event.startswith("changed") and "v2" in event
    assert due_events([{"name": "t", "every": 60, "prompt": "tick"}], state, 100.0) == ["tick"]
    assert due_events([{"name": "t", "every": 60, "prompt": "tick"}], state, 130.0) == []


def test_watch_lets_system1_skip_noise(home, llm):
    from friday.proactive import watch

    agent = make_agent(home, llm, {"act": (False, 0.95)})
    agent.cfg["watch"] = [{"name": "t", "every": 0, "prompt": "nothing happened"}]
    watch(agent, once=True)
    assert llm.requests == [] and any("skipped" in line for line in agent.lines)


# -- operating a screen -------------------------------------------------------------------


class FakeEnv:
    def __init__(self):
        self.done, self.closed = [], False

    def observe(self):
        return ["input: Search", "button: Go"], "a page"

    def do(self, name, args):
        self.done.append((name, args))

    def close(self):
        self.closed = True


def test_operate_uses_system1_when_sure_and_system2_when_not(home, llm, monkeypatch):
    from friday.operate import operate

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    steps = iter([
        {"done": (False, 0.9), "action": ("click", 0.9), "target": ("e1", 0.9)},   # sure: click Go
        {"done": (False, 0.9), "action": ("click", 0.3), "target": ("e0", 0.9)},   # unsure: ask System 2
        {"done": (True, 0.95), "action": ("stuck", 0.9), "target": ("e0", 0.1)},   # finished
    ])
    agent = make_agent(home, llm)
    from friday.system1 import Answer

    agent.s1.decide = lambda state, qs, emulate=True: {k: Answer(*v) for k, v in next(steps).items()}
    llm.script = [say(calls=[("type_text", {"element": 0, "text": "cats"})])]
    env = FakeEnv()
    result = operate(agent, env, "search for cats")
    assert env.done == [("click", {"element": 1}), ("type_text", {"element": 0, "text": "cats"})]
    assert env.closed and "untrusted" in result
    assert agent.stats["operate_s1"] == 2 and agent.stats["operate_s2"] == 1
    # The step System 2 had to take became training data for Needle.
    row = json.loads((home / "home" / "train" / "needle.jsonl").read_text())
    assert row["tool_code"] == 'type_text(element=0, text="cats")'


def test_real_browser_clicks_through_a_page(home, llm, tmp_path):
    pytest.importorskip("playwright")
    from friday.operate import Browser, operate

    (tmp_path / "a.html").write_text('<a href="b.html">Next page</a><button onclick="1">Other</button>')
    (tmp_path / "b.html").write_text("<h1>Arrived</h1>")
    os.environ.setdefault("FRIDAY_CHROMIUM", "/opt/pw-browsers/chromium")
    try:
        env = Browser(f"file://{tmp_path}/a.html")
    except Exception as e:
        pytest.skip(f"no browser here: {e}")
    assert env.observe()[0] == ["link: Next page", "button: Other"]
    llm.script = [say(calls=[("click", {"element": 0})]), say(calls=[("done", {"answer": "there"})])]
    result = operate(make_agent(home, llm), env, "go to the next page")
    assert result.startswith("there") and "Arrived" in result


def test_mirage_boxes_become_click_points():
    from friday.operate import elements

    seen = {"text": [{"string": "Sign in", "bbox": [100, 200, 300, 400]}], "objects": [{"name": "icon", "bbox": [0, 0, 1000, 1000]}]}
    points, labels = elements(seen, 2000, 1000)
    assert points == [(400, 300), (1000, 500)] and labels == ["text: Sign in", "object: icon"]


# -- the rest -----------------------------------------------------------------------------


def test_honcho_env_points_every_feature_at_fridays_model(monkeypatch):
    from friday.stack import FEATURES, honcho_env

    monkeypatch.setenv("MY_KEY", "sk-test")
    cfg = {"models": {"fast": {"model": "m", "base_url": "http://localhost:11434/v1", "api_key_env": "MY_KEY"}}, "honcho": {}}
    env = honcho_env(cfg)
    assert "LLM_OPENAI_API_KEY=sk-test" in env and len(FEATURES) == 9
    for feature in FEATURES:
        assert f"{feature}_MODEL_CONFIG__MODEL=m" in env
        assert f"{feature}_MODEL_CONFIG__OVERRIDES__BASE_URL=http://host.docker.internal:11434/v1" in env


def test_coin_shows_a_different_face_each_side_and_a_rim_edge_on():
    from friday.coin import frame

    lit = lambda theta: sum(ch != "⠀" for line in frame(theta) for ch in line.plain)
    front, back, edge = frame(0.0), frame(math.pi), frame(math.pi / 2)
    assert len(front) == 12 and all(len(line.plain) == 24 for line in front)
    assert [l.plain for l in front] != [l.plain for l in back]
    assert 0 < lit(math.pi / 2) < lit(0.0) / 2  # edge-on is a thin strip, but not nothing
    assert "142,162,255" in str(front[5].spans[12].style) and "245,180,84" in str(back[5].spans[12].style)


def test_run_prints_only_the_answer_on_stdout(home, llm, capsys, monkeypatch):
    from friday import cli, config

    (config.HOME / "config.toml").write_text(
        f'[models.main]\nmodel = "m"\nbase_url = "{llm.url}"\n[honcho]\nbase_url = ""\n'
    )
    monkeypatch.setattr("sys.stdin", type("Tty", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("friday.agent.Memory", lambda *_: NoMemory())
    llm.script = [say("42")]
    cli.main(["run", "what is six times seven"])
    out = capsys.readouterr()
    assert out.out == "42\n" and "plan → main" in out.err
