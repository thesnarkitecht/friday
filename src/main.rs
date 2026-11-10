//! The `friday` command.

use anyhow::{bail, Result};
use clap::{Parser, Subcommand};
use friday::agent::{Agent, Shared, Ui};
use friday::system1::S1;
use friday::{bus::Bus, config, memory, proactive, ui};
use std::io::{IsTerminal, Read};
use std::sync::atomic::AtomicBool;
use std::sync::Arc;

#[derive(Parser)]
#[command(name = "friday", version, about = "A System-1-first agent harness. Run with no arguments to talk.")]
struct Cli {
    /// Never ask before writing or running
    #[arg(long, global = true)]
    yolo: bool,
    /// This agent's name on the bus
    #[arg(long = "as", global = true, default_value = "friday")]
    name: String,
    /// Pin a model tier instead of letting System 1 choose
    #[arg(long, global = true, value_parser = config::TIERS)]
    tier: Option<String>,
    /// Skip the opening animation
    #[arg(long, global = true)]
    no_anim: bool,
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Do one task and exit; only the answer goes to stdout
    Run { task: String },
    /// Stay running and act on events (see [[watch]] in the config)
    Watch,
    /// Choose models
    Init,
    /// Test every part
    Doctor,
    /// List skills and their record
    Skills,
    /// Show the Needle training data Friday has collected
    Train,
    /// Message another agent
    Send { to: String, text: String },
}

fn open(cfg: config::Config, ui: Arc<dyn Ui>, interactive: bool, yolo: bool) -> Result<Arc<Shared>> {
    let s1 = Arc::new(S1::new(&cfg));
    // The embedding model is shared by every Friday home on this machine, so it is fetched once.
    let cache = std::env::var_os("XDG_CACHE_HOME").map(Into::into).unwrap_or_else(|| std::env::home_dir().unwrap_or_default().join(".cache")).join("friday");
    let memory = memory::Memory::open(&cfg, s1.clone(), memory::local_embedder(&cache))?;
    let wake = memory.wake_up();
    let bus = Bus::open(&cfg.home)?;
    Ok(Arc::new(Shared { cfg, s1, memory, bus, ui, interactive, yolo: AtomicBool::new(yolo), wake }))
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let home = config::home();
    match &cli.cmd {
        Some(Cmd::Init) => return ui::init(&home),
        Some(Cmd::Send { to, text }) => {
            Bus::open(&home)?.send(&cli.name, to, text)?;
            return Ok(println!("  sent to {to}"));
        }
        Some(Cmd::Train) => {
            let path = home.join("train/needle.jsonl");
            let count = std::fs::read_to_string(&path).map(|t| t.lines().count()).unwrap_or(0);
            println!("  {count} examples in {}", path.display());
            println!("  cactus finetune {} --out needle-friday.safetensors", path.display());
            println!("  cactus build --lora needle-friday.safetensors --out needle-friday.cact");
            return Ok(println!("  then set needle_weights = \"/path/to/needle-friday.cact\" under [s1]"));
        }
        _ => {}
    }

    let mut cfg = config::load()?;
    if matches!(cli.cmd, Some(Cmd::Skills)) {
        return Ok(ui::show_skills(&cfg));
    }
    let piped = (!std::io::stdin().is_terminal()).then(|| {
        let mut text = String::new();
        let _ = std::io::stdin().read_to_string(&mut text);
        text.trim().to_string()
    });
    if cfg.models.is_empty() {
        if cli.cmd.is_some() || piped.is_some() {
            bail!("No model configured. Run `friday init`.");
        }
        friday::coin::spin(&["".into(), "".into(), "".into(), "\x1b[1mF R I D A Y\x1b[0m".into(), "".into(), "Let's pick your models.".into()], if cli.no_anim { 0.0 } else { 1.7 });
        ui::init(&home)?;
        cfg = config::load()?;
    }

    let task = match (&cli.cmd, piped) {
        (Some(Cmd::Run { task }), _) => Some(task.clone()),
        (None, Some(piped)) => Some(piped),
        _ => None,
    };
    if let Some(task) = task {
        let mut agent = Agent::new(open(cfg, Arc::new(ui::Plain), false, cli.yolo)?, &cli.name);
        agent.force_tier = cli.tier;
        println!("{}", agent.turn(&task));
        if let Some(learner) = agent.learner.take() {
            let _ = learner.join();
        }
        agent.shared.memory.flush();
        return Ok(());
    }
    match cli.cmd {
        Some(Cmd::Watch) => {
            let mut agent = Agent::new(open(cfg, Arc::new(ui::Plain), false, cli.yolo)?, &cli.name);
            agent.force_tier = cli.tier;
            eprintln!("  {} is watching {} sources and its inbox. ctrl-c to stop.", agent.name, agent.shared.cfg.watch.len());
            proactive::watch(&mut agent)
        }
        Some(Cmd::Doctor) => {
            let mut agent = Agent::new(open(cfg, Arc::new(ui::Plain), false, false)?, &cli.name);
            ui::doctor(&mut agent);
            agent.shared.memory.flush();
            Ok(())
        }
        _ => {
            let screen = Arc::new(ui::Screen::default());
            let mut agent = Agent::new(open(cfg, screen.clone(), true, cli.yolo)?, &cli.name);
            agent.force_tier = cli.tier;
            ui::chat_loop(&mut agent, &screen, !cli.no_anim)?;
            agent.shared.memory.flush();
            Ok(())
        }
    }
}
