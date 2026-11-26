# Project TITAN — military medical logistics & cold-chain verification

A working prototype that answers one question in the field — **"is this crate safe to use?"** — and keeps a
tamper-evident record of everything that happened to it. It runs end-to-end **without any hardware**: a simulator
plays the part of the sensor box inside each crate.

```
 ┌────────────────────┐   telemetry (HTTP)   ┌──────────────────────────────┐   SQL   ┌──────────────┐
 │ edge_simulator.py  │ ───────────────────▶ │  FastAPI backend  (app/)     │ ──────▶ │  MariaDB     │
 │  GPS route, temp,  │   burst-sync after   │  · rules: lockout, expiry    │         │  titan_db    │
 │  tamper, offline   │   offline buffering  │  · Haversine geofencing      │ ◀────── │  append-only │
 └────────────────────┘                      │  · SHA-256 audit hash chain  │         │  audit trail │
                                             │  · anchor: mock/IPFS/Polygon │         └──────────────┘
 ┌────────────────────┐   REST + same origin │  · serves the web app        │
 │ React PWA          │ ◀──────────────────▶ │  · demo control (simulator)  │
 │ scan · inventory · │                      └──────────────────────────────┘
 │ map · simulator    │
 └────────────────────┘
```
Everything is served from **one port**: the web app at `/`, the API under `/api`, interactive API docs at `/docs`.

## Quick start
```bash
chmod +x setup.sh run.sh
./setup.sh              # installs MariaDB if needed, builds DB + web app, runs all tests, seeds demo data, starts TITAN
```
Open **http://127.0.0.1:8000**. Next time just `./run.sh`.

| Command | Use |
|---|---|
| `./setup.sh --sqlite` | no MariaDB needed (dev only: no append-only triggers / DB grants) |
| `./setup.sh --reset` | wipe `titan_db` and re-seed (needed to replay a crate's whole trip) |
| `./run.sh --https` | **phone camera**: self-signed HTTPS on your network (see below) |
| `./run.sh --simulate thermal` | start the server *and* a simulator run on crate 1 |
| `./run.sh --lan` / `--port N` / `--dev` | LAN HTTP / other port / auto-reload |

Requirements: Python 3.10+, bash. MariaDB is installed by `setup.sh` (apt / dnf / pacman / Homebrew). Node 18+ is only
needed to *rebuild* the web app; a prebuilt copy is bundled in `frontend/dist`.

## A 5-minute demo
1. **Scan tab** → *Start camera* (or type `TITAN-CRATE-0003`). Green **SAFE TO ADMINISTER**, with an "expiring soon" warning.
   Try `TITAN-CRATE-0004` (thermal excursion) and `0005` (expired item): red **COMPROMISED — DO NOT USE**, with the reasons.
   No camera? Open the **Labels** tab on a second screen and scan the QR codes there.
2. **Simulator tab** → pick crate 1 → **▶ Cooling failure**. Watch the log scroll.
3. Switch to **Inventory** (live refresh) and **Map**: crate 1 leaves ALPHA, shows up *IN TRANSIT* with vehicle, driver and live
   coordinates, then flips to *LOCKED OUT* when the temperature passes 8 °C, then arrives at BRAVO.
4. Back on **Scan**, verify crate 1: **COMPROMISED**, with the cold-chain breach spelled out. Tap *Audit trail* to see every event
   and its SHA-256 link; tap *Anchor chain head* to publish the head hash (mock receipt by default).
5. Try **Signal loss + failure**: readings buffer while "offline", then burst-sync; the excursion that happened in the dead zone is caught at sync.

## Using the camera on a phone
Browsers only allow camera access on **https://** or `localhost`. So for a phone:
```bash
./run.sh --https        # prints https://<your-LAN-IP>:8000
```
Open that address on the phone (same Wi-Fi), accept the one-time self-signed-certificate warning, allow the camera.
Then "Add to Home Screen" to install it as an app. If the camera is unavailable the scan screen explains why and offers manual entry.

## The three parts
**Backend (`app/`)** — rules and records
- **Telemetry → state.** Haversine distance to origin/destination bases decides `AT_BASE` / `IN_TRANSIT` / `AT_BATTLEFIELD`; departures and
  arrivals update the dispatch and log `GEOFENCE_TRANSITION`, `DISPATCH_DEPARTED`, `BASE_HANDOFF`.
- **Zero-tolerance lockout.** Any reading outside 2–8 °C, an MKT breach, or an LDR tamper flag locks the crate, quarantines its items and
  logs `LOCKOUT`. Lockout is sticky.
- **Verification.** `/api/crates/{id}/verify` re-derives the answer from raw telemetry, item expiry, the lockout flag, the hash chain and any
  external anchor. Missing telemetry or audit trail = not safe. Each verification is itself logged.
- **Non-repudiation.** One append-only SHA-256 chain per crate (`current_hash = SHA256(previous_hash + canonical_event_json)`), enforced by
  DB triggers and least-privilege grants; pluggable anchoring (`ANCHOR_BACKEND` = mock / ipfs / polygon).
- **Offline-friendly ingest.** Per-device sequence numbers make burst-sync replays idempotent; late readings still count for the cold chain
  but never move the crate backwards.

**Simulator (`edge_simulator.py`)** — scenarios `normal, thermal, freeze, tamper, mkt, offline, offline_thermal`; also real offline
buffering if the API goes away mid-run. Run it from the web app's Simulator tab or the command line (`--help`).

**Web app (`frontend/`)** — React + Vite PWA
- **Scan:** live camera scanning (`getUserMedia` + jsQR), manual entry, huge green/red verdict, vibration + tone cue, recent scans.
- **Inventory:** filters (location, type, base, expiry), transit metadata (vehicle, driver, departure, live position), expiry & lockout badges, auto-refresh.
- **Map:** tile-free schematic of bases, routes and live crate positions (works with no internet).
- **Crate detail:** audit trail with chain status, verify-now, anchor, QR code.
- **Simulator:** launch and watch simulation runs. **Labels:** printable crate QR codes.

**Safe by default.** If the server can't be reached, the app says **UNVERIFIED — DO NOT USE**; it never reuses an old verdict as current.
The service worker caches only the app shell, never API responses. Stale inventory is shown only with a clear "last known data" warning.

## API summary
| Endpoint | Purpose |
|---|---|
| `POST /api/telemetry/ingest`, `/ingest/batch` | single reading / offline-buffer burst sync |
| `GET /api/inventory` | filters `movement_status`, `item_type`, `base_code`, `expiry_status`; transit or storage info per item |
| `GET /api/crates/{id}/verify`, `/api/crates/by-qr/{payload}/verify` | **SAFE TO ADMINISTER** / **COMPROMISED — DO NOT USE** + reasons |
| `GET /api/crates`, `/api/crates/{id}`, `/api/bases` | map, labels, filters |
| `GET /api/crates/{id}/audit`, `POST /api/crates/{id}/anchor` | hash chain / external anchoring |
| `/api/demo/*` | start/stop/poll simulator runs (`TITAN_DEMO=1`) |

Full schema and try-it-out forms: `/docs`.

## Configuration (`.env`, written by `setup.sh`; see `.env.example`)
`DATABASE_URL`, `TEMP_MIN_C` / `TEMP_MAX_C`, `EXPIRING_SOON_DAYS`, `CORS_ORIGINS`, `TITAN_DEMO`, `ANCHOR_BACKEND`, `IPFS_API_URL`,
`IPFS_API_AUTH`, `POLYGON_RPC_URL`, `POLYGON_CHAIN_ID`, `POLYGON_PRIVATE_KEY`. Env overrides for setup: `TITAN_DB_PASSWORD`, `MYSQL_ROOT_PASSWORD`, `PORT`.

## Tests
`pytest -q` → 61 backend + simulator + integration tests · `cd frontend && npm test` → 21 web-app tests (verdict screens, scan flow,
offline behaviour, filters, QR encode→decode round-trip, camera-error handling). `setup.sh` runs both. The backend, simulator, demo control
and web app were also exercised together over real HTTP and HTTPS against MariaDB 10.11.

## Honest limits — read before relying on this
- **Not tested with a physical camera or on a phone.** Scanning is verified by decoding generated QR images with the same library, plus mocked
  camera permission/secure-context errors. Test on your target devices; lighting, glare and damaged labels matter in the field.
- **PWA install/offline shell** is generated and served correctly, but I could not install it on a real device here.
- **Anchoring to IPFS/Polygon is implemented but untested live** (needs credentials; start on the Amoy testnet). The default `mock` backend
  produces clearly-labelled fake receipts, so *external* tamper evidence only exists once you configure a real one.
- **No authentication.** Anyone who can reach the server can ingest telemetry, verify and anchor. Add device API keys and operator login
  before any real use, and set `TITAN_DEMO=0` (the demo API starts processes on the server).
- **Prototype modelling:** one 2–8 °C band for every item type; geofences are circles; no lockout-release, dispensing or dispatch-creation workflow.
- **Prototype, not a medical device.** The verdict is only as good as the sensors and data feeding it.

## Troubleshooting
| Symptom | Fix |
|---|---|
| "Camera needs a secure connection" | use `./run.sh --https` and open the `https://` address |
| Page shows only the API | web app not built: install Node 18+ and re-run `./setup.sh` |
| Setup can't log in to MariaDB | `MYSQL_ROOT_PASSWORD=… ./setup.sh`, or `./setup.sh --sqlite` |
| Simulator crate "jumps back" | the crate already arrived; `./setup.sh --reset` or simulate another crate |
| Port in use | `./run.sh --port 9000` |

## Layout
```
setup.sh  run.sh        one-shot setup / launcher          schema.sql   MariaDB DDL, triggers, grants
app/                    FastAPI backend                    seed.py      demo data via the real ingest path
edge_simulator.py       simulator                          tests/       pytest suite
frontend/               React PWA (src/, dist/ = prebuilt)
```
