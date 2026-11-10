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
    /// Follow this saved skill for the task (repeatable)
    #[arg(long = "skill", global = true)]
    skills: Vec<String>,
    /// Pick up the last conversation where it stopped
    #[arg(long = "continue", short = 'c', global = true)]
    resume: bool,
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
    /// List skills and their record, or `skills add <git-url|path>` to install a skill pack
    Skills {
        #[command(subcommand)]
        action: Option<SkillsCmd>,
    },
    /// Show the Needle training data Friday has collected
    Train,
    /// Use an image (PNG or JPEG) as the logo on the opening screen; `--reset` for the coin
    Logo {
        image: Option<std::path::PathBuf>,
        #[arg(long)]
        reset: bool,
    },
    /// Put files back as they were before Friday's last change in this folder
    Undo,
    /// Message another agent
    Send { to: String, text: String },
}

#[derive(Subcommand)]
enum SkillsCmd {
    /// Install a skill pack from a git repository or a local folder
    Add { source: String },
}

fn open(cfg: config::Config, ui: Arc<dyn Ui>, interactive: bool, yolo: bool) -> Result<Arc<Shared>> {
    let s1 = Arc::new(S1::new(&cfg));
    // The embedding model is shared by every Friday home on this machine, so it is fetched once.
    let cache = std::env::var_os("XDG_CACHE_HOME").map(Into::into).unwrap_or_else(|| std::env::home_dir().unwrap_or_default().join(".cache")).join("friday");
    let memory = memory::Memory::open(&cfg, s1.clone(), memory::local_embedder(&cache))?;
    memory.warm_up();
    let wake = memory.wake_up();
    let bus = Bus::open(&cfg.home)?;
    let mcp = friday::mcp::start_all(&cfg.mcp, &|line| ui.line(&line));
    Ok(Arc::new(Shared { cfg, s1, memory, bus, ui, interactive, yolo: AtomicBool::new(yolo), wake, mcp }))
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    // First Ctrl-C stops the running turn; a second one, before it has stopped, quits.
    let _ = ctrlc::set_handler(|| {
        if friday::interrupted() {
            std::process::exit(130);
        }
        friday::set_interrupted(true);
    });
    let home = config::home();
    match &cli.cmd {
        Some(Cmd::Init) => return ui::init(&home),
        Some(Cmd::Logo { image, reset }) => {
            let path = home.join("logo.txt");
            if *reset {
                let _ = std::fs::remove_file(&path);
                return Ok(println!("  back to the coin"));
            }
            let Some(image) = image else { bail!("give an image: friday logo path/to/logo.png") };
            let art = friday::coin::image_to_art(image, friday::coin::ROWS as u32)?;
            std::fs::create_dir_all(&home)?;
            std::fs::write(&path, art.join("\n"))?;
            friday::coin::reveal(&art, &[], false);
            return Ok(println!("  saved; it opens every session. `friday logo --reset` brings back the coin"));
        }
        Some(Cmd::Undo) => return Ok(println!("  {}", friday::checkpoint::undo(&home, &std::env::current_dir()?)?)),
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
    if let Some(Cmd::Skills { action }) = &cli.cmd {
        return match action {
            None => Ok(ui::show_skills(&cfg)),
            Some(SkillsCmd::Add { source }) => {
                let (name, count) = friday::skills::add(&cfg, source)?;
                Ok(println!("  added {name}: {count} skill{}", if count == 1 { "" } else { "s" }))
            }
        };
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
        // From a terminal a person is there to approve; from a pipe or a script nobody is.
        let interactive = std::io::stdin().is_terminal();
        let mut agent = Agent::new(open(cfg, Arc::new(ui::Plain), interactive, cli.yolo)?, &cli.name);
        agent.force_tier = cli.tier;
        agent.force_skills = cli.skills;
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
            let here = std::env::current_dir().unwrap_or_default();
            let shipped = friday::skills::load(&config::Config { skill_dirs: vec![here.join(".friday/skills")], ..Default::default() }).len();
            if shipped > 0 && !config::is_trusted(&cfg.home, &here) && ui::confirm(&format!("This folder ships {shipped} skill(s) in .friday/skills. Let Friday follow them?")) {
                config::trust(&cfg.home, &here)?;
                cfg = config::load()?;
            }
            let screen = Arc::new(ui::Screen::default());
            let mut agent = Agent::new(open(cfg, screen.clone(), true, cli.yolo)?, &cli.name);
            agent.force_tier = cli.tier;
            agent.force_skills = cli.skills;
            if cli.resume {
                agent.messages = ui::load_session(&agent);
            }
            ui::chat_loop(&mut agent, &screen, !cli.no_anim)?;
            agent.shared.memory.flush();
            Ok(())
        }
    }
}
