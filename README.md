# Nebraska Mesh Node Tracker

Pulls node metadata for the Nebraska Mesh network and keeps a SQLite
database of nodes plus a change/activity log, on a schedule.

## Important: about the data source

`nebraskamesh.net` is built on **MeshCore**, not a custom backend. As of
Sep 2026 the site's own "Open the Map" / "Open Analyzer" buttons point at
placeholder (`example.com`) URLs — it doesn't (yet) expose its own
node-list API. Its live map is really an embed of the **global** MeshCore
network map, which is powered by:

```
https://map.meshcore.dev/api/v1/nodes
```

This tool calls that same public API and filters the results to a
Nebraska bounding box (`~39.9–43.1°N, ~-104.15–-95.2°W`, padded a bit past
the state line). That means:

- You'll get every MeshCore node that *reports a location* inside that
  box — this should be effectively "Nebraska Mesh's nodes," but a
  stray node just across a state line, or a Nebraska node with no/wrong
  GPS fix, can show up or be missed.
- If Nebraska Mesh later stands up its own API (their site looks early /
  templated), point `MESH_API_URL` at it instead — the `run` command's
  bbox filter still applies fine on top of a smaller dataset.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
# one-time fetch + DB update
python nebraska_mesh_tracker.py run

# list currently-active tracked nodes
python nebraska_mesh_tracker.py list

# recent changes (new nodes, renames, moves, went-inactive, etc.)
python nebraska_mesh_tracker.py history --limit 50

# recent fetch-run summaries (counts, errors)
python nebraska_mesh_tracker.py runs
```

By default the DB lives at `./nebraska_mesh.db` (override with `--db` or
the `MESH_DB_PATH` env var).

## Web pages

`index.html` (all Nebraska nodes) and `ham.html` (nodes named with a ham
callsign) have no data built in. Each time one of them loads, it downloads
`nebraska_mesh.db` and queries it in the browser with
[sql.js](https://sql.js.org/). The "Data scraped …" line comes from the
latest successful row in `fetch_runs`.

So nothing needs to regenerate the HTML. To update the pages, run the
tracker and publish the new `nebraska_mesh.db` next to them (e.g. commit
and push it to GitHub Pages).

Serve the pages over HTTP (`python -m http.server`), not `file://`,
because browsers block `fetch()` of local files.

## Scheduling

### Option A — cron (simplest)

```cron
# every 15 minutes
*/15 * * * * cd /path/to/nebraska-mesh-tracker && .venv/bin/python nebraska_mesh_tracker.py run >> run.log 2>&1
```

### Option B — systemd timer (recommended for a homelab box)

`/etc/systemd/system/nebraska-mesh.service`:
```ini
[Unit]
Description=Nebraska Mesh node tracker

[Service]
Type=oneshot
WorkingDirectory=/path/to/nebraska-mesh-tracker
ExecStart=/path/to/nebraska-mesh-tracker/.venv/bin/python nebraska_mesh_tracker.py run
```

`/etc/systemd/system/nebraska-mesh.timer`:
```ini
[Unit]
Description=Run Nebraska Mesh node tracker every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nebraska-mesh.timer
```

### Option C — built-in loop (quick and dirty)

```bash
python nebraska_mesh_tracker.py run --loop 3600   # every hour, foreground
```
Fine for testing; for anything long-running, prefer A or B so a crash
doesn't just stop tracking silently.

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `MESH_API_URL` | `https://map.meshcore.dev/api/v1/nodes` | Source API |
| `MESH_DB_PATH` | `./nebraska_mesh.db` | SQLite file location |
| `MESH_LAT_MIN` / `MESH_LAT_MAX` | `39.9` / `43.1` | Bounding box |
| `MESH_LON_MIN` / `MESH_LON_MAX` | `-104.15` / `-95.2` | Bounding box |
| `MESH_COORD_THRESHOLD` | `0.0005` (~55m) | Min movement to log as "moved" |
| `MESH_MISSING_GRACE` | `1` | Consecutive missed runs before marking inactive |
| `MESH_HTTP_TIMEOUT` | `30` | HTTP timeout (seconds) |
| `MESH_LOG_LEVEL` | `INFO` | Python logging level |

## Database schema

- **nodes** — current state per node (`public_key` is the primary key),
  including `is_active` / `missing_streak` for nodes that have stopped
  reporting.
- **node_activity** — append-only log: `new`, `changed`, `reactivated`,
  `went_inactive`, with a JSON `detail` blob describing exactly what
  changed.
- **fetch_runs** — one row per run: counts and any error, so you can spot
  a broken schedule or a flaky API day.
