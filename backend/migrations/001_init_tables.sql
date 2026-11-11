-- Schema Migration 001: Initial Core Digital Twin Tables
-- Aligned with Section 5.7 and PostgreSQL / TimescaleDB architecture

-- 1. Sites and Asset Registry
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Africa/Accra',
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    gateway_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assets (
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    name TEXT NOT NULL,
    manufacturer TEXT,
    model TEXT,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    connections JSONB NOT NULL DEFAULT '{}'::jsonb,
    revision INT NOT NULL DEFAULT 1,
    PRIMARY KEY (site_id, asset_id)
);

CREATE TABLE IF NOT EXISTS gateways (
    gateway_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
    version TEXT,
    cfg_version TEXT,
    last_seen_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'offline',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. Ingestion & Batch Idempotency Tracking
CREATE TABLE IF NOT EXISTS ingest_batches (
    gateway_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    seq BIGINT NOT NULL,
    site_id TEXT NOT NULL,
    n_samples INT NOT NULL,
    t_first_ms BIGINT NOT NULL,
    t_last_ms BIGINT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (gateway_id, stream, seq)
);

CREATE TABLE IF NOT EXISTS data_gaps (
    gap_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    gateway_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    t_from_ms BIGINT NOT NULL,
    t_to_ms BIGINT NOT NULL,
    cause TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 3. Telemetry Time Series Hypertable
CREATE TABLE IF NOT EXISTS telemetry (
    ts TIMESTAMPTZ NOT NULL,
    site_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    value_num DOUBLE PRECISION,
    value_txt TEXT,
    quality SMALLINT NOT NULL DEFAULT 0,
    resolution TEXT NOT NULL DEFAULT 'raw',
    v_min DOUBLE PRECISION,
    v_max DOUBLE PRECISION,
    n INT,
    recv_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    batch_seq BIGINT,
    PRIMARY KEY (site_id, asset_id, tag, ts, resolution)
);

-- TimescaleDB hypertable setup (will execute conditionally if timescaledb is installed)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable('telemetry', 'ts', if_not_exists => TRUE, chunk_time_interval => INTERVAL '7 days');
        ALTER TABLE telemetry SET (timescaledb.compress, timescaledb.compress_segmentby = 'site_id,asset_id,tag');
        PERFORM add_compression_policy('telemetry', INTERVAL '7 days', if_not_exists => TRUE);
    END IF;
END $$;

-- 4. Events, Alarms and Commands
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    gateway_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    seq BIGINT NOT NULL,
    kind TEXT NOT NULL,
    asset_id TEXT,
    code TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    cleared_ts TIMESTAMPTZ,
    context JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS commands (
    cmd_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    gateway_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    type TEXT NOT NULL,
    class TEXT NOT NULL,
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL DEFAULT 'REQUESTED',
    readback JSONB,
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS command_audit (
    audit_id SERIAL PRIMARY KEY,
    cmd_id TEXT NOT NULL REFERENCES commands(cmd_id),
    state TEXT NOT NULL,
    details TEXT,
    logged_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5. Digital Twin Live State
CREATE TABLE IF NOT EXISTS twin_state (
    site_id TEXT PRIMARY KEY,
    asof TIMESTAMPTZ NOT NULL,
    mode TEXT NOT NULL DEFAULT 'live',
    link_state TEXT NOT NULL DEFAULT 'offline',
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
