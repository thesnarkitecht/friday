//! System 1: fast, typed, confidence-scored. Decides, acts and sees so System 2 doesn't have to.
//!
//! decide -> Jev (TypeSafe): typed answers to named questions about a state, with confidence.
//! act    -> Needle 3 (Cactus): one on-device tool call from text, with confidence.
//! see    -> Mirage (Cactus): on-device OCR and object boxes from an image.
//!
//! Every answer carries a confidence and every caller gates on it: System 1 is either sure or it
//! hands the decision up. It never blocks the agent - a missing key or model degrades to defaults.

use crate::config::{Config, S1Config};
use anyhow::{anyhow, Result};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::sync::Mutex;

#[derive(Debug, Clone, Default)]
pub struct Answer {
    pub value: Value,
    /// 0..1. Zero means nobody answered and `value` is the question's default.
    pub confidence: f64,
    /// For choice questions: option -> probability.
    pub probs: HashMap<String, f64>,
}

impl Answer {
    pub fn yes(&self) -> bool {
        self.value.as_bool().unwrap_or(false)
    }
    pub fn text(&self) -> &str {
        self.value.as_str().unwrap_or("")
    }
}

type Needle = Box<dyn Fn(&str, &str, &str) -> Result<Value> + Send + Sync>;

pub struct S1 {
    cfg: S1Config,
    /// (system, tools_json, text) -> the engine's envelope. Swappable so tests need no model.
    pub needle: Needle,
    pub stats: Mutex<HashMap<&'static str, u64>>,
    pub last_error: Mutex<String>,
}

impl S1 {
    pub fn new(cfg: &Config) -> Self {
        let tuned = cfg.s1.needle_weights.clone();
        Self {
            cfg: cfg.s1.clone(),
            needle: Box::new(move |system, tools, text| {
                let tuned = (!tuned.is_empty()).then(|| std::path::Path::new(&tuned));
                crate::cactus::complete(system, tools, text, tuned)
            }),
            stats: Mutex::default(),
            last_error: Mutex::default(),
        }
    }

    pub fn count(&self, what: &'static str) {
        *self.stats.lock().unwrap().entry(what).or_default() += 1;
    }

    pub fn stat(&self, what: &str) -> u64 {
        self.stats.lock().unwrap().get(what).copied().unwrap_or(0)
    }

    fn failed(&self, error: impl std::fmt::Display) {
        self.count("s1_error");
        *self.last_error.lock().unwrap() = error.to_string();
    }

    pub fn has_jev(&self) -> bool {
        self.cfg.jev_key.is_some()
    }

    /// Answer Jev-format questions (choice / noul / score) about `state`.
    ///
    /// A question may carry a "default"; it comes back with confidence 0 when nothing can answer.
    /// `emulate` is a slower stand-in (an LLM) used only when there is no Jev key.
    pub fn decide(&self, state: &str, questions: &Value, emulate: Option<&dyn Fn(&str) -> Result<String>>) -> HashMap<String, Answer> {
        let empty = Map::new();
        let questions = questions.as_object().unwrap_or(&empty);
        let mut out: HashMap<String, Answer> = questions
            .iter()
            .map(|(name, q)| (name.clone(), Answer { value: q["default"].clone(), ..Default::default() }))
            .collect();
        let answered = if self.has_jev() {
            self.count("jev");
            self.decide_jev(state, questions)
        } else if let Some(llm) = emulate {
            self.count("s2_as_s1");
            decide_llm(state, questions, llm)
        } else {
            Ok(HashMap::new())
        };
        match answered {
            Ok(answers) => out.extend(answers.into_iter().filter(|(name, _)| questions.contains_key(name))),
            Err(e) => self.failed(format!("{e:#}")), // a decision must never take the agent down
        }
        out
    }

    fn decide_jev(&self, state: &str, questions: &Map<String, Value>) -> Result<HashMap<String, Answer>> {
        // "default" is Friday's own field; it never goes on the wire.
        let wire: Map<String, Value> = questions
            .iter()
            .map(|(name, q)| {
                let mut q = q.clone();
                q.as_object_mut().map(|o| o.remove("default"));
                (name.clone(), q)
            })
            .collect();
        let response: Value = crate::http(&self.cfg.jev_url)
            .post(&self.cfg.jev_url)
            .set("Authorization", &format!("Bearer {}", self.cfg.jev_key.as_deref().unwrap_or_default()))
            .send_json(json!({"model": self.cfg.jev_model, "state": state, "questions": wire}))
            .map_err(|e| match e {
                ureq::Error::Status(code, r) => anyhow!("Jev answered {code}: {}", r.into_string().unwrap_or_default().chars().take(200).collect::<String>()),
                other => anyhow!("reaching Jev: {other}"),
            })?
            .into_json()?;
        let mut out = HashMap::new();
        for (name, a) in response["answers"].as_object().into_iter().flatten() {
            let confidence = a["confidence"].as_f64().unwrap_or(0.0);
            let answer = match a["type"].as_str() {
                Some("choice") => Answer {
                    value: a["choice"].clone(),
                    confidence,
                    probs: a["probabilities"].as_object().into_iter().flatten().filter_map(|(k, v)| Some((k.clone(), v.as_f64()?))).collect(),
                },
                // A noul is the probability of "yes", not a boolean.
                Some("noul") => {
                    let p = a["noul"].as_f64().unwrap_or(0.5);
                    Answer { value: json!(p >= 0.5), confidence: (p - 0.5).abs() * 2.0, ..Default::default() }
                }
                Some("score") => Answer { value: a["score"].clone(), confidence, ..Default::default() },
                _ => continue,
            };
            out.insert(name.clone(), answer);
        }
        Ok(out)
    }

    /// One on-device tool call for `text`. Returns ([{name, arguments}], confidence).
    pub fn act(&self, text: &str, tools: &[Value], system: &str) -> (Vec<Value>, f64) {
        match (self.needle)(system, &json!(tools).to_string(), text) {
            Ok(reply) => {
                self.count("needle");
                (reply["function_calls"].as_array().cloned().unwrap_or_default(), reply["confidence"].as_f64().unwrap_or(0.0))
            }
            Err(e) => {
                self.failed(format!("{e:#}")); // no weights, offline, unsupported platform
                (Vec::new(), 0.0)
            }
        }
    }

    /// OCR text and objects with 0-1000 normalised [x0, y0, x1, y1] boxes.
    pub fn see(&self, rgb: &[u8], width: u32, height: u32) -> Result<Value> {
        self.count("mirage");
        crate::cactus::describe(rgb, width, height)
    }
}

fn allowed(q: &Value) -> Vec<Value> {
    match q["type"].as_str() {
        Some("choice") => q["criteria"].as_object().into_iter().flatten().map(|(k, _)| json!(k)).collect(),
        Some("score") => (0..q["criteria"].as_array().map_or(0, Vec::len)).map(|i| json!(i)).collect(),
        _ => vec![json!(true), json!(false)],
    }
}

fn decide_llm(state: &str, questions: &Map<String, Value>, llm: &dyn Fn(&str) -> Result<String>) -> Result<HashMap<String, Answer>> {
    let spec: Map<String, Value> = questions
        .iter()
        .map(|(name, q)| (name.clone(), json!({"instructions": q["instructions"], "allowed": allowed(q)})))
        .collect();
    let raw = llm(&format!(
        "Answer each question about the STATE. Reply with one JSON object mapping each question \
         name to one of its allowed values, nothing else.\n\nQUESTIONS:\n{}\n\nSTATE:\n{state}",
        Value::Object(spec)
    ))?;
    let (start, end) = (raw.find('{').ok_or_else(|| anyhow!("no JSON in the reply"))?, raw.rfind('}').unwrap_or(0));
    let data: Map<String, Value> = serde_json::from_str(raw.get(start..=end).unwrap_or("{}"))?;
    // shortcut: an LLM has no calibrated confidence, so valid answers get a flat 0.75;
    // set TYPESAFE_API_KEY for real ones.
    Ok(data
        .into_iter()
        .filter(|(name, value)| questions.get(name).is_some_and(|q| allowed(q).contains(value)))
        .map(|(name, value)| {
            let probs = value.as_str().map(|v| HashMap::from([(v.to_string(), 0.75)])).unwrap_or_default();
            (name, Answer { value, confidence: 0.75, probs })
        })
        .collect())
}
