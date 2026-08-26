# coding=utf-8
"""通用多模态数据集任务编排器。

编排顺序：Discovery → SQLite 候选登记 → 页面/媒体处理 → 可选标注
→ 资产本地化 → manifest + SQLite 幂等提交。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from io import BytesIO
from typing import Any, Iterable, Optional

from loguru import logger
from PIL import Image

from .dataset_contracts import (
    CandidateResource,
    LabelPolicy,
    Modality,
    ModalityAsset,
    ModalityRelation,
    QualityMetrics,
    SampleRecord,
    normalize_url,
)
from .dataset_state import DatasetStateStore
from .http_client import SmartHttpClient
from .multimodal_pipeline import (
    AdaptiveSourceRouter,
    AnnotationRouter,
    DiscoveryTask,
    PageSampleExtractor,
    SourceCallable,
    StaticPageSource,
)
from .multimodal_scale import QualityReport, ShardedManifestWriter


class AssetMaterializationError(RuntimeError):
    """媒体资产下载、验证或原子保存失败。"""


class AssetStore:
    """将远程图片保存为内容哈希路径，避免重复下载和文件名冲突。"""

    def __init__(self, output_dir: str, max_image_bytes: int = 25 * 1024 * 1024):
        self.root = os.path.join(output_dir, "assets")
        self.max_image_bytes = max_image_bytes
        self._lock = threading.Lock()
        os.makedirs(self.root, exist_ok=True)

    def save_image(self, content: bytes) -> tuple[str, str, str]:
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise AssetMaterializationError("image content must be bytes-like")
        content = bytes(content)
        if len(content) > self.max_image_bytes:
            raise AssetMaterializationError("image exceeds max_image_bytes")
        try:
            image = Image.open(BytesIO(content))
            image.verify()
            image_format = (image.format or "JPEG").lower()
        except Exception as exc:
            raise AssetMaterializationError(f"invalid image: {exc}") from exc

        extension = {
            "jpeg": ".jpg",
            "jpg": ".jpg",
            "png": ".png",
            "webp": ".webp",
            "gif": ".gif",
            "avif": ".avif",
        }.get(image_format, ".bin")
        digest = hashlib.sha256(content).hexdigest()
        relative = os.path.join("assets", digest[:2], digest + extension)
        absolute = os.path.join(os.path.dirname(self.root), relative)
        os.makedirs(os.path.dirname(absolute), exist_ok=True)

        with self._lock:
            if not os.path.exists(absolute):
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=os.path.dirname(absolute),
                        prefix=".asset-",
                        suffix=".tmp",
                        delete=False,
                    ) as handle:
                        temporary = handle.name
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, absolute)
                except Exception:
                    if temporary:
                        try:
                            os.unlink(temporary)
                        except OSError:
                            pass
                    raise
        return relative, f"image/{'jpeg' if image_format == 'jpg' else image_format}", digest


class MultimodalManifestWriter(ShardedManifestWriter):
    """兼容旧接口的多模态 manifest 写入器。"""

    def __init__(self, output_dir: str, shard_size: int = 0):
        super().__init__(output_dir, shard_size=shard_size, prefix="manifest")


@dataclass
class MultimodalJobConfig:
    output_dir: str
    job_id: str = ""
    max_samples: int = 1000
    state_db: Optional[str] = None
    materialize_images: bool = True
    max_image_bytes: int = 25 * 1024 * 1024
    manifest_shard_size: int = 0
    annotation_batch_size: int = 32
    quality_report_path: Optional[str] = None
    allowed_modalities: tuple[Modality, ...] = (
        Modality.TEXT,
        Modality.IMAGE,
        Modality.WEBPAGE,
    )
    label_policy: LabelPolicy = field(default_factory=LabelPolicy)

    def __post_init__(self):
        self.allowed_modalities = tuple(
            item if isinstance(item, Modality) else Modality(str(item))
            for item in self.allowed_modalities
        )
        if self.max_samples <= 0:
            raise ValueError("max_samples must be positive")
        if self.manifest_shard_size < 0:
            raise ValueError("manifest_shard_size must be non-negative")
        if self.annotation_batch_size <= 0:
            raise ValueError("annotation_batch_size must be positive")
        if not self.job_id:
            self.job_id = hashlib.sha256(
                os.path.abspath(self.output_dir).encode("utf-8")
            ).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_dir": self.output_dir,
            "job_id": self.job_id,
            "max_samples": self.max_samples,
            "materialize_images": self.materialize_images,
            "max_image_bytes": self.max_image_bytes,
            "manifest_shard_size": self.manifest_shard_size,
            "annotation_batch_size": self.annotation_batch_size,
            "allowed_modalities": [item.value for item in self.allowed_modalities],
            "label_policy": self.label_policy.to_dict(),
        }


@dataclass
class MultimodalJobReport:
    job_id: str
    tasks: int = 0
    discovered: int = 0
    accepted: int = 0
    rejected: int = 0
    materialized_images: int = 0
    quality_report_path: str = ""
    route_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MultimodalDatasetOrchestrator:
    """执行通用多模态任务，保持来源、存储、模型和状态相互解耦。"""

    def __init__(
        self,
        config: MultimodalJobConfig,
        http_client: Optional[SmartHttpClient] = None,
        extractor: Optional[PageSampleExtractor] = None,
        source_router: Optional[AdaptiveSourceRouter] = None,
        annotation_router: Optional[AnnotationRouter] = None,
        state_store: Optional[DatasetStateStore] = None,
        manifest_writer: Optional[MultimodalManifestWriter] = None,
        image_fetcher: Optional[Any] = None,
    ):
        self.config = config
        os.makedirs(config.output_dir, exist_ok=True)
        self.http_client = http_client
        self.extractor = extractor or PageSampleExtractor()
        self.source_router = source_router or AdaptiveSourceRouter()
        self.annotation_router = annotation_router or AnnotationRouter(config.label_policy)
        self.state = state_store or DatasetStateStore(
            config.state_db or os.path.join(config.output_dir, ".dataset_state.sqlite3")
        )
        self.state.create_job(config.job_id, config.to_dict())
        self.manifest = manifest_writer or MultimodalManifestWriter(
            config.output_dir,
            shard_size=config.manifest_shard_size,
        )
        self.asset_store = AssetStore(config.output_dir, config.max_image_bytes)
        self.quality_report = QualityReport()
        self.quality_report_path = self._resolve_quality_report_path(config.quality_report_path)
        self.image_fetcher = image_fetcher or (
            http_client.get_bytes if http_client is not None else None
        )
        self._owns_state = state_store is None
        self._owns_manifest = manifest_writer is None

    def _resolve_quality_report_path(self, path: Optional[str]) -> str:
        if not path:
            return os.path.join(self.config.output_dir, "quality_report.json")
        return path if os.path.isabs(path) else os.path.join(self.config.output_dir, path)

    def close(self):
        try:
            self.quality_report.write(self.quality_report_path)
        finally:
            if self._owns_manifest:
                self.manifest.close()
            if self._owns_state:
                self.state.close()

    def run(
        self,
        tasks: Iterable[DiscoveryTask],
        static_source: SourceCallable,
        browser_source: Optional[SourceCallable] = None,
    ) -> MultimodalJobReport:
        report = MultimodalJobReport(
            job_id=self.config.job_id,
            quality_report_path=self.quality_report_path,
        )
        try:
            for task in tasks:
                if report.accepted >= self.config.max_samples:
                    break
                report.tasks += 1
                result = self.source_router.discover(task, static_source, browser_source)
                route = result.decision.action.value
                report.route_counts[route] = report.route_counts.get(route, 0) + 1
                self.quality_report.observe_route(route)
                self._record_source_diagnostics(
                    result.static_response,
                    source="static",
                    report=report,
                )
                if result.browser_response is not None:
                    self._record_source_diagnostics(
                        result.browser_response,
                        source="browser",
                        report=report,
                    )
                report.discovered += len(result.candidates)

                candidate_ids = self._register_candidates(result.candidates, task)
                sample = self._select_source_sample(result)
                if sample is not None:
                    page_id = self._register_page_candidate(task, sample)
                    results = self._submit_samples(
                        [(sample, candidate_ids, page_id)], report
                    )
                    if results and results[0]:
                        report.accepted += 1
                    else:
                        report.rejected += 1
                    continue

                pending = []
                for candidate in result.candidates:
                    if report.accepted + len(pending) >= self.config.max_samples:
                        break
                    sample = self._sample_from_candidate(candidate)
                    if sample is None:
                        report.rejected += 1
                        self.quality_report.observe_rejection("sample_generation_failed")
                        continue
                    pending.append((sample, candidate_ids, None))
                    if len(pending) >= self.config.annotation_batch_size:
                        accepted = self._submit_samples(pending, report)
                        report.accepted += sum(1 for item in accepted if item)
                        report.rejected += sum(1 for item in accepted if not item)
                        pending = []
                if pending:
                    accepted = self._submit_samples(pending, report)
                    report.accepted += sum(1 for item in accepted if item)
                    report.rejected += sum(1 for item in accepted if not item)
        finally:
            self.close()
        return report

    @staticmethod
    def _select_source_sample(result) -> Optional[SampleRecord]:
        if result.browser_response and result.browser_response.sample:
            return result.browser_response.sample
        return result.static_response.sample

    def _record_source_diagnostics(
        self,
        response: Any,
        source: str,
        report: MultimodalJobReport,
    ):
        diagnostics = []
        if response.error:
            diagnostics.append(f"{source}: {response.error}")
        metadata_errors = response.metadata.get("errors", []) if response.metadata else []
        for error in metadata_errors:
            diagnostics.append(f"{source}: {error}")
        for diagnostic in diagnostics:
            if diagnostic not in report.errors:
                report.errors.append(diagnostic)
            self.quality_report.observe_error(diagnostic)

    def _register_candidates(
        self,
        candidates: Iterable[CandidateResource],
        task: DiscoveryTask,
    ) -> dict[str, str]:
        ids = {}
        for candidate in candidates:
            if candidate.modality not in self.config.allowed_modalities:
                continue
            try:
                ids[candidate.candidate_id] = self.state.add_candidate(
                    self.config.job_id, candidate
                )
            except Exception as exc:
                logger.warning(f"candidate registration failed: {exc}")
        return ids

    def _register_page_candidate(self, task: DiscoveryTask, sample: SampleRecord) -> Optional[str]:
        url = sample.provenance.get("url") or task.start_url
        if not url:
            return None
        candidate = CandidateResource(
            url=url,
            source=sample.provenance.get("source", task.source or "page"),
            modality=Modality.WEBPAGE,
            query=task.query,
            title=sample.provenance.get("title", ""),
            source_meta={"sample_id": sample.sample_id, "role": "page"},
        )
        try:
            return self.state.add_candidate(self.config.job_id, candidate)
        except Exception as exc:
            logger.warning(f"page candidate registration failed: {exc}")
            return None

    def _sample_from_candidate(self, candidate: CandidateResource) -> Optional[SampleRecord]:
        if candidate.modality in {Modality.WEBPAGE, Modality.TEXT}:
            if self.http_client is None:
                return None
            page_source = StaticPageSource(self.http_client, self.extractor)
            response = page_source(DiscoveryTask(
                query=candidate.query,
                start_url=candidate.url,
                source=candidate.source,
                modalities=self.config.allowed_modalities,
            ))
            return response.sample

        asset_id = "asset-" + hashlib.sha256(candidate.url.encode("utf-8")).hexdigest()[:16]
        assets = [ModalityAsset(
            modality=candidate.modality,
            role="input",
            uri=candidate.url,
            asset_id=asset_id,
            mime_type=f"{candidate.modality.value}/unknown",
            metadata={"title": candidate.title, "alt": candidate.alt},
        )]
        if candidate.query or candidate.title:
            text_id = asset_id + "-context"
            assets.append(ModalityAsset(
                modality=Modality.TEXT,
                role="context",
                text=candidate.title or candidate.query,
                asset_id=text_id,
                metadata={"query": candidate.query},
            ))
            relations = [ModalityRelation(
                source=text_id,
                target=asset_id,
                relation="context-of",
            )]
        else:
            relations = []
        sample_id = "sha256:" + hashlib.sha256(
            f"{candidate.candidate_id}:{candidate.query}".encode("utf-8")
        ).hexdigest()
        return SampleRecord(
            sample_id=sample_id,
            file=candidate.url if candidate.modality == Modality.IMAGE else "",
            provenance={
                "source": candidate.source,
                "query": candidate.query,
                "url": candidate.url,
            },
            pipeline={"extractor": "candidate", "status": "discovered"},
            modalities=assets,
            relations=relations,
            task_type="image_text_alignment" if candidate.modality == Modality.IMAGE else "multimodal",
        )

    def _submit_sample(
        self,
        sample: SampleRecord,
        candidate_ids: dict[str, str],
        page_id: Optional[str],
        report: MultimodalJobReport,
    ) -> bool:
        results = self._submit_samples([(sample, candidate_ids, page_id)], report)
        return bool(results and results[0])

    def _submit_samples(
        self,
        entries: list[tuple[SampleRecord, dict[str, str], Optional[str]]],
        report: MultimodalJobReport,
    ) -> list[bool]:
        """先完成媒体准备，再以 batch 方式调用标注后端并提交样本。"""
        statuses = [False] * len(entries)
        prepared: list[tuple[int, SampleRecord, list[str], Optional[str]]] = []
        for index, (sample, candidate_ids, page_id) in enumerate(entries):
            try:
                claimed = self._prepare_sample(sample, candidate_ids, report)
                prepared.append((index, sample, claimed, page_id))
            except Exception as exc:
                self._reject_sample(sample, [], page_id, str(exc), report)

        try:
            annotations = self._annotate_samples(
                [item[1] for item in prepared]
            )
        except Exception as exc:
            for _, sample, claimed, page_id in prepared:
                self._reject_sample(sample, claimed, page_id, str(exc), report)
            return statuses
        for index, sample, claimed, page_id in prepared:
            annotation = annotations.get(sample.sample_id)
            if annotation is None:
                self._reject_sample(
                    sample,
                    claimed,
                    page_id,
                    "annotation_result_missing",
                    report,
                )
                continue
            statuses[index] = self._commit_sample(
                sample,
                claimed,
                page_id,
                annotation,
                report,
            )
        return statuses

    def _prepare_sample(
        self,
        sample: SampleRecord,
        candidate_ids: dict[str, str],
        report: MultimodalJobReport,
    ) -> list[str]:
        claimed: list[str] = []
        for asset in sample.modalities:
            if asset.modality != Modality.IMAGE or not asset.uri.startswith(("http://", "https://")):
                continue
            candidate_id = candidate_ids.get(
                hashlib.sha256(normalize_url(asset.uri).encode("utf-8")).hexdigest()
            )
            if candidate_id is None:
                nested_candidate = CandidateResource(
                    url=asset.uri,
                    source=sample.provenance.get("source", "page"),
                    modality=Modality.IMAGE,
                    query=sample.provenance.get("query", ""),
                    alt=str(asset.metadata.get("alt", "")),
                    source_meta={"sample_id": sample.sample_id, "role": asset.role},
                )
                candidate_id = self.state.add_candidate(
                    self.config.job_id, nested_candidate
                )
                candidate_ids[nested_candidate.candidate_id] = candidate_id
            if candidate_id and self.state.claim_candidate(candidate_id):
                claimed.append(candidate_id)
            if not self.config.materialize_images:
                continue
            if self.image_fetcher is None:
                raise AssetMaterializationError("image_fetcher is not configured")
            content = self.image_fetcher(asset.uri)
            relative, mime_type, digest = self.asset_store.save_image(content)
            original_url = asset.uri
            asset.uri = relative
            asset.mime_type = mime_type
            asset.metadata["source_url"] = original_url
            asset.metadata["content_hash"] = digest
            if sample.file == original_url:
                sample.file = relative
            report.materialized_images += 1
            self.quality_report.observe_materialized_image()
            if candidate_id:
                self.state.complete_candidate(candidate_id)
        return claimed

    def _annotate_samples(self, samples: list[SampleRecord]):
        if not samples:
            return {}
        batch_method = getattr(self.annotation_router, "annotate_batch", None)
        if callable(batch_method):
            return batch_method(samples)
        return {
            sample.sample_id: self.annotation_router.annotate(sample)
            for sample in samples
        }

    def _commit_sample(
        self,
        sample: SampleRecord,
        claimed: list[str],
        page_id: Optional[str],
        annotation: Any,
        report: MultimodalJobReport,
    ) -> bool:
        try:
            sample.labels = annotation.resolution.labels
            sample.pipeline["annotation"] = {
                "used_backends": annotation.used_backends,
                "errors": annotation.errors,
                "candidates": [item.to_dict() for item in annotation.resolution.candidates],
            }
            for error in annotation.errors.values():
                self.quality_report.observe_error(error)
            sample.pipeline["status"] = "accepted"
            primary_candidate = page_id or (claimed[0] if claimed else None)
            accepted = self.state.add_sample(
                self.config.job_id,
                sample,
                candidate_id=primary_candidate,
                content_hash=sample.sample_id,
            )
            if not accepted:
                self._reject_sample(
                    sample,
                    claimed,
                    page_id,
                    "duplicate_or_existing_sample",
                    report,
                )
                return False
            self.manifest.write(sample)
            for candidate_id in claimed:
                if candidate_id != primary_candidate:
                    try:
                        self.state.complete_candidate(candidate_id)
                    except Exception:
                        pass
            self.quality_report.observe_sample(sample)
            return True
        except Exception as exc:
            self._reject_sample(sample, claimed, page_id, str(exc), report)
            return False

    def _reject_sample(
        self,
        sample: SampleRecord,
        claimed: list[str],
        page_id: Optional[str],
        reason: str,
        report: MultimodalJobReport,
    ):
        logger.warning(f"multimodal sample rejected: {reason}")
        report.errors.append(reason)
        self.quality_report.observe_rejection(reason)
        self.quality_report.observe_error(reason)
        for candidate_id in claimed:
            try:
                self.state.fail_candidate(candidate_id, reason)
            except Exception:
                pass
        if page_id:
            try:
                self.state.fail_candidate(page_id, reason)
            except Exception:
                pass
