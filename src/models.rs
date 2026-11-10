//! System 2: slow, generative models. Any OpenAI-compatible endpoint, always streamed.

use crate::config::Config;
use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader};

#[derive(Debug, Default, Clone)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub arguments: String,
}

#[derive(Debug, Default)]
pub struct Reply {
    pub content: String,
    pub tool_calls: Vec<ToolCall>,
    pub prompt_tokens: u64,
}

/// One completion on `tier`. `on_delta` receives the reply text as it arrives.
pub fn chat(cfg: &Config, tier: &str, messages: &[Value], tools: &[Value], mut on_delta: Option<&mut dyn FnMut(&str)>) -> Result<Reply> {
    let spec = cfg.spec(tier)?;
    let base = if spec.base_url.is_empty() { "https://api.openai.com/v1" } else { spec.base_url.trim_end_matches('/') };
    let mut body = json!({
        "model": spec.model,
        "messages": messages,
        "stream": true,
        "stream_options": {"include_usage": true},
    });
    if !tools.is_empty() {
        body["tools"] = json!(tools);
    }
    let url = format!("{base}/chat/completions");
    let mut request = crate::http(&url).post(&url);
    let key_env = if spec.api_key_env.is_empty() { "OPENAI_API_KEY" } else { &spec.api_key_env };
    if let Ok(key) = std::env::var(key_env) {
        request = request.set("Authorization", &format!("Bearer {key}"));
    }
    let response = match request.send_json(&body) {
        Ok(r) => r,
        Err(ureq::Error::Status(code, r)) => {
            let detail: String = r.into_string().unwrap_or_default().chars().take(300).collect();
            bail!("{} answered {code}: {detail}", spec.model)
        }
        Err(e) => return Err(e).with_context(|| format!("reaching {base}")),
    };

    let mut reply = Reply::default();
    for line in BufReader::new(response.into_reader()).lines() {
        let line = line?;
        let Some(data) = line.strip_prefix("data:").map(str::trim) else { continue };
        if data == "[DONE]" {
            break;
        }
        let Ok(chunk) = serde_json::from_str::<Value>(data) else { continue };
        if crate::interrupted() {
            bail!("interrupted");
        }
        // Some providers (OpenRouter among them) report failures inside a 200 stream.
        if let Some(error) = chunk.get("error") {
            bail!("{} failed mid-reply: {}", spec.model, error["message"].as_str().unwrap_or(&error.to_string()));
        }
        if let Some(tokens) = chunk["usage"]["prompt_tokens"].as_u64() {
            reply.prompt_tokens = tokens;
        }
        let delta = &chunk["choices"][0]["delta"];
        if let Some(text) = delta["content"].as_str().filter(|t| !t.is_empty()) {
            reply.content.push_str(text);
            if let Some(f) = on_delta.as_mut() {
                f(text);
            }
        }
        // Tool calls arrive in fragments, addressed by index.
        for part in delta["tool_calls"].as_array().into_iter().flatten() {
            // Without an index (some servers omit it), a fragment with an id starts a new call.
            let index = match part["index"].as_u64() {
                Some(i) => i as usize,
                None if part["id"].is_string() || reply.tool_calls.is_empty() => reply.tool_calls.len(),
                None => reply.tool_calls.len() - 1,
            };
            if reply.tool_calls.len() <= index {
                reply.tool_calls.resize(index + 1, ToolCall::default());
            }
            let call = &mut reply.tool_calls[index];
            if let Some(id) = part["id"].as_str() {
                call.id = id.into();
            }
            if let Some(name) = part["function"]["name"].as_str() {
                call.name.push_str(name);
            }
            if let Some(arguments) = part["function"]["arguments"].as_str() {
                call.arguments.push_str(arguments);
            }
        }
    }
    for (i, call) in reply.tool_calls.iter_mut().enumerate() {
        if call.id.is_empty() {
            call.id = format!("call_{i}"); // a tool result must name the call it answers
        }
    }
    if reply.prompt_tokens == 0 {
        // No usage reported: estimate (about 4 characters a token) so compaction still happens.
        reply.prompt_tokens = (json!(messages).to_string().len() / 4) as u64;
    }
    Ok(reply)
}

/// A one-shot prompt with no tools.
pub fn ask(cfg: &Config, tier: &str, prompt: &str) -> Result<String> {
    Ok(chat(cfg, tier, &[json!({"role": "user", "content": prompt})], &[], None)?.content)
}
