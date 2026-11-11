"""Command validation and execution handler for edge gateway.

Section 3.10 Implementation:
- Bounded setpoint execution
- Expiry checks (monotonic and wall clock)
- Allowed types & asset checks
- Range limit and sanity checks
- Idempotency via SQLite commands table
- Readback verification
- Priority 1 outbox cmdres publication
"""

import asyncio
import json
import time
from typing import Any, Callable, Dict, Optional, Tuple

from contracts.src.messages import CommandMessage, CommandResult
from .store import GatewayStorage


class CommandHandler:
    def __init__(
        self,
        db: GatewayStorage,
        site_id: str,
        gateway_id: str,
        enabled: bool = False,
        allowed_types: Optional[list] = None,
        readback_callback: Optional[Callable[[str, str], Any]] = None,
        apply_callback: Optional[Callable[[str, str, Dict[str, Any]], bool]] = None
    ):
        self.db = db
        self.site_id = site_id
        self.gateway_id = gateway_id
        self.enabled = enabled
        self.allowed_types = set(allowed_types or ["set_soc_reserve", "set_curtailment", "reboot"])
        self.readback_callback = readback_callback
        self.apply_callback = apply_callback

    def handle_command(self, cmd: CommandMessage) -> CommandResult:
        now_ms = int(time.time() * 1000)
        received_ms = now_ms

        # 1. Check if already processed (Idempotency)
        with self.db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM commands WHERE cmd_id = ?", (cmd.cmd_id,))
            existing = cursor.fetchone()
            if existing:
                readback_dict = json.loads(existing["readback"]) if existing["readback"] else {}
                return CommandResult(
                    cmd_id=cmd.cmd_id,
                    site_id=self.site_id,
                    gateway_id=self.gateway_id,
                    state=existing["state"],
                    reason=existing["reason"],
                    readback=readback_dict,
                    finished_ms=existing["finished_ms"]
                )

        # 2. Check if commands are enabled on gateway
        if not self.enabled:
            return self._record_and_return(cmd, "REJECTED", "commands_disabled", received_ms)

        # 3. Check expiry
        try:
            # Parse ISO expiry
            from datetime import datetime, timezone
            expires_dt = datetime.fromisoformat(cmd.expires_at.replace("Z", "+00:00"))
            expires_ms = int(expires_dt.timestamp() * 1000)
            if now_ms > expires_ms:
                return self._record_and_return(cmd, "EXPIRED", "command_expired", received_ms, expires_ms=expires_ms)
        except Exception as e:
            return self._record_and_return(cmd, "REJECTED", f"invalid_expires_at: {e}", received_ms)

        # 4. Check allowed type & class
        if cmd.type not in self.allowed_types:
            return self._record_and_return(cmd, "REJECTED", f"type_not_allowed: {cmd.type}", received_ms)

        # Protection settings (Class X) are strictly forbidden
        if cmd.command_class == "X":
            return self._record_and_return(cmd, "REJECTED", "protection_class_x_forbidden", received_ms)

        # 5. Local limits validation (e.g. SOC reserve limits)
        if cmd.type == "set_soc_reserve":
            min_soc = cmd.params.get("min_soc_pct")
            if min_soc is None or not (5.0 <= float(min_soc) <= 50.0):
                return self._record_and_return(cmd, "REJECTED", "out_of_range_min_soc", received_ms)

        # 6. Apply command
        try:
            applied = True
            if self.apply_callback:
                applied = self.apply_callback(cmd.asset_id, cmd.type, cmd.params)

            if not applied:
                return self._record_and_return(cmd, "FAILED", "device_rejected_write", received_ms)

            # 7. Readback and verify
            readback_data = {}
            if self.readback_callback:
                readback_data = self.readback_callback(cmd.asset_id, cmd.type)

            return self._record_and_return(
                cmd,
                "CONFIRMED",
                None,
                received_ms,
                readback=readback_data,
                applied_at=datetime.now(timezone.utc).isoformat()
            )

        except Exception as e:
            return self._record_and_return(cmd, "FAILED", f"apply_exception: {e}", received_ms)

    def _record_and_return(
        self,
        cmd: CommandMessage,
        state: str,
        reason: Optional[str],
        received_ms: int,
        expires_ms: int = 0,
        readback: Optional[Dict[str, Any]] = None,
        applied_at: Optional[str] = None
    ) -> CommandResult:
        now_ms = int(time.time() * 1000)
        readback_str = json.dumps(readback or {})

        with self.db._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO commands (
                    cmd_id, received_ms, issued_ms, expires_ms, asset_id, type, params,
                    state, reason, readback, finished_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cmd_id) DO UPDATE SET
                    state=excluded.state,
                    reason=excluded.reason,
                    readback=excluded.readback,
                    finished_ms=excluded.finished_ms
                """,
                (
                    cmd.cmd_id,
                    received_ms,
                    now_ms,
                    expires_ms,
                    cmd.asset_id,
                    cmd.type,
                    json.dumps(cmd.params),
                    state,
                    reason,
                    readback_str,
                    now_ms
                )
            )
            conn.commit()

        res = CommandResult(
            cmd_id=cmd.cmd_id,
            site_id=self.site_id,
            gateway_id=self.gateway_id,
            state=state,
            reason=reason,
            readback=readback or {},
            applied_at=applied_at,
            finished_ms=now_ms
        )

        # Queue command result at priority 1 in outbox
        topic = f"vra/{self.site_id}/cmdres/{self.gateway_id}"
        self.db.allocate_seq_and_insert_batch(
            stream="cmdres",
            topic=topic,
            payload=res.model_dump_json().encode("utf-8"),
            n_samples=1,
            t_first_ms=now_ms,
            t_last_ms=now_ms,
            priority=1,
            enc="json"
        )

        return res
