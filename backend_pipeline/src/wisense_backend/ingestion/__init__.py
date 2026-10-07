"""Ingestion layer for WiSense-Twin.

Provides data sources implementing `CSIRecordSource`, starting with
the synthetic generator `SyntheticCSISource`.
"""

from .frame_parser import (
    CSIFrame,
    CSIFrameHeader,
    CSIFrameParser,
    build_frame,
    compute_crc16,
)
from .serial_source import SerialCSISource, SerialSourceConfig
from .synthetic_source import ScenarioConfig, ScenarioEvent, SyntheticCSISource

__all__ = [
    "CSIFrame",
    "CSIFrameHeader",
    "CSIFrameParser",
    "ScenarioConfig",
    "ScenarioEvent",
    "SerialCSISource",
    "SerialSourceConfig",
    "SyntheticCSISource",
    "build_frame",
    "compute_crc16",
]
