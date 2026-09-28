"""Доступ владельца к таблицам арендатора (Tenancy T5): BigQuery REST, только Tables API.

Инструменты владельца (tenant_binding.py, tenant_lifecycle.py) читают журналы tabledata.list и
дописывают insertAll — те же примитивы, что у control, но под учётными данными владельца
(gcloud). Запросов (jobs) нет: журналы остаются append-only и здесь.

Значения identity из этих таблиц в журналы и файлы не пишутся: инструменты печатают их только
в терминал владельца и только по явному флагу (--reveal).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "pipelines" / "ozon" / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))          # чистые модули runtime: identity, lifecycle_core, checkpoints

BQ = "https://www.googleapis.com/bigquery/v2"


class TableError(RuntimeError):
    pass


def _token() -> str:
    return subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True,
                          check=True).stdout.strip()


def owner_account() -> str:
    return subprocess.run(["gcloud", "config", "get-value", "account"], capture_output=True, text=True,
                          check=True).stdout.strip()


def _req(method, url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return _parse(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise TableError(f"{method} {url.split('?')[0].rsplit('/', 3)[-3:]}: HTTP {e.code}") from None


def _parse(text):
    from tools.tenancy.validation import parse_tenant_json   # единый строгий разборщик JSON
    return parse_tenant_json(text.decode("utf-8") if isinstance(text, bytes) else text)


def _cast(field, v):
    if v is None:
        return None
    t = field["type"]
    if t in ("INTEGER", "INT64"):
        return int(v)
    if t in ("FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"):
        return float(v)
    if t in ("BOOLEAN", "BOOL"):
        return v == "true"
    return v                                   # STRING, DATE, TIMESTAMP (секунды эпохи строкой)


class Tables:
    def __init__(self, project: str):
        self.project = project

    def rows(self, dataset: str, table: str):
        base = f"{BQ}/projects/{self.project}/datasets/{dataset}/tables/{table}"
        schema = _req("GET", base)["schema"]["fields"]
        token = ""
        while True:
            page = _req("GET", f"{base}/data?maxResults=10000" + (f"&pageToken={urllib.parse.quote(token)}" if token else ""))
            for r in page.get("rows") or []:
                yield {f["name"]: _cast(f, c.get("v")) for f, c in zip(schema, r["f"])}
            token = page.get("pageToken") or ""
            if not token:
                return

    def append(self, dataset: str, table: str, rows: list[dict]) -> None:
        body = {"rows": [{"insertId": hashlib.sha256(json.dumps(r, sort_keys=True, default=str).encode()).hexdigest()[:32],
                          "json": r} for r in rows], "skipInvalidRows": False, "ignoreUnknownValues": False}
        out = _req("POST", f"{BQ}/projects/{self.project}/datasets/{dataset}/tables/{table}/insertAll", body)
        if out.get("insertErrors"):
            raise TableError(f"{table}: insertAll отклонил {len(out['insertErrors'])} строк")
