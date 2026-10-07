"""Automated integration tests for scripts/dashboard_demo.py."""

import sys
import threading
import time
from pathlib import Path

import pytest
import websockets.sync.client as ws_client

# Ensure scripts and backend_pipeline/src are importable
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
BACKEND_SRC = ROOT_DIR / "backend_pipeline" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import dashboard_demo
from wisense_backend.contracts.state_message import StateMessage


class TestDashboardDemoScenario:
    """Validate scenario construction and calibration in dashboard_demo."""

    def test_build_scenario_config_structure(self) -> None:
        """Verify scenario duration and event sequencing across N zones."""
        cfg = dashboard_demo.build_scenario_config(
            zone_count=4,
            subcarrier_count=64,
            sample_rate_hz=20.0,
            realtime=False,
        )
        assert cfg.zone_count == 4
        assert cfg.subcarrier_count == 64
        assert cfg.sample_rate_hz == 20.0
        assert cfg.realtime is False
        assert len(cfg.events) == 4
        # Check event sequence
        for idx, event in enumerate(cfg.events, start=1):
            assert event.zone_id == idx
            assert event.end_time_s > event.start_time_s
            assert event.motion_type == "walking"

    def test_dynamic_calibration_arbitrary_zones(self) -> None:
        """Verify dynamic calibration works for arbitrary zone counts (e.g. 3 zones)."""
        profile = dashboard_demo.load_or_calibrate_profile(
            profile_path=None,
            zone_count=3,
            subcarrier_count=64,
            sample_rate_hz=20.0,
        )
        assert profile.zone_count == 3
        assert profile.subcarrier_count == 64
        assert len(profile.zone_signatures) == 3
        for z in (1, 2, 3):
            assert z in profile.zone_signatures


class TestDashboardDemoEndToEndStream:
    """Validate full end-to-end WebSocket publishing and client consumption."""

    def test_demo_stream_and_client_reception(self) -> None:
        """Run dashboard_demo in a background thread and connect a WebSocket client."""
        test_port = 8799
        received_messages: list[StateMessage] = []
        client_error: list[Exception] = []
        ready_event = threading.Event()

        def client_worker() -> None:
            assert ready_event.wait(timeout=5.0), "Publisher failed to signal ready"
            url = f"ws://127.0.0.1:{test_port}"
            try:
                with ws_client.connect(url) as client:
                    for _ in range(50):
                        raw = client.recv(timeout=5.0)
                        msg = StateMessage.from_json(raw)
                        received_messages.append(msg)
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
                "realtime": False,  # fast execution
                "ready_event": ready_event,
                "min_clients": 1,
            },
            daemon=True,
        )

        demo_thread.start()
        client_thread.start()

        client_thread.join(timeout=10.0)
        demo_thread.join(timeout=10.0)

        assert not client_error, f"Client encountered error: {client_error}"
        assert len(received_messages) == 50

        # Validate wire contract invariants on all received messages
        for msg in received_messages:
            assert msg.schema_version == 1
            assert msg.timestamp > 0.0
            assert isinstance(msg.is_motion, bool)
            assert isinstance(msg.presence, bool)

            if not msg.is_motion:
                assert msg.zone_id is None, "Motion-gating invariant: zone_id must be None when is_motion is False"
                assert msg.separation_margin is None, "Motion-gating invariant: separation_margin must be None when is_motion is False"
            else:
                assert msg.zone_id is not None
                assert 1 <= msg.zone_id <= 4
                assert msg.separation_margin is not None
                assert msg.separation_margin >= 0.0

    def test_full_scenario_all_zones_streamed(self) -> None:
        """Run full scenario pass and verify all 4 zones and presence states appear."""
        test_port = 8798
        all_messages: list[StateMessage] = []
        client_error: list[Exception] = []
        ready_event = threading.Event()

        def client_worker() -> None:
            assert ready_event.wait(timeout=5.0), "Publisher failed to signal ready"
            url = f"ws://127.0.0.1:{test_port}"
            try:
                with ws_client.connect(url) as client:
                    while True:
                        try:
                            raw = client.recv(timeout=1.0)
                            msg = StateMessage.from_json(raw)
                            all_messages.append(msg)
                        except Exception:
                            # Stream ended (publisher closed on completion)
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
            },
            daemon=True,
        )

        demo_thread.start()
        client_thread.start()

        demo_thread.join(timeout=10.0)
        client_thread.join(timeout=10.0)

        assert not client_error, f"Client error: {client_error}"
        assert len(all_messages) > 500, f"Expected >500 messages, got {len(all_messages)}"

        # Verify all 4 zones were observed during motion
        active_zones_seen = {m.zone_id for m in all_messages if m.zone_id is not None}
        assert active_zones_seen == {1, 2, 3, 4}, f"Expected zones {1,2,3,4}, got {active_zones_seen}"

        # Verify presence persistence: periods where is_motion is False but presence is True
        presence_tail_messages = [m for m in all_messages if not m.is_motion and m.presence]
        assert len(presence_tail_messages) > 0, "Expected presence timeout decay frames"
        for m in presence_tail_messages:
            assert m.zone_id is None, "Motion-gating invariant: zone_id must be None during presence decay"
            assert m.separation_margin is None

