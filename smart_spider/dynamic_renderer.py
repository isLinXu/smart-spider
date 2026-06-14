# coding=utf-8
"""DynamicRenderer 桥接模块。

从 browser.py 导出 DynamicRenderer 类，保持向后兼容。
"""
from .browser import DynamicRenderer

__all__ = ["DynamicRenderer"]
