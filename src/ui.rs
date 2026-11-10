//! The terminal: the opening coin, streamed Markdown, a spinner, approvals, setup and doctor.

use crate::agent::{Agent, Ui};
use crate::config::{Config, TIERS};
use crate::{coin, models, skills};
use anyhow::{bail, Result};
use serde_json::{json, Value};
use std::io::Write;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

const ACCENT: &str = "\x1b[38;2;142;162;255m";
const BOLT: &str = "\x1b[38;2;245;180;84m";
const DIM: &str = "\x1b[38;5;244m";
const BOLD: &str = "\x1b[1m";
const OFF: &str = "\x1b[0m";

// -- the screen -------------------------------------------------------------------------------

#[derive(Default)]
struct State {
    busy: bool,
    spinner: Option<(Arc<AtomicBool>, std::thread::JoinHandle<()>)>,
    partial: String,
    in_code: bool,
    streaming: bool,
    streamed: String,
}

/// Renders one conversation. All output goes through here so the spinner never tears a line.
#[derive(Default)]
pub struct Screen {
    state: Mutex<State>,
}

fn pause(s: &mut State) {
    if let Some((stop, thread)) = s.spinner.take() {
        stop.store(true, Ordering::Relaxed);
        let _ = thread.join();
        print!("\r\x1b[K");
    }
}

fn resume(s: &mut State) {
    if !s.busy || s.spinner.is_some() || !crossterm::tty::IsTty::is_tty(&std::io::stdout()) {
        return;
    }
    let stop = Arc::new(AtomicBool::new(false));
    let flag = stop.clone();
    let thread = std::thread::spawn(move || {
        for frame in ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"].iter().cycle() {
            if flag.load(Ordering::Relaxed) {
                break;
            }
            print!("\r  {BOLT}{frame}{OFF} {DIM}working{OFF}");
            let _ = std::io::stdout().flush();
            std::thread::sleep(std::time::Duration::from_millis(80));
        }
    });
    s.spinner = Some((stop, thread));
}

/// One line of Markdown, styled. Line-at-a-time so it can render while the reply streams.
fn render(line: &str, in_code: &mut bool) -> String {
    let trimmed = line.trim_start();
    if trimmed.starts_with("```") {
        *in_code = !*in_code;
        return format!("  {DIM}{}{OFF}", "─".repeat(40));
    }
    if *in_code {
        return format!("  {BOLT}{line}{OFF}");
    }
    if let Some(title) = trimmed.strip_prefix('#') {
        return format!("  {BOLD}{ACCENT}{}{OFF}", title.trim_start_matches('#').trim());
    }
    let skin = termimad::MadSkin::default();
    let indent = &line[..line.len() - trimmed.len()];
    match trimmed.strip_prefix("- ").or_else(|| trimmed.strip_prefix("* ")) {
        Some(item) => format!("  {indent}{ACCENT}•{OFF} {}", skin.inline(item)),
        None => format!("  {}", skin.inline(line)),
    }
}

/// Colour a status line by what it is: a plan, a tool call, a result, a fast-path action.
fn style(text: &str) -> String {
    let t = text.trim_start();
    let pad = &text[..text.len() - t.len()];
    if let Some(rest) = t.strip_prefix("⚡ ") {
        let (name, arg) = rest.split_once("  ").unwrap_or((rest, ""));
        return format!("{pad}{BOLT}⚡ {BOLD}{name}{OFF}  {DIM}{arg}{OFF}");
    }
    if let Some(rest) = t.strip_prefix("⏺ ") {
        let (name, arg) = rest.split_once("  ").unwrap_or((rest, ""));
        return format!("{pad}{ACCENT}⏺{OFF} {BOLD}{name}{OFF}  {DIM}{arg}{OFF}");
    }
    if t.starts_with("jev →") || t.starts_with("plan →") {
        let lit = t.replacen("needle only · no LLM", &format!("{BOLT}needle only · no LLM{DIM}"), 1);
        let lit = match lit.find("llm: ") {
            Some(i) => {
                let end = lit[i..].find(" ·").map_or(lit.len(), |j| i + j);
                format!("{}{ACCENT}{}{DIM}{}", &lit[..i], &lit[i..end], &lit[end..])
            }
            None => lit,
        };
        return format!("{pad}{DIM}{lit}{OFF}");
    }
    if t.starts_with('✎') {
        return format!("{pad}{BOLT}{t}{OFF}");
    }
    if t.starts_with('↑') || t.starts_with("needle ") {
        return format!("{pad}\x1b[33m{t}{OFF}");
    }
    format!("{DIM}{text}{OFF}")
}

impl Ui for Screen {
    fn line(&self, text: &str) {
        let mut s = self.state.lock().unwrap();
        pause(&mut s);
        println!("{}", style(text));
        resume(&mut s);
    }

    fn token(&self, delta: Option<&str>) {
        let mut s = self.state.lock().unwrap();
        let Some(delta) = delta else {
            if s.streaming {
                let (rest, mut in_code) = (std::mem::take(&mut s.partial), s.in_code);
                if !rest.is_empty() {
                    println!("{}", render(&rest, &mut in_code));
                }
                s.in_code = false;
                s.streaming = false;
            }
            resume(&mut s);
            return;
        };
        if !s.streaming {
            pause(&mut s);
            s.streaming = true;
            s.streamed.clear();
        }
        s.streamed.push_str(delta);
        s.partial.push_str(delta);
        while let Some(end) = s.partial.find('\n') {
            let line: String = s.partial.drain(..=end).collect();
            let mut in_code = s.in_code;
            println!("{}", render(line.trim_end_matches('\n'), &mut in_code));
            s.in_code = in_code;
        }
        let _ = std::io::stdout().flush();
    }

    fn ask(&self, prompt: &str) -> String {
        let mut s = self.state.lock().unwrap();
        pause(&mut s);
        print!("\x1b[33m{prompt}{OFF}");
        let _ = std::io::stdout().flush();
        let mut answer = String::new();
        let _ = std::io::stdin().read_line(&mut answer);
        resume(&mut s);
        answer
    }
}

impl Screen {
    /// Run one turn and draw it: plan, tool calls, the streamed reply, and a one-line footer.
    pub fn turn(&self, agent: &mut Agent, text: &str) {
        let s1 = agent.shared.s1.clone();
        let count = |keys: &[&str]| keys.iter().map(|k| s1.stat(k)).sum::<u64>();
        let (fast_before, slow_before, started) = (count(&["jev", "needle", "mirage"]), count(&["s2"]), Instant::now());
        {
            let mut s = self.state.lock().unwrap();
            s.busy = true;
            s.streamed.clear();
            resume(&mut s);
        }
        crate::set_interrupted(false);
        let reply = agent.turn(text);
        crate::set_interrupted(false);
        if let Some(learner) = agent.learner.take() {
            let _ = learner.join(); // so a "learned skill" line lands before the prompt returns
        }
        let mut s = self.state.lock().unwrap();
        s.busy = false;
        pause(&mut s);
        // A one-line reflex result is already on screen as its tool line's preview.
        let shown = agent.last_tier == "reflex" && reply.trim().lines().count() <= 1;
        if !shown && !reply.trim().is_empty() && reply != s.streamed {
            // Reflex results and errors are not streamed.
            let mut in_code = false;
            for line in reply.lines() {
                println!("{}", if agent.last_tier == "reflex" { format!("  {line}") } else { render(line, &mut in_code) });
            }
        }
        let route = if agent.last_tier == "reflex" {
            format!("{BOLT}⚡ needle{DIM}")
        } else {
            format!("{ACCENT}◆ {}{DIM} {}", agent.last_tier, agent.shared.cfg.spec(&agent.last_tier).map(|s| s.model.as_str()).unwrap_or(""))
        };
        println!(
            "{DIM}  {route} · system 1 ×{} · system 2 ×{} · context {:.0}% · {:.1}s{OFF}\n",
            count(&["jev", "needle", "mirage"]) - fast_before,
            count(&["s2"]) - slow_before,
            agent.context_used * 100.0,
            started.elapsed().as_secs_f64()
        );
    }
}

/// Status lines to stderr, nothing else: for `friday run` and `friday watch`.
pub struct Plain;

impl Ui for Plain {
    fn line(&self, text: &str) {
        eprintln!("{text}");
    }

    fn ask(&self, prompt: &str) -> String {
        eprint!("{prompt}");
        let mut answer = String::new();
        let _ = std::io::stdin().read_line(&mut answer);
        answer
    }
}

// -- the conversation -------------------------------------------------------------------------

const COMMANDS: [(&str, &str); 8] = [
    ("/help", "show this"),
    ("/tier", "pin a model tier: /tier fast|main|deep, or /tier auto to let System 1 choose"),
    ("/skills", "list skills and how they have performed"),
    ("/stats", "how much of this session System 1 handled"),
    ("/yolo", "toggle approval prompts"),
    ("/clear", "start a fresh conversation (memory is kept)"),
    ("/undo", "put files back as they were before Friday's last change"),
    ("/exit", "leave"),
];

#[derive(rustyline::Helper, rustyline::Hinter, rustyline::Validator)]
struct Line {
    /// Slash commands, then `/<skill>` for every skill.
    words: Vec<String>,
}

impl rustyline::completion::Completer for Line {
    type Candidate = String;
    fn complete(&self, line: &str, pos: usize, _: &rustyline::Context<'_>) -> rustyline::Result<(usize, Vec<String>)> {
        let typed = &line[..pos];
        let matches = if typed.starts_with('/') && !typed.contains(' ') { self.words.iter().filter(|w| w.starts_with(typed)).cloned().collect() } else { Vec::new() };
        Ok((0, matches))
    }
}

impl rustyline::highlight::Highlighter for Line {
    fn highlight_prompt<'b, 's: 'b, 'p: 'b>(&'s self, prompt: &'p str, _: bool) -> std::borrow::Cow<'b, str> {
        format!("{BOLD}{ACCENT}{prompt}{OFF}").into()
    }
}

fn mark(on: bool) -> String {
    if on { "\x1b[32m●\x1b[0m".into() } else { format!("{DIM}○{OFF}") }
}

fn banner(agent: &Agent) -> Vec<String> {
    let cfg = &agent.shared.cfg;
    let mut models: Vec<&str> = Vec::new();
    for tier in TIERS {
        if let Ok(spec) = cfg.spec(tier) {
            if !models.contains(&spec.model.as_str()) {
                models.push(&spec.model);
            }
        }
    }
    let (drawers, conclusions) = agent.shared.memory.counts();
    vec![
        String::new(),
        format!("{BOLD}{ACCENT}F R I D A Y{OFF}"),
        format!("{DIM}fast decisions first, deep thought when it counts{OFF}"),
        String::new(),
        format!("{BOLT}system 1{OFF}  {} jev   {} needle   {} mirage", mark(agent.shared.s1.has_jev()), mark(true), mark(true)),
        format!("{ACCENT}system 2{OFF}  {}", models.join(" · ")),
        format!("{DIM}memory{OFF}    {drawers} drawers · {conclusions} things known about {}", cfg.user),
        format!("{DIM}skills{OFF}    {}", skills::load(cfg).len()),
        String::new(),
        format!("{DIM}/help for commands · ctrl-d to leave{OFF}"),
    ]
}

/// A yes/no question; anything but y is no.
pub fn confirm(question: &str) -> bool {
    prompt(&format!("  \x1b[33m{question} [y/N]\x1b[0m ")).eq_ignore_ascii_case("y")
}

pub fn show_skills(cfg: &Config) {
    let (all, marks) = (skills::load(cfg), skills::scores(cfg));
    if all.is_empty() {
        return println!("{DIM}  no skills yet; Friday writes them as it works{OFF}");
    }
    println!("{DIM}  {:28} {:8} {:>9}  when{OFF}", "skill", "from", "record");
    for (name, skill) in all {
        let (uses, wins) = marks.get(&name).copied().unwrap_or_default();
        let record = if uses == 0 { "—".to_string() } else { format!("{wins}/{uses}") };
        let state = if skills::healthy(marks.get(&name)) { "" } else { "\x1b[31mretired\x1b[0m " };
        let when: String = skill.description.chars().take(60).collect();
        println!("  {ACCENT}{name:28}{OFF} {DIM}{:8}{OFF} {record:>9}  {state}{DIM}{when}{OFF}", skill.origin());
    }
    println!("{DIM}  /<skill> runs a turn with that skill · friday skills add <git-url> installs a pack{OFF}");
}

fn show_stats(agent: &Agent) {
    let s = |k| agent.shared.s1.stat(k);
    println!(
        "  system 1: {} calls (jev {}, needle {}, mirage {})   system 2: {} calls   screen steps: {} fast / {} slow",
        s("jev") + s("needle") + s("mirage"), s("jev"), s("needle"), s("mirage"), s("s2"), s("operate_s1"), s("operate_s2")
    );
    for (part, error) in agent.shared.memory.errors.lock().unwrap().iter() {
        println!("  \x1b[33m{part}\x1b[0m: {error}");
    }
    let error = agent.shared.s1.last_error.lock().unwrap();
    if !error.is_empty() {
        println!("  \x1b[33msystem 1\x1b[0m: {error}");
    }
}

fn session_path(agent: &Agent) -> std::path::PathBuf {
    agent.shared.cfg.home.join("sessions").join(format!("{}.json", agent.name))
}

/// The conversation so far, so `friday --continue` can pick it up.
fn save_session(agent: &Agent) {
    let path = session_path(agent);
    let _ = std::fs::create_dir_all(path.parent().unwrap());
    let _ = std::fs::write(path.with_extension("tmp"), serde_json::to_string(&agent.messages).unwrap_or_default());
    let _ = std::fs::rename(path.with_extension("tmp"), path);
}

pub fn load_session(agent: &Agent) -> Vec<Value> {
    let messages: Vec<Value> = std::fs::read_to_string(session_path(agent)).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
    println!("{DIM}  continuing: {} earlier messages{OFF}", messages.len());
    messages
}

pub fn chat_loop(agent: &mut Agent, screen: &Screen, animate: bool) -> Result<()> {
    match std::fs::read_to_string(agent.shared.cfg.home.join("logo.txt")) {
        Ok(logo) => coin::reveal(&logo.lines().map(String::from).collect::<Vec<_>>(), &banner(agent), animate),
        Err(_) => coin::spin(&banner(agent), if animate { 1.7 } else { 0.0 }),
    }
    println!();
    let mut editor = rustyline::Editor::<Line, rustyline::history::DefaultHistory>::new()?;
    let mut words: Vec<String> = COMMANDS.iter().map(|c| c.0.to_string()).collect();
    words.extend(skills::load(&agent.shared.cfg).into_keys().map(|n| format!("/{n}")));
    editor.set_helper(Some(Line { words }));
    let history = agent.shared.cfg.home.join("history");
    let _ = editor.load_history(&history);
    loop {
        let text = match editor.readline("› ") {
            Ok(text) => text.trim().to_string(),
            Err(rustyline::error::ReadlineError::Interrupted) => continue,
            Err(_) => break,
        };
        if text.is_empty() {
            continue;
        }
        let _ = editor.add_history_entry(&text);
        let (command, arg) = text.split_once(' ').unwrap_or((&text, ""));
        match command {
            "/exit" => break,
            "/help" => COMMANDS.iter().for_each(|(name, what)| println!("  {ACCENT}{name:8}{OFF} {DIM}{what}{OFF}")),
            "/tier" => {
                agent.force_tier = TIERS.contains(&arg).then(|| arg.to_string());
                println!("{DIM}  tier: {}{OFF}", agent.force_tier.as_deref().unwrap_or("auto"));
            }
            "/skills" => show_skills(&agent.shared.cfg),
            "/stats" => show_stats(agent),
            "/yolo" => {
                let on = !agent.shared.yolo.load(Ordering::Relaxed);
                agent.shared.yolo.store(on, Ordering::Relaxed);
                println!("{DIM}  approvals {}{OFF}", if on { "off" } else { "on" });
            }
            "/clear" => agent.messages.clear(),
            "/undo" => match crate::checkpoint::undo(&agent.shared.cfg.home, &std::env::current_dir().unwrap_or_default()) {
                Ok(summary) => println!("{DIM}  {summary}{OFF}"),
                Err(e) => println!("{DIM}  {e:#}{OFF}"),
            },
            _ if command.starts_with('/') && skills::load(&agent.shared.cfg).contains_key(&command[1..]) => {
                agent.force_skills = vec![command[1..].to_string()];
                let task = if arg.is_empty() { format!("Use the `{}` skill.", &command[1..]) } else { arg.to_string() };
                screen.turn(agent, &task);
            }
            _ if command.starts_with('/') => println!("{DIM}  unknown command; /help lists them, /skills lists skills{OFF}"),
            _ => screen.turn(agent, &text),
        }
        save_session(agent);
    }
    let _ = editor.save_history(&history);
    Ok(())
}

// -- first run --------------------------------------------------------------------------------

/// (name, base URL, key env var, suggested fast/main/deep models)
const PROVIDERS: [(&str, &str, &str, [&str; 3]); 6] = [
    ("Ollama (local)", "http://localhost:11434/v1", "", ["", "", ""]),
    ("OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", ["", "", ""]),
    ("OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY", ["", "", ""]),
    ("Anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY", ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]),
    ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", ["", "", ""]),
    ("Another OpenAI-compatible endpoint", "", "", ["", "", ""]),
];

fn prompt(label: &str) -> String {
    print!("{label}");
    let _ = std::io::stdout().flush();
    let mut answer = String::new();
    let _ = std::io::stdin().read_line(&mut answer);
    answer.trim().to_string()
}

fn ollama_models() -> Vec<String> {
    let agent = ureq::AgentBuilder::new().timeout(std::time::Duration::from_millis(500)).build();
    let tags: Value = agent.get("http://localhost:11434/api/tags").call().ok().and_then(|r| r.into_json().ok()).unwrap_or(json!({}));
    tags["models"].as_array().into_iter().flatten().filter_map(|m| m["name"].as_str().map(String::from)).collect()
}

/// Ask the few things Friday cannot guess and write the config file.
pub fn init(home: &std::path::Path) -> Result<()> {
    let local = ollama_models();
    println!("\n{BOLD}Where should Friday think?{OFF}  {DIM}(System 1 runs on this machine regardless){OFF}\n");
    for (i, (name, _, env, _)) in PROVIDERS.iter().enumerate() {
        let found = (i == 0 && !local.is_empty()) || (!env.is_empty() && std::env::var_os(env).is_some());
        println!("  {ACCENT}{}{OFF}  {name}{}", i + 1, if found { "  \x1b[32mdetected\x1b[0m" } else { "" });
    }
    let pick: usize = prompt(&format!("\n{ACCENT}›{OFF} number: ")).parse().ok().filter(|n| (1..=PROVIDERS.len()).contains(n)).unwrap_or(1);
    let (name, base, env, hint) = PROVIDERS[pick - 1];
    let (mut base, mut env) = (base.to_string(), env.to_string());
    if base.is_empty() {
        base = prompt("  base URL: ");
        env = prompt("  env var holding the API key (blank if none): ");
    }
    if name.starts_with("Ollama") && !local.is_empty() {
        println!("{DIM}  installed: {}{OFF}", local.join(", "));
    }
    let model = |label: &str, default: &str| {
        let typed = prompt(&format!("  {label} model{}: ", if default.is_empty() { String::new() } else { format!(" [{default}]") }));
        if typed.is_empty() { default.to_string() } else { typed }
    };
    let main = model("main", hint[1]);
    if main.is_empty() {
        bail!("a main model is required");
    }
    let fast = Some(model("fast (small and cheap; blank = same as main)", hint[0])).filter(|m| !m.is_empty()).unwrap_or_else(|| main.clone());
    let deep = Some(model("deep (strongest; blank = same as main)", hint[2])).filter(|m| !m.is_empty()).unwrap_or_else(|| main.clone());
    let mut text = format!("user = {}\n", json!(std::env::var("USER").unwrap_or_else(|_| "user".into())));
    for (tier, model) in TIERS.iter().zip([&fast, &main, &deep]) {
        text.push_str(&format!("\n[models.{tier}]\nmodel = {}\nbase_url = {}\n", json!(model), json!(base)));
        if !env.is_empty() {
            text.push_str(&format!("api_key_env = {}\n", json!(env)));
        }
    }
    std::fs::create_dir_all(home)?;
    std::fs::write(home.join("config.toml"), text)?;
    println!("\n  wrote {BOLD}{}{OFF}", home.join("config.toml").display());
    if !env.is_empty() && std::env::var_os(&env).is_none() {
        println!("  \x1b[33mset {env} in your shell before chatting\x1b[0m");
    }
    if std::env::var_os("TYPESAFE_API_KEY").is_none() {
        println!("{DIM}  set TYPESAFE_API_KEY so Jev does the routing, skill choice and verification{OFF}");
    }
    println!("{DIM}  `friday doctor` checks every part{OFF}\n");
    Ok(())
}

// -- doctor -----------------------------------------------------------------------------------

/// Exercise every part for real and say what is wrong in plain words.
pub fn doctor(agent: &mut Agent) {
    let shared = agent.shared.clone();
    let cfg = &shared.cfg;
    let check = |label: &str, run: &mut dyn FnMut() -> Result<String>| {
        let started = Instant::now();
        let (ok, detail) = match run() {
            Ok(detail) => ("\x1b[32mok  \x1b[0m", detail),
            Err(e) => ("\x1b[31mfail\x1b[0m", format!("{e:#}")),
        };
        let detail: String = detail.chars().take(150).collect();
        println!("  {ok} {label:20} {DIM}{detail}  ({} ms){OFF}", started.elapsed().as_millis());
    };
    for tier in TIERS {
        check(&format!("system 2 · {tier}"), &mut || Ok(format!("{} said {:?}", cfg.spec(tier)?.model, models::ask(cfg, tier, "Say ok.")?)));
    }
    check("system 1 · jev", &mut || {
        if !shared.s1.has_jev() {
            bail!("TYPESAFE_API_KEY is not set; decisions fall back to the fast model");
        }
        let a = shared.s1.decide("The sky is blue.", &json!({"q": {"type": "noul", "instructions": "The text mentions a colour."}}), None);
        if a["q"].confidence == 0.0 {
            bail!("{}", shared.s1.last_error.lock().unwrap());
        }
        Ok(format!("answered {} at confidence {:.2}", a["q"].value, a["q"].confidence))
    });
    check("system 1 · needle", &mut || {
        let tool = json!({"name": "read_file", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}});
        let (calls, confidence) = shared.s1.act("read the file notes.txt", &[tool], "");
        match calls.first() {
            Some(call) => Ok(format!("{}({}) at confidence {confidence:.2}", call["name"].as_str().unwrap_or(""), call["arguments"])),
            None => bail!("{}", shared.s1.last_error.lock().unwrap()),
        }
    });
    check("system 1 · mirage", &mut || {
        let seen = shared.s1.see(&vec![255u8; 160 * 80 * 3], 160, 80)?;
        Ok(format!("described a test image: {} objects", seen["objects"].as_array().map_or(0, Vec::len)))
    });
    check("memory", &mut || {
        shared.memory.add("doctor", "check", "Friday's doctor filed this line to prove memory works.", "doctor")?;
        let found = shared.memory.search("what did the doctor file to prove memory works?", 1)?;
        if found.is_empty() {
            bail!("a line just filed could not be found again");
        }
        let (drawers, conclusions) = shared.memory.counts();
        Ok(format!("{drawers} drawers, {conclusions} conclusions, search works"))
    });
    check("browser", &mut || crate::operate::Browser::open("").map(|_| "Chrome started".to_string()));
    check("desktop control", &mut || crate::operate::Desktop::open(shared.s1.clone()).map(|_| "ready".to_string()));
}
