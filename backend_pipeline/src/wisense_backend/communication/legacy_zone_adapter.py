"""Legacy Unreal zone-protocol adapter and publisher bridge for WiSense-Twin.

Translates authoritative `StateMessage` v1 streams into incremental legacy zone updates
compatible with the historical Unreal Engine 5 prototype visualizer:
    {"zone": <int>, "occupied": <bool>}

SEMANTIC NOTICE:
The field name `occupied` is a legacy wire name retained solely for compatibility with the
prototype Unreal visualizer. It denotes "motion-derived presence held in this zone", NOT
confirmed occupancy detection. A stationary individual without active motion perturbation
will decay to occupied=false upon presence timeout. At most one zone is occupied at any
time under the current single-person scope.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from wisense_backend.contracts.legacy_zone_message import LegacyZoneMessage
from wisense_backend.contracts.state_message import StateMessage

logger = logging.getLogger(__name__)


class LegacyZoneAdapter:
    """Pure, stateful translator converting StateMessage decisions into legacy zone messages.

    SEMANTIC NOTICE:
    The wire field name `occupied` denotes motion-derived presence held in a zone,
    not literal occupancy detection. At most one zone can be occupied at any time.

    Translation Invariant Rules:
    - is_motion=True, zone_id=k in 1..zone_count:
        If k != current_occupied_zone:
          1. Emit {zone: current_occupied_zone, occupied: False} (if a zone was occupied).
          2. Emit {zone: k, occupied: True}.
          3. Set current_occupied_zone = k.
    - is_motion=True, zone_id=None:
        Hold current zone state; emit nothing.
    - is_motion=False, presence=True:
        Hold current zone state; emit nothing (presence hold decay phase).
    - presence=False:
        If a zone is currently occupied:
          1. Emit {zone: current_occupied_zone, occupied: False}.
          2. Clear current_occupied_zone to None.
        If no zone occupied, emit nothing.
    - zone_id outside 1..zone_count:
        Ignore decision; emit nothing; log one throttled warning.
    - Repeat of current occupied state:
        Emit nothing (suppress duplicate wire emissions).
    """

    def __init__(
        self,
        zone_count: int,
        clock: Callable[[], float] = time.monotonic,
        warning_throttle_seconds: float = 5.0,
    ) -> None:
        """Initialize the LegacyZoneAdapter.

        Args:
            zone_count: Total number of configured spatial zones (must be >= 1).
            clock: Time provider function for throttling warnings (default: time.monotonic).
            warning_throttle_seconds: Minimum interval between throttled warnings.

        Raises:
            TypeError: If zone_count is not an integer.
            ValueError: If zone_count is < 1.
        """
        if isinstance(zone_count, bool) or not isinstance(zone_count, int):
            raise TypeError(f"zone_count must be an integer, got {type(zone_count).__name__}")
        if zone_count < 1:
            raise ValueError(f"zone_count must be >= 1, got {zone_count}")

        self._zone_count = zone_count
        self._clock = clock
        self._warning_throttle_seconds = float(warning_throttle_seconds)
        self._occupied_zone: Optional[int] = None
        self._last_warning_time: float = -float("inf")

    @property
    def zone_count(self) -> int:
        """Return the total number of configured spatial zones."""
        return self._zone_count

    @property
    def clock(self) -> Callable[[], float]:
        """Return the clock callable used by the adapter."""
        return self._clock

    @property
    def occupied_zone(self) -> Optional[int]:
        """Return the currently occupied zone ID, or None if idle."""
        return self._occupied_zone

    def process(self, state: StateMessage) -> list[LegacyZoneMessage]:
        """Translate a StateMessage decision into zero or more LegacyZoneMessage updates.

        Args:
            state: Authoritative StateMessage instance from pipeline.

        Returns:
            List of LegacyZoneMessage instances to broadcast (empty if no change).

        Raises:
            TypeError: If state is not a StateMessage instance.
        """
        if not isinstance(state, StateMessage):
            raise TypeError(f"Expected StateMessage instance, got {type(state).__name__}")

        # Rule: presence is False -> decay to clear
        if not state.presence:
            if self._occupied_zone is not None:
                prev_zone = self._occupied_zone
                self._occupied_zone = None
                return [LegacyZoneMessage(zone=prev_zone, occupied=False)]
            return []

        # state.presence is True:
        # Rule: is_motion False, presence True -> hold current zone, emit nothing
        if not state.is_motion:
            return []

        # state.is_motion is True:
        # Rule: zone_id is None -> hold current zone, emit nothing
        if state.zone_id is None:
            return []

        k = state.zone_id

        # Rule: zone_id outside 1..zone_count -> ignore, throttled warning, emit nothing
        if k < 1 or k > self._zone_count:
            now = self._clock()
            if (now - self._last_warning_time) >= self._warning_throttle_seconds:
                logger.warning(
                    "StateMessage zone_id %d is outside configured range [1, %d]; ignoring",
                    k,
                    self._zone_count,
                )
                self._last_warning_time = now
            return []

        # Rule: repeat of current state -> emit nothing (no duplicates)
        if k == self._occupied_zone:
            return []

        # Rule: zone switch emits old zone's false before new zone's true
        messages: list[LegacyZoneMessage] = []
        if self._occupied_zone is not None:
            messages.append(LegacyZoneMessage(zone=self._occupied_zone, occupied=False))
        self._occupied_zone = k
        messages.append(LegacyZoneMessage(zone=k, occupied=True))
        return messages

    def snapshot(self) -> list[LegacyZoneMessage]:
        """Return a complete state snapshot across all zones 1..zone_count.

        Every zone is false except the currently occupied zone (if any) which is true.
        This allows new clients (or resynchronization cycles) to establish ground state.

        Returns:
            List of LegacyZoneMessage for all zones in 1..zone_count.
        """
        return [
            LegacyZoneMessage(zone=z, occupied=(z == self._occupied_zone))
            for z in range(1, self._zone_count + 1)
        ]

    def reset(self) -> None:
        """Reset internal occupancy tracking to idle baseline."""
        self._occupied_zone = None
        self._last_warning_time = -float("inf")


class LegacyZoneBridge:
    """Coordinating bridge between LegacyZoneAdapter and a WebSocket publisher.

    Invariants & Behavior:
    - Atomicity & Order Invariants: Snapshot delivery to a newly connecting client and
      registration into the publisher's broadcast set execute as a single critical section
      under the bridge's lock. The client is not in the broadcast set before receiving its
      snapshot. Lock ordering is strictly identical across paths (bridge lock first,
      publisher clients lock second) to eliminate deadlock.
    - Snapshot on connect: Registers an on-connect hook on the publisher so every new client
      immediately receives a full snapshot() of all zones (hiding un-occupied cubes in Unreal).
    - Periodic resynchronization: When resync_interval_seconds > 0, re-sends full snapshot()
      whenever the interval has elapsed since the last transmission, checked during publish_state().
      0 disables resync. No background thread is spawned.
    - Clean shutdown & bounded flush: close() performs a best-effort send of all-false across
      all zones before server termination. Callers should apply a best-effort bounded flush
      before stopping the underlying publisher.
    - Known limitation: The connect hook sends under the bridge lock, so one blocked client
      socket can delay publishing.
    """

    def __init__(
        self,
        adapter: LegacyZoneAdapter,
        publisher: Any,
        resync_interval_seconds: float = 5.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """Initialize LegacyZoneBridge.

        Args:
            adapter: Stateful LegacyZoneAdapter instance.
            publisher: WebSocket publisher instance providing publish_text(payload: str).
            resync_interval_seconds: Re-send interval for periodic snapshot resync (>= 0.0, 0 disables).
            clock: Optional clock callable for time tracking (defaults to adapter.clock).

        Raises:
            TypeError: If arguments are of incorrect types.
            ValueError: If resync_interval_seconds < 0.
        """
        if not isinstance(adapter, LegacyZoneAdapter):
            raise TypeError(f"Expected LegacyZoneAdapter, got {type(adapter).__name__}")
        if not hasattr(publisher, "publish_text"):
            raise TypeError("publisher must provide publish_text(payload: str)")
        if isinstance(resync_interval_seconds, bool) or not isinstance(
            resync_interval_seconds, (int, float)
        ):
            raise TypeError(
                f"resync_interval_seconds must be a number, got {type(resync_interval_seconds).__name__}"
            )
        if resync_interval_seconds < 0.0:
            raise ValueError(
                f"resync_interval_seconds must be >= 0, got {resync_interval_seconds}"
            )

        self._adapter = adapter
        self._publisher = publisher
        self._resync_interval_seconds = float(resync_interval_seconds)
        self._clock = clock if clock is not None else adapter.clock
        self._last_send_time: float = self._clock()
        self._lock = threading.RLock()

        # Register connect hook and synchronization lock if publisher supports them
        if hasattr(self._publisher, "set_connect_lock"):
            self._publisher.set_connect_lock(self._lock)
        if hasattr(self._publisher, "set_connect_hook"):
            self._publisher.set_connect_hook(self._on_client_connected)

    @property
    def lock(self) -> threading.RLock:
        """Return the bridge synchronization lock."""
        return self._lock

    @property
    def adapter(self) -> LegacyZoneAdapter:
        """Return the underlying LegacyZoneAdapter."""
        return self._adapter

    @property
    def publisher(self) -> Any:
        """Return the publisher instance."""
        return self._publisher

    @property
    def resync_interval_seconds(self) -> float:
        """Return the periodic snapshot resynchronization interval in seconds."""
        return self._resync_interval_seconds

    def _on_client_connected(self) -> list[str]:
        """Connect hook generating initial snapshot for newly connected clients."""
        with self._lock:
            return [msg.to_json() for msg in self._adapter.snapshot()]

    def publish_state(self, state: StateMessage) -> None:
        """Process StateMessage through adapter and broadcast legacy zone messages.

        Checks and emits periodic snapshot resynchronization if resync_interval_seconds
        has elapsed since the last transmission. Executes under bridge lock to prevent
        out-of-order race conditions with connecting clients.

        Args:
            state: Authoritative StateMessage from pipeline.
        """
        with self._lock:
            now = self._clock()
            # Resync check: whenever interval has elapsed since last send
            if (
                self._resync_interval_seconds > 0.0
                and (now - self._last_send_time) >= self._resync_interval_seconds
            ):
                for msg in self._adapter.snapshot():
                    self._publisher.publish_text(msg.to_json())
                self._last_send_time = now

            # Process state through adapter
            messages = self._adapter.process(state)
            if messages:
                for msg in messages:
                    self._publisher.publish_text(msg.to_json())
                self._last_send_time = now

    def emit_all_false(self) -> None:
        """Make a best-effort broadcast of occupied=false across all zones and reset state."""
        with self._lock:
            try:
                for z in range(1, self._adapter.zone_count + 1):
                    msg = LegacyZoneMessage(zone=z, occupied=False)
                    self._publisher.publish_text(msg.to_json())
                self._adapter.reset()
            except Exception as e:
                logger.warning("Error emitting all-false on LegacyZoneBridge: %s", e)

    def close(self) -> None:
        """Make a best-effort send of all-false before the caller stops the publisher."""
        self.emit_all_false()

    def __enter__(self) -> LegacyZoneBridge:
        """Enter context manager."""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Exit context manager, closing bridge."""
        self.close()
