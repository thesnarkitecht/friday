#![allow(dead_code)]
//! The shared harness: System 2 and Jev are fake servers on localhost reached through the real
//! HTTP code; Needle is swapped for a scripted reply. The live models have their own tests.

use friday::agent::{Agent, Shared, Ui};
use friday::config::{Config, ModelSpec};
use friday::system1::S1;
use friday::{bus::Bus, memory::Memory};
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};

pub type ChatFn = Box<dyn FnMut(&Value) -> Option<Value> + Send>;
pub type JevFn = Box<dyn FnMut(&Value) -> Value + Send>;

pub struct Fake {
    pub url: String,
    pub chat: Arc<Mutex<ChatFn>>,
    pub jev: Arc<Mutex<JevFn>>,
    pub requests: Arc<Mutex<Vec<Value>>>,
}

pub fn sse(model: &str, reply: &Value) -> String {
    let chunk = |delta: Value| json!({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": model, "choices": [{"index": 0, "delta": delta}]});
    let mut chunks = vec![chunk(json!({"role": "assistant", "content": ""}))];
    for word in reply["content"].as_str().unwrap_or("").split_inclusive(' ') {
        chunks.push(chunk(json!({"content": word})));
    }
    for (i, call) in reply["calls"].as_array().into_iter().flatten().enumerate() {
        let f = json!({"name": call[0], "arguments": call[1].to_string()});
        chunks.push(chunk(json!({"tool_calls": [{"index": i, "id": format!("call_{i}"), "type": "function", "function": f}]})));
    }
    chunks.push(json!({"id": "x", "choices": [], "usage": {"prompt_tokens": reply["tokens"].as_u64().unwrap_or(10)}}));
    chunks.iter().map(|c| format!("data: {c}\n\n")).collect::<String>() + "data: [DONE]\n\n"
}

impl Fake {
    pub fn start() -> Fake {
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let url = format!("http://127.0.0.1:{}", server.server_addr().to_ip().unwrap().port());
        let fake = Fake { url, chat: Arc::new(Mutex::new(Box::new(|_| None))), jev: Arc::new(Mutex::new(jev(&[]))), requests: Arc::default() };
        let (chat, jev, requests) = (fake.chat.clone(), fake.jev.clone(), fake.requests.clone());
        std::thread::spawn(move || {
            for mut req in server.incoming_requests() {
                let mut body = String::new();
                let _ = req.as_reader().read_to_string(&mut body);
                let body: Value = serde_json::from_str(&body).unwrap_or(json!({}));
                let response = if req.url().ends_with("/jev") {
                    let answers = (jev.lock().unwrap())(&body["questions"]);
                    tiny_http::Response::from_string(json!({"model": "jev-test", "answers": answers, "usage": {}}).to_string())
                } else {
                    requests.lock().unwrap().push(body.clone());
                    match (chat.lock().unwrap())(&body) {
                        Some(reply) => tiny_http::Response::from_string(sse(body["model"].as_str().unwrap_or(""), &reply)),
                        None => tiny_http::Response::from_string(r#"{"error": {"message": "boom"}}"#).with_status_code(500),
                    }
                };
                let _ = req.respond(response);
            }
        });
        fake
    }

    pub fn script(&self, replies: Vec<Value>) {
        let mut queue: VecDeque<Value> = replies.into();
        *self.chat.lock().unwrap() = Box::new(move |_| queue.pop_front());
    }

    pub fn requests(&self) -> Vec<Value> {
        self.requests.lock().unwrap().clone()
    }
}

pub fn say(content: &str, calls: &[(&str, Value)]) -> Value {
    json!({"content": content, "calls": calls.iter().map(|(n, a)| json!([n, a])).collect::<Vec<_>>()})
}

/// Jev's wire answer for a decision. Strings are choices, booleans nouls.
pub fn wire(value: &Value, confidence: f64, probs: &Value) -> Value {
    match value {
        Value::Bool(yes) => json!({"type": "noul", "noul": if *yes { 0.5 + confidence / 2.0 } else { 0.5 - confidence / 2.0 }}),
        Value::String(s) => json!({"type": "choice", "choice": s, "confidence": confidence, "probabilities": if probs.is_null() { json!({s: confidence}) } else { probs.clone() }}),
        other => json!({"type": "score", "score": other, "confidence": confidence}),
    }
}

/// A Jev that answers the routing questions with sure, minimal answers, plus `extra`.
/// Questions it has no answer for are left out, as Jev would never do: they get their defaults.
pub fn jev(extra: &[(&str, Value, f64)]) -> JevFn {
    let mut all: HashMap<String, (Value, f64, Value)> = [("tier", json!("main")), ("web", json!(false)), ("desktop", json!(false)), ("agents", json!(false))]
        .into_iter()
        .map(|(k, v)| (k.to_string(), (v, 0.9, Value::Null)))
        .collect();
    for (k, v, c) in extra {
        all.insert(k.to_string(), (v.clone(), *c, Value::Null));
    }
    Box::new(move |questions| {
        let mut out = serde_json::Map::new();
        for name in questions.as_object().into_iter().flatten().map(|(k, _)| k) {
            if let Some((v, c, p)) = all.get(name) {
                out.insert(name.clone(), wire(v, *c, p));
            }
        }
        Value::Object(out)
    })
}

#[derive(Default)]
pub struct Rec {
    pub lines: Mutex<Vec<String>>,
    pub tokens: Mutex<Vec<Option<String>>>,
}

impl Ui for Rec {
    fn line(&self, text: &str) {
        self.lines.lock().unwrap().push(text.into());
    }
    fn token(&self, delta: Option<&str>) {
        self.tokens.lock().unwrap().push(delta.map(String::from));
    }
}

impl Rec {
    pub fn saw(&self, needle: &str) -> bool {
        self.lines.lock().unwrap().iter().any(|l| l.contains(needle))
    }
}

pub struct Rig {
    pub agent: Agent,
    pub ui: Arc<Rec>,
    pub dir: tempfile::TempDir,
}

impl Rig {
    pub fn path(&self, name: &str) -> PathBuf {
        self.dir.path().join(name)
    }
    pub fn home(&self) -> PathBuf {
        self.agent.shared.cfg.home.clone()
    }
    pub fn finish(&mut self) {
        if let Some(learner) = self.agent.learner.take() {
            learner.join().unwrap();
        }
        self.agent.shared.memory.flush();
    }
}

pub fn rig(fake: &Fake, needle: Value, yolo: bool, tweak: impl FnOnce(&mut Config)) -> Rig {
    rig_with(fake, needle, yolo, tweak, Vec::new())
}

pub fn rig_with(fake: &Fake, needle: Value, yolo: bool, tweak: impl FnOnce(&mut Config), mcp: Vec<friday::mcp::Server>) -> Rig {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("home");
    let mut cfg = Config { user: "ryan".into(), skill_dirs: vec![home.join("skills")], home, ..Default::default() };
    for tier in ["fast", "main", "deep"] {
        cfg.models.insert(tier.into(), ModelSpec { model: format!("{tier}-model"), base_url: format!("{}/v1", fake.url), api_key_env: "FRIDAY_TEST_NO_KEY".into() });
    }
    cfg.s1.jev_url = format!("{}/jev", fake.url);
    cfg.s1.jev_key = Some("test".into());
    tweak(&mut cfg);
    let mut s1 = S1::new(&cfg);
    s1.needle = Box::new(move |_, _, _| Ok(needle.clone()));
    let s1 = Arc::new(s1);
    let memory = Memory::open(&cfg, s1.clone(), Box::new(|_| None)).unwrap();
    let bus = Bus::open(&cfg.home).unwrap();
    let ui = Arc::new(Rec::default());
    let shared = Arc::new(Shared { cfg, s1, memory, bus, ui: ui.clone(), interactive: false, yolo: AtomicBool::new(yolo), wake: String::new(), mcp });
    Rig { agent: Agent::new(shared, "friday"), ui, dir }
}

pub fn no_needle() -> Value {
    json!({"function_calls": [], "confidence": 0.0})
}

pub fn needle_says(name: &str, args: Value, confidence: f64) -> Value {
    json!({"function_calls": [{"name": name, "arguments": args}], "confidence": confidence})
}

pub fn offered(request: &Value) -> Vec<String> {
    request["tools"].as_array().into_iter().flatten().filter_map(|t| t["function"]["name"].as_str().map(String::from)).collect()
}

