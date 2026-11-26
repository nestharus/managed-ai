"""Typed immutable configuration objects for the published source library.

Replaces scattered module-level limit constants with frozen dataclasses that
can be constructed from defaults, dicts, JSON files, or environment variables.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _require_positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _require_nonempty_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


@dataclass(frozen=True)
class LibraryLimits:
    """Byte and cardinality bounds for published libraries and archives."""

    max_manifest_bytes: int = 4 * 1024 * 1024
    max_body_bytes: int = 2 * 1024 * 1024
    max_bodies: int = 2048
    max_modules: int = 256
    max_package_bytes: int = 32 * 1024 * 1024
    max_archive_bytes: int = 40 * 1024 * 1024
    max_edition_bytes: int = 512
    max_title_bytes: int = 1024
    max_summary_bytes: int = 4096
    max_text_bytes: int = 16384
    max_references_per_body: int = 2048
    max_reference_target_bytes: int = 8192
    max_reference_anchor_bytes: int = 1024
    max_parent_role_bytes: int = 4096
    max_external_participation: int = 256
    read_chunk_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        for attr in (
            "max_manifest_bytes",
            "max_body_bytes",
            "max_bodies",
            "max_modules",
            "max_package_bytes",
            "max_archive_bytes",
            "max_edition_bytes",
            "max_title_bytes",
            "max_summary_bytes",
            "max_text_bytes",
            "max_references_per_body",
            "max_reference_target_bytes",
            "max_reference_anchor_bytes",
            "max_parent_role_bytes",
            "max_external_participation",
            "read_chunk_bytes",
        ):
            _require_positive_int(getattr(self, attr), attr)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LibraryLimits:
        allowed = set(cls.__dataclass_fields__)
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown LibraryLimits fields: {sorted(unknown)}")
        return cls(**dict(data))


@dataclass(frozen=True)
class SearchConfig:
    """Lexical nomination, FTS5 analyzer, and producer cache configuration."""

    policy: str = "knowledge-search/1"
    index_format: str = "knowledge-search-index/1"
    max_index_bytes: int = 40 * 1024 * 1024
    default_limit: int = 20
    max_limit: int = 100
    default_scan_limit: int = 512
    default_max_bytes: int = 8 * 1024 * 1024
    max_query_bytes: int = 2048
    max_distinct_units: int = 128
    fts_weights: tuple[int, int, int, int] = (1, 5, 2, 1)
    fts_tokenizer: str = "porter unicode61"
    max_retained_catalogues: int = 8

    def __post_init__(self) -> None:
        _require_nonempty_str(self.policy, "policy")
        _require_nonempty_str(self.index_format, "index_format")
        _require_nonempty_str(self.fts_tokenizer, "fts_tokenizer")
        for attr in (
            "max_index_bytes",
            "default_limit",
            "max_limit",
            "default_scan_limit",
            "default_max_bytes",
            "max_query_bytes",
            "max_distinct_units",
            "max_retained_catalogues",
        ):
            _require_positive_int(getattr(self, attr), attr)

        if self.default_limit > self.max_limit:
            raise ValueError("default_limit cannot exceed max_limit")

        if (
            not isinstance(self.fts_weights, tuple)
            or len(self.fts_weights) != 4
            or any(type(w) is not int or w <= 0 for w in self.fts_weights)
        ):
            raise ValueError("fts_weights must be a 4-tuple of positive integers")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SearchConfig:
        allowed = set(cls.__dataclass_fields__)
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown SearchConfig fields: {sorted(unknown)}")

        normalized = dict(data)
        if "fts_weights" in normalized and isinstance(normalized["fts_weights"], list):
            normalized["fts_weights"] = tuple(normalized["fts_weights"])
        return cls(**normalized)


@dataclass(frozen=True)
class BatchConfig:
    """Bounds for shared-index multi-query discovery batches."""

    max_queries: int = 32
    max_input_bytes: int = 128 * 1024
    max_result_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        _require_positive_int(self.max_queries, "max_queries")
        _require_positive_int(self.max_input_bytes, "max_input_bytes")
        _require_positive_int(self.max_result_bytes, "max_result_bytes")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BatchConfig:
        allowed = set(cls.__dataclass_fields__)
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown BatchConfig fields: {sorted(unknown)}")
        return cls(**dict(data))


@dataclass(frozen=True)
class LibraryConfig:
    """Top-level configuration bundle for the source library domain."""

    library_format: str = "knowledge-library/1"
    selection_format: str = "knowledge-selection/1"
    limits: LibraryLimits = field(default_factory=LibraryLimits)
    search: SearchConfig = field(default_factory=SearchConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.library_format, "library_format")
        _require_nonempty_str(self.selection_format, "selection_format")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LibraryConfig:
        if not isinstance(data, Mapping):
            raise ValueError("LibraryConfig mapping required")

        allowed = {"library_format", "selection_format", "limits", "search", "batch"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown LibraryConfig fields: {sorted(unknown)}")

        kwargs: dict[str, Any] = {}
        if "library_format" in data:
            kwargs["library_format"] = data["library_format"]
        if "selection_format" in data:
            kwargs["selection_format"] = data["selection_format"]
        if "limits" in data:
            raw_limits = data["limits"]
            kwargs["limits"] = (
                raw_limits
                if isinstance(raw_limits, LibraryLimits)
                else LibraryLimits.from_dict(raw_limits)
            )
        if "search" in data:
            raw_search = data["search"]
            kwargs["search"] = (
                raw_search
                if isinstance(raw_search, SearchConfig)
                else SearchConfig.from_dict(raw_search)
            )
        if "batch" in data:
            raw_batch = data["batch"]
            kwargs["batch"] = (
                raw_batch
                if isinstance(raw_batch, BatchConfig)
                else BatchConfig.from_dict(raw_batch)
            )

        return cls(**kwargs)

    @classmethod
    def from_json(cls, path: Path | str) -> LibraryConfig:
        from managed_ai.platform import strict_json_loads

        raw = Path(path).read_bytes()
        return cls.from_dict(strict_json_loads(raw, path, require_object=True))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LibraryConfig:
        source = os.environ if env is None else env
        search_overrides: dict[str, Any] = {}
        batch_overrides: dict[str, Any] = {}

        env_map: tuple[tuple[str, dict[str, Any], str], ...] = (
            (
                "MANAGED_AI_LIBRARY_MAX_RETAINED_CATALOGUES",
                search_overrides,
                "max_retained_catalogues",
            ),
            ("MANAGED_AI_LIBRARY_DEFAULT_SCAN_LIMIT", search_overrides, "default_scan_limit"),
            ("MANAGED_AI_LIBRARY_DEFAULT_MAX_BYTES", search_overrides, "default_max_bytes"),
            ("MANAGED_AI_LIBRARY_MAX_BATCH_QUERIES", batch_overrides, "max_queries"),
            ("MANAGED_AI_LIBRARY_MAX_BATCH_RESULT_BYTES", batch_overrides, "max_result_bytes"),
        )

        for env_key, bucket, field_name in env_map:
            raw_val = source.get(env_key)
            if raw_val is not None:
                try:
                    bucket[field_name] = int(raw_val)
                except ValueError:
                    raise ValueError(f"{env_key} must be an integer") from None

        search_cfg = (
            SearchConfig.from_dict(search_overrides) if search_overrides else SearchConfig()
        )
        batch_cfg = BatchConfig.from_dict(batch_overrides) if batch_overrides else BatchConfig()
        return cls(search=search_cfg, batch=batch_cfg)


DEFAULT_LIBRARY_CONFIG = LibraryConfig()
