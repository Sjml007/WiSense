#!/usr/bin/env python3
"""Main entry point for the WiSense-Twin backend pipeline.

Bootstraps the backend Application, loads configuration, configures structured logging,
and handles operating system termination signals for graceful shutdown.
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from typing import Optional, Sequence

# Ensure src/ is on Python search path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from wisense_backend.app import Application


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command line arguments for the application runner."""
    parser = argparse.ArgumentParser(
        description="WiSense-Twin Backend Pipeline Service",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default="config/system_config.json",
        help="Path to system configuration JSON file",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main execution function."""
    args = parse_args(argv)

    try:
        app = Application(config_path=args.config)
    except Exception as err:
        sys.stderr.write(f"Failed to initialize WiSense-Twin application: {err}\n")
        return 1

    # Register OS signal handlers for graceful shutdown
    def _signal_handler(signum: int, frame: object) -> None:
        sys.stderr.write(f"\nReceived signal {signum}, initiating graceful shutdown...\n")
        app.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)

    try:
        processed = app.run()
        sys.stdout.write(f"Pipeline executed successfully. Processed {processed} records.\n")
        return 0
    except KeyboardInterrupt:
        app.stop()
        return 0
    except Exception as err:
        sys.stderr.write(f"Pipeline execution failed: {err}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
