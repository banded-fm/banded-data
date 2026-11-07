"""Derived output for Banded: opened_for edges (opener → headliner), and every
act on every show with its venue's location (for Banded's scenes script)."""

import csv
import sqlite3
from pathlib import Path

QUERY = """
SELECT o.artist_id AS opener_id, oa.name AS opener_name,
       h.artist_id AS headliner_id, ha.name AS headliner_name,
       COUNT(*) AS shows,
       ROUND(MAX(MIN(o.confidence, h.confidence)), 3) AS confidence,
       MIN(s.date) AS first_date, MAX(s.date) AS last_date
FROM billings o
JOIN billings h ON h.show_id = o.show_id AND h.role = 'headliner'
JOIN shows s ON s.id = o.show_id
JOIN artists oa ON oa.id = o.artist_id
JOIN artists ha ON ha.id = h.artist_id
WHERE o.role = 'opener'
  AND s.is_festival = 0
  AND s.status NOT IN ('cancelled', 'postponed')
  AND o.confidence >= :min_conf AND h.confidence >= :min_conf
  AND (:known_only = 0 OR (oa.mbid IS NOT NULL AND ha.mbid IS NOT NULL))
GROUP BY o.artist_id, h.artist_id
ORDER BY shows DESC, opener_name
"""


OPENED_FOR_COLUMNS = ["opener_id", "opener_name", "headliner_id", "headliner_name",
                      "shows", "confidence", "first_date", "last_date"]

# One row per act per show: cancelled and postponed shows left out, festivals flagged.
SHOWS_QUERY = """
SELECT s.id AS show_id, s.date, s.is_festival,
       v.id AS venue_id, v.name AS venue, v.city, v.state, v.country, v.lat, v.lon,
       a.mbid, a.name AS artist, b.role, ROUND(b.confidence, 3) AS confidence
FROM billings b
JOIN shows s ON s.id = b.show_id
JOIN venues v ON v.id = s.venue_id
JOIN artists a ON a.id = b.artist_id
WHERE s.status NOT IN ('cancelled', 'postponed')
  AND b.confidence >= :min_conf
  AND (:known_only = 0 OR a.mbid IS NOT NULL)
ORDER BY s.date, s.id, b.role = 'opener', b.position
"""

SHOWS_COLUMNS = ["show_id", "date", "is_festival", "venue_id", "venue", "city", "state", "country", "lat", "lon",
                 "mbid", "artist", "role", "confidence"]


def opened_for(conn: sqlite3.Connection, min_conf: float = 0.4, known_only: bool = False) -> list[sqlite3.Row]:
    """known_only: both acts are MusicBrainz artists (not unmatched 'name:' ones)."""
    return conn.execute(QUERY, {"min_conf": min_conf, "known_only": int(known_only)}).fetchall()


def shows(conn: sqlite3.Connection, min_conf: float = 0.0, known_only: bool = False) -> list[sqlite3.Row]:
    """Every act on every show that wasn't cancelled or postponed, with the venue's city and coordinates.
    min_conf is the role's confidence, so the default 0 keeps acts whose role is unknown.
    known_only: acts matched to MusicBrainz only."""
    return conn.execute(SHOWS_QUERY, {"min_conf": min_conf, "known_only": int(known_only)}).fetchall()


def write_csv(rows: list[sqlite3.Row], path: str | Path, cols: list[str] = OPENED_FOR_COLUMNS) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] for c in cols])
