//! Checkpoints: before the first file change of a turn, snapshot the working folder into a
//! shadow git repository under ~/.friday, so `/undo` can put it back. The project's own git
//! history is never touched.

use anyhow::{bail, Context, Result};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::process::Command;

const EXCLUDE: &str = "target/\nnode_modules/\n.venv/\nvenv/\n__pycache__/\n.git/\ndist/\nbuild/\n*.log\n.DS_Store\n";

fn store(home: &Path, work: &Path) -> PathBuf {
    let digest: String = Sha256::digest(work.to_string_lossy().as_bytes()).iter().take(8).map(|b| format!("{b:02x}")).collect();
    home.join("checkpoints").join(digest)
}

fn git(dir: &Path, work: &Path, args: &[&str]) -> Result<String> {
    let out = Command::new("git")
        .arg("--git-dir").arg(dir).arg("--work-tree").arg(work)
        .args(["-c", "user.name=friday", "-c", "user.email=friday@localhost", "-c", "core.autocrlf=false"])
        .args(args)
        .output()
        .context("git is needed for checkpoints")?;
    if !out.status.success() {
        bail!("git {}: {}", args.join(" "), String::from_utf8_lossy(&out.stderr).trim());
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Whether a folder is worth snapshotting: never a whole home directory or the filesystem root.
fn sensible(work: &Path) -> bool {
    let home = std::env::home_dir().unwrap_or_default();
    work.parent().is_some() && work != home
}

/// Snapshot `work` and remember it as an undo point. Returns the commit, or None when skipped.
pub fn snapshot(home: &Path, work: &Path, label: &str) -> Result<Option<String>> {
    if !sensible(work) {
        return Ok(None);
    }
    let dir = store(home, work);
    if !dir.exists() {
        std::fs::create_dir_all(&dir)?;
        git(&dir, work, &["init", "-q"])?;
        std::fs::create_dir_all(dir.join("info"))?;
        std::fs::write(dir.join("info/exclude"), EXCLUDE)?;
    }
    git(&dir, work, &["add", "-A", "."])?;
    git(&dir, work, &["commit", "-q", "--allow-empty", "--no-verify", "-m", label])?;
    let commit = git(&dir, work, &["rev-parse", "HEAD"])?;
    let mut stack = std::fs::read_to_string(dir.join("friday-undo")).unwrap_or_default();
    stack.push_str(&commit);
    stack.push('\n');
    std::fs::write(dir.join("friday-undo"), stack)?;
    Ok(Some(commit))
}

/// Put `work` back as it was at the last checkpoint: changed and deleted files restored, files
/// created since removed. Returns a one-line summary.
pub fn undo(home: &Path, work: &Path) -> Result<String> {
    let dir = store(home, work);
    let stack = std::fs::read_to_string(dir.join("friday-undo")).unwrap_or_default();
    let mut points: Vec<&str> = stack.lines().filter(|l| !l.is_empty()).collect();
    let Some(target) = points.pop() else { bail!("nothing to undo in {}", work.display()) };
    // Record the present first, so the undo can itself be inspected or reversed by hand.
    git(&dir, work, &["add", "-A", "."])?;
    git(&dir, work, &["commit", "-q", "--allow-empty", "--no-verify", "-m", "before undo"])?;
    let created = git(&dir, work, &["diff", "--name-only", "--diff-filter=A", target, "HEAD"])?;
    let changed = git(&dir, work, &["diff", "--name-only", "--diff-filter=MDT", target, "HEAD"])?;
    for file in created.lines().filter(|l| !l.is_empty()) {
        let _ = std::fs::remove_file(work.join(file));
    }
    git(&dir, work, &["checkout", target, "--", "."])?;
    std::fs::write(dir.join("friday-undo"), points.iter().map(|p| format!("{p}\n")).collect::<String>())?;
    let count = |s: &str| s.lines().filter(|l| !l.is_empty()).count();
    Ok(format!("restored {} file(s), removed {} new file(s)", count(&changed), count(&created)))
}
