//! Ctrl-C sets a process-wide flag, so this runs in its own test binary.
mod common;
use common::*;
use serde_json::json;

#[test]
fn an_interrupt_stops_the_turn_instead_of_escalating() {
    let fake = Fake::start();
    fake.script(vec![say("", &[("shell", json!({"command": "sleep 30"}))]), say("should not be reached", &[])]);
    let mut r = rig(&fake, no_needle(), true, |_| {});
    let flip = std::thread::spawn(|| {
        std::thread::sleep(std::time::Duration::from_millis(600));
        friday::set_interrupted(true);
    });
    let started = std::time::Instant::now();
    let reply = r.agent.turn("wait");
    flip.join().unwrap();
    friday::set_interrupted(false);
    assert_eq!(reply, "Stopped.");
    assert!(started.elapsed().as_secs() < 10);
    let tool = r.agent.messages.iter().find(|m| m["role"] == "tool").unwrap();
    assert!(tool["content"].as_str().unwrap().starts_with("stopped: interrupted"));
    assert_eq!(fake.requests().len(), 1, "no model call after the interrupt");
    r.finish();
}
