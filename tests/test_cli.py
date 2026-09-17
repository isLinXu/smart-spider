# coding=utf-8
"""Unified smart-spider CLI dispatch tests."""
import pytest

from smart_spider.cli import main as cli_main


def test_cli_help_lists_subcommands(capsys):
    assert cli_main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "dataset" in out
    assert "browse" in out
    assert "crawl" in out


def test_cli_dataset_help():
    with pytest.raises(SystemExit) as exc:
        cli_main(["dataset", "--help"])
    assert exc.value.code == 0


def test_cli_crawl_help():
    with pytest.raises(SystemExit) as exc:
        cli_main(["crawl", "--help"])
    assert exc.value.code == 0


def test_cli_legacy_keywords_still_parse():
    with pytest.raises(SystemExit) as exc:
        cli_main(["--keywords"])
    assert exc.value.code != 0
