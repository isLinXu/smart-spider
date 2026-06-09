# coding=utf-8
"""站点深度爬取模块测试 — SiteParser + SiteCrawler。"""
import os
import threading
from unittest.mock import MagicMock, patch

import pytest

from smart_spider.site_parser import (
    SiteParser,
    JDlingyuParser,
    SITE_PARSER_REGISTRY,
    get_site_parser,
    register_site_parser,
)


# ════════════════════════════════════════════════════════════════════════════════
# SiteParser 抽象基类
# ════════════════════════════════════════════════════════════════════════════════

class TestSiteParserAbstract:
    """验证 SiteParser 不能直接实例化。"""

    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            SiteParser()


class TestCustomParserRegistration:
    """验证自定义解析器注册流程。"""

    def test_register_and_get(self):
        class DummyParser(SiteParser):
            name = "dummy_test"
            base_url = "https://example.com"
            listing_url = "https://example.com/list"

            def collect_pages(self, html):
                return []

            def extract_images(self, html):
                return []

        p = DummyParser()
        register_site_parser(p)
        assert "dummy_test" in SITE_PARSER_REGISTRY
        assert get_site_parser("dummy_test") is p

        # cleanup
        del SITE_PARSER_REGISTRY["dummy_test"]

    def test_register_empty_name_raises(self):
        class BadParser(SiteParser):
            name = ""
            base_url = "https://example.com"
            listing_url = "https://example.com/list"

            def collect_pages(self, html):
                return []

            def extract_images(self, html):
                return []

        with pytest.raises(ValueError, match="non-empty 'name'"):
            register_site_parser(BadParser())

    def test_get_unknown_raises(self):
        with pytest.raises(KeyError, match="Unknown site parser"):
            get_site_parser("this_does_not_exist")


# ════════════════════════════════════════════════════════════════════════════════
# JDlingyuParser
# ════════════════════════════════════════════════════════════════════════════════

class TestJDlingyuParser:
    """验证 JDlingyuParser 的解析逻辑。"""

    def setup_method(self):
        self.parser = JDlingyuParser()

    def test_collect_pages_extracts_articles(self):
        html = '''
        <div><a href="https://www.jdlingyu.com/149524.html"><span>Title A</span></a></div>
        <div><a href="https://www.jdlingyu.com/149525.html"><span>Title B</span></a></div>
        '''
        items = self.parser.collect_pages(html)
        assert len(items) == 2
        assert items[0]["id"] == "149524"
        assert items[0]["url"] == "https://www.jdlingyu.com/149524.html"
        assert "Title A" in items[0]["title"]

    def test_collect_pages_deduplicates(self):
        html = '''
        <a href="https://www.jdlingyu.com/149524.html">A</a>
        <a href="https://www.jdlingyu.com/149524.html">A dup</a>
        '''
        items = self.parser.collect_pages(html)
        assert len(items) == 1

    def test_collect_pages_no_match(self):
        html = "<div>No links here</div>"
        items = self.parser.collect_pages(html)
        assert items == []

    def test_extract_images_lazy_load(self):
        html = '<img class="lazy" data-src="https://img.jdlingyu.com/i/2024/photo1.webp">'
        urls = self.parser.extract_images(html)
        assert len(urls) == 1
        assert urls[0] == "https://img.jdlingyu.com/i/2024/photo1.webp"

    def test_extract_images_src_attribute(self):
        html = '<img src="https://img.jdlingyu.com/i/2024/photo2.webp">'
        urls = self.parser.extract_images(html)
        assert len(urls) == 1

    def test_extract_images_filters_default(self):
        html = '<img src="https://www.jdlingyu.com/wp-content/themes/default-img.jpg">'
        urls = self.parser.extract_images(html)
        assert len(urls) == 0

    def test_extract_images_filters_avatar(self):
        html = '<img src="https://img.jdlingyu.com/avatar/user.png">'
        urls = self.parser.extract_images(html)
        assert len(urls) == 0

    def test_extract_images_no_duplicates(self):
        html = '''
        <img class="lazy" data-src="https://img.jdlingyu.com/i/x.webp">
        <img src="https://img.jdlingyu.com/i/x.webp">
        '''
        urls = self.parser.extract_images(html)
        assert len(urls) == 1

    def test_get_detail_pagination_chinese(self):
        html = '<span>3 页</span>'
        assert self.parser.get_detail_pagination(html) == 3

    def test_get_detail_pagination_link_pattern(self):
        html = '/149524.html/2</a><a>/149524.html/5</a>'
        assert self.parser.get_detail_pagination(html) == 5

    def test_get_detail_pagination_default(self):
        html = '<div>No pagination</div>'
        assert self.parser.get_detail_pagination(html) == 1

    def test_build_listing_url(self):
        assert self.parser.build_listing_url(1) == "https://www.jdlingyu.com/collection/meizitu"
        assert self.parser.build_listing_url(2) == "https://www.jdlingyu.com/collection/meizitu/page/2"

    def test_build_detail_page_url(self):
        url = "https://www.jdlingyu.com/149524.html"
        assert self.parser.build_detail_page_url(url, 1) == url
        assert self.parser.build_detail_page_url(url, 2) == url + "/2"

    def test_should_filter_image_default(self):
        assert not self.parser.should_filter_image("https://img.jdlingyu.com/i/photo.webp")


# ════════════════════════════════════════════════════════════════════════════════
# SiteCrawler 初始化
# ════════════════════════════════════════════════════════════════════════════════

class TestSiteCrawlerInit:
    """验证 SiteCrawler 初始化参数正确传递。"""

    def test_default_init(self):
        from smart_spider.site_crawler import SiteCrawler
        parser = JDlingyuParser()
        crawler = SiteCrawler(site_parser=parser)
        assert crawler.parser is parser
        assert crawler.start_page == 1
        assert crawler.end_page == 5
        assert crawler.max_workers == 8

    def test_custom_init(self):
        from smart_spider.site_crawler import SiteCrawler
        parser = JDlingyuParser()
        crawler = SiteCrawler(
            site_parser=parser,
            output_dir="/tmp/test_site",
            start_page=3,
            end_page=10,
            max_workers=4,
            rate=3.0,
        )
        assert crawler.start_page == 3
        assert crawler.end_page == 10
        assert crawler.max_workers == 4


# ════════════════════════════════════════════════════════════════════════════════
# 导出验证
# ════════════════════════════════════════════════════════════════════════════════

class TestExports:
    """验证新模块正确导出到顶层包。"""

    def test_site_parser_exports(self):
        from smart_spider import SiteParser, SITE_PARSER_REGISTRY, get_site_parser, register_site_parser
        assert SiteParser is not None
        assert isinstance(SITE_PARSER_REGISTRY, dict)
        assert callable(get_site_parser)
        assert callable(register_site_parser)

    def test_site_crawler_exports(self):
        from smart_spider import SiteCrawler
        assert SiteCrawler is not None

    def test_jdlingyu_registered(self):
        from smart_spider import SITE_PARSER_REGISTRY
        assert "jdlingyu" in SITE_PARSER_REGISTRY
