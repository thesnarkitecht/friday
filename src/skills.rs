//! Skills: SKILL.md folders (the format Hermes and Claude Code use), chosen by System 1.
//!
//! What makes this more than a folder of prompts:
//! - Jev ranks the whole library for each request, so no skill index sits in the big model's context.
//! - Every use is scored (did the turn resolve without escalating?), and a skill that keeps
//!   losing is retired from selection until it is rewritten.
//! - After a multi-step turn Jev decides whether the work becomes a new skill or a rewrite of
//!   the one that was followed, and System 2 writes it.

use crate::config::Config;
use anyhow::Result;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

pub struct Skill {
    pub description: String,
    pub path: PathBuf,
}

fn field(text: &str, name: &str) -> Option<String> {
    let prefix = format!("{name}:");
    text.lines().take(40).find_map(|l| l.strip_prefix(&prefix)).map(|v| v.trim().trim_matches('"').to_string()).filter(|v| !v.is_empty())
}

fn find(dir: &Path, depth: u8, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    let mut paths: Vec<PathBuf> = entries.flatten().map(|e| e.path()).collect();
    paths.sort();
    for path in paths {
        if path.is_dir() && depth > 0 {
            find(&path, depth - 1, out);
        } else if path.file_name().is_some_and(|n| n == "SKILL.md") {
            out.push(path);
        }
    }
}

/// name -> skill. Later directories override earlier ones.
pub fn load(cfg: &Config) -> BTreeMap<String, Skill> {
    let mut found = BTreeMap::new();
    for dir in &cfg.skill_dirs {
        let mut files = Vec::new();
        find(dir, 4, &mut files); // Hermes nests skills under category folders
        for path in files {
            let text = std::fs::read_to_string(&path).unwrap_or_default();
            let folder = path.parent().and_then(Path::file_name).map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            let name = field(&text, "name").unwrap_or(folder);
            found.insert(name, Skill { description: field(&text, "description").unwrap_or_default(), path });
        }
    }
    found
}

/// Write a model-authored SKILL.md under the home skills folder. None if it has no usable name.
pub fn save(cfg: &Config, text: &str) -> Result<Option<String>> {
    let text = text.trim();
    let text = text.strip_prefix("```markdown").or_else(|| text.strip_prefix("```md")).or_else(|| text.strip_prefix("```")).unwrap_or(text);
    let text = text.strip_suffix("```").unwrap_or(text).trim();
    let Some(name) = field(text, "name") else { return Ok(None) };
    // The name came from a model: reduce it to one safe path segment.
    let mut slug = String::new();
    for c in name.to_lowercase().chars() {
        if c.is_ascii_alphanumeric() {
            slug.push(c);
        } else if !slug.ends_with('-') {
            slug.push('-');
        }
    }
    let slug: String = slug.trim_matches('-').chars().take(60).collect();
    if slug.is_empty() {
        return Ok(None);
    }
    let dir = cfg.home.join("skills").join(&slug);
    std::fs::create_dir_all(&dir)?;
    std::fs::write(dir.join("SKILL.md"), format!("{text}\n"))?;
    Ok(Some(slug))
}

/// name -> (uses, wins).
pub fn scores(cfg: &Config) -> BTreeMap<String, (u32, u32)> {
    std::fs::read_to_string(cfg.home.join("skills/scores.json")).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default()
}

fn write_scores(cfg: &Config, data: &BTreeMap<String, (u32, u32)>) -> Result<()> {
    std::fs::create_dir_all(cfg.home.join("skills"))?;
    Ok(std::fs::write(cfg.home.join("skills/scores.json"), serde_json::to_string(data)?)?)
}

pub fn record(cfg: &Config, names: &[String], won: bool) -> Result<()> {
    let mut data = scores(cfg);
    for name in names {
        let entry = data.entry(name.clone()).or_default();
        *entry = (entry.0 + 1, entry.1 + won as u32);
    }
    write_scores(cfg, &data)
}

/// A skill that has won fewer than a quarter of four or more uses stops being offered.
pub fn healthy(score: Option<&(u32, u32)>) -> bool {
    score.is_none_or(|(uses, wins)| *uses < 4 || wins * 4 >= *uses)
}

/// A rewritten skill starts with a clean record.
pub fn reset(cfg: &Config, name: &str) -> Result<()> {
    let mut data = scores(cfg);
    if data.remove(name).is_some() {
        write_scores(cfg, &data)?;
    }
    Ok(())
}

pub fn distill_prompt(existing: &str, work: &str) -> String {
    format!(
        "Turn the work below into a reusable skill so the next similar request is faster and avoids the same \
         dead ends. Reply with only a SKILL.md: YAML frontmatter with `name` (kebab-case) and a one-line \
         `description` saying when to use it, then the steps, the pitfalls hit, and how to check the result. \
         Be specific to what actually worked. {existing}\n\nWORK:\n{work}"
    )
}
