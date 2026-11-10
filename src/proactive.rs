//! Proactive mode: Friday watches things and speaks first.
//!
//! Events come from [[watch]] entries in the config (a timer, or a command whose output changed)
//! and from mail other agents send. System 1 decides whether each event is worth a turn at all,
//! so an idle watcher costs nothing.

use crate::agent::Agent;
use crate::config::Watch;
use anyhow::Result;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::process::Command;

/// Events ready to fire at `now` (seconds). Updates `state` in place.
pub fn due_events(watches: &[Watch], state: &mut Value, now: u64) -> Vec<String> {
    let mut events = Vec::new();
    for w in watches {
        let seen = &mut state[&w.name];
        if seen.is_null() {
            *seen = json!({"at": null, "hash": ""});
        }
        if seen["at"].as_u64().is_some_and(|at| now.saturating_sub(at) < w.every) {
            continue;
        }
        seen["at"] = json!(now);
        if w.cmd.is_empty() {
            events.push(w.prompt.clone());
            continue;
        }
        let Ok(out) = Command::new("sh").arg("-c").arg(&w.cmd).output() else { continue };
        let output = format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
        let digest: String = Sha256::digest(output.as_bytes()).iter().map(|b| format!("{b:02x}")).collect();
        let first_look = seen["hash"] == "";
        if seen["hash"] != digest.as_str() {
            seen["hash"] = json!(digest);
            if !first_look {
                // The first run only records the baseline.
                let shown: String = output.chars().take(4000).collect();
                events.push(format!("{}\n\nOutput of `{}`:\n{shown}", w.prompt, w.cmd));
            }
        }
    }
    events
}

fn worth_a_turn(agent: &Agent, event: &str) -> bool {
    let a = agent.shared.s1.decide(
        event,
        &json!({"act": {"type": "noul", "instructions": "This event needs the user's attention or an action now; it is not routine noise.", "default": true}}),
        None,
    );
    a["act"].yes() || a["act"].confidence < agent.shared.cfg.s1.confidence
}

fn notify(agent: &Agent, text: &str) {
    use std::io::Write;
    let stamp = chrono::Local::now().format("%Y-%m-%d %H:%M");
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(agent.shared.cfg.home.join("notifications.md")) {
        let _ = writeln!(f, "\n## {stamp} {}\n{text}", agent.name);
    }
    let short: String = text.chars().take(300).collect();
    // Whichever notifier this desktop has; neither existing is fine.
    let _ = Command::new("notify-send").args([&agent.name, &short]).status();
    let _ = Command::new("osascript").args(["-e", &format!("display notification {} with title {}", json!(short), json!(agent.name))]).status();
}

/// One pass: collect what is due, let System 1 drop the noise, act on the rest.
pub fn tick(agent: &mut Agent, state: &mut Value, now: u64) -> Result<()> {
    let watches = agent.shared.cfg.watch.clone();
    let mut events = due_events(&watches, state, now);
    events.extend(agent.shared.bus.inbox(&agent.name)?.into_iter().map(|(from, body)| format!("Message from agent {from}: {body}")));
    for event in events {
        if !worth_a_turn(agent, &event) {
            let shown: String = event.chars().take(80).collect();
            agent.shared.ui.line(&format!("  · skipped (System 1: not worth a turn): {shown}"));
            continue;
        }
        let reply = agent.turn(&event);
        agent.shared.ui.line(&reply);
        notify(agent, &reply);
    }
    Ok(())
}

pub fn watch(agent: &mut Agent) -> Result<()> {
    let path = agent.shared.cfg.home.join("watch.json");
    let mut state: Value = std::fs::read_to_string(&path).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or(json!({}));
    loop {
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_secs();
        tick(agent, &mut state, now)?;
        std::fs::write(&path, state.to_string())?;
        std::thread::sleep(std::time::Duration::from_secs(5));
    }
}
