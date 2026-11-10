//! Config: defaults < ~/.friday/config.toml. Secrets stay in environment variables.

use anyhow::{Context, Result};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::PathBuf;

pub const TIERS: [&str; 3] = ["fast", "main", "deep"];

#[derive(Deserialize, Clone, Debug, Default, PartialEq)]
pub struct ModelSpec {
    pub model: String,
    #[serde(default)]
    pub base_url: String,
    /// Name of the environment variable holding the key (never the key itself).
    #[serde(default)]
    pub api_key_env: String,
}

#[derive(Deserialize, Clone, Debug)]
#[serde(default)]
pub struct S1Config {
    pub jev_url: String,
    pub jev_model: String,
    /// Read from TYPESAFE_API_KEY at load; not a config-file field.
    #[serde(skip)]
    pub jev_key: Option<String>,
    /// Below this, a Jev decision falls back to the safe default.
    pub confidence: f64,
    /// Below this, a Needle tool call is handed to System 2 instead of being run.
    pub needle_confidence: f64,
    /// A fine-tuned Needle archive (see `friday train`); empty uses the stock model.
    pub needle_weights: String,
}

impl Default for S1Config {
    fn default() -> Self {
        Self {
            jev_url: "https://api.typesafe.ai/v1/systemone".into(),
            jev_model: "jev-latest".into(),
            jev_key: None,
            confidence: 0.6,
            needle_confidence: 0.5,
            needle_weights: String::new(),
        }
    }
}

#[derive(Deserialize, Clone, Debug)]
#[serde(default)]
pub struct ContextConfig {
    pub window: u64,
    pub compact_at: f64,
    pub keep_recent: usize,
}

impl Default for ContextConfig {
    fn default() -> Self {
        Self { window: 128_000, compact_at: 0.7, keep_recent: 8 }
    }
}

/// A proactive trigger: a timer, or (with `cmd`) a command whose output changed.
#[derive(Deserialize, Clone, Debug)]
pub struct Watch {
    pub name: String,
    #[serde(default = "five_minutes")]
    pub every: u64,
    #[serde(default)]
    pub cmd: String,
    pub prompt: String,
}

fn five_minutes() -> u64 {
    300
}

#[derive(Deserialize, Clone, Debug, Default)]
#[serde(default)]
pub struct Config {
    pub user: String,
    /// tier -> model. Only "main" is required; fast and deep fall back to it.
    pub models: BTreeMap<String, ModelSpec>,
    pub s1: S1Config,
    pub context: ContextConfig,
    pub watch: Vec<Watch>,
    /// MCP servers to connect at startup.
    pub mcp: Vec<crate::mcp::McpConfig>,
    /// Where Friday keeps everything: memory, skills, mail, training data.
    #[serde(skip)]
    pub home: PathBuf,
    /// Skill folders, lowest precedence first.
    #[serde(skip)]
    pub skill_dirs: Vec<PathBuf>,
}

impl Config {
    /// The model for a tier, falling back to main, then to whatever is configured.
    pub fn spec(&self, tier: &str) -> Result<&ModelSpec> {
        self.models
            .get(tier)
            .or_else(|| self.models.get("main"))
            .or_else(|| self.models.values().next())
            .context("No model configured. Run `friday init`.")
    }

    /// Tiers to try from `start` upward, skipping ones that resolve to an already-listed model.
    pub fn ladder(&self, start: &str) -> Result<Vec<&'static str>> {
        let from = TIERS.iter().position(|t| *t == start).unwrap_or(1);
        let mut out: Vec<&'static str> = Vec::new();
        for tier in &TIERS[from..] {
            let spec = self.spec(tier)?;
            if !out.iter().any(|t| self.spec(t).ok() == Some(spec)) {
                out.push(tier);
            }
        }
        Ok(out)
    }
}

pub fn home() -> PathBuf {
    std::env::var_os("FRIDAY_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::home_dir().unwrap_or_default().join(".friday"))
}

fn trusted_list(home: &std::path::Path) -> Vec<String> {
    std::fs::read_to_string(home.join("trusted")).unwrap_or_default().lines().map(String::from).collect()
}

pub fn is_trusted(home: &std::path::Path, dir: &std::path::Path) -> bool {
    trusted_list(home).iter().any(|t| std::path::Path::new(t) == dir)
}

pub fn trust(home: &std::path::Path, dir: &std::path::Path) -> Result<()> {
    std::fs::create_dir_all(home)?;
    let mut list = trusted_list(home);
    list.push(dir.display().to_string());
    std::fs::write(home.join("trusted"), list.join("\n") + "\n")?;
    Ok(())
}

pub fn load() -> Result<Config> {
    let home = home();
    let path = home.join("config.toml");
    let mut cfg: Config = match std::fs::read_to_string(&path) {
        Ok(text) => toml::from_str(&text).with_context(|| format!("in {}", path.display()))?,
        Err(_) => Config::default(),
    };
    if cfg.user.is_empty() {
        cfg.user = std::env::var("USER").unwrap_or_else(|_| "user".into());
    }
    cfg.s1.jev_key = std::env::var("TYPESAFE_API_KEY").ok().filter(|k| !k.is_empty());
    // Existing Claude Code and Hermes skills work as they are; Friday's own take precedence.
    let user_home = std::env::home_dir().unwrap_or_default();
    cfg.skill_dirs = vec![
        user_home.join(".claude/skills"),
        user_home.join(".hermes/skills"),
        home.join("skills"),
    ];
    // A cloned repo's skills go into the system prompt, so they load only once the folder is trusted.
    let project = std::env::current_dir().unwrap_or_default();
    if is_trusted(&home, &project) {
        cfg.skill_dirs.push(project.join(".friday/skills"));
    }
    cfg.home = home;
    Ok(cfg)
}
