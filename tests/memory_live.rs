//! Recall quality with the real local embedding model. Skipped where the model cannot be fetched.
use friday::{config::Config, memory, system1::S1};
use std::sync::Arc;

const DOCS: [&str; 10] = [
    "I'm allergic to peanuts so never suggest satay or pad thai with peanuts.",
    "The staging database password rotates every Monday; ask Priya for the new one.",
    "We decided to use Postgres with pgvector instead of Pinecone for the search index.",
    "My daughter's birthday is on March 14th and she wants a telescope.",
    "The deploy script is at scripts/deploy.sh and needs the AWS_PROFILE=prod env var.",
    "I prefer tabs over spaces and 100 character line width in Rust code.",
    "Flight to Tokyo leaves on the 3rd of December at 9am from SFO.",
    "The login bug was caused by a race in the session refresh middleware.",
    "Remember that the client meeting with Acme moved to Thursday afternoon.",
    "I usually go running in the morning before work, around 6:30.",
];
const QUERIES: [(&str, usize); 8] = [
    ("what food should I avoid recommending?", 0),
    ("which vector database did we pick?", 2),
    ("how do I deploy to production?", 4),
    ("when is my kid's birthday?", 3),
    ("what caused the authentication issue?", 7),
    ("what are my code formatting preferences?", 5),
    ("when do I fly to Japan?", 6),
    ("when is the Acme meeting?", 8),
];

#[test]
fn hybrid_search_finds_paraphrased_memories() {
    let cache = std::env::home_dir().unwrap().join(".cache/friday");
    if memory::local_embedder(&cache)("probe").is_none() {
        return eprintln!("skipped: no embedding model here");
    }
    let dir = tempfile::tempdir().unwrap();
    let cfg = Config { home: dir.path().into(), user: "ryan".into(), ..Default::default() };
    let m = memory::Memory::open(&cfg, Arc::new(S1::new(&cfg)), memory::local_embedder(&cache)).unwrap();
    for doc in DOCS {
        assert_eq!(m.add("ryan", "2026-11-10", doc, "test").unwrap(), 1);
    }
    assert_eq!(m.add("ryan", "2026-11-10", DOCS[0], "test").unwrap(), 0, "the same words are filed once");
    let mut first = 0;
    let mut top3 = 0;
    for (query, want) in QUERIES {
        let hits = m.search(query, 3).unwrap();
        let rank = hits.iter().position(|h| h.text == DOCS[want]);
        eprintln!("{query:45} rank {:?} of {}", rank.map(|r| r + 1), hits.len());
        first += (rank == Some(0)) as usize;
        top3 += rank.is_some() as usize;
    }
    eprintln!("top-1 {first}/8, top-3 {top3}/8");
    assert!(first >= 7 && top3 >= 8, "recall regressed: top-1 {first}/8, top-3 {top3}/8");
    // Something unrelated to every memory should bring back little or nothing.
    let noise = m.search("quantum chromodynamics lattice gauge theory", 3).unwrap();
    eprintln!("unrelated query returned {} hits", noise.len());
    assert!(noise.len() <= 1);
}
