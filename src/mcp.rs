//! MCP servers over stdio (JSON-RPC 2.0, one message per line).
//!
//! Each server's tools are a tool group of their own. Jev decides per request whether a server
//! is needed at all, so a dozen connected servers cost no context until one is.

use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{channel, Receiver};
use std::sync::Mutex;
use std::time::Duration;

#[derive(Deserialize, Clone, Debug)]
pub struct McpConfig {
    pub name: String,
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// Run its tools without asking. Off by default: an MCP tool can do anything.
    #[serde(default)]
    pub trust: bool,
}

struct Io {
    child: Child,
    stdin: ChildStdin,
    lines: Receiver<String>,
    next: u64,
}

pub struct Server {
    pub name: String,
    pub trust: bool,
    /// The server's tools, as {name, description, parameters} with names prefixed `mcp__<server>__`.
    pub tools: Vec<Value>,
    io: Mutex<Io>,
}

/// OpenAI-style function names allow letters, digits, `_` and `-`, up to 64 characters.
fn safe(name: &str) -> String {
    name.chars().map(|c| if c.is_ascii_alphanumeric() || c == '-' { c } else { '_' }).collect()
}

impl Server {
    pub fn start(cfg: &McpConfig) -> Result<Server> {
        let mut child = Command::new(&cfg.command)
            .args(&cfg.args)
            .envs(&cfg.env)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .with_context(|| format!("starting MCP server `{}` ({})", cfg.name, cfg.command))?;
        let stdin = child.stdin.take().context("no stdin")?;
        let stdout = child.stdout.take().context("no stdout")?;
        // A reader thread, so a silent server costs a timeout rather than a hang.
        let (send, lines) = channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if send.send(line).is_err() {
                    break;
                }
            }
        });
        let mut server = Server { name: cfg.name.clone(), trust: cfg.trust, tools: Vec::new(), io: Mutex::new(Io { child, stdin, lines, next: 0 }) };
        server.request("initialize", json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "friday", "version": env!("CARGO_PKG_VERSION")}}), Duration::from_secs(20))?;
        server.notify("notifications/initialized")?;
        let listed = server.request("tools/list", json!({}), Duration::from_secs(20))?;
        let prefix = format!("mcp__{}__", safe(&cfg.name));
        server.tools = listed["tools"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|t| {
                let name: String = format!("{prefix}{}", safe(t["name"].as_str().unwrap_or(""))).chars().take(64).collect();
                json!({"name": name, "description": t["description"].as_str().unwrap_or(""), "parameters": if t["inputSchema"].is_object() { t["inputSchema"].clone() } else { json!({"type": "object", "properties": {}}) }, "mcp_name": t["name"]})
            })
            .collect();
        Ok(server)
    }

    fn send(io: &mut Io, message: Value) -> Result<()> {
        writeln!(io.stdin, "{message}")?;
        io.stdin.flush()?;
        Ok(())
    }

    fn notify(&self, method: &str) -> Result<()> {
        Self::send(&mut self.io.lock().unwrap(), json!({"jsonrpc": "2.0", "method": method}))
    }

    fn request(&self, method: &str, params: Value, limit: Duration) -> Result<Value> {
        let mut io = self.io.lock().unwrap();
        io.next += 1;
        let id = io.next;
        Self::send(&mut io, json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}))?;
        let deadline = std::time::Instant::now() + limit;
        loop {
            let left = deadline.saturating_duration_since(std::time::Instant::now());
            let line = io.lines.recv_timeout(left).map_err(|_| anyhow::anyhow!("MCP server `{}` did not answer {method} in {}s", self.name, limit.as_secs()))?;
            let Ok(message) = serde_json::from_str::<Value>(&line) else { continue };
            if message["id"] == json!(id) && message.get("method").is_none() {
                if let Some(error) = message.get("error") {
                    bail!("{}: {}", self.name, error["message"].as_str().unwrap_or(&error.to_string()));
                }
                return Ok(message["result"].clone());
            }
            // A request from the server (sampling, roots...): Friday offers none of those.
            if message.get("method").is_some() && message.get("id").is_some() {
                Self::send(&mut io, json!({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "not supported"}}))?;
            }
        }
    }

    /// Call one of this server's tools by its Friday name.
    pub fn call(&self, name: &str, args: &Value) -> Result<String> {
        let tool = self.tools.iter().find(|t| t["name"] == name).context("no such tool")?;
        let result = self.request("tools/call", json!({"name": tool["mcp_name"], "arguments": args}), Duration::from_secs(300))?;
        let text: Vec<String> = result["content"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|c| c["text"].as_str().map(String::from).unwrap_or_else(|| format!("[{} content]", c["type"].as_str().unwrap_or("other"))))
            .collect();
        let text = text.join("\n");
        if result["isError"] == true {
            bail!("{text}");
        }
        Ok(text)
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.io.lock().map(|mut io| io.child.kill());
    }
}

/// Start every configured server; one that fails is reported and skipped.
pub fn start_all(configs: &[McpConfig], report: &dyn Fn(String)) -> Vec<Server> {
    let started: Vec<_> = std::thread::scope(|scope| {
        let running: Vec<_> = configs.iter().map(|c| scope.spawn(move || Server::start(c))).collect();
        running.into_iter().map(|h| h.join().unwrap_or_else(|_| Err(anyhow::anyhow!("crashed")))).collect()
    });
    started
        .into_iter()
        .filter_map(|s| s.map_err(|e| report(format!("  mcp: {e:#}"))).ok())
        .collect()
}
