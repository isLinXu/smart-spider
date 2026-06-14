# coding=utf-8
"""Browser-Use Agent 入口。

将 BrowserController + PagePerception + ReActAgent 串联为完整的 agent，
同时桥接到 SmartSpider 的基础设施（UrlDeduplicator、CrawlStats、CLIP 推理管线）。

两种使用模式
------------
1. **独立模式**：直接创建 BrowserUseAgent，自带浏览器和 HTTP 客户端
2. **嵌入模式**：从 SmartSpider 实例创建，共享去重器、统计、回调等

用法
----
>>> # 独立模式
>>> from smart_spider.agent import BrowserUseAgent
>>> agent = BrowserUseAgent(llm_model="gpt-4o", llm_api_key="sk-xxx")
>>> result = agent.run("搜索关于猫的图片", start_url="https://www.google.com")
>>> agent.close()
>>>
>>> # 嵌入模式（从 SmartSpider 创建）
>>> spider = SmartSpider(keywords=["猫咪"], max_items=50)
>>> agent = BrowserUseAgent.from_spider(spider, llm_model="gpt-4o", llm_api_key="sk-xxx")
>>> result = agent.run("在小红书上找猫咪图片", start_url="https://www.xiaohongshu.com")
>>> agent.close()
"""
import os
from typing import Any, Optional

from loguru import logger

from .browser_controller import BrowserController
from .perception import PagePerception, PageState
from .decision import (
    ReActAgent,
    LLMBackend,
    OpenAIBackend,
    QwenBackend,
    ToolRegistry,
    Action,
    StepRecord,
    AgentResult,
)
from .http_client import SmartHttpClient, ProxyPool
from .tools import register_tool, get_tool, list_tools


class BrowserUseAgent:
    """Browser-Use Agent。

    将 BrowserController + PagePerception + ReActAgent 串联为完整的 agent，
    同时桥接到 SmartSpider 的基础设施。

    属性
    ----
    browser_controller : BrowserController
        浏览器控制器
    perception : PagePerception
        页面感知器
    react_agent : ReActAgent
        ReAct 决策代理
    http_client : SmartHttpClient
        HTTP 客户端（用于下载图片等）
    deduplicator : UrlDeduplicator or None
        URL 去重器（嵌入模式下共享 SmartSpider 的实例）
    stats : CrawlStats or None
        采集统计（嵌入模式下共享 SmartSpider 的实例）
    """

    def __init__(
        self,
        # LLM 配置
        llm_model: str = "gpt-4o",
        llm_api_key: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        # 浏览器配置
        headless: bool = True,
        proxy: Optional[str] = None,
        cookies: Optional[list[dict]] = None,
        # 感知配置
        enable_clip: bool = True,
        enable_ocr: bool = False,
        clip_model_name: str = "ViT-B/32",
        clip_device: str = "cpu",
        # Agent 配置
        max_iterations: int = 10,
        verbose: bool = True,
        # SmartSpider 基础设施（嵌入模式）
        http_client: Optional[SmartHttpClient] = None,
        deduplicator: Optional[Any] = None,  # UrlDeduplicator
        stats: Optional[Any] = None,  # CrawlStats
        callbacks: Optional[list] = None,  # list[CallbackFn]
        proxies: Optional[list[str]] = None,
    ):
        """初始化 Agent。

        Args:
            llm_model: LLM 模型名称
            llm_api_key: LLM API Key
            llm_base_url: LLM API Base URL
            headless: 是否使用无头浏览器
            proxy: 代理地址
            cookies: Cookie 列表
            enable_clip: 是否启用 CLIP
            enable_ocr: 是否启用 OCR
            clip_model_name: CLIP 模型名称
            clip_device: CLIP 推理设备
            max_iterations: 最大循环次数
            verbose: 是否打印详细日志
            http_client: 共享的 SmartHttpClient（嵌入模式）
            deduplicator: 共享的 UrlDeduplicator（嵌入模式）
            stats: 共享的 CrawlStats（嵌入模式）
            callbacks: 共享的回调列表（嵌入模式）
            proxies: 代理列表
        """
        # 1. 浏览器控制器（使用 PersistentBrowserSession）
        self.browser_controller = BrowserController(
            headless=headless,
            proxy=proxy,
            cookies=cookies,
        )

        # 2. 感知器
        self.perception = PagePerception(
            enable_clip=enable_clip,
            enable_ocr=enable_ocr,
            clip_model_name=clip_model_name,
            clip_device=clip_device,
        )

        # 3. LLM 后端
        self.llm_backend = self._create_llm_backend(
            llm_model, llm_api_key, llm_base_url
        )

        # 4. ReAct Agent
        self.react_agent = ReActAgent(
            browser_controller=self.browser_controller,
            perception=self.perception,
            llm_backend=self.llm_backend,
            max_iterations=max_iterations,
            verbose=verbose,
        )

        # 5. 共享基础设施（嵌入模式下由 SmartSpider 提供）
        self.http_client = http_client or SmartHttpClient(
            proxies=proxies or [],
            rate=5.0,
            max_retries=3,
            timeout=10,
        )
        self.deduplicator = deduplicator
        self.stats = stats
        self.callbacks = callbacks or []

        # 6. 注册额外工具（注入依赖）
        self._register_extra_tools()

    @classmethod
    def from_spider(
        cls,
        spider: "SmartSpider",
        llm_model: str = "gpt-4o",
        llm_api_key: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        max_iterations: int = 10,
        verbose: bool = True,
    ) -> "BrowserUseAgent":
        """从 SmartSpider 实例创建 Agent（嵌入模式）。

        共享 SmartSpider 的基础设施：
        - SmartHttpClient（代理池、限速、TLS 指纹）
        - UrlDeduplicator（BloomFilter 去重）
        - CrawlStats（采集统计）
        - CrawlEvent 回调钩子
        - CLIP 模型（如果已加载）

        Args:
            spider: SmartSpider 实例
            llm_model: LLM 模型名称
            llm_api_key: LLM API Key
            llm_base_url: LLM API Base URL
            max_iterations: 最大循环次数
            verbose: 是否打印详细日志

        Returns:
            BrowserUseAgent 实例
        """
        # 从 spider 获取代理
        proxy = spider._browser_proxy or (
            spider._proxy_pool.get().url if not spider._proxy_pool.is_empty() else None
        )

        # 共享 CLIP 模型：如果 spider 已加载 CLIP，复用它
        clip_device = getattr(spider, "device", "cpu")
        clip_model_name = getattr(spider, "clip_model_name", "ViT-B/32")

        agent = cls(
            llm_model=llm_model,
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            headless=spider._headless,
            proxy=proxy,
            cookies=spider._renderer_cookies,
            enable_clip="image" in spider.media_types,
            clip_model_name=clip_model_name,
            clip_device=clip_device,
            max_iterations=max_iterations,
            verbose=verbose,
            http_client=spider._http,
            deduplicator=spider._dedup,
            stats=spider.stats,
            callbacks=spider._callbacks,
            proxies=[],  # 已通过 http_client 共享
        )

        # 如果 spider 已加载 CLIP 模型，直接注入到 perception
        if spider.model is not None and spider.preprocess is not None:
            from .perception.clip_inference import CLIPInference
            clip_inf = CLIPInference.__new__(CLIPInference)
            clip_inf.device = spider.device
            clip_inf.model_name = spider.clip_model_name
            clip_inf.model = spider.model
            clip_inf.preprocess = spider.preprocess
            agent.perception._clip = clip_inf
            logger.info("Shared CLIP model from SmartSpider")

        return agent

    def _create_llm_backend(
        self,
        model: str,
        api_key: Optional[str],
        base_url: Optional[str],
    ) -> LLMBackend:
        """创建 LLM 后端。"""
        if "gpt" in model.lower() or "o1" in model.lower() or "o3" in model.lower():
            return OpenAIBackend(model=model, api_key=api_key, base_url=base_url)
        elif "qwen" in model.lower():
            return QwenBackend(model=model, api_key=api_key)
        else:
            return OpenAIBackend(model=model, api_key=api_key, base_url=base_url)

    def _register_extra_tools(self):
        """注册额外工具（注入依赖）。"""
        self.react_agent.tool_registry.register(
            "download_image",
            self._tool_download_image,
            "下载图片到本地。参数: url (str), save_dir (str, 可选, 默认 ./output)",
        )
        self.react_agent.tool_registry.register(
            "extract_links",
            self._tool_extract_links,
            "从当前页面提取链接列表。无参数",
        )
        self.react_agent.tool_registry.register(
            "extract_images",
            self._tool_extract_images,
            "从当前页面提取图片列表。无参数",
        )
        self.react_agent.tool_registry.register(
            "clip_filter",
            self._tool_clip_filter,
            "使用 CLIP 过滤图片相关性。参数: keyword (str), threshold (float, 可选)",
        )
        self.react_agent.tool_registry.register(
            "content_dedup",
            self._tool_content_dedup,
            "检查内容是否重复。参数: url (str)",
        )

    def _tool_download_image(self, url: str, save_dir: str = "./output", **kwargs) -> str:
        """下载图片（使用共享的 SmartHttpClient）。"""
        try:
            peek, resp = self.http_client.get_stream(url, peek_bytes=8192)
            try:
                rest = b"".join(resp.iter_content(chunk_size=65536))
                content = peek + rest
            finally:
                resp.close()

            if not content:
                return f"Download failed: {url}"

            # 内容去重
            if self.deduplicator and self.deduplicator.is_content_seen(content):
                return f"Content duplicate, skipped: {url[:60]}"

            import hashlib
            name = hashlib.md5(url.encode()).hexdigest()

            # 检测扩展名
            ext = ".jpg"
            if content[:8] == b'\x89PNG\r\n\x1a\n':
                ext = ".png"
            elif content[:4] == b'RIFF' and content[8:12] == b'WEBP':
                ext = ".webp"

            fp = os.path.join(save_dir, f"{name}{ext}")
            os.makedirs(save_dir, exist_ok=True)
            with open(fp, "wb") as f:
                f.write(content)

            # 更新统计
            if self.stats:
                self.stats.inc_saved("image")

            return f"Downloaded: {fp}"
        except Exception as e:
            if self.stats:
                self.stats.inc_failed("image")
            return f"Error: {e}"

    def _tool_extract_links(self, **kwargs) -> str:
        """从当前页面提取链接列表。"""
        html = self.browser_controller.current_html
        if not html:
            return "当前无页面内容，请先使用 navigate 导航"
        links = self.perception._extract_links(html)
        if not links:
            return "未找到链接"
        result_lines = [f"共找到 {len(links)} 个链接:"]
        for i, link in enumerate(links[:30]):
            result_lines.append(f"  [{i+1}] {link.get('text', '')[:40]} → {link.get('href', '')[:80]}")
        return "\n".join(result_lines)

    def _tool_extract_images(self, **kwargs) -> str:
        """从当前页面提取图片列表。"""
        html = self.browser_controller.current_html
        if not html:
            return "当前无页面内容，请先使用 navigate 导航"
        images = self.perception._extract_images(html)
        if not images:
            return "未找到图片"
        result_lines = [f"共找到 {len(images)} 张图片:"]
        for i, img in enumerate(images[:20]):
            result_lines.append(f"  [{i+1}] {img.get('src', '')[:80]} (alt: {img.get('alt', '')[:30]})")
        return "\n".join(result_lines)

    def _tool_clip_filter(self, keyword: str, threshold: float = 0.25, **kwargs) -> str:
        """使用 CLIP 过滤图片相关性。"""
        html = self.browser_controller.current_html
        if not html:
            return "当前无页面内容，请先使用 navigate 导航"

        state = self.perception.perceive(html, url=self.browser_controller.current_url)
        state = self.perception.compute_clip_scores(state, [keyword], http_client=self.http_client)

        scores = state.clip_scores
        if not scores or keyword not in scores:
            return f"CLIP 过滤失败：无法计算关键词 '{keyword}' 的相似度"

        score = scores[keyword]
        passed = score > threshold
        return f"CLIP 相似度: {score:.4f} (阈值: {threshold}) → {'通过' if passed else '未通过'}"

    def _tool_content_dedup(self, url: str, **kwargs) -> str:
        """检查内容是否重复。"""
        if self.deduplicator is None:
            return "去重器未启用"

        # 先检查 URL 去重
        if self.deduplicator.is_seen(url):
            return f"URL 已存在: {url[:60]}"

        return f"URL 未重复: {url[:60]}"

    def run(self, task: str, start_url: str = "") -> AgentResult:
        """运行 Agent。

        Args:
            task: 任务描述
            start_url: 起始 URL

        Returns:
            AgentResult 运行结果
        """
        logger.info(f"Agent started: task='{task}', start_url='{start_url}'")
        result = self.react_agent.run(task, start_url=start_url)
        logger.info(
            f"Agent finished: success={result.success}, "
            f"steps={len(result.steps)}, time={result.total_time:.2f}s"
        )
        return result

    def close(self):
        """关闭 Agent，释放资源。"""
        self.browser_controller.close()
        logger.info("Agent closed")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
