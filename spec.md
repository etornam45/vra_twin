# Digital Twin for the Volta Lake Island Mini-Grids
## Consolidated Architecture, Design and Project Plan

**Beneficiary:** Volta River Authority (VRA) · **Type:** MPhil research project with pilot deployment
**Scope:** Pilot for one island, designed to scale to the fleet (≈8 operating → 35 → 50 → ~200 sites)
**Illustrative pilot:** `VLK-ISL-001` (60 kWp PV, 200 kWh LFP battery, 50 kW inverter). All numbers are examples, not VRA data.

---

## Contents
1. Summary, objectives, assumptions
2. System architecture
3. Edge data gateway
4. Gateway web UI, configuration and CLI
5. Component model, validation and schemas
6. Twin engine and analytics
7. Repository structure
8. Project plan
9. Open questions
10. Outcomes and references

---

## 1. Summary, Objectives, Assumptions

A **digital twin** is a live virtual replica of an island mini-grid, fed by SCADA/field data. It lets VRA monitor, diagnose, simulate and optimise remote assets from one place, instead of relying only on a local administrator and caretaker per island.

**Objectives (from the brief)**
- **O1** Data model and virtual replica of a representative island mini-grid (PV, battery, inverter, load, meters).
- **O2** Near-real-time field data integration for state monitoring and what-if simulation (load growth, battery ageing, dispatch).
- **O3** Remote diagnostics, performance benchmarking and predictive O&M scheduling.

**Core dataflow**
```
Uplink   (telemetry, events):  [SCADA] ──► [Data Gateway] ──► [Backend]
Downlink (commands, config):   [SCADA] ◄── [Data Gateway] ◄── [Backend]
```

**Assumptions (confirm in Phase 0)**
- The pilot island has, or can get, a logger/SCADA exposing PV, battery, inverter and meter data (Modbus TCP/RTU, OPC UA or vendor API).
- Some history exists (ideally 6–12 months) or can be collected.
- Access is **read-only first**; control is considered only after safety sign-off.
- Diesel backup is optional and modelled as an extension.

**Key constraints:** poor or intermittent connectivity (GSM/satellite/radio); little local IT capacity; local protection always has priority over remote action; MPhil timeline; many vendors across ~200 sites.

**Requirements (condensed)**

| Area | Requirement |
|---|---|
| Ingest | Telemetry at 1 s–1 min by tag class, with quality flags and original device timestamps |
| Resilience | ≥7 days edge buffering; ≥99 % completeness after outages via replay |
| Latency | < 10 s typical when online |
| State | Live twin state per asset; stale/lost data always flagged |
| Intelligence | Fault detection, KPIs, forecasts, what-if, battery SOH and replacement timing |
| Control | Supervised, gated, audited (later phase) |
| Security | TLS + mutual authentication, least privilege, audit log |
| Scale | Design for 200 sites; onboarding by configuration, not code |
| Fidelity | Defined per model (e.g. PV power MAPE < 10 %, battery SOC error < 5 points), set in calibration |

---

## 2. System Architecture

### 2.1 Principles
1. **Edge-first resilience:** data is persisted at the edge before sending; the island never depends on the link.
2. **At-least-once delivery + idempotent backend** = effectively-once storage.
3. **Contracts first:** every message and config is a versioned model in one place (`contracts/`).
4. **Canonical model:** vendor differences stop at the gateway; nothing above it knows a register address.
5. **Uplink read-only, downlink gated:** two separate paths with separate permissions.
6. **Provenance on every value:** `measured / calculated / estimated / predicted / simulated`, plus quality and timestamp.
7. **Configuration over code:** a new island is a small config folder.
8. **Reproducibility:** every simulation records model versions, parameters, inputs and seed.
9. **Boring technology:** SQLite, MQTT, PostgreSQL/TimescaleDB, FastAPI.

### 2.2 Layers

```
L6 Applications     Web dashboard · fleet view · alerts · reports · what-if workbench · (later) command console
L5 Twin & analytics Component models · state sync · simulation · forecasting · diagnostics · O&M
L4 Backend          API · ingestion · broker · registry · time-series DB · command service · auth/audit
L3 Edge gateway     Adapters · mapping · buffer · forwarder · command handler · local UI
L2 Site SCADA       Logger/SCADA · PLC/RTU · BMS · inverter controllers   (authoritative for local control)
L1 Physical         PV · battery · inverters · meters · loads · weather sensors
```

### 2.3 Deployment and trust boundaries

```
┌──────────────── ISLAND ────────────────┐   unreliable link   ┌──────────── CENTRAL ────────────┐
│ Devices ► SCADA ◄► Edge gateway        │ ◄── MQTT 5 / TLS ──►│ Broker ► ingestion ► Timescale  │
│            (outbound connections only) │      (outbound)     │          twin · api · web       │
└────────────────────────────────────────┘                     │          command-service        │
                                                               └─────────────────────────────────┘
```

| Boundary | Between | Controls |
|---|---|---|
| B1 | Devices ↔ SCADA | Existing site controls, unchanged |
| B2 | SCADA ↔ gateway | Dedicated OT port, firewall, read-only credentials by default |
| B3 | Gateway ↔ backend | Outbound-only mTLS, per-gateway certificate, topic ACLs, signed commands |
| B4 | Backend ↔ users | SSO/OIDC, RBAC, per-site scoping, audit |

### 2.4 Backend services

| Service | Responsibility |
|---|---|
| `ingestion` | Consume, validate, dedupe, store, send **selective acks** |
| `registry` | Sites, assets, topology, tag registry, mapping versions, gateway enrolment |
| `twin-engine` | Live state, staleness, state estimation, model stepping |
| `diagnostics` | Residual rules and anomaly detection |
| `scheduler/workers` | Forecasts, KPIs, calibration, simulations, O&M |
| `api` | REST + WebSocket for UI and external consumers |
| `command-service` | Request → authorise → validate (twin) → sign → publish → track → audit |
| `notifier` | Alerts to engineers and caretakers |

**Pilot simplification:** run these as one FastAPI/worker codebase in 2–3 containers, keeping module boundaries so they can be split later.

### 2.5 Interfaces

**MQTT** (a gateway may publish only under its own site):

| Topic | Dir | QoS | Content |
|---|---|---|---|
| `vra/{site}/telemetry/{gw}` | up | 1 | Columnar telemetry batch |
| `vra/{site}/events/{gw}` | up | 1 | Alarms, state changes, data-gap markers |
| `vra/{site}/status/{gw}` | up | 1, retained | Health/heartbeat (+ Last Will `offline`) |
| `vra/{site}/cmdres/{gw}` | up | 1 | Command results |
| `vra/{site}/ack/{gw}` | down | 1 | Selective ack ranges per stream |
| `vra/{site}/cmd/{gw}` | down | 1 | Signed commands |
| `vra/{site}/config/{gw}` | down | 1, retained | Config update notices |

**REST (indicative):** `/v1/sites`, `/v1/sites/{id}/assets`, `/state`, `/events`, `/kpis`, `/forecasts`, `/health`, `/v1/timeseries?site&asset&tag&from&to&agg`, `POST /v1/simulations`, `POST /v1/commands`, and `WS /v1/stream/sites/{id}`.

### 2.6 Data lifecycle

| Tier | Resolution | Retention (pilot default) |
|---|---|---|
| Edge buffer, acked | Batched raw | 24 h after ack |
| Edge buffer, unsent | Raw, then downsampled under disk pressure | Until acked, within disk budget |
| Raw (Timescale) | As received | 24 months (compressed after 7 d) |
| 1-min / 15-min aggregates | mean/min/max/last | 5 / 10 years |
| Daily KPIs, events, sim runs | Daily / event / run | Indefinite |

- **Gaps are explicit.** Missing data is never silently interpolated; the gateway sends `data_gap` markers. Model-filled values are stored as `substituted`, provenance `estimated`.
- **Late data:** stored by device timestamp; live state is updated only if the sample is newer than current state.

### 2.7 Security and safety (system level)
- Gateway makes **outbound connections only**; SCADA is never exposed to the internet.
- TLS 1.2+/mTLS, per-gateway certificates with rotation and revocation; per-site topic ACLs.
- RBAC (viewer, caretaker, analyst, operator, engineer, admin), per-site scoping, immutable command and access logs.
- Signed container images, staged rollouts with rollback; IEC 62443 principles.
- **Control safety:** local protection and PLC interlocks always win; commands are bounded, expiring, simulated against the twin first, confirmed by read-back, and fully logged; loss of link never changes local operation; protection settings are never remote.

### 2.8 Scaling to ~200 sites

| Concern | Response |
|---|---|
| Onboarding | Site template + equipment catalogue + per-site config; target: days |
| Vendor diversity | Adapter plugins, canonical model, catalogue register maps |
| Remote gateway management | Remote config, health monitoring, staged updates |
| Data volume | Hypertables, continuous aggregates, retention tiers |
| Compute | Stateless ingestion/API; batch analytics on schedule; simulations as queued jobs |
| Verification | Synthetic 200-site load test |

### 2.9 Environments and delivery
`dev` (Compose + SCADA and fleet simulators) → `staging` (integration, chaos, load tests) → `prod-pilot` (one server + island gateway) → `prod-fleet` (Kubernetes, managed Postgres/Timescale). CI: lint, type-check, unit tests, validation of contracts and sites, gateway↔ingestion contract tests, container build. Gateway rollout: signed images, staged (1 → 10 % → all), automatic rollback. Contracts use SemVer; every message carries `schema`; backend accepts versions N and N−1.

### 2.10 Observability
Gateway: queue depth, oldest unsent age, link state, replay rate, disk %, adapter errors, clock drift. Backend: ingest lag, duplicate rate, ack latency, per-site completeness. Twin: model residuals, stale counts, simulation queue. Alerts: site offline > 15 min, oldest unsent > 6 h, disk > 80 %, completeness < 95 %, clock drift > 5 s. Tooling: Prometheus, Loki, Grafana.

### 2.11 Technology stack

| Area | Choice |
|---|---|
| Edge | Linux, Docker, Python; Mosquitto as local MQ; SQLite |
| Protocols | Modbus (pymodbus), OPC UA (asyncua), vendor REST, CSV fallback |
| Messaging | MQTT 5 (EMQX or Mosquitto; Kafka only at larger scale) |
| Backend | Python, FastAPI |
| Storage | TimescaleDB (time series), PostgreSQL (registry), S3-compatible (artefacts) |
| Twin/sim | pvlib, custom battery models, scipy; pandapower/OpenDSS only if network effects matter |
| ML | scikit-learn, XGBoost/LightGBM, statsmodels |
| Frontend | Next.js, Grafana for engineering panels, MapLibre for fleet map |
| Deployment | Docker Compose (pilot) → Kubernetes (fleet) |

**Standards for reference:** IEC 61850, IEC 61968/61970 (CIM), IEC 62541 (OPC UA), IEC 62443 (security).

---

## 3. Edge Data Gateway

**Purpose:** take data from the island SCADA, survive a bad or absent internet link, and relay everything to the backend with no loss and no duplicates in storage. Also carry validated commands back.

**Goals:** no data loss across outages up to the disk budget; automatic replay; live data first; survive power cuts and crashes; low bandwidth; explicit gaps; vendor-neutral; safe downlink; self-observing and remotely manageable.
**Non-goals:** replacing SCADA or protection; running heavy analytics on the island.

### 3.1 Process architecture

```
 SCADA/devices ─► adapters ─► local MQ (Mosquitto, localhost) ─► recorder/batcher ─► SQLite (WAL)
                    ▲                                                                 │ outbox, tag_state,
                    │ local/write                                                     │ commands, alarms
              command handler ◄───────────── forwarder (link state, publish, replay, acks) ◄┘
                                                    │ MQTT 5 / TLS (outbound only)
              health agent · config service · local API/UI                              ▼
                                                                                  Cloud broker
```

| Piece | Reason |
|---|---|
| Adapters separate from core | A crashing vendor driver cannot take down the buffer; adapters can be any language |
| Local MQ | Decouples polling from persistence; clean seam for the command write path |
| **SQLite is the source of truth** | The MQ is a transport only; nothing is safe until committed to SQLite |
| Forwarder reads only SQLite | The send path never depends on adapter health; replay is the normal path |

### 3.2 Key decisions

| # | Decision | Trade-off |
|---|---|---|
| D1 | Outbox pattern in SQLite (WAL) | Single writer (fine at this load) |
| D2 | Store **batches**, not individual readings (~10× less disk) | No raw local queries without decoding |
| D3 | Local Mosquitto as the edge MQ (alternative: in-process `asyncio.Queue`; `bus/` is swappable) | One extra process |
| D4 | At-least-once + idempotent backend | Backend must dedupe (cheap primary key) |
| D5 | Sequence numbers allocated **in the same transaction** as the batch | No gaps/reuse after a crash |
| D6 | Application-level **selective ack** from backend (PUBACK only proves the broker has it) | Extra topic and logic |
| D7 | Live-first, backfill throttled | Backfill arrives out of order (backend handles) |
| D8 | Downsample, then drop, under disk pressure, with explicit gap markers | Some resolution loss in extreme outages |
| D9 | Gateway re-validates every command locally | Limits live in config (one source) |
| D10 | MQTT 5, persistent session, fixed client ID | Broker must support v5 |

### 3.3 Components

- **Adapters:** `modbus_tcp`, `modbus_rtu`, `opcua`, `vendor_rest`, `csv_tail`. Poll groups by tag class; device timestamp if available (`ts_source`); per-device timeout and retry; `comms_lost` events; values marked `bad/stale` rather than omitted; one process per adapter type with watchdog.
- **Mapping/normalisation:** register → value (type, scale, offset); unit conversion; **sign normalisation** to canonical convention; range check (out of range → `bad`); optional stuck-value detection (→ `uncertain`).
- **Local MQ:** Mosquitto on `127.0.0.1`, persistence on, QoS 1. Topics: `local/raw/{adapter}/{asset}/{tag}`, `local/event/{adapter}`, `local/write/...`, `local/writeresult/...`. Raw message: `{"a":"BESS_01","t":"soc","v":78.2,"q":0,"ts":1731319200123,"src":"device"}`.
- **Recorder/batcher:** per-tag **deadband** with periodic refresh (`max_interval_s`) so flat signals are cheap but provably alive; windowing (10 s live, 60 s on poor links; early flush on alarms); **columnar** encoding (time deltas, values, quality per series); **zstd** compression; in one SQLite transaction allocate `seq`, insert batch, update `tag_state`, commit, *then* ack the MQ message.
- **Health agent:** retained heartbeat every 60 s and on change; MQTT Last Will `offline`.
- **Config agent:** see section 4.4.
- **Timekeeper:** tracks NTP offset; if unsynced, batches carry `clock_quality="unsynced"` and the offset so the backend can correct or flag.

### 3.4 SQLite design

Pragmas: `journal_mode=WAL`, `synchronous=NORMAL` (use `FULL` if there is no protected power), `foreign_keys=ON`, `busy_timeout=5000`, `auto_vacuum=INCREMENTAL`. A power cut can lose the last few transactions but never corrupts the DB; power the gateway from a protected DC supply.

```sql
CREATE TABLE seq_counter (stream TEXT PRIMARY KEY, next_seq INTEGER NOT NULL);  -- telemetry|events|cmdres|health

CREATE TABLE outbox (
  stream TEXT NOT NULL, seq INTEGER NOT NULL,
  priority INTEGER NOT NULL DEFAULT 5,            -- 1 highest (cmdres, alarms) … 9 lowest (backfill)
  topic TEXT NOT NULL, payload BLOB NOT NULL, enc TEXT NOT NULL DEFAULT 'zstd+json', bytes INTEGER NOT NULL,
  n_samples INTEGER, t_first_ms INTEGER, t_last_ms INTEGER,
  state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (state IN ('PENDING','INFLIGHT','ACKED','DOWNSAMPLED','DROPPED')),
  attempts INTEGER NOT NULL DEFAULT 0, created_ms INTEGER NOT NULL,
  last_attempt_ms INTEGER, acked_ms INTEGER, lineage TEXT,
  PRIMARY KEY (stream, seq)
) WITHOUT ROWID;
CREATE INDEX ix_outbox_send ON outbox (state, priority, stream, seq) WHERE state IN ('PENDING','INFLIGHT');

CREATE TABLE tag_state (asset_id TEXT, tag TEXT, value_num REAL, value_text TEXT, quality INTEGER,
                        t_ms INTEGER, last_sent_ms INTEGER, PRIMARY KEY (asset_id, tag));
CREATE TABLE alarm_state (alarm_key TEXT PRIMARY KEY, asset_id TEXT, code TEXT, severity TEXT,
                          raised_ms INTEGER, cleared_ms INTEGER, event_id TEXT);
CREATE TABLE commands (cmd_id TEXT PRIMARY KEY, received_ms INTEGER, issued_ms INTEGER, expires_ms INTEGER,
                       asset_id TEXT, type TEXT, params TEXT,
                       state TEXT CHECK (state IN ('RECEIVED','REJECTED','EXPIRED','APPLYING','APPLIED','FAILED','CONFIRMED')),
                       reason TEXT, readback TEXT, finished_ms INTEGER);
CREATE TABLE data_gaps (gap_id TEXT PRIMARY KEY, stream TEXT, t_from_ms INTEGER, t_to_ms INTEGER,
                        cause TEXT, reported INTEGER DEFAULT 0);   -- disk_pressure|adapter_down|gateway_down|clock
```

**Maintenance:** delete acked rows every 5 min in chunks (keep 24 h); incremental vacuum hourly if > 10 % free; daily `quick_check` and on boot after unclean shutdown (on failure: move DB aside, start fresh, emit `gateway_db_corrupt`).

**Sizing (illustrative):** ~150 tags (30 @ 10 s, 60 @ 60 s, 60 @ 5 min/on-change) ≈ 363 k samples/day ≈ 18 MB raw JSON → **1–2 MB/day** columnar + zstd. 7 days ≈ 10–15 MB. Configure a disk budget (e.g. 2 GB) rather than a day count.

### 3.5 Batch format

```json
{
  "schema": "telemetry.batch/1.0", "site_id": "VLK-ISL-001", "gateway_id": "GW-VLK-001",
  "stream": "telemetry", "seq": 128734, "t0_ms": 1731319200000,
  "clock_quality": "synced", "clock_offset_ms": 12, "cfg_version": "2026-11-05.3", "resolution": "raw",
  "series": [
    {"asset": "BESS_01", "tag": "soc",  "u": "%",  "t": [0, 10000], "v": [78.0, 78.1],   "q": [0, 0]},
    {"asset": "BESS_01", "tag": "p_dc", "u": "kW", "t": [0, 10000], "v": [-40.0, -39.2], "q": [0, 0]},
    {"asset": "PV_INV_01","tag": "p_ac","u": "kW", "t": [0, 10000], "v": [42.1, 43.0],   "q": [0, 0]}
  ]
}
```
`t` = ms offsets from `t0_ms`; `q` codes: 0 good, 1 uncertain, 2 bad, 3 stale. Compressed with zstd; MQTT 5 user properties carry `enc`, `schema`, `seq`, `stream`. Target ≤ 32 KB compressed (≤ 8 KB on very poor links). Batch identity: `{gateway_id}:{stream}:{seq}`. Downsampled batches use `resolution: agg_1m|agg_5m` with `agg.min/max/n` arrays.

### 3.6 Delivery protocol (the core of link tolerance)

**Guarantees:** at-least-once to the backend; idempotent backend (PK `(gateway_id, stream, seq)` for batches, `(site, asset, tag, ts)` for samples); a batch is `ACKED` only when the backend confirms it **committed to the database**.

```
PENDING ──publish──► INFLIGHT ──ack range──► ACKED ──retention──► deleted
   ▲                    │ timeout / disconnect → back to PENDING
   └── under disk pressure ──► DOWNSAMPLED / DROPPED (always with gap or lineage record)
```

**Ack (backend → gateway):**
```json
{"schema":"ack/1.0","site_id":"VLK-ISL-001","gateway_id":"GW-VLK-001","stream":"telemetry",
 "ranges":[[128700,128734],[128740,128740]],
 "rejected":[{"seq":128736,"reason":"schema","detail":"series[3].t length != v length"}],
 "committed_at":"2026-11-11T10:00:20Z"}
```
Selective ranges (not one cumulative number) because live and backfill batches interleave.

**Send scheduler priorities:** (1) `cmdres` and `events`; (2) `health`; (3) **live telemetry** (batches younger than `live_window_s`, default 120 s); (4) **backfill**, oldest first, rate-limited (`backfill_max_kBps`, share of window ≤ 50 %) so live data keeps flowing.

**Rules**
- Inflight window `W` (default 5; 1–2 on very poor links); adaptive: halve on timeout, +1 after successes.
- Ack timeout default 30 s (adaptive to RTT); on timeout `INFLIGHT → PENDING`, per-batch exponential backoff with jitter (cap 5 min).
- On reconnect, **all `INFLIGHT` → `PENDING`**; duplicates are harmless.
- No cross-batch ordering guarantee; the backend orders by sample time.
- **Poison batches:** after `max_attempts` with explicit rejects, quarantine, raise an event, continue. Never block the queue.

```python
async def forwarder_loop(db, mqtt, cfg, link):
    window = AdaptiveWindow(cfg.window_max)
    while True:
        await link.wait_online()
        db.requeue_stale_inflight(cfg.ack_timeout_s)
        while window.available() and link.online:
            batch = db.next_to_send(cfg.live_window_s, backfill_budget=limiter.allow())
            if batch is None: break
            db.mark_inflight(batch.stream, batch.seq)
            try:
                await mqtt.publish(batch.topic, batch.payload, qos=1, properties=batch.mqtt_props())
                window.on_sent(batch)
            except (MqttError, TimeoutError):
                db.mark_pending(batch.stream, batch.seq); window.on_timeout(); link.report_failure(); break
        await asyncio.sleep(cfg.loop_interval_s)

async def on_ack(msg):
    ack = Ack.model_validate_json(msg.payload)
    db.mark_acked(ack.stream, ack.ranges, now_ms())      # one transaction
    db.quarantine(ack.stream, ack.rejected)
    window.on_ack(ack)
```

### 3.7 Link state machine

| State | Behaviour |
|---|---|
| `CONNECTING` | TLS + MQTT connect; subscribe `ack/cmd/config`; publish retained `online` |
| `ONLINE` | Normal flow, backlog ≈ 0 |
| `BACKFILLING` | Backlog > threshold; live-first plus throttled replay |
| `DEGRADED` | High loss/RTT: smaller window, larger batching window, skip low-priority backfill |
| `OFFLINE` | Keep recording; reconnect with exponential backoff (1 s → 5 min, ±30 % jitter) |

MQTT settings: fixed `client_id = gateway_id`; `clean_start=false`; session expiry 7 days; keepalive 30–60 s; `receive_maximum` = window; Last Will = offline.

### 3.8 Disk pressure and gaps

| Disk use (of budget) | Action |
|---|---|
| < 70 % | Normal |
| 70–85 % | Downsample oldest pending telemetry to 1-min aggregates; record lineage |
| 85–95 % | Downsample to 5-min; stop retaining acked rows; event `gateway_disk_pressure` |
| > 95 % | Drop oldest pending telemetry with exact `data_gaps` ranges |

**Never dropped:** alarms/events, command results, gap markers. Other gap causes also produce markers: `adapter_down`, `gateway_down` (boot id change), `clock_jump`.

### 3.9 Backend obligations (ingestion contract)
Validate batch envelope and array lengths (vectorised, not per-sample objects); dedupe with `ON CONFLICT DO NOTHING`; **commit before ack**; reject with a reason so the gateway can quarantine; store by device time; never let older samples overwrite live state; treat `data_gap` events as authoritative for completeness KPIs; correct or flag unsynced clocks; acks are safe to repeat; request resend if a sequence is missing for too long.

### 3.10 Downlink: commands
1. Receive on `vra/{site}/cmd/{gw}`.
2. **Idempotency:** insert `cmd_id`; if it exists, return the stored result without re-executing.
3. Verify **signature** (backend public key pinned in config).
4. Check **expiry** (reject if the gateway clock is unsynced).
5. **Whitelist:** command type and asset must be allowed in config.
6. **Local limits:** value range, rate limit, mode preconditions, fresh interlock state, SOC/temperature windows.
7. Apply via `local/write/...`, **read back and verify**, mark `CONFIRMED` or `FAILED`.
8. Publish `cmdres` at priority 1 through the outbox.

Safety: `commands.enabled: false` by default; setpoints may carry `revert_after_s` (watchdog); protection settings never exposed.
**Command classes:** A advisory (no write) · B bounded setpoints · C mode changes (two-person approval) · D switching/critical (out of pilot scope) · X protection (never remote).

```json
{"schema":"command/1.0","cmd_id":"0192f4a1-…","site_id":"VLK-ISL-001","gateway_id":"GW-VLK-001",
 "asset_id":"PCS_01","type":"set_soc_reserve","class":"B","params":{"min_soc_pct":25},
 "reason":"Forecast low irradiance next 48 h","simulation_ref":"sim_20261111_0007",
 "requested_by":"user:eng_ama","issued_at":"2026-11-11T10:05:00Z","expires_at":"2026-11-11T10:15:00Z",
 "revert_after_s":172800,"signature":"MEUCIQ…","key_id":"cmd-key-2026-10"}
```
Result: `{"schema":"command.result/1.0","cmd_id":…,"state":"CONFIRMED","applied_at":…,"readback":{…}}`; rejection reasons: `bad_signature, expired, not_allowed, out_of_range, interlock, rate_limit, clock_unsynced, commands_disabled`. Backend lifecycle: `REQUESTED → AUTHORISED → VALIDATED → SENT → RECEIVED → APPLIED → CONFIRMED` (failures: `DENIED, REJECTED, EXPIRED, FAILED, TIMED_OUT`).

### 3.11 Time handling
Prefer device timestamps; chrony/NTP when online; battery-backed RTC if possible; unsynced start flagged and corrected later; detect jumps > 2 s (`clock_jump` event, split batches); monotonic clock for intervals, wall clock only for sample time.

### 3.12 Failure modes

| Failure | Response | Data impact |
|---|---|---|
| Link down | OFFLINE, keep recording, backoff reconnect | None |
| Flapping / high loss | DEGRADED: shrink window and batch size | None; slower |
| Broker or backend DB down/slow | INFLIGHT timeout, retry with backoff | None |
| Ack lost | Re-send; backend dedupes and re-acks | None |
| Gateway crash / power cut | Resume from SQLite; WAL recovery; `quick_check` | Gap during downtime only (marked) |
| SQLite corruption | Quarantine DB, new DB, event | Unsent data lost, explicitly reported |
| Disk full | Downsample, then drop oldest with markers | Reduced resolution or marked gaps |
| SCADA unreachable | `comms_lost` event; `bad/stale` values; retry | Source gap, flagged |
| Adapter / local MQ crash | Restart; persistent subscriptions | Seconds at most |
| Clock wrong | Flag unsynced; correct later; refuse commands | Time quality flagged |
| Duplicate / late command | Stored result / `EXPIRED` | None |
| Poison batch | Quarantine and continue | One batch isolated |
| Bad config pushed | Roll back to previous version | None |

### 3.13 Gateway health message (heartbeat)
```json
{"schema":"gateway.health/1.0","gateway_id":"GW-VLK-001","ts":"2026-11-11T10:00:00Z","boot_id":"b1a6c2d0",
 "uptime_s":864000,"version":"0.4.2","cfg_version":"2026-11-05.3",
 "link":{"state":"BACKFILLING","rtt_ms":840,"disconnects_24h":7},
 "queue":{"pending":412,"inflight":5,"oldest_unsent_age_s":5400,"bytes_pending":981233},
 "store":{"db_mb":14.2,"disk_used_pct":3.1,"gaps_open":0},
 "adapters":[{"name":"scada_modbus","state":"ok","error_rate_5m":0.0}],
 "clock":{"quality":"synced","offset_ms":12},"host":{"cpu_pct":7,"mem_pct":31,"temp_c":48,"supply_v":12.2}}
```

### 3.14 Hardware
Fanless industrial PC (4 GB RAM) or Raspberry Pi 4/5 class on SSD/NVMe (**avoid SD cards for the DB**); 9–36 V DC input from protected supply; dual-SIM 4G or satellite, Ethernet to SCADA, optional RS-485; Debian/Ubuntu LTS minimal with Docker; hardware watchdog; IP-rated enclosure with surge protection.

### 3.15 Test plan

| Test | Pass criterion |
|---|---|
| Clean 1 h run | 100 % completeness, p95 latency < 10 s |
| **6 h blackout** | All batches acked, zero duplicate samples, live data resumes < 15 s after restore |
| 7-day simulated outage | Disk within budget, no loss, backfill completes |
| Flapping link (20 s up / 40 s down, 2 h) | No stuck INFLIGHT, no duplicates |
| 30 % loss, 2 s RTT | DEGRADED engages; eventual full delivery |
| `kill -9` ×100 and power cut ×50 | DB intact, no sequence gaps/reuse, gap marker for lost window |
| Broker restart / ingestion down 1 h | Reconnect, dedupe, recover |
| Disk pressure | Downsample then drop; events never dropped |
| Clock skew | Flags raised; commands refused if unsynced |
| Command replay / expired command | Executed once / refused |
| Poison batch, adapter crash | Quarantined / restarted with `comms_lost` event |
| 200 simulated gateways | Broker and ingestion hold SLOs |

**Invariants asserted automatically:** (1) no holes in sequence numbers except declared gaps; (2) backend sample count = gateway-emitted count − declared gaps; (3) no command executed twice or after expiry; (4) oldest-unsent age returns to ≈ 0 after recovery.

### 3.16 Gateway build order
1. SCADA simulator + network-chaos scripts (1 wk) → 2. SQLite store/outbox API (1 wk) → 3. Recorder: mapping, deadband, encoder (1–2 wk) → 4. Forwarder: MQTT 5, window, acks, replay, link state (2 wk) → 5. Backend ingestion stub with dedupe and ack (1 wk) → 6. Chaos tests green (1–2 wk) → 7. Config service, local API, CLI (1.5 wk) → 8. UI + wizard (2–3 wk) → 9. Health/config agents, packaging, provisioning (1–2 wk) → 10. Command handler, disabled by default (2 wk) → 11. Hardware bench and power-cut tests (2+ wk).
**First milestone: steps 1–6 working end-to-end against the simulator before buying hardware.**

---

## 4. Gateway Web UI, Configuration and CLI

**Purpose:** let a technician install and troubleshoot on site with a laptop or phone, while SSH and file-based configuration remain fully supported.

### 4.1 Design

| Decision | Choice |
|---|---|
| Stack | FastAPI + Jinja2 + HTMX; no Node build; all assets bundled (works offline) |
| Process | Separate `ui` process from the core, so a UI crash or attack cannot stop forwarding |
| Data access | UI/API reads SQLite **read-only**; config changes go through the core's `configsvc` |
| One API | UI, CLI and central backend share the same local REST API |
| Network | Binds to the **LAN/service interface only**, never WAN/4G; self-signed HTTPS; reachable via mDNS (`gw-<id>.local`) or fixed service IP; optional Wi-Fi AP |
| Footprint | < 100 MB RAM |

### 4.2 Config source of truth and change pipeline
**The config files on disk (`/etc/vra/`) are the single source of truth.** UI edits, SSH edits and central pushes all produce the same files and pass the same pipeline, so what the UI shows always matches what an admin sees over SSH.

```
edit (UI / file / central push)
 → 1 validate (same validator as `vra-validate`)       fail → show errors, nothing changes
 → 2 stage as a new version
 → 3 dry-run checks (adapters open, registers readable, broker reachable)
 → 4 apply atomically (symlink swap, reload only affected services)
 → 5 health-check window (~60 s)
 → 6 commit, or automatic rollback + event
```
Last 20 versions kept with who/what/when; "revert to version" available. If a central push arrives during a local edit, the draft is never silently overwritten; `config.managed_by: local | central` (local during installation, central after commissioning).

### 4.3 Pages

| Page | Content |
|---|---|
| Setup wizard | See 4.4 |
| Dashboard | Link state, queue depth, oldest unsent, disk %, clock drift, adapter status, last ack, version |
| Live values | All tags: value, unit, quality, age; filter and sparkline |
| Devices | Catalogue picker, IP/unit ID, **Test connection**, per-device errors |
| Mapping | Table editor, CSV import/export, **Read now** per point, scale/sign hints, save as catalogue entry |
| Uplink | Broker, profile, enrolment, certificate expiry, **Send test batch**, last ack |
| Network / Time | Interfaces, 4G APN/signal, ping to broker; NTP offset, manual time set, timezone |
| Events & logs | Local alarms, comms events, config history, filtered service logs |
| Commands | **Read-only** status of downlink and recent commands |
| Config / System | Raw YAML with validation, version history/revert; reboot, restart, update status, **support bundle** (redacted) |

Roles: `viewer`, `installer`, `admin`.

### 4.4 Installer wizard (target < 30 minutes)
1. Log in with the **per-device installer password** (on the label) and change it.
2. **Enrol** with a one-time token from the backend, or offline: the UI shows a CSR/request code (QR + text), the office returns a signed certificate bundle. **The private key is generated on the device and never leaves it.**
3. **Network:** service/SCADA subnet, WAN or 4G/satellite; broker reachable.
4. **Devices:** pick from catalogue, enter IP/unit IDs, test connection.
5. **Mapping check:** live read of every point against expected ranges; flags suspicious scale/sign.
6. **Guided sign test:** "charge the battery now" → `BESS_01.p_dc` must go negative; PV running → `PV_INV_01.p_ac` positive.
7. **Time:** NTP sync or manual; drift shown.
8. **Uplink test:** test batch, end-to-end ack with latency.
9. **Outage test (optional, 5 min):** unplug WAN, reconnect, watch backlog drain.
10. **Finish:** generate and upload a **commissioning report** (JSON + printable).

### 4.5 Local REST API
`GET /api/v1/status`, `/tags`, `/queue`, `/events`, `/devices`; `POST /devices/{id}/test`; `POST /mapping/read` (read only); `GET|PUT /config`, `POST /config/validate`, `GET /config/versions`, `POST /config/revert`; `POST /enrol`; `POST /uplink/test`; `GET /support-bundle`; `POST /system/{reboot|restart}`; `POST /commissioning/run`.

### 4.6 UI security

| Risk | Control |
|---|---|
| Internet exposure | LAN/service interface only; firewall denies WAN; no port forwarding |
| Default passwords | Unique per-device installer password, forced change |
| Brute force / web attacks | Argon2, rate limit and lockout, CSRF tokens, strict CSP, short sessions, input validated with the same models |
| Writing to equipment from UI | **Off by default**; only guided commissioning tests when `ui.allow_commissioning_writes: true`, in an expiring (30 min) commissioning mode, installer role, confirmation, audit |
| Enabling remote control | **Not possible from the UI.** `commands.enabled` changes only by signed central config or an admin's SSH edit |
| Secrets | Never shown or exported; support bundle redacts them |
| Remote support | No inbound access; optional time-limited, logged reverse tunnel, off by default |

### 4.7 SSH and CLI (always available)
Key-only SSH for named admins. `gw status | queue | tags [--asset X] | devices test | config validate | config apply | config versions | config revert <v> | enrol --token T | uplink test | replay --dry-run | logs --follow | support-bundle`. Editing `/etc/vra/*.yaml` then `gw config apply` is equivalent to using the UI.

---

## 5. Component Model, Validation and Schemas

### 5.1 Principles and conventions
1. Separate **identity, configuration, measurements, state and model parameters**.
2. **Canonical tags and units:** kW, kvar, kWh, V, A, Hz, °C, %, W/m²; conversion at the gateway.
3. **Sign conventions (fixed):** sources (PV, genset) generation +; **storage and bidirectional inverter: discharge/export +, charge/import −**; meters: import from network +; loads: consumption +. Vendor inversion is declared in the mapping (`sign: invert`).
4. Every value carries **quality** (`good/uncertain/bad/stale/substituted`), **timestamp**, **provenance**.
5. **Topology is explicit** (buses and connections) so energy balance and power flow can be computed.
6. Everything versioned (`schema` field, asset revisions, model-parameter versions).
7. **Same model for live, replay and simulation.**

**IDs:** site `VLK-ISL-NNN`; gateway `GW-VLK-NNN`; assets `BESS_01, PV_ARR_01, PV_INV_01, PCS_01, MTR_PCC, FDR_01, BRK_01, WX_01, LOAD_RES`; buses `BUS_AC_MAIN`; global tag `{site}.{asset}.{tag}`; events/commands UUIDv7.

**Reference topology**
```
PV_ARR_01 ─► PV_INV_01 ───────────┐
BESS_01 ◄─► PCS_01 ──────────► BUS_AC_MAIN ─► MTR_PCC ─► BRK_n ─► FDR_n ─► LOAD_n
```

### 5.2 Source of truth
**Pydantic models in `contracts/` are the single source of truth**; JSON Schema is exported automatically (for docs, UI forms and TypeScript types). One language, real cross-field validators, the same validator in gateway UI, CLI, CI and backend.

### 5.3 Asset types and key configuration

Every asset has: `asset_id`, `asset_type`, `name`, `identity` (manufacturer, model, serial, firmware, dates, location), `config`, `connections` (bus/parent/children), `tags`, `model` (`model_id`, `version`, `params_ref`), `revision`.

| Type | Key config | Key tags |
|---|---|---|
| `pv_array` | `capacity_kwp`, modules, strings, tilt, azimuth, temp coeff (−0.35 %/°C), NOCT, annual degradation, baseline soiling | `poa_irradiance` (via weather), `p_dc_est` |
| `pv_inverter` | `rated_ac_kw`, MPPT count, max DC V, efficiency curve, curtailment support, ramp rate | `p_ac, q_ac, v_ac_*, f_ac, p_dc, v_dc_mppt*, t_internal, e_day, e_total, status, alarm_code` |
| `bess` | chemistry, rated/usable kWh, max charge/discharge kW, `soc_min/max_pct`, temp limits, racks/modules/cells, nominal RTE, warranty (years, min SOH, throughput, cycles at DoD), reference capacity test | `soc, soh, p_dc, v_dc, i_dc, t_max, t_min, v_cell_delta, status, limit_charge_kw, limit_discharge_kw, e_charged_total, e_discharged_total, alarm_code` |
| `pcs` | rated kVA/kW, grid-forming, efficiency curve, droop, modes, remote setpoints | `p_ac, q_ac, v_ac_*, f_ac, p_dc, mode, status, alarm_code` |
| `meter` | role (`pcc/generation/feeder/customer`), CT ratio, accuracy class, direction convention | `p, q, pf, v_l1..l3, i_l1..l3, f, e_active_import, e_active_export` |
| `feeder` / `breaker` | rated current, length, conductor, R/X per km, customers / type, protection, remote-operable | `p, q, v, i, loading_pct` / `state, trip_count` |
| `load_group` | category, customers, connected kW, metering, profile ref, growth (annual %, new connections), priority, deferrable kW | `p_est, e_day` |
| `weather_station` | height, sensors, cleaning interval | `ghi, poa, t_amb, t_module, rh, wind_speed, wind_dir, rain` |
| `genset` (optional) | rated kW/kVA, tank, min load, fuel curve, remote start | `p_ac, f_ac, fuel_pct, t_coolant, hours_total, status` |
| `gateway` | gateway_id, uplink type, adapters, disk budget | `link_state, queue_pending, oldest_unsent_age_s, disk_used_pct, clock_offset_ms` |

**Tag rules:** each tag has unit, class (`fast/standard/slow/event`), optional min/max, `is_counter`, `writable`. Energy tags are monotonic counters; ingestion computes deltas and flags resets (never treated as negative energy). Status tags use enums (e.g. `off, standby, running, fault, derated`).

**Example asset (battery)**
```json
{"asset_id":"BESS_01","asset_type":"bess","name":"Island battery",
 "identity":{"manufacturer":"ExampleBatt","model":"EB-LFP-200","commissioned_on":"2023-06-15"},
 "config":{"chemistry":"LFP","rated_energy_kwh":200,"usable_energy_kwh":180,"max_charge_kw":50,"max_discharge_kw":50,
   "soc_min_pct":10,"soc_max_pct":95,"temp_limits_c":{"charge_min":5,"charge_max":45,"discharge_min":-10,"discharge_max":55},
   "warranty":{"years":10,"min_soh_pct":70,"cycles_at_dod":{"cycles":6000,"dod_pct":80,"soh_pct":70}},
   "reference_capacity_test":{"date":"2026-03-10","capacity_kwh":188.4,"temp_c":27,"c_rate":0.25}},
 "connections":{"bus_id":"BUS_DC_BESS","parent":"PCS_01"},
 "model":{"model_id":"battery.ecm_ageing","version":"1.0","params_ref":"bess_01"},"revision":2}
```

### 5.4 Validation (four layers)

| Layer | Checks | Runs where |
|---|---|---|
| 1 Structure | Types, required fields, enums, units | Everywhere |
| 2 Semantics (within an asset) | `soc_min < soc_max`; `usable ≤ rated`; efficiency curve sensible | Everywhere |
| 3 Site integrity (across files) | Topology connected, one grid-forming source, every referenced asset exists, every expected tag mapped or **waived**, writable only where allowed, sign test declared, energy counters monotonic | CLI, CI, gateway UI, backend import |
| 4 Runtime data | Batch envelope valid, equal array lengths, range, quality, counters | Gateway (per reading), backend (per batch, vectorised) |

`vra-validate sites/VLK-ISL-001` prints errors (block deploy) and warnings (need waiver or decision) with file, line and suggested fix, e.g. `✖ BESS_01.config soc_min_pct (30) must be less than soc_max_pct (25) → site.yaml line 14`. Runs in: pre-commit, CI, gateway CLI/UI before any apply, on central config push (rejected and old config kept if invalid), backend registry import, backend ingest.

```python
class BessConfig(BaseModel):
    chemistry: str
    rated_energy_kwh: float = Field(gt=0)
    usable_energy_kwh: float = Field(gt=0)
    max_charge_kw: float = Field(gt=0)
    max_discharge_kw: float = Field(gt=0)
    soc_min_pct: float = Field(10, ge=0, le=50)
    soc_max_pct: float = Field(95, ge=50, le=100)

    @model_validator(mode="after")
    def _check(self):
        if self.soc_min_pct >= self.soc_max_pct:
            raise ValueError(f"soc_min_pct ({self.soc_min_pct}) must be less than soc_max_pct ({self.soc_max_pct})")
        if self.usable_energy_kwh > self.rated_energy_kwh:
            raise ValueError("usable_energy_kwh cannot exceed rated_energy_kwh")
        return self
```

### 5.5 Simple configuration: catalogue + inference + defaults

Per island there are **two files plus an optional CSV**. Nameplate data and register maps come from a shared **equipment catalogue**; topology is inferred from references.

**`site.yaml`** (the whole island)
```yaml
site: VLK-ISL-001
name: Pilot Island
timezone: Africa/Accra
location: [7.80, -0.05]
gateway: GW-VLK-001
assets:
  PV_ARR_01: { type: pv_array, kwp: 60.5, tilt: 10, azimuth: 180, inverter: PV_INV_01 }
  PV_INV_01: { type: pv_inverter, catalog: exampleinv/EI-50K }
  BESS_01:   { type: bess, catalog: examplebatt/EB-LFP-200, pcs: PCS_01 }
  PCS_01:    { type: pcs, catalog: exampleinv/EI-GF50 }
  MTR_PCC:   { type: meter, role: pcc, catalog: examplemeter/EM-3P }
  WX_01:     { type: weather_station, waive: [rain, wind_dir] }
  FDR_01:    { type: feeder, customers: 140, meter: MTR_FDR_01 }
  LOAD_RES:  { type: load_group, category: residential, customers: 140, feeder: FDR_01 }
```
**`gateway.yaml`**
```yaml
gateway: GW-VLK-001
uplink: { broker: mqtt.example-vra.org, profile: slow_link }   # normal | slow_link | metered
devices:
  - { asset: PV_INV_01, protocol: modbus_tcp, host: 10.10.1.21, unit_id: 1 }
  - { asset: BESS_01,   protocol: modbus_tcp, host: 10.10.1.22, unit_id: 1 }
  - { asset: PCS_01,    protocol: modbus_tcp, host: 10.10.1.23, unit_id: 1 }
  - { asset: MTR_PCC,   protocol: modbus_tcp, host: 10.10.1.24, unit_id: 2 }
commands: { enabled: false }
```
Uplink **profiles** set batching window, inflight window, backfill rate and deadbands; advanced overrides (store budget, pressure thresholds, ack timeout, backoff, command allow-list) are optional, with defaults: flush 10 s, 32 KB batches, zstd 3, budget 2 GB, 24 h post-ack retention, pressure 70/85/95 %, rate limit 6 commands/min, read-back timeout 15 s.

**`mapping.csv`** (optional, opens in Excel)
```
asset,tag,register,function,type,scale,unit,sign,class,min,max,writable
WX_01,poa,30201,input,uint16,1,W/m2,as_is,standard,0,1500,false
BESS_01,p_dc,40014,holding,int16,0.1,kW,invert,standard,-60,60,false
```
**Catalogue entry** (`sites/_catalog/equipment/examplebatt/EB-LFP-200.yaml`): type, manufacturer, model, `config` (nameplate/limits), `registers:` (CSV of the same columns as above), `expected_tags`. The loader fills nameplate and limits from it, infers buses and connections, applies defaults, and expands to the full model stored in the backend registry. The gateway UI can **save a commissioned mapping as a new catalogue entry**, so each new equipment type is entered once for the whole fleet.

### 5.6 Other message and entity schemas (key fields)

**Event** (`event/1.0`): `event_id`, `site_id`, `gateway_id`, `stream`, `seq`, `kind` (`alarm_raise|alarm_clear|state_change|comms_lost|comms_restored|data_gap|gateway|config_applied|command`), `asset_id`, `code`, `source_code` (vendor), `severity` (`info..critical`), `message`, `ts`, `cleared_ts`, `context` (snapshot of values at raise).
```json
{"kind":"alarm_raise","asset_id":"BESS_01","code":"BESS_OVERTEMP","source_code":"E047","severity":"high",
 "ts":"2026-11-11T13:42:10Z","context":{"t_max":47.8,"p_dc":-38.0,"soc":91.2,"t_amb":36.1}}
{"kind":"data_gap","code":"GAP_DISK_PRESSURE","context":{"stream":"telemetry","from":"2026-11-04T00:00:00Z","to":"2026-11-04T06:30:00Z","cause":"disk_pressure"}}
```
**Alarm catalogue (starter):** `BESS_OVERTEMP, BESS_LOW_SOC, BESS_CELL_IMBALANCE, BESS_COMMS_LOST, PV_INV_FAULT, PV_UNDERPERF` (twin-generated)`, PCS_FAULT, GRID_FREQ_OOR, GRID_VOLT_OOR, FDR_BREAKER_TRIP, FDR_OVERLOAD, GW_LINK_DOWN, GW_QUEUE_AGE, GW_DISK_HIGH, GW_CLOCK_DRIFT, SCADA_COMMS_LOST, DATA_STALE`.

**Twin state** (`twin.state/1.0`): per asset `status` (`ok|warning|fault|offline|unknown`), `freshness_s`, `confidence` (`verified|unverified|estimated`), `values` (measurements), `derived` (calculated/estimated), `residuals`, `active_alarms`; plus `energy_balance` (pv, battery, genset, load, losses, imbalance, renewable fraction) and `mode` (`live|replay|forecast|what_if|advisory`).
```json
{"site_id":"VLK-ISL-001","asof":"2026-11-11T10:00:20Z","mode":"live","link_state":"online",
 "assets":{"BESS_01":{"status":"ok","freshness_s":4.2,"confidence":"verified",
   "values":{"soc":{"v":78.1,"u":"%","t":"…","q":"good","p":"measured"}},
   "derived":{"soc_model":{"v":77.6,"u":"%","q":"good","p":"calculated"}},"residuals":{"soc_error_pct":0.5}},
  "MTR_PCC":{"status":"unknown","freshness_s":640,"confidence":"unverified","active_alarms":["DATA_STALE"]}},
 "energy_balance":{"pv_kw":43.0,"bess_kw":-39.2,"load_kw":52.3,"losses_kw":1.9,"imbalance_kw":-2.2}}
```

**Model parameters** (`model_params.yaml`, produced by calibration, versioned, with `fit_quality`):
- `pv.pvlib_sd`: capacity, gamma, NOCT, baseline performance ratio, soiling, mismatch, clipping, degradation.
- `battery.ecm_ageing`: current capacity (from reference test), OCV–SOC curve, R0, RC pairs, charge/discharge efficiency, thermal model, **ageing** (calendar: √time with Arrhenius and SOC stress; cycle: power law in DoD with C-rate and temperature stress), EOL SOH 70 %, SOH estimator (capacity/throughput EKF).
- `inverter.eff_curve`: efficiency points, standby loss.
- `load.profile_growth`: 48-point day profiles (weekday/weekend), base peak kW, seasonality (harmattan/rainy), noise, growth (annual %, new connections, kW per connection).

**Scenario** (`scenario/1.0`): `scenario_id`, `site_id`, `type` (`load_growth|battery_ageing|dispatch_strategy|sizing|component_outage|weather_stress|replacement_timing|composite`), `horizon`, `time_step_s`, `baseline` (site revision, params version, weather `tmy|measured_replay|forecast|synthetic_stress`, load source), `changes` (load, pv, bess incl. `replace_at`, outages, weather), `dispatch` (strategy `rule_soc_window|load_following|peak_shaving|mpc|genset_assist` + params), `monte_carlo` (runs, seed), `outputs`.
**Result** (`sim.result/1.0`): `run` (engine version, **model versions**, seed, `inputs_hash`), `kpis` (specific yield, renewable fraction, unserved energy and loss-of-load hours by year, equivalent cycles, curtailment), `soh_trajectory` (mean/p10/p90), `replacement_window` (earliest/median/latest, driver), `artefacts` (parquet in object store).
```json
{"scenario_id":"sim_20261111_0007","type":"composite","time_step_s":900,
 "horizon":{"start":"2026-11-12T00:00:00Z","end":"2031-11-12T00:00:00Z"},
 "changes":{"load":{"annual_growth_pct":8,"added_connections":60},"bess":{"replace_at":"2030-06-01"}},
 "dispatch":{"strategy":"rule_soc_window","params":{"soc_min_pct":20,"soc_max_pct":95}},
 "monte_carlo":{"runs":50,"seed":42},"outputs":["kpis","soh_trajectory","unserved_energy","replacement_window"]}
```

**Forecast** (`forecast/1.0`): `target`, `kind`, `issued_at`, `resolution_s`, `horizon_s`, model id/version/features, `p50/p10/p90` arrays aligned to `t0 + i·resolution`, `skill_vs_persistence`.
**KPI** (`kpi/1.0`): site, period, `method_version`, `completeness_pct`, kpis (PV energy, specific yield, performance ratio, energy delivered, peak demand, load factor, battery energy in/out, round-trip efficiency, equivalent cycles, availability, unserved energy, curtailed PV, alarm count). **Generation, battery throughput and delivered energy are reported separately to avoid double counting.**
**Health** (`health/1.0`): per asset indices (SOH with CI, resistance growth, cell-imbalance trend, thermal stress, PV PR trend, soiling index) and `rul` (median/p10/p90 date to threshold).
**O&M recommendation / work order:** type, priority, evidence (e.g. PR trend −2.8 %/30 d, 21 days since rain), suggested-by date, estimated gain → work order with assignee, due date, checklist, outcome.

### 5.7 Database (PostgreSQL + TimescaleDB)

| Group | Tables |
|---|---|
| Registry | `sites`, `assets` (+ `asset_revisions`), `connections`, `tag_registry`, `gateways`, `mapping_versions` |
| Ingestion | `ingest_batches (gateway_id, stream, seq, received_at, n_samples, t_first, t_last, …)` PK `(gateway_id, stream, seq)`; `data_gaps` |
| Time series | `telemetry` hypertable |
| Events/control | `events`, `commands`, `command_audit` (append-only) |
| Twin/analytics | `twin_state`, `model_params`, `sim_runs`, `forecasts`, `kpis`, `health_indices`, `om_recommendations`, `work_orders` |
| Access | `users`, `role_grants (user, role, site NULL = all)` |

```sql
CREATE TABLE telemetry (
  ts TIMESTAMPTZ NOT NULL, site_id TEXT NOT NULL, asset_id TEXT NOT NULL, tag TEXT NOT NULL,
  value_num DOUBLE PRECISION, value_txt TEXT, quality SMALLINT NOT NULL DEFAULT 0,
  resolution TEXT NOT NULL DEFAULT 'raw', v_min DOUBLE PRECISION, v_max DOUBLE PRECISION, n INT,
  recv_ts TIMESTAMPTZ NOT NULL DEFAULT now(), batch_seq BIGINT,
  PRIMARY KEY (site_id, asset_id, tag, ts, resolution));
SELECT create_hypertable('telemetry','ts', chunk_time_interval => INTERVAL '7 days');
ALTER TABLE telemetry SET (timescaledb.compress, timescaledb.compress_segmentby = 'site_id,asset_id,tag');
SELECT add_compression_policy('telemetry', INTERVAL '7 days');
-- continuous aggregates telemetry_1m / telemetry_15m (avg, min, max, last, count, min quality)
```

### 5.8 Commissioning checklist (data side)
Tag coverage report; unit/scale sanity against nameplate; **sign test** (charge/discharge, PV on/off); clock drift; **outage-replay test** (pull the SIM for 1 h); 24 h energy balance at the PCC within ±3 %.

---

## 6. Twin Engine and Analytics

### 6.1 Modes and structure

| Mode | Input | Purpose |
|---|---|---|
| Live | Telemetry | Current state, residuals, alarms |
| Replay | Historical window | Validation and calibration |
| Forecast | Weather + load forecast | Next 24–72 h |
| What-if | Scenario | Planning, sizing, ageing |
| Advisory | Live + forecast + optimiser | Suggested set-points shown to caretaker (no write) |

Engine parts: site graph (from topology), per-asset state, runner (event-driven updates + scheduled stepping), model plugin registry.

```python
class AssetModel(Protocol):
    model_id: str; version: str
    def params_schema(self) -> dict: ...
    def init_state(self, asset, params) -> dict: ...
    def step(self, state: dict, inputs: dict, dt_s: float) -> tuple[dict, dict]: ...   # (new_state, outputs)
    def estimate(self, state: dict, measurements: dict) -> dict: ...                  # correction from measurements
```
**Simulation runner:** builds the site graph, instantiates calibrated models, steps time (e.g. 60 s–15 min), applies dispatch logic, records outputs and KPIs. Each run is a queued job with persisted inputs, model versions and seed.

### 6.2 Behavioural models (baseline → advanced)

| Component | Model |
|---|---|
| PV array | Irradiance + cell-temperature with performance ratio → single-diode/pvlib calibrated to site; soiling and degradation |
| Battery | Energy-balance SOC with efficiency and limits → equivalent-circuit with temperature and rate effects; **calendar + cycle ageing**; SOH from capacity/throughput trends |
| Inverter/PCS | Efficiency curve, clipping, limits, mode logic |
| Load | Class profiles, seasonality, growth, stochastic variability |
| Dispatch | Rule-based emulating real site logic → optimisation/MPC |
| Network | Simple bus balance first; pandapower/OpenDSS only if network effects matter |

Balance per step (battery discharge +): `P_pv + P_batt + P_genset = P_load + P_losses + P_curtailed`; `SOC(t+dt) = SOC(t) − P_batt·dt/E_rated × (1/η_dis if discharging, η_chg if charging)`.

### 6.3 State synchronisation
Each measurement stores value, quality, timestamp, received-at. **Staleness rules** per tag class (e.g. fast: 3 missed reports; standard: 3 missed polls; gateway heartbeat: 5 min → "site offline", twin enters degraded mode). Stale values are never shown as live. Light **state estimation** reconciles redundant measurements (e.g. sum of sources vs load meter), fills short gaps with labelled model estimates, and a periodic **calibration loop** updates PV PR, battery efficiency/capacity and load profiles. Live state updates only with newer samples; backfill goes to history.

### 6.4 What-if scenarios

| Scenario | Outputs |
|---|---|
| Load growth | Unserved energy, battery cycling, curtailment, SOC margin |
| Battery ageing | SOH trajectory, end-of-life date, replacement timing |
| Dispatch strategy | Autonomy hours, battery-life impact, energy served |
| Sizing (PV kWp, BESS kWh/kW) | Reliability, cost indicators, upgrade timing |
| Component outage / low-irradiance spell | Resilience impact |

### 6.5 Diagnostics (O3)

| Fault | Detection |
|---|---|
| PV underperformance, soiling, shading | Measured vs modelled power; PR trend; time-of-day signature |
| Inverter faults/derating | Alarm codes, efficiency drop, unexpected clipping |
| Battery anomalies | Temperature spread, cell imbalance, capacity drift, resistance growth |
| Metering/data faults | Energy-balance mismatch, frozen or out-of-range values |
| Comms/gateway faults | Heartbeat loss, buffer growth, gap detection |

Start with **rule- and residual-based** methods (transparent, auditable); add ML anomaly detection once labelled history exists (cf. Jain et al., 2020).

### 6.6 KPIs and benchmarking
PV specific yield and performance ratio; energy availability and loss-of-load/unserved energy; battery round-trip efficiency, equivalent cycles, depth of discharge, SOH and fade rate; curtailment ratio; load factor and peak demand; communication completeness; mean time to detect/repair. Sites are compared on **normalised KPIs** (per kWp, per kWh, per customer) within peer groups by size and configuration.

### 6.7 Predictive O&M and replacement planning
Per-asset health indices (battery SOH, inverter derating events, PV PR trend, soiling index) projected under forecast usage give remaining useful life with uncertainty bands. Outputs: O&M suggestions (cleaning, inspection, firmware check, capacity test, replacement window) and, later, visit grouping by island and boat route.

### 6.8 Forecasting
PV: clear-sky index + NWP irradiance + local measurements; persistence baseline → gradient boosting. Load: profile-based with calendar and weather features, per site. Evaluate with MAE/MAPE/RMSE and **skill against persistence**.

---

## 7. Repository Structure

Monorepo, eight working folders; one Python package per deployable; single-file modules until one passes ~400 lines.

```
vra-island-twin/
├── README.md, Makefile                 # make up | test | validate | build
├── contracts/                          # shared language (SOURCE OF TRUTH)
│   ├── src/vra_contracts/              # common.py assets.py messages.py config.py twin.py validate.py catalog.py
│   ├── jsonschema/                     # auto-exported
│   ├── examples/                       # valid/ and invalid/ samples used as tests
│   └── tests/
├── gateway/                            # runs on the island
│   ├── Dockerfile, compose.yml, mosquitto.conf
│   ├── src/vra_gateway/
│   │   ├── main.py  collect.py  store.py  forward.py  commands.py  health.py
│   │   ├── configsvc.py  enrol.py  cli.py
│   │   ├── adapters/                   # modbus_tcp, modbus_rtu, opcua, csv_tail
│   │   ├── api/                        # local REST + auth
│   │   └── ui/                         # templates/, static/ (HTMX, bundled)
│   └── tests/                          # unit, integration, chaos, ui (Playwright)
├── backend/
│   ├── src/vra_backend/                # ingest.py registry.py commands.py auth.py api/ jobs/
│   ├── migrations/                     # SQL
│   └── tests/
├── twin/
│   ├── src/vra_twin/                   # state.py sim.py diagnostics.py forecast.py calibrate.py om.py models/{pv,battery,inverter,load,dispatch}.py
│   ├── notebooks/
│   └── tests/
├── web/                                # Next.js dashboard
├── sites/
│   ├── _catalog/{equipment,registers}/ # reusable equipment + register maps
│   ├── _templates/solar_bess_basic/
│   └── VLK-ISL-001/                    # site.yaml, gateway.yaml, mapping.csv (optional)
├── tools/                              # scada_sim/, fleet_sim/, netem/, scripts/
├── deploy/                             # docker, compose, k8s, grafana, prometheus, CI helpers
├── tests/                              # end-to-end only
└── docs/                               # this document, ADRs, runbooks, research notes
```

**Conventions:** Python 3.12 (TypeScript for web); ruff + mypy (strict on contracts and gateway) + pytest; trunk-based development; conventional commits; contract changes bump version and changelog; any change under `sites/` triggers validation in CI; no secrets in the repo; one ADR per significant decision.

**Dissertation ownership:** A (platform) leads `contracts/`, `gateway/`, `backend/`, `web/`, `deploy/`, `tools/`; B (intelligence) leads `twin/models`, `forecast`, `diagnostics`, `calibrate`, `om`, `notebooks`; `twin/state`, `sim` and `sites/` are shared.

**Initial ADRs:** SQLite WAL outbox · MQTT 5 with local Mosquitto at the edge · selective acks in addition to PUBACK · columnar zstd batches · TimescaleDB + PostgreSQL · **Pydantic as contract source (JSON Schema exported)** · signed expiring commands re-validated at the gateway · monorepo · discharge-positive sign convention · *open:* hosting (VRA data centre vs cloud).

---

## 8. Project Plan

### 8.1 Phases (indicative 24 months, aligned to MPhil)

| Phase | Months | Focus | Key deliverables |
|---|---|---|---|
| 0 Discovery | 1–3 | Stakeholders, site survey, data audit, literature review, requirements | Requirements spec, pilot island chosen, data inventory, risk register, data-sharing agreement |
| 1 Architecture & data model | 3–6 | Detailed design, component model, catalogue, tag dictionary | Architecture v1, canonical model, catalogue entries for pilot equipment |
| 2 Edge & data pipeline | 5–9 | Gateway core, broker, ingestion, DB, replay | Working uplink from pilot (or replayed data), data-quality dashboard |
| 3 Twin core & monitoring | 8–13 | Live state twin, baseline models, dashboard, alarms, gateway UI | Live pilot twin, site dashboard, KPI engine |
| 4 Calibration & validation | 11–16 | Calibrate PV/battery/load; fidelity validation; diagnostics | Validation report, calibrated models, fault-detection rules |
| 5 Simulation, forecasting, O&M | 14–20 | What-if workbench, ageing, forecasting, benchmarking, O&M | What-if tool, SOH and replacement projections, benchmark reports |
| 6 Supervised control (gated, optional) | 18–22 | Command service, gateway write path, advisory then bounded control | Command path tested in simulation, advisory mode live, VRA safety sign-off |
| 7 Fleet scale & handover | 20–24 | Multi-site onboarding, load test, docs, training | Replicable framework, onboarding playbook, scalability report, dissertations |

```
Month   1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24
P0      ██ ██ ██
P1            ░░ ██ ██ ██
P2                  ░░ ██ ██ ██ ██
P3                           ██ ██ ██ ██ ██ ██
P4                                    ░░ ██ ██ ██ ██ ██
P5                                             ░░ ██ ██ ██ ██ ██ ██
P6                                                      ░░ ██ ██ ██ ██
P7                                                            ░░ ██ ██ ██ ██
```

### 8.2 Milestones and gates

| ID | Month | Milestone | Gate criterion |
|---|---|---|---|
| M1 | 3 | Pilot island and data access confirmed | Data agreement signed; ≥3 months data or logging plan |
| M2 | 6 | Architecture and data model approved | Review by supervisors and VRA engineering |
| M3 | 9 | First end-to-end uplink | Pilot data flows SCADA → gateway → backend; outage test passed |
| M4 | 13 | Live twin v1 | Dashboard with staleness handling; baseline models running |
| M5 | 16 | Validation complete | Model error within agreed targets on held-out data |
| M6 | 20 | What-if and O&M tools delivered | Reproducible scenarios; replacement-timing method backtested |
| M7 | 22 | **Control decision gate** | Proceed to bounded control only if safety review passes; otherwise remain advisory |
| M8 | 24 | Handover | Documentation, playbook, dissertations submitted |

### 8.3 Work packages
WP1 requirements and site survey · WP2 data audit/logging · WP3 data model, catalogue and registry · WP4 edge gateway (core, config service, UI) · WP5 backend platform · WP6 twin models · WP7 monitoring and diagnostics · WP8 simulation and forecasting · WP9 O&M and benchmarking · WP10 control and safety · WP11 scale-out and handover · WP12 research and dissertation.

### 8.4 Dissertation split (two students)
- **A, Data and twin platform:** data model/catalogue, gateway and pipeline, state synchronisation and data quality, dashboard, scalability and security.
- **B, Intelligence and decision support:** battery ageing and SOH, dispatch/what-if optimisation, forecasting and fault diagnosis, predictive O&M and replacement timing, benchmarking.
- Shared: site template, validation dataset, evaluation protocol. With one student, prioritise Phases 0–5 and leave Phase 6 as documented future work.

### 8.5 Team and roles
MPhil student(s) (design, implementation, research) · supervisor(s) · VRA sponsor (access, priorities, approvals) · VRA SCADA/mini-grid engineer (site systems, change approval, safety review) · island administrator and caretaker (field context, validating alerts, usability) · IT/network support · equipment vendors (register maps, firmware).

### 8.6 Validation plan

| What | Metric |
|---|---|
| Data pipeline | Completeness, duplicate rate, latency under outage/fault injection |
| PV model | MAPE / nRMSE of power and energy on hold-out period |
| Battery model | SOC error, capacity-estimate error vs capacity tests |
| Load model and forecasts | MAPE, peak error; skill vs persistence |
| Fault detection | Precision, recall, detection delay (historical incidents + injected faults) |
| Replacement guidance | SOH projection error, end-of-life date range (backtest) |
| Usability | Task success and feedback from VRA engineers and caretakers |
| Scalability | Throughput, latency, cost per site at 200 simulated sites |

### 8.7 Risk register

| Risk | L | I | Mitigation |
|---|---|---|---|
| No usable SCADA data/access at pilot | M | H | Early survey; independent logger/meter; vendor-cloud/historical exports; data simulator |
| Poor connectivity | H | M | Store-and-forward, batching, lower rates, edge pre-aggregation |
| Closed/undocumented vendor protocols | M | H | Request register maps in Phase 0; prefer Modbus/OPC UA; cloud API or file fallback |
| Insufficient battery data for ageing | H | M | Literature-based ageing models calibrated with field capacity tests |
| Safety/cyber concerns block control | M | M | Gated approach; advisory-only remains a valid outcome |
| Scope too large for MPhil | H | H | MoSCoW: core = Phases 0–5; control and fleet features optional |
| Stakeholder availability | M | M | Named contacts, regular cadence, remote check-ins |
| Hardware delays or island damage (power/surge/environment) | M | M | Order early; bench emulator; surge protection, protected DC supply, rugged enclosure |
| Customer-data privacy | L | M | Aggregate/anonymise; data-sharing agreement |
| Model drift as site changes | M | L | Scheduled recalibration; site change log |
| Time sync errors | M | M | NTP/GPS, drift monitoring, device timestamps retained |
| **Local UI becomes an attack surface; config drift between UI and files** | M | M | Controls in 4.6; files on disk are the source of truth |

### 8.8 Plan items to price in Phase 0
Gateway hardware (industrial PC, adapters, 4G/satellite modem, surge protection, enclosure); extra meters or sensors if missing; hosting, domain and certificates; weather/irradiance data service; boat travel and accommodation; open-source software preferred.

### 8.9 Governance
Fortnightly supervisor meeting; monthly VRA steering update; ADRs for decisions; everything in version control; gate reviews at M2, M5, M7, M8; documentation and runbooks kept with the code.

### 8.10 Pilot island selection

| Criterion | Preference |
|---|---|
| Configuration | Typical VRA design (PV + battery + inverter + loads + meters) |
| Data | Existing SCADA/logger with months of history |
| Connectivity | Some usable link |
| Access | Reasonable travel; cooperative administrator and caretaker |
| Documentation | Single-line diagram and datasheets available |
| Representativeness | Load size and growth typical of pipeline sites |
| Permission | VRA agrees to gateway installation |

---

## 9. Open Questions (resolve in Phase 0)
1. Which SCADA/logger and inverter/BMS vendors and protocols are on the operating islands?
2. What data exists (resolution, duration, quality) and who owns it?
3. What connectivity is available on candidate islands, at what cost?
4. Is remote write access technically and contractually permitted, and who signs off safety? Are any commissioning-time writes allowed, or should signs be verified by observation only?
5. Which battery chemistry and warranty terms apply?
6. Hosting: VRA infrastructure or cloud; data-residency rules?
7. VRA's priority KPIs and reporting formats, including World Bank programme reporting; is a printed/PDF commissioning report required?
8. Customer metering type (prepaid, smart, manual) and which customer data may be used?
9. Gateway hardware: second Ethernet port or Wi-Fi so a technician can connect without touching the SCADA network?
10. Who issues gateway enrolment tokens, and does VRA need a portal page for it?
11. Should island administrators/caretakers get a read-only view, and in which languages?

---

## 10. Outcomes and References

| Brief outcome | Delivered by |
|---|---|
| Pilot platform for one island, scalable to the fleet | Phases 2–3, 7; catalogue + site template; adapter pattern |
| Simulation-based guidance on sizing, dispatch and battery-replacement timing | Phase 5; what-if workbench, ageing model |
| MPhil dissertation(s), replicable framework/dashboard | WP12, Phase 7; playbook and open dashboard |
| Lower supervision cost, earlier fault detection | Remote monitoring, diagnostics, alarms |
| Better replacement planning, decision tool for ~200 sites | Predictive O&M, benchmarking, scalable design |

**References**
1. Grieves, M. & Vickers, J. (2017). *Digital Twin: Mitigating Unpredictable, Undesirable Emergent Behavior in Complex Systems.* In Transdisciplinary Perspectives on Complex Systems. Springer.
2. Tao, F. et al. (2019). *Digital Twin in Industry: State-of-the-Art.* IEEE Trans. Industrial Informatics, 15(4).
3. Jain, P. et al. (2020). *A Digital Twin Approach for Fault Diagnosis in Distributed Photovoltaic Systems.* IEEE Trans. Power Electronics, 35(1).
4. IRENA (2020). *Innovation Landscape Brief: Artificial Intelligence and Big Data.* Abu Dhabi.
5. ESMAP / World Bank (2019). *Mini Grids for Half a Billion People.* Washington, DC.

*Suggested additions:* battery-ageing literature, pvlib documentation, IEC 62443 and 61850/CIM references, published mini-grid O&M and remote-monitoring case studies.