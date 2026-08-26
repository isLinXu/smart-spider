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
