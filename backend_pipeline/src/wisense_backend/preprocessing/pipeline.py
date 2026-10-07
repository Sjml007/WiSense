"""Chained preprocessing pipeline for CSI frame streams.

Composes an ordered, config-driven sequence of `Preprocessor` stages (e.g. Hampel
outlier suppression followed by moving average smoothing). The pipeline itself
conforms to the `Preprocessor` protocol (Composite pattern).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from wisense_backend.contracts.csi_source import CSIRecord
from wisense_backend.contracts.preprocessor import Preprocessor
from wisense_backend.preprocessing.hampel_filter import HampelConfig, HampelFilter
from wisense_backend.preprocessing.moving_average import (
    MovingAverageConfig,
    MovingAverageFilter,
)


@dataclass(frozen=True)
class FilterStepConfig:
    """Configuration for an individual filter step within the pipeline.

    Attributes:
        filter_type: Identifier of the filter algorithm ("hampel", "moving_average").
        enabled: If False, this filter step is bypassed completely during pipeline construction.
        parameters: Dictionary of algorithm-specific parameters.
    """

    filter_type: str
    enabled: bool = True
    parameters: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.filter_type or not isinstance(self.filter_type, str):
            raise ValueError("filter_type must be a non-empty string")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FilterStepConfig:
        filter_type = str(data.get("type", data.get("filter_type", "")))
        enabled = bool(data.get("enabled", True))

        # Accept either nested 'parameters' dict or top-level algorithm params
        if "parameters" in data and isinstance(data["parameters"], dict):
            params = dict(data["parameters"])
        else:
            params = {k: v for k, v in data.items() if k not in ("type", "filter_type", "enabled")}

        return cls(filter_type=filter_type, enabled=enabled, parameters=params)


@dataclass(frozen=True)
class PreprocessingConfig:
    """Complete specification of the ordered preprocessing pipeline.

    Attributes:
        filters: Ordered tuple of filter step configurations.
    """

    filters: tuple[FilterStepConfig, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PreprocessingConfig:
        raw_filters = data.get("filters", [])
        filters = tuple(FilterStepConfig.from_dict(f) for f in raw_filters)
        return cls(filters=filters)

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> PreprocessingConfig:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Preprocessing configuration file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


class PreprocessingPipeline(Preprocessor):
    """Composed pipeline executing an ordered sequence of Preprocessor stages.

    Transforms CSI records through each active filter in the order specified
    by configuration. An empty or all-disabled pipeline acts as an identity pass-through.
    """

    def __init__(self, filters: Sequence[Preprocessor]) -> None:
        self.filters: list[Preprocessor] = list(filters)

    @classmethod
    def from_config(cls, config: PreprocessingConfig) -> PreprocessingPipeline:
        """Construct a pipeline from a PreprocessingConfig object."""
        built_filters: list[Preprocessor] = []

        for step in config.filters:
            if not step.enabled:
                continue

            normalized_type = step.filter_type.strip().lower()
            if normalized_type == "hampel":
                h_cfg = HampelConfig.from_dict(step.parameters)
                built_filters.append(HampelFilter(h_cfg))
            elif normalized_type in ("moving_average", "moving-average", "ma"):
                ma_cfg = MovingAverageConfig.from_dict(step.parameters)
                built_filters.append(MovingAverageFilter(ma_cfg))
            else:
                raise ValueError(
                    f"Unknown filter type: '{step.filter_type}'. "
                    f"Supported types: 'hampel', 'moving_average'"
                )

        return cls(filters=built_filters)

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> PreprocessingPipeline:
        """Construct a pipeline directly from a JSON configuration file."""
        cfg = PreprocessingConfig.from_json_file(file_path)
        return cls.from_config(cfg)

    def reset(self) -> None:
        """Reset internal states of all constituent filter stages."""
        for filter_stage in self.filters:
            filter_stage.reset()

    def process(self, record: CSIRecord) -> CSIRecord:
        """Process a record sequentially through all active filter stages."""
        current = record
        for filter_stage in self.filters:
            current = filter_stage.process(current)
        return current

    def process_stream(self, records: Iterable[CSIRecord]) -> Iterator[CSIRecord]:
        """Process an entire stream of records through the pipeline."""
        for record in records:
            yield self.process(record)
