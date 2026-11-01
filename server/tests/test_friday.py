import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from friday import config
from friday.addressee import Addressee
from friday.app import create_app
from friday.calls import twilio_signature, twiml
from friday.feeds import parse_caltrans, parse_nyc, parse_tfl, parse_usgs
from friday.memory import Memory, importance
from friday.proactive import tick


class FakeLLM:
    """Replays scripted replies; records what it was sent."""
    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    async def chat(self, messages, tools=None, model=None, max_tokens=800):
        self.sent.append(messages)
        r = self.replies.pop(0) if self.replies else "ok"
        return r if isinstance(r, dict) else {"content": r}

    async def text(self, prompt, model=None, max_tokens=800):
        return (await self.chat([{"role": "user", "content": prompt}]))["content"]


class FakeFeeds:
    def __init__(self, alerts=(), quakes=(), gh=()):
        self.a, self.q, self.g = list(alerts), list(quakes), list(gh)

    async def alerts(self, lat, lon): return self.a
    async def quakes(self, lat, lon, radius_km=300): return self.q
    async def github(self): return self.g


def settings(tmp_path, **kw):
    s = config.load()
    s.token, s.db, s.owner = "secret", str(tmp_path / "f.db"), "Ryan"
    s.__dict__.update(kw)
    return s


# --- memory -------------------------------------------------------------------
def test_importance_ranks_commitments_over_filler():
    assert importance("ok") == 0.0
    assert importance("remind me to call mom tomorrow at 5pm") > importance("the weather is kind of grey") > 0


def test_consolidation_keeps_facts_and_drops_other_peoples_words(tmp_path):
    m = Memory(str(tmp_path / "m.db"))
    m.add_utterance("I'm allergic to shellfish", speaker="owner", ts=time.time() - 60)
    m.add_utterance("my number is 555 123 4567", speaker="unknown", ts=time.time() - 30)
    llm = FakeLLM(json.dumps({"summary": "Talked about food.", "facts": [
        {"text": "Ryan is allergic to shellfish", "category": "health", "importance": 0.9}],
        "reminders": [{"text": "call mom", "due": "2030-01-01T09:00:00"}]}))
    asyncio.run(m.consolidate(llm))
    assert "[health] Ryan is allergic to shellfish" in m.profile()
    texts = [u["text"] for u in m.recent()]
    assert "I'm allergic to shellfish" in texts and not any("555" in t for t in texts)
    assert m.search("shellfish")
    assert m.due_reminders(now=1.9e9) == ["call mom"]


def test_search_survives_fts_syntax(tmp_path):
    m = Memory(str(tmp_path / "m.db"))
    m.remember('He said "NEAR(" AND OR *')
    assert m.search('"NEAR(" AND OR *')  # FTS operators are quoted as plain words, not parsed
    assert m.search("*") == []


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


# --- feeds ------------------------------------------------------------------------
def test_camera_parsers_pin_official_hosts():
    nyc = parse_nyc([{"id": "1b3248f9-f26e-4f36-943f-b7a7731595fb", "name": "QBB", "latitude": 40.75,
                      "longitude": -73.94, "isOnline": "true"},
                     {"id": "../../evil", "latitude": 1, "longitude": 1, "isOnline": "true"}])
    assert [c["id"] for c in nyc] == ["nyc-1b3248f9-f26e-4f36-943f-b7a7731595fb"]
    tfl = parse_tfl([{"id": "JamCams_1", "commonName": "A", "lat": 51.5, "lon": -0.1, "additionalProperties": [
        {"key": "available", "value": "true"},
        {"key": "imageUrl", "value": "https://evil.example/x.jpg"}]}])
    assert tfl == []
    cal = parse_caltrans({"data": [{"cctv": {"index": "9", "inService": "true",
        "location": {"latitude": "37.7", "longitude": "-122.4", "locationName": "TV1 -- I-80"},
        "imageData": {"static": {"currentImageURL": "https://cwwp2.dot.ca.gov/d4/x.jpg"}}}}]})
    assert cal[0]["id"] == "caltrans-9"


def test_usgs_radius():
    data = {"features": [{"id": "a", "geometry": {"coordinates": [-74.0, 40.7]}, "properties": {"mag": 3, "place": "NYC"}},
                         {"id": "b", "geometry": {"coordinates": [139.7, 35.7]}, "properties": {"mag": 6, "place": "Tokyo"}}]}
    assert [q["id"] for q in parse_usgs(data, 40.7, -74.0, 300)] == ["a"]


# --- calls ------------------------------------------------------------------------
def test_twilio_signature_matches_twilio_docs_example():
    # Expected value computed with twilio-python's RequestValidator.compute_signature.
    url = "https://example.com/myapp.php?foo=1&bar=2"
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
              "From": "+12349013030", "To": "+18005551212"}
    assert twilio_signature("12345", url, params) == "vNe7KK2kJwCsxc9K3OLkkKB3qqI="


def test_twiml_escapes_model_text():
    assert "&lt;Hangup/&gt;" in twiml("<Hangup/>", gather_action="https://x/turn?call=1")


# --- app ----------------------------------------------------------------------------
def test_api_requires_token_and_answers_wake_word(tmp_path):
    llm = FakeLLM("Your 3pm moved to 4.")
    app = create_app(settings(tmp_path), llm=llm, feeds=FakeFeeds(), background=False)
    with TestClient(app) as c:
        assert c.post("/api/utterance", json={"text": "hey friday"}).status_code == 401
        h = {"Authorization": "Bearer secret"}
        assert c.post("/api/utterance", json={"text": "pass the salt"}, headers=h).json() == {
            "addressed": False, "reply": None}
        r = c.post("/api/utterance", json={"text": "Friday, what's my afternoon look like?"}, headers=h).json()
        assert r == {"addressed": True, "reply": "Your 3pm moved to 4."}
        assert "Ryan" in llm.sent[0][0]["content"]  # persona built with owner name


def test_tool_call_reaches_phone_over_websocket(tmp_path):
    llm = FakeLLM(
        {"content": None, "tool_calls": [{"id": "t1", "type": "function", "function": {
            "name": "run_shortcut", "arguments": json.dumps({"name": "Lights Off"})}}]},
        "Lights are off.")
    app = create_app(settings(tmp_path), llm=llm, feeds=FakeFeeds(), background=False)
    with TestClient(app) as c:
        with c.websocket_connect("/ws?token=secret") as ws:
            ws.send_json({"type": "utterance", "text": "Friday, lights off"})
            assert ws.receive_json() == {"type": "action", "kind": "url",
                                         "url": "shortcuts://run-shortcut?name=Lights%20Off"}
            assert ws.receive_json() == {"type": "say", "text": "Lights are off."}


def test_twilio_webhook_rejects_unsigned(tmp_path):
    app = create_app(settings(tmp_path, twilio_token="tok", public_url="https://f.example"),
                     llm=FakeLLM(), feeds=FakeFeeds(), background=False)
    with TestClient(app) as c:
        assert c.post("/twilio/turn?call=x", content="SpeechResult=hi").status_code == 403


# --- proactive ------------------------------------------------------------------------
def test_tick_speaks_each_alert_once(tmp_path):
    said_log = []

    async def notify(kind, text):
        said_log.append(text)

    state = SimpleNamespace(memory=Memory(str(tmp_path / "p.db")), location={"lat": 40.7, "lon": -74.0},
                            settings=settings(tmp_path), notify=notify, feeds=FakeFeeds(
                                alerts=[{"id": "x", "severity": "Severe", "headline": "Tornado warning", "event": "Tornado"},
                                        {"id": "y", "severity": "Minor", "headline": "Fog", "event": "Fog"}],
                                gh=[{"id": "1", "reason": "review_requested", "repo": "a/b", "title": "Fix"}]))
    state.memory.add_reminder("stand up", time.time() - 1)
    asyncio.run(tick(state))
    asyncio.run(tick(state))
    assert said_log == ["Reminder: stand up", "Tornado warning", "GitHub: review requested on a/b: Fix"]
