# coding=utf-8
"""spider_tools_bridge 模块的单元测试。"""
import pytest
from unittest.mock import MagicMock, patch
from smart_spider.spider_tools_bridge import (
    SpiderToolsBridge,
    SpiderToolsURL,
    parse_page_spec,
    list_available_sites,
    _SPIDER_TOOLS_REGISTRY,
)


# ──────────────────────────────────────────────────────────────────────────────
# SpiderToolsURL 数据类
# ──────────────────────────────────────────────────────────────────────────────

class TestSpiderToolsURL:
    def test_basic_creation(self):
        u = SpiderToolsURL(url="https://example.com/img.jpg", site="test")
        assert u.url == "https://example.com/img.jpg"
        assert u.site == "test"
        assert u.album is None
        assert u.referer is None
        assert u.tags is None

    def test_full_creation(self):
        u = SpiderToolsURL(
            url="https://example.com/img.jpg",
            site="danbooru",
            album="1girl",
            referer="https://danbooru.donmai.us/posts/123",
            tags=["1girl", "solo"],
        )
        assert u.album == "1girl"
        assert u.referer == "https://danbooru.donmai.us/posts/123"
        assert u.tags == ["1girl", "solo"]


# ──────────────────────────────────────────────────────────────────────────────
# parse_page_spec
# ──────────────────────────────────────────────────────────────────────────────

class TestParsePageSpec:
    def test_range(self):
        assert parse_page_spec("1-5") == [1, 2, 3, 4, 5]

    def test_comma_separated(self):
        assert parse_page_spec("1,3,7") == [1, 3, 7]

    def test_mixed(self):
        assert parse_page_spec("1-3,7,10-12") == [1, 2, 3, 7, 10, 11, 12]

    def test_single(self):
        assert parse_page_spec("5") == [5]

    def test_empty_parts(self):
        assert parse_page_spec("1,,3") == [1, 3]


# ──────────────────────────────────────────────────────────────────────────────
# list_available_sites
# ──────────────────────────────────────────────────────────────────────────────

class TestListAvailableSites:
    def test_returns_known_sites(self):
        sites = list_available_sites()
        assert "danbooru" in sites
        assert "safebooru" in sites
        assert "wallhaven" in sites

    def test_returns_list(self):
        assert isinstance(list_available_sites(), list)


# ──────────────────────────────────────────────────────────────────────────────
# SpiderToolsBridge
# ──────────────────────────────────────────────────────────────────────────────

class TestSpiderToolsBridge:
    def test_default_site_config(self):
        bridge = SpiderToolsBridge()
        cfg = bridge._default_site_config()
        assert cfg["rps"] == 1.5
        assert cfg["phash_dedup"] is False
        assert cfg["quality"]["min_width"] == 200

    def test_unknown_site_raises(self):
        bridge = SpiderToolsBridge()
        with pytest.raises(ValueError, match="Unknown spider_tools site"):
            bridge.collect_urls("nonexistent_site")

    def test_collect_urls_mock(self):
        """Mock 一个 spider 类来测试 collect_urls 流程。"""
        mock_task = MagicMock()
        mock_task.url = "https://cdn.example.com/img001.jpg"
        mock_task.site = "test_site"
        mock_task.album = "test_album"
        mock_task.referer = None
        mock_task.tags = ["tag1"]

        mock_spider_cls = MagicMock()
        mock_spider_instance = MagicMock()
        mock_spider_instance.iter_tasks.return_value = [mock_task]
        mock_spider_cls.return_value = mock_spider_instance

        mock_http_cls = MagicMock()
        mock_http_instance = MagicMock()
        mock_http_cls.return_value = mock_http_instance

        bridge = SpiderToolsBridge()
        bridge._loaded_spiders["test_site"] = mock_spider_cls

        with patch.dict("sys.modules", {
            "core.http_client": MagicMock(HttpClient=mock_http_cls, HttpConfig=MagicMock()),
        }):
            results = bridge.collect_urls("test_site", tags="test", pages=[1])

        assert len(results) == 1
        assert results[0].url == "https://cdn.example.com/img001.jpg"
        assert results[0].site == "test_site"

    def test_collect_urls_with_limit(self):
        mock_tasks = [
            MagicMock(url=f"https://cdn.example.com/img{i:03d}.jpg",
                     site="test", album=None, referer=None, tags=None)
            for i in range(10)
        ]

        mock_spider_cls = MagicMock()
        mock_spider_instance = MagicMock()
        mock_spider_instance.iter_tasks.return_value = mock_tasks
        mock_spider_cls.return_value = mock_spider_instance

        mock_http_cls = MagicMock()
        mock_http_instance = MagicMock()
        mock_http_cls.return_value = mock_http_instance

        bridge = SpiderToolsBridge()
        bridge._loaded_spiders["test"] = mock_spider_cls

        with patch.dict("sys.modules", {
            "core.http_client": MagicMock(HttpClient=mock_http_cls, HttpConfig=MagicMock()),
        }):
            results = bridge.collect_urls("test", limit=3)

        assert len(results) == 3

    def test_collect_urls_multi_dedup(self):
        """测试多站点收集时的 URL 去重。"""
        # site_a 产出 shared.jpg + a_only.jpg
        mock_spider_cls_a = MagicMock()
        mock_instance_a = MagicMock()
        mock_instance_a.iter_tasks.return_value = [
            MagicMock(url="https://cdn.example.com/shared.jpg",
                     site="site_a", album=None, referer=None, tags=None),
            MagicMock(url="https://cdn.example.com/a_only.jpg",
                     site="site_a", album=None, referer=None, tags=None),
        ]
        mock_spider_cls_a.return_value = mock_instance_a

        # site_b 产出 shared.jpg（重复）+ b_only.jpg
        mock_spider_cls_b = MagicMock()
        mock_instance_b = MagicMock()
        mock_instance_b.iter_tasks.return_value = [
            MagicMock(url="https://cdn.example.com/shared.jpg",
                     site="site_b", album=None, referer=None, tags=None),
            MagicMock(url="https://cdn.example.com/b_only.jpg",
                     site="site_b", album=None, referer=None, tags=None),
        ]
        mock_spider_cls_b.return_value = mock_instance_b

        mock_http_cls = MagicMock()
        mock_http_instance = MagicMock()
        mock_http_cls.return_value = mock_http_instance

        bridge = SpiderToolsBridge()
        bridge._loaded_spiders["site_a"] = mock_spider_cls_a
        bridge._loaded_spiders["site_b"] = mock_spider_cls_b

        with patch.dict("sys.modules", {
            "core.http_client": MagicMock(HttpClient=mock_http_cls, HttpConfig=MagicMock()),
        }):
            results = bridge.collect_urls_multi(["site_a", "site_b"])

        # shared.jpg 来自 site_a，site_b 的 shared.jpg 被去重
        # 最终：shared.jpg + a_only.jpg + b_only.jpg = 3
        assert len(results) == 3
        urls = [r.url for r in results]
        assert "https://cdn.example.com/shared.jpg" in urls
        assert "https://cdn.example.com/a_only.jpg" in urls
        assert "https://cdn.example.com/b_only.jpg" in urls

    def test_collect_urls_multi_error_handling(self):
        """测试某站点失败时不影响其他站点。"""
        mock_good_cls = MagicMock()
        mock_good_instance = MagicMock()
        mock_good_instance.iter_tasks.return_value = [
            MagicMock(url="https://cdn.example.com/good.jpg",
                     site="good", album=None, referer=None, tags=None),
        ]
        mock_good_cls.return_value = mock_good_instance

        mock_bad_cls = MagicMock()
        mock_bad_instance = MagicMock()
        mock_bad_instance.iter_tasks.side_effect = RuntimeError("API down")
        mock_bad_cls.return_value = mock_bad_instance

        mock_http_cls = MagicMock()
        mock_http_instance = MagicMock()
        mock_http_cls.return_value = mock_http_instance

        bridge = SpiderToolsBridge()
        bridge._loaded_spiders["good"] = mock_good_cls
        bridge._loaded_spiders["bad"] = mock_bad_cls

        with patch.dict("sys.modules", {
            "core.http_client": MagicMock(HttpClient=mock_http_cls, HttpConfig=MagicMock()),
        }):
            results = bridge.collect_urls_multi(["bad", "good"])

        assert len(results) == 1
        assert results[0].site == "good"


# ──────────────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────────────

class TestRegistry:
    def test_registry_has_expected_sites(self):
        assert "danbooru" in _SPIDER_TOOLS_REGISTRY
        assert "safebooru" in _SPIDER_TOOLS_REGISTRY
        assert "wallhaven" in _SPIDER_TOOLS_REGISTRY
        assert "unsplash" in _SPIDER_TOOLS_REGISTRY
        assert "flickr" in _SPIDER_TOOLS_REGISTRY

    def test_registry_values_are_tuples(self):
        for site, (mod, cls) in _SPIDER_TOOLS_REGISTRY.items():
            assert isinstance(mod, str)
            assert isinstance(cls, str)
