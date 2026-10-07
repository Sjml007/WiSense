"""Unit and integration tests for WebSocketStatePublisher."""

import json
import time
from pathlib import Path
from typing import Callable

import pytest
import websockets.sync.client as ws_client
from websockets.exceptions import ConnectionClosed

from wisense_backend.calibration.calibrator import ZoneCalibrator
from wisense_backend.calibration.labeled_sample import LabeledFeatureSample
from wisense_backend.classification.nearest_signature_classifier import (
    NearestSignatureZoneClassifier,
)
from wisense_backend.communication.websocket_server import (
    WebSocketConfig,
    WebSocketStatePublisher,
)
from wisense_backend.contracts.feature_extractor import FeatureVector
from wisense_backend.contracts.state_message import StateMessage
from wisense_backend.contracts.websocket_publisher import WebSocketPublisher
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
from wisense_backend.preprocessing.pipeline import PreprocessingPipeline


def wait_for_condition(
    predicate: Callable[[], bool],
    timeout: float = 2.0,
    interval: float = 0.02,
) -> bool:
    """Poll predicate until True or timeout reached."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestWebSocketConfig:
    """Tests for WebSocketConfig loading and validation."""

    def test_default_config(self) -> None:
        """Verify default configuration parameters."""
        cfg = WebSocketConfig()
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8765

    def test_from_dict_and_to_dict(self) -> None:
        """Verify roundtrip conversion to/from dictionary."""
        d = {"host": "0.0.0.0", "port": 9000}
        cfg = WebSocketConfig.from_dict(d)
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 9000
        assert cfg.to_dict() == d

    def test_from_json_file(self) -> None:
        """Verify loading from the project's canonical config/websocket_config.json."""
        config_path = Path(__file__).resolve().parent.parent / "config" / "websocket_config.json"
        assert config_path.is_file()
        cfg = WebSocketConfig.from_json_file(config_path)
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8765

    def test_validation_errors(self) -> None:
        """Verify invalid host and port inputs are rejected."""
        with pytest.raises(ValueError, match="host must be a non-empty string"):
            WebSocketConfig(host="")

        with pytest.raises(ValueError, match="host must be a non-empty string"):
            WebSocketConfig(host="   ")

        with pytest.raises(TypeError, match="port must be an integer"):
            WebSocketConfig(port="8765")  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="port must be an integer"):
            WebSocketConfig(port=True)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="port must be in range"):
            WebSocketConfig(port=-1)

        with pytest.raises(ValueError, match="port must be in range"):
            WebSocketConfig(port=70000)


class TestWebSocketPublisherProtocol:
    """Tests checking protocol conformance and server lifecycle."""

    def test_protocol_conformance(self) -> None:
        """Verify WebSocketStatePublisher satisfies the WebSocketPublisher protocol."""
        pub = WebSocketStatePublisher()
        assert isinstance(pub, WebSocketPublisher)

    def test_lifecycle_and_idempotence(self) -> None:
        """Verify clean start, repeated start, and repeated stop."""
        pub = WebSocketStatePublisher(WebSocketConfig(port=0))
        assert not pub.is_running
        assert pub.actual_port is None

        pub.start()
        assert pub.is_running
        assert pub.actual_port is not None and pub.actual_port > 0

        # Repeated start is safe
        pub.start()
        assert pub.is_running

        pub.stop()
        assert not pub.is_running
        assert pub.actual_port is None

        # Repeated stop is safe
        pub.stop()
        assert not pub.is_running

    def test_context_manager(self) -> None:
        """Verify start and stop via context manager."""
        with WebSocketStatePublisher(WebSocketConfig(port=0)) as pub:
            assert pub.is_running
            assert pub.actual_port is not None
        assert not pub.is_running

    def test_publish_not_running_raises(self) -> None:
        """Verify publishing when not running raises RuntimeError."""
        pub = WebSocketStatePublisher(WebSocketConfig(port=0))
        msg = StateMessage(
            schema_version=1,
            timestamp=1700000000.0,
            zone_id=None,
            is_motion=False,
            presence=False,
            separation_margin=None,
        )
        with pytest.raises(RuntimeError, match="WebSocketStatePublisher is not running"):
            pub.publish(msg)

    def test_publish_wrong_type_raises(self) -> None:
        """Verify publishing a non-StateMessage object raises TypeError."""
        with WebSocketStatePublisher(WebSocketConfig(port=0)) as pub:
            with pytest.raises(TypeError, match="Expected StateMessage"):
                pub.publish({"not": "a StateMessage"})  # type: ignore[arg-type]


class TestWebSocketStreaming:
    """Tests for live message delivery, broadcasting, and disconnect resilience."""

    def test_zero_client_publish_is_safe_noop(self) -> None:
        """Verify publishing with 0 clients returns immediately without error."""
        with WebSocketStatePublisher(WebSocketConfig(port=0)) as pub:
            assert pub.client_count == 0
            msg = StateMessage(
                schema_version=1,
                timestamp=1700000000.0,
                zone_id=None,
                is_motion=False,
                presence=False,
                separation_margin=None,
            )
            # Must not raise or hang
            pub.publish(msg)
            assert pub.client_count == 0

    def test_single_client_receive(self) -> None:
        """Verify a connected client receives a broadcast StateMessage faithfully."""
        with WebSocketStatePublisher(WebSocketConfig(port=0)) as pub:
            port = pub.actual_port
            url = f"ws://127.0.0.1:{port}"

            with ws_client.connect(url) as client:
                assert wait_for_condition(lambda: pub.client_count == 1)

                msg = StateMessage(
                    schema_version=1,
                    timestamp=1700000005.123,
                    zone_id=2,
                    is_motion=True,
                    presence=True,
                    separation_margin=0.034567,
                    calibration_version=1,
                )
                pub.publish(msg)

                raw = client.recv(timeout=2.0)
                assert isinstance(raw, str)
                received_dict = json.loads(raw)

                assert received_dict["schema_version"] == 1
                assert received_dict["timestamp"] == 1700000005.123
                assert received_dict["zone_id"] == 2
                assert received_dict["is_motion"] is True
                assert received_dict["presence"] is True
                assert received_dict["separation_margin"] == 0.034567
                assert received_dict["calibration_version"] == 1

                # Deserialize back into StateMessage
                received_msg = StateMessage.from_json(raw)
                assert received_msg == msg

    def test_multi_client_broadcast(self) -> None:
        """Verify multiple connected clients all receive the broadcast StateMessage."""
        with WebSocketStatePublisher(WebSocketConfig(port=0)) as pub:
            port = pub.actual_port
            url = f"ws://127.0.0.1:{port}"

            with ws_client.connect(url) as client1:
                with ws_client.connect(url) as client2:
                    with ws_client.connect(url) as client3:
                        assert wait_for_condition(lambda: pub.client_count == 3)

                        msg = StateMessage(
                            schema_version=1,
                            timestamp=1700000010.0,
                            zone_id=1,
                            is_motion=True,
                            presence=True,
                            separation_margin=0.15,
                            calibration_version=2,
                        )
                        pub.publish(msg)

                        for client in (client1, client2, client3):
                            raw = client.recv(timeout=2.0)
                            received_msg = StateMessage.from_json(raw)
                            assert received_msg == msg

    def test_client_disconnect_resilience(self) -> None:
        """Verify that disconnecting one client does not interrupt publishing to other clients."""
        with WebSocketStatePublisher(WebSocketConfig(port=0)) as pub:
            port = pub.actual_port
            url = f"ws://127.0.0.1:{port}"

            client1 = ws_client.connect(url, legacy=True)
            client2 = ws_client.connect(url, legacy=True)

            try:
                assert wait_for_condition(lambda: pub.client_count == 2)

                # Client 1 closes abruptly
                client1.close()
                assert wait_for_condition(lambda: pub.client_count == 1)

                # Publisher broadcasts a new message
                msg = StateMessage(
                    schema_version=1,
                    timestamp=1700000020.0,
                    zone_id=None,
                    is_motion=False,
                    presence=True,
                    separation_margin=None,
                    calibration_version=2,
                )
                pub.publish(msg)

                # Client 2 receives the message intact
                raw2 = client2.recv(timeout=2.0)
                received2 = StateMessage.from_json(raw2)
                assert received2 == msg
            finally:
                client2.close()


class TestEndToEndPipelineIntegration:
    """Test full 8-stage pipeline output streamed over WebSocket."""

    def test_full_pipeline_stream_to_websocket(self) -> None:
        """Feed synthetic data through preprocessing, features, motion, and classification,

        and stream resulting state decisions over live WebSocket connection.
        """
        # 1. Pipeline stages setup
        from wisense_backend.preprocessing.hampel_filter import HampelConfig, HampelFilter
        from wisense_backend.preprocessing.moving_average import (
            MovingAverageConfig,
            MovingAverageFilter,
        )

        prep = PreprocessingPipeline(
            filters=[
                HampelFilter(HampelConfig(window_size=7)),
                MovingAverageFilter(MovingAverageConfig(window_size=5)),
            ]
        )
        feat_ext = VarianceFeatureExtractor(
            VarianceExtractorConfig(window_size_samples=10, window_step_samples=1)
        )
        detector = ThresholdMotionDetector(
            ThresholdMotionConfig(
                enter_threshold=0.08,
                exit_threshold=0.04,
                presence_timeout_seconds=2.0,
            )
        )

        # 2. Synthetic calibration profile
        sig_zone1 = tuple([0.50] * 20 + [0.02] * 32)
        sig_zone2 = tuple([0.02] * 30 + [0.50] * 22)

        from wisense_backend.contracts.calibration import CalibrationProfile, ZoneSignature

        sig1 = ZoneSignature(
            zone_id=1,
            subcarrier_count=52,
            mean_variance_per_subcarrier=sig_zone1,
            mean_aggregate_variance=sum(sig_zone1),
            sample_count=10,
        )
        sig2 = ZoneSignature(
            zone_id=2,
            subcarrier_count=52,
            mean_variance_per_subcarrier=sig_zone2,
            mean_aggregate_variance=sum(sig_zone2),
            sample_count=10,
        )
        profile = CalibrationProfile(
            zone_count=2,
            subcarrier_count=52,
            calibration_version=1,
            created_at="2026-09-15T00:00:00Z",
            source="synthetic",
            zone_signatures={1: sig1, 2: sig2},
        )
        classifier = NearestSignatureZoneClassifier(profile)

        # 3. Stream through WebSocket
        with WebSocketStatePublisher(WebSocketConfig(port=0)) as pub:
            url = f"ws://127.0.0.1:{pub.actual_port}"

            with ws_client.connect(url) as client:
                assert wait_for_condition(lambda: pub.client_count == 1)

                scenario = ScenarioConfig(
                    scenario_name="test_ws",
                    duration_seconds=2.0,
                    sample_rate_hz=25.0,
                    subcarrier_count=52,
                    zone_count=2,
                    realtime=False,
                    start_epoch_time=1700000000.0,
                    events=(ScenarioEvent(start_time_s=0.2, end_time_s=1.8, zone_id=2),),
                )

                messages_sent = 0
                with SyntheticCSISource(scenario) as source:
                    for raw_record in source.records():
                        clean_record = prep.process(raw_record)
                        feature_vec = feat_ext.process(clean_record)
                        if feature_vec is None:
                            continue

                        motion_state = detector.process(feature_vec)
                        class_result = classifier.classify(feature_vec, motion_state)

                        state_msg = StateMessage(
                            schema_version=1,
                            timestamp=feature_vec.window_end_timestamp,
                            zone_id=class_result.zone_id,
                            is_motion=motion_state.is_motion,
                            presence=motion_state.presence,
                            separation_margin=class_result.separation_margin,
                            calibration_version=profile.calibration_version,
                        )
                        pub.publish(state_msg)
                        messages_sent += 1

                        raw_payload = client.recv(timeout=2.0)
                        client_msg = StateMessage.from_json(raw_payload)

                        assert client_msg.schema_version == 1
                        assert client_msg.is_motion == motion_state.is_motion
                        assert client_msg.presence == motion_state.presence
                        assert client_msg.zone_id == class_result.zone_id
                        assert client_msg.calibration_version == profile.calibration_version

                assert messages_sent > 30, f"Expected >30 messages, got {messages_sent}"
