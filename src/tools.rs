//! Tools. Groups let System 1 leave whole capabilities out of the context when a request
//! doesn't need them.

use crate::agent::Agent;
use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::process::Command;

const MAX_OUTPUT: usize = 8000;
pub const DENIED: &str = "denied: the user did not approve this (unattended runs need --yolo)";

pub struct Tool {
    pub name: &'static str,
    pub group: &'static str,
    pub description: &'static str,
    /// (name, JSON type, required)
    pub params: &'static [(&'static str, &'static str, bool)],
    pub run: fn(&mut Agent, &Value) -> Result<String>,
}

pub fn all() -> Vec<Tool> {
    vec![
        Tool { name: "read_file", group: "core", description: "Read a text file.", params: &[("path", "string", true)], run: read_file },
        Tool { name: "write_file", group: "core", description: "Create or overwrite a file with the given content.", params: &[("path", "string", true), ("content", "string", true)], run: write_file },
        Tool { name: "edit_file", group: "core", description: "Replace the single exact occurrence of `old` in a file with `new`.", params: &[("path", "string", true), ("old", "string", true), ("new", "string", true)], run: edit_file },
        Tool { name: "shell", group: "core", description: "Run a shell command in the working directory and return its output.", params: &[("command", "string", true)], run: shell },
        Tool { name: "recall", group: "core", description: "Search long-term memory for what was said or done before. Returns the exact words.", params: &[("query", "string", true)], run: recall },
        Tool { name: "ask_about_user", group: "core", description: "Ask what is known about the user: preferences, habits, goals, how they like things done. Use it before guessing what they would want.", params: &[("question", "string", true)], run: ask_about_user },
        Tool { name: "delegate", group: "agents", description: "Run independent tasks in parallel, each in a fresh sub-agent, and return their reports. Give each task everything it needs: sub-agents do not see this conversation.", params: &[("tasks", "array", true), ("role", "string", false)], run: delegate },
        Tool { name: "send_message", group: "agents", description: "Send a message to another agent by name. They read it on their next inbox check.", params: &[("to", "string", true), ("text", "string", true)], run: send_message },
        Tool { name: "read_inbox", group: "agents", description: "Read unread messages other agents sent to you.", params: &[], run: read_inbox },
        Tool { name: "browse", group: "web", description: "Do something in a web browser: open a URL, click, type, read. Describe the goal in plain words; returns the final page's text.", params: &[("goal", "string", true), ("url", "string", false)], run: browse },
        Tool { name: "use_computer", group: "desktop", description: "Operate this computer's screen with mouse and keyboard to reach a goal.", params: &[("goal", "string", true)], run: use_computer },
    ]
}

/// {name, description, parameters} for every tool in `groups`: the shape Needle takes directly
/// and OpenAI-style APIs take wrapped in {"type": "function"}.
pub fn schemas(groups: &[&str]) -> Vec<Value> {
    all()
        .into_iter()
        .filter(|t| groups.contains(&t.group))
        .map(|t| {
            let properties: serde_json::Map<String, Value> = t
                .params
                .iter()
                .map(|(name, kind, _)| ((*name).into(), if *kind == "array" { json!({"type": "array", "items": {"type": "string"}}) } else { json!({"type": kind}) }))
                .collect();
            let required: Vec<&str> = t.params.iter().filter(|p| p.2).map(|p| p.0).collect();
            json!({"name": t.name, "description": t.description, "parameters": {"type": "object", "properties": properties, "required": required}})
        })
        .collect()
}

/// Keep the head and tail of long output: errors usually sit at one end.
pub fn clip(text: &str) -> String {
    let count = text.chars().count();
    if count <= MAX_OUTPUT {
        return text.to_string();
    }
    let half = MAX_OUTPUT / 2;
    let head: String = text.chars().take(half).collect();
    let tail: String = text.chars().skip(count - half).collect();
    format!("{head}\n... [{} chars omitted] ...\n{tail}", count - MAX_OUTPUT)
}

fn text<'a>(args: &'a Value, name: &str) -> Result<&'a str> {
    args[name].as_str().with_context(|| format!("missing argument `{name}`"))
}

fn path(args: &Value) -> Result<PathBuf> {
    let raw = text(args, "path")?;
    Ok(match raw.strip_prefix("~/") {
        Some(rest) => std::env::home_dir().unwrap_or_default().join(rest),
        None => raw.into(),
    })
}

const READ_ONLY: [&str; 12] = ["ls", "cat", "head", "tail", "wc", "pwd", "rg", "grep", "find", "which", "date", "tree"];

/// Commands that only look. Anything chained, redirected or substituted is not read-only.
pub fn is_read_only(command: &str) -> bool {
    let first = command.split_whitespace().next().unwrap_or("");
    READ_ONLY.contains(&first) && !command.contains([';', '|', '&', '<', '>', '`', '$', '\n'])
}

fn read_file(_: &mut Agent, args: &Value) -> Result<String> {
    Ok(String::from_utf8_lossy(&std::fs::read(path(args)?)?).into_owned())
}

fn write_file(agent: &mut Agent, args: &Value) -> Result<String> {
    let (path, content) = (path(args)?, text(args, "content")?);
    if !agent.approve(&format!("write {}", path.display())) {
        return Ok(DENIED.into());
    }
    if let Some(dir) = path.parent().filter(|d| !d.as_os_str().is_empty()) {
        std::fs::create_dir_all(dir)?;
    }
    std::fs::write(&path, content)?;
    Ok(format!("wrote {} chars to {}", content.chars().count(), path.display()))
}

fn edit_file(agent: &mut Agent, args: &Value) -> Result<String> {
    let (path, old, new) = (path(args)?, text(args, "old")?, text(args, "new")?);
    let before = std::fs::read_to_string(&path)?;
    let count = before.matches(old).count();
    if count != 1 {
        bail!("`old` occurs {count} times; it must occur exactly once");
    }
    if !agent.approve(&format!("edit {}", path.display())) {
        return Ok(DENIED.into());
    }
    std::fs::write(&path, before.replacen(old, new, 1))?;
    Ok(format!("edited {}", path.display()))
}

fn shell(agent: &mut Agent, args: &Value) -> Result<String> {
    let command = text(args, "command")?;
    if !is_read_only(command) && !agent.approve(&format!("run `{command}`")) {
        return Ok(DENIED.into());
    }
    // shortcut: no timeout; a hung command is stopped with ctrl-c. Add one if unattended runs need it.
    let out = Command::new("sh").arg("-c").arg(command).output()?;
    Ok(format!("exit {}\n{}{}", out.status.code().unwrap_or(-1), String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr)))
}

fn recall(agent: &mut Agent, args: &Value) -> Result<String> {
    let found = agent.shared.memory.recall(text(args, "query")?, 8);
    Ok(if found.is_empty() { "nothing found".into() } else { found })
}

fn ask_about_user(agent: &mut Agent, args: &Value) -> Result<String> {
    agent.shared.memory.ask(text(args, "question")?)
}

fn delegate(agent: &mut Agent, args: &Value) -> Result<String> {
    if agent.depth >= 2 {
        bail!("sub-agents may not delegate further");
    }
    let tasks: Vec<&str> = args["tasks"].as_array().context("missing argument `tasks`")?.iter().filter_map(Value::as_str).collect();
    let role = args["role"].as_str().unwrap_or("");
    let mut subs: Vec<Agent> = (1..=tasks.len()).map(|i| agent.spawn(&format!("{}.{i}", agent.name), role)).collect();
    let reports: Vec<String> = std::thread::scope(|scope| {
        let running: Vec<_> = subs.iter_mut().zip(&tasks).map(|(sub, task)| scope.spawn(move || sub.turn(task))).collect();
        running.into_iter().map(|h| h.join().unwrap_or_else(|_| "error: the sub-agent crashed".into())).collect()
    });
    Ok(subs.iter().zip(reports).map(|(sub, report)| format!("## {}\n{report}", sub.name)).collect::<Vec<_>>().join("\n\n"))
}

fn send_message(agent: &mut Agent, args: &Value) -> Result<String> {
    let to = text(args, "to")?;
    agent.shared.bus.send(&agent.name, to, text(args, "text")?)?;
    Ok(format!("sent to {to}"))
}

fn read_inbox(agent: &mut Agent, _: &Value) -> Result<String> {
    let mail = agent.shared.bus.inbox(&agent.name)?;
    Ok(if mail.is_empty() { "no new messages".into() } else { mail.iter().map(|(from, body)| format!("from {from}: {body}")).collect::<Vec<_>>().join("\n") })
}

fn browse(agent: &mut Agent, args: &Value) -> Result<String> {
    let (goal, url) = (text(args, "goal")?, args["url"].as_str().unwrap_or(""));
    if !agent.approve(format!("browse: {goal} {url}").trim()) {
        return Ok(DENIED.into());
    }
    let mut env = crate::operate::Browser::open(url)?;
    crate::operate::operate(agent, &mut env, goal)
}

fn use_computer(agent: &mut Agent, args: &Value) -> Result<String> {
    let goal = text(args, "goal")?;
    if !agent.approve(&format!("control the mouse and keyboard to: {goal}")) {
        return Ok(DENIED.into());
    }
    let mut env = crate::operate::Desktop::open(agent.shared.s1.clone())?;
    crate::operate::operate(agent, &mut env, goal)
}
