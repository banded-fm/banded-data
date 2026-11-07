import json
import sqlite3

import pytest

from concertdb import cli, ledger, scenes

pytest.importorskip("networkx")

LA, LB, SF = (34.05, -118.25), (33.77, -118.19), (37.77, -122.42)
GENRES = {1: "punk", 2: "hip hop", 3: "pop", 4: "rock"}


def _graph(path, nodes, edges):
    """A graph.db like Banded's: nodes (id, name, type, area, genres), edges, genres, graph_meta."""
    g = sqlite3.connect(path)
    g.executescript("""
        CREATE TABLE nodes (id TEXT PRIMARY KEY, name TEXT, type TEXT, disambiguation TEXT, area TEXT, genres TEXT);
        CREATE TABLE edges (source TEXT, target TEXT, rel_type TEXT, count INTEGER);
        CREATE TABLE genres (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE graph_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO graph_meta VALUES ('built_at', '2026-11-01');""")
    g.executemany("INSERT INTO genres VALUES (?, ?)", GENRES.items())
    g.executemany("INSERT INTO nodes VALUES (?, ?, ?, NULL, ?, ?)", nodes)
    g.executemany("INSERT INTO edges VALUES (?, ?, ?, ?)", edges)
    g.commit()
    return g


def _band(prefix, area, genre, n=9):
    """A group and n-1 members who also all record together (dense enough to stay one group on its own)."""
    nodes = [(f"{prefix}0", f"{prefix.upper()} Band", "group", area, str(genre))]
    nodes += [(f"{prefix}{i}", f"{prefix.upper()} {i}", "person", area, str(genre)) for i in range(1, n)]
    edges = [(f"{prefix}{i}", f"{prefix}0", "member_of", None) for i in range(1, n)]
    edges += [(f"{prefix}{i}", f"{prefix}{j}", "collaborated_with", 3) for i in range(1, n) for j in range(i + 1, n)]
    return nodes, edges


def _show(conn, date, venue, city, lat_lon, mbids):
    v = ledger.get_or_create_venue(conn, venue, city)
    conn.execute("UPDATE venues SET lat = ?, lon = ? WHERE id = ?", (*lat_lon, v))
    s = ledger.find_or_create_show(conn, date, v)
    for i, m in enumerate(mbids):
        a = ledger.get_or_create_artist(conn, m, m)
        ledger.add_claim(conn, s, a, "manual", f"manual:{date}:{venue}", role="headliner" if i == 0 else "opener",
                         confidence=1.0, position=i)
    return s


@pytest.fixture
def city(conn, tmp_path):
    punk_n, punk_e = _band("p", "Los Angeles", 1)
    rap_n, rap_e = _band("r", "Long Beach", 2)
    nodes = punk_n + rap_n + [
        ("kim", "Kim", "person", "Los Angeles", "3"),            # one song with a rapper: no scene
        ("sf", "SF Band", "group", "San Francisco", "4"),        # not local: ignored
        ("local", "Local Openers", "group", "", "1"),            # no area, but plays LA almost only
    ]
    edges = punk_e + rap_e + [("kim", "r1", "collaborated_with", 1), ("sf", "p0", "opened_for", None)]
    graph = _graph(tmp_path / "graph.db", nodes, edges)
    # Shows: the punk band and its members' bands share bills at the Echo; Local Openers opens for them.
    for d in range(1, 5):
        _show(conn, f"2025-0{d}-01", "The Echo", "Los Angeles", LA, ["p0", "local", f"p{d}"])
    _show(conn, "2025-06-01", "Bottom of the Hill", "San Francisco", SF, ["sf", "p0"])
    _show(conn, "2025-07-01", "Alex's Bar", "Long Beach", LB, ["r0"])
    conn.commit()
    return graph


def test_scenes_for_a_city(conn, city):
    r = scenes.find(conn, city, "Los Angeles")
    by_name = {s["name"]: s for s in r["scenes"]}
    assert set(by_name) == {"Los Angeles punk", "Long Beach hip hop"}
    punk, rap = by_name["Los Angeles punk"], by_name["Long Beach hip hop"]
    members = {a["id"] for a in punk["artists"]}
    assert "local" in members                       # counted as local from its shows
    assert "sf" not in members
    assert all(a["id"] != "kim" for s in r["scenes"] for a in s["artists"])
    assert punk["location"]["source"] == "shared bills" and punk["location"]["venue"] == "The Echo"
    assert punk["location"]["place"] == "Los Angeles" and punk["location"]["spread_km"] == 0.0
    # The rappers have one show (too few), but come from Long Beach.
    assert rap["location"]["source"] == "home place" and rap["location"]["place"] == "Long Beach"
    assert r["graph_built_at"] == "2026-11-01" and r["in_scenes"] == punk["size"] + rap["size"]


def test_radius_and_min_size(conn, city):
    # Long Beach is ~31 km away. With one band left, a resolution above 1 would split it up (Louvain
    # then prefers single artists to one group holding the whole network), so this call uses 1.0.
    near = scenes.find(conn, city, "Los Angeles", radius_km=10, resolution=1.0)["scenes"]
    assert [s["name"] for s in near] == ["Los Angeles punk"]
    assert scenes.find(conn, city, "Los Angeles", min_size=20)["scenes"] == []
    with pytest.raises(ValueError):
        scenes.find(conn, city, "Nowhere")


def test_extra_area(conn, city, tmp_path):
    oc_n, oc_e = _band("o", "Orange County", 4)
    city.executemany("INSERT INTO nodes VALUES (?, ?, ?, NULL, ?, ?)", oc_n)
    city.executemany("INSERT INTO edges VALUES (?, ?, ?, ?)", oc_e)
    assert len(scenes.find(conn, city, "Los Angeles")["scenes"]) == 2
    names = {s["name"] for s in scenes.find(conn, city, "Los Angeles", extra_areas=("Orange County",))["scenes"]}
    assert "Los Angeles rock" in names


def test_names_are_unique():
    used = scenes.Counter()
    assert scenes._name("LA", ["hip hop", "g-funk"], used) == "LA hip hop"
    assert scenes._name("LA", ["hip hop", "g-funk"], used) == "LA g-funk"
    assert scenes._name("LA", ["hip hop"], used) == "LA hip hop 2"
    assert scenes._name("LA", [], used) == "LA scene"


def test_km():
    assert round(scenes.km(LA, SF)) == 559


def test_cli_writes_json(conn, city, tmp_path, capsys):
    city.close()
    out = tmp_path / "scenes" / "la.json"
    cli.main(["--db", str(tmp_path / "ledger.sqlite"), "scenes", "--city", "Los Angeles",
              "--graph", str(tmp_path / "graph.db"), "--out", str(out)])
    data = json.loads(out.read_text())
    assert [s["name"] for s in data["scenes"]] and data["city"] == "Los Angeles"
    assert "Los Angeles punk" in capsys.readouterr().out
    run = conn.execute("SELECT step, ok, stats FROM runs WHERE step = 'scenes'").fetchone()
    assert run["ok"] == 1 and json.loads(run["stats"])["scenes"] == 2 and "_result" not in run["stats"]
    assert scenes.default_path(tmp_path, "Los Angeles") == tmp_path / "scenes" / "los-angeles.json"
