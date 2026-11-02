"""Outside-world feeds: NWS alerts, USGS quakes, GitHub, and public traffic cameras.
Camera catalogs are ported from bilawalsidhu/gods-eye-view (MIT): server/providers/cctv/sources.js."""
import math
import re
import time

import httpx

UA = {"User-Agent": "FRIDAY personal assistant (self-hosted)"}


def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


# --- parsers (pure, tested) -------------------------------------------------
def parse_nws(payload):
    out = []
    for f in payload.get("features", []):
        p = f.get("properties", {})
        out.append({"id": p.get("id") or f.get("id"), "event": p.get("event"), "severity": p.get("severity"),
                    "headline": p.get("headline"), "area": p.get("areaDesc")})
    return out


def parse_usgs(payload, lat, lon, radius_km):
    out = []
    for f in payload.get("features", []):
        qlon, qlat = f["geometry"]["coordinates"][:2]
        d = km(lat, lon, qlat, qlon)
        if d <= radius_km:
            p = f["properties"]
            out.append({"id": f["id"], "mag": p.get("mag"), "place": p.get("place"), "km": round(d)})
    return sorted(out, key=lambda q: -(q["mag"] or 0))


_NYC_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _pinned(url, origin):
    # Only fetch frames from the agency's own host, never wherever a catalog row points (SSRF guard, as upstream).
    return url if isinstance(url, str) and url.startswith(origin) else None


def parse_nyc(rows):
    cams = []
    for r in rows if isinstance(rows, list) else []:
        uid = str(r.get("id", ""))
        if str(r.get("isOnline")).lower() != "true" or not _NYC_ID.match(uid):
            continue
        try:
            cams.append({"id": f"nyc-{uid.lower()}", "name": r.get("name") or uid, "lat": float(r["latitude"]),
                         "lon": float(r["longitude"]), "provider": "NYC DOT",
                         "image": f"https://webcams.nyctmc.org/api/cameras/{uid}/image"})
        except (KeyError, TypeError, ValueError):
            continue
    return cams


def parse_tfl(places):
    cams = []
    for p in places if isinstance(places, list) else []:
        props = {a.get("key"): a.get("value") for a in p.get("additionalProperties", [])}
        img = _pinned(props.get("imageUrl"), "https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/")
        if str(props.get("available")).lower() != "true" or not img or p.get("lat") is None:
            continue
        cams.append({"id": "tfl-" + str(p.get("id", "")).replace("JamCams_", ""), "name": p.get("commonName"),
                     "lat": float(p["lat"]), "lon": float(p["lon"]), "provider": "Transport for London", "image": img})
    return cams


def parse_caltrans(payload):
    cams = []
    for row in payload.get("data", []):
        c = row.get("cctv") or {}
        loc = c.get("location") or {}
        img = _pinned(((c.get("imageData") or {}).get("static") or {}).get("currentImageURL"), "https://cwwp2.dot.ca.gov/")
        if str(c.get("inService")).lower() != "true" or not img:
            continue
        try:
            cams.append({"id": "caltrans-" + str(c.get("index", len(cams))), "name": loc.get("locationName"),
                         "lat": float(loc["latitude"]), "lon": float(loc["longitude"]), "provider": "Caltrans",
                         "image": img})
        except (KeyError, TypeError, ValueError):
            continue
    return cams


CAMERA_SOURCES = [
    ("https://webcams.nyctmc.org/api/cameras", parse_nyc),
    ("https://api.tfl.gov.uk/Place/Type/JamCam", parse_tfl),
    *[(f"https://cwwp2.dot.ca.gov/data/d{d}/cctv/cctvStatusD{d:02d}.json", parse_caltrans) for d in (3, 4, 7, 11)],
]


class Feeds:
    def __init__(self, github_token="", client=None):
        self.client = client or httpx.AsyncClient(timeout=20, headers=UA, follow_redirects=False)
        self.github_token = github_token
        self._cams, self._cams_at = [], 0.0

    async def _json(self, url, **kw):
        r = await self.client.get(url, **kw)
        r.raise_for_status()
        return r.json()

    async def alerts(self, lat, lon):
        return parse_nws(await self._json(f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}"))

    async def quakes(self, lat, lon, radius_km=300):
        data = await self._json("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson")
        return parse_usgs(data, lat, lon, radius_km)

    async def github(self):
        if not self.github_token:
            return []
        rows = await self._json("https://api.github.com/notifications", headers={
            "Authorization": f"Bearer {self.github_token}", "Accept": "application/vnd.github+json"})
        return [{"id": n["id"], "repo": n["repository"]["full_name"], "title": n["subject"]["title"],
                 "type": n["subject"]["type"], "reason": n["reason"]} for n in rows]

    async def cameras(self):
        if time.time() - self._cams_at > 6 * 3600 or not self._cams:
            cams = []
            for url, parse in CAMERA_SOURCES:
                try:
                    cams += parse(await self._json(url))
                except (httpx.HTTPError, ValueError):
                    continue  # one agency down shouldn't blind the rest
            self._cams, self._cams_at = cams, time.time()
        return self._cams

    async def cameras_near(self, lat, lon, n=5, max_km=8):
        cams = [dict(c, km=round(km(lat, lon, c["lat"], c["lon"]), 2)) for c in await self.cameras()]
        return sorted((c for c in cams if c["km"] <= max_km), key=lambda c: c["km"])[:n]

    async def frame(self, camera_id):
        cam = next((c for c in await self.cameras() if c["id"] == camera_id), None)
        if not cam:
            return None
        r = await self.client.get(cam["image"])
        r.raise_for_status()
        return r.content
