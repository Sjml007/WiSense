"""Contracts and shared interfaces for the WiSense-Twin pipeline.

This module defines the public data classes and Protocol interfaces shared
between ingestion, processing, and downstream components.
"""

from .csi_source import CSIRecord, CSIRecordSource
from .feature_extractor import FeatureExtractor, FeatureVector
from .motion_detector import MotionDetector, MotionState
from .preprocessor import Preprocessor

__all__ = [
    "CSIRecord",
    "CSIRecordSource",
    "Preprocessor",
    "FeatureExtractor",
    "FeatureVector",
    "MotionDetector",
    "MotionState",
]



