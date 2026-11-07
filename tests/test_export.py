from concertdb import export, ledger
from conftest import FLAT_WORMS, OSEES, add_graph_artist


def _show(conn, date, venue, acts, source="manual", conf=1.0):
    v = ledger.get_or_create_venue(conn, venue, "Los Angeles")
    s = ledger.find_or_create_show(conn, date, v)
    for i, name in enumerate(acts):
        a = ledger.get_or_create_artist(conn, name)
        ledger.add_claim(conn, s, a, source, f"{source}:{date}:{venue}", role="headliner" if i == 0 else "opener",
                         confidence=conf, position=i)
    return s


def test_opened_for_edges(conn):
    add_graph_artist(conn, OSEES, "Osees")
    add_graph_artist(conn, FLAT_WORMS, "Flat Worms")
    _show(conn, "2026-09-17", "Zebulon", ["Iguana Death Cult", "Flat Worms"])
    _show(conn, "2025-04-12", "The Echo", ["Osees", "Flat Worms"])
    _show(conn, "2025-06-01", "Teragram", ["Osees", "Flat Worms"])
    rows = export.opened_for(conn)
    edges = {(r["opener_name"], r["headliner_name"]): r["shows"] for r in rows}
    assert edges == {("Flat Worms", "Osees"): 2, ("Flat Worms", "Iguana Death Cult"): 1}
    known = export.opened_for(conn, known_only=True)
    assert [(r["opener_id"], r["headliner_id"]) for r in known] == [(FLAT_WORMS, OSEES)]


def test_cancelled_and_low_confidence_excluded(conn):
    s = _show(conn, "2026-10-05", "Zebulon", ["A", "B"])
    ledger.set_show_status(conn, s, "cancelled")
    _show(conn, "2026-10-06", "Zebulon", ["C", "D"], source="llm", conf=0.3)
    assert export.opened_for(conn) == []


def test_write_csv(conn, tmp_path):
    _show(conn, "2026-09-17", "Zebulon", ["Iguana Death Cult", "Flat Worms"])
    out = tmp_path / "e.csv"
    export.write_csv(export.opened_for(conn), out)
    lines = out.read_text().splitlines()
    assert lines[0].startswith("opener_id,opener_name")
    assert "Flat Worms" in lines[1]


def test_shows_export(conn, tmp_path):
    add_graph_artist(conn, OSEES, "Osees")
    add_graph_artist(conn, FLAT_WORMS, "Flat Worms")
    s = _show(conn, "2025-04-12", "The Echo", ["Osees", "Flat Worms"])
    conn.execute("UPDATE venues SET lat = 34.078, lon = -118.26")
    cancelled = _show(conn, "2025-05-01", "Zebulon", ["Osees"])
    ledger.set_show_status(conn, cancelled, "cancelled")
    _show(conn, "2025-06-01", "Zebulon", ["Unmatched Band"])
    rows = export.shows(conn)
    assert [(r["date"], r["artist"], r["role"]) for r in rows] == [
        ("2025-04-12", "Osees", "headliner"), ("2025-04-12", "Flat Worms", "opener"),
        ("2025-06-01", "Unmatched Band", "headliner")]
    assert rows[0]["venue"] == "The Echo" and rows[0]["city"] == "Los Angeles" and rows[0]["lat"] == 34.078
    assert rows[0]["show_id"] == s and rows[0]["mbid"] == OSEES and rows[0]["is_festival"] == 0
    assert [r["artist"] for r in export.shows(conn, known_only=True)] == ["Osees", "Flat Worms"]
    out = tmp_path / "shows.csv"
    export.write_csv(rows, out, export.SHOWS_COLUMNS)
    lines = out.read_text().splitlines()
    assert lines[0] == ",".join(export.SHOWS_COLUMNS) and len(lines) == 4


def test_cli_export_shows(tmp_path, capsys):
    from concertdb import cli
    dbp = str(tmp_path / "ledger.sqlite")
    cli.main(["--db", dbp, "add-show", "--date", "2025-04-12", "--venue", "The Echo", "--city", "Los Angeles",
              "--headliner", "Osees", "--opener", "Flat Worms"])
    out = tmp_path / "shows.csv"
    cli.main(["--db", dbp, "export", "shows", "--out", str(out)])
    assert "wrote 2 acts on 1 shows" in capsys.readouterr().out
    assert len(out.read_text().splitlines()) == 3
