#!/usr/bin/env python3
"""Ozon акции v2: проба формы ответа ТОЛЬКО методами чтения (для фикстуры адаптера v2).

По умолчанию — сухой режим: печатает план запросов, сети нет. Живой запрос — только с
`--execute` и только по OWNER ACK. Закрытый список путей: /v1/actions (GET) и четыре метода
чтения v2 (POST с телом-фильтром). Любой другой путь, в т. ч. мутирующие
(/v1/actions/products/update, /v2/actions/products/deactivate, /v2/actions/auto-add/products/update|delete),
падает ДО сети. Ничего не пишется в BigQuery и в кабинет; ответы сохраняются локально
(--out, по умолчанию вне репозитория) — в них коммерческие данные, в Git не класть.

Ключи EVETIS читаются из Secret Manager (EVETIS_OZON_CLIENT_ID, EVETIS_OZON_API_KEY) учёткой
владельца в момент запуска и нигде не печатаются.

    python3 tools/ozon/promo_v2_probe.py                 # план, без сети
    python3 tools/ozon/promo_v2_probe.py --execute       # по OWNER ACK
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

SELLER = "https://api-seller.ozon.ru"
PROJECT = "project-fa311fc0-4d87-4781-986"
READ_ONLY = {
    "/v1/actions": "GET",
    "/v2/actions/products": "POST",
    "/v2/actions/candidates": "POST",
    "/v2/actions/auto-add/products/list": "POST",
    "/v2/actions/auto-add/products/candidates": "POST",
}
PROBE_LIMIT = 5          # товаров на запрос: форма ответа, а не выгрузка
MAX_ACTIONS = 3
MAX_DATES = 2


class ProbePathDenied(RuntimeError):
    pass


def check(path: str, method: str) -> None:
    if READ_ONLY.get(path) != method:
        raise ProbePathDenied(f"{method} {path} вне закрытого списка чтения пробы")


def plan(actions: list[dict]) -> list[tuple[str, str, dict | None]]:
    """Список (метод, путь, тело) для уже полученного списка акций."""
    out: list[tuple[str, str, dict | None]] = []
    for a in actions[:MAX_ACTIONS]:
        aid = a.get("id")
        out.append(("POST", "/v2/actions/products", {"action_id": aid, "limit": PROBE_LIMIT, "last_id": ""}))
        out.append(("POST", "/v2/actions/candidates", {"action_id": aid, "limit": PROBE_LIMIT, "last_id": ""}))
        for d in (a.get("auto_add_dates") or [])[:MAX_DATES]:
            for p in ("/v2/actions/auto-add/products/list", "/v2/actions/auto-add/products/candidates"):
                out.append(("POST", p, {"action_id": aid, "auto_add_date": d, "limit": PROBE_LIMIT, "offset": 0}))
    for m, p, _ in out:
        check(p, m)
    return out


def secret(name: str) -> str:
    return subprocess.run(["gcloud", "secrets", "versions", "access", "latest", f"--secret={name}", f"--project={PROJECT}"],
                          check=True, capture_output=True, text=True).stdout.strip()


def call(method: str, path: str, body: dict | None, headers: dict) -> tuple[int, object]:
    check(path, method)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SELLER + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, {"_error": e.read().decode(errors="replace")[:500]}


def shape(payload: object) -> dict:
    """Форма ответа без значений: ключи верхнего уровня и первого товара, признак Money."""
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    root = payload.get("result", payload) if isinstance(payload.get("result"), dict) else payload
    items = root.get("products") if isinstance(root, dict) else None
    first = items[0] if isinstance(items, list) and items else {}
    return {"top_keys": sorted(payload), "wrapped_in_result": "result" in payload, "items": len(items or []),
            "item_keys": sorted(first), "money_fields": sorted(k for k, v in first.items() if isinstance(v, dict) and "amount" in v)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="живые запросы чтения (только по OWNER ACK)")
    ap.add_argument("--out", default=str(Path.home() / "EVETIS_AUDIT_REPORTS" / "ozon_promo_v2_probe"))
    a = ap.parse_args(argv)
    if not a.execute:
        print("СУХОЙ РЕЖИМ: сети нет. План — GET /v1/actions, затем на каждую из ≤", MAX_ACTIONS,
              "акций: POST /v2/actions/products|candidates (limit", PROBE_LIMIT, "), на ≤", MAX_DATES,
              "дат автодобавления — POST /v2/actions/auto-add/products/list|candidates (limit", PROBE_LIMIT, ").")
        print("Разрешено только:", ", ".join(f"{m} {p}" for p, m in READ_ONLY.items()))
        return 0
    headers = {"Client-Id": secret("EVETIS_OZON_CLIENT_ID"), "Api-Key": secret("EVETIS_OZON_API_KEY"),
               "Content-Type": "application/json"}
    run_dir = Path(a.out) / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=False)
    code, acts = call("GET", "/v1/actions", None, headers)
    (run_dir / "00_v1_actions.json").write_text(json.dumps({"status": code, "body": acts}, ensure_ascii=False, indent=1))
    summary = [{"call": "GET /v1/actions", "status": code, **shape(acts)}]
    actions = acts.get("result", []) if isinstance(acts, dict) else []
    for n, (m, p, b) in enumerate(plan(actions), start=1):
        c, body = call(m, p, b, headers)
        (run_dir / f"{n:02d}_{p.strip('/').replace('/', '_')}.json").write_text(
            json.dumps({"request": b, "status": c, "body": body}, ensure_ascii=False, indent=1))
        summary.append({"call": f"{m} {p}", "request": b, "status": c, **shape(body)})
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    for s in summary:
        print(s["status"], s["call"], "wrapped" if s.get("wrapped_in_result") else "flat",
              "items", s.get("items"), "money", s.get("money_fields"))
    print("Сохранено:", run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
