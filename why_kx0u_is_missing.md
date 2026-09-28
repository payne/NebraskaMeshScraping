# Why KX0U isn't in the Nebraska Mesh database

*Investigated 2026-09-27*

## Question

KX0U's node is named `KX0U` and has its location set to **41.18, -95.96**
(Omaha). We had been exchanging messages with it on the MeshCore air for
about ten minutes, but it didn't show up in `nebraska_mesh.db` or when
running `python nebraska_mesh_tracker.py run`.

## Findings

KX0U is **not** in the database, and it is **not** in the data source the
scraper reads. The scraper isn't dropping it.

### 1. Local database (`nebraska_mesh.db`)

- No node has `KX0U` anywhere in its name.
- No node is within about 1 km of 41.18, -95.96. About 40 other Omaha-area
  nodes are present (W8ING-RPT, KC0YKN_R@47th&Schroeder, KF0TIL Repeater,
  etc.), so the area is being collected fine.
- The latest runs (`fetch_runs` ids 5–7) all succeeded: about 63,970 nodes
  globally, 290 in the Nebraska bounding box.

### 2. Live global API (`https://map.meshcore.dev/api/v1/nodes`)

Using the scraper's own `fetch_raw_nodes()`, all 63,970 raw records were
searched, **before** the Nebraska bounding-box filter and including records
with no location:

- No record contains "kx0u" in any field.
- No record is within 0.01° of 41.18, -95.96.

The bounding box (`39.9–43.1°N`, `-104.15 – -95.2°W`) isn't the cause
either, because -95.96 is inside it.

## Why: how nodes get onto the MeshCore map

The scraper doesn't listen to the radio. It downloads the public MeshCore
web map's database, and a node only appears there if someone **uploads**
it. Talking on the air doesn't add a node to the map.

How the 290 Nebraska nodes got there (the `source` column):

| source   | type        | count |
|----------|-------------|------:|
| uploader | Repeater    |   213 |
| uploader | Room Server |    15 |
| app      | Client      |    34 |
| app      | Repeater    |    25 |
| app      | Room Server |     3 |

- **uploader**: observer stations that automatically upload the adverts
  they hear. In this data they only upload **repeaters and room servers**,
  never clients.
- **app**: the node's owner uploaded it manually from the MeshCore app.
  **All 34 client (companion) nodes came in this way.**

Since KX0U is being used for messaging, it is probably a companion/client
node. Uploaders won't add it to the map, so it will only appear once its
owner uploads it from the app.

## How to get KX0U onto the map

- **If KX0U is a companion/client node:** KX0U uploads the node to the map
  from the MeshCore app, using the add/upload-to-map option; the location
  set on the node is used. The next
  `python nebraska_mesh_tracker.py run` should then pick it up.
- **If KX0U is a repeater:** it will appear once an uploader station hears
  its advert. Sending a flood advert can help.

## Possible enhancement

To track nodes heard on the air that aren't on the public map, the scraper
would need a second data source. One option is to read the contact list
from our own companion radio over USB/BLE with the `meshcore` Python
library and merge those contacts into the database.
