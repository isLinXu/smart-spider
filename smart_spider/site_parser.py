# coding=utf-8
"""站点深度爬取解析器。

架构说明
--------
SiteParser 定义了「列表页 → 详情页 → 资源」两级跳转的标准接口，
让 SmartSpider 框架在原有搜索引擎模式之外，也能支持站点深度爬取模式。

两种模式的对比
------------
1. 搜索引擎模式（广度优先）：keywords → 搜索引擎 → 图片/视频/文本 URL
2. 站点深度模式（深度优先）：列表页 → 详情页 → 图片/文本 URL

SiteParser 接口设计
------------------
- collect_pages(html)  : 从列表页 HTML 解析出详情页链接列表
- extract_images(html) : 从详情页 HTML 解析出图片 URL 列表
- extract_texts(html)  : 从详情页 HTML 提取文本内容（可选）
- get_detail_pagination(html) : 解析详情页分页总数
- build_listing_url(base_url, page) : 构造列表页 URL
- build_detail_page_url(detail_url, page) : 构造详情页分页 URL
- 各种 Referer 方法 : 返回防盗链所需的请求头

注册表
------
SITE_PARSER_REGISTRY 以 SiteParser.name 为 key 注册所有解析器，
CLI 通过 --site 参数指定名称来选用解析器。

用法
----
>>> from smart_spider.site_parser import get_site_parser, SITE_PARSER_REGISTRY
>>> parser = get_site_parser("jdlingyu")
>>> pages = parser.collect_pages(html)
>>> images = parser.extract_images(detail_html)
"""

import re
from abc import ABC, abstractmethod
from typing import Optional


# ════════════════════════════════════════════════════════════════════════════════
# 抽象基类
# ════════════════════════════════════════════════════════════════════════════════

class SiteParser(ABC):
    """站点深度爬取解析器抽象基类。

    子类只需实现 collect_pages() 和 extract_images() 即可接入框架，
    其余方法均有合理默认值。

    设计原则
    --------
    1. 最小接口：只需实现 2 个抽象方法即可工作
    2. 渐进增强：可选覆盖更多方法获得精细控制
    3. 纯解析：不涉及网络请求，只做 HTML 解析，方便单元测试
    """

    name: str = ""           # 唯一标识符，用于 CLI --site 参数
    base_url: str = ""       # 站点根 URL
    listing_url: str = ""    # 列表页入口 URL（不含分页）

    # ── 必须实现的抽象方法 ──────────────────────────────────────────

    @abstractmethod
    def collect_pages(self, html: str) -> list[dict]:
        """从列表页 HTML 解析出详情页链接。

        Args:
            html: 列表页完整 HTML

        Returns:
            详情页字典列表，每项至少包含:
            - "url"  : 详情页 URL（str）
            - "title": 文章标题（str，可为空）
            - "id"   : 唯一标识（str，用于去重和目录命名）

        示例返回值::

            [
                {"url": "https://example.com/123.html", "title": "Foo", "id": "123"},
                {"url": "https://example.com/456.html", "title": "Bar", "id": "456"},
            ]
        """

    @abstractmethod
    def extract_images(self, html: str) -> list[str]:
        """从详情页 HTML 解析出图片真实 URL。

        Args:
            html: 详情页完整 HTML

        Returns:
            图片 URL 列表（去重由 SiteCrawler 负责）

        示例::

            ["https://cdn.example.com/img/1.webp", "https://cdn.example.com/img/2.webp"]
        """

    # ── 可选覆盖的方法 ─────────────────────────────────────────────

    def extract_texts(self, html: str) -> list[str]:
        """从详情页 HTML 提取文本段落。默认返回空列表。

        如需支持 text 模态，覆盖此方法返回正文段落列表。
        """
        return []

    def get_detail_pagination(self, html: str) -> int:
        """解析详情页的分页总数。默认返回 1（无分页）。

        对于长文章分多页展示的站点，覆盖此方法返回实际页数。
        """
        return 1

    def build_listing_url(self, page: int) -> str:
        """构造列表页 URL。

        默认行为:
        - page=1 → listing_url 本身
        - page>1 → listing_url/page/{page}

        如站点分页规则不同，覆盖此方法。
        """
        if page == 1:
            return self.listing_url
        return f"{self.listing_url}/page/{page}"

    def build_detail_page_url(self, detail_url: str, page: int) -> str:
        """构造详情页分页 URL。

        默认行为:
        - page=1 → detail_url 本身
        - page>1 → detail_url/{page}

        如站点分页规则不同，覆盖此方法。
        """
        if page == 1:
            return detail_url
        return f"{detail_url}/{page}"

    def get_listing_referer(self) -> str:
        """返回列表页请求所需的 Referer 头。默认为站点根 URL。"""
        return self.base_url + "/"

    def get_detail_referer(self, listing_url: str = "") -> str:
        """返回详情页请求所需的 Referer 头。默认为列表页 URL。"""
        return listing_url or self.listing_url

    def get_download_referer(self) -> str:
        """返回图片下载所需的 Referer 头（防防盗链）。默认为站点根 URL。"""
        return self.base_url + "/"

    def should_filter_image(self, url: str) -> bool:
        """判断图片 URL 是否应被过滤。默认不过滤。

        常见过滤场景:
        - 占位图 / 默认头像
        - 内网地址
        - 图标 / Logo 等装饰性图片
        """
        return False


# ════════════════════════════════════════════════════════════════════════════════
# 注册表
# ════════════════════════════════════════════════════════════════════════════════

SITE_PARSER_REGISTRY: dict[str, SiteParser] = {}


def register_site_parser(parser: SiteParser) -> None:
    """注册站点解析器到全局注册表。"""
    if not parser.name:
        raise ValueError(f"SiteParser {parser.__class__.__name__} must have a non-empty 'name'")
    SITE_PARSER_REGISTRY[parser.name] = parser


def get_site_parser(name: str) -> SiteParser:
    """按名称获取已注册的站点解析器。

    Raises:
        KeyError: 名称未注册时
    """
    if name not in SITE_PARSER_REGISTRY:
        available = ", ".join(sorted(SITE_PARSER_REGISTRY.keys())) or "(none)"
        raise KeyError(
            f"Unknown site parser '{name}'. Available: {available}"
        )
    return SITE_PARSER_REGISTRY[name]


# ════════════════════════════════════════════════════════════════════════════════
# 内置解析器: 绝对领域 (jdlingyu.com)
# ════════════════════════════════════════════════════════════════════════════════

class JDlingyuParser(SiteParser):
    """绝对领域 (jdlingyu.com) 妹子图专区解析器。

    网站结构
    --------
    1. 列表页: https://www.jdlingyu.com/collection/meizitu/page/{n}
       - 共 184 页，2202 篇文章
       - 每页约 12 个图集卡片，卡片链接格式: /{id}.html

    2. 详情页: https://www.jdlingyu.com/{id}.html[/page/{n}]
       - 图片使用懒加载: <img class="lazy" data-src="https://img.jdlingyu.com/...">
       - 未加载时 src 为占位图 (default-img.jpg)
       - 多页文章有分页: /{id}.html/2, /{id}.html/3 ...

    3. 图片 CDN: img.jdlingyu.com — webp 格式
    """

    name = "jdlingyu"
    base_url = "https://www.jdlingyu.com"
    listing_url = "https://www.jdlingyu.com/collection/meizitu"

    def collect_pages(self, html: str) -> list[dict]:
        """从列表页 HTML 解析文章链接。"""
        items = []
        seen = set()
        for m in re.finditer(
            r'href="(https://www\.jdlingyu\.com/(\d+)\.html)"[^>]*>(.*?)</a>',
            html, re.DOTALL,
        ):
            url = m.group(1)
            aid = m.group(2)
            title = re.sub(r"<[^>]+>", "", m.group(3)).strip()
            if aid in seen:
                continue
            seen.add(aid)
            items.append({"url": url, "title": title, "id": aid})
        return items

    def extract_images(self, html: str) -> list[str]:
        """从详情页 HTML 解析图片真实 URL。

        三层提取策略:
        1. data-src 属性（懒加载，主要来源）
        2. src 属性中已是真实 URL（已加载的图片）
        3. 其他 CDN 域名的图片（排除占位图和内网地址）

        每层都会调用 should_filter_image() 进行过滤。
        """
        urls = []

        # 方式1: data-src 属性（懒加载）
        for m in re.finditer(r'data-src="(https://img\.jdlingyu\.com/[^"]+)"', html):
            url = m.group(1)
            if not self.should_filter_image(url):
                urls.append(url)

        # 方式2: src 属性中已是真实 URL
        for m in re.finditer(r'src="(https://img\.jdlingyu\.com/[^"]+)"', html):
            url = m.group(1)
            if url not in urls and not self.should_filter_image(url):
                urls.append(url)

        # 方式3: 其他 CDN 域名的图片
        for m in re.finditer(
            r'(?:data-src|src)="(https?://[^"]*\.(?:jpg|jpeg|png|webp|gif)[^"]*)"',
            html, re.IGNORECASE,
        ):
            url = m.group(1)
            if url not in urls and not self.should_filter_image(url):
                urls.append(url)

        return urls

    def get_detail_pagination(self, html: str) -> int:
        """解析详情页的分页总数。

        分页格式: <a href="/149524.html/2">2</a> ... <span>3 页</span>
        """
        m = re.search(r'(\d+)\s*页', html)
        if m:
            return int(m.group(1))

        pages = re.findall(r'/\d+\.html/(\d+)', html)
        if pages:
            return max(int(p) for p in pages)

        return 1

    def should_filter_image(self, url: str) -> bool:
        """过滤占位图、头像和内网地址。"""
        if "default-img" in url:
            return True
        if "avatar" in url:
            return True
        if url.startswith(("http://192.", "http://10.", "http://172.")):
            return True
        return False


# ════════════════════════════════════════════════════════════════════════════════
# 自动注册内置解析器
# ════════════════════════════════════════════════════════════════════════════════

register_site_parser(JDlingyuParser())
