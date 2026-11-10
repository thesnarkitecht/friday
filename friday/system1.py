"""System 1: fast, typed, confidence-scored. Decides, acts and sees so System 2 doesn't have to.

decide -> Jev (TypeSafe): typed answers to named questions about a state, with confidence.
act    -> Needle 3 (Cactus): one on-device tool call from text, with confidence.
see    -> Mirage (Cactus): on-device OCR + object boxes from an image.

Every answer carries a confidence, and every caller gates on it: System 1 either is sure or hands
the decision up. It never blocks the agent - a missing key or model degrades to defaults.
"""

import json
import os
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class Answer:
    value: object
    confidence: float = 0.0  # 0..1; 0 means "nobody answered, this is the default"
    probs: dict = field(default_factory=dict)  # choice questions: option -> probability


def _questions_for_wire(questions: dict) -> dict:
    return {k: {f: v for f, v in q.items() if f != "default"} for k, q in questions.items()}


class S1:
    def __init__(self, cfg: dict, llm=None):
        self.cfg = cfg["s1"]
        self.llm = llm  # callable(prompt) -> str; emulates Jev when no key is set
        self.stats = Counter()
        self.last_error = ""
        self._jev = None

    # -- decide ---------------------------------------------------------------------------

    @property
    def has_jev(self) -> bool:
        return bool(os.environ.get("TYPESAFE_API_KEY"))

    def decide(self, state: str, questions: dict, emulate: bool = True) -> dict[str, Answer]:
        """Answer Jev-format questions (choice / noul / score) about `state`.

        Each question may carry a "default"; it is returned with confidence 0 when nothing can
        answer. `emulate=False` skips the LLM stand-in, for callers where a slow answer is
        worse than none.
        """
        out = {k: Answer(q.get("default")) for k, q in questions.items()}
        try:
            if self.has_jev:
                out.update(self._decide_jev(state, questions))
                self.stats["jev"] += 1
            elif emulate and self.llm:
                out.update(self._decide_llm(state, questions))
                self.stats["s2_as_s1"] += 1
        except Exception as e:  # a decision must never take the agent down
            self.stats["s1_error"] += 1
            self.last_error = repr(e)
        return out

    def _decide_jev(self, state: str, questions: dict) -> dict[str, Answer]:
        if self._jev is None:
            from typesafe_sdk import TypeSafeClient

            self._jev = TypeSafeClient(model=self.cfg["jev_model"])
        answers = self._jev.system_one(state, _questions_for_wire(questions)).answers
        out = {}
        for name, a in answers.items():
            if a.type == "choice":
                out[name] = Answer(a.choice, a.confidence, dict(a.probabilities))
            elif a.type == "noul":  # a probability of "yes", not a bool
                out[name] = Answer(a.noul >= 0.5, abs(a.noul - 0.5) * 2)
            elif a.type == "score":
                out[name] = Answer(a.score, a.confidence)
        return out

    def _decide_llm(self, state: str, questions: dict) -> dict[str, Answer]:
        spec = {
            k: {"instructions": q.get("instructions"), "allowed": _allowed(q)}
            for k, q in questions.items()
        }
        raw = self.llm(
            "Answer each question about the STATE. Reply with one JSON object mapping each "
            "question name to one of its allowed values, nothing else.\n\n"
            f"QUESTIONS:\n{json.dumps(spec)}\n\nSTATE:\n{state}"
        )
        data = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
        # shortcut: an LLM has no calibrated confidence, so valid answers get a flat 0.75;
        # set TYPESAFE_API_KEY for real ones.
        return {
            k: Answer(v, 0.75, {v: 0.75} if questions[k]["type"] == "choice" else {})
            for k, v in data.items()
            if k in questions and v in _allowed(questions[k])
        }

    # -- act ------------------------------------------------------------------------------

    def act(self, text: str, tools: list[dict], system: str | None = None) -> tuple[list[dict], float]:
        """One-shot tool call(s) for `text`. Returns ([{name, arguments}], confidence)."""
        try:
            import cactus

            agent = cactus.Cactus(
                tools=tools, system=system, weights=self.cfg["needle_weights"] or None, stateless=True
            )
            r = agent.complete(text)
        except Exception as e:  # no weights / offline / unsupported platform
            self.stats["s1_error"] += 1
            self.last_error = repr(e)
            return [], 0.0
        self.stats["needle"] += 1
        return r.get("function_calls") or [], r.get("confidence") or 0.0

    # -- see ------------------------------------------------------------------------------

    def see(self, image) -> dict:
        """OCR text and objects with 0-1000 normalised [x0, y0, x1, y1] boxes."""
        import cactus

        self.stats["mirage"] += 1
        return cactus.describe(image)


def _allowed(q: dict) -> list:
    if q["type"] == "choice":
        return list(q["criteria"])
    if q["type"] == "score":
        return list(range(len(q["criteria"])))
    return [True, False]
