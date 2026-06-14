# coding=utf-8
"""ReAct 循环引擎。

核心循环:
1. 观察 (Observe): 通过 PagePerception 获取页面状态
2. 思考 (Think): 使用 LLM 分析页面状态，决定下一步行动
3. 行动 (Act): 通过 BrowserController 执行操作（点击、输入、滚动等）

设计原则
--------
1. LLM 无关：支持 OpenAI / Qwen / 本地模型，通过 LLMBackend 抽象
2. 工具可扩展：通过 ToolRegistry 注册自定义工具
3. 可中断：支持 max_iterations 和 timeout 防止无限循环
4. 可观测：每次循环记录 observation / thought / action，便于调试

用法
----
>>> from smart_spider.decision import ReActAgent
>>> from smart_spider.browser_controller import BrowserController
>>> from smart_spider.perception import PagePerception
>>>
>>> agent = ReActAgent(
...     browser_controller=browser_controller,
...     perception=perception,
...     llm_backend=llm_backend,
... )
>>> result = agent.run("搜索关于猫的图片")
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from loguru import logger

from ..perception.page_perception import PagePerception, PageState
from ..browser_controller import BrowserController


# ──────────────────────────────────────────────────────────────────────────────
# 数据类
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Action:
    """行动指令。

    由 LLM 生成，描述 agent 要执行的操作。
    """
    name: str          # 工具名称（如 "navigate", "click", "type_text"）
    args: dict = field(default_factory=dict)  # 工具参数
    thought: str = ""  # LLM 的思考过程（用于调试）

    @classmethod
    def from_json(cls, json_str: str) -> "Action":
        """从 JSON 字符串解析 Action。"""
        try:
            data = json.loads(json_str)
            return cls(
                name=data.get("action", "unknown"),
                args=data.get("args", {}),
                thought=data.get("thought", ""),
            )
        except json.JSONDecodeError:
            # 尝试从文本中提取 action
            match = re.search(r'"action"\s*:\s*"(\w+)"', json_str)
            if match:
                return cls(name=match.group(1))
            return cls(name="unknown")

    def to_json(self) -> str:
        """转换为 JSON 字符串。"""
        return json.dumps({
            "action": self.name,
            "args": self.args,
            "thought": self.thought,
        }, ensure_ascii=False)


@dataclass
class StepRecord:
    """单步记录。

    记录 ReAct 循环中每一步的观察、思考和行动。
    """
    step: int
    observation: str = ""
    thought: str = ""
    action: Optional[Action] = None
    result: Any = None
    error: Optional[str] = None
    timestamp: float = 0.0


@dataclass
class AgentResult:
    """Agent 运行结果。"""
    success: bool
    answer: str = ""
    steps: list[StepRecord] = field(default_factory=list)
    total_time: float = 0.0
    error: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────────
# LLM 后端抽象
# ──────────────────────────────────────────────────────────────────────────────

class LLMBackend:
    """LLM 后端抽象基类。

    子类需要实现 generate() 方法。
    """

    def generate(self, prompt: str, system_prompt: str = "") -> str:
        """生成文本。

        Args:
            prompt: 用户提示
            system_prompt: 系统提示

        Returns:
            生成的文本
        """
        raise NotImplementedError


class OpenAIBackend(LLMBackend):
    """OpenAI API 后端。"""

    def __init__(self, model: str = "gpt-4o", api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = base_url

    def generate(self, prompt: str, system_prompt: str = "") -> str:
        """调用 OpenAI API 生成文本。"""
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.1,
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            return ""


class QwenBackend(LLMBackend):
    """Qwen API 后端。"""

    def __init__(self, model: str = "qwen-vl-plus", api_key: Optional[str] = None):
        self.model = model
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")

    def generate(self, prompt: str, system_prompt: str = "") -> str:
        """调用 Qwen API 生成文本。"""
        try:
            import dashscope
            dashscope.api_key = self.api_key
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            response = dashscope.Generation.call(
                model=self.model,
                messages=messages,
                result_format="message",
            )
            return response.output.choices[0].message.content
        except Exception as e:
            logger.error(f"Qwen API error: {e}")
            return ""


# ──────────────────────────────────────────────────────────────────────────────
# 工具注册表
# ──────────────────────────────────────────────────────────────────────────────

class ToolRegistry:
    """工具注册表。

    管理所有可用的工具函数，供 ReAct 循环调用。
    """

    def __init__(self):
        self._tools: dict[str, Callable] = {}
        self._descriptions: dict[str, str] = {}

    def register(self, name: str, func: Callable, description: str = ""):
        """注册工具函数。"""
        self._tools[name] = func
        self._descriptions[name] = description

    def get(self, name: str) -> Optional[Callable]:
        """获取工具函数。"""
        return self._tools.get(name)

    def get_description(self, name: str) -> str:
        """获取工具描述。"""
        return self._descriptions.get(name, "")

    def list_tools(self) -> list[dict]:
        """列出所有工具。"""
        return [
            {"name": name, "description": desc}
            for name, desc in self._descriptions.items()
        ]

    def format_tools_prompt(self) -> str:
        """格式化工具列表为 LLM 提示。"""
        lines = []
        for name, desc in self._descriptions.items():
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# ReAct Agent
# ──────────────────────────────────────────────────────────────────────────────

# 系统 Prompt 模板
_SYSTEM_PROMPT = """你是一个智能浏览器代理。你可以通过浏览器操作来完成任务。

可用工具:
{tools_prompt}

当前任务: {task}

请按照以下格式回复:
1. Thought: 分析当前页面状态，思考下一步该做什么
2. Action: 选择一个工具执行，格式为 JSON: {{"action": "工具名", "args": {{参数}}}}

注意:
- 每次只执行一个操作
- 如果任务已完成，使用 finish 工具返回结果
- 如果遇到错误，尝试其他方法
- 最多执行 {max_iterations} 步
"""

_OBSERVATION_PROMPT = """当前页面状态:
{observation}

历史操作:
{history}

请分析当前状态并决定下一步操作。"""


class ReActAgent:
    """ReAct 决策代理。

    实现 观察→思考→行动 的决策循环，驱动 BrowserController。

    属性
    ----
    browser_controller : BrowserController
        浏览器控制器实例
    perception : PagePerception
        页面感知器实例
    llm_backend : LLMBackend
        LLM 后端实例
    tool_registry : ToolRegistry
        工具注册表
    max_iterations : int
        最大循环次数
    """

    def __init__(
        self,
        browser_controller: BrowserController,
        perception: PagePerception,
        llm_backend: LLMBackend,
        max_iterations: int = 10,
        verbose: bool = True,
    ):
        self.browser_controller = browser_controller
        self.perception = perception
        self.llm_backend = llm_backend
        self.max_iterations = max_iterations
        self.verbose = verbose
        self.tool_registry = ToolRegistry()

        # 注册内置工具
        self._register_builtin_tools()

    def _register_builtin_tools(self):
        """注册内置工具。"""
        self.tool_registry.register(
            "navigate",
            self._tool_navigate,
            "导航到指定 URL。参数: url (str)",
        )
        self.tool_registry.register(
            "click",
            self._tool_click,
            "点击页面元素。参数: selector (str)",
        )
        self.tool_registry.register(
            "type_text",
            self._tool_type_text,
            "在输入框中输入文本并按 Enter。参数: selector (str), text (str)",
        )
        self.tool_registry.register(
            "scroll",
            self._tool_scroll,
            "滚动页面到底部。无参数",
        )
        self.tool_registry.register(
            "screenshot",
            self._tool_screenshot,
            "保存页面截图。参数: path (str, 可选)",
        )
        self.tool_registry.register(
            "extract_links",
            self._tool_extract_links,
            "从当前页面提取链接列表。无参数",
        )
        self.tool_registry.register(
            "extract_images",
            self._tool_extract_images,
            "从当前页面提取图片列表。无参数",
        )
        self.tool_registry.register(
            "clip_filter",
            self._tool_clip_filter,
            "使用 CLIP 过滤图片相关性。参数: keyword (str), threshold (float, 可选)",
        )
        self.tool_registry.register(
            "finish",
            self._tool_finish,
            "完成任务，返回结果。参数: answer (str)",
        )

    # ──────────────────────────────────────────────────────────────────────
    # 工具实现
    # ──────────────────────────────────────────────────────────────────────

    def _tool_navigate(self, url: str, **kwargs) -> str:
        """导航到指定 URL。"""
        html = self.browser_controller.navigate(url)
        if html:
            return f"成功导航到 {url}，页面长度 {len(html)} 字符"
        return f"导航失败: {url}"

    def _tool_click(self, selector: str, **kwargs) -> str:
        """点击页面元素。"""
        success, html = self.browser_controller.click(selector)
        if success:
            return f"成功点击: {selector}，页面已更新（{len(html)} 字符）"
        return f"点击失败: {selector}"

    def _tool_type_text(self, selector: str, text: str, **kwargs) -> str:
        """在输入框中输入文本。"""
        success, html = self.browser_controller.type_text(selector, text)
        if success:
            return f"成功输入文本到: {selector}，页面已更新（{len(html)} 字符）"
        return f"输入失败: {selector}"

    def _tool_scroll(self, **kwargs) -> str:
        """滚动页面到底部。"""
        success, html = self.browser_controller.scroll()
        if success:
            return f"成功滚动到底部（{len(html)} 字符）"
        return "滚动失败"

    def _tool_screenshot(self, path: str = "", **kwargs) -> str:
        """保存页面截图。"""
        success, saved_path = self.browser_controller.screenshot(path)
        if success:
            return f"截图已保存: {saved_path}"
        return "截图失败"

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

        images = self.perception._extract_images(html)
        if not images:
            return "当前页面无图片可供过滤"

        # 利用 perception 计算 CLIP 分数
        state = self.perception.perceive(html, url=self.browser_controller.current_url)
        state = self.perception.compute_clip_scores(state, [keyword])

        scores = state.clip_scores
        if not scores or keyword not in scores:
            return f"CLIP 过滤失败：无法计算关键词 '{keyword}' 的相似度"

        score = scores[keyword]
        passed = score > threshold
        return f"CLIP 相似度: {score:.4f} (阈值: {threshold}) → {'通过' if passed else '未通过'}"

    def _tool_finish(self, answer: str = "", **kwargs) -> str:
        """完成任务。"""
        return answer or "任务完成"

    # ──────────────────────────────────────────────────────────────────────
    # ReAct 循环
    # ──────────────────────────────────────────────────────────────────────

    def run(self, task: str, start_url: str = "") -> AgentResult:
        """运行 ReAct 循环。

        Args:
            task: 任务描述
            start_url: 起始 URL（可选）

        Returns:
            AgentResult 运行结果
        """
        start_time = time.time()
        steps: list[StepRecord] = []
        current_url = start_url
        current_html = ""

        # 如果有起始 URL，先导航
        if start_url:
            current_html = self.browser_controller.navigate(start_url)
            current_url = start_url

        for step in range(1, self.max_iterations + 1):
            step_start = time.time()

            # 1. 观察
            state = self.perception.perceive(current_html, url=current_url)
            observation = state.to_observation()

            if self.verbose:
                logger.info(f"[Step {step}] Observation: {observation[:200]}")

            # 2. 思考 + 行动（调用 LLM）
            history = self._format_history(steps)
            prompt = _OBSERVATION_PROMPT.format(
                observation=observation,
                history=history,
            )
            system_prompt = _SYSTEM_PROMPT.format(
                tools_prompt=self.tool_registry.format_tools_prompt(),
                task=task,
                max_iterations=self.max_iterations,
            )

            llm_response = self.llm_backend.generate(prompt, system_prompt=system_prompt)

            if not llm_response:
                record = StepRecord(
                    step=step,
                    observation=observation,
                    error="LLM returned empty response",
                    timestamp=time.time() - step_start,
                )
                steps.append(record)
                continue

            # 3. 解析行动
            action = self._parse_action(llm_response)

            if self.verbose:
                logger.info(f"[Step {step}] Thought: {action.thought}")
                logger.info(f"[Step {step}] Action: {action.name}({action.args})")

            # 4. 执行行动
            result = self._execute_action(action)

            record = StepRecord(
                step=step,
                observation=observation,
                thought=action.thought,
                action=action,
                result=result,
                timestamp=time.time() - step_start,
            )
            steps.append(record)

            # 5. 检查是否完成
            if action.name == "finish":
                return AgentResult(
                    success=True,
                    answer=str(result),
                    steps=steps,
                    total_time=time.time() - start_time,
                )

            # 6. 更新页面状态
            # navigate 已经在 BrowserController 内部更新了 current_html
            # click / type_text / scroll 也已经更新了 current_html
            # 对于所有交互操作，从 BrowserController 获取最新的 HTML
            if action.name in ("navigate", "click", "type_text", "scroll"):
                updated_html = self.browser_controller.current_html
                if updated_html:
                    current_html = updated_html
                if action.name == "navigate" and "url" in action.args:
                    current_url = action.args["url"]
                elif self.browser_controller.current_url:
                    current_url = self.browser_controller.current_url

        # 达到最大步数
        return AgentResult(
            success=False,
            answer="",
            steps=steps,
            total_time=time.time() - start_time,
            error=f"Max iterations ({self.max_iterations}) reached",
        )

    def _parse_action(self, llm_response: str) -> Action:
        """从 LLM 响应中解析行动。"""
        # 尝试提取 JSON 格式的行动
        json_match = re.search(r'\{[^{}]*"action"[^{}]*\}', llm_response, re.DOTALL)
        if json_match:
            try:
                return Action.from_json(json_match.group(0))
            except Exception:
                pass

        # 尝试提取 Thought 和 Action
        thought = ""
        thought_match = re.search(r"Thought:\s*(.*?)(?:\n|$)", llm_response, re.DOTALL)
        if thought_match:
            thought = thought_match.group(1).strip()

        action_match = re.search(r"Action:\s*(.*?)(?:\n|$)", llm_response, re.DOTALL)
        if action_match:
            action_text = action_match.group(1).strip()
            # 尝试从 action_text 中提取 JSON
            json_match = re.search(r'\{.*\}', action_text, re.DOTALL)
            if json_match:
                try:
                    return Action.from_json(json_match.group(0))
                except Exception:
                    pass

        return Action(name="unknown", thought=thought)

    def _execute_action(self, action: Action) -> Any:
        """执行行动。"""
        tool = self.tool_registry.get(action.name)
        if tool is None:
            logger.error(f"Unknown tool: {action.name}")
            return f"Unknown tool: {action.name}"

        try:
            result = tool(**action.args)
            return result
        except Exception as e:
            logger.error(f"Tool execution error: {e}")
            return f"Error: {e}"

    def _format_history(self, steps: list[StepRecord]) -> str:
        """格式化历史记录。"""
        if not steps:
            return "无历史操作"

        lines = []
        for step in steps[-5:]:  # 最多保留最近 5 步
            line = f"Step {step.step}: "
            if step.thought:
                line += f"Thought: {step.thought[:100]} | "
            if step.action:
                line += f"Action: {step.action.name}({step.action.args}) | "
            if step.result:
                line += f"Result: {str(step.result)[:100]}"
            lines.append(line)

        return "\n".join(lines)
