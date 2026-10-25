"""Integration and unit tests for legacy zone publishing, resynchronization, and publisher hooks."""

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import pytest
import websockets.sync.client as ws_client
from websockets.exceptions import ConnectionClosed

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from wisense_backend.communication.legacy_zone_adapter import (
    LegacyZoneAdapter,
    LegacyZoneBridge,
)
from wisense_backend.communication.websocket_server import (
    WebSocketConfig,
    WebSocketStatePublisher,
)
from wisense_backend.contracts.legacy_zone_message import LegacyZoneMessage
from wisense_backend.contracts.state_message import StateMessage


class FakeClock:
    """Controllable clock for deterministic timing verification."""

    def __init__(self, start_time: float = 100.0) -> None:
        self.current_time = start_time

    def __call__(self) -> float:
        return self.current_time

    def advance(self, delta_seconds: float) -> None:
        self.current_time += delta_seconds


class MockPublisher:
    """Mock publisher capturing text emissions and hook registration."""

    def __init__(self) -> None:
        self.published_text: list[str] = []
        self.hook: Optional[Callable[[], list[str]]] = None

    def publish_text(self, payload: str) -> None:
        self.published_text.append(payload)

    def set_connect_hook(self, hook: Callable[[], list[str]]) -> None:
        self.hook = hook


def wait_for_condition(
    predicate: Callable[[], bool],
    timeout: float = 3.0,
    interval: float = 0.02,
) -> bool:
    """Poll predicate until True or timeout reached."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def make_state(
    zone_id: Optional[int],
    is_motion: bool,
    presence: bool,
    timestamp: float = 1700000000.0,
) -> StateMessage:
    """Helper creating valid StateMessage instances."""
    return StateMessage(
        schema_version=1,
        timestamp=timestamp,
        zone_id=zone_id,
        is_motion=is_motion,
        presence=presence,
        separation_margin=0.05 if zone_id is not None and is_motion else None,
        calibration_version=1,
    )


class TestLegacyZoneBridgeResync:
    """Verify periodic snapshot resynchronization logic and parameters."""

    def test_resync_interval_and_timing(self) -> None:
        """Verify snapshot is re-sent after interval elapses, not before, and timing updates."""
        clock = FakeClock(start_time=100.0)
        adapter = LegacyZoneAdapter(zone_count=4, clock=clock)
        publisher = MockPublisher()
        bridge = LegacyZoneBridge(
            adapter=adapter,
            publisher=publisher,
            resync_interval_seconds=5.0,
            clock=clock,
        )

        # Hook was registered on publisher
        assert publisher.hook is not None
        initial_hook_payloads = publisher.hook()
        assert len(initial_hook_payloads) == 4

        # Initial state: motion in zone 1 at t=100.0
        bridge.publish_state(make_state(zone_id=1, is_motion=True, presence=True))
        assert len(publisher.published_text) == 1
        assert publisher.published_text[0] == '{"zone":1,"occupied":true}'
        publisher.published_text.clear()

        # Advance 1.0s (t=101.0): same state -> no resync, no emission
        clock.advance(1.0)
        bridge.publish_state(make_state(zone_id=1, is_motion=True, presence=True))
        assert len(publisher.published_text) == 0

        # Advance 3.9s (t=104.9, elapsed 4.9s < 5.0s): still no resync
        clock.advance(3.9)
        bridge.publish_state(make_state(zone_id=1, is_motion=True, presence=True))
        assert len(publisher.published_text) == 0

        # Advance 0.1s (t=105.0, elapsed 5.0s >= 5.0s): resync must emit snapshot!
        clock.advance(0.1)
        bridge.publish_state(make_state(zone_id=1, is_motion=True, presence=True))
        assert len(publisher.published_text) == 4
        received = [LegacyZoneMessage.from_json(s) for s in publisher.published_text]
        assert received == adapter.snapshot()
        assert received[0] == LegacyZoneMessage(zone=1, occupied=True)
        assert all(m.occupied is False for m in received[1:])
        publisher.published_text.clear()

        # Immediate next frame at t=105.1: no resync
        clock.advance(0.1)
        bridge.publish_state(make_state(zone_id=1, is_motion=True, presence=True))
        assert len(publisher.published_text) == 0

        # Advance 5.0s (t=110.1): second resync fires
        clock.advance(5.0)
        bridge.publish_state(make_state(zone_id=1, is_motion=True, presence=True))
        assert len(publisher.published_text) == 4

    def test_resync_disabled_when_zero(self) -> None:
        """Verify resync_interval_seconds=0 disables periodic snapshot resync completely."""
        clock = FakeClock(start_time=100.0)
        adapter = LegacyZoneAdapter(zone_count=4, clock=clock)
        publisher = MockPublisher()
        bridge = LegacyZoneBridge(
            adapter=adapter,
            publisher=publisher,
            resync_interval_seconds=0.0,
            clock=clock,
        )

        bridge.publish_state(make_state(zone_id=2, is_motion=True, presence=True))
        assert len(publisher.published_text) == 1
        publisher.published_text.clear()

        # Advance 100 seconds: no resync ever emitted
        clock.advance(100.0)
        bridge.publish_state(make_state(zone_id=2, is_motion=True, presence=True))
        assert len(publisher.published_text) == 0

    def test_bridge_parameter_validation(self) -> None:
        """Verify constructor validation for bridge."""
        clock = FakeClock()
        adapter = LegacyZoneAdapter(zone_count=4, clock=clock)
        publisher = MockPublisher()

        with pytest.raises(ValueError, match="resync_interval_seconds must be >= 0"):
            LegacyZoneBridge(adapter, publisher, resync_interval_seconds=-1.0)

        with pytest.raises(TypeError, match="resync_interval_seconds must be a number"):
            LegacyZoneBridge(adapter, publisher, resync_interval_seconds="5.0")  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="resync_interval_seconds must be a number"):
            LegacyZoneBridge(adapter, publisher, resync_interval_seconds=True)  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="Expected LegacyZoneAdapter"):
            LegacyZoneBridge("not an adapter", publisher)  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="publisher must provide publish_text"):
            LegacyZoneBridge(adapter, object())


class TestPublisherExtensions:
    """Verify concrete publish_text, zero-client no-op, multi-client broadcast, and connect hook."""

    def test_publish_text_zero_client_noop(self) -> None:
        """Verify publish_text is a safe no-op with zero connected clients."""
        with WebSocketStatePublisher(WebSocketConfig(port=0)) as pub:
            assert pub.client_count == 0
            # Must not raise or fail
            pub.publish_text('{"zone":1,"occupied":true}')
            assert pub.client_count == 0

    def test_publish_text_broadcast_multiple_clients(self) -> None:
        """Verify publish_text broadcasts raw text payloads to multiple connected test clients."""
        with WebSocketStatePublisher(WebSocketConfig(port=0)) as pub:
            url = f"ws://127.0.0.1:{pub.actual_port}"

            with ws_client.connect(url) as client1:
                with ws_client.connect(url) as client2:
                    assert wait_for_condition(lambda: pub.client_count == 2)

                    payload = '{"zone":3,"occupied":true}'
                    pub.publish_text(payload)

                    msg1 = client1.recv(timeout=2.0)
                    msg2 = client2.recv(timeout=2.0)
                    assert msg1 == payload
                    assert msg2 == payload

    def test_connect_hook_delivers_to_new_client_only_before_broadcasts(self) -> None:
        """Verify on-connect hook delivers initial frames to just the new client before later broadcasts."""
        with WebSocketStatePublisher(WebSocketConfig(port=0)) as pub:
            url = f"ws://127.0.0.1:{pub.actual_port}"

            call_count = 0

            def hook() -> list[str]:
                nonlocal call_count
                call_count += 1
                return [
                    '{"zone":1,"occupied":false}',
                    '{"zone":2,"occupied":false}',
                ]

            pub.set_connect_hook(hook)

            # Connect client 1: receives hook frames
            with ws_client.connect(url) as client1:
                assert wait_for_condition(lambda: pub.client_count == 1)
                assert call_count == 1

                c1_init1 = client1.recv(timeout=2.0)
                c1_init2 = client1.recv(timeout=2.0)
                assert c1_init1 == '{"zone":1,"occupied":false}'
                assert c1_init2 == '{"zone":2,"occupied":false}'

                # Publisher broadcasts a live transition
                pub.publish_text('{"zone":1,"occupied":true}')
                c1_live = client1.recv(timeout=2.0)
                assert c1_live == '{"zone":1,"occupied":true}'

                # Now connect client 2: hook runs for client 2 only!
                with ws_client.connect(url) as client2:
                    assert wait_for_condition(lambda: pub.client_count == 2)
                    assert call_count == 2

                    c2_init1 = client2.recv(timeout=2.0)
                    c2_init2 = client2.recv(timeout=2.0)
                    assert c2_init1 == '{"zone":1,"occupied":false}'
                    assert c2_init2 == '{"zone":2,"occupied":false}'

                    # Verify client 1 did NOT receive client 2's hook messages
                    # Broadcast another message to both
                    pub.publish_text('{"zone":2,"occupied":true}')
                    c1_next = client1.recv(timeout=2.0)
                    c2_next = client2.recv(timeout=2.0)
                    assert c1_next == '{"zone":2,"occupied":true}'
                    assert c2_next == '{"zone":2,"occupied":true}'

    def test_raising_connect_hook_does_not_break_connection(self) -> None:
        """Verify that an exception raised inside the connect hook is isolated and does not drop client."""
        with WebSocketStatePublisher(WebSocketConfig(port=0)) as pub:
            url = f"ws://127.0.0.1:{pub.actual_port}"

            def failing_hook() -> list[str]:
                raise RuntimeError("Hook exploded intentionally")

            pub.set_connect_hook(failing_hook)

            with ws_client.connect(url) as client:
                assert wait_for_condition(lambda: pub.client_count == 1)

                # Client remains connected and receives subsequent broadcasts
                pub.publish_text('{"zone":4,"occupied":true}')
                msg = client.recv(timeout=2.0)
                assert msg == '{"zone":4,"occupied":true}'


class TestWebSocketConfigExtended:
    """Verify WebSocketConfig message_format and legacy_resync_interval_seconds."""

    def test_config_defaults(self) -> None:
        """Verify default configuration has state_message_v1 format and 5.0s resync."""
        cfg = WebSocketConfig()
        assert cfg.message_format == "state_message_v1"
        assert cfg.legacy_resync_interval_seconds == 5.0

    def test_config_custom_valid(self) -> None:
        """Verify custom configuration with legacy_zone format."""
        cfg = WebSocketConfig(
            host="0.0.0.0",
            port=9000,
            message_format="legacy_zone",
            legacy_resync_interval_seconds=10.0,
        )
        assert cfg.message_format == "legacy_zone"
        assert cfg.legacy_resync_interval_seconds == 10.0
        d = cfg.to_dict()
        assert d == {
            "host": "0.0.0.0",
            "port": 9000,
            "message_format": "legacy_zone",
            "legacy_resync_interval_seconds": 10.0,
        }

    def test_config_validation(self) -> None:
        """Verify validation errors on invalid format or negative resync."""
        with pytest.raises(ValueError, match="message_format must be one of"):
            WebSocketConfig(message_format="invalid_format")

        with pytest.raises(ValueError, match="legacy_resync_interval_seconds must be >= 0"):
            WebSocketConfig(legacy_resync_interval_seconds=-0.5)

        with pytest.raises(TypeError, match="legacy_resync_interval_seconds must be a number"):
            WebSocketConfig(legacy_resync_interval_seconds="5.0")  # type: ignore[arg-type]

    def test_canonical_json_loading(self) -> None:
        """Verify loading from backend_pipeline/config/websocket_config.json."""
        config_path = (
            Path(__file__).resolve().parent.parent / "config" / "websocket_config.json"
        )
        assert config_path.is_file()
        cfg = WebSocketConfig.from_json_file(config_path)
        assert cfg.message_format == "state_message_v1"
        assert cfg.legacy_resync_interval_seconds == 5.0


class TestLegacyZoneBridgePipelineIntegration:
    """Integration test streaming multi-zone pipeline output through bridge to real WebSocket client."""

    def test_sequential_4_zone_pipeline_stream_and_invariants(self) -> None:
        """Stream a 4-zone sequential scenario through the pipeline, bridge, and WebSocket client.

        Asserts:
        - Exact compact JSON wire format.
        - Each zone goes true then false.
        - At most one zone is true at any point in time.
        - Client-side tracking ends with the same state as bridge.adapter.snapshot().
        - After bridge.close(), all zones are set to false.
        """
        from wisense_backend.calibration.calibrator import ZoneCalibrator
        from wisense_backend.calibration.labeled_sample import LabeledFeatureSample
        from wisense_backend.classification.nearest_signature_classifier import (
            NearestSignatureZoneClassifier,
            ZoneClassifierConfig,
        )
        from wisense_backend.detection.threshold_motion_detector import (
            ThresholdMotionConfig,
            ThresholdMotionDetector,
        )
        from wisense_backend.features.variance_extractor import (
            VarianceExtractorConfig,
            VarianceFeatureExtractor,
        )
        from wisense_backend.ingestion.synthetic_source import (
            ScenarioConfig,
            ScenarioEvent,
            SyntheticCSISource,
        )
        from wisense_backend.preprocessing.hampel_filter import HampelConfig, HampelFilter
        from wisense_backend.preprocessing.moving_average import (
            MovingAverageConfig,
            MovingAverageFilter,
        )
        from wisense_backend.preprocessing.pipeline import PreprocessingPipeline

        zone_count = 4
        subcarriers = 32

        # 1. Calibration
        calib_events = [
            ScenarioEvent(start_time_s=1.0, end_time_s=3.0, zone_id=1),
            ScenarioEvent(start_time_s=4.0, end_time_s=6.0, zone_id=2),
            ScenarioEvent(start_time_s=7.0, end_time_s=9.0, zone_id=3),
            ScenarioEvent(start_time_s=10.0, end_time_s=12.0, zone_id=4),
        ]
        calib_scen = ScenarioConfig(
            scenario_name="calib",
            zone_count=zone_count,
            subcarrier_count=subcarriers,
            sample_rate_hz=20.0,
            duration_seconds=13.0,
            realtime=False,
            events=tuple(calib_events),
        )
        with SyntheticCSISource(calib_scen) as src:
            calib_raw = list(src)

        prep = PreprocessingPipeline([
            HampelFilter(HampelConfig(window_size=5)),
            MovingAverageFilter(MovingAverageConfig(window_size=3)),
        ])
        feat_ext = VarianceFeatureExtractor(VarianceExtractorConfig(window_size_samples=10, window_step_samples=1))
        fvs = list(feat_ext.process_stream(prep.process_stream(calib_raw)))

        start_t = calib_raw[0].timestamp
        labeled_samples = []
        for fv in fvs:
            mid_t = (fv.window_start_timestamp + fv.window_end_timestamp) / 2.0
            rel_t = mid_t - start_t
            active = [e for e in calib_events if e.start_time_s <= rel_t <= e.end_time_s]
            if len(active) == 1:
                labeled_samples.append(LabeledFeatureSample(fv, zone_id=active[0].zone_id))

        calibrator = ZoneCalibrator(zone_count=zone_count, subcarrier_count=subcarriers, calibration_version=1)
        profile = calibrator.calibrate(labeled_samples)
        classifier = NearestSignatureZoneClassifier(profile, config=ZoneClassifierConfig(min_separation_margin=0.01))

        # 2. Server & Bridge setup
        with WebSocketStatePublisher(WebSocketConfig(port=0)) as publisher:
            adapter = LegacyZoneAdapter(zone_count=zone_count)
            bridge = LegacyZoneBridge(
                adapter=adapter,
                publisher=publisher,
                resync_interval_seconds=0.0,  # disable periodic resync during discrete test stream
            )

            client_url = f"ws://127.0.0.1:{publisher.actual_port}"

            with ws_client.connect(client_url) as client:
                assert wait_for_condition(lambda: publisher.client_count == 1)

                # Client first receives initial snapshot (hiding cubes)
                client_zones: dict[int, bool] = {}
                for _ in range(zone_count):
                    raw_snap = client.recv(timeout=2.0)
                    msg_snap = LegacyZoneMessage.from_json(raw_snap)
                    client_zones[msg_snap.zone] = msg_snap.occupied

                assert len(client_zones) == zone_count
                assert all(occ is False for occ in client_zones.values())

                # 3. Stream sequential live motion across all 4 zones
                live_scen = ScenarioConfig(
                    scenario_name="live_run",
                    zone_count=zone_count,
                    subcarrier_count=subcarriers,
                    sample_rate_hz=20.0,
                    duration_seconds=12.0,
                    realtime=False,
                    events=(
                        ScenarioEvent(start_time_s=0.5, end_time_s=2.5, zone_id=1),
                        ScenarioEvent(start_time_s=3.0, end_time_s=5.0, zone_id=2),
                        ScenarioEvent(start_time_s=5.5, end_time_s=7.5, zone_id=3),
                        ScenarioEvent(start_time_s=8.0, end_time_s=10.0, zone_id=4),
                    ),
                )

                detector = ThresholdMotionDetector(
                    ThresholdMotionConfig(
                        enter_threshold=0.08,
                        exit_threshold=0.04,
                        presence_timeout_seconds=0.4,
                    )
                )

                pipe_prep = PreprocessingPipeline([
                    HampelFilter(HampelConfig(window_size=5)),
                    MovingAverageFilter(MovingAverageConfig(window_size=3)),
                ])
                pipe_feat = VarianceFeatureExtractor(
                    VarianceExtractorConfig(window_size_samples=10, window_step_samples=1)
                )

                zones_seen_true: set[int] = set()
                zones_seen_false_after_true: set[int] = set()

                with SyntheticCSISource(live_scen) as source:
                    for raw_rec in source:
                        clean = pipe_prep.process(raw_rec)
                        fv = pipe_feat.process(clean)
                        if fv is None:
                            continue
                        motion_st = detector.process(fv)
                        res = classifier.classify(fv, motion_st)

                        state_msg = StateMessage(
                            schema_version=1,
                            timestamp=time.time(),
                            zone_id=res.zone_id,
                            is_motion=motion_st.is_motion,
                            presence=motion_st.presence,
                            separation_margin=res.separation_margin,
                            calibration_version=profile.calibration_version,
                        )

                        bridge.publish_state(state_msg)

                # Drain all published messages on client
                while True:
                    try:
                        raw = client.recv(timeout=0.2)
                        msg = LegacyZoneMessage.from_json(raw)

                        # Update client-side state
                        client_zones[msg.zone] = msg.occupied

                        # Invariant: at most one zone is true at any point in time
                        true_zones = [z for z, occ in client_zones.items() if occ]
                        assert (
                            len(true_zones) <= 1
                        ), f"Mutual exclusivity violated: multiple zones active: {true_zones}"

                        if msg.occupied:
                            zones_seen_true.add(msg.zone)
                        elif msg.zone in zones_seen_true:
                            zones_seen_false_after_true.add(msg.zone)

                    except TimeoutError:
                        break

                # Verify each zone 1..4 went true then false
                assert zones_seen_true == {1, 2, 3, 4}, f"Zones seen true: {zones_seen_true}"
                assert zones_seen_false_after_true == {
                    1,
                    2,
                    3,
                    4,
                }, f"Zones seen false after true: {zones_seen_false_after_true}"

                # Verify client-side state matches adapter snapshot
                expected_snap = {m.zone: m.occupied for m in adapter.snapshot()}
                assert client_zones == expected_snap

                # 4. Close bridge -> best effort send of all-false
                bridge.close()

                for _ in range(zone_count):
                    raw_close = client.recv(timeout=2.0)
                    msg_close = LegacyZoneMessage.from_json(raw_close)
                    assert msg_close.occupied is False
                    client_zones[msg_close.zone] = False

                # After close, all zones are false
                assert all(occ is False for occ in client_zones.values())


class TestConnectSnapshotRaceCondition:
    """Deterministic tests proving prevention of connect/snapshot ordering races."""

    def test_connect_snapshot_atomic_ordering_with_transition_race(self) -> None:
        """Introduce a controlled delay inside connect hook while another thread calls publish_state.

        Verifies:
        - The client receives the initial snapshot first.
        - The transition message arrives after the snapshot.
        - Applying all received messages produces exact parity with adapter.snapshot().
        """
        with WebSocketStatePublisher(WebSocketConfig(port=0)) as pub:
            url = f"ws://127.0.0.1:{pub.actual_port}"
            adapter = LegacyZoneAdapter(zone_count=4)
            bridge = LegacyZoneBridge(adapter, pub, resync_interval_seconds=0.0)

            connect_started = threading.Event()
            original_hook = pub.connect_hook
            assert original_hook is not None

            def slow_hook() -> list[str]:
                connect_started.set()
                # Controlled delay inside connect path to simulate slow send/snapshot
                time.sleep(0.15)
                return original_hook()

            pub.set_connect_hook(slow_hook)

            def publisher_worker() -> None:
                assert connect_started.wait(timeout=2.0)
                # Attempt to publish a transition while client is mid-connection
                msg = make_state(zone_id=2, is_motion=True, presence=True)
                bridge.publish_state(msg)

            pub_thread = threading.Thread(target=publisher_worker, daemon=True)
            pub_thread.start()

            received_messages: list[LegacyZoneMessage] = []
            with ws_client.connect(url) as client:
                pub_thread.join(timeout=2.0)

                # Expect 5 messages: 4 snapshot messages followed by 1 transition message
                for _ in range(5):
                    raw = client.recv(timeout=2.0)
                    received_messages.append(LegacyZoneMessage.from_json(raw))

            # 1. Assert that the client received the initial snapshot first (all 4 zones false)
            assert len(received_messages) == 5
            snapshot_msgs = received_messages[:4]
            transition_msg = received_messages[4]

            assert [m.zone for m in snapshot_msgs] == [1, 2, 3, 4]
            assert all(m.occupied is False for m in snapshot_msgs), "Initial snapshot must be all false"

            # 2. Assert transition message arrived after snapshot
            assert transition_msg == LegacyZoneMessage(zone=2, occupied=True)

            # 3. Assert client applying all received messages ends in the same state as adapter.snapshot()
            client_state = {m.zone: m.occupied for m in received_messages}
            expected_state = {m.zone: m.occupied for m in adapter.snapshot()}
            assert client_state == expected_state


class TestDashboardDemoSmokeLegacyZone:
    """Smoke test running dashboard_demo with wire_format='legacy_zone'."""

    def test_dashboard_demo_legacy_zone_smoke(self) -> None:
        """Run dashboard_demo in legacy_zone mode with fast pacing and verify received wire payloads."""
        import dashboard_demo

        test_port = 8794
        ready_event = threading.Event()
        received_messages: list[dict[str, Any]] = []
        client_error: list[Exception] = []

        def client_worker() -> None:
            assert ready_event.wait(timeout=5.0), "Demo server failed to signal ready"
            url = f"ws://127.0.0.1:{test_port}"
            try:
                with ws_client.connect(url) as client:
                    while True:
                        try:
                            raw = client.recv(timeout=1.0)
                            data = json.loads(raw)
                            received_messages.append(data)
                        except Exception:
                            # Stream finished on publisher shutdown
                            break
            except Exception as e:
                client_error.append(e)

        client_thread = threading.Thread(target=client_worker, daemon=True)

        demo_thread = threading.Thread(
            target=dashboard_demo.run_demo,
            kwargs={
                "host": "127.0.0.1",
                "port": test_port,
                "sample_rate_hz": 20.0,
                "zone_count": 4,
                "subcarrier_count": 64,
                "loop": False,
                "realtime": False,
                "ready_event": ready_event,
                "min_clients": 1,
                "wire_format": "legacy_zone",
            },
            daemon=True,
        )

        demo_thread.start()
        client_thread.start()

        demo_thread.join(timeout=15.0)
        client_thread.join(timeout=15.0)

        assert not client_error, f"Client encountered error: {client_error}"
        assert len(received_messages) > 0, "Expected to receive legacy zone messages"

        # Verify every received message has EXACTLY two keys: zone (int) and occupied (bool)
        zones_seen_true: set[int] = set()
        zones_seen_false_after_true: set[int] = set()

        for d in received_messages:
            assert set(d.keys()) == {"zone", "occupied"}
            assert type(d["zone"]) is int
            assert not isinstance(d["zone"], bool)
            assert 1 <= d["zone"] <= 4
            assert type(d["occupied"]) is bool

            z = d["zone"]
            occ = d["occupied"]
            if occ:
                zones_seen_true.add(z)
            elif z in zones_seen_true:
                zones_seen_false_after_true.add(z)

        # Strengthened assertion: every zone 1..4 appears with occupied: true and later false
        assert zones_seen_true == {1, 2, 3, 4}, f"Expected zones 1..4 to be occupied=true, got {zones_seen_true}"
        assert zones_seen_false_after_true == {
            1,
            2,
            3,
            4,
        }, f"Expected zones 1..4 to be occupied=false after true, got {zones_seen_false_after_true}"

        # Verify shutdown flush: when the scenario is already idle, close() produces exactly
        # {zone: 1..N, occupied: false} in order as the final N messages, with nothing after them.
        zone_count = 4
        expected_flush = [{"zone": z, "occupied": False} for z in range(1, zone_count + 1)]
        last_n_messages = received_messages[-zone_count:]
        assert (
            last_n_messages == expected_flush
        ), f"Expected final {zone_count} messages from close() flush to be {expected_flush}, got {last_n_messages}"

