"""Memory: MemPalace keeps the exact words; Honcho builds a model of who said them.

Writes happen on a background thread so nothing here slows the conversation. Either backend
may be absent or down; the agent keeps working with whatever is left.
"""

import datetime
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor

LOCAL_HONCHO = "http://localhost:8000"


def honcho_url(cfg: dict) -> str:
    """The configured Honcho, or the bundled one if `friday up` has it running."""
    url = cfg["honcho"]["base_url"]
    if url != "auto":
        return url
    try:
        urllib.request.urlopen(f"{LOCAL_HONCHO}/health", timeout=0.3)
        return LOCAL_HONCHO
    except OSError:
        return ""


class Memory:
    def __init__(self, cfg: dict, session: str):
        self.user = cfg["user"]
        self.session_id = session
        self.errors: dict[str, str] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="friday-memory")
        self._honcho = self._session = None
        if url := honcho_url(cfg):
            try:
                from honcho import Honcho

                self._honcho = Honcho(
                    base_url=url,
                    workspace_id=cfg["honcho"]["workspace"],
                    api_key=os.environ.get("HONCHO_API_KEY"),
                )
                self._session = self._honcho.session(session)
            except Exception as e:
                self.errors["honcho"] = repr(e)

    def remember(self, speaker: str, text: str) -> None:
        """Store `text` verbatim, attributed to `speaker` (the user or an agent name)."""
        if text and text.strip():
            self._pool.submit(self._remember, speaker, text)

    def _remember(self, speaker: str, text: str) -> None:
        try:
            from mempalace.mcp_server import tool_add_drawer

            room = datetime.date.today().isoformat()
            r = tool_add_drawer(wing=speaker, room=room, content=text, added_by="friday")
            if isinstance(r, dict) and r.get("error"):
                self.errors["mempalace"] = str(r["error"])
        except Exception as e:
            self.errors["mempalace"] = repr(e)
        if self._session is not None:
            try:
                self._session.add_messages([self._honcho.peer(speaker).message(text)])
            except Exception as e:
                self.errors["honcho"] = repr(e)

    def recall(self, query: str, limit: int = 4) -> str:
        """Context worth injecting for `query`: verbatim hits plus Honcho's picture of the user."""
        parts = []
        try:
            from mempalace.mcp_server import tool_search

            hits = tool_search(query=query, limit=limit).get("results") or []
            parts += [f"[{h.get('wing', '?')}] {h['text']}" for h in hits if h.get("text")]
        except Exception as e:
            self.errors["mempalace"] = repr(e)
        if self._session is not None:
            try:
                ctx = self._session.context(
                    summary=False, tokens=800, peer_target=self.user, search_query=query
                )
                if ctx.peer_representation:
                    parts.append(f"[what is known about {self.user}] {ctx.peer_representation}")
            except Exception as e:
                self.errors["honcho"] = repr(e)
        return "\n".join(parts)

    @property
    def honcho_on(self) -> bool:
        return self._session is not None

    def ask(self, question: str) -> str:
        """Honcho's dialectic: a reasoned answer about the user from everything it has seen."""
        return str(self._honcho.peer(self.user).chat(question))

    def wake_up(self) -> str:
        """MemPalace's identity + essential-story layers, for the start of a session."""
        try:
            from mempalace.layers import MemoryStack

            return MemoryStack().wake_up()
        except Exception as e:
            self.errors["mempalace"] = repr(e)
            return ""

    def flush(self) -> None:
        self._pool.submit(lambda: None).result()
