# coding=utf-8
"""DatasetCrawler 单元测试。"""
import json
import os
import shutil
import tempfile
import threading

import pytest

from smart_spider.dataset_crawler import (
    DatasetCrawler,
    DatasetDirManager,
    MetadataWriter,
    ProgressManager,
)


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
