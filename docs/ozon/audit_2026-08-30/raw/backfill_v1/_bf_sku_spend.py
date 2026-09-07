#!/usr/bin/env python3
"""Stage 2.1 — SKU-уровневый рекламный расход через асинхронный отчёт Performance API.

ТОЛЬКО READ. Лимиты Ozon: не более 62 дней и не более 10 кампаний на отчёт,
одна одновременная выгрузка на аккаунт. Токен в памяти, на диск не пишется.
"""
import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bf_common import PerfToken, PERF, log_error, save  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "performance", "sku_spend")
os.makedirs(OUT, exist_ok=True)

# кампании, у которых за период был расход: 15 текущих CPC + 4 архивных legacy + SEARCH_PROMO
CAMPAIGNS = ["32157891", "32157919", "32157947", "32157965", "32157988", "32158008",
             "32158051", "32158176", "32158299", "33678192", "33678220", "33678261",
             "33678288", "33678331", "33703975",
             "27443733", "27307598", "27307535", "27251978", "14503166"]
CHUNKS = [("2026-06-01", "2026-07-31"), ("2026-08-01", "2026-08-31")]


def post(path, body, token):
    req = urllib.request.Request(
        PERF + path, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def get_raw(path, token):
    req = urllib.request.Request(PERF + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def run(batch, frm, to, tk, tag):
    body = {"campaigns": batch, "from": f"{frm}T00:00:00Z", "to": f"{to}T00:00:00Z",
            "groupBy": "DATE"}
    try:
        uuid = post("/api/client/statistics", body, tk.get()).get("UUID")
    except urllib.error.HTTPError as e:
        log_error("perf_sku_spend", "/api/client/statistics", tag, e.code, 1, True,
                  e.read().decode()[:200])
        print(f"{tag}: submit FAILED", flush=True)
        return
    if not uuid:
        print(f"{tag}: нет UUID", flush=True)
        return
    print(f"{tag}: UUID={uuid}", flush=True)
    for i in range(60):
        time.sleep(10)
        try:
            st = json.loads(get_raw(f"/api/client/statistics/{uuid}", tk.get()))
        except Exception:                                            # noqa: BLE001
            continue
        s = st.get("state")
        if s == "OK":
            break
        if s == "ERROR":
            log_error("perf_sku_spend", "/api/client/statistics", tag, "ERROR", i, True,
                      json.dumps(st)[:200])
            print(f"{tag}: отчёт упал", flush=True)
            return
    else:
        log_error("perf_sku_spend", "/api/client/statistics", tag, "TIMEOUT", 60, True,
                  "state не стал OK за 10 минут")
        print(f"{tag}: таймаут", flush=True)
        return
    blob = get_raw(f"/api/client/statistics/report?UUID={uuid}", tk.get())
    n = 0
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        for name in z.namelist():
            open(os.path.join(OUT, name), "wb").write(z.read(name))
            n += 1
    except zipfile.BadZipFile:
        open(os.path.join(OUT, f"{tag}_raw.csv"), "wb").write(blob)
        n = 1
    print(f"{tag}: скачано файлов {n}", flush=True)


if __name__ == "__main__":
    tk = PerfToken()
    for frm, to in CHUNKS:
        for i in range(0, len(CAMPAIGNS), 10):
            batch = CAMPAIGNS[i:i + 10]
            run(batch, frm, to, tk, f"{frm}_{to}_b{i // 10 + 1}")
            time.sleep(5)
    print("=== SKU SPEND DONE ===", flush=True)
