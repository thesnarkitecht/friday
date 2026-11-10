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
