//! The real on-device models. Skipped (with a message) where they cannot be fetched.
use friday::cactus;

const TOOLS: &str = r#"[{"name":"read_file","description":"Read a text file","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}]"#;

#[test]
fn needle_makes_a_tool_call_and_mirage_reads_an_image() {
    let reply = match cactus::complete("", TOOLS, "read the file notes.txt", None) {
        Ok(reply) => reply,
        Err(e) => return eprintln!("skipped: {e:#}"),
    };
    assert_eq!(reply["function_calls"][0]["name"], "read_file");
    assert_eq!(reply["function_calls"][0]["arguments"]["path"], "notes.txt");
    assert!(reply["confidence"].as_f64().unwrap() > 0.0);

    // A white image with a black bar: enough to prove the vision path returns structure.
    let (w, h) = (160u32, 80u32);
    let mut rgb = vec![255u8; (w * h * 3) as usize];
    for y in 30..50 {
        for x in 20..140 {
            let i = ((y * w + x) * 3) as usize;
            rgb[i..i + 3].copy_from_slice(&[0, 0, 0]);
        }
    }
    let seen = cactus::describe(&rgb, w, h).unwrap();
    assert!(seen.get("objects").is_some() && seen.get("text").is_some(), "{seen}");
    // And Needle still works after Mirage was loaded alongside it.
    assert!(cactus::complete("", TOOLS, "read the file a.txt", None).is_ok());
}

/// The reflex gate, on the real model: given the tool Jev picked, Needle's call is either right and
/// grounded in the request, or rejected. It is never a wrong action.
#[test]
fn a_reflex_is_right_or_refused_never_wrong() {
    use friday::agent::grounded;
    use serde_json::json;
    if cactus::complete("", "[]", "x", None).is_err() {
        return eprintln!("skipped: no Needle here");
    }
    let all = friday::tools::schemas(&["core".to_string(), "web".to_string()]);
    let cases = [
        ("read notes.txt", "read_file", json!({"path": "notes.txt"})),
        ("show me config.toml", "read_file", json!({"path": "config.toml"})),
        ("open src/main.rs", "read_file", json!({"path": "src/main.rs"})),
        ("run cargo test", "shell", json!({"command": "cargo test"})),
        ("run the tests with npm test", "shell", json!({"command": "npm test"})),
        ("list the files here", "shell", json!({"command": "ls"})),
        ("fetch https://example.com", "fetch_url", json!({"url": "https://example.com"})),
        ("search my memory for the deploy key", "recall", json!({"query": "deploy key"})),
    ];
    let mut taken = 0;
    for (request, tool, want) in cases {
        let one: Vec<_> = all.iter().filter(|s| s["name"] == tool).cloned().collect();
        let reply = cactus::complete("", &json!(one).to_string(), request, None).unwrap();
        let calls = reply["function_calls"].as_array().cloned().unwrap_or_default();
        let accepted = calls.len() == 1 && calls[0]["name"] == tool && grounded(&calls[0]["arguments"], request);
        eprintln!("{request:40} accepted={accepted} {}", json!(calls));
        if accepted {
            taken += 1;
            assert_eq!(calls[0]["arguments"], want, "an accepted reflex must be the right call");
        }
    }
    eprintln!("{taken}/8 handled without an LLM");
    assert!(taken >= 5, "only {taken}/8 reflexes taken");
}

/// With no Chrome installed, the browser tool fetches Chromium into ~/.friday/chromium once.
/// Ignored by default: it downloads ~150 MB. Run with `cargo test -- --ignored chromium`.
#[test]
#[ignore]
fn chromium_is_fetched_when_none_is_installed() {
    let home = tempfile::tempdir().unwrap();
    std::env::set_var("FRIDAY_HOME", home.path());
    std::env::remove_var("FRIDAY_CHROMIUM");
    std::env::set_var("FRIDAY_CHROMIUM_NO_SANDBOX", "1");
    std::env::set_var("PATH", ""); // hide any installed Chrome
    let mut browser = friday::operate::Browser::open("data:text/html,<button>Fetched</button>").expect("fetches and starts Chromium");
    use friday::operate::Env;
    assert_eq!(browser.observe().unwrap().0, ["button: Fetched"]);
    assert!(home.path().join("chromium").exists());
}
