# coding=utf-8
"""OCR 模块。

使用 PaddleOCR 或 EasyOCR 进行文字识别。
"""
try:
    from paddleocr import PaddleOCR
    _PADDLEOCR_AVAILABLE = True
except ImportError:
    _PADDLEOCR_AVAILABLE = False
    PaddleOCR = None

try:
    from easyocr import Reader
    _EASYOCR_AVAILABLE = True
except ImportError:
    _EASYOCR_AVAILABLE = False
    Reader = None

from loguru import logger


class OCRModule:
    """OCR 封装。"""

    def __init__(self, use_paddleocr: bool = True, use_easyocr: bool = False, lang: str = "ch"):
        self.use_paddleocr = use_paddleocr
        self.use_easyocr = use_easyocr
        self.lang = lang
        self._ocr = None

        if use_paddleocr and _PADDLEOCR_AVAILABLE:
            self._ocr = PaddleOCR(use_angle_cls=True, lang=lang, show_log=False)
        elif use_easyocr and _EASYOCR_AVAILABLE:
            self._ocr = Reader([lang], gpu=False)
        else:
            self._ocr = None

    def detect_text(self, image: "Image.Image") -> list[dict]:
        """检测图片中的文字区域。"""
        if self._ocr is None:
            logger.warning("OCR engine not initialized")
            return []

        result = self._ocr.ocr(image, cls=True)
        return result if result else []

    def recognize_text(self, image: "Image.Image") -> list[dict]:
        """识别图片中的文字。"""
        if self._ocr is None:
            return []

        result = self._ocr.ocr(image, cls=True)
        texts = []
        for line in result[0] if result and result[0] else []:
            texts.append({
                "text": line[1][0],
                "confidence": line[1][1],
                "bbox": line[0],
            })
        return texts
