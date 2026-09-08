# coding=utf-8
"""多模态 CLI 参数契约测试。"""
import pytest

from smart_spider.multimodal_cli import _build_parser
from smart_spider.multimodal_cli import main as multimodal_cli_main


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


def test_multimodal_cli_exposes_scene_gate_and_private_host_controls():
    args = _build_parser().parse_args([
        "--urls", "https://example.com/a",
        "--scene", "smoking",
        "--scene-quality-gate",
        "--allow-private-hosts",
    ])
    assert args.scene == "smoking"
    assert args.scene_quality_gate is True
    assert args.allow_private_hosts is True


def test_multimodal_cli_requires_scene_for_quality_gate():
    with pytest.raises(SystemExit):
        multimodal_cli_main([
            "--urls", "https://example.com/a",
            "--scene-quality-gate",
        ])


def test_multimodal_cli_exposes_playbook_and_session_flags():
    args = _build_parser().parse_args([
        "--urls", "https://example.com/a",
        "--browser-playbook",
        "--allow-hosts", "example.com",
        "--storage-state", "./state.json",
        "--session-cookies", "./cookies.json",
        "--export-storage-state", "./out-state.json",
        "--ignore-robots",
        "--license", "CC-BY-4.0",
        "--source-terms", "public pages only",
    ])
    assert args.browser_playbook is True
    assert args.allow_hosts == "example.com"
    assert args.storage_state == "./state.json"
    assert args.export_storage_state == "./out-state.json"
    assert args.ignore_robots is True
    assert args.license == "CC-BY-4.0"
    assert args.source_terms == "public pages only"
