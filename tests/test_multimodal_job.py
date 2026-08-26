# coding=utf-8
"""通用多模态任务编排器测试。"""
import io
import json
import os
import tempfile

from PIL import Image

from smart_spider.dataset_contracts import CandidateResource, Modality
from smart_spider.multimodal_job import MultimodalDatasetOrchestrator, MultimodalJobConfig
from smart_spider.multimodal_pipeline import DiscoveryTask, PageSampleExtractor, SourceResponse


HTML = """
<html><head><title>猫咪页面</title></head>
<body><p>这是用于多模态训练的数据页面正文。</p>
<img src="https://cdn.example.com/cat.jpg" alt="一只猫">
</body></html>
"""


def _jpeg_bytes():
    image = Image.new("RGB", (64, 64))
    image.putdata([(x * 3 % 255, y * 4 % 255, (x + y) % 255) for y in range(64) for x in range(64)])
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


class _BytesHttpClient:
    def get_bytes(self, url):
        return _jpeg_bytes()


def test_orchestrator_materializes_page_assets_and_commits_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        extractor = PageSampleExtractor(min_text_chars=10)
        sample = extractor.extract_page(
            "https://example.com/article/1",
            HTML,
            source="test",
            query="cat",
        )
        candidate = CandidateResource(
            "https://cdn.example.com/cat.jpg",
            "test",
            modality=Modality.IMAGE,
            query="cat",
        )

        def static_source(task):
            return SourceResponse(
                candidates=[candidate],
                html_length=len(HTML),
                sample=sample,
            )

        job = MultimodalDatasetOrchestrator(
            MultimodalJobConfig(output_dir=tmp, max_samples=1),
            http_client=_BytesHttpClient(),
            extractor=extractor,
        )
        report = job.run(
            [DiscoveryTask(query="cat", start_url="https://example.com/article/1")],
            static_source,
        )

        assert report.accepted == 1
        assert report.materialized_images == 1
        assert report.quality_report_path == os.path.join(tmp, "quality_report.json")
        with open(report.quality_report_path, encoding="utf-8") as handle:
            quality = json.load(handle)
        assert quality["accepted"] == 1
        assert quality["materialized_images"] == 1
        with open(os.path.join(tmp, "manifest.jsonl"), encoding="utf-8") as handle:
            manifest = json.loads(handle.readline())
        image_asset = next(item for item in manifest["modalities"] if item["modality"] == "image")
        assert image_asset["uri"].startswith("assets/")
        assert os.path.exists(os.path.join(tmp, image_asset["uri"]))
        assert manifest["task_type"] == "image_text_alignment"


def test_orchestrator_can_keep_remote_assets_for_reference_only():
    with tempfile.TemporaryDirectory() as tmp:
        extractor = PageSampleExtractor(min_text_chars=10)
        sample = extractor.extract_page("https://example.com/a", HTML, query="cat")
        candidate = CandidateResource(
            "https://cdn.example.com/cat.jpg", "test", modality=Modality.IMAGE
        )

        job = MultimodalDatasetOrchestrator(
            MultimodalJobConfig(
                output_dir=tmp,
                max_samples=1,
                materialize_images=False,
                manifest_shard_size=1,
            ),
        )
        report = job.run(
            [DiscoveryTask(start_url="https://example.com/a")],
            lambda task: SourceResponse(candidates=[candidate], sample=sample, html_length=1000),
        )
        assert report.accepted == 1
        with open(os.path.join(tmp, "manifest-00000.jsonl"), encoding="utf-8") as handle:
            manifest = json.loads(handle.readline())
        image_asset = next(item for item in manifest["modalities"] if item["modality"] == "image")
        assert image_asset["uri"].startswith("https://")
