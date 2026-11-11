"""Network chaos proxy / connection controller to test intermittent connectivity.

Simulates:
- Link Up / Link Down (Blackout testing)
- Packet loss injection
- RTT delay injection
- Link flapping (e.g. 20s online / 40s offline)
"""

import asyncio
import time
from typing import Optional


class FlappingLinkController:
    """Controls virtual link state for testing store-and-forward edge logic."""
    def __init__(self, up_duration_s: float = 20.0, down_duration_s: float = 40.0):
        self.up_duration_s = up_duration_s
        self.down_duration_s = down_duration_s
        self.is_online = True
        self.flapping = False
        self._task: Optional[asyncio.Task] = None

    async def _flap_loop(self):
        while self.flapping:
            self.is_online = True
            print(f"[NetChaos] Link ONLINE for {self.up_duration_s}s")
            await asyncio.sleep(self.up_duration_s)
            if not self.flapping:
                break
            self.is_online = False
            print(f"[NetChaos] Link OFFLINE for {self.down_duration_s}s")
            await asyncio.sleep(self.down_duration_s)

    def start_flapping(self):
        self.flapping = True
        self._task = asyncio.create_task(self._flap_loop())

    def stop_flapping(self, set_online: bool = True):
        self.flapping = False
        if self._task:
            self._task.cancel()
        self.is_online = set_online
        print(f"[NetChaos] Stopped flapping. Current status: {'ONLINE' if self.is_online else 'OFFLINE'}")

    def set_blackout(self, duration_s: float):
        """Forces an immediate blackout for duration_s seconds."""
        async def _blackout():
            self.is_online = False
            print(f"[NetChaos] Blackout started for {duration_s}s")
            await asyncio.sleep(duration_s)
            self.is_online = True
            print("[NetChaos] Blackout ended. Link restored.")
        asyncio.create_task(_blackout())
