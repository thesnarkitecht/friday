# How FRIDAY works

One install (`pip install ./server`) is all of it. `friday` runs everything below on the device; with
`friday serve` on an always-on machine, other devices log in and only their headphone audio travels.

## The whole agent

```mermaid
flowchart TB
    subgraph DEVICE["Any device: friday + paired headphones"]
        buds["Headphone mic + speaker"]
        vad["Speech detection<br/>cuts utterances · barge-in stops FRIDAY"]
        typed["Typing, friday ask,<br/>OpenAI clients"]
    end

    subgraph SERVER["FRIDAY engine (same process, or a shared hub)"]
        stt["SenseVoice<br/>speech → text + how it sounded"]
        tts["Piper voice (sherpa-onnx)<br/>text → speech"]
        hear["hear()<br/>score + remember every line"]
        who{"Meant for FRIDAY?<br/>wake word · 10 s follow-up<br/>alone + sounds like a request → Claude decides (typed)<br/>other voice → never"}

        subgraph BRAIN["Brain"]
            prefix["Cached prefix<br/>persona + skill index + tools"]
            ctx["Per-turn [context]<br/>time · situation · last 5 min overheard<br/>8 facts · 4 memories"]
            claude(["Claude or any OpenAI-compatible API<br/>streaming: each sentence spoken as written"])
            tools["Tools<br/>remember · forget · recall · reminders<br/>routines · skill_view · skill_save<br/>+ plugin tools"]
        end

        mem[("Memory (SQLite)<br/>facts · interests · day summaries<br/>reminders · routines · questions<br/>graph: entities + links")]
        skills[("Skills<br/>SKILL.md")]

        subgraph PRO["Proactive loop (every minute)"]
            due["Reminders · routines"]
            feeds["Plugin feeds, plain HTTP, zero tokens<br/>new models (OpenRouter) · Hacker News"]
            ask["Greeting when headphones go on<br/>check-ins · one question a day"]
            gate{"Speak now?<br/>urgent → yes<br/>company or quiet hours → hold<br/>learned score below 0.4 → hold"}
            held[("Held for a good moment")]
        end

        subgraph NIGHT["3am sleep pass (typed outputs)"]
            keep["Keep what matters<br/>facts · rapport · summary · reminders<br/>check-ins · one pattern<br/>delete the rest"]
            review["Rebuild micro skills<br/>from today's tool use"]
            newq["Think of new<br/>preference questions"]
        end
    end

    hermes(["Hermes Agent<br/>OpenAI-compatible API<br/>research + coding"])
    you(("Your reaction<br/>within 90 s"))

    buds --> vad -- "audio over /ws" --> stt --> hear
    typed -- "typed = you talking" --> ctx
    hear --> mem
    hear --> who
    who -- yes --> ctx
    mem -. recall .-> ctx
    prefix --> claude
    ctx --> claude
    claude <--> tools
    tools <--> mem
    tools <--> skills
    tools -- ask_hermes --> hermes
    hermes -- "report back (always speaks)" --> gate
    claude -- reply --> tts

    due --> gate
    feeds --> gate
    ask --> tts
    gate -- now --> tts
    gate -- later --> held
    held -- "company leaves<br/>or you talk to FRIDAY" --> tts
    tts -- "speech over /ws" --> buds

    buds --> you
    you -- "not now / engaged<br/>tunes each kind" --> gate
    you -- "answers become<br/>preferences + interests" --> mem
    mem -. interests .-> feeds

    mem --> keep --> mem
    claude -. worklog .-> review --> skills
    keep --> newq --> mem
    skills -. "index refreshed once a night" .-> prefix
```

## The learning loops

```mermaid
flowchart LR
    subgraph WHEN["When to speak (every interruption)"]
        a1[FRIDAY interrupts] --> a2{Your reaction}
        a2 -- engages --> a3[score up]
        a2 -- "not now, I know, stop" --> a4[score down]
        a3 & a4 --> a5{score below 0.4?}
        a5 -- yes --> a6[waits for a good moment]
        a5 -- no --> a7[keeps interrupting]
    end

    subgraph HOW["How to do things (nightly)"]
        b1[Tasks done with tools today] --> b2[Claude reviews them]
        b2 --> b3[Write or fix SKILL.md]
        b3 --> b4[Index goes into tomorrow's prompt]
        b4 --> b5[skill_view loads the steps when needed]
        b5 --> b1
    end

    subgraph WHO["Who you are (daily)"]
        c1[One preference question] --> c2[Your answer]
        c2 --> c3[Preference + interests saved]
        c3 --> c4[Hacker News watches those topics]
        c3 --> c5[Nightly: new questions from what's still unknown]
        c5 --> c1
    end
```

## Design rules

- **The prompt prefix stays fixed.** Persona, skill index and tool list are byte-identical every turn, so
  Claude's prompt cache covers them. The per-turn context is small and capped. The prefix changes once a
  night, when new skills land.
- **Watching costs nothing.** Feeds are plain HTTP and string matching; Claude is only called when you
  talk to FRIDAY, for routines, and in the nightly pass.
- **Quick decisions are typed.** Whether a line was meant for FRIDAY, what to keep from the day, which
  skills to write, which questions to ask: each comes back as a validated Pydantic object, not text
  to parse.
- **Assist, don't automate.** Tools read, remember and report. Hermes is consulted, not given control.
  Text from tools is information, never instructions.

Skills and progressive disclosure are adapted from [Hermes Agent](https://github.com/NousResearch/hermes-agent)
by Nous Research (MIT). FRIDAY reads Hermes and agentskills.io `SKILL.md` files as they are.
