#!/usr/bin/env python3
"""Stage 2 backfill — общий слой: секреты, HTTP, throttling, retry, журнал ошибок.

ТОЛЬКО READ. Ни один mutation endpoint здесь не описан и не вызывается.
Секреты живут в памяти процесса: не печатаются, не логируются, не пишутся в файлы.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
ROOT = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.abspath(os.path.join(ROOT, "..", ".."))
ERRORS = os.path.join(AUDIT, "data", "backfill_errors.csv")

SELLER = "https://api-seller.ozon.ru"
PERF = "https://api-performance.ozon.ru"

PERIOD_START = date(2026, 6, 1)
PERIOD_END = date(2026, 8, 31)

_BACKOFF = [3, 6, 12, 24, 48]          # Stage 1.7: 5 попыток
_cache = {}


def secret(name):
    if name not in _cache:
        _cache[name] = subprocess.run(
            ["gcloud", "secrets", "versions", "access", "latest", f"--secret={name}"],
            capture_output=True, text=True, check=True).stdout.strip()
    return _cache[name]


def now_msk():
    return datetime.now(MSK).isoformat()


def days(start=PERIOD_START, end=PERIOD_END):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def log_error(dataset, endpoint, chunk, http_status, attempts, required, message):
    new = not os.path.exists(ERRORS)
    os.makedirs(os.path.dirname(ERRORS), exist_ok=True)
    with open(ERRORS, "a", encoding="utf-8") as f:
        if new:
            f.write("timestamp_msk,dataset,endpoint,chunk,http_status,attempts,"
                    "required,message\n")
        msg = str(message).replace('"', "'").replace("\n", " ")[:300]
        f.write(f'{now_msk()},{dataset},{endpoint},{chunk},{http_status},'
                f'{attempts},{required},"{msg}"\n')


def _request(req, dataset, endpoint, chunk, required, raw_text=False):
    """Один запрос с экспоненциальным backoff на 429 и 5xx."""
    for attempt in range(len(_BACKOFF) + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read()
                return r.status, (body.decode("utf-8") if raw_text else json.loads(body))
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")
            retryable = e.code == 429 or 500 <= e.code < 600
            if retryable and attempt < len(_BACKOFF):
                wait = _BACKOFF[attempt]
                print(f"    HTTP {e.code}, пауза {wait}s ({chunk}, попытка {attempt + 1})",
                      flush=True)
                time.sleep(wait)
                continue
            log_error(dataset, endpoint, chunk, e.code, attempt + 1, required, payload)
            return e.code, {"_error": payload}
        except Exception as e:                                   # noqa: BLE001
            if attempt < len(_BACKOFF):
                time.sleep(_BACKOFF[attempt])
                continue
            log_error(dataset, endpoint, chunk, "EXC", attempt + 1, required, repr(e))
            return "EXC", {"_error": repr(e)}


def seller_post(path, body, dataset, chunk, required=True):
    req = urllib.request.Request(
        SELLER + path, data=json.dumps(body).encode(),
        headers={"Client-Id": secret("EVETIS_OZON_CLIENT_ID"),
                 "Api-Key": secret("EVETIS_OZON_API_KEY"),
                 "Content-Type": "application/json"})
    return _request(req, dataset, path, chunk, required)


class PerfToken:
    """Токен Performance API живёт 1800 с. Обновляем каждые 25 минут."""

    def __init__(self):
        self._tok = None
        self._at = 0

    def get(self):
        if self._tok and time.time() - self._at < 1500:
            return self._tok
        body = json.dumps({"client_id": secret("EVETIS_OZON_PERFORMANCE_CLIENT_ID"),
                           "client_secret": secret("EVETIS_OZON_PERFORMANCE_CLIENT_SECRET"),
                           "grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(
            PERF + "/api/client/token", data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        code, d = _request(req, "perf_token", "/api/client/token", "-", True)
        if code != 200:
            raise RuntimeError("не удалось получить токен Performance API")
        self._tok = d["access_token"]
        self._at = time.time()
        return self._tok


def perf_get(path, token, dataset, chunk, required=True, raw_text=True):
    req = urllib.request.Request(
        PERF + path, headers={"Authorization": f"Bearer {token}",
                              "Accept": "application/json"})
    return _request(req, dataset, path.split("?")[0], chunk, required, raw_text=raw_text)


def save(subdir, name, payload, is_text=False):
    d = os.path.join(ROOT, subdir)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(payload if is_text else json.dumps(payload, ensure_ascii=False))
    return p
