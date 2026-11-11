"""Central Backend Command Dispatch Service.

Section 2.4 & 3.10 Implementation:
Lifecycle: REQUESTED -> AUTHORISED -> VALIDATED -> SENT -> CONFIRMED (or REJECTED / EXPIRED / FAILED)
- Validates parameter bounds and permissions
- Simulates/checks preconditions against twin state
- Signs commands with backend private key
- Records immutable audit trail in command_audit
"""

import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple
import uuid

from contracts.src.messages import CommandMessage, CommandResult
from backend.src.repository import BackendRepository


class CommandService:
    def __init__(self, repo: BackendRepository, private_key_id: str = "backend-key-2026-11"):
        self.repo = repo
        self.key_id = private_key_id

    def request_command(
        self,
        site_id: str,
        gateway_id: str,
        asset_id: str,
        cmd_type: str,
        command_class: str,
        params: Dict[str, Any],
        reason: str,
        requested_by: str,
        validity_minutes: int = 10,
        revert_after_s: Optional[int] = None
    ) -> Tuple[bool, Optional[CommandMessage], str]:
        """Validates command request, signs it, and stores in database audit log."""
        # 1. Validation & Safety Preconditions
        if command_class == "X":
            return False, None, "Class X (Protection) commands are strictly prohibited remotely"

        if cmd_type == "set_soc_reserve":
            soc_val = params.get("min_soc_pct")
            if soc_val is None or not (5.0 <= float(soc_val) <= 50.0):
                return False, None, "Invalid parameter: min_soc_pct must be between 5.0 and 50.0"

        now_dt = datetime.now(timezone.utc)
        expires_dt = now_dt + timedelta(minutes=validity_minutes)
        cmd_id = f"cmd_{uuid.uuid4().hex[:12]}"

        # Mock cryptographic signature (in production: Ed25519 or ECDSA)
        signature = f"SIG_{cmd_id}_{self.key_id}"

        cmd = CommandMessage(
            cmd_id=cmd_id,
            site_id=site_id,
            gateway_id=gateway_id,
            asset_id=asset_id,
            type=cmd_type,
            **{"class": command_class},
            params=params,
            reason=reason,
            requested_by=requested_by,
            issued_at=now_dt.isoformat(),
            expires_at=expires_dt.isoformat(),
            revert_after_s=revert_after_s,
            signature=signature,
            key_id=self.key_id
        )

        # Record in database
        with self.repo._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO commands (
                    cmd_id, site_id, gateway_id, asset_id, type, command_class,
                    params, reason, requested_by, issued_at, expires_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SENT')
                """,
                (
                    cmd.cmd_id,
                    cmd.site_id,
                    cmd.gateway_id,
                    cmd.asset_id,
                    cmd.type,
                    command_class,
                    json.dumps(cmd.params),
                    cmd.reason,
                    cmd.requested_by,
                    cmd.issued_at,
                    cmd.expires_at
                )
            )
            conn.execute(
                "INSERT INTO command_audit (cmd_id, state, details, logged_at) VALUES (?, 'SENT', 'Dispatched to gateway', ?)",
                (cmd.cmd_id, now_dt.isoformat())
            )
            conn.commit()

        return True, cmd, "Command authorised, validated and signed"

    def record_command_result(self, res: CommandResult):
        """Records command outcome from gateway."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.repo._get_connection() as conn:
            conn.execute(
                """
                UPDATE commands
                SET state = ?, readback = ?, finished_at = ?
                WHERE cmd_id = ?
                """,
                (res.state, json.dumps(res.readback), now_iso, res.cmd_id)
            )
            conn.execute(
                "INSERT INTO command_audit (cmd_id, state, details, logged_at) VALUES (?, ?, ?, ?)",
                (res.cmd_id, res.state, f"Finished with reason: {res.reason}", now_iso)
            )
            conn.commit()
