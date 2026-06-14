# coding=utf-8
"""ReAct 决策核心。

实现 观察→思考→行动 的决策循环，驱动 BrowserController。

核心类
------
- ReActAgent    : ReAct 决策代理
- LLMBackend    : LLM 后端抽象基类
- OpenAIBackend  : OpenAI API 后端
- QwenBackend   : Qwen API 后端
- ToolRegistry   : 工具注册表
- Action         : 行动指令
- StepRecord     : 单步记录
- AgentResult    : Agent 运行结果
"""
from .react import (
    ReActAgent,
    LLMBackend,
    OpenAIBackend,
    QwenBackend,
    ToolRegistry,
    Action,
    StepRecord,
    AgentResult,
)

__all__ = [
    "ReActAgent",
    "LLMBackend",
    "OpenAIBackend",
    "QwenBackend",
    "ToolRegistry",
    "Action",
    "StepRecord",
    "AgentResult",
]
