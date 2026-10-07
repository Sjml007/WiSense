"""Shared preprocessor interface for the WiSense-Twin pipeline.

This module establishes the `Preprocessor` protocol implemented by all signal
conditioning and noise reduction algorithms (Hampel filter, Moving Average filter,
etc.), ensuring swappable, composable, and config-driven filter pipelines.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Protocol, runtime_checkable

from wisense_backend.contracts.csi_source import CSIRecord


@runtime_checkable
class Preprocessor(Protocol):
    """Abstract protocol for CSI frame conditioning and filtering.

    Every filtering stage in the preprocessing pipeline must conform to this
    protocol, allowing individual filters to be swapped, reordered, or chained
    without coupling downstream components to concrete implementations.
    """

    def process(self, record: CSIRecord) -> CSIRecord:
        """Process a single CSI record and return the cleaned equivalent.

        The preprocessor must preserve all record attributes that are not
        specifically conditioned (timestamp, subcarrier_count, rssi, channel,
        sequence_number), updating only the signal features (amplitudes, and
        optionally phases).

        Args:
            record: The raw or upstream-filtered `CSIRecord`.

        Returns:
            A new `CSIRecord` containing filtered amplitudes/phases and
            original metadata.
        """
        ...

    def reset(self) -> None:
        """Reset internal filter state, clearing rolling window buffers."""
        ...

    def process_stream(self, records: Iterable[CSIRecord]) -> Iterator[CSIRecord]:
        """Convenience method to transform a stream of CSI records.

        Args:
            records: An iterable of `CSIRecord` instances.

        Yields:
            Cleaned `CSIRecord` instances.
        """
        ...
