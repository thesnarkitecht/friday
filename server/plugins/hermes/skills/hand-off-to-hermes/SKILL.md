---
name: hand-off-to-hermes
description: When a request needs real research, reading code, or writing code (more than a quick answer).
---

1. Restate the task for Hermes with everything it needs: the goal, constraints the owner mentioned,
   what "done" looks like, and which repo or topic. Hermes can't ask the owner follow-up questions.
2. Call `ask_hermes` once. Tell the owner in one sentence that it's handed off.
3. When Hermes reports back, `recall` finds the full answer ("Hermes on ...").
