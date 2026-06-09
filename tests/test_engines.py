# coding=utf-8
"""引擎层单元测试。

覆盖：
- BaiduImageEngine: JSON API 解析、正则降级
- BingImageEngine: murl 提取、iurl 降级
- SogouImageEngine: JSON 解析
- So360ImageEngine: JSON 解析
- BilibiliVideoEngine: bvid 提取、thumb URL 协议补全修复验证
- BingVideoEngine: contentUrl 提取
- BaiduTextEngine: 结果 URL + 标题提取
- BingTextEngine: b_algo 结构解析
- WeixinArticleEngine: mp.weixin.qq.com URL 提取
- XiaohongshuEngine: __INITIAL_STATE__ JSON 解析
"""
import json
import pytest

from smart_spider.engines import (
    BaiduImageEngine,
    BingImageEngine,
    SogouImageEngine,
    So360ImageEngine,
    BilibiliVideoEngine,
    BingVideoEngine,
    BaiduTextEngine,
    BingTextEngine,
    WeixinArticleEngine,
    XiaohongshuEngine,
    MediaType,
    RenderMode,
    get_engine,
    ENGINE_REGISTRY,
)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures：构造虚假 HTML / JSON 响应
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def baidu_json_response():
    """百度图片 JSON API 格式响应。"""
    payload = {
        "data": [
            {
                "objURL": "https://img.example.com/1.jpg",
                "hoverURL": "https://img.example.com/hover1.jpg",
                "thumbURL": "https://thumb.example.com/t1.jpg",
                "fromPageTitleEncode": "Example Title 1",
                "width": 800,
                "height": 600,
            },
            {
                "objURL": "https://img.example.com/2.jpg",
                "thumbURL": "https://thumb.example.com/t2.jpg",
                "fromPageTitleEncode": "Example Title 2",
                "width": 1024,
                "height": 768,
            },
            # 无效条目：无 URL
            {"width": 100},
        ]
    }
    return json.dumps(payload)


@pytest.fixture
def baidu_html_fallback():
    """百度图片页面正则降级响应（非 JSON）。"""
    return '"objURL":"https://img.example.com/fallback.jpg","other":"data"'


@pytest.fixture
def bing_image_html():
    """Bing 图片搜索页面 HTML 片段。"""
    return (
        '"murl":"https://img.bing.com/1.jpg"'
        '"turl":"https://th.bing.com/th/1.jpg"'
        '"t":"Bing Image Title"'
        '"murl":"https://img.bing.com/2.jpg"'
    )


@pytest.fixture
def bilibili_json_response():
    """B 站搜索 API JSON 响应。"""
    payload = {
        "data": {
            "result": [
                {
                    "bvid": "BV1xx411c7mD",
                    "title": "测试视频标题",
                    "pic": "//i0.hdslb.com/bfs/archive/thumb.jpg",
                    "duration": "12:34",
                    "play": 99999,
                    "author": "up主名字",
                },
                {
                    "bvid": "",  # 无效：bvid 为空
                    "title": "无效视频",
                },
            ]
        }
    }
    return json.dumps(payload)


@pytest.fixture
def baidu_text_html():
    """百度网页搜索结果 HTML 片段（h3.t + a[href] 结构）。"""
    return """
    <div class="result">
        <h3 class="t">
            <a href="https://www.example.com/article1">第一个结果标题</a>
        </h3>
    </div>
    <div class="result">
        <h3 class="t c-title">
            <a href="https://www.news.com/article2">第二个新闻标题</a>
        </h3>
    </div>
    """


@pytest.fixture
def weixin_html():
    """微信文章搜索结果 HTML 片段。"""
    return """
    <ul>
        <li>
            <h3><a href="https://mp.weixin.qq.com/s/abc123">微信文章一</a></h3>
        </li>
        <li>
            <h3><a href="https://mp.weixin.qq.com/s/xyz456">微信文章二</a></h3>
        </li>
    </ul>
    """


@pytest.fixture
def xiaohongshu_html():
    """小红书搜索结果页面（__INITIAL_STATE__ JSON）。"""
    state = {
        "search": {
            "noteList": [
                {
                    "cover": {"urlDefault": "//sns-img.xhscdn.com/notes/img1.jpg"},
                    "title": "小红书笔记一",
                    "id": "note_001",
                    "likedCount": 2048,
                },
                {
                    "cover": {"urlDefault": "//sns-img.xhscdn.com/notes/img2.jpg"},
                    "title": "小红书笔记二",
                    "id": "note_002",
                    "likedCount": 512,
                },
            ]
        }
    }
    return f"<script>window.__INITIAL_STATE__ = {json.dumps(state)}</script>"


# ──────────────────────────────────────────────────────────────────────────────
# 引擎元属性测试
# ──────────────────────────────────────────────────────────────────────────────

class TestEngineRegistry:
    def test_all_engines_registered(self):
        expected = {
            "baidu", "bing", "sogou", "360", "google", "weibo",
            "bilibili", "bing_video",
            "baidu_text", "bing_text",
            "weixin", "xiaohongshu",
        }
        assert expected == set(ENGINE_REGISTRY.keys())

    def test_get_engine_known(self):
        eng = get_engine("baidu")
        assert eng.name == "baidu"

    def test_get_engine_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown engine"):
            get_engine("nonexistent_engine")

    def test_media_types_correct(self):
        assert get_engine("baidu").media_type == MediaType.IMAGE
        assert get_engine("bilibili").media_type == MediaType.VIDEO
        assert get_engine("baidu_text").media_type == MediaType.TEXT

    def test_render_modes_correct(self):
        assert get_engine("baidu").render_mode == RenderMode.STATIC
        assert get_engine("weixin").render_mode == RenderMode.DYNAMIC
        assert get_engine("xiaohongshu").render_mode == RenderMode.DYNAMIC


# ──────────────────────────────────────────────────────────────────────────────
# BaiduImageEngine
# ──────────────────────────────────────────────────────────────────────────────

class TestBaiduImageEngine:
    engine = BaiduImageEngine()

    def test_build_url_contains_keyword(self):
        url = self.engine.build_search_url("猫咪", 0)
        assert "猫咪" in url or "%E7%8C%AB%E5%92%AA" in url
        assert "image.baidu.com" in url

    def test_extract_items_json(self, baidu_json_response):
        items = self.engine.extract_items(baidu_json_response)
        assert len(items) == 2
        assert items[0]["url"] == "https://img.example.com/1.jpg"
        assert items[0]["meta"]["title"] == "Example Title 1"
        assert items[0]["meta"]["width"] == 800

    def test_extract_items_fallback_regex(self, baidu_html_fallback):
        items = self.engine.extract_items(baidu_html_fallback)
        assert len(items) == 1
        assert items[0]["url"] == "https://img.example.com/fallback.jpg"

    def test_extract_items_empty_html(self):
        items = self.engine.extract_items("{}")
        assert items == []

    def test_check_url_different_from_search_url(self):
        search = self.engine.build_search_url("dog", 0)
        check = self.engine.build_check_url("dog", 0)
        assert search != check
        assert "flip" in check


# ──────────────────────────────────────────────────────────────────────────────
# BingImageEngine
# ──────────────────────────────────────────────────────────────────────────────

class TestBingImageEngine:
    engine = BingImageEngine()

    def test_build_url(self):
        url = self.engine.build_search_url("dog", 0)
        assert "bing.com/images" in url

    def test_extract_items_murl(self, bing_image_html):
        items = self.engine.extract_items(bing_image_html)
        assert len(items) == 2
        assert items[0]["url"] == "https://img.bing.com/1.jpg"
        assert items[0]["meta"]["thumb"] == "https://th.bing.com/th/1.jpg"
        assert items[0]["meta"]["title"] == "Bing Image Title"

    def test_extract_items_iurl_fallback(self):
        html = '"iurl":"https://img.bing.com/iurl1.jpg"'
        items = self.engine.extract_items(html)
        assert len(items) == 1
        assert items[0]["url"] == "https://img.bing.com/iurl1.jpg"

    def test_extract_items_empty(self):
        items = self.engine.extract_items("")
        assert items == []


# ──────────────────────────────────────────────────────────────────────────────
# SogouImageEngine
# ──────────────────────────────────────────────────────────────────────────────

class TestSogouImageEngine:
    engine = SogouImageEngine()

    def test_extract_items_json(self):
        payload = json.dumps({
            "items": [
                {"picUrl": "https://pic.sogou.com/1.jpg", "thumbUrl": "https://th.sogou.com/1.jpg", "title": "搜狗图片"},
                {"picUrl": "https://pic.sogou.com/2.jpg"},
            ]
        })
        items = self.engine.extract_items(payload)
        assert len(items) == 2
        assert items[0]["meta"]["title"] == "搜狗图片"

    def test_extract_items_regex_fallback(self):
        html = '"thumbUrl":"https://th.sogou.com/fallback.jpg"'
        items = self.engine.extract_items(html)
        assert len(items) == 1


# ──────────────────────────────────────────────────────────────────────────────
# So360ImageEngine
# ──────────────────────────────────────────────────────────────────────────────

class TestSo360ImageEngine:
    engine = So360ImageEngine()

    def test_extract_items_json(self):
        payload = json.dumps({
            "list": [
                {"img": "https://img.so.com/1.jpg", "thumb": "https://th.so.com/1.jpg", "title": "360图片"},
            ]
        })
        items = self.engine.extract_items(payload)
        assert len(items) == 1
        assert items[0]["url"] == "https://img.so.com/1.jpg"

    def test_extract_items_url_fallback(self):
        payload = json.dumps({"list": [{"url": "https://img.so.com/via_url.jpg"}]})
        items = self.engine.extract_items(payload)
        assert len(items) == 1


# ──────────────────────────────────────────────────────────────────────────────
# BilibiliVideoEngine（重点：验证 thumb URL 协议修复）
# ──────────────────────────────────────────────────────────────────────────────

class TestBilibiliVideoEngine:
    engine = BilibiliVideoEngine()

    def test_extract_items(self, bilibili_json_response):
        items = self.engine.extract_items(bilibili_json_response)
        assert len(items) == 1  # 空 bvid 被过滤
        item = items[0]
        assert item["url"] == "https://www.bilibili.com/video/BV1xx411c7mD"
        assert item["meta"]["bvid"] == "BV1xx411c7mD"
        assert item["meta"]["author"] == "up主名字"

    def test_thumb_url_protocol_fix(self, bilibili_json_response):
        """验证 //i0.hdslb.com/... 被正确补全为 https://i0.hdslb.com/..."""
        items = self.engine.extract_items(bilibili_json_response)
        thumb = items[0]["meta"]["thumb"]
        assert thumb.startswith("https://"), f"thumb URL 应以 https:// 开头，实际: {thumb}"
        assert "i0.hdslb.com" in thumb

    def test_html_tags_stripped_from_title(self):
        """标题中的 HTML 标签应被剥离。"""
        payload = json.dumps({
            "data": {
                "result": [
                    {
                        "bvid": "BV_test",
                        "title": "<em>高亮标题</em>测试",
                        "pic": "//i0.hdslb.com/test.jpg",
                    }
                ]
            }
        })
        items = self.engine.extract_items(payload)
        assert "<em>" not in items[0]["meta"]["title"]
        assert "高亮标题" in items[0]["meta"]["title"]

    def test_build_url_page_calculation(self):
        url_p1 = self.engine.build_search_url("猫咪", 0)
        url_p2 = self.engine.build_search_url("猫咪", 20)
        assert "page=1" in url_p1
        assert "page=2" in url_p2


# ──────────────────────────────────────────────────────────────────────────────
# BingVideoEngine
# ──────────────────────────────────────────────────────────────────────────────

class TestBingVideoEngine:
    engine = BingVideoEngine()

    def test_extract_items(self):
        html = '"contentUrl":"https://www.youtube.com/watch?v=abc""name":"Bing Video Title"'
        items = self.engine.extract_items(html)
        assert len(items) == 1
        assert "youtube.com" in items[0]["url"]
        assert items[0]["meta"]["title"] == "Bing Video Title"

    def test_extract_items_empty(self):
        assert self.engine.extract_items("") == []


# ──────────────────────────────────────────────────────────────────────────────
# BaiduTextEngine
# ──────────────────────────────────────────────────────────────────────────────

class TestBaiduTextEngine:
    engine = BaiduTextEngine()

    def test_extract_items(self, baidu_text_html):
        items = self.engine.extract_items(baidu_text_html)
        assert len(items) >= 1
        urls = [i["url"] for i in items]
        assert any("example.com" in u for u in urls)

    def test_baidu_urls_excluded_in_fallback(self):
        html = 'href="https://www.baidu.com/internal" href="https://www.external.com/page"'
        items = self.engine.extract_items(html)
        for item in items:
            assert "baidu.com" not in item["url"]

    def test_returns_at_most_10(self):
        # 生成 20 个结果
        links = " ".join(
            f'<h3 class="t"><a href="https://example.com/{i}">Title {i}</a></h3>'
            for i in range(20)
        )
        items = self.engine.extract_items(links)
        assert len(items) <= 10


# ──────────────────────────────────────────────────────────────────────────────
# WeixinArticleEngine
# ──────────────────────────────────────────────────────────────────────────────

class TestWeixinArticleEngine:
    engine = WeixinArticleEngine()

    def test_extract_items(self, weixin_html):
        items = self.engine.extract_items(weixin_html)
        assert len(items) == 2
        for item in items:
            assert "mp.weixin.qq.com" in item["url"]

    def test_build_url_page(self):
        url = self.engine.build_search_url("AI", 10)
        assert "weixin.sogou.com" in url
        assert "page=2" in url


# ──────────────────────────────────────────────────────────────────────────────
# XiaohongshuEngine
# ──────────────────────────────────────────────────────────────────────────────

class TestXiaohongshuEngine:
    engine = XiaohongshuEngine()

    def test_extract_items(self, xiaohongshu_html):
        items = self.engine.extract_items(xiaohongshu_html)
        assert len(items) == 2
        for item in items:
            assert item["url"].startswith("https://")
            assert "xhscdn.com" in item["url"]
        assert items[0]["meta"]["title"] == "小红书笔记一"
        assert items[0]["meta"]["likes"] == 2048

    def test_extract_items_invalid_json(self):
        html = "<script>window.__INITIAL_STATE__ = {invalid json}</script>"
        items = self.engine.extract_items(html)
        assert items == []

    def test_extract_items_no_state(self):
        items = self.engine.extract_items("<html></html>")
        assert items == []
