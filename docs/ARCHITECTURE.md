# How FRIDAY works

## The whole agent

```mermaid
flowchart TB
    subgraph IN["Senses (clients)"]
        mic["Pocket Pi mic<br/>speech → text + voice ID<br/>(owner / other)"]
        radar["Room radars<br/>LD2450 → people per room"]
        ptt["Push-to-talk / scripts<br/>/api/ask"]
    end

    subgraph ENGINE["FRIDAY engine (server/friday)"]
        hear["hear()<br/>remember every utterance"]
        who{"Addressed to FRIDAY?<br/>wake word · follow-up window<br/>guest voice → never<br/>company → model breaks tie"}
        presence["Presence + company<br/>sensors count · other voice in last 2 min"]

        subgraph BRAIN["Brain: agent loop"]
            prefix["Cached prefix (same every turn)<br/>persona + skill index + tool list"]
            ctx["Per-turn [context]<br/>time · place · situation<br/>8 facts · 4 memories"]
            llm(["LLM<br/>llama.cpp on the Pi or Blackhole box"])
            tools["Tools<br/>remember · recall · reminders · routines<br/>skill_view · skill_save<br/>situation · cameras · github · calls"]
        end

        mem[("Memory (SQLite + FTS)<br/>utterances · facts · day summaries<br/>reminders · routines")]
        skills[("Skills folder<br/>SKILL.md files")]

        subgraph PRO["Proactive loop (every 2 min)"]
            feeds["Reminders · NWS alerts · quakes<br/>GitHub · routines"]
            gate{"Speak now?<br/>urgent → yes<br/>with company → hold<br/>learned score below 0.4 → hold"}
            held[("Held for later")]
        end

        subgraph NIGHT["Nightly sleep pass (3am)"]
            consolidate["Consolidate the day<br/>summary · facts · reminders<br/>(in 6k-char chunks)"]
            review["Skill review<br/>today's tool use → new or better skills"]
        end
    end

    out["Voice out<br/>Piper TTS / any client on /ws"]
    owner(("Owner's reaction<br/>within 90 s"))

    mic --> hear
    ptt --> llm
    radar --> presence
    presence --> who
    hear --> mem
    hear --> who
    who -- yes --> ctx
    prefix --> llm
    ctx --> llm
    mem -. recall .-> ctx
    llm <--> tools
    tools <--> mem
    tools <--> skills
    llm --> out

    feeds --> gate
    presence --> gate
    gate -- now --> out
    gate -- later --> held
    held -- "company leaves<br/>or owner talks" --> out

    out --> owner
    owner -- "pushback / engagement<br/>tunes per-kind score" --> gate

    mem --> consolidate --> mem
    llm -. worklog .-> review --> skills
    skills -. "index refreshed once a night" .-> prefix
```

## The two learning loops

```mermaid
flowchart LR
    subgraph WHEN["Learns WHEN to speak (every interruption)"]
        a1[FRIDAY interrupts] --> a2{Owner's reaction}
        a2 -- "engages" --> a3[score up]
        a2 -- "not now, I know, stop" --> a4[score down]
        a3 & a4 --> a5{score below 0.4?}
        a5 -- yes --> a6[that kind waits for a good moment]
        a5 -- no --> a7[keeps interrupting]
    end

    subgraph HOW["Learns HOW to do things (nightly)"]
        b1[Tasks done with tools today] --> b2[Review with the model]
        b2 --> b3[Write or fix SKILL.md]
        b3 --> b4[Index goes into tomorrow's prompt]
        b4 --> b5[skill_view loads the steps when a request matches]
        b5 --> b1
    end
```

## Why it stays fast on a small model

- **Cached prefix.** The persona, skill index and tool list are byte-identical on every turn, so
  llama.cpp and vLLM only process the new message. The prefix changes once a night, when new skills land.
- **Progressive disclosure.** Each skill is one line in the prompt. The steps load only when used.
- **Capped context.** 8 facts, 4 memories, 12 history messages, 3,000 characters per tool result.

Skills and the progressive-disclosure design come from
[Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research (MIT), rewritten small.
FRIDAY reads Hermes and agentskills.io `SKILL.md` files as they are.
