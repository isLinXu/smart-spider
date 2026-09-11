# coding=utf-8
"""数据集图片采集任务配置（对齐 MultimodalJobConfig）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Mapping, Optional

from .dataset_contracts import LabelPolicy


@dataclass
class DatasetCrawlConfig:
    """图片兼容采集任务的可校验配置。

    CLI / API / 测试应构造本对象后交给 ``DatasetCrawler``；
    仍支持 ``DatasetCrawler(**kwargs)`` 以保持兼容。
    """

    keywords: list[str]
    total_count: int = 1000
    output_dir: str = "./dataset_output"
    batch_size: int = 100
    use_clip: bool = True
    similarity_threshold: float = 0.22
    clip_model: str = "ViT-B/32"
    proxies: Optional[list[str]] = None
    rate: float = 8.0
    max_retries: int = 3
    timeout: int = 10
    connect_timeout: Optional[float] = None
    read_timeout: Optional[float] = None
    max_workers: int = 20
    min_width: int = 200
    min_height: int = 200
    min_variance: float = 10.0
    min_file_size: int = 1024
    search_engines: Optional[list[str]] = None
    site_parsers: Optional[list[str]] = None
    site_start_page: int = 1
    site_end_page: int = 10
    st_sites: Optional[list[str]] = None
    st_tags: str = ""
    st_query: str = ""
    st_pages: Optional[list[int]] = None
    st_limit_per_site: int = 0
    resume: bool = False
    label_mode: str = "hybrid"
    labels: Optional[list[str]] = None
    label_policy: Optional[LabelPolicy] = None
    state_db: Optional[str] = None
    job_id: Optional[str] = None
    disk_guard_mb: int = 100
    query_image: Optional[str] = None
    image_similarity_threshold: float = 0.75
    image_output_format: Optional[str] = "jpg"
    jpeg_quality: int = 95
    max_file_size: int = 12 * 1024 * 1024
    max_image_pixels: int = 50_000_000
    use_curl_cffi: Optional[bool] = None
    allow_private_hosts: bool = False
    max_inflight_pages: Optional[int] = None
    max_inflight_downloads: Optional[int] = None
    max_pending_candidates: Optional[int] = None
    per_domain_concurrency: int = 2
    memory_budget_mb: int = 512
    scene_targets: dict[str, int] = field(default_factory=dict)
    max_source_share: float = 1.0
    max_domain_share: float = 1.0
    scene_quality_gate_enabled: bool = False
    scene_review_queue_path: Optional[str] = None
    license: str = ""
    source_terms: str = ""
    respect_robots: bool = True
    redact_urls: bool = True

    def __post_init__(self) -> None:
        if not self.keywords:
            raise ValueError("keywords must be a non-empty list")
        self.keywords = [str(k).strip() for k in self.keywords if str(k).strip()]
        if not self.keywords:
            raise ValueError("keywords must be a non-empty list")
        if self.total_count <= 0:
            raise ValueError("total_count must be positive")
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if self.max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if self.per_domain_concurrency <= 0:
            raise ValueError("per_domain_concurrency must be positive")
        if self.memory_budget_mb <= 0:
            raise ValueError("memory_budget_mb must be positive")
        if not -1.0 <= self.image_similarity_threshold <= 1.0:
            raise ValueError("image_similarity_threshold must be between -1 and 1")
        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be between 1 and 100")
        if self.max_file_size <= 0 or self.max_file_size < self.min_file_size:
            raise ValueError("max_file_size must be positive and >= min_file_size")
        if self.max_image_pixels <= 0:
            raise ValueError("max_image_pixels must be positive")
        if not 0.0 < self.max_source_share <= 1.0:
            raise ValueError("max_source_share must be in (0, 1]")
        if not 0.0 < self.max_domain_share <= 1.0:
            raise ValueError("max_domain_share must be in (0, 1]")
        if self.scene_review_queue_path is not None and not str(self.scene_review_queue_path).strip():
            raise ValueError("scene_review_queue_path cannot be empty")
        if self.scene_targets:
            normalized: dict[str, int] = {}
            for scene, count in self.scene_targets.items():
                name = str(scene).strip()
                value = int(count)
                if not name or value <= 0:
                    raise ValueError("scene_targets need non-empty names and positive counts")
                normalized[name] = normalized.get(name, 0) + value
            self.scene_targets = normalized
            if sum(self.scene_targets.values()) > self.total_count:
                raise ValueError("total_count cannot be lower than the sum of scene_targets")
        if self.image_output_format is not None:
            fmt = self.image_output_format.strip().lower().lstrip(".")
            if fmt in {"original", "source"}:
                self.image_output_format = None
            elif fmt in {"jpg", "jpeg"}:
                self.image_output_format = "jpg"
            else:
                raise ValueError(
                    "image_output_format must be 'jpg' (default), 'original', or None"
                )
        if self.labels is not None:
            self.labels = [str(x).strip() for x in self.labels if str(x).strip()]
        self.validate()

    def validate(self) -> None:
        """Cross-field constraints previously enforced only in CLI."""
        if self.scene_quality_gate_enabled and not self.scene_targets:
            raise ValueError(
                "scene_quality_gate_enabled requires non-empty scene_targets"
            )
        if self.scene_targets and self.total_count < sum(self.scene_targets.values()):
            raise ValueError("total_count cannot be lower than the sum of scene_targets")
        if self.query_image and not self.use_clip:
            raise ValueError("query_image requires use_clip=True")

    def compliance_policy(self):
        from .compliance import CompliancePolicy

        return CompliancePolicy(
            license=self.license,
            source_terms=self.source_terms,
            respect_robots=self.respect_robots,
            redact_urls=self.redact_urls,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.label_policy is not None:
            data["label_policy"] = self.label_policy.to_dict()
        return data

    def fingerprint(self) -> str:
        from .dataset_lineage import config_fingerprint

        return config_fingerprint(self.to_dict())

    def crawler_kwargs(self) -> dict[str, Any]:
        """Keyword arguments accepted by ``DatasetCrawler.__init__`` (sans config)."""
        skip = {
            "label_mode",
            "labels",
            "license",
            "source_terms",
            "respect_robots",
            "redact_urls",
        }
        kwargs: dict[str, Any] = {}
        for item in fields(self):
            if item.name in skip:
                continue
            kwargs[item.name] = getattr(self, item.name)
        if self.label_policy is None:
            kwargs["label_mode"] = self.label_mode
            kwargs["labels"] = self.labels
        else:
            kwargs["label_policy"] = self.label_policy
        return kwargs

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DatasetCrawlConfig":
        known = {f.name for f in fields(cls)}
        payload = {k: v for k, v in raw.items() if k in known}
        if "label_policy" in payload and isinstance(payload["label_policy"], dict):
            payload["label_policy"] = LabelPolicy.from_dict(payload["label_policy"])
        return cls(**payload)
