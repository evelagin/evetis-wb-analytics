"""Wildberries Feedbacks API client.

Endpoints used (MVP = reviews only):
  GET  {base}/api/v1/feedbacks?isAnswered=false&take=&skip=&order=dateDesc
  {method} {base}{answer_path}   body {"id": <feedback_id>, "text": <answer>}

Defaults for the answer call follow the current WB OpenAPI spec: a FIRST answer
to a feedback is `POST /api/v1/feedbacks/answer` (editing a published answer is
PATCH on the same path). Our flow only posts a first answer. The method/path/body
stay configurable in Settings because the exact contract MUST still be confirmed
against the live official Swagger before the first real publish. Reviews /
questions / buyer-chats are three DIFFERENT WB APIs and are never merged.

Auth: WB expects the raw API token in the `Authorization` header (no "Bearer").

Retry policy: transient only — 429 (WBRateLimitError) and 5xx (WBServerError)
and transport errors are retried; 401/403 (WBAuthError) and other 4xx are NOT.
"""
from __future__ import annotations

import threading
import time

import httpx

from app.config import Settings
from app.domain.exceptions import WBApiError, WBAuthError, WBRateLimitError, WBServerError
from app.utils.logging import get_logger
from app.utils.retry import retry_call

logger = get_logger(__name__)

_RETRY_ON = (WBServerError, httpx.TransportError)  # WBRateLimitError ⊂ WBServerError


class WBClient:
    def __init__(self, settings: Settings, token: str, client: httpx.Client | None = None):
        self._s = settings
        self._token = token
        self._client = client or httpx.Client(base_url=settings.wb_api_base_url, timeout=30.0)
        self._lock = threading.Lock()
        self._last_call = 0.0

    def _throttle(self) -> None:
        with self._lock:
            wait = self._s.wb_min_interval_seconds - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    @property
    def _headers(self) -> dict:
        return {"Authorization": self._token, "Content-Type": "application/json"}

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        self._throttle()
        resp = self._client.request(method, url, headers=self._headers, **kwargs)
        if resp.status_code == 429:
            err = WBRateLimitError("WB rate limit (429)", status_code=429)
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    err.retry_after = float(retry_after)
                except ValueError:
                    pass
            raise err
        if resp.status_code in (401, 403):
            # non-retriable auth failure; never echo the token
            raise WBAuthError("WB auth failed", status_code=resp.status_code)
        if resp.status_code >= 500:
            raise WBServerError("WB server error", status_code=resp.status_code)
        return resp

    def get_unanswered_feedbacks(self, take: int | None = None, skip: int = 0) -> list[dict]:
        take = take or self._s.wb_poll_batch_size
        params = {"isAnswered": "false", "take": take, "skip": skip, "order": "dateDesc"}

        def _do() -> httpx.Response:
            return self._request("GET", self._s.wb_feedbacks_path, params=params)

        resp = retry_call(_do, retries=3, retry_on=_RETRY_ON)
        if resp.status_code >= 400:
            raise WBApiError("WB feedbacks fetch failed", status_code=resp.status_code)
        data = resp.json() or {}
        feedbacks = ((data.get("data") or {}).get("feedbacks")) or []
        return feedbacks

    def iter_unanswered_feedbacks(self) -> list[dict]:
        """Paginate through unanswered feedbacks up to max_pages / max_items.

        WB returns `dateDesc`-ordered pages; we walk `skip` until a short page or
        a configured cap. If the cap truncates results, that is logged (never a
        silent cut).
        """
        take = self._s.wb_poll_batch_size
        collected: list[dict] = []
        pages = 0
        skip = 0
        while pages < self._s.wb_max_pages and len(collected) < self._s.wb_max_items:
            batch = self.get_unanswered_feedbacks(take=take, skip=skip)
            pages += 1
            if not batch:
                break
            collected.extend(batch)
            if len(batch) < take:
                break
            skip += take
        if len(collected) > self._s.wb_max_items:
            logger.warning(
                "WB pagination capped: collected=%d capped_to=%d (increase WB_MAX_ITEMS)",
                len(collected), self._s.wb_max_items,
            )
            collected = collected[: self._s.wb_max_items]
        if pages >= self._s.wb_max_pages and len(collected) and len(collected) % take == 0:
            logger.warning("WB pagination hit WB_MAX_PAGES=%d; more may remain", self._s.wb_max_pages)
        logger.info("fetched %d unanswered feedbacks across %d page(s)", len(collected), pages)
        return collected

    def publish_answer(self, feedback_id: str, text: str) -> dict:
        body = {"id": feedback_id, "text": text}

        def _do() -> httpx.Response:
            return self._request(self._s.wb_answer_method, self._s.wb_answer_path, json=body)

        resp = retry_call(_do, retries=2, retry_on=_RETRY_ON)
        if resp.status_code >= 400:
            raise WBApiError(
                "WB publish rejected", status_code=resp.status_code, body=(resp.text or "")[:300]
            )
        try:
            return resp.json() if resp.content else {"status_code": resp.status_code}
        except ValueError:
            return {"status_code": resp.status_code}

    # --- questions (separate WB entity/endpoint) ---
    def get_unanswered_questions(self, take: int | None = None, skip: int = 0) -> list[dict]:
        take = take or self._s.wb_poll_batch_size
        params = {"isAnswered": "false", "take": take, "skip": skip, "order": "dateDesc"}

        def _do() -> httpx.Response:
            return self._request("GET", self._s.wb_questions_path, params=params)

        resp = retry_call(_do, retries=3, retry_on=_RETRY_ON)
        if resp.status_code >= 400:
            raise WBApiError("WB questions fetch failed", status_code=resp.status_code)
        data = resp.json() or {}
        questions = ((data.get("data") or {}).get("questions")) or []
        return questions

    def iter_unanswered_questions(self) -> list[dict]:
        """Paginate unanswered questions (dateDesc) up to max_pages / max_items —
        same bounded pagination as feedbacks."""
        take = self._s.wb_poll_batch_size
        collected: list[dict] = []
        pages = 0
        skip = 0
        while pages < self._s.wb_max_pages and len(collected) < self._s.wb_max_items:
            batch = self.get_unanswered_questions(take=take, skip=skip)
            pages += 1
            if not batch:
                break
            collected.extend(batch)
            if len(batch) < take:
                break
            skip += take
        if len(collected) > self._s.wb_max_items:
            logger.warning("WB questions pagination capped to %d", self._s.wb_max_items)
            collected = collected[: self._s.wb_max_items]
        logger.info("fetched %d unanswered questions across %d page(s)", len(collected), pages)
        return collected

    def publish_question_answer(self, question_id: str, text: str, state: str | None = None) -> dict:
        # WB spec: PATCH /api/v1/questions  body {"id","text","state"} where
        # state="wbRu" publishes the answer publicly.
        body = {"id": question_id, "text": text, "state": state or self._s.wb_question_answer_state}

        def _do() -> httpx.Response:
            return self._request(
                self._s.wb_question_answer_method, self._s.wb_questions_path, json=body
            )

        resp = retry_call(_do, retries=2, retry_on=_RETRY_ON)
        if resp.status_code >= 400:
            raise WBApiError(
                "WB question publish rejected", status_code=resp.status_code,
                body=(resp.text or "")[:300],
            )
        try:
            return resp.json() if resp.content else {"status_code": resp.status_code}
        except ValueError:
            return {"status_code": resp.status_code}

    def close(self) -> None:
        self._client.close()
