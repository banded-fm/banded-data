"""Music scenes: groups of a city's artists who play and record together, each
with a suggested name and a place on the map. Run on demand
(`concertdb scenes --city "Los Angeles"`), not as part of `run`.

Inputs:
  - Banded's graph.db(.gz) (the same file `seed` reads): each artist's
    MusicBrainz area and genres, and the links between artists (band
    memberships, support musicians, song credits, producers, opening slots);
  - this ledger: shows at the city's venues, so artists who share bills are
    linked, and so a scene can be placed where its artists play.

Steps:
  1. The city is its venues: those whose city is the one asked for give the
     centre, and every venue within the radius (60 km) counts. Their city
     names ("Long Beach", "Pasadena") are the places the city is made of.
  2. Local artists: a MusicBrainz area that is one of those places (or an
     extra --area name such as "Orange County"), or an artist who has played
     at least 3 shows here and at least 60% of their shows here.
  3. Links between local artists: a band membership weighs 3, a support
     musician or an opening slot 2, a producer 1, song credits one per song
     (up to 3), and each bill shared at a venue here 1 (up to 3).
  4. Groups: Louvain communities on that weighted network (resolution 1.5,
     fixed seed, so the same inputs give the same scenes).
  5. Not every artist belongs to a scene. An artist stays in its group only if
     at least half of its link weight here is inside the group and that weight
     is at least 3: a band member, or several songs or shows with the group.
     An actor with one song credit with a rapper is dropped. Groups with fewer
     than 8 artists left are dropped.
  6. Name: the place plus the group's most distinctive genre (MusicBrainz
     genres held by at least 12% of the group, ranked by share times how
     over-represented they are against the city's artists, capped at 3x), e.g.
     "Los Angeles thrash metal"; a genre a bigger scene there already took is skipped.
     The place is the city, or a place within it that at least half of the
     group's artists with a narrower area come from. Names are suggestions.
  7. Location, the first that applies: the venue nearest all the others among
     those where the group's artists shared bills (3+ shows), or else played
     (5+ shows); the place most of them come from; the city centre.
     `spread_km` is the median distance of those shows from it.

Writes a JSON file (default data/scenes/<city>.json); nothing in the ledger
changes apart from the record of the run.
"""

import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .normalize import norm

RADIUS_KM = 60
REL_WEIGHT = {"member_of": 3, "former_member_of": 3, "support_musician": 2, "opened_for": 2, "produced_for": 1}
CREDIT_CAP = 3              # collaborated_with: one per shared song credit, up to this
BILL_CAP = 3                # one per bill shared here, up to this
LOCAL_MIN_SHOWS = 3
LOCAL_SHARE = 0.6
CORE_SHARE = 0.5
CORE_WEIGHT = 3
MIN_SIZE = 8
RESOLUTION = 1.5         # higher splits more: 1.5 separates e.g. LA's West Coast jazz from its session players.
                         # Above 1 a network that is a single group is split up, which is fine for a city.
SEED = 7
GENRE_MIN_SHARE = 0.12
LIFT_CAP = 3            # a genre 3x as common here as in the city counts as fully distinctive
PLACE_NAME_SHARE = 0.5
PLACE_LOCATION_SHARE = 0.3
MIN_SHARED_BILLS = 3
MIN_SHOWS = 5


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def _mean(points: list[tuple[float, float]]) -> tuple[float, float]:
    return statistics.fmean(p[0] for p in points), statistics.fmean(p[1] for p in points)


def city_venues(conn: sqlite3.Connection, city: str, radius_km: float = RADIUS_KM):
    """(centre, {venue_id: venue row}, {place_norm: {"name", "lat", "lon", "venues"}}) for the city."""
    rows = conn.execute("SELECT id, name, city, lat, lon FROM venues WHERE lat IS NOT NULL AND lon IS NOT NULL").fetchall()
    core = [(r["lat"], r["lon"]) for r in rows if norm(r["city"]) == norm(city)]
    if not core:
        raise ValueError(f"no venues with coordinates in {city!r}")
    centre = _mean(core)
    venues = {r["id"]: r for r in rows if km(centre, (r["lat"], r["lon"])) <= radius_km}
    by_place = defaultdict(list)
    for r in venues.values():
        if r["city"]:
            by_place[norm(r["city"])].append(r)
    places = {}
    for key, vs in by_place.items():
        lat, lon = _mean([(v["lat"], v["lon"]) for v in vs])
        places[key] = {"name": Counter(v["city"] for v in vs).most_common(1)[0][0], "lat": lat, "lon": lon,
                       "venues": len(vs)}
    return centre, venues, places


def _shows(conn: sqlite3.Connection, venues: dict):
    """({show_id: (venue_id, [mbid])} for shows here, Counter of every show per MBID artist, Counter of shows here)."""
    here, plays, plays_here = defaultdict(list), Counter(), Counter()
    venue_of = {}
    for sid, vid, mbid in conn.execute(
            "SELECT s.id, s.venue_id, a.mbid FROM shows s JOIN billings b ON b.show_id = s.id"
            " JOIN artists a ON a.id = b.artist_id"
            " WHERE a.mbid IS NOT NULL AND s.is_festival = 0 AND s.status NOT IN ('cancelled', 'postponed')"):
        plays[mbid] += 1
        if vid in venues:
            plays_here[mbid] += 1
            here[sid].append(mbid)
            venue_of[sid] = vid
    return {sid: (venue_of[sid], mbids) for sid, mbids in here.items()}, plays, plays_here


def _louvain(weights: dict, resolution: float, seed: int) -> list[set]:
    import networkx as nx   # optional dependency: pip install -e '.[scenes]'
    from networkx.algorithms.community import louvain_communities
    g = nx.Graph()
    for (a, b), w in weights.items():
        g.add_edge(a, b, weight=w)
    return louvain_communities(g, weight="weight", resolution=resolution, seed=seed)


def find(conn: sqlite3.Connection, graph: sqlite3.Connection, city: str, radius_km: float = RADIUS_KM,
         extra_areas: tuple[str, ...] = (), min_size: int = MIN_SIZE, resolution: float = RESOLUTION,
         seed: int = SEED) -> dict:
    """conn: the ledger; graph: an open graph.db. Returns the scenes as a JSON-ready dict."""
    centre, venues, places = city_venues(conn, city, radius_km)
    area_keys = set(places) | {norm(a) for a in extra_areas}
    nodes = {}
    for mbid, name, typ, area, genres in graph.execute(
            "SELECT id, name, type, area, genres FROM nodes WHERE type IN ('person', 'group')"):
        nodes[mbid] = (name, typ, area or "", [int(x) for x in (genres or "").split(",") if x.strip()])
    genre_name = dict(graph.execute("SELECT id, name FROM genres"))
    shows, plays, plays_here = _shows(conn, venues)

    by_area = {m for m, n in nodes.items() if norm(n[2]) in area_keys}
    by_shows = {m for m, k in plays_here.items()
                if m in nodes and k >= LOCAL_MIN_SHOWS and k >= LOCAL_SHARE * plays[m]}
    local = by_area | by_shows

    weights: Counter = Counter()

    def link(a, b, w):
        if a != b:
            weights[(a, b) if a < b else (b, a)] += w
    ids = sorted(local)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        for s, t, rel, count in graph.execute(
                f"SELECT source, target, rel_type, count FROM edges WHERE source IN ({q})", chunk):
            if t in local:
                link(s, t, REL_WEIGHT.get(rel, min(count or 1, CREDIT_CAP)))
    shared = Counter()
    for _, (_, mbids) in shows.items():
        acts = sorted({m for m in mbids if m in local})
        for i, a in enumerate(acts):
            for b in acts[i + 1:]:
                shared[(a, b)] += 1
    for (a, b), k in shared.items():
        link(a, b, min(k, BILL_CAP))

    strength: Counter = Counter()
    for (a, b), w in weights.items():
        strength[a] += w
        strength[b] += w
    city_genres = Counter(g for m in strength for g in set(nodes[m][3]))
    with_genres = sum(1 for m in strength if nodes[m][3]) or 1

    scenes = []
    for group in _louvain(weights, resolution, seed) if weights else []:
        inside: Counter = Counter()
        for (a, b), w in weights.items():
            if a in group and b in group:
                inside[a] += w
                inside[b] += w
        core = {m for m in group if inside[m] >= CORE_WEIGHT and inside[m] >= CORE_SHARE * strength[m]}
        if len(core) < min_size:
            continue
        scenes.append(_describe(core, inside, nodes, genre_name, city_genres, with_genres, places, venues, shows,
                                city, centre))
    scenes.sort(key=lambda s: -s["weight"])
    used: Counter = Counter()
    for i, s in enumerate(scenes, 1):
        s["id"] = f"{norm(city).replace(' ', '-')}-{i}"
        s["name"] = _name(s.pop("_place"), s.pop("_genres"), used)
    return {
        "city": city, "centre": {"lat": round(centre[0], 5), "lon": round(centre[1], 5)}, "radius_km": radius_km,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "graph_built_at": _meta(graph, "built_at"),
        "local_artists": len(local), "linked_artists": len(strength),
        "in_scenes": sum(s["size"] for s in scenes), "scenes": scenes,
    }


def _name(place: str, genres: list[str], used: Counter) -> str:
    """The place and the first genre a bigger scene there hasn't taken; "<place> scene" without one."""
    for g in genres:
        name = f"{place} {g}"
        if not used[name]:
            used[name] += 1
            return name
    base = f"{place} {genres[0]}" if genres else f"{place} scene"
    used[base] += 1
    return base if used[base] == 1 else f"{base} {used[base]}"


def _meta(graph: sqlite3.Connection, key: str) -> str | None:
    try:
        row = graph.execute("SELECT value FROM graph_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _describe(core, inside, nodes, genre_name, city_genres, with_genres, places, venues, shows, city, centre) -> dict:
    # Genres: held by 20%+ of the group, ranked by how over-represented they are against the city.
    has = [m for m in core if nodes[m][3]]
    gc = Counter(g for m in has for g in set(nodes[m][3]))
    genres = [(genre_name.get(g, str(g)), round(k / len(has), 2),
               (k / len(has)) * min((k / len(has)) / (city_genres[g] / with_genres), LIFT_CAP))
              for g, k in gc.items() if has and k / len(has) >= GENRE_MIN_SHARE]
    genres.sort(key=lambda x: (-x[2], -x[1]))
    # Places within the city the artists come from (narrower than the city itself).
    homes = Counter(nodes[m][2] for m in core if norm(nodes[m][2]) in places and norm(nodes[m][2]) != norm(city))
    narrow = sum(homes.values())
    home, home_n = homes.most_common(1)[0] if homes else (None, 0)
    place_name = home if home and home_n >= PLACE_NAME_SHARE * narrow and home_n >= 3 else city

    together = Counter(vid for vid, mbids in shows.values() if sum(1 for m in set(mbids) if m in core) >= 2)
    played = Counter(vid for vid, mbids in shows.values() for m in set(mbids) if m in core)
    location = None
    for counts, minimum, source in ((together, MIN_SHARED_BILLS, "shared bills"), (played, MIN_SHOWS, "shows")):
        if sum(counts.values()) >= minimum:
            pts = [(venues[v]["lat"], venues[v]["lon"]) for v, k in counts.items() for _ in range(k)]
            # The venue closest to all the others: a mean of venues across a big city lands in between them.
            hub = min(counts, key=lambda v: sum(k * km((venues[v]["lat"], venues[v]["lon"]),
                                                       (venues[u]["lat"], venues[u]["lon"])) for u, k in counts.items()))
            point = (venues[hub]["lat"], venues[hub]["lon"])
            location = {"lat": point[0], "lon": point[1], "source": source, "venue": venues[hub]["name"],
                        "spread_km": round(statistics.median(km(point, p) for p in pts), 1),
                        "venues": [[venues[v]["name"], k] for v, k in counts.most_common(5)]}
            break
    if location is None and home and home_n >= PLACE_LOCATION_SHARE * narrow and home_n >= 3:
        p = places[norm(home)]
        location = {"lat": p["lat"], "lon": p["lon"], "source": "home place", "venue": None, "spread_km": None,
                    "venues": []}
    if location is None:
        location = {"lat": centre[0], "lon": centre[1], "source": "city", "venue": None, "spread_km": None,
                    "venues": []}
    point = (location["lat"], location["lon"])
    venues_city = None
    if location.get("venue"):
        venues_city = next((v["city"] for v in venues.values() if v["name"] == location["venue"] and v["city"]), None)
    location["place"] = venues_city or min(places.values(), key=lambda p: km(point, (p["lat"], p["lon"])))["name"]
    location["lat"], location["lon"] = round(location["lat"], 5), round(location["lon"], 5)

    artists = sorted(core, key=lambda m: (-inside[m], nodes[m][0]))
    return {
        "name": None, "_place": place_name, "_genres": [g for g, _, _ in genres], "size": len(core), "weight": sum(inside[m] for m in core) // 2,
        "location": location,
        "genres": [[g, share] for g, share, _ in genres[:6]],
        "places": [[p, k] for p, k in homes.most_common(5)],
        "artists": [{"id": m, "name": nodes[m][0], "type": nodes[m][1], "links": inside[m]} for m in artists],
    }


def write(result: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    return path


def default_path(data_dir: Path, city: str) -> Path:
    return data_dir / "scenes" / f"{norm(city).replace(' ', '-')}.json"
