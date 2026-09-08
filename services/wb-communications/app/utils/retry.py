"""Small retry helper with exponential backoff.

Retry can be gated two ways (combined with AND):
  * ``retry_on`` — exception types eligible for retry;
  * ``should_retry`` — optional predicate for finer classification
    (e.g. retry an OpenAI error only when it is transient).

``sleep`` is resolved at call time so tests can monkeypatch
``app.utils.retry.time.sleep`` to make backoff instant.
"""
from __future__ import annotations

import time
from typing import Callable, Iterable, Optional, TypeVar

T = TypeVar("T")


def retry_call(
    func: Callable[[], T],
    *,
    retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_on: Iterable[type[BaseException]] = (Exception,),
    should_retry: Optional[Callable[[BaseException], bool]] = None,
    sleep: Callable[[float], None] | None = None,
) -> T:
    if sleep is None:
        sleep = time.sleep
    retry_on = tuple(retry_on)
    attempt = 0
    while True:
        try:
            return func()
        except retry_on as exc:  # noqa: PERF203
            if should_retry is not None and not should_retry(exc):
                raise
            attempt += 1
            if attempt > retries:
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            override = getattr(exc, "retry_after", None)
            if override:
                delay = min(max_delay, float(override))
            sleep(delay)
