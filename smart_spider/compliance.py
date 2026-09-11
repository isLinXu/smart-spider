# coding=utf-8
"""合规辅助：URL 脱敏、许可字段、发布清单（ADR-0016）。

不替代站点 ToS 审查；只把可审计字段落到产物里。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .dataset_contracts import utc_now

PUBLISH_CHECKLIST_FILENAME = ".publish_checklist.json"

_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "key",
        "password",
        "secret",
        "session",
        "signature",
        "sig",
        "token",
    }
)


def redact_url(url: str) -> str:
    """去掉 fragment，并剔除常见敏感 query 参数。"""
    value = (url or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if not parts.scheme and not parts.netloc:
        return value.split("#", 1)[0]
    filtered = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        if key.casefold() in _SENSITIVE_QUERY_KEYS:
            continue
        filtered.append((key, item))
    query = urlencode(filtered, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


@dataclass
class CompliancePolicy:
    """任务级合规声明（写入配置快照与样本 provenance）。"""

    license: str = ""
    source_terms: str = ""
    respect_robots: bool = True
    redact_urls: bool = True
    allow_hosts: tuple[str, ...] = ()
    deny_hosts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "license": self.license,
            "source_terms": self.source_terms,
            "respect_robots": self.respect_robots,
            "redact_urls": self.redact_urls,
            "allow_hosts": list(self.allow_hosts),
            "deny_hosts": list(self.deny_hosts),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "CompliancePolicy":
        data = dict(raw or {})
        return cls(
            license=str(data.get("license") or ""),
            source_terms=str(data.get("source_terms") or data.get("terms") or ""),
            respect_robots=bool(data.get("respect_robots", True)),
            redact_urls=bool(data.get("redact_urls", True)),
            allow_hosts=tuple(data.get("allow_hosts") or ()),
            deny_hosts=tuple(data.get("deny_hosts") or ()),
        )


def apply_compliance_to_provenance(
    provenance: dict[str, Any],
    policy: CompliancePolicy,
) -> dict[str, Any]:
    """就地补充许可字段并可选脱敏 URL；返回同一 dict。"""
    out = provenance
    if policy.license:
        out.setdefault("license", policy.license)
    if policy.source_terms:
        out.setdefault("source_terms", policy.source_terms)
    out["respect_robots"] = bool(policy.respect_robots)
    if policy.redact_urls and out.get("url"):
        original = str(out.get("url") or "")
        redacted = redact_url(original)
        out["url"] = redacted
        if redacted != original:
            out["url_redacted"] = True
    return out


@dataclass
class PublishChecklist:
    """数据集发布前的合规自检清单。"""

    job_id: str
    created_at: str = field(default_factory=utc_now)
    license: str = ""
    source_terms: str = ""
    respect_robots: bool = True
    redact_urls: bool = True
    allow_hosts: list[str] = field(default_factory=list)
    deny_hosts: list[str] = field(default_factory=list)
    route_counts: dict[str, int] = field(default_factory=dict)
    route_reasons: dict[str, int] = field(default_factory=dict)
    block_kinds: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def evaluate(self) -> "PublishChecklist":
        warnings: list[str] = []
        if not (self.license or "").strip():
            warnings.append("license_missing")
        if not (self.source_terms or "").strip():
            warnings.append("source_terms_missing")
        if not self.respect_robots:
            warnings.append("robots_disabled")
        if not self.redact_urls:
            warnings.append("url_redaction_disabled")
        if self.block_kinds.get("challenge", 0) > 0:
            warnings.append("challenge_pages_observed")
        self.warnings = warnings
        self.ready = len(warnings) == 0
        return self


def build_publish_checklist(
    *,
    job_id: str,
    policy: CompliancePolicy,
    route_counts: Optional[Mapping[str, int]] = None,
    route_reasons: Optional[Mapping[str, int]] = None,
    block_kinds: Optional[Mapping[str, int]] = None,
) -> PublishChecklist:
    checklist = PublishChecklist(
        job_id=job_id,
        license=policy.license,
        source_terms=policy.source_terms,
        respect_robots=policy.respect_robots,
        redact_urls=policy.redact_urls,
        allow_hosts=list(policy.allow_hosts),
        deny_hosts=list(policy.deny_hosts),
        route_counts=dict(route_counts or {}),
        route_reasons=dict(route_reasons or {}),
        block_kinds=dict(block_kinds or {}),
    )
    return checklist.evaluate()


def write_publish_checklist(output_dir: str, checklist: PublishChecklist) -> str:
    path = os.path.join(output_dir, PUBLISH_CHECKLIST_FILENAME)
    os.makedirs(os.path.abspath(output_dir), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(checklist.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path
