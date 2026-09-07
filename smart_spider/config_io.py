# coding=utf-8
"""配置文件加载 / 合并 / 导出（S2）。

合并优先级（高覆盖低）::

    CLI / mapping overrides  >  environment (SMART_SPIDER_*)  >  YAML/JSON file
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional, Type, TypeVar

T = TypeVar("T")

_ENV_PREFIX = "SMART_SPIDER_"


class ConfigIOError(ValueError):
    """Invalid config file or merge payload."""


def _load_raw_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    file_path = Path(path).expanduser()
    text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise ConfigIOError(
                "PyYAML is required for YAML configs; install with: pip install pyyaml"
            ) from exc
        data = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    else:
        # Try YAML then JSON for extension-less / .toml-like misuse.
        try:
            import yaml

            data = yaml.safe_load(text)
        except Exception:
            data = json.loads(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigIOError(f"config root must be a mapping: {file_path}")
    return dict(data)


def _coerce_env_value(raw: str) -> Any:
    text = raw.strip()
    lower = text.lower()
    if lower in {"true", "yes", "1"}:
        return True
    if lower in {"false", "no", "0"}:
        return False
    if lower in {"null", "none", ""}:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if text.startswith("[") or text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return text


def env_overrides(prefix: str = _ENV_PREFIX) -> dict[str, Any]:
    """Map ``SMART_SPIDER_TOTAL_COUNT=10`` → ``{"total_count": 10}``."""
    result: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        field = key[len(prefix):].lower()
        if not field:
            continue
        result[field] = _coerce_env_value(value)
    return result


def deep_merge(base: Mapping[str, Any], *overlays: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for overlay in overlays:
        for key, value in overlay.items():
            if (
                key in merged
                and isinstance(merged[key], dict)
                and isinstance(value, Mapping)
            ):
                merged[key] = deep_merge(merged[key], value)
            else:
                merged[key] = value
    return merged


def load_mapping(
    path: Optional[str | os.PathLike[str]] = None,
    *,
    overrides: Optional[Mapping[str, Any]] = None,
    use_env: bool = True,
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if path:
        data = _load_raw_file(path)
    layers: list[Mapping[str, Any]] = [data]
    if use_env:
        layers.append(env_overrides())
    if overrides:
        layers.append(overrides)
    return deep_merge({}, *layers)


def dump_mapping(
    data: Mapping[str, Any],
    path: str | os.PathLike[str],
    *,
    fmt: Optional[str] = None,
) -> None:
    file_path = Path(path).expanduser()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = (fmt or file_path.suffix.lstrip(".") or "yaml").lower()
    if fmt in {"yaml", "yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise ConfigIOError(
                "PyYAML is required to dump YAML; install with: pip install pyyaml"
            ) from exc
        file_path.write_text(
            yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return
    if fmt == "json":
        file_path.write_text(
            json.dumps(dict(data), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return
    raise ConfigIOError(f"unsupported config format: {fmt}")


def load_dataclass(
    cls: Type[T],
    path: Optional[str | os.PathLike[str]] = None,
    *,
    overrides: Optional[Mapping[str, Any]] = None,
    use_env: bool = True,
) -> T:
    """Load a config dataclass that exposes ``from_mapping``."""
    mapping = load_mapping(path, overrides=overrides, use_env=use_env)
    from_mapping = getattr(cls, "from_mapping", None)
    if from_mapping is None:
        raise ConfigIOError(f"{cls.__name__} does not implement from_mapping()")
    return from_mapping(mapping)
