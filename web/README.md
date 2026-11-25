# Central dashboard prototype

This is a read-only, same-origin dashboard served by the central FastAPI app.
It uses the repository's existing site registry, telemetry, event, and twin-state
records; no third-party JavaScript, charting service, or external font request is
required.

## Run locally

From the repository root, install the project dependencies into the existing
`venv/` environment and start the dashboard:

```sh
make install
make run-dashboard
```

If you still need to create the environment, run `python3 -m venv venv` first.
The Makefile uses `venv/` by default; override it with `make VENV=.venv ...`
if your environment uses a different path.

Open <http://127.0.0.1:8000/dashboard>. The default database is `vra-twin.db`
in the repository root. Override the database path or local bind address with
`VRA_DB_PATH`, `VRA_API_HOST`, and `VRA_API_PORT`.

Run the full test suite with `make test`; it includes the Modbus/MQTT simulator
integration tests when `pymodbus` and the system `mosquitto` broker are
available. Validate the example site with `make validate`.

The API binds to loopback by default. Do not expose this prototype to a network:
authentication, authorization, TLS termination, and production deployment
hardening are not implemented here.

## Current view

- Site selection, most recent telemetry-ingest time (not heartbeat-derived link
  health), latest readings, registered assets,
  recent events, and the most recent 24-hour sample count.
- PV, battery, load, and power-imbalance cards from the twin state when present,
  with direct latest telemetry as a fallback for the power readings.
- A selectable numeric signal chart over 1-hour, 6-hour, or 24-hour windows.
- Battery state of charge and estimated state of health when those values exist.
- Automatic refresh every 15 seconds. The page never issues commands.

Missing state and telemetry are shown as missing; the UI does not fabricate zero
readings or seed sample values. The configured VLK pilot figures remain
illustrative. Confirm real VRA site details, units, thresholds, and KPIs before
using this for operational decisions.

## Storage boundary

SQLite remains the default local development repository. The backend also has
a PostgreSQL/TimescaleDB repository path, selected with `DATABASE_URL`.

## PostgreSQL / TimescaleDB

The central API can use the PostgreSQL repository when `DATABASE_URL` is set.
It applies the backend migration and enables the TimescaleDB extension at startup;
the database role therefore needs permission to create that extension. For local
development with Docker Compose:

```sh
export POSTGRES_PASSWORD='choose-a-local-development-password'
docker compose -f deploy/compose.yaml up -d
export DATABASE_URL="postgresql://vra:${POSTGRES_PASSWORD}@127.0.0.1:5432/vra_twin"
make run-dashboard
```

Keep the database bound to loopback for local work. For a managed or production
database, configure TLS, access controls, backups, and secret management outside
this prototype. The PostgreSQL integration test runs when `DATABASE_URL` is set;
CI runs it against a TimescaleDB service.
