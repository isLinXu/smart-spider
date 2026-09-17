# coding=utf-8
"""单张图片下载尝试：URL/内容去重、窗口槽位、候选状态与配额回滚。"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from .dataset_contracts import CandidateResource


class ImageDownloadSession:
    """Owns claim/slot/candidate lifecycle for one ``_download_and_save`` call.

    ``close()`` is idempotent and always safe: uncommitted URL/content claims,
    quota holds, HTTP responses, and window slots are released.
    """

    def __init__(
        self,
        *,
        url: str,
        keyword: str,
        source: str,
        job_id: str,
        dedup: Any,
        window: Any,
        state_store: Any = None,
    ) -> None:
        self.url = url
        self.keyword = keyword
        self.source = source
        self.job_id = job_id
        self._dedup = dedup
        self._window = window
        self._state_store = state_store
        self.candidate_id: Optional[str] = None
        self.url_claimed = False
        self.content_claimed = False
        self.content: Optional[bytes] = None
        self.candidate_slot = False
        self.domain_slot: Any = None
        self.response: Any = None
        self.quota_hold: Any = None
        self._closed = False

    def claim_url(self) -> bool:
        self.url_claimed = bool(self._dedup.claim(self.url))
        return self.url_claimed

    def register_candidate(self) -> None:
        if self._state_store is None:
            return
        try:
            self.candidate_id = self._state_store.add_candidate(
                self.job_id,
                CandidateResource(
                    url=self.url,
                    source=self.source,
                    query=self.keyword,
                    source_meta={"keyword": self.keyword},
                ),
            )
        except Exception as state_err:
            logger.warning(f"State store candidate error: {state_err}")

    def acquire_slots(self) -> Optional[str]:
        """Acquire candidate then download slots. Returns a fail reason or None."""
        if not self._window.acquire_candidate_slot():
            return "crawl_stopped_before_candidate_processing"
        self.candidate_slot = True
        slot = self._window.acquire_download_slot(self.url)
        if slot is None:
            return "crawl_stopped_before_download"
        self.domain_slot = slot
        return None

    def attach_response(self, response: Any) -> None:
        self.response = response

    def claim_content(self, content: bytes) -> bool:
        if not self._dedup.claim_content(content):
            return False
        self.content = content
        self.content_claimed = True
        return True

    def reject(self, reason: str) -> bool:
        if self.candidate_id and self._state_store is not None:
            try:
                self._state_store.reject_candidate(self.candidate_id, reason)
            except Exception as state_err:
                logger.warning(f"State store reject error: {state_err}")
        return False

    def fail(self, error: str) -> bool:
        if self.candidate_id and self._state_store is not None:
            try:
                self._state_store.fail_candidate(self.candidate_id, error)
            except Exception as state_err:
                logger.warning(f"State store failure error: {state_err}")
        return False

    def commit_success(self) -> None:
        if self.content_claimed and self.content is not None:
            self._dedup.commit_content(self.content)
            self.content_claimed = False
        if self.url_claimed:
            self._dedup.commit(self.url)
            self.url_claimed = False
        if self.quota_hold is not None:
            self.quota_hold.commit()
            self.quota_hold = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.response is not None:
            try:
                self.response.close()
            except Exception:
                pass
            self.response = None
        if self.content_claimed and self.content is not None:
            self._dedup.release_content(self.content)
            self.content_claimed = False
        if self.quota_hold is not None:
            self.quota_hold.release()
            self.quota_hold = None
        if self.url_claimed:
            self._dedup.release(self.url)
            self.url_claimed = False
        if self.domain_slot is not None:
            self._window.release_download_slot(self.domain_slot)
            self.domain_slot = None
        if self.candidate_slot:
            self._window.release_candidate_slot()
            self.candidate_slot = False
