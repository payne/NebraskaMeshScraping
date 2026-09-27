#!/usr/bin/env python3
"""
Nebraska Mesh node tracker.

nebraskamesh.net is a MeshCore (LoRa mesh) network. As of Sep 2026 the
nebraskamesh.net site itself doesn't expose its own node-list API -- its
"Open the Map" / "Open Analyzer" links point at placeholder URLs, and its
live map is really an embed of the *global* MeshCore network map. So this
tool pulls node data from the same backend that global map uses
(https://map.meshcore.dev), filters it down to a Nebraska bounding box, and
upserts the result into a local SQLite database along with a change/activity
log. If Nebraska Mesh ever stands up its own dedicated API, swap NODE_API_URL
below.

Usage:
    python nebraska_mesh_tracker.py run                 # one fetch + DB update
    python nebraska_mesh_tracker.py run --loop 3600      # repeat every hour (foreground)
    python nebraska_mesh_tracker.py list                 # show nodes currently tracked
    python nebraska_mesh_tracker.py history [--limit 50] # recent activity log
    python nebraska_mesh_tracker.py runs                 # recent fetch-run summaries

Typically you'd run `run` on a schedule via cron / systemd timer rather than
using --loop (see README.md).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

# --------------------------------------------------------------------------
# Configuration (override via environment variables)
# --------------------------------------------------------------------------

NODE_API_URL = os.environ.get("MESH_API_URL", "https://map.meshcore.dev/api/v1/nodes")
DB_PATH = os.environ.get("MESH_DB_PATH", str(Path(__file__).parent / "nebraska_mesh.db"))
REQUEST_TIMEOUT = float(os.environ.get("MESH_HTTP_TIMEOUT", "30"))

# Nebraska bounding box, padded a bit beyond the state line so nodes just
# across the border (western Iowa, northern Kansas, etc.) aren't dropped.
# Override any of these with env vars if you want a tighter/looser area.
BBOX = {
    "lat_min": float(os.environ.get("MESH_LAT_MIN", "39.9")),
    "lat_max": float(os.environ.get("MESH_LAT_MAX", "43.1")),
    "lon_min": float(os.environ.get("MESH_LON_MIN", "-104.15")),
    "lon_max": float(os.environ.get("MESH_LON_MAX", "-95.2")),
}

# How far apart two fetches of the same node's coordinates have to be
# (in degrees, ~0.0005 ~= 55m) before we log it as "moved" rather than noise.
COORD_CHANGE_THRESHOLD = float(os.environ.get("MESH_COORD_THRESHOLD", "0.0005"))

# A node that stops appearing in the filtered result set for this many
# consecutive runs gets marked inactive (rather than on the very first miss,
# which could just be a transient API hiccup).
MISSING_RUNS_BEFORE_INACTIVE = int(os.environ.get("MESH_MISSING_GRACE", "1"))

TYPE_NAMES = {1: "Client", 2: "Repeater", 3: "Room Server", 4: "Sensor"}

# Maps the API's field names to the ones used here. Covers the short names
# from "short=1" / msgpack mode, plus adv_lat/adv_lon, which is how the API
# returns coordinates (as of Sep 2026 it ignores short=1 and sends full names).
SHORT_KEY_MAP = {
    "pk": "public_key",
    "t": "type",
    "n": "adv_name",
    "la": "last_advert",
    "id": "inserted_date",
    "ud": "updated_date",
    "p": "params",
    "l": "link",
    "s": "source",
    "adv_lat": "lat",
    "adv_lon": "lon",
}

logging.basicConfig(
    level=os.environ.get("MESH_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("nebraska_mesh_tracker")


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_raw_nodes() -> list[dict[str, Any]]:
    """Fetch the full node list from the MeshCore map API.

    Tries JSON first (short field names to keep the payload down). Falls
    back to msgpack decoding if the server responds with a binary body
    instead -- this only imports the `msgpack` package if actually needed.
    """
    resp = requests.get(
        NODE_API_URL,
        params={"short": 1},
        headers={"Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    if "json" in content_type:
        return resp.json()

    try:
        return json.loads(resp.text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    try:
        import msgpack  # optional dependency, only needed for this fallback
    except ImportError as exc:
        raise RuntimeError(
            "API returned a non-JSON (likely msgpack) body and the "
            "'msgpack' package isn't installed. Run: pip install msgpack"
        ) from exc

    return msgpack.unpackb(resp.content, timestamp=3, raw=False)


def _to_hex(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, str):
        return value
    if isinstance(value, list):  # msgpack sometimes surfaces bytes as int lists
        return bytes(value).hex()
    return str(value)


def _to_iso(value: Any) -> Optional[str]:
    """Normalize a timestamp field (epoch seconds/millis, ISO string, or a
    datetime already decoded by msgpack's timestamp extension) to ISO 8601."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        # Heuristic: treat 13+ digit numbers as milliseconds.
        seconds = value / 1000.0 if value > 10**12 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    return str(value)


def normalize_node(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Rename short keys to readable ones and coerce field types.
    Returns None if the record is missing something essential (pk/lat/lon)."""
    node: dict[str, Any] = {}
    for key, value in raw.items():
        mapped_key = SHORT_KEY_MAP.get(key, key)
        node[mapped_key] = value

    if "public_key" not in node or "lat" not in node or "lon" not in node:
        return None
    if node["lat"] is None or node["lon"] is None:
        return None

    node["public_key"] = _to_hex(node["public_key"])
    node["link"] = _to_hex(node.get("link"))
    node["last_advert"] = _to_iso(node.get("last_advert"))
    node["inserted_date"] = _to_iso(node.get("inserted_date"))
    node["updated_date"] = _to_iso(node.get("updated_date"))

    type_code = node.get("type")
    node["type_code"] = type_code
    node["type_name"] = TYPE_NAMES.get(type_code, f"Unknown ({type_code})")

    params = node.get("params") or {}
    node["freq_mhz"] = params.get("freq")
    node["bandwidth_khz"] = params.get("bw")
    node["spreading_factor"] = params.get("sf")
    node["coding_rate"] = params.get("cr")

    node["lat"] = float(node["lat"])
    node["lon"] = float(node["lon"])

    return node


def in_bbox(node: dict[str, Any], bbox: dict[str, float] = BBOX) -> bool:
    return (
        bbox["lat_min"] <= node["lat"] <= bbox["lat_max"]
        and bbox["lon_min"] <= node["lon"] <= bbox["lon_max"]
    )


def fetch_nebraska_nodes(bbox: dict[str, float] = BBOX) -> tuple[list[dict[str, Any]], int]:
    """Returns (nebraska_nodes, total_global_count)."""
    raw_nodes = fetch_raw_nodes()
    total = len(raw_nodes)

    result = []
    for raw in raw_nodes:
        node = normalize_node(raw)
        if node and in_bbox(node, bbox):
            result.append(node)

    return result, total


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    public_key       TEXT PRIMARY KEY,
    adv_name         TEXT,
    type_code        INTEGER,
    type_name        TEXT,
    lat              REAL,
    lon              REAL,
    freq_mhz         REAL,
    bandwidth_khz    REAL,
    spreading_factor INTEGER,
    coding_rate      INTEGER,
    source           TEXT,
    last_advert      TEXT,
    api_inserted_date TEXT,
    api_updated_date TEXT,
    first_seen_local TEXT NOT NULL,
    last_seen_local  TEXT NOT NULL,
    is_active        INTEGER NOT NULL DEFAULT 1,
    missing_streak   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS node_activity (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    public_key   TEXT NOT NULL,
    event_type   TEXT NOT NULL,   -- new, name_change, moved, type_change, reactivated, went_inactive
    detail       TEXT,            -- JSON blob describing the change
    occurred_at  TEXT NOT NULL,
    FOREIGN KEY (public_key) REFERENCES nodes (public_key)
);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at              TEXT NOT NULL,
    status              TEXT NOT NULL,   -- ok, error
    total_nodes_global  INTEGER,
    total_nodes_nebraska INTEGER,
    new_count           INTEGER,
    changed_count       INTEGER,
    went_inactive_count INTEGER,
    error_message       TEXT
);

CREATE INDEX IF NOT EXISTS idx_activity_pk ON node_activity (public_key);
CREATE INDEX IF NOT EXISTS idx_activity_time ON node_activity (occurred_at);
"""


def get_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@dataclass
class RunResult:
    total_global: int = 0
    total_nebraska: int = 0
    new_count: int = 0
    changed_count: int = 0
    went_inactive_count: int = 0


def _log_activity(conn: sqlite3.Connection, public_key: str, event_type: str,
                   detail: dict[str, Any], now: str) -> None:
    conn.execute(
        "INSERT INTO node_activity (public_key, event_type, detail, occurred_at) "
        "VALUES (?, ?, ?, ?)",
        (public_key, event_type, json.dumps(detail), now),
    )


def upsert_node(conn: sqlite3.Connection, node: dict[str, Any], now: str) -> str:
    """Insert or update one node; logs activity for meaningful changes.
    Returns 'new', 'changed', or 'unchanged'."""
    existing = conn.execute(
        "SELECT * FROM nodes WHERE public_key = ?", (node["public_key"],)
    ).fetchone()

    if existing is None:
        conn.execute(
            """INSERT INTO nodes (
                public_key, adv_name, type_code, type_name, lat, lon,
                freq_mhz, bandwidth_khz, spreading_factor, coding_rate,
                source, last_advert, api_inserted_date, api_updated_date,
                first_seen_local, last_seen_local, is_active, missing_streak
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0)""",
            (
                node["public_key"], node.get("adv_name"), node.get("type_code"),
                node.get("type_name"), node["lat"], node["lon"],
                node.get("freq_mhz"), node.get("bandwidth_khz"),
                node.get("spreading_factor"), node.get("coding_rate"),
                node.get("source"), node.get("last_advert"),
                node.get("inserted_date"), node.get("updated_date"),
                now, now,
            ),
        )
        _log_activity(conn, node["public_key"], "new", {
            "adv_name": node.get("adv_name"),
            "type_name": node.get("type_name"),
            "lat": node["lat"], "lon": node["lon"],
        }, now)
        return "new"

    changes: dict[str, Any] = {}
    if existing["adv_name"] != node.get("adv_name"):
        changes["adv_name"] = {"old": existing["adv_name"], "new": node.get("adv_name")}
    if existing["type_code"] != node.get("type_code"):
        changes["type_name"] = {"old": existing["type_name"], "new": node.get("type_name")}
    moved = (
        abs((existing["lat"] or 0) - node["lat"]) > COORD_CHANGE_THRESHOLD
        or abs((existing["lon"] or 0) - node["lon"]) > COORD_CHANGE_THRESHOLD
    )
    if moved:
        changes["location"] = {
            "old": [existing["lat"], existing["lon"]],
            "new": [node["lat"], node["lon"]],
        }
    reactivated = not existing["is_active"]
    if reactivated:
        changes["reactivated"] = True

    conn.execute(
        """UPDATE nodes SET
            adv_name=?, type_code=?, type_name=?, lat=?, lon=?,
            freq_mhz=?, bandwidth_khz=?, spreading_factor=?, coding_rate=?,
            source=?, last_advert=?, api_updated_date=?,
            last_seen_local=?, is_active=1, missing_streak=0
           WHERE public_key=?""",
        (
            node.get("adv_name"), node.get("type_code"), node.get("type_name"),
            node["lat"], node["lon"], node.get("freq_mhz"), node.get("bandwidth_khz"),
            node.get("spreading_factor"), node.get("coding_rate"), node.get("source"),
            node.get("last_advert"), node.get("updated_date"),
            now, node["public_key"],
        ),
    )

    if changes:
        event = "reactivated" if reactivated and len(changes) == 1 else "changed"
        _log_activity(conn, node["public_key"], event, changes, now)
        return "changed"
    return "unchanged"


def mark_missing_nodes(conn: sqlite3.Connection, seen_keys: set[str], now: str) -> int:
    """Bump missing_streak for previously-active nodes absent this run;
    flips them inactive once the grace threshold is crossed."""
    went_inactive = 0
    rows = conn.execute(
        "SELECT public_key, missing_streak FROM nodes WHERE is_active = 1"
    ).fetchall()
    for row in rows:
        if row["public_key"] in seen_keys:
            continue
        new_streak = row["missing_streak"] + 1
        if new_streak >= MISSING_RUNS_BEFORE_INACTIVE:
            conn.execute(
                "UPDATE nodes SET is_active=0, missing_streak=? WHERE public_key=?",
                (new_streak, row["public_key"]),
            )
            _log_activity(conn, row["public_key"], "went_inactive", {
                "missing_streak": new_streak,
            }, now)
            went_inactive += 1
        else:
            conn.execute(
                "UPDATE nodes SET missing_streak=? WHERE public_key=?",
                (new_streak, row["public_key"]),
            )
    return went_inactive


def run_once(conn: sqlite3.Connection, bbox: dict[str, float] = BBOX) -> RunResult:
    now = datetime.now(timezone.utc).isoformat()
    result = RunResult()

    try:
        nodes, total_global = fetch_nebraska_nodes(bbox)
    except Exception as exc:  # noqa: BLE001 - want to record any failure
        log.exception("Fetch failed")
        conn.execute(
            "INSERT INTO fetch_runs (run_at, status, error_message) VALUES (?, 'error', ?)",
            (now, str(exc)),
        )
        conn.commit()
        raise

    result.total_global = total_global
    result.total_nebraska = len(nodes)

    seen_keys: set[str] = set()
    for node in nodes:
        seen_keys.add(node["public_key"])
        outcome = upsert_node(conn, node, now)
        if outcome == "new":
            result.new_count += 1
        elif outcome == "changed":
            result.changed_count += 1

    result.went_inactive_count = mark_missing_nodes(conn, seen_keys, now)

    conn.execute(
        """INSERT INTO fetch_runs (
            run_at, status, total_nodes_global, total_nodes_nebraska,
            new_count, changed_count, went_inactive_count
        ) VALUES (?, 'ok', ?, ?, ?, ?, ?)""",
        (now, result.total_global, result.total_nebraska,
         result.new_count, result.changed_count, result.went_inactive_count),
    )
    conn.commit()

    log.info(
        "run ok: %d/%d nodes in Nebraska bbox | %d new, %d changed, %d went inactive",
        result.total_nebraska, result.total_global,
        result.new_count, result.changed_count, result.went_inactive_count,
    )
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> None:
    conn = get_db(args.db)
    if args.loop:
        log.info("Looping every %ds (Ctrl+C to stop)", args.loop)
        while True:
            try:
                run_once(conn)
            except Exception:  # noqa: BLE001 - keep the loop alive on transient errors
                log.error("Run failed, will retry next cycle")
            time.sleep(args.loop)
    else:
        run_once(conn)


def cmd_list(args: argparse.Namespace) -> None:
    conn = get_db(args.db)
    query = "SELECT * FROM nodes"
    if not args.include_inactive:
        query += " WHERE is_active = 1"
    query += " ORDER BY type_name, adv_name"
    rows = conn.execute(query).fetchall()
    for row in rows:
        flag = "" if row["is_active"] else " [inactive]"
        print(f"{row['type_name']:<12} {row['adv_name'] or '(unnamed)':<24} "
              f"{row['lat']:.4f},{row['lon']:.4f}  last seen {row['last_seen_local']}{flag}")
    print(f"\n{len(rows)} node(s)")


def cmd_history(args: argparse.Namespace) -> None:
    conn = get_db(args.db)
    rows = conn.execute(
        "SELECT a.*, n.adv_name FROM node_activity a "
        "LEFT JOIN nodes n ON n.public_key = a.public_key "
        "ORDER BY a.occurred_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    for row in rows:
        name = row["adv_name"] or row["public_key"][:12]
        print(f"{row['occurred_at']}  {row['event_type']:<14} {name}  {row['detail']}")


def cmd_runs(args: argparse.Namespace) -> None:
    conn = get_db(args.db)
    rows = conn.execute(
        "SELECT * FROM fetch_runs ORDER BY run_at DESC LIMIT ?", (args.limit,)
    ).fetchall()
    for row in rows:
        if row["status"] == "ok":
            print(f"{row['run_at']}  ok  {row['total_nodes_nebraska']}/{row['total_nodes_global']} "
                  f"nodes  +{row['new_count']} new  ~{row['changed_count']} changed  "
                  f"-{row['went_inactive_count']} inactive")
        else:
            print(f"{row['run_at']}  ERROR  {row['error_message']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DB_PATH, help=f"SQLite DB path (default: {DB_PATH})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Fetch nodes and update the database")
    p_run.add_argument("--loop", type=int, metavar="SECONDS",
                        help="Keep running, fetching every N seconds (foreground)")
    p_run.set_defaults(func=cmd_run)

    p_list = sub.add_parser("list", help="List tracked nodes")
    p_list.add_argument("--include-inactive", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_hist = sub.add_parser("history", help="Show recent node activity")
    p_hist.add_argument("--limit", type=int, default=50)
    p_hist.set_defaults(func=cmd_history)

    p_runs = sub.add_parser("runs", help="Show recent fetch-run summaries")
    p_runs.add_argument("--limit", type=int, default=20)
    p_runs.set_defaults(func=cmd_runs)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
