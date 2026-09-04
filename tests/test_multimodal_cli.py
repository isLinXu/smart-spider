# coding=utf-8
"""多模态 CLI 参数契约测试。"""
from smart_spider.multimodal_cli import _build_parser


def test_multimodal_cli_accepts_query_source_and_options():
    args = _build_parser().parse_args([
        "--queries", "cat,dog",
        "--output", "./out",
        "--modalities", "text,image",
        "--label-mode", "hybrid",
        "--no-materialize-images",
    ])
    assert args.queries == "cat,dog"
    assert args.modalities == "text,image"
    assert args.no_materialize_images is True


def test_multimodal_cli_exposes_split_timeout_and_image_limits():
    args = _build_parser().parse_args([
        "--urls", "https://example.com/a",
        "--connect-timeout", "3",
        "--read-timeout", "12",
        "--max-image-bytes", "1024",
        "--max-image-pixels", "100000",
    ])
    assert args.connect_timeout == 3
    assert args.read_timeout == 12
    assert args.max_image_bytes == 1024
    assert args.max_image_pixels == 100000
