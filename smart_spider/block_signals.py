# coding=utf-8
"""拦截/挑战信号分类（授权爬取可观测，非对抗绕过）。

将 HTTP 状态与页面文案映射为可重试或应死信的类别，供 worker/API 使用。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class BlockKind(str, Enum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"
    AUTH_REQUIRED = "auth_required"
    CHALLENGE = "challenge"  # CAPTCHA / human check — operator intervention
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    SERVER_ERROR = "server_error"
    EMPTY = "empty"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BlockSignal:
    kind: BlockKind
    retryable: bool
    reason: str
    status_code: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        return payload

    @property
    def queue_error(self) -> str:
        """写入 TaskQueue.complete(error=...) 的稳定短标签。"""
        return f"block:{self.kind.value}:{self.reason}"


_CHALLENGE_MARKERS = (
    "captcha",
    "verify you are human",
    "unusual traffic",
    "cf-challenge",
    "attention required",
    "请完成安全验证",
    "安全验证",
    "人机验证",
    "滑动验证",
)

_AUTH_MARKERS = (
    "sign in",
    "log in",
    "login required",
    "unauthorized",
    "请登录",
    "登录后",
)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def classify_http_status(status_code: int, *, body_text: str = "") -> BlockSignal:
    code = int(status_code or 0)
    text = _normalize_text(body_text)
    if code == 0 and not text:
        return BlockSignal(BlockKind.EMPTY, False, "empty_response", 0)
    if code in {0, 200, 201, 204} or 200 <= code < 300:
        if any(marker in text for marker in _CHALLENGE_MARKERS):
            return BlockSignal(
                BlockKind.CHALLENGE, False, "challenge_page", code or 200
            )
        return BlockSignal(BlockKind.OK, False, "ok", code or 200)
    if code == 429:
        return BlockSignal(BlockKind.RATE_LIMITED, True, "http_429", code)
    if code in {401, 407}:
        return BlockSignal(BlockKind.AUTH_REQUIRED, False, f"http_{code}", code)
    if code == 403:
        if any(marker in text for marker in _CHALLENGE_MARKERS):
            return BlockSignal(BlockKind.CHALLENGE, False, "http_403_challenge", code)
        return BlockSignal(BlockKind.FORBIDDEN, False, "http_403", code)
    if code == 404:
        return BlockSignal(BlockKind.NOT_FOUND, False, "http_404", code)
    if 500 <= code <= 599 or code == 408:
        return BlockSignal(BlockKind.SERVER_ERROR, True, f"http_{code}", code)
    return BlockSignal(BlockKind.UNKNOWN, False, f"http_{code}", code)


def classify_page_html(html: str, *, status_code: int = 200) -> BlockSignal:
    text = _normalize_text(html)
    if not text:
        return BlockSignal(BlockKind.EMPTY, False, "empty_html", status_code)
    if any(marker in text for marker in _CHALLENGE_MARKERS):
        return BlockSignal(BlockKind.CHALLENGE, False, "challenge_markers", status_code)
    if status_code in {401, 403} or any(marker in text for marker in _AUTH_MARKERS):
        if status_code in {401, 407} or "unauthorized" in text or "请登录" in text:
            return BlockSignal(BlockKind.AUTH_REQUIRED, False, "auth_markers", status_code)
    return classify_http_status(status_code, body_text=html)


def decide_queue_action(signal: BlockSignal) -> str:
    """返回 succeed | retry | dead。"""
    if signal.kind == BlockKind.OK:
        return "succeed"
    if signal.retryable:
        return "retry"
    return "dead"


def apply_block_to_queue(queue, task_id: str, signal: BlockSignal):
    """按分类写回队列；返回 complete 后的 TaskRecord。"""
    action = decide_queue_action(signal)
    if action == "succeed":
        return queue.complete(task_id)
    return queue.complete(
        task_id,
        error=signal.queue_error,
        terminal=(action == "dead"),
    )
