//! Memory, built in: one local SQLite file, no server, nothing leaves the machine.
//!
//! The palace (after MemPalace) keeps every word verbatim in drawers, filed by wing (who said
//! it) and room (the day), append-only, and finds them again with full-text ranking fused with
//! on-device embeddings. Nothing is ever summarised in place of the original.
//!
//! Peers (after Honcho) is the model of who Friday works with: a background deriver turns what
//! a person says into conclusions about them, a "dream" pass consolidates those into a peer
//! card, and `ask` answers questions about the person from both.
//!
//! Writes and derivation run on a background thread; the conversation never waits on memory.

use crate::config::Config;
use crate::models;
use crate::system1::S1;
use anyhow::Result;
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex};

const CHUNK: usize = 1800; // characters per drawer
const DERIVE_BATCH: i64 = 6; // without Jev, derive once this many messages are waiting
const DREAM_EVERY: i64 = 12; // rebuild a peer card after this many new conclusions

/// text -> embedding, or None when no model is available (search is then full-text only).
pub type Embedder = Box<dyn Fn(&str) -> Option<Vec<f32>> + Send + Sync>;

pub struct Hit {
    pub wing: String,
    pub room: String,
    pub text: String,
}

enum Job {
    Remember { speaker: String, text: String },
    Flush(Sender<()>),
}

pub struct Memory {
    cfg: Config,
    s1: Arc<S1>,
    db: Mutex<Connection>,
    embed: Embedder,
    jobs: Mutex<Sender<Job>>,
    pub errors: Mutex<HashMap<&'static str, String>>,
}

/// all-MiniLM-L6-v2 on this machine (ONNX): the sentence-embedding model MemPalace itself
/// defaults to. Downloaded once on first use; if that fails, search is full-text only.
pub fn local_embedder(cache: &std::path::Path) -> Embedder {
    use fastembed::{EmbeddingModel, TextEmbedding, TextInitOptions};
    let options = TextInitOptions::new(EmbeddingModel::AllMiniLML6V2).with_cache_dir(cache.join("models")).with_show_download_progress(false);
    let model = std::sync::OnceLock::<Option<Mutex<TextEmbedding>>>::new();
    Box::new(move |text| {
        let model = model.get_or_init(|| TextEmbedding::try_new(options.clone()).ok().map(Mutex::new)).as_ref()?;
        model.lock().unwrap().embed(vec![text], None).ok()?.pop()
    })
}

fn to_blob(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
}

fn from_blob(b: &[u8]) -> Vec<f32> {
    b.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect()
}

fn cosine(a: &[f32], b: &[f32]) -> f32 {
    let (dot, na, nb) = a.iter().zip(b).fold((0f32, 0f32, 0f32), |(d, x, y), (p, q)| (d + p * q, x + p * p, y + q * q));
    if na == 0.0 || nb == 0.0 { 0.0 } else { dot / (na.sqrt() * nb.sqrt()) }
}

fn hash(parts: &[&str]) -> String {
    let mut h = Sha256::new();
    for p in parts {
        h.update(p.as_bytes());
        h.update([0]);
    }
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

fn today() -> String {
    chrono::Local::now().format("%Y-%m-%d").to_string()
}

/// Split into pieces of at most CHUNK characters, at line breaks where possible. Joining the
/// pieces gives back the original text exactly.
pub fn chunks(text: &str) -> Vec<String> {
    let mut out = vec![String::new()];
    for line in text.split_inclusive('\n') {
        if out.last().unwrap().chars().count() + line.chars().count() > CHUNK && !out.last().unwrap().is_empty() {
            out.push(String::new());
        }
        for c in line.chars() {
            if out.last().unwrap().chars().count() >= CHUNK {
                out.push(String::new());
            }
            out.last_mut().unwrap().push(c);
        }
    }
    out
}

/// A full-text query that matches any of the words. User text never reaches FTS syntax raw.
fn fts_query(text: &str) -> String {
    let words: Vec<String> = text
        .split(|c: char| !c.is_alphanumeric())
        .filter(|w| w.chars().count() > 1)
        .take(24)
        .map(|w| format!("\"{w}\""))
        .collect();
    words.join(" OR ")
}

impl Memory {
    pub fn open(cfg: &Config, s1: Arc<S1>, embed: Embedder) -> Result<Arc<Memory>> {
        std::fs::create_dir_all(&cfg.home)?;
        let db = Connection::open(cfg.home.join("memory.db"))?;
        db.busy_timeout(std::time::Duration::from_secs(10))?;
        db.execute_batch(
            "PRAGMA journal_mode = WAL;
             CREATE TABLE IF NOT EXISTS drawers(id INTEGER PRIMARY KEY, wing TEXT NOT NULL, room TEXT NOT NULL,
                 content TEXT NOT NULL, added_by TEXT NOT NULL, hash TEXT UNIQUE NOT NULL, vec BLOB);
             CREATE VIRTUAL TABLE IF NOT EXISTS drawers_fts USING fts5(content, tokenize = 'porter unicode61');
             CREATE TABLE IF NOT EXISTS conclusions(id INTEGER PRIMARY KEY, peer TEXT NOT NULL, kind TEXT NOT NULL,
                 content TEXT NOT NULL, day TEXT NOT NULL, hash TEXT UNIQUE NOT NULL);
             CREATE VIRTUAL TABLE IF NOT EXISTS conclusions_fts USING fts5(content, tokenize = 'porter unicode61');
             CREATE TABLE IF NOT EXISTS cards(peer TEXT PRIMARY KEY, card TEXT NOT NULL, built_at INTEGER NOT NULL);
             CREATE TABLE IF NOT EXISTS pending(id INTEGER PRIMARY KEY, peer TEXT NOT NULL, content TEXT NOT NULL);",
        )?;
        let (jobs, inbox) = channel();
        let memory = Arc::new(Memory { cfg: cfg.clone(), s1, db: Mutex::new(db), embed, jobs: Mutex::new(jobs), errors: Mutex::default() });
        let worker = memory.clone();
        std::thread::spawn(move || {
            for job in inbox {
                match job {
                    Job::Remember { speaker, text } => {
                        let filed = worker.add(&speaker, &today(), &text, "friday");
                        worker.note("palace", filed.map(|_| ()));
                        if speaker == worker.cfg.user {
                            let derived = worker.observe(&speaker, &text);
                            worker.note("peers", derived);
                        }
                    }
                    Job::Flush(done) => drop(done.send(())),
                }
            }
        });
        Ok(memory)
    }

    fn note(&self, part: &'static str, result: Result<()>) {
        if let Err(e) = result {
            self.errors.lock().unwrap().insert(part, format!("{e:#}"));
        }
    }

    /// Store `text` verbatim, attributed to `speaker`. Returns at once; the work is queued.
    pub fn remember(&self, speaker: &str, text: &str) {
        if !text.trim().is_empty() {
            let _ = self.jobs.lock().unwrap().send(Job::Remember { speaker: speaker.into(), text: text.into() });
        }
    }

    /// Wait until everything queued so far has been written and derived.
    pub fn flush(&self) {
        let (done, wait) = channel();
        if self.jobs.lock().unwrap().send(Job::Flush(done)).is_ok() {
            let _ = wait.recv();
        }
    }

    // -- the palace -------------------------------------------------------------------------

    /// File content into a wing and room, verbatim. Returns how many new drawers were added.
    pub fn add(&self, wing: &str, room: &str, content: &str, added_by: &str) -> Result<usize> {
        // Embed before taking the database lock: the model is the slow part.
        let pieces: Vec<(String, Option<Vec<f32>>)> = chunks(content).into_iter().map(|c| { let v = (self.embed)(&c); (c, v) }).collect();
        let db = self.db.lock().unwrap();
        let mut added = 0;
        for (piece, vec) in pieces {
            let inserted = db.execute(
                "INSERT OR IGNORE INTO drawers(wing, room, content, added_by, hash, vec) VALUES (?, ?, ?, ?, ?, ?)",
                params![wing, room, piece, added_by, hash(&[wing, &piece]), vec.map(|v| to_blob(&v))],
            )?;
            if inserted == 1 {
                db.execute("INSERT INTO drawers_fts(rowid, content) VALUES (?, ?)", params![db.last_insert_rowid(), piece])?;
                added += 1;
            }
        }
        Ok(added)
    }

    /// The drawers most relevant to `query`: full-text rank fused with embedding similarity.
    pub fn search(&self, query: &str, limit: usize) -> Result<Vec<Hit>> {
        let query_vec = (self.embed)(query);
        let db = self.db.lock().unwrap();
        let mut score: HashMap<i64, f64> = HashMap::new();
        let words = fts_query(query);
        if !words.is_empty() {
            let mut q = db.prepare("SELECT rowid FROM drawers_fts WHERE drawers_fts MATCH ? ORDER BY bm25(drawers_fts) LIMIT 30")?;
            for (rank, id) in q.query_map([&words], |r| r.get::<_, i64>(0))?.flatten().enumerate() {
                *score.entry(id).or_default() += 1.0 / (60.0 + rank as f64);
            }
        }
        if let Some(qv) = query_vec {
            // shortcut: an exact scan of every vector; add an index once a palace passes ~100k drawers.
            let mut q = db.prepare("SELECT id, vec FROM drawers WHERE vec IS NOT NULL")?;
            let rows: Vec<(i64, Vec<f32>)> = q.query_map([], |r| Ok((r.get(0)?, from_blob(&r.get::<_, Vec<u8>>(1)?))))?.flatten().collect();
            let mut near: Vec<(f32, i64)> = rows.iter().map(|(id, v)| (cosine(&qv, v), *id)).collect();
            near.sort_by(|a, b| b.0.total_cmp(&a.0));
            for (rank, (similarity, id)) in near.into_iter().take(10).enumerate() {
                // Something is always "nearest"; only count it if it is actually close, or
                // the words already matched.
                if similarity >= MIN_SIMILARITY || score.contains_key(&id) {
                    *score.entry(id).or_default() += 1.0 / (60.0 + rank as f64);
                }
            }
        }
        let mut ranked: Vec<(i64, f64)> = score.into_iter().collect();
        ranked.sort_by(|a, b| b.1.total_cmp(&a.1).then(b.0.cmp(&a.0)));
        let mut hits = Vec::new();
        for (id, _) in ranked.into_iter().take(limit) {
            hits.push(db.query_row("SELECT wing, room, content FROM drawers WHERE id = ?", [id], |r| {
                Ok(Hit { wing: r.get(0)?, room: r.get(1)?, text: r.get(2)? })
            })?);
        }
        Ok(hits)
    }

    pub fn counts(&self) -> (i64, i64) {
        let db = self.db.lock().unwrap();
        let count = |table: &str| db.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0)).unwrap_or(0);
        (count("drawers"), count("conclusions"))
    }

    // -- peers ------------------------------------------------------------------------------

    /// Take in something `peer` said, and derive conclusions about them when it is worth it.
    fn observe(&self, peer: &str, text: &str) -> Result<()> {
        let waiting: i64 = {
            let db = self.db.lock().unwrap();
            db.execute("INSERT INTO pending(peer, content) VALUES (?, ?)", params![peer, text])?;
            db.query_row("SELECT count(*) FROM pending WHERE peer = ?", [peer], |r| r.get(0))?
        };
        let derive_now = if self.s1.has_jev() {
            // Jev reads every message and decides, in milliseconds, whether it says anything lasting.
            let a = self.s1.decide(
                text,
                &json!({"lasting": {"type": "noul", "instructions": "This message states or implies something lasting about its author: a preference, trait, goal, relationship, routine, constraint or standing decision."}}),
                None,
            );
            let lasting = &a["lasting"];
            if !lasting.yes() && lasting.confidence >= self.cfg.s1.confidence {
                self.db.lock().unwrap().execute("DELETE FROM pending WHERE peer = ?", [peer])?;
            }
            lasting.yes()
        } else {
            waiting >= DERIVE_BATCH
        };
        if derive_now {
            self.derive(peer)?;
        }
        Ok(())
    }

    /// One structured model call turns the waiting messages into conclusions.
    fn derive(&self, peer: &str) -> Result<()> {
        let messages: Vec<String> = {
            let db = self.db.lock().unwrap();
            let mut q = db.prepare("SELECT content FROM pending WHERE peer = ? ORDER BY id")?;
            let rows = q.query_map([peer], |r| r.get(0))?.flatten().collect();
            rows
        };
        if messages.is_empty() {
            return Ok(());
        }
        self.s1.count("s2");
        let raw = models::ask(&self.cfg, "fast", &format!(
            "Messages written by {peer} follow. Extract what they establish about {peer} that will still be true \
             next month. Reply with only a JSON array of objects {{\"kind\": \"explicit\" | \"deductive\", \"text\": \"...\"}}: \
             explicit for what they directly said, deductive for what follows from it. One short sentence each, \
             naming {peer}. Reply [] if nothing lasting was said.\n\n{}",
            messages.iter().map(|m| format!("- {m}")).collect::<Vec<_>>().join("\n")
        ))?;
        let found: Vec<Value> = raw.find('[').zip(raw.rfind(']')).and_then(|(a, b)| serde_json::from_str(raw.get(a..=b)?).ok()).unwrap_or_default();
        let total: i64 = {
            let db = self.db.lock().unwrap();
            for c in &found {
                let (Some(text), kind) = (c["text"].as_str(), c["kind"].as_str().unwrap_or("explicit")) else { continue };
                let key = hash(&[peer, &text.to_lowercase()]);
                if db.execute("INSERT OR IGNORE INTO conclusions(peer, kind, content, day, hash) VALUES (?, ?, ?, ?, ?)", params![peer, kind, text, today(), key])? == 1 {
                    db.execute("INSERT INTO conclusions_fts(rowid, content) VALUES (?, ?)", params![db.last_insert_rowid(), text])?;
                }
            }
            db.execute("DELETE FROM pending WHERE peer = ?", [peer])?;
            db.query_row("SELECT count(*) FROM conclusions WHERE peer = ?", [peer], |r| r.get(0))?
        };
        let built_at: i64 = self.db.lock().unwrap().query_row("SELECT built_at FROM cards WHERE peer = ?", [peer], |r| r.get(0)).optional()?.unwrap_or(0);
        if total - built_at >= DREAM_EVERY {
            self.dream(peer, total)?;
        }
        Ok(())
    }

    /// Consolidate everything concluded about a peer into one short card.
    fn dream(&self, peer: &str, total: i64) -> Result<()> {
        let all = self.conclusions(peer, "", 300)?;
        self.s1.count("s2");
        let card = models::ask(&self.cfg, "fast", &format!(
            "Consolidate these conclusions about {peer} into a profile of at most 25 short lines: who they are, \
             how they like to work, what they are doing, what to avoid. Merge duplicates; where two conflict, \
             keep the later one. Reply with only the profile.\n\n{}",
            all.join("\n")
        ))?;
        self.db.lock().unwrap().execute(
            "INSERT INTO cards(peer, card, built_at) VALUES (?, ?, ?) ON CONFLICT(peer) DO UPDATE SET card = excluded.card, built_at = excluded.built_at",
            params![peer, card, total],
        )?;
        Ok(())
    }

    /// Conclusions about `peer`: the ones matching `query` first, then the most recent.
    fn conclusions(&self, peer: &str, query: &str, limit: usize) -> Result<Vec<String>> {
        let db = self.db.lock().unwrap();
        let mut out: Vec<String> = Vec::new();
        let words = fts_query(query);
        if !words.is_empty() {
            let mut q = db.prepare(
                "SELECT c.content FROM conclusions_fts f JOIN conclusions c ON c.id = f.rowid
                 WHERE conclusions_fts MATCH ? AND c.peer = ? ORDER BY bm25(conclusions_fts) LIMIT ?",
            )?;
            out.extend(q.query_map(params![words, peer, limit as i64], |r| r.get::<_, String>(0))?.flatten());
        }
        let mut q = db.prepare("SELECT content FROM conclusions WHERE peer = ? ORDER BY id DESC LIMIT ?")?;
        for recent in q.query_map(params![peer, limit as i64], |r| r.get::<_, String>(0))?.flatten() {
            if out.len() < limit && !out.contains(&recent) {
                out.push(recent);
            }
        }
        Ok(out)
    }

    fn card(&self, peer: &str) -> String {
        self.db.lock().unwrap().query_row("SELECT card FROM cards WHERE peer = ?", [peer], |r| r.get(0)).unwrap_or_default()
    }

    // -- what the agent uses ------------------------------------------------------------------

    /// Context worth putting in front of the model for `query`.
    pub fn recall(&self, query: &str, limit: usize) -> String {
        let mut parts = Vec::new();
        match self.search(query, limit) {
            Ok(hits) => parts.extend(hits.into_iter().map(|h| format!("[{} · {}] {}", h.wing, h.room, h.text))),
            Err(e) => self.note("palace", Err(e)),
        }
        match self.conclusions(&self.cfg.user, query, 6) {
            Ok(known) if !known.is_empty() => parts.push(format!("[known about {}]\n- {}", self.cfg.user, known.join("\n- "))),
            Ok(_) => {}
            Err(e) => self.note("peers", Err(e)),
        }
        parts.join("\n")
    }

    /// A reasoned answer about the user, from everything derived and everything they said.
    pub fn ask(&self, question: &str) -> Result<String> {
        let user = &self.cfg.user;
        let said: Vec<String> = self.search(question, 6)?.into_iter().filter(|h| &h.wing == user).map(|h| h.text).collect();
        self.s1.count("s2");
        models::ask(&self.cfg, "fast", &format!(
            "Answer the question about {user} from what is known. Say so plainly if it is not known; do not guess.\n\n\
             PROFILE:\n{}\n\nCONCLUSIONS:\n{}\n\nTHINGS {user} SAID:\n{}\n\nQUESTION: {question}",
            self.card(user),
            self.conclusions(user, question, 12)?.join("\n"),
            said.join("\n---\n")
        ))
    }

    /// For the start of a session: who the user says they are, and who Friday has found them to be.
    pub fn wake_up(&self) -> String {
        let identity = std::fs::read_to_string(self.cfg.home.join("identity.md")).unwrap_or_default();
        [identity.trim(), self.card(&self.cfg.user).trim()].iter().filter(|s| !s.is_empty()).copied().collect::<Vec<_>>().join("\n\n")
    }
}

/// Cosine below this is not a match for MiniLM; calibrated in tests/memory_live.rs.
const MIN_SIMILARITY: f32 = 0.25;
