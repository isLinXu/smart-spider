# coding=utf-8
"""搜索引擎策略模式：多模态扩展版。

模态（MediaType）
----------------
- image  : 图片（原有功能）
- video  : 视频（B站、YouTube-DL 协议）
- text   : 文章/网页正文
- audio  : 音频（暂留接口）

每个 Engine 通过 `media_type` 属性声明自己产出什么类型的资源，
下游 SmartSpider 根据此属性将资源分流到不同的保存/过滤管道。

渲染模式（RenderMode）
---------------------
- static  : 直接 HTTP GET，适用于有 JSON API 的引擎
- dynamic : 需要 Playwright 渲染 JS，适用于 SPA 页面
"""
import json
import re
from enum import Enum
from typing import Optional
from urllib.parse import quote_plus, unquote, parse_qs, urlparse


class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    TEXT  = "text"
    AUDIO = "audio"


class RenderMode(str, Enum):
    STATIC  = "static"    # 普通 HTTP 请求即可
    DYNAMIC = "dynamic"   # 需要 Playwright JS 渲染


# ──────────────────────────────────────────────────────────────────────────────
# 基类
# ──────────────────────────────────────────────────────────────────────────────

class SearchEngine:
    """搜索引擎基类。

    子类必须实现：
        - build_search_url(keyword, page) -> str
        - extract_items(html) -> list[dict]  每条结果为 {"url": ..., "meta": {...}}

    可选覆盖：
        - build_check_url(keyword, page) -> str  （默认同 build_search_url）
        - page_step -> int  每页步长（默认 20）
    """

    name: str = ""
    media_type: MediaType = MediaType.IMAGE
    render_mode: RenderMode = RenderMode.STATIC
    page_step: int = 20

    def build_search_url(self, keyword: str, page: int) -> str:
        raise NotImplementedError

    def build_check_url(self, keyword: str, page: int) -> str:
        return self.build_search_url(keyword, page)

    def extract_items(self, html: str) -> list[dict]:
        """解析搜索结果页，返回 item 列表。

        每个 item 格式：
        {
            "url":  str,          # 资源直链
            "meta": {             # 可选附加信息
                "thumb": str,     # 缩略图 URL
                "title": str,     # 标题
                "duration": int,  # 视频时长（秒，视频引擎）
                "size": int,      # 文件大小（字节）
                ...
            }
        }
        """
        raise NotImplementedError

    # 向下兼容旧接口
    def extract_image_urls(self, html: str) -> list[str]:
        return [item["url"] for item in self.extract_items(html)]


# ──────────────────────────────────────────────────────────────────────────────
# 图片引擎
# ──────────────────────────────────────────────────────────────────────────────

class BaiduImageEngine(SearchEngine):
    name = "baidu"
    media_type = MediaType.IMAGE
    render_mode = RenderMode.STATIC

    def build_search_url(self, keyword, page):
        encoded = quote_plus(keyword)
        return (
            f"https://image.baidu.com/search/acjson"
            f"?tn=resultjson_com&logid=0&ipn=rj&ct=201326592&is=&fp=result"
            f"&queryWord={encoded}&cl=2&lm=-1&ie=utf-8&oe=utf-8"
            f"&adpicid=&st=-1&z=&ic=0&hd=&latest=&copyright="
            f"&word={encoded}&s=&se=&tab=&width=&height=&face=0"
            f"&istype=2&qc=&nc=1&fr=&expermode=&force=&pn={page}&rn=30&gsm=1e&1629350504307="
        )

    def build_check_url(self, keyword, page):
        encoded = quote_plus(keyword)
        return f"http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={encoded}&pn={page}"

    def extract_items(self, html):
        items = []
        # 优先解析 JSON API 响应
        try:
            data = json.loads(html)
            for entry in data.get("data", []):
                # objURL 可能是编码格式（非 http 开头），优先使用可直接访问的 URL
                # 优先使用 middleURL（中等尺寸原图），其次 hoverURL，最后 thumbURL
                url = entry.get("middleURL") or entry.get("hoverURL") or entry.get("thumbURL") or entry.get("objURL")
                if url and url.startswith("http"):
                    # 确保百度图片 URL 包含完整参数，避免返回 HTML 错误页
                    if "baidu.com/it/" in url and "fmt=" not in url:
                        url = url + "&fmt=auto&f=JPEG" if url.endswith(("&fm=253", "&fm=253&")) else url
                    items.append({
                        "url": url,
                        "meta": {
                            "thumb": entry.get("thumbURL", ""),
                            "title": entry.get("fromPageTitleEncode", ""),
                            "width": entry.get("width", 0),
                            "height": entry.get("height", 0),
                        }
                    })
        except (json.JSONDecodeError, KeyError):
            # 降级为正则
            for url in re.findall(r'"objURL":"(https?://[^"]+)"', html):
                items.append({"url": url, "meta": {}})
        return items


class BingImageEngine(SearchEngine):
    name = "bing"
    media_type = MediaType.IMAGE
    render_mode = RenderMode.STATIC

    def build_search_url(self, keyword, page):
        encoded = quote_plus(keyword)
        return f"https://www.bing.com/images/search?q={encoded}&first={page}&FORM=IBASEP"

    def extract_items(self, html):
        items = []
        # murl = 原图, turl = 缩略图
        murls = re.findall(r'"murl":"(https?://[^"]+)"', html)
        turls = re.findall(r'"turl":"(https?://[^"]+)"', html)
        titles = re.findall(r'"t":"([^"]*)"', html)
        for i, url in enumerate(murls):
            items.append({
                "url": url,
                "meta": {
                    "thumb": turls[i] if i < len(turls) else "",
                    "title": titles[i] if i < len(titles) else "",
                }
            })
        if not items:
            # Bing 当前图片页常把原图放在详情链接的 mediaurl 查询参数中，
            # 而不是旧版的 murl JSON 字段；兼容 HTML entity + URL 编码。
            for encoded_url in re.findall(
                r"mediaurl=([^&\"'<>\s]+)", html, flags=re.IGNORECASE
            ):
                url = unquote(encoded_url)
                if url.startswith("http"):
                    items.append({"url": url, "meta": {}})
        if not items:
            for url in re.findall(r'"iurl":"(https?://[^"]+)"', html):
                items.append({"url": url, "meta": {}})
        return items


class SogouImageEngine(SearchEngine):
    name = "sogou"
    media_type = MediaType.IMAGE
    render_mode = RenderMode.STATIC

    def build_search_url(self, keyword, page):
        encoded = quote_plus(keyword)
        return f"https://pic.sogou.com/pics?query={encoded}&start={page}&reqType=ajax&tn=resultjson"

    def extract_items(self, html):
        items = []
        try:
            data = json.loads(html)
            for entry in data.get("items", []):
                url = entry.get("picUrl") or entry.get("thumbUrl")
                if url:
                    items.append({
                        "url": url,
                        "meta": {
                            "thumb": entry.get("thumbUrl", ""),
                            "title": entry.get("title", ""),
                        }
                    })
        except (json.JSONDecodeError, KeyError):
            for url in re.findall(r'"thumbUrl":"(https?://[^"]+)"', html):
                items.append({"url": url, "meta": {}})
        return items


class So360ImageEngine(SearchEngine):
    name = "360"
    media_type = MediaType.IMAGE
    render_mode = RenderMode.STATIC

    def build_search_url(self, keyword, page):
        encoded = quote_plus(keyword)
        return f"https://image.so.com/i?q={encoded}&pn={page}&src=srp"

    def extract_items(self, html):
        items = []
        try:
            data = json.loads(html)
            for entry in data.get("list", []):
                url = entry.get("img") or entry.get("url")
                if url and url.startswith("http"):
                    items.append({
                        "url": url,
                        "meta": {
                            "thumb": entry.get("thumb", ""),
                            "title": entry.get("title", ""),
                        }
                    })
        except (json.JSONDecodeError, KeyError):
            for url in re.findall(r'"img":"(https?://[^"]+)"', html):
                items.append({"url": url, "meta": {}})
        return items


class GoogleImageEngine(SearchEngine):
    """Google 图片搜索引擎（自定义搜索 API）。

    需要设置环境变量（或初始化时传参）：
        GOOGLE_API_KEY   — Cloud Console API Key
        GOOGLE_CSE_ID    — Custom Search Engine ID（支持图片搜索）

    如未设置 API Key，自动降级为 HTML 正则抓取模式（不稳定，易被封）。

    获取方式：
        https://developers.google.com/custom-search/v1/overview
    """
    name = "google"
    media_type = MediaType.IMAGE
    render_mode = RenderMode.STATIC
    page_step = 10  # Google CSE 每页最多 10 条

    def __init__(
        self,
        api_key: Optional[str] = None,
        cse_id: Optional[str] = None,
    ):
        import os as _os
        self._api_key = api_key or _os.environ.get("GOOGLE_API_KEY", "")
        self._cse_id  = cse_id  or _os.environ.get("GOOGLE_CSE_ID", "")

    def build_search_url(self, keyword: str, page: int) -> str:
        encoded = quote_plus(keyword)
        start = page + 1  # Google CSE 从 1 开始
        if self._api_key and self._cse_id:
            return (
                f"https://www.googleapis.com/customsearch/v1"
                f"?key={self._api_key}&cx={self._cse_id}"
                f"&q={encoded}&searchType=image&start={start}&num=10"
            )
        # 降级：Google 图片搜索 HTML（容易触发 CAPTCHA，仅备用）
        return f"https://www.google.com/search?q={encoded}&tbm=isch&start={page}"

    def extract_items(self, html: str) -> list[dict]:
        items = []
        # 优先：Google CSE JSON 响应
        try:
            data = json.loads(html)
            for entry in data.get("items", []):
                link = entry.get("link", "")
                if link and link.startswith("http"):
                    image_info = entry.get("image", {})
                    items.append({
                        "url": link,
                        "meta": {
                            "thumb": image_info.get("thumbnailLink", ""),
                            "title": entry.get("title", ""),
                            "width": image_info.get("width", 0),
                            "height": image_info.get("height", 0),
                            "context_url": entry.get("image", {}).get("contextLink", ""),
                        }
                    })
            return items
        except (json.JSONDecodeError, KeyError):
            pass
        # 降级：正则从 HTML 抓图片 URL（不稳定）
        for url in re.findall(r'"ou":"(https?://[^"]+)"', html):
            items.append({"url": url, "meta": {}})
        return items


# ──────────────────────────────────────────────────────────────────────────────
# 视频引擎（返回视频页面 URL，由 VideoDownloader 用 yt-dlp 处理）
# ──────────────────────────────────────────────────────────────────────────────

class BilibiliVideoEngine(SearchEngine):
    """B 站视频搜索引擎。

    返回视频页 URL（https://www.bilibili.com/video/BVxxx），
    由 VideoDownloader 调用 yt-dlp 下载实际视频流。
    """
    name = "bilibili"
    media_type = MediaType.VIDEO
    render_mode = RenderMode.STATIC
    page_step = 20

    def build_search_url(self, keyword, page):
        encoded = quote_plus(keyword)
        pg = page // self.page_step + 1
        return (
            f"https://api.bilibili.com/x/web-interface/search/type"
            f"?search_type=video&keyword={encoded}&page={pg}&pagesize=20"
        )

    def extract_items(self, html):
        items = []
        try:
            data = json.loads(html)
            results = data.get("data", {}).get("result", [])
            for entry in results:
                bvid = entry.get("bvid", "")
                if not bvid:
                    continue
                items.append({
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "meta": {
                        "title": re.sub(r"<[^>]+>", "", entry.get("title", "")),
                        "thumb": ("https:" + entry.get("pic", "")) if entry.get("pic", "").startswith("//") else entry.get("pic", ""),
                        "duration": entry.get("duration", ""),
                        "play": entry.get("play", 0),
                        "author": entry.get("author", ""),
                        "bvid": bvid,
                    }
                })
        except (json.JSONDecodeError, KeyError) as e:
            pass
        return items


class BingVideoEngine(SearchEngine):
    """Bing 视频搜索引擎（静态 API）。"""
    name = "bing_video"
    media_type = MediaType.VIDEO
    render_mode = RenderMode.STATIC
    page_step = 25

    def build_search_url(self, keyword, page):
        encoded = quote_plus(keyword)
        return f"https://www.bing.com/videos/search?q={encoded}&first={page}&FORM=VDRESM"

    def extract_items(self, html):
        items = []
        # Bing 视频结果嵌在 JSON 数据岛中。
        # Bug fix: old regex had no re.DOTALL so ".*?" stopped at newline,
        # causing misses when contentUrl and name appear on separate lines.
        for m in re.finditer(
            r'"contentUrl":"(https?://[^"]+)".*?"name":"([^"]*)"',
            html,
            re.DOTALL,
        ):
            items.append({
                "url": m.group(1),
                "meta": {"title": m.group(2)}
            })
        return items


# ──────────────────────────────────────────────────────────────────────────────
# 文本引擎（返回文章页 URL + 标题摘要，由 TextExtractor 提取正文）
# ──────────────────────────────────────────────────────────────────────────────

class BaiduTextEngine(SearchEngine):
    """百度网页搜索 → 返回文章页 URL。

    百度搜索结果 href 可能是两种形式：
    1. 直链：https://www.example.com/article
    2. 跳转链：https://www.baidu.com/link?url=XXXXXXXX（Base64/编码包装）

    本引擎对跳转链做解码尝试，优先还原为直链；失败时保留跳转链。
    """
    name = "baidu_text"
    media_type = MediaType.TEXT
    render_mode = RenderMode.STATIC
    page_step = 10

    @staticmethod
    def _decode_baidu_link(url: str) -> str:
        """尝试解码百度跳转链 https://www.baidu.com/link?url=...。

        百度跳转链有两种编码：
        - 纯参数形式：?url=https%3A%2F%2F...（普通 URL encode）
        - Base64 混淆形式：需要 HTTP 跟踪跳转才能还原（此处不做网络请求）

        策略：先尝试 URL decode，若解码后仍是 baidu.com 则保留原链。
        """
        if "baidu.com/link" not in url:
            return url
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            raw = params.get("url", [""])[0]
            if raw:
                decoded = unquote(raw)
                if decoded.startswith("http") and "baidu.com" not in decoded:
                    return decoded
        except Exception:
            pass
        return url  # 无法解码，保留跳转链（TextExtractor 会 follow redirect）

    def build_search_url(self, keyword, page):
        encoded = quote_plus(keyword)
        pn = page
        return f"https://www.baidu.com/s?wd={encoded}&pn={pn}&ie=utf-8&oe=utf-8"

    def extract_items(self, html):
        items = []
        # 提取搜索结果条目（href + 标题）
        for m in re.finditer(
            r'<h3[^>]*class="[^"]*t[^"]*"[^>]*>.*?<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL
        ):
            raw_url = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            url = self._decode_baidu_link(raw_url)
            items.append({"url": url, "meta": {"title": title}})
        # 兜底：直接抓 href
        if not items:
            for raw_url in re.findall(r'href="(https?://[^"&]+)"', html):
                if "baidu.com" not in raw_url:
                    items.append({"url": raw_url, "meta": {}})
        return items[:10]


class BingTextEngine(SearchEngine):
    """Bing 网页搜索 → 返回文章页 URL。"""
    name = "bing_text"
    media_type = MediaType.TEXT
    render_mode = RenderMode.STATIC
    page_step = 10

    def build_search_url(self, keyword, page):
        encoded = quote_plus(keyword)
        first = page + 1
        return f"https://www.bing.com/search?q={encoded}&first={first}"

    def extract_items(self, html):
        items = []
        for m in re.finditer(
            r'<li[^>]+class="b_algo"[^>]*>.*?<h2>.*?<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL
        ):
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            items.append({"url": m.group(1), "meta": {"title": title}})
        return items


# ──────────────────────────────────────────────────────────────────────────────
# 动态渲染引擎（Playwright）
# ──────────────────────────────────────────────────────────────────────────────

class WeixinArticleEngine(SearchEngine):
    """微信公众号文章搜索（sogou weixin），需要 JS 渲染。"""
    name = "weixin"
    media_type = MediaType.TEXT
    render_mode = RenderMode.DYNAMIC
    page_step = 10

    def build_search_url(self, keyword, page):
        encoded = quote_plus(keyword)
        pg = page // self.page_step + 1
        return f"https://weixin.sogou.com/weixin?type=2&query={encoded}&page={pg}"

    def extract_items(self, html):
        items = []
        for m in re.finditer(
            r'<h3>.*?<a[^>]+href="(https?://mp\.weixin\.qq\.com/[^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL
        ):
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            items.append({"url": m.group(1), "meta": {"title": title}})
        return items


class XiaohongshuEngine(SearchEngine):
    """小红书笔记搜索（动态渲染，需要 Playwright + Cookie）。"""
    name = "xiaohongshu"
    media_type = MediaType.IMAGE   # 图文混合，以图为主
    render_mode = RenderMode.DYNAMIC
    page_step = 20

    def build_search_url(self, keyword, page):
        encoded = quote_plus(keyword)
        return f"https://www.xiaohongshu.com/search_result?keyword={encoded}&source=web_search_result_notes"

    def extract_items(self, html):
        items = []
        # 小红书 JSON 数据嵌在 window.__INITIAL_STATE__ 中
        m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>', html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                notes = (data.get("search", {})
                            .get("noteList", []))
                for note in notes:
                    img = note.get("cover", {}).get("urlDefault", "")
                    if img:
                        items.append({
                            "url": "https:" + img if img.startswith("//") else img,
                            "meta": {
                                "title": note.get("title", ""),
                                "note_id": note.get("id", ""),
                                "likes": note.get("likedCount", 0),
                            }
                        })
            except (json.JSONDecodeError, KeyError):
                pass
        return items


class WeiboPicEngine(SearchEngine):
    """微博图片搜索引擎（微博综合搜索 API，无需登录）。

    接口说明
    --------
    使用微博搜索 API 获取含图微博，提取 original_pic 字段（原图 URL）。
    每页约 20 条，默认 static 渲染。

    注意
    ----
    - 微博对高频请求限速，建议配合代理池使用
    - 部分账号内容需登录可见，可通过 Cookie 注入提升覆盖率
    """
    name = "weibo"
    media_type = MediaType.IMAGE
    render_mode = RenderMode.STATIC
    page_step = 20

    def build_search_url(self, keyword: str, page: int) -> str:
        encoded = quote_plus(keyword)
        pg = page // self.page_step + 1
        # 微博综合搜索 API：type=1 为含图过滤
        return (
            f"https://m.weibo.cn/api/container/getIndex"
            f"?containerid=100103type%3D1%26q%3D{encoded}"
            f"&page_type=searchall&page={pg}"
        )

    def extract_items(self, html: str) -> list[dict]:
        items = []
        try:
            data = json.loads(html)
            cards = data.get("data", {}).get("cards", [])
            for card in cards:
                # 支持直接 card 和嵌套 card_group
                mblog = card.get("mblog") or {}
                sub_cards = card.get("card_group", [])
                all_mblogs = [mblog] + [c.get("mblog", {}) for c in sub_cards if c.get("mblog")]
                for mb in all_mblogs:
                    if not mb:
                        continue
                    # 优先取 original_pic（原图），降级 thumbnail_pic
                    pic_url = mb.get("original_pic") or mb.get("thumbnail_pic", "")
                    if not pic_url or not pic_url.startswith("http"):
                        continue
                    # 将缩略图 URL 转为原图（sinaimg.cn 规则：/thumb150/ → /large/）
                    pic_url = re.sub(r"/thumb\d+/", "/large/", pic_url)
                    pic_url = re.sub(r"/small/", "/large/", pic_url)
                    title = re.sub(r"<[^>]+>", "", mb.get("text", ""))[:100]
                    items.append({
                        "url": pic_url,
                        "meta": {
                            "title": title,
                            "user": mb.get("user", {}).get("screen_name", ""),
                            "reposts": mb.get("reposts_count", 0),
                            "likes": mb.get("attitudes_count", 0),
                            "mid": mb.get("mid", ""),
                        }
                    })
        except (json.JSONDecodeError, KeyError, AttributeError):
            # 降级：从 HTML 中抓 sinaimg.cn 原图
            for url in re.findall(r'https?://wx\d+\.sinaimg\.cn/large/[^"\'>\s]+', html):
                items.append({"url": url, "meta": {}})
        return items


# ──────────────────────────────────────────────────────────────────────────────
# 注册表
# ──────────────────────────────────────────────────────────────────────────────

ENGINE_REGISTRY: dict[str, SearchEngine] = {
    # 图片
    "baidu":        BaiduImageEngine(),
    "bing":         BingImageEngine(),
    "sogou":        SogouImageEngine(),
    "360":          So360ImageEngine(),
    "google":       GoogleImageEngine(),
    "weibo":        WeiboPicEngine(),
    # 视频
    "bilibili":     BilibiliVideoEngine(),
    "bing_video":   BingVideoEngine(),
    # 文本
    "baidu_text":   BaiduTextEngine(),
    "bing_text":    BingTextEngine(),
    "weixin":       WeixinArticleEngine(),
    # 图文混合
    "xiaohongshu":  XiaohongshuEngine(),
}


def get_engine(name: str) -> SearchEngine:
    engine = ENGINE_REGISTRY.get(name)
    if engine is None:
        supported = ", ".join(sorted(ENGINE_REGISTRY.keys()))
        raise ValueError(f"Unknown engine '{name}'. Supported: {supported}")
    return engine


def get_engines_by_media(media_type: MediaType) -> list[SearchEngine]:
    """获取指定模态的所有引擎。"""
    return [e for e in ENGINE_REGISTRY.values() if e.media_type == media_type]


# ──────────────────────────────────────────────────────────────────────────────
# 引擎健康检查
# ──────────────────────────────────────────────────────────────────────────────

def check_engine_health(
    engine_name: str,
    keyword: str = "test",
    http_client=None,
    timeout: int = 5,
) -> dict:
    """快速探测引擎是否可用（返回非空结果）。

    Args:
        engine_name: 引擎名称
        keyword:     探测用关键词（默认 "test"）
        http_client: SmartHttpClient 实例（为 None 时使用裸 requests）
        timeout:     请求超时秒数

    Returns:
        {
            "engine": str,
            "ok": bool,           # 是否返回了 >=1 条结果
            "item_count": int,    # 实际返回条数
            "latency_ms": float,  # 请求耗时毫秒
            "error": str,         # 错误信息（ok=False 时）
        }
    """
    import time as _time
    eng = get_engine(engine_name)
    url = eng.build_check_url(keyword, 0)
    t0 = _time.monotonic()
    error_msg = ""
    items = []
    try:
        if http_client is not None:
            html = http_client.get_text(url, engine=engine_name)
        else:
            import requests as _req
            resp = _req.get(
                url,
                timeout=timeout,
                stream=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            try:
                max_health_bytes = 2 * 1024 * 1024
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > max_health_bytes:
                    raise ValueError("health response exceeds 2 MiB")
                chunks = []
                total = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_health_bytes:
                        raise ValueError("health response exceeds 2 MiB")
                    chunks.append(chunk)
                encoding = getattr(resp, "encoding", None) or "utf-8"
                html = b"".join(chunks).decode(encoding, errors="replace")
            finally:
                resp.close()
        items = eng.extract_items(html)
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"

    latency_ms = (_time.monotonic() - t0) * 1000
    ok = len(items) > 0
    return {
        "engine": engine_name,
        "ok": ok,
        "item_count": len(items),
        "latency_ms": round(latency_ms, 1),
        "error": error_msg,
    }


def check_all_engines(keyword: str = "test", http_client=None) -> list[dict]:
    """并行健康检查所有静态引擎（dynamic 引擎跳过）。

    Returns:
        按 latency_ms 升序排列的健康状态列表
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
    static_engines = [
        name for name, eng in ENGINE_REGISTRY.items()
        if eng.render_mode == RenderMode.STATIC
    ]
    results = []
    with ThreadPoolExecutor(max_workers=len(static_engines)) as pool:
        futures = {
            pool.submit(check_engine_health, name, keyword, http_client): name
            for name in static_engines
        }
        for f in _as_completed(futures):
            results.append(f.result())
    results.sort(key=lambda x: (not x["ok"], x["latency_ms"]))
    return results
