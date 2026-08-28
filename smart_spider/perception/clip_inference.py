# coding=utf-8
"""CLIP 推理模块。

使用 torch + clip 进行图片相似度过滤。
"""
try:
    import torch
    import clip as _clip
    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False
    _clip = None

try:
    from PIL import Image
except ImportError:
    Image = None

from loguru import logger


class CLIPInference:
    """CLIP 推理封装。"""

    def __init__(self, model_name: str = "ViT-B/32", device: str = "cpu"):
        if not _TORCH_AVAILABLE:
            raise RuntimeError("torch is not installed")
        if _clip is None:
            raise RuntimeError("openai-clip is not installed")

        self.device = device
        self.model_name = model_name

        # 加载模型和预处理
        self.model, self.preprocess = _clip.load(model_name, device=device)
        self.model.eval()

    def encode_text(self, text: str) -> "torch.Tensor":
        """编码文本为归一化特征向量。"""
        import torch as _torch
        tokens = _clip.tokenize([text]).to(self.device)
        with _torch.no_grad():
            text_features = self.model.encode_text(tokens)
        # 归一化
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        return text_features

    def encode_image(self, image: "PIL.Image.Image") -> "torch.Tensor":
        """编码图片为归一化特征向量。"""
        import torch as _torch
        with _torch.no_grad():
            image_features = self.model.encode_image(self.preprocess(image).unsqueeze(0).to(self.device))
        # 归一化
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        return image_features

    def encode_images(self, images: list["PIL.Image.Image"]) -> "torch.Tensor":
        """批量编码图片为归一化特征向量。"""
        import torch as _torch
        if not images:
            raise ValueError("images must not be empty")
        tensors = _torch.stack([self.preprocess(image) for image in images]).to(self.device)
        with _torch.no_grad():
            image_features = self.model.encode_image(tensors)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        return image_features

    def compute_similarity(self, image_features: "torch.Tensor", text_features: "torch.Tensor") -> float:
        """计算余弦相似度。"""
        import torch as _torch
        with _torch.no_grad():
            similarity = _torch.nn.functional.cosine_similarity(image_features, text_features).item()
        return similarity
