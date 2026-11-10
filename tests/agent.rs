//! End-to-end tests. System 2 and Jev are fake servers on localhost reached through the real
//! HTTP code; Needle is swapped for a scripted reply. The live models have their own tests.

mod common;
use common::*;
use friday::config::{Config, ModelSpec, Watch};
use friday::operate::{elements, operate, Env};
use friday::system1::S1;
use friday::{bus::Bus, coin, proactive, skills, tools};
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

// -- the loop ---------------------------------------------------------------------------------

#[test]
fn system2_uses_tools_then_answers() {
    let fake = Fake::start();
    let mut r = rig(&fake, no_needle(), true, |_| {});
    let file = r.path("a.txt");
    fake.script(vec![say("", &[("write_file", json!({"path": file, "content": "hi"}))]), say("done", &[])]);
    assert_eq!(r.agent.turn("make a.txt"), "done");
    assert_eq!(std::fs::read_to_string(&file).unwrap(), "hi");
    let first = &fake.requests()[0];
    assert_eq!(first["model"], "main-model");
    // Tool groups System 1 ruled out never reach the model's context.
    let tools = offered(first);
    assert!(tools.contains(&"shell".into()) && !tools.iter().any(|t| ["browse", "use_computer", "delegate"].contains(&t.as_str())));
    r.finish();
}

#[test]
fn a_reflex_needs_no_system2() {
    let fake = Fake::start();
    *fake.jev.lock().unwrap() = jev(&[("tier", json!("reflex"), 0.95)]);
    let dir = tempfile::tempdir().unwrap();
    let notes = dir.path().join("notes.txt");
    std::fs::write(&notes, "remember the milk").unwrap();
    let mut r = rig(&fake, needle_says("read_file", json!({"path": notes}), 0.9), true, |_| {});
    assert_eq!(r.agent.turn("read notes.txt"), "remember the milk");
    assert!(fake.requests().is_empty());
    assert_eq!(r.agent.last_tier, "reflex");
    r.finish();
}

#[test]
fn an_unsure_reflex_goes_to_the_fast_model() {
    let fake = Fake::start();
    *fake.jev.lock().unwrap() = jev(&[("tier", json!("reflex"), 0.95)]);
    fake.script(vec![say("thought about it", &[])]);
    let mut r = rig(&fake, needle_says("shell", json!({"command": "rm -rf /"}), 0.2), true, |_| {});
    assert_eq!(r.agent.turn("hmm"), "thought about it");
    assert_eq!(fake.requests()[0]["model"], "fast-model");
    r.finish();
}

#[test]
fn an_unsure_route_falls_back_to_main() {
    let fake = Fake::start();
    *fake.jev.lock().unwrap() = jev(&[("tier", json!("deep"), 0.3)]);
    fake.script(vec![say("ok", &[])]);
    let mut r = rig(&fake, no_needle(), true, |_| {});
    r.agent.turn("x");
    assert_eq!(fake.requests()[0]["model"], "main-model");
    r.finish();
}

#[test]
fn a_failing_model_escalates_a_tier() {
    let fake = Fake::start();
    *fake.jev.lock().unwrap() = jev(&[("tier", json!("fast"), 0.9)]);
    *fake.chat.lock().unwrap() = Box::new(|body| (body["model"] != "fast-model").then(|| say("from main", &[])));
    let mut r = rig(&fake, no_needle(), true, |_| {});
    assert_eq!(r.agent.turn("x"), "from main");
    assert!(r.ui.saw("escalating"));
    r.finish();
}

#[test]
fn jev_judging_a_reply_unresolved_climbs_the_ladder() {
    let fake = Fake::start();
    *fake.jev.lock().unwrap() = jev(&[("tier", json!("fast"), 0.9), ("ok", json!(false), 0.9)]);
    fake.script(vec![say("half done", &[]), say("still half", &[]), say("tried hardest", &[])]);
    let mut r = rig(&fake, no_needle(), true, |_| {});
    // Every rung is judged unresolved, so the top rung's answer is what comes back.
    assert_eq!(r.agent.turn("x"), "tried hardest");
    let models: Vec<Value> = fake.requests().iter().map(|q| q["model"].clone()).collect();
    assert_eq!(models, [json!("fast-model"), json!("main-model"), json!("deep-model")]);
    r.finish();
}

#[test]
fn writes_are_denied_without_approval() {
    let fake = Fake::start();
    let mut r = rig(&fake, no_needle(), false, |_| {});
    let made = r.path("made");
    fake.script(vec![say("", &[("shell", json!({"command": format!("touch {}", made.display())})), ("shell", json!({"command": "ls"}))]), say("ok", &[])]);
    r.agent.turn("x");
    let results: Vec<String> = r.agent.messages.iter().filter(|m| m["role"] == "tool").map(|m| m["content"].as_str().unwrap().to_string()).collect();
    assert_eq!(results[0], tools::DENIED);
    assert!(results[1].starts_with("exit 0"), "read-only commands need no approval");
    assert!(!made.exists());
    r.finish();
}

#[test]
fn the_read_only_allowlist_rejects_chained_commands() {
    assert!(tools::is_read_only("ls -la") && tools::is_read_only("grep -rn TODO src"));
    for sneaky in ["find . -delete", "find . -exec rm {} +", "rg --pre ./x y", "tree -o f", "date -s 2020-01-01", "ls; rm -rf x", "cat a > b", "ls && rm x", "cat `rm x`", "rm x", "ls $(rm x)", "ls\nrm x", "find . -delete | ls"] {
        assert!(!tools::is_read_only(sneaky), "{sneaky}");
    }
}

#[test]
fn replies_stream_and_tool_calls_survive_streaming() {
    let fake = Fake::start();
    let mut r = rig(&fake, no_needle(), true, |_| {});
    let file = r.path("s.txt");
    fake.script(vec![say("let me", &[("write_file", json!({"path": file, "content": "x"}))]), say("all done now", &[])]);
    assert_eq!(r.agent.turn("go"), "all done now");
    assert!(file.exists());
    let tokens = r.ui.tokens.lock().unwrap().clone();
    assert_eq!(tokens.iter().flatten().cloned().collect::<String>(), "let meall done now");
    assert_eq!(tokens.iter().filter(|t| t.is_none()).count(), 2, "one end marker per model call");
    r.finish();
}

#[test]
fn compaction_keeps_tool_pairs_and_files_the_old_turns_verbatim() {
    let fake = Fake::start();
    let mut over_budget = say("", &[("recall", json!({"query": "q"}))]);
    over_budget["tokens"] = json!(1_000_000);
    fake.script(vec![say("one", &[]), over_budget, say("summary of earlier work", &[]), say("three", &[])]);
    let mut r = rig(&fake, no_needle(), true, |c| c.context.keep_recent = 2);
    r.agent.turn("first");
    r.finish();
    assert_eq!(r.agent.turn("second"), "three");
    let first = r.agent.messages[0]["content"].as_str().unwrap();
    assert!(first.starts_with("[Earlier conversation, compacted") && first.contains("summary of earlier work"));
    let roles: Vec<&str> = r.agent.messages.iter().map(|m| m["role"].as_str().unwrap()).collect();
    assert_eq!(roles, ["user", "user", "assistant", "tool", "assistant"], "the call kept its result");
    r.finish();
    let filed = r.agent.shared.memory.search("compacted context first", 10).unwrap();
    assert!(filed.iter().any(|h| h.text.starts_with("[compacted context]") && h.text.contains("first")));
}

// -- system 1 ---------------------------------------------------------------------------------

#[test]
fn jev_answers_become_typed_decisions_and_defaults_stay_off_the_wire() {
    let fake = Fake::start();
    let sent = Arc::new(Mutex::new(Value::Null));
    let seen = sent.clone();
    *fake.jev.lock().unwrap() = Box::new(move |questions| {
        *seen.lock().unwrap() = questions.clone();
        json!({
            "tier": {"type": "choice", "choice": "deep", "confidence": 0.8, "probabilities": {"deep": 0.8, "main": 0.2}},
            "web": {"type": "noul", "noul": 0.95},
            "nope": {"type": "noul", "noul": 0.1},
            "stranger": {"type": "noul", "noul": 0.9}
        })
    });
    let r = rig(&fake, no_needle(), true, |_| {});
    let a = r.agent.shared.s1.decide("x", &json!({
        "tier": {"type": "choice", "criteria": {"deep": "", "main": ""}, "default": "main"},
        "web": {"type": "noul"}, "nope": {"type": "noul"},
        "missing": {"type": "noul", "default": true}
    }), None);
    assert_eq!((a["tier"].text(), a["tier"].probs["main"]), ("deep", 0.2));
    assert!(a["web"].yes() && (a["web"].confidence - 0.9).abs() < 1e-9);
    assert!(!a["nope"].yes() && (a["nope"].confidence - 0.8).abs() < 1e-9);
    assert!(a["missing"].yes() && a["missing"].confidence == 0.0, "unanswered: the default");
    assert!(!a.contains_key("stranger"), "answers to questions not asked are dropped");
    assert!(sent.lock().unwrap()["tier"].get("default").is_none(), "Friday's own field never goes on the wire");
}

#[test]
fn a_model_standing_in_for_jev_cannot_answer_outside_the_options() {
    let cfg = Config::default();
    let s1 = S1::new(&cfg);
    let llm = |_: &str| Ok(r#"Sure: {"tier": "fast", "web": "maybe"}"#.to_string());
    let a = s1.decide("x", &json!({"tier": {"type": "choice", "criteria": {"fast": "", "main": ""}}, "web": {"type": "noul", "default": true}}), Some(&llm));
    assert_eq!(a["tier"].text(), "fast");
    assert!(a["web"].yes() && a["web"].confidence == 0.0);
}

#[test]
fn a_broken_system1_never_breaks_a_decision() {
    let s1 = S1::new(&Config::default());
    let llm = |_: &str| -> anyhow::Result<String> { anyhow::bail!("down") };
    let a = s1.decide("x", &json!({"tier": {"type": "choice", "criteria": {"a": ""}, "default": "main"}}), Some(&llm));
    assert_eq!(a["tier"].text(), "main");
    assert_eq!(*s1.last_error.lock().unwrap(), "down");
}

// -- skills -----------------------------------------------------------------------------------

fn write_skill(home: &std::path::Path, name: &str, description: &str) {
    let dir = home.join("skills").join(name);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("SKILL.md"), format!("---\nname: {name}\ndescription: {description}\n---\nSteps for {name}.\n")).unwrap();
}

#[test]
fn jev_ranks_the_skills_and_the_best_ones_reach_the_prompt() {
    let fake = Fake::start();
    let probs = json!({"deploy": 0.55, "release-notes": 0.3, "unrelated": 0.05, "none": 0.1});
    let mut routing = jev(&[]);
    *fake.jev.lock().unwrap() = Box::new(move |questions| {
        let mut out = routing(questions);
        if questions.get("skills").is_some() {
            out["skills"] = wire(&json!("deploy"), 0.7, &probs);
        }
        out
    });
    fake.script(vec![say("shipped", &[])]);
    let mut r = rig(&fake, no_needle(), true, |_| {});
    for name in ["deploy", "release-notes", "unrelated"] {
        write_skill(&r.home(), name, &format!("how to {name}"));
    }
    r.agent.turn("ship it");
    let system = fake.requests()[0]["messages"][0]["content"].as_str().unwrap().to_string();
    assert!(system.contains("Steps for deploy.") && system.contains("Steps for release-notes."));
    assert!(!system.contains("Steps for unrelated."));
    r.finish();
    let scores = skills::scores(&r.agent.shared.cfg);
    assert_eq!(scores.get("deploy"), Some(&(1, 1)));
    assert_eq!(scores.get("release-notes"), Some(&(1, 1)));
}

#[test]
fn a_skill_that_keeps_losing_is_retired_until_rewritten() {
    let dir = tempfile::tempdir().unwrap();
    let cfg = Config { home: dir.path().into(), ..Default::default() };
    for won in [false, false, false, true] {
        skills::record(&cfg, &["flaky".into()], won).unwrap();
    }
    assert!(skills::healthy(skills::scores(&cfg).get("flaky")));
    skills::record(&cfg, &["flaky".into()], false).unwrap(); // 1 win in 5
    assert!(!skills::healthy(skills::scores(&cfg).get("flaky")));
    skills::reset(&cfg, "flaky").unwrap();
    assert!(skills::healthy(skills::scores(&cfg).get("flaky")));
}

#[test]
fn a_model_written_skill_cannot_escape_the_skills_folder() {
    let dir = tempfile::tempdir().unwrap();
    let cfg = Config { home: dir.path().into(), skill_dirs: vec![dir.path().join("skills")], ..Default::default() };
    let name = skills::save(&cfg, "```markdown\n---\nname: ../../etc/Evil Skill\ndescription: d\n---\nbody\n```").unwrap();
    assert_eq!(name.as_deref(), Some("etc-evil-skill"));
    assert!(dir.path().join("skills/etc-evil-skill/SKILL.md").exists());
    assert_eq!(skills::save(&cfg, "no frontmatter at all").unwrap(), None);
}

#[test]
fn claude_code_and_hermes_skills_are_found_where_they_live() {
    let dir = tempfile::tempdir().unwrap();
    let nested = dir.path().join("hermes/skills/devops/k8s-debug");
    std::fs::create_dir_all(&nested).unwrap();
    std::fs::write(nested.join("SKILL.md"), "---\nname: k8s-debug\ndescription: when pods crash\n---\n").unwrap();
    let cfg = Config { skill_dirs: vec![dir.path().join("hermes/skills")], ..Default::default() };
    assert_eq!(skills::load(&cfg)["k8s-debug"].description, "when pods crash");
}

#[test]
fn multi_step_work_becomes_a_skill_and_a_lone_call_trains_needle() {
    let fake = Fake::start();
    *fake.jev.lock().unwrap() = jev(&[("learn", json!("new"), 0.9)]);
    let ls = ("shell", json!({"command": "ls"}));
    fake.script(vec![
        say("", &[ls.clone(), ls.clone(), ls]),
        say("did it", &[]),
        say("---\nname: list-things\ndescription: when asked to list\n---\n1. ls\n", &[]),
        say("", &[("shell", json!({"command": "pwd"}))]),
        say("here", &[]),
    ]);
    let mut r = rig(&fake, no_needle(), true, |_| {});
    r.agent.turn("list things three ways");
    r.finish();
    assert!(r.home().join("skills/list-things/SKILL.md").exists());
    assert!(r.ui.saw("learned skill: list-things"));
    r.agent.turn("where am I");
    r.finish();
    let rows: Vec<Value> = std::fs::read_to_string(r.home().join("train/needle.jsonl")).unwrap().lines().map(|l| serde_json::from_str(l).unwrap()).collect();
    assert_eq!(rows, [json!({"text": "where am I", "tool_code": "shell(command=\"pwd\")"})]);
}

// -- memory -----------------------------------------------------------------------------------

#[test]
fn chunks_join_back_to_the_exact_original() {
    let text = format!("short line\n{}\n{}\n", "x".repeat(5000), "é".repeat(2000));
    let pieces = friday::memory::chunks(&text);
    assert!(pieces.len() > 3 && pieces.iter().all(|p| p.chars().count() <= 1000));
    assert_eq!(pieces.concat(), text);
}

#[test]
fn the_palace_files_verbatim_once_and_survives_hostile_queries() {
    let fake = Fake::start();
    let r = rig(&fake, no_needle(), true, |_| {});
    let m = &r.agent.shared.memory;
    assert_eq!(m.add("ryan", "2026-11-10", "The deploy key lives in 1Password under \"prod\".", "test").unwrap(), 1);
    assert_eq!(m.add("ryan", "2026-11-10", "The deploy key lives in 1Password under \"prod\".", "test").unwrap(), 0);
    let hits = m.search("where is the deploy key?", 3).unwrap();
    assert_eq!(hits[0].text, "The deploy key lives in 1Password under \"prod\".");
    for hostile in ["\"", "NEAR(", "a* OR -", "'); DROP TABLE drawers; --", ""] {
        assert!(m.search(hostile, 3).is_ok(), "{hostile}");
    }
}

#[test]
fn what_the_user_says_becomes_conclusions_that_come_back_as_context() {
    let fake = Fake::start();
    *fake.jev.lock().unwrap() = jev(&[("lasting", json!(true), 0.9)]);
    fake.script(vec![say(r#"[{"kind": "explicit", "text": "ryan is allergic to peanuts"}, {"kind": "deductive", "text": "ryan should not be offered satay"}]"#, &[])]);
    let r = rig(&fake, no_needle(), true, |_| {});
    let m = &r.agent.shared.memory;
    m.remember("ryan", "heads up, I'm allergic to peanuts");
    m.remember("friday", "noted"); // only the user's own words are derived from
    m.flush();
    assert_eq!(fake.requests().len(), 1);
    let context = m.recall("can I order satay for ryan?", 4);
    assert!(context.contains("[known about ryan]") && context.contains("should not be offered satay"), "{context}");
    assert_eq!(m.counts().1, 2);
}

// -- agents talking ---------------------------------------------------------------------------

#[test]
fn the_bus_delivers_each_message_once() {
    let dir = tempfile::tempdir().unwrap();
    let bus = Bus::open(dir.path()).unwrap();
    bus.send("a", "b", "hello").unwrap();
    bus.send("c", "b", "again").unwrap();
    assert_eq!(bus.inbox("b").unwrap(), [("a".into(), "hello".into()), ("c".into(), "again".into())]);
    assert!(bus.inbox("b").unwrap().is_empty() && bus.inbox("a").unwrap().is_empty());
}

#[test]
fn delegate_runs_subagents_in_parallel() {
    let fake = Fake::start();
    *fake.jev.lock().unwrap() = jev(&[("agents", json!(true), 0.9)]);
    *fake.chat.lock().unwrap() = Box::new(|body| {
        let last = body["messages"].as_array().unwrap().last().unwrap();
        Some(if last["role"] == "tool" {
            say("merged", &[])
        } else if last["content"] == "split" {
            say("", &[("delegate", json!({"tasks": ["task A", "task B"]}))])
        } else {
            say(&format!("report on {}", last["content"].as_str().unwrap()), &[])
        })
    });
    let mut r = rig(&fake, no_needle(), true, |_| {});
    assert_eq!(r.agent.turn("split"), "merged");
    let report = r.agent.messages.iter().find(|m| m["role"] == "tool").unwrap()["content"].as_str().unwrap().to_string();
    assert!(report.contains("## friday.1\nreport on task A") && report.contains("## friday.2\nreport on task B"), "{report}");
    r.finish();
}

// -- proactive --------------------------------------------------------------------------------

#[test]
fn a_watcher_fires_only_when_command_output_changes() {
    let dir = tempfile::tempdir().unwrap();
    let target = dir.path().join("watched");
    std::fs::write(&target, "v1").unwrap();
    let watches = [Watch { name: "f".into(), every: 0, cmd: format!("cat {}", target.display()), prompt: "changed".into() }];
    let mut state = json!({});
    assert!(proactive::due_events(&watches, &mut state, 1).is_empty(), "the first look is the baseline");
    assert!(proactive::due_events(&watches, &mut state, 2).is_empty());
    std::fs::write(&target, "v2").unwrap();
    let events = proactive::due_events(&watches, &mut state, 3);
    assert!(events.len() == 1 && events[0].starts_with("changed") && events[0].contains("v2"));
    let timer = [Watch { name: "t".into(), every: 60, cmd: String::new(), prompt: "tick".into() }];
    assert_eq!(proactive::due_events(&timer, &mut state, 100), ["tick"]);
    assert!(proactive::due_events(&timer, &mut state, 130).is_empty());
}

#[test]
fn system1_lets_a_watcher_skip_noise_for_free() {
    let fake = Fake::start();
    *fake.jev.lock().unwrap() = jev(&[("act", json!(false), 0.95)]);
    let mut r = rig(&fake, no_needle(), true, |c| c.watch = vec![Watch { name: "t".into(), every: 0, cmd: String::new(), prompt: "nothing happened".into() }]);
    proactive::tick(&mut r.agent, &mut json!({}), 1).unwrap();
    assert!(fake.requests().is_empty() && r.ui.saw("skipped"));
}

#[test]
fn mail_from_another_agent_starts_a_turn() {
    let fake = Fake::start();
    fake.script(vec![say("on it", &[])]);
    let mut r = rig(&fake, no_needle(), true, |_| {});
    r.agent.shared.bus.send("scout", "friday", "the build broke").unwrap();
    proactive::tick(&mut r.agent, &mut json!({}), 1).unwrap();
    let asked = fake.requests()[0]["messages"].as_array().unwrap().last().unwrap()["content"].clone();
    assert_eq!(asked, "Message from agent scout: the build broke");
    assert!(r.ui.saw("on it"));
    r.finish();
}

// -- operating a screen -----------------------------------------------------------------------

#[derive(Default)]
struct FakeEnv {
    done: Vec<(String, Value)>,
}

impl Env for FakeEnv {
    fn observe(&mut self) -> anyhow::Result<(Vec<String>, String)> {
        Ok((vec!["input: Search".into(), "button: Go".into()], "a page".into()))
    }
    fn click(&mut self, element: usize) -> anyhow::Result<()> {
        Ok(self.done.push(("click".into(), json!({"element": element}))))
    }
    fn type_text(&mut self, element: usize, text: &str) -> anyhow::Result<()> {
        Ok(self.done.push(("type_text".into(), json!({"element": element, "text": text}))))
    }
    fn press_enter(&mut self) -> anyhow::Result<()> {
        Ok(self.done.push(("press_enter".into(), json!({}))))
    }
    fn goto(&mut self, url: &str) -> anyhow::Result<()> {
        Ok(self.done.push(("goto".into(), json!({"url": url}))))
    }
}

#[test]
fn operate_takes_sure_steps_with_system1_and_the_rest_with_system2() {
    let fake = Fake::start();
    let mut steps: VecDeque<JevFn> = VecDeque::from([
        jev(&[("done", json!(false), 0.9), ("action", json!("click"), 0.9), ("target", json!("e1"), 0.9)]), // sure: click Go
        jev(&[("done", json!(false), 0.9), ("action", json!("click"), 0.3), ("target", json!("e0"), 0.9)]), // unsure: System 2
        jev(&[("done", json!(true), 0.95)]), // finished
    ]);
    *fake.jev.lock().unwrap() = Box::new(move |q| (steps.pop_front().expect("more steps than scripted"))(q));
    fake.script(vec![say("", &[("type_text", json!({"element": 0, "text": "cats"}))])]);
    let r = rig(&fake, no_needle(), true, |_| {});
    let mut env = FakeEnv::default();
    let result = operate(&r.agent, &mut env, "search for cats").unwrap();
    assert_eq!(env.done, [("click".into(), json!({"element": 1})), ("type_text".into(), json!({"element": 0, "text": "cats"}))]);
    assert!(result.contains("untrusted"));
    assert_eq!((r.agent.shared.s1.stat("operate_s1"), r.agent.shared.s1.stat("operate_s2")), (2, 1));
    // The step System 2 had to take became training data for Needle.
    let row: Value = serde_json::from_str(std::fs::read_to_string(r.home().join("train/needle.jsonl")).unwrap().trim()).unwrap();
    assert_eq!(row["tool_code"], "type_text(element=0, text=\"cats\")");
}

#[test]
fn a_real_browser_clicks_through_a_page() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("a.html"), r#"<a href="b.html">Next page</a><button onclick="1">Other</button>"#).unwrap();
    std::fs::write(dir.path().join("b.html"), "<h1>Arrived</h1>").unwrap();
    if std::env::var_os("FRIDAY_CHROMIUM").is_none() && std::path::Path::new("/opt/pw-browsers/chromium").exists() {
        std::env::set_var("FRIDAY_CHROMIUM", "/opt/pw-browsers/chromium");
        std::env::set_var("FRIDAY_CHROMIUM_NO_SANDBOX", "1"); // this sandbox runs as root
    }
    let mut env = match friday::operate::Browser::open(&format!("file://{}/a.html", dir.path().display())) {
        Ok(env) => env,
        Err(e) => return eprintln!("skipped: no browser here: {e:#}"),
    };
    assert_eq!(env.observe().unwrap().0, ["link: Next page", "button: Other"]);
    let fake = Fake::start();
    fake.script(vec![say("", &[("click", json!({"element": 0}))]), say("", &[("done", json!({"answer": "there"}))])]);
    let r = rig(&fake, no_needle(), true, |_| {});
    let result = operate(&r.agent, &mut env, "go to the next page").unwrap();
    assert!(result.starts_with("there") && result.contains("Arrived"), "{result}");
}

#[test]
fn mirage_boxes_become_click_points() {
    let seen = json!({"text": [{"string": "Sign in", "bbox": [100, 200, 300, 400]}], "objects": [{"name": "icon", "bbox": [0, 0, 1000, 1000]}]});
    let (points, labels) = elements(&seen, 2000, 1000);
    assert_eq!(points, [(400, 300), (1000, 500)]);
    assert_eq!(labels, ["text: Sign in", "object: icon"]);
}

// -- the rest ---------------------------------------------------------------------------------

#[test]
fn the_coin_shows_a_different_face_each_side_and_a_rim_edge_on() {
    let plain = |theta: f64| -> Vec<String> {
        coin::frame(theta).iter().map(|l| l.chars().filter(|c| ('\u{2800}'..='\u{28ff}').contains(c)).collect()).collect()
    };
    let lit = |theta: f64| plain(theta).concat().chars().filter(|c| *c != '\u{2800}').count();
    let front = plain(0.0);
    assert_eq!(front.len(), coin::ROWS);
    assert!(front.iter().all(|l| l.chars().count() == coin::ROWS * 2));
    assert_ne!(front, plain(std::f64::consts::PI));
    let edge = lit(std::f64::consts::FRAC_PI_2);
    assert!(edge > 0 && edge < lit(0.0) / 2, "edge-on is a thin strip, but not nothing");
    assert!(coin::frame(0.0)[5].contains("142;162;255") && coin::frame(std::f64::consts::PI)[5].contains("245;180;84"));
}

#[test]
fn friday_run_prints_only_the_answer_on_stdout() {
    let fake = Fake::start();
    fake.script(vec![say("42", &[])]);
    let home = tempfile::tempdir().unwrap();
    std::fs::write(home.path().join("config.toml"), format!("user = \"ryan\"\n[models.main]\nmodel = \"m\"\nbase_url = \"{}/v1\"\n", fake.url)).unwrap();
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_friday"))
        .args(["run", "what is six times seven"])
        .env("FRIDAY_HOME", home.path())
        .env_remove("TYPESAFE_API_KEY")
        .stdin(std::process::Stdio::null())
        .output()
        .unwrap();
    assert_eq!(String::from_utf8_lossy(&out.stdout), "42\n", "stderr: {}", String::from_utf8_lossy(&out.stderr));
    assert!(String::from_utf8_lossy(&out.stderr).contains("plan → main"));
}


#[test]
fn a_hung_command_times_out_and_keeps_its_output() {
    let started = std::time::Instant::now();
    let out = tools::run_command("echo before; sleep 30", std::time::Duration::from_millis(500)).unwrap();
    assert!(started.elapsed().as_secs() < 5);
    assert!(out.starts_with("stopped: timed out") && out.contains("before"), "{out}");
    let ok = tools::run_command("echo out; echo err >&2; exit 3", std::time::Duration::from_secs(10)).unwrap();
    assert_eq!(ok, "exit 3\nout\nerr\n");
}

#[test]
fn a_provider_error_mid_stream_is_an_error_not_an_empty_answer() {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let url = format!("http://127.0.0.1:{}/v1", server.server_addr().to_ip().unwrap().port());
    std::thread::spawn(move || {
        for req in server.incoming_requests() {
            let _ = req.respond(tiny_http::Response::from_string("data: {\"error\": {\"message\": \"rate limited\"}}\n\ndata: [DONE]\n\n"));
        }
    });
    let mut cfg = Config::default();
    cfg.models.insert("main".into(), ModelSpec { model: "m".into(), base_url: url, api_key_env: "X".into() });
    let err = friday::models::chat(&cfg, "main", &[json!({"role": "user", "content": "hi"})], &[], None).unwrap_err();
    assert!(err.to_string().contains("rate limited"), "{err}");
}

#[test]
fn tool_calls_without_index_or_id_still_come_out_whole() {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let url = format!("http://127.0.0.1:{}/v1", server.server_addr().to_ip().unwrap().port());
    std::thread::spawn(move || {
        let body = [
            r#"{"choices":[{"delta":{"tool_calls":[{"function":{"name":"ls","arguments":"{}"}}]}}]}"#,
            r#"{"choices":[{"delta":{"tool_calls":[{"id":"b","function":{"name":"pwd","arguments":"{"}}]}}]}"#,
            r#"{"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"}"}}]}}]}"#,
        ]
        .iter()
        .map(|c| format!("data: {c}\n\n"))
        .collect::<String>();
        for req in server.incoming_requests() {
            let _ = req.respond(tiny_http::Response::from_string(body.clone()));
        }
    });
    let mut cfg = Config::default();
    cfg.models.insert("main".into(), ModelSpec { model: "m".into(), base_url: url, api_key_env: "X".into() });
    let reply = friday::models::chat(&cfg, "main", &[json!({"role": "user", "content": "hi"})], &[], None).unwrap();
    let calls: Vec<(&str, &str, &str)> = reply.tool_calls.iter().map(|c| (c.id.as_str(), c.name.as_str(), c.arguments.as_str())).collect();
    assert_eq!(calls, [("call_0", "ls", "{}"), ("b", "pwd", "{}")]);
    assert!(reply.prompt_tokens > 0, "no usage reported: estimated");
}

// -- skills, continued ------------------------------------------------------------------------

#[test]
fn a_large_library_is_shortlisted_before_jev_sees_it() {
    let fake = Fake::start();
    let asked = Arc::new(Mutex::new(Vec::<String>::new()));
    let seen = asked.clone();
    let mut routing = jev(&[]);
    *fake.jev.lock().unwrap() = Box::new(move |q| {
        if let Some(c) = q["skills"]["criteria"].as_object() {
            *seen.lock().unwrap() = c.keys().cloned().collect();
        }
        routing(q)
    });
    fake.script(vec![say("ok", &[])]);
    let mut r = rig(&fake, no_needle(), true, |_| {});
    for i in 0..60 {
        write_skill(&r.home(), &format!("filler-{i}"), &format!("unrelated chore number {i}"));
    }
    write_skill(&r.home(), "postgres-migrations", "write and run postgres schema migrations safely");
    r.agent.turn("write a postgres migration that adds an index");
    let asked = asked.lock().unwrap().clone();
    assert_eq!(asked.len(), 25, "24 shortlisted plus `none`");
    assert!(asked.contains(&"postgres-migrations".to_string()));
    r.finish();
}

#[test]
fn a_named_skill_is_followed_with_its_supporting_files() {
    let fake = Fake::start();
    fake.script(vec![say("released", &[])]);
    let mut r = rig(&fake, no_needle(), true, |_| {});
    write_skill(&r.home(), "release", "cut a release");
    let scripts = r.home().join("skills/release/scripts");
    std::fs::create_dir_all(&scripts).unwrap();
    std::fs::write(scripts.join("bump.sh"), "echo bump").unwrap();
    r.agent.force_skills = vec!["release".into()];
    r.agent.turn("go");
    let system = fake.requests()[0]["messages"][0]["content"].as_str().unwrap().to_string();
    assert!(system.contains("Steps for release.") && system.contains("scripts/bump.sh"), "{system}");
    assert!(r.ui.saw("skills: release"));
    r.finish();
}

#[test]
fn rewriting_a_skill_keeps_the_old_version() {
    let dir = tempfile::tempdir().unwrap();
    let cfg = Config { home: dir.path().into(), skill_dirs: vec![dir.path().join("skills")], ..Default::default() };
    skills::save(&cfg, "---\nname: deploy\ndescription: v1\n---\nold steps").unwrap();
    skills::save(&cfg, "---\nname: deploy\ndescription: v2\n---\nnew steps").unwrap();
    assert_eq!(skills::load(&cfg)["deploy"].description, "v2");
    let history: Vec<_> = std::fs::read_dir(dir.path().join("skills/deploy/history")).unwrap().flatten().collect();
    assert_eq!(history.len(), 1);
    assert!(std::fs::read_to_string(history[0].path()).unwrap().contains("old steps"));
    assert_eq!(skills::load(&cfg).len(), 1, "history is not loaded as skills");
}

#[test]
fn a_skill_that_gets_retired_is_rewritten_from_the_failure() {
    let fake = Fake::start();
    // Sure it applies, sure the work should not become a skill: only self-healing can rewrite it.
    let mut routing = jev(&[("learn", json!("no"), 0.95), ("ok", json!(false), 0.9), ("tier", json!("deep"), 0.9)]);
    *fake.jev.lock().unwrap() = Box::new(move |q| {
        let mut out = routing(q);
        if q.get("skills").is_some() {
            out["skills"] = wire(&json!("flaky"), 0.9, &Value::Null);
        }
        out
    });
    fake.script(vec![say("half", &[]), say("---\nname: flaky\ndescription: fixed\n---\nbetter steps\n", &[])]);
    let mut r = rig(&fake, no_needle(), true, |_| {});
    write_skill(&r.home(), "flaky", "does the thing");
    let cfg = r.agent.shared.cfg.clone();
    for _ in 0..3 {
        skills::record(&cfg, &["flaky".into()], false).unwrap();
    }
    r.agent.turn("do the thing"); // 4th loss: retired, then rewritten
    r.finish();
    assert!(r.ui.saw("improved skill: flaky"));
    assert_eq!(skills::load(&cfg)["flaky"].description, "fixed");
    assert!(skills::healthy(skills::scores(&cfg).get("flaky")), "a rewrite starts with a clean record");
}

#[test]
fn the_agent_can_read_and_write_skills_mid_task() {
    let fake = Fake::start();
    fake.script(vec![
        say("", &[("skill_view", json!({"name": "lint"})), ("skill_save", json!({"content": "---\nname: fmt-check\ndescription: check formatting\n---\nrun the formatter"}))]),
        say("done", &[]),
    ]);
    let mut r = rig(&fake, no_needle(), true, |_| {});
    write_skill(&r.home(), "lint", "run the linter");
    r.agent.turn("x");
    let results: Vec<String> = r.agent.messages.iter().filter(|m| m["role"] == "tool").map(|m| m["content"].as_str().unwrap().to_string()).collect();
    assert!(results[0].contains("Steps for lint.") && results[1] == "saved skill `fmt-check`");
    assert!(skills::load(&r.agent.shared.cfg).contains_key("fmt-check"));
    r.finish();
}

#[test]
fn a_skill_pack_installs_from_git() {
    let src = tempfile::tempdir().unwrap();
    let pack = src.path().join("my-pack");
    for name in ["one", "two"] {
        std::fs::create_dir_all(pack.join(name)).unwrap();
        std::fs::write(pack.join(name).join("SKILL.md"), format!("---\nname: {name}\ndescription: d\n---\n")).unwrap();
    }
    let git = |args: &[&str]| assert!(std::process::Command::new("git").args(args).current_dir(&pack).output().unwrap().status.success());
    git(&["init", "-q"]);
    git(&["add", "."]);
    git(&["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "pack"]);
    let home = tempfile::tempdir().unwrap();
    let cfg = Config { home: home.path().into(), skill_dirs: vec![home.path().join("skills")], ..Default::default() };
    assert_eq!(skills::add(&cfg, pack.to_str().unwrap()).unwrap(), ("my-pack".into(), 2));
    assert!(skills::load(&cfg).contains_key("two"));
    assert!(skills::add(&cfg, pack.to_str().unwrap()).is_err(), "never overwrites");
}

#[test]
fn project_skills_load_only_from_trusted_folders() {
    let home = tempfile::tempdir().unwrap();
    let project = std::path::Path::new("/some/cloned/repo");
    assert!(!friday::config::is_trusted(home.path(), project));
    friday::config::trust(home.path(), project).unwrap();
    assert!(friday::config::is_trusted(home.path(), project));
    assert!(!friday::config::is_trusted(home.path(), std::path::Path::new("/some/cloned")));
}

// -- harness: MCP, checkpoints, tools ---------------------------------------------------------

fn echo_server(trust: bool) -> friday::mcp::Server {
    let cfg = friday::mcp::McpConfig {
        name: "demo".into(),
        command: "python3".into(),
        args: vec![format!("{}/tests/fixtures/mcp_echo.py", env!("CARGO_MANIFEST_DIR"))],
        env: Default::default(),
        trust,
    };
    friday::mcp::Server::start(&cfg).expect("python3 runs the test MCP server")
}

#[test]
fn mcp_tools_load_only_when_jev_says_the_server_is_needed() {
    let fake = Fake::start();
    *fake.jev.lock().unwrap() = jev(&[("mcp:demo", json!(false), 0.9)]);
    fake.script(vec![say("no tools needed", &[])]);
    let mut r = rig_with(&fake, no_needle(), true, |_| {}, vec![echo_server(true)]);
    r.agent.turn("hello");
    assert!(!offered(&fake.requests()[0]).iter().any(|t| t.starts_with("mcp__")));
    r.finish();

    *fake.jev.lock().unwrap() = jev(&[("mcp:demo", json!(true), 0.9)]);
    fake.script(vec![say("", &[("mcp__demo__echo", json!({"text": "hi"})), ("mcp__demo__echo", json!({"text": "fail"}))]), say("done", &[])]);
    r.agent.turn("echo hi");
    assert!(offered(&fake.requests()[1]).contains(&"mcp__demo__echo".to_string()));
    let results: Vec<String> = r.agent.messages.iter().filter(|m| m["role"] == "tool").map(|m| m["content"].as_str().unwrap().to_string()).collect();
    assert_eq!(results, ["echo: hi", "error: echo: fail"]);
    r.finish();
}

#[test]
fn an_untrusted_mcp_tool_asks_first() {
    let fake = Fake::start();
    *fake.jev.lock().unwrap() = jev(&[("mcp:demo", json!(true), 0.9)]);
    fake.script(vec![say("", &[("mcp__demo__echo", json!({"text": "hi"}))]), say("ok", &[])]);
    let mut r = rig_with(&fake, no_needle(), false, |_| {}, vec![echo_server(false)]);
    r.agent.turn("echo hi");
    let result = r.agent.messages.iter().find(|m| m["role"] == "tool").unwrap()["content"].clone();
    assert_eq!(result, tools::DENIED);
    r.finish();
}

#[test]
fn undo_restores_changed_and_deleted_files_and_removes_new_ones() {
    let home = tempfile::tempdir().unwrap();
    let work = tempfile::tempdir().unwrap();
    let w = work.path();
    std::fs::write(w.join("keep.txt"), "original").unwrap();
    std::fs::write(w.join("gone.txt"), "will be deleted").unwrap();
    std::fs::create_dir_all(w.join("node_modules")).unwrap();
    std::fs::write(w.join("node_modules/big.js"), "x").unwrap();
    assert!(friday::checkpoint::snapshot(home.path(), w, "before").unwrap().is_some());
    std::fs::write(w.join("keep.txt"), "clobbered").unwrap();
    std::fs::remove_file(w.join("gone.txt")).unwrap();
    std::fs::write(w.join("new.txt"), "made by the agent").unwrap();
    let summary = friday::checkpoint::undo(home.path(), w).unwrap();
    assert_eq!(std::fs::read_to_string(w.join("keep.txt")).unwrap(), "original");
    assert_eq!(std::fs::read_to_string(w.join("gone.txt")).unwrap(), "will be deleted");
    assert!(!w.join("new.txt").exists());
    assert!(w.join("node_modules/big.js").exists(), "excluded folders are left alone");
    assert_eq!(summary, "restored 2 file(s), removed 1 new file(s)");
    assert!(friday::checkpoint::undo(home.path(), w).is_err(), "nothing left to undo");
}

#[test]
fn a_turn_that_changes_files_checkpoints_once_first() {
    let fake = Fake::start();
    let mut r = rig(&fake, no_needle(), true, |_| {});
    let file = std::env::current_dir().unwrap().join("target/checkpoint-probe.txt");
    fake.script(vec![say("", &[("write_file", json!({"path": file, "content": "a"})), ("write_file", json!({"path": file, "content": "b"}))]), say("ok", &[])]);
    r.agent.turn("write twice");
    let store = std::fs::read_dir(r.home().join("checkpoints")).unwrap().next().unwrap().unwrap().path();
    assert_eq!(std::fs::read_to_string(store.join("friday-undo")).unwrap().lines().count(), 1, "one checkpoint per turn");
    let _ = std::fs::remove_file(file);
    r.finish();
}

#[test]
fn todo_ask_user_and_fetch_url_work() {
    let page = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let url = format!("http://127.0.0.1:{}/", page.server_addr().to_ip().unwrap().port());
    std::thread::spawn(move || {
        for req in page.incoming_requests() {
            let html = "<html><head><style>p{}</style><script>evil()</script></head><body><h1>Docs</h1><p>Use &lt;b&gt; &amp; more</p></body></html>";
            let header = tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"text/html"[..]).unwrap();
            let _ = req.respond(tiny_http::Response::from_string(html).with_header(header));
        }
    });
    let fake = Fake::start();
    *fake.jev.lock().unwrap() = jev(&[("web", json!(true), 0.9)]);
    fake.script(vec![
        say("", &[("todo", json!({"items": ["[x] read docs", "write code"]})), ("ask_user", json!({"question": "which db?"})), ("fetch_url", json!({"url": url}))]),
        say("ok", &[]),
    ]);
    let mut r = rig(&fake, no_needle(), true, |_| {});
    r.agent.turn("x");
    let results: Vec<String> = r.agent.messages.iter().filter(|m| m["role"] == "tool").map(|m| m["content"].as_str().unwrap().to_string()).collect();
    assert_eq!(results[0], "[x] read docs\nwrite code");
    assert!(results[1].starts_with("Nobody is here to answer"));
    assert!(results[2].ends_with("Docs Use <b> & more") && !results[2].contains("evil"), "{}", results[2]);
    assert!(r.ui.saw("1/2 done"));
    r.finish();
}
