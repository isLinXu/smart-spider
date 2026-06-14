# coding=utf-8
"""多模态感知层。

集成 CLIP 推理和 OCR，为 BrowserController 提供环境感知能力。

核心类
------
- PagePerception : 页面感知器，从 HTML 中提取多模态信息
- PageState      : 页面状态快照，供 ReAct 决策核心使用
- CLIPInference  : CLIP 推理封装
- OCRModule      : OCR 封装
"""
from .clip_inference import CLIPInference
from .ocr import OCRModule
from .page_perception import PagePerception, PageState

__all__ = ["CLIPInference", "OCRModule", "PagePerception", "PageState"]
