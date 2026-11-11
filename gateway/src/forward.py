"""MQTT 5 Uplink Forwarder with adaptive windowing and selective ack handling.

Section 3.6 & 3.7 Implementation:
- State Machine: CONNECTING, ONLINE, BACKFILLING, DEGRADED, OFFLINE
- Adaptive Window: shrinks on timeouts/packet loss, grows on ACKs
- Selective ACK processing
- Reconnection safety: requeues INFLIGHT to PENDING
- Priority send loop (live first, backfill throttled)
"""

import asyncio
import json
import time
from typing import Callable, Optional
import paho.mqtt.client as mqtt

from contracts.src.messages import SelectiveAck
from .store import GatewayStorage, OutboxBatch


class AdaptiveWindow:
    def __init__(self, max_window: int = 5):
        self.max_window = max(1, max_window)
        self.current_window = min(2, self.max_window)
        self.inflight_count = 0
        self.consecutive_successes = 0

    def available(self) -> bool:
        return self.inflight_count < self.current_window

    def on_sent(self):
        self.inflight_count += 1

    def on_ack_received(self, count: int = 1):
        self.inflight_count = max(0, self.inflight_count - count)
        self.consecutive_successes += count
        if self.consecutive_successes >= 5:
            if self.current_window < self.max_window:
                self.current_window += 1
            self.consecutive_successes = 0

    def on_timeout(self):
        self.current_window = max(1, self.current_window // 2)
        self.consecutive_successes = 0


class Forwarder:
    def __init__(
        self,
        db: GatewayStorage,
        site_id: str,
        gateway_id: str,
        broker_host: str = "localhost",
        broker_port: int = 1883,
        window_max: int = 5,
        live_window_s: int = 120,
        ack_timeout_s: int = 30
    ):
        self.db = db
        self.site_id = site_id
        self.gateway_id = gateway_id
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.live_window_s = live_window_s
        self.ack_timeout_s = ack_timeout_s

        self.window = AdaptiveWindow(window_max)
        self.link_state = "OFFLINE"
        self._running = False
        self._mqtt_client: Optional[mqtt.Client] = None
        self._ack_topic = f"vra/{site_id}/ack/{gateway_id}"
        self._connected_event = asyncio.Event()

    def _setup_mqtt(self):
        client = mqtt.Client(
            client_id=self.gateway_id,
            protocol=mqtt.MQTTv5,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._mqtt_client = client

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            print(f"[Forwarder] MQTT Connected to {self.broker_host}:{self.broker_port}")
            self.link_state = "ONLINE"
            client.subscribe(self._ack_topic, qos=1)
            # Requeue any batches that were inflight before reconnect
            self.db.requeue_all_inflight()
            self._connected_event.set()
        else:
            print(f"[Forwarder] MQTT Connection failed: {reason_code}")
            self.link_state = "OFFLINE"
            self._connected_event.clear()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        print(f"[Forwarder] MQTT Disconnected: {reason_code}")
        self.link_state = "OFFLINE"
        self._connected_event.clear()
        self.window.on_timeout()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            if payload.get("schema") == "ack/1.0":
                ack = SelectiveAck.model_validate(payload)
                self._process_ack(ack)
        except Exception as e:
            print(f"[Forwarder] Failed to process incoming message on {msg.topic}: {e}")

    def _process_ack(self, ack: SelectiveAck):
        if ack.ranges:
            self.db.mark_acked_ranges(ack.stream, ack.ranges)
            total_acked = sum((end - start + 1) for start, end in ack.ranges)
            self.window.on_ack_received(total_acked)
            print(f"[Forwarder] Received ACK for {total_acked} batch(es) on {ack.stream}")

        if ack.rejected:
            for rej in ack.rejected:
                self.db.quarantine_rejected(ack.stream, rej.seq, rej.reason)
                print(f"[Forwarder] Batch {rej.seq} rejected: {rej.reason}")

    async def forwarder_loop(self):
        self._running = True
        self._setup_mqtt()

        while self._running:
            # 1. Connect if offline
            if self.link_state == "OFFLINE":
                try:
                    self._mqtt_client.connect_async(self.broker_host, self.broker_port, keepalive=30)
                    self._mqtt_client.loop_start()
                    # Wait briefly for connection
                    try:
                        await asyncio.wait_for(self._connected_event.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        pass
                except Exception as e:
                    await asyncio.sleep(2.0)
                    continue

            # 2. Check for stale inflight timeouts
            self.db.requeue_stale_inflight(self.ack_timeout_s)

            # 3. Publish pending batches while window allows
            while self.window.available() and self.link_state != "OFFLINE":
                batch = self.db.next_to_send(self.live_window_s, allow_backfill=True)
                if batch is None:
                    break

                self.db.mark_inflight(batch.stream, batch.seq)
                try:
                    # In MQTT 5, user properties can carry stream & seq metadata
                    props = mqtt.Properties(mqtt.PacketTypes.PUBLISH)
                    props.UserProperty = [("enc", batch.enc), ("seq", str(batch.seq)), ("stream", batch.stream)]

                    info = self._mqtt_client.publish(
                        batch.topic,
                        batch.payload,
                        qos=1,
                        properties=props
                    )
                    self.window.on_sent()
                except Exception as e:
                    print(f"[Forwarder] Error publishing batch {batch.seq}: {e}")
                    self.db.mark_pending(batch.stream, batch.seq)
                    self.window.on_timeout()
                    break

            await asyncio.sleep(0.5)

    def stop(self):
        self._running = False
        if self._mqtt_client:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
