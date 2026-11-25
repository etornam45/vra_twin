"""Main orchestration daemon for the edge gateway on the island.

Integrates:
- Storage & Outbox
- Modbus TCP adapter polling
- Encoder & batching
- Forwarder with MQTT 5 link tolerance
- Health heartbeats
- Command handler
"""

import asyncio
import signal
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional
import yaml

from contracts.src.config import GatewayConfig, SiteConfig
from contracts.src.catalog import RegisterMapEntry
from contracts.src.common import SignConvention, TagClass
from gateway.src.store import GatewayStorage
from gateway.src.encoder import TelemetryEncoder, sample_thresholds, should_emit_sample
from gateway.src.forward import Forwarder
from gateway.src.health import HealthAgent
from gateway.src.commands import CommandHandler
from gateway.src.adapters.modbus_tcp import ModbusDeviceAdapter


class GatewayDaemon:
    def __init__(self, site_dir: Path, db_path: Path):
        self.site_dir = Path(site_dir)
        self.db = GatewayStorage(db_path)

        with open(self.site_dir / "site.yaml") as f:
            self.site_cfg = SiteConfig.model_validate(yaml.safe_load(f))

        with open(self.site_dir / "gateway.yaml") as f:
            self.gw_cfg = GatewayConfig.model_validate(yaml.safe_load(f))

        self.site_id = self.site_cfg.site
        self.gateway_id = self.gw_cfg.gateway

        self.encoder = TelemetryEncoder(self.site_id, self.gateway_id)
        self.forwarder = Forwarder(
            db=self.db,
            site_id=self.site_id,
            gateway_id=self.gateway_id,
            broker_host=self.gw_cfg.uplink.broker,
            broker_port=self.gw_cfg.uplink.port,
            window_max=self.gw_cfg.uplink.inflight_max,
            live_window_s=self.gw_cfg.uplink.live_window_s,
            ack_timeout_s=self.gw_cfg.uplink.ack_timeout_s
        )
        self.health_agent = HealthAgent(
            db=self.db,
            gateway_id=self.gateway_id,
            site_id=self.site_id
        )
        self.cmd_handler = CommandHandler(
            db=self.db,
            site_id=self.site_id,
            gateway_id=self.gateway_id,
            enabled=self.gw_cfg.commands.enabled,
            allowed_types=self.gw_cfg.commands.allowed_types
        )

        self.adapters: List[ModbusDeviceAdapter] = []
        self.register_map: Dict[tuple, RegisterMapEntry] = {}
        self._running = False

    def load_adapters_from_mapping(self, mapping_csv_path: Optional[Path] = None):
        """Builds polling adapters using device configuration and mapping.csv."""
        mapping_path = mapping_csv_path or (self.site_dir / "mapping.csv")
        regs_by_asset: Dict[str, List[RegisterMapEntry]] = {}

        if mapping_path.exists():
            import csv
            with open(mapping_path, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    reg = RegisterMapEntry(
                        asset=row["asset"],
                        tag=row["tag"],
                        register=int(row["register"]),
                        function=row.get("function", "holding"),
                        type=row.get("type", "uint16"),
                        scale=float(row.get("scale", 1.0)),
                        unit=row.get("unit", ""),
                        sign=row.get("sign", "as_is"),
                        **{"class": row.get("class", "standard")},
                        min=float(row["min"]) if row.get("min") else None,
                        max=float(row["max"]) if row.get("max") else None,
                        deadband=float(row.get("deadband") or 0.0),
                        max_interval_s=int(row.get("max_interval_s") or 60),
                        writable=row.get("writable", "false").lower() == "true"
                    )
                    regs_by_asset.setdefault(reg.asset, []).append(reg)
                    self.register_map[(reg.asset, reg.tag)] = reg

        for dev in self.gw_cfg.devices:
            if dev.protocol == "modbus_tcp":
                dev_regs = regs_by_asset.get(dev.asset, [])
                adapter = ModbusDeviceAdapter(
                    asset_id=dev.asset,
                    host=dev.host or "127.0.0.1",
                    port=dev.port or 5020,
                    unit_id=dev.unit_id or 1,
                    registers=dev_regs
                )
                self.adapters.append(adapter)

    async def collect_loop(self):
        """Periodically polls all adapters and commits compressed batches to SQLite."""
        while self._running:
            await self.collect_once()
            await asyncio.sleep(5.0)

    async def collect_once(self):
        t0_ms = int(time.time() * 1000)
        series_data: Dict[tuple, Dict[str, list]] = {}
        tag_updates = []
        budget_bytes = self.gw_cfg.disk_budget_mb * 1024 * 1024
        queue_bytes = self.db.get_queue_stats()["bytes_pending"]
        pressure_pct = queue_bytes * 100 / budget_bytes

        if pressure_pct < self.gw_cfg.drop_pressure_pct:
            for gap in self.db.get_unreported_data_gaps():
                self.db.queue_data_gap_event(
                    gap,
                    site_id=self.site_id,
                    gateway_id=self.gateway_id
                )
                queue_bytes = self.db.get_queue_stats()["bytes_pending"]
                pressure_pct = queue_bytes * 100 / budget_bytes
                if pressure_pct >= self.gw_cfg.drop_pressure_pct:
                    break

        for adapter in self.adapters:
            readings = await adapter.poll()
            for r in readings:
                now_sample_ms = int(r.get("ts_ms") or time.time() * 1000)
                register = self.register_map[(r["asset"], r["tag"])]
                tag_state = self.db.get_tag_state(r["asset"], r["tag"])
                last_sent_ms = tag_state["last_sent_ms"] if tag_state else None
                previous_value = tag_state["value_num"] if tag_state else None
                quality = int(r["q"])
                previous_quality = tag_state["quality"] if tag_state else None

                if not should_emit_sample(
                    value=r["v"],
                    quality=quality,
                    sample_ms=now_sample_ms,
                    previous_value=previous_value,
                    previous_quality=previous_quality,
                    last_sent_ms=last_sent_ms,
                    deadband=register.deadband,
                    max_interval_s=register.max_interval_s
                ):
                    continue

                thresholds = sample_thresholds(
                    register.deadband,
                    register.max_interval_s,
                    pressure_pct,
                    self.gw_cfg.downsample_pressure_pct,
                    self.gw_cfg.drop_pressure_pct
                )
                if thresholds is None:
                    self.db.add_data_gap(
                        asset_id=r["asset"],
                        tag=r["tag"],
                        t_ms=now_sample_ms,
                        cause="disk_pressure"
                    )
                    continue

                if not should_emit_sample(
                    value=r["v"],
                    quality=quality,
                    sample_ms=now_sample_ms,
                    previous_value=previous_value,
                    previous_quality=previous_quality,
                    last_sent_ms=last_sent_ms,
                    deadband=thresholds[0],
                    max_interval_s=thresholds[1]
                ):
                    self.db.add_data_gap(
                        asset_id=r["asset"],
                        tag=r["tag"],
                        t_ms=now_sample_ms,
                        cause="disk_downsample"
                    )
                    continue

                offset_ms = now_sample_ms - t0_ms
                key = (r["asset"], r["tag"], r["u"])
                if key not in series_data:
                    series_data[key] = {"t": [], "v": [], "q": []}
                series_data[key]["t"].append(offset_ms)
                series_data[key]["v"].append(r["v"])
                series_data[key]["q"].append(quality)

                tag_updates.append((
                    r["asset"],
                    r["tag"],
                    r["v"],
                    None,
                    quality,
                    now_sample_ms,
                    now_sample_ms
                ))

        if series_data:
            offsets = [
                offset
                for data in series_data.values()
                for offset in data["t"]
            ]
            n_samples = sum(len(data["t"]) for data in series_data.values())
            t_first = t0_ms + min(offsets)
            t_last = t0_ms + max(offsets)
            topic = f"vra/{self.site_id}/telemetry/{self.gateway_id}"
            self.db.allocate_seq_and_insert_batch(
                stream="telemetry",
                topic=topic,
                payload=lambda seq: self.encoder.build_and_compress_batch(
                    series_data=series_data,
                    t0_ms=t0_ms,
                    seq=seq
                )[0],
                n_samples=n_samples,
                t_first_ms=t_first,
                t_last_ms=t_last,
                priority=5,
                enc="zstd+json",
                tag_updates=tag_updates
            )

    async def health_loop(self):
        """Emits periodic health messages every 60s."""
        while self._running:
            try:
                self.health_agent.emit_heartbeat(link_state=self.forwarder.link_state)
            except Exception as e:
                print(f"[Health] Error emitting heartbeat: {e}")
            await asyncio.sleep(60.0)

    async def start(self):
        self._running = True
        self.load_adapters_from_mapping()

        tasks = [
            asyncio.create_task(self.collect_loop()),
            asyncio.create_task(self.health_loop()),
            asyncio.create_task(self.forwarder.forwarder_loop()),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        self._running = False
        self.forwarder.stop()
        for adapter in self.adapters:
            await adapter.close()


if __name__ == "__main__":
    site_dir = Path("sites/VLK-ISL-001")
    db_path = Path("gateway.db")
    daemon = GatewayDaemon(site_dir, db_path)
    asyncio.run(daemon.start())
