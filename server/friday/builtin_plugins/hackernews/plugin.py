"""Hacker News stories that match the owner's interests, checked every 10 minutes (no tokens).

Interests are the facts saved with category "interest" (FRIDAY saves them when you say what you
care about, or answer "Which topics should I watch Hacker News for?")."""
import re

API = "https://hn.algolia.com/api/v1"


def matches(hits, interests, min_points=30):
    """Pure: front-page hits -> stories whose title mentions an interest as a whole word."""
    pats = [(i, re.compile(rf"(?<!\w){re.escape(i)}(?!\w)", re.I)) for i in interests if i.strip()]
    out = []
    for h in hits:
        title = h.get("title") or ""
        hit = next((i for i, p in pats if p.search(title)), None)
        if hit and (h.get("points") or 0) >= min_points:
            out.append({"key": h["objectID"], "kind": "hn",
                        "text": f"On Hacker News, for your interest in {hit}: {title}."})
    return out


def setup(p):
    @p.feed(every_s=600)
    async def front_page():
        interests = p.memory.interests()
        if not interests:
            return None  # nothing to watch for yet
        r = await p.http.get(f"{API}/search", params={"tags": "front_page", "hitsPerPage": 60})
        r.raise_for_status()
        return matches(r.json().get("hits", []), interests)

    @p.tool("Search Hacker News stories (newest relevant first).", query=("string", "what to look for"))
    async def hacker_news(query):
        r = await p.http.get(f"{API}/search", params={"query": query, "tags": "story", "hitsPerPage": 8})
        r.raise_for_status()
        return {"stories": [{"title": h.get("title"), "points": h.get("points"), "url": h.get("url"),
                             "comments": f"https://news.ycombinator.com/item?id={h['objectID']}"}
                            for h in r.json().get("hits", [])]}
