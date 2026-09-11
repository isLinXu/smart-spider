# coding=utf-8
"""浏览器会话状态：Cookie / Playwright storage_state 读写。

用于授权站点登录态复用；不涉及指纹伪造或验证码绕过。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Union


StorageState = dict[str, Any]
PathLike = Union[str, Path]


def load_json_file(path: PathLike) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_file(path: PathLike, payload: Any) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return str(target)


def load_cookies(path: PathLike) -> list[dict[str, Any]]:
    """加载 Cookie 列表；兼容 ``[{...}]`` 与 ``{"cookies": [...]}``。"""
    payload = load_json_file(path)
    if isinstance(payload, dict) and "cookies" in payload:
        payload = payload["cookies"]
    if not isinstance(payload, list):
        raise ValueError("cookies file must be a list or {cookies: [...]}")
    return [dict(item) for item in payload]


def load_storage_state(path: PathLike) -> StorageState:
    """加载 Playwright ``storage_state`` JSON。"""
    payload = load_json_file(path)
    if not isinstance(payload, dict):
        raise ValueError("storage_state file must be a JSON object")
    # Minimal shape check; Playwright accepts cookies/origins keys.
    if "cookies" not in payload and "origins" not in payload:
        raise ValueError("storage_state must contain cookies and/or origins")
    return dict(payload)


def cookies_from_storage_state(state: StorageState) -> list[dict[str, Any]]:
    cookies = state.get("cookies") or []
    if not isinstance(cookies, list):
        raise ValueError("storage_state.cookies must be a list")
    return [dict(item) for item in cookies]


def resolve_session_inputs(
    *,
    storage_state_path: str = "",
    cookies_path: str = "",
) -> tuple[Optional[StorageState], list[dict[str, Any]]]:
    """优先 storage_state；否则仅 cookies。两者都提供时合并 cookies（state 优先）。"""
    state: Optional[StorageState] = None
    cookies: list[dict[str, Any]] = []
    if storage_state_path:
        state = load_storage_state(storage_state_path)
        cookies = cookies_from_storage_state(state)
    if cookies_path:
        extra = load_cookies(cookies_path)
        if state is None:
            cookies = extra
        else:
            # Append extras not already present by name+domain.
            seen = {
                (str(item.get("name")), str(item.get("domain") or ""))
                for item in cookies
            }
            for item in extra:
                key = (str(item.get("name")), str(item.get("domain") or ""))
                if key not in seen:
                    cookies.append(item)
                    seen.add(key)
    return state, cookies
