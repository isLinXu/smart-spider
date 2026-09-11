# coding=utf-8
"""拦截信号分类测试。"""
from __future__ import annotations

from smart_spider.block_signals import (
    BlockKind,
    apply_block_to_queue,
    classify_http_status,
    classify_page_html,
    decide_queue_action,
)
from smart_spider.pipeline import LocalSqliteTaskQueue


def test_classify_http_status_families():
    assert classify_http_status(200).kind == BlockKind.OK
    assert classify_http_status(429).retryable is True
    assert decide_queue_action(classify_http_status(429)) == "retry"
    assert classify_http_status(403).kind == BlockKind.FORBIDDEN
    assert decide_queue_action(classify_http_status(403)) == "dead"
    assert classify_http_status(503).retryable is True
    assert classify_http_status(401).kind == BlockKind.AUTH_REQUIRED


def test_classify_challenge_page_even_on_200():
    html = "<html><body>Please verify you are human</body></html>"
    signal = classify_page_html(html, status_code=200)
    assert signal.kind == BlockKind.CHALLENGE
    assert decide_queue_action(signal) == "dead"


def test_apply_block_terminal_dead(tmp_path):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"), default_max_attempts=5)
    record = queue.enqueue("authorized_browse", {"url": "https://example.com"})
    claimed = queue.claim()
    assert claimed is not None
    signal = classify_page_html("安全验证 captcha", status_code=200)
    done = apply_block_to_queue(queue, record.task_id, signal)
    assert done.status == "dead"
    assert done.error.startswith("block:challenge:")
