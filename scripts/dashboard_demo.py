#!/usr/bin/env python3
"""WiSense-Twin Developer Dashboard Verification Demo (Section 12).

Streams real-time synthetic CSI data through the complete 8-stage pipeline:
1. Ingestion: SyntheticCSISource (paced at 20 Hz with wall-clock time)
2. Preprocessing: PreprocessingPipeline (Hampel outlier filter + Moving average)
3. Feature Extraction: VarianceFeatureExtractor (sliding window of subcarrier variance)
4. Motion Detection: ThresholdMotionDetector (dual-threshold hysteresis + presence timeout)
5. Zone Calibration & Classification: NearestSignatureZoneClassifier
6. Publishing: WebSocketStatePublisher (streaming StateMessage wire contracts)

Allows developers to open `dashboard/index.html` in a web browser, connect to
the WebSocket server, and visually verify pipeline behavior end-to-end.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Sequence

# Resolve backend_pipeline/src path
ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_SRC = ROOT_DIR / "backend_pipeline" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from wisense_backend.calibration.calibrator import ZoneCalibrator
from wisense_backend.calibration.labeled_sample import LabeledFeatureSample
from wisense_backend.classification.nearest_signature_classifier import (
    NearestSignatureZoneClassifier,
    ZoneClassifierConfig,
)
from wisense_backend.communication.legacy_zone_adapter import (
    LegacyZoneAdapter,
    LegacyZoneBridge,
)
from wisense_backend.communication.websocket_server import (
    WebSocketConfig,
    WebSocketStatePublisher,
)
from wisense_backend.contracts.calibration import CalibrationProfile
from wisense_backend.contracts.state_message import StateMessage
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


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments for the dashboard verification demo."""
    parser = argparse.ArgumentParser(
        description="WiSense-Twin Dashboard Demo: Streams synthetic multi-zone CSI pipeline over WebSocket",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host address to bind WebSocket publisher to",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="TCP port to bind WebSocket publisher to (default: 8765)",
    )
    parser.add_argument(
        "--sample-rate",
        type=float,
        default=20.0,
        help="Sampling rate in Hz for synthetic frame generation",
    )
    parser.add_argument(
        "--zones",
        type=int,
        default=4,
        help="Number of spatial zones in synthetic scenario",
    )
    parser.add_argument(
        "--subcarriers",
        type=int,
        default=64,
        help="Subcarrier count per CSI record",
    )
    parser.add_argument(
        "--calibration-profile",
        type=str,
        default=None,
        help="Optional path to CalibrationProfile JSON file (auto-calibrates if omitted)",
    )
    parser.add_argument(
        "--no-loop",
        action="store_true",
        default=False,
        help="Run scenario once instead of looping continuously",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        default=False,
        help="Run without realtime wall-clock pacing (for headless/automated tests)",
    )
    parser.add_argument(
        "--wire-format",
        type=str,
        choices=["state_message_v1", "legacy_zone"],
        default="state_message_v1",
        help="Wire protocol format to publish ('state_message_v1' or 'legacy_zone')",
    )
    return parser.parse_args(argv)


def build_scenario_config(
    zone_count: int,
    subcarrier_count: int,
    sample_rate_hz: float,
    realtime: bool = True,
) -> ScenarioConfig:
    """Construct a repeatable multi-zone scenario with quiet intervals and presence tails.

    Timeline:
    - 0.0s .. 3.0s: Quiet baseline (all zones clear)
    - For each zone z in 1..zone_count:
        * 4.0s active walking motion in zone z
        * 3.0s quiet transition (presence decay timeout verification)
    """
    events: list[ScenarioEvent] = []
    current_time = 3.0
    motion_duration = 4.0
    quiet_duration = 3.0

    for z in range(1, zone_count + 1):
        events.append(
            ScenarioEvent(
                start_time_s=current_time,
                end_time_s=current_time + motion_duration,
                zone_id=z,
                motion_type="walking",
                intensity=1.0,
            )
        )
        current_time += motion_duration + quiet_duration

    total_duration = current_time + 1.0

    return ScenarioConfig(
        scenario_name="dashboard_demo_multizone",
        description=f"Sequential motion across {zone_count} zones with quiet intervals for dashboard verification",
        sample_rate_hz=sample_rate_hz,
        subcarrier_count=subcarrier_count,
        zone_count=zone_count,
        duration_seconds=total_duration,
        realtime=realtime,
        random_seed=42,
        baseline_amplitude=30.0,
        noise_std=0.25,
        events=tuple(events),
    )


def build_preprocessing_pipeline() -> PreprocessingPipeline:
    """Instantiate preprocessing pipeline from config or default filters."""
    prep_config_path = ROOT_DIR / "backend_pipeline" / "config" / "preprocessing_config.json"
    if prep_config_path.is_file():
        return PreprocessingPipeline.from_json_file(prep_config_path)

    from wisense_backend.preprocessing.hampel_filter import HampelConfig, HampelFilter
    from wisense_backend.preprocessing.moving_average import (
        MovingAverageConfig,
        MovingAverageFilter,
    )
    return PreprocessingPipeline([
        HampelFilter(HampelConfig(window_size=7, threshold_multiplier=3.0)),
        MovingAverageFilter(MovingAverageConfig(window_size=5)),
    ])


def load_or_calibrate_profile(
    profile_path: Optional[str | Path],
    zone_count: int,
    subcarrier_count: int,
    sample_rate_hz: float,
) -> CalibrationProfile:
    """Load existing calibration profile or run an offline calibration pass."""
    default_path = (
        ROOT_DIR
        / "backend_pipeline"
        / "config"
        / "calibration_profiles"
        / "default_calibration.json"
    )

    candidate = Path(profile_path) if profile_path else default_path

    if candidate.is_file():
        try:
            profile = CalibrationProfile.from_json_file(candidate)
            if profile.zone_count == zone_count and profile.subcarrier_count == subcarrier_count:
                sys.stdout.write(f"Loaded calibration profile from {candidate} (v{profile.calibration_version})\n")
                return profile
        except Exception as e:
            sys.stderr.write(f"Could not load {candidate} ({e}), running dynamic calibration...\n")

    # Dynamic calibration pass
    sys.stdout.write(f"Generating calibration profile for {zone_count} zones, {subcarrier_count} subcarriers...\n")
    offline_scenario = build_scenario_config(
        zone_count=zone_count,
        subcarrier_count=subcarrier_count,
        sample_rate_hz=sample_rate_hz,
        realtime=False,
    )

    with SyntheticCSISource(offline_scenario) as source:
        raw_records = list(source)

    prep = build_preprocessing_pipeline()
    clean_records = list(prep.process_stream(raw_records))

    feat_ext = VarianceFeatureExtractor(VarianceExtractorConfig(window_size_samples=20, window_step_samples=1))
    feature_vectors = list(feat_ext.process_stream(clean_records))

    start_epoch = raw_records[0].timestamp
    labeled_samples: list[LabeledFeatureSample] = []
    for fv in feature_vectors:
        midpoint = (fv.window_start_timestamp + fv.window_end_timestamp) / 2.0
        rel_t = midpoint - start_epoch
        active_events = [e for e in offline_scenario.events if e.start_time_s <= rel_t <= e.end_time_s]
        if len(active_events) == 1:
            labeled_samples.append(LabeledFeatureSample(fv, zone_id=active_events[0].zone_id))
        elif len(active_events) == 0:
            labeled_samples.append(LabeledFeatureSample(fv, zone_id=None))

    calibrator = ZoneCalibrator(
        zone_count=zone_count,
        subcarrier_count=subcarrier_count,
        calibration_version=1,
    )
    profile = calibrator.calibrate(labeled_samples)
    sys.stdout.write(f"Dynamic calibration complete: {len(profile.zone_signatures)} zone signatures created.\n")
    return profile


def run_demo(
    host: str = "127.0.0.1",
    port: int = 8765,
    sample_rate_hz: float = 20.0,
    zone_count: int = 4,
    subcarrier_count: int = 64,
    profile_path: Optional[str] = None,
    loop: bool = True,
    realtime: bool = True,
    ready_event: Optional[threading.Event] = None,
    min_clients: int = 0,
    wire_format: str = "state_message_v1",
    resync_interval_seconds: float = 5.0,
) -> None:
    """Run the live WebSocket streaming verification demo."""
    stop_event = False

    def _sigint_handler(signum: int, frame: object) -> None:
        nonlocal stop_event
        sys.stdout.write("\nReceived shutdown signal. Stopping WebSocket publisher...\n")
        stop_event = True

    if threading.current_thread() is threading.main_thread():
        try:
            signal.signal(signal.SIGINT, _sigint_handler)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, _sigint_handler)
        except (ValueError, AttributeError):
            pass

    # 1. Prepare Calibration & Classifier
    profile = load_or_calibrate_profile(
        profile_path=profile_path,
        zone_count=zone_count,
        subcarrier_count=subcarrier_count,
        sample_rate_hz=sample_rate_hz,
    )
    classifier = NearestSignatureZoneClassifier(
        profile,
        config=ZoneClassifierConfig(metric="cosine_distance", min_separation_margin=0.02),
    )

    # 2. Configure WebSocket Publisher
    ws_config = WebSocketConfig(
        host=host,
        port=port,
        message_format=wire_format,
        legacy_resync_interval_seconds=resync_interval_seconds,
    )
    publisher = WebSocketStatePublisher(ws_config)
    publisher.start()

    actual_port = publisher.actual_port or port
    ws_url = f"ws://{host}:{actual_port}"

    # 3. Setup LegacyZoneBridge if legacy_zone wire format requested
    bridge: Optional[LegacyZoneBridge] = None
    if wire_format == "legacy_zone":
        adapter = LegacyZoneAdapter(zone_count=zone_count)
        bridge = LegacyZoneBridge(
            adapter=adapter,
            publisher=publisher,
            resync_interval_seconds=resync_interval_seconds,
        )

    if ready_event is not None:
        ready_event.set()

    sys.stdout.write("\n" + "=" * 70 + "\n")
    sys.stdout.write("  WiSense-Twin Developer Dashboard Verification Demo (Section 12/17B)\n")
    sys.stdout.write("=" * 70 + "\n")
    sys.stdout.write(f"  WebSocket Endpoint: {ws_url}\n")
    sys.stdout.write(f"  Wire Format:        {wire_format}\n")
    if wire_format == "legacy_zone":
        sys.stdout.write("  Notice:             Legacy Unreal Zone Protocol Active\n")
        sys.stdout.write("  Instructions:\n")
        sys.stdout.write(f"    1. Connect the Unreal Engine prototype client to: {ws_url}\n")
        sys.stdout.write("    2. Observe incremental zone updates: {\"zone\": <int>, \"occupied\": <bool>}\n")
        sys.stdout.write("    3. Note: The web browser dashboard (/dashboard/index.html) speaks StateMessage v1 only\n")
        sys.stdout.write("    4. Press Ctrl+C in this terminal to stop cleanly\n")
    else:
        sys.stdout.write(f"  Dashboard File:     {ROOT_DIR / 'dashboard' / 'index.html'}\n")
        sys.stdout.write("  Instructions:\n")
        sys.stdout.write("    1. Open dashboard/index.html in your web browser\n")
        sys.stdout.write(f"    2. Ensure endpoint field is set to: {ws_url}\n")
        sys.stdout.write("    3. Click 'Connect' to observe real-time StateMessage telemetry\n")
        sys.stdout.write("    4. Press Ctrl+C in this terminal to stop cleanly\n")
    sys.stdout.write("=" * 70 + "\n\n")

    if min_clients > 0:
        sys.stdout.write(f"Waiting for at least {min_clients} client(s) to connect...\n")
        deadline = time.time() + 10.0
        while publisher.client_count < min_clients and time.time() < deadline and not stop_event:
            time.sleep(0.05)

    iteration = 0
    total_frames_published = 0

    try:
        while not stop_event:
            iteration += 1
            if loop and iteration > 1:
                sys.stdout.write(f"\n--- Scenario Loop #{iteration} Restarting ---\n")

            scenario = build_scenario_config(
                zone_count=zone_count,
                subcarrier_count=subcarrier_count,
                sample_rate_hz=sample_rate_hz,
                realtime=realtime,
            )

            # Initialize fresh per-run pipeline filters
            preprocessor = build_preprocessing_pipeline()
            feat_extractor = VarianceFeatureExtractor(
                VarianceExtractorConfig(window_size_samples=20, window_step_samples=1)
            )
            motion_detector = ThresholdMotionDetector(
                ThresholdMotionConfig(
                    enter_threshold=0.10,
                    exit_threshold=0.05,
                    presence_timeout_seconds=2.0,
                )
            )

            last_report_time = 0.0

            with SyntheticCSISource(scenario) as source:
                for raw_record in source:
                    if stop_event:
                        break

                    clean_record = preprocessor.process(raw_record)
                    feat_vector = feat_extractor.process(clean_record)
                    if feat_vector is None:
                        continue

                    motion_state = motion_detector.process(feat_vector)
                    class_result = classifier.classify(feat_vector, motion_state)

                    state_msg = StateMessage(
                        schema_version=1,
                        timestamp=time.time(),
                        zone_id=class_result.zone_id,
                        is_motion=motion_state.is_motion,
                        presence=motion_state.presence,
                        separation_margin=class_result.separation_margin,
                        calibration_version=profile.calibration_version,
                    )

                    if bridge is not None:
                        bridge.publish_state(state_msg)
                    else:
                        publisher.publish(state_msg)
                    total_frames_published += 1

                    # Periodic console reporting (every ~1 second)
                    now = time.time()
                    if now - last_report_time >= 1.0:
                        last_report_time = now
                        zone_str = f"Zone {state_msg.zone_id}" if state_msg.zone_id is not None else "None"
                        margin_str = f"{state_msg.separation_margin:.4f}" if state_msg.separation_margin is not None else "None"
                        sys.stdout.write(
                            f"[Stream #{total_frames_published:04d}] "
                            f"Motion={state_msg.is_motion!s:<5} | "
                            f"Presence={state_msg.presence!s:<5} | "
                            f"Zone={zone_str:<7} | "
                            f"Margin={margin_str:<7} | "
                            f"Clients={publisher.client_count}\n"
                        )
                        sys.stdout.flush()

            if not loop:
                sys.stdout.write("Single scenario pass completed.\n")
                break

    finally:
        if bridge is not None:
            try:
                bridge.close()
                # Best-effort bounded flush (200ms): in-flight all-false close frames
                # drain across OS and network buffers to connected clients before server shutdown.
                time.sleep(0.20)
            except Exception as e:
                sys.stderr.write(f"Error during bridge close: {e}\n")
        publisher.stop()
        sys.stdout.write(f"\nPublisher stopped. Total frames published: {total_frames_published}\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main execution function."""
    args = parse_args(argv)
    try:
        run_demo(
            host=args.host,
            port=args.port,
            sample_rate_hz=args.sample_rate,
            zone_count=args.zones,
            subcarrier_count=args.subcarriers,
            profile_path=args.calibration_profile,
            loop=not args.no_loop,
            realtime=not args.fast,
            wire_format=args.wire_format,
        )
        return 0
    except Exception as err:
        sys.stderr.write(f"Error running dashboard demo: {err}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
