# coding=utf-8
"""本地图片以图搜图索引测试。"""

import numpy as np
import pytest
from PIL import Image

from smart_spider.image_retrieval import ImageSimilarityIndex, iter_image_files
from smart_spider.image_search_cli import _build_parser


class MeanColorEncoder:
    """用平均 RGB 作为可预测的测试向量，避免加载真实 CLIP 模型。"""

    def encode_images(self, images):
        return np.asarray([
            np.asarray(image.resize((1, 1)), dtype=np.float32)[0, 0]
            for image in images
        ])


def _write_image(path, color):
    Image.new("RGB", (12, 8), color).save(path)


def test_build_and_search_returns_visual_similarity_order(tmp_path):
    red = tmp_path / "red.jpg"
    yellow = tmp_path / "yellow.jpg"
    blue = tmp_path / "blue.jpg"
    broken = tmp_path / "broken.jpg"
    _write_image(red, (255, 0, 0))
    _write_image(yellow, (255, 255, 0))
    _write_image(blue, (0, 0, 255))
    broken.write_text("not an image", encoding="utf-8")

    index = ImageSimilarityIndex(MeanColorEncoder(), batch_size=2)
    report = index.build_from_directory(tmp_path)

    assert report.indexed == 3
    assert report.skipped == 1
    assert len(iter_image_files(tmp_path)) == 4

    results = index.search(red, top_k=3)
    assert [result.path for result in results] == [str(yellow.resolve()), str(blue.resolve())]
    assert results[0].score > results[1].score
    assert results[0].metadata["width"] == 12


def test_save_and_load_preserves_results(tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    query = tmp_path / "query.png"
    _write_image(first, (255, 0, 0))
    _write_image(second, (0, 255, 0))
    _write_image(query, (250, 10, 0))

    index = ImageSimilarityIndex(MeanColorEncoder())
    index.build([first, second])
    index_path = tmp_path / "nested" / "images.npz"
    assert index.save(index_path) == str(index_path.resolve())

    loaded = ImageSimilarityIndex.load(index_path, encoder=MeanColorEncoder())
    results = loaded.search(query, top_k=1)
    assert results[0].path == str(first.resolve())
    assert results[0].score > 0.99


def test_empty_index_round_trips(tmp_path):
    index_path = tmp_path / "empty.npz"
    index = ImageSimilarityIndex(MeanColorEncoder())
    index.save(index_path)

    loaded = ImageSimilarityIndex.load(index_path, encoder=MeanColorEncoder())
    assert loaded.search(tmp_path / "missing.jpg") == []


def test_search_validates_arguments(tmp_path):
    image = tmp_path / "image.jpg"
    _write_image(image, (10, 20, 30))
    index = ImageSimilarityIndex(MeanColorEncoder())
    index.build([image])

    with pytest.raises(ValueError, match="top_k"):
        index.search(image, top_k=0)
    with pytest.raises(ValueError, match="threshold"):
        index.search(image, threshold=2)


def test_image_search_cli_parser():
    args = _build_parser().parse_args([
        "--query-image", "query.jpg",
        "--index", "images.npz",
        "--top-k", "5",
        "--threshold", "0.7",
        "--include-query",
    ])
    assert args.query_image == "query.jpg"
    assert args.top_k == 5
    assert args.threshold == 0.7
    assert args.include_query is True
