# coding=utf-8
"""CLIP / ML 冒烟：缺依赖时自动 skip，有依赖时做最小端到端编码。"""
from __future__ import annotations

import io

import pytest
from PIL import Image

torch = pytest.importorskip("torch")
clip = pytest.importorskip("clip")


@pytest.mark.smoke
def test_clip_encode_image_and_text_smoke():
    device = "cpu"
    model, preprocess = clip.load("ViT-B/32", device=device)
    model.eval()

    image = Image.new("RGB", (64, 64), color=(20, 120, 200))
    image_input = preprocess(image).unsqueeze(0).to(device)
    text_input = clip.tokenize(["a blue square", "a red car"]).to(device)

    with torch.no_grad():
        image_features = model.encode_image(image_input)
        text_features = model.encode_text(text_input)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        sims = (image_features @ text_features.T).squeeze(0).tolist()

    assert len(sims) == 2
    assert sims[0] > sims[1]


@pytest.mark.smoke
def test_dataset_crawler_clip_optional_path(tmp_path):
    """Ensure DatasetCrawler can construct with CLIP enabled when available."""
    from smart_spider.dataset_config import DatasetCrawlConfig
    from smart_spider.dataset_crawler import DatasetCrawler

    config = DatasetCrawlConfig(
        keywords=["cat"],
        total_count=1,
        output_dir=str(tmp_path / "clip_out"),
        use_clip=True,
        clip_model="ViT-B/32",
        state_db="",
    )
    crawler = DatasetCrawler.from_config(config)
    assert crawler.use_clip is True
    assert crawler.model is not None
