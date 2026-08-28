# coding=utf-8
"""DatasetCrawler 单元测试。"""
import io
import json
import os
import shutil
import tempfile
import threading

import pytest
from PIL import Image

from smart_spider.dataset_crawler import (
    DatasetCrawler,
    DatasetDirManager,
    MetadataWriter,
    ProgressManager,
)
from smart_spider.dataset_contracts import LabelPolicy
from smart_spider.smart_spider import UrlDeduplicator


# ──────────────────────────────────────────────────────────────────────────────
# DatasetDirManager 测试
# ──────────────────────────────────────────────────────────────────────────────

class TestDatasetDirManager:
    """分桶目录管理器测试。"""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_basic_path_assignment(self):
        """基本路径分配。"""
        dm = DatasetDirManager(self.tmp, batch_size=100)
        path = dm.get_save_path("http://example.com/a.jpg", ".jpg")
        assert "batch_0000" in path
        assert os.path.basename(path).startswith("0000_")
        assert path.endswith(".jpg")

    def test_batch_transition(self):
        """跨桶过渡：第 100 张应进入 batch_0100。"""
        dm = DatasetDirManager(self.tmp, batch_size=100)
        for i in range(100):
            dm.get_save_path(f"http://example.com/img{i}.jpg", ".jpg")
        # 第 101 张
        path = dm.get_save_path("http://example.com/img100.jpg", ".jpg")
        assert "batch_0100" in path
        assert os.path.basename(path).startswith("0100_")

    def test_custom_batch_size(self):
        """自定义分桶大小。"""
        dm = DatasetDirManager(self.tmp, batch_size=50)
        for i in range(50):
            dm.get_save_path(f"http://example.com/img{i}.jpg", ".jpg")
        path = dm.get_save_path("http://example.com/img50.jpg", ".jpg")
        assert "batch_0050" in path

    def test_thread_safety(self):
        """多线程并发分配路径。"""
        dm = DatasetDirManager(self.tmp, batch_size=100)
        paths = []
        lock = threading.Lock()

        def assign(idx):
            path = dm.get_save_path(f"http://example.com/img{idx}.jpg", ".jpg")
            with lock:
                paths.append(path)

        threads = [threading.Thread(target=assign, args=(i,)) for i in range(200)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(paths) == 200
        assert dm.saved_count == 200
        # 所有路径唯一
        assert len(set(paths)) == 200

    def test_list_batches(self):
        """列出分桶目录。"""
        dm = DatasetDirManager(self.tmp, batch_size=100)
        for i in range(150):
            dm.get_save_path(f"http://example.com/img{i}.jpg", ".jpg")
        batches = dm.list_batches()
        assert batches == ["batch_0000", "batch_0100"]

    def test_count_existing_images(self):
        """统计已有图片数量。"""
        dm = DatasetDirManager(self.tmp, batch_size=100)
        # 创建一些空文件模拟已有图片
        batch_dir = os.path.join(self.tmp, "batch_0000")
        os.makedirs(batch_dir, exist_ok=True)
        for i in range(5):
            with open(os.path.join(batch_dir, f"{i:04d}_test.jpg"), "w") as f:
                f.write("fake")
        count = dm.count_existing_images()
        assert count == 5

    def test_url_hash_in_filename(self):
        """文件名包含 URL hash。"""
        dm = DatasetDirManager(self.tmp, batch_size=100)
        path1 = dm.get_save_path("http://example.com/unique1.jpg", ".jpg")
        path2 = dm.get_save_path("http://example.com/unique2.jpg", ".jpg")
        name1 = os.path.basename(path1)
        name2 = os.path.basename(path2)
        # 不同 URL 应产生不同 hash
        assert name1 != name2

    def test_atomic_save_respects_max_count_under_concurrency(self):
        """并发保存不能超过全局目标，也不能产生重复路径。"""
        dm = DatasetDirManager(self.tmp, batch_size=10)
        paths = []
        lock = threading.Lock()

        def save(idx):
            result = dm.save_content(
                f"http://example.com/atomic-{idx}.jpg",
                ".jpg",
                b"image-bytes",
                max_count=25,
            )
            if result is not None:
                with lock:
                    paths.append(result)

        threads = [threading.Thread(target=save, args=(i,)) for i in range(100)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(paths) == 25
        assert dm.saved_count == 25
        assert len({path for _, path in paths}) == 25
        assert dm.count_existing_images() == 25


def test_url_dedup_claim_is_retryable_until_committed():
    dedup = UrlDeduplicator(content_dedup=True)
    url = "https://example.com/transient.jpg"
    content = b"transient-content"
    try:
        assert dedup.claim(url) is True
        assert dedup.claim(url) is False
        assert dedup.claim_content(content) is True
        dedup.release_content(content)
        dedup.release(url)

        assert dedup.claim(url) is True
        assert dedup.claim_content(content) is True
        dedup.commit_content(content)
        dedup.commit(url)
        assert dedup.claim(url) is False
        assert dedup.claim_content(content) is False
    finally:
        dedup.close()


# ──────────────────────────────────────────────────────────────────────────────
# ProgressManager 测试
# ──────────────────────────────────────────────────────────────────────────────

class TestProgressManager:
    """断点续传进度管理器测试。"""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_load(self):
        """保存和加载进度。"""
        pm = ProgressManager(self.tmp)
        pm.save(saved_count=500, total_target=1000,
                keywords_done=["cat"], keywords_remaining=["dog"])
        data = pm.load()
        assert data["saved_count"] == 500
        assert data["total_target"] == 1000
        assert data["keywords_done"] == ["cat"]
        assert data["keywords_remaining"] == ["dog"]

    def test_load_nonexistent(self):
        """加载不存在的进度文件。"""
        pm = ProgressManager(self.tmp)
        data = pm.load()
        assert data == {}

    def test_keywords_done_property(self):
        """keywords_done 属性。"""
        pm = ProgressManager(self.tmp)
        pm.save(saved_count=100, total_target=500,
                keywords_done=["cat", "dog"], keywords_remaining=["bird"])
        assert pm.keywords_done == ["cat", "dog"]


# ──────────────────────────────────────────────────────────────────────────────
# MetadataWriter 测试
# ──────────────────────────────────────────────────────────────────────────────

class TestMetadataWriter:
    """元数据写入器测试。"""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_and_read(self):
        """写入和读取元数据。"""
        mw = MetadataWriter(self.tmp)
        mw.write({"index": 0, "url": "http://example.com/a.jpg", "keyword": "cat"})
        mw.write({"index": 1, "url": "http://example.com/b.jpg", "keyword": "cat"})
        mw.close()

        path = os.path.join(self.tmp, "metadata.jsonl")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 2
        data0 = json.loads(lines[0])
        assert data0["index"] == 0
        assert data0["keyword"] == "cat"

    def test_thread_safety(self):
        """多线程并发写入。"""
        mw = MetadataWriter(self.tmp)
        lock = threading.Lock()
        counter = [0]

        def write_item(idx):
            mw.write({"index": idx, "url": f"http://example.com/img{idx}.jpg"})
            with lock:
                counter[0] += 1

        threads = [threading.Thread(target=write_item, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        mw.close()

        path = os.path.join(self.tmp, "metadata.jsonl")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 100


# ──────────────────────────────────────────────────────────────────────────────
# DatasetCrawler 集成测试（不实际下载，仅验证初始化和配置）
# ──────────────────────────────────────────────────────────────────────────────

class TestDatasetCrawlerInit:
    """DatasetCrawler 初始化测试（不触发网络请求）。"""

    def test_init_no_clip(self):
        """不使用 CLIP 初始化。"""
        tmp = tempfile.mkdtemp()
        try:
            crawler = DatasetCrawler(
                keywords=["test"],
                total_count=100,
                output_dir=tmp,
                use_clip=False,
            )
            assert crawler.use_clip is False
            assert crawler.model is None
            assert crawler.total_count == 100
            assert crawler.batch_size == 100
            assert crawler.image_output_format == "jpg"
            assert crawler.jpeg_quality == 95
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_image_output_format_can_preserve_source(self):
        """默认转 JPG，但允许任务显式保留源格式。"""
        tmp = tempfile.mkdtemp()
        try:
            crawler = DatasetCrawler(
                keywords=["test"],
                total_count=1,
                output_dir=tmp,
                use_clip=False,
                image_output_format="original",
            )
            assert crawler.image_output_format is None
            crawler.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_init_with_engines(self):
        """指定搜索引擎初始化。"""
        tmp = tempfile.mkdtemp()
        try:
            crawler = DatasetCrawler(
                keywords=["test"],
                total_count=100,
                output_dir=tmp,
                use_clip=False,
                search_engines=["baidu"],
            )
            assert crawler.search_engines == ["baidu"]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_query_image_requires_clip(self):
        """以图搜图不能在明确关闭 CLIP 时静默失效。"""
        tmp = tempfile.mkdtemp()
        query = os.path.join(tmp, "query.jpg")
        Image.new("RGB", (32, 32), (255, 0, 0)).save(query)
        with pytest.raises(RuntimeError, match="query_image requires CLIP"):
            DatasetCrawler(
                keywords=["test"],
                total_count=1,
                output_dir=os.path.join(tmp, "dataset"),
                use_clip=False,
                query_image=query,
            )
        shutil.rmtree(tmp, ignore_errors=True)

    def test_image_query_similarity_uses_visual_embedding(self):
        """查询图片过滤器应使用归一化图像向量计算余弦相似度。"""
        torch = pytest.importorskip("torch")
        tmp = tempfile.mkdtemp()
        crawler = None
        try:
            crawler = DatasetCrawler(
                keywords=["test"],
                total_count=1,
                output_dir=tmp,
                use_clip=False,
            )

            class FakeModel:
                def encode_image(self, tensor):
                    return tensor

            crawler.model = FakeModel()
            crawler.preprocess = lambda image: torch.tensor([1.0, 0.0, 0.0])
            crawler._image_query_feature = torch.tensor([[1.0, 0.0, 0.0]])
            similarity = crawler._image_query_similarity(Image.new("RGB", (8, 8)))
            assert similarity == pytest.approx(1.0)
        finally:
            if crawler is not None:
                crawler.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_init_with_sites(self):
        """指定站点解析器初始化。"""
        tmp = tempfile.mkdtemp()
        try:
            crawler = DatasetCrawler(
                keywords=["test"],
                total_count=100,
                output_dir=tmp,
                use_clip=False,
                site_parsers=["jdlingyu"],
            )
            assert crawler._site_parsers == ["jdlingyu"]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_init_resume(self):
        """断点续传初始化。"""
        tmp = tempfile.mkdtemp()
        try:
            # 先创建一些已有文件
            batch_dir = os.path.join(tmp, "batch_0000")
            os.makedirs(batch_dir, exist_ok=True)
            for i in range(10):
                with open(os.path.join(batch_dir, f"{i:04d}_test.jpg"), "w") as f:
                    f.write("fake")

            crawler = DatasetCrawler(
                keywords=["test"],
                total_count=100,
                output_dir=tmp,
                use_clip=False,
                resume=True,
            )
            assert crawler._dir_manager.saved_count == 10
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dir_manager_integration(self):
        """目录管理器集成。"""
        tmp = tempfile.mkdtemp()
        try:
            crawler = DatasetCrawler(
                keywords=["test"],
                total_count=500,
                output_dir=tmp,
                use_clip=False,
                batch_size=100,
            )
            assert crawler._dir_manager.batch_size == 100
            assert crawler._dir_manager.output_dir == tmp
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_query_label_decisions_support_multiple_labels_and_aliases(self):
        """组合查询词应解析为多个固定标签，并支持可选别名。"""
        tmp = tempfile.mkdtemp()
        crawler = None
        try:
            crawler = DatasetCrawler(
                keywords=["forklift worker"],
                total_count=1,
                output_dir=tmp,
                use_clip=False,
                label_policy=LabelPolicy(
                    fixed_labels=["叉车", "工区", "工人"],
                    aliases={"forklift": "叉车", "worker": "工人"},
                ),
            )
            decisions = crawler._query_label_decisions("forklift worker")
            assert [item.name for item in decisions] == ["叉车", "工人"]
        finally:
            if crawler is not None:
                crawler.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_download_and_save_valid_image(self):
        """真实图片字节应通过验证链并写入 metadata。"""
        tmp = tempfile.mkdtemp()
        crawler = None
        try:
            image = Image.new("RGB", (256, 256))
            image.putdata([
                (x % 256, y % 256, (x + y) % 256)
                for y in range(256)
                for x in range(256)
            ])
            image_bytes = io.BytesIO()
            image.save(image_bytes, format="PNG")
            payload = image_bytes.getvalue()

            class FakeResponse:
                def __init__(self, remainder):
                    self.remainder = remainder

                def iter_content(self, chunk_size=65536):
                    yield self.remainder

                def close(self):
                    pass

            crawler = DatasetCrawler(
                keywords=["blue square"],
                total_count=1,
                output_dir=tmp,
                use_clip=False,
                min_file_size=100,
                min_variance=1.0,
            )
            crawler._http.get_stream = lambda url, peek_bytes=8192: (
                payload[:peek_bytes],
                FakeResponse(payload[peek_bytes:]),
            )

            assert crawler._download_and_save(
                "https://img.example.com/blue.jpg",
                "blue square",
                "test",
            ) is True
            assert crawler._dir_manager.saved_count == 1
            image_files = [
                os.path.join(root, name)
                for root, _, names in os.walk(tmp)
                for name in names
                if name.endswith(".jpg")
            ]
            assert len(image_files) == 1
            with Image.open(image_files[0]) as saved_image:
                assert saved_image.format == "JPEG"
            with open(os.path.join(tmp, "metadata.jsonl"), encoding="utf-8") as f:
                record = json.loads(f.readline())
            assert record["index"] == 0
            assert record["file_path"] == image_files[0]
            assert record["ext"] == ".jpg"
            assert record["source_ext"] == ".png"
            assert record["quality"]["format"] == "jpeg"
            assert record["format_conversion"]["enabled"] is True
            assert [item["name"] for item in record["labels"]] == ["blue square"]
            assert crawler._state_store.count_candidates(crawler.job_id, "accepted") == 1
            with open(os.path.join(tmp, "manifest.jsonl"), encoding="utf-8") as f:
                manifest = json.loads(f.readline())
            assert manifest["task_type"] == "image_classification"
            assert manifest["modalities"][0]["modality"] == "image"
            assert manifest["modalities"][0]["uri"] == os.path.relpath(record["file_path"], tmp)
        finally:
            if crawler is not None:
                crawler.close()
            shutil.rmtree(tmp, ignore_errors=True)
