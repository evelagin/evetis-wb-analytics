#!/usr/bin/env python3
"""Stage 2 — QA-гейты перед любыми бизнес-выводами."""
import csv
import glob
import json
import os
import subprocess
from collections import Counter, defaultdict
from datetime import date, timedelta

AUDIT = os.path.dirname(os.path.abspath(__file__))
BF = os.path.join(AUDIT, "raw", "backfill_v1")
DATA = os.path.join(AUDIT, "data")
ALL_DAYS = [(date(2026, 6, 1) + timedelta(d)).isoformat() for d in range(92)]

gates = []


def gate(name, ok, detail, critical=True):
    gates.append(dict(gate=name, status="PASS" if ok else "FAIL",
                      critical="yes" if critical else "no", detail=detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def load(n):
    return list(csv.DictReader(open(os.path.join(DATA, n), encoding="utf-8")))


print("=== QA GATES ===")

# 1. Каталог 20/20
cat = load("catalog_reconciliation.csv")
mapped = [r for r in cat if r["internal_sku"] and r["nm_id_wb"] and r["offer_id"] == r["nm_id_wb"]]
gate("catalog_mapping_20_of_20", len(cat) == 20 and len(mapped) == 20,
     f"{len(mapped)}/{len(cat)} товаров имеют internal_sku и offer_id == nm_id")

# 2. Покрытие COGS
cov = [r for r in cat if r["cogs_found"] == "yes"]
gate("cogs_coverage_catalog", len(cov) == len(cat), f"{len(cov)}/{len(cat)} товаров с COGS")

ue = load("sku_unit_economics_daily.csv")
missing = [r for r in ue if r["cogs_status"] != "OK"]
gate("cogs_coverage_transactions", not missing,
     f"строк экономики без действующего COGS: {len(missing)} из {len(ue)}")

# 3. Полнота финансов
fin_files = sorted(glob.glob(os.path.join(BF, "finance", "accrual_by_day_*.json")))
fin_dates, bad_http, truncated = set(), [], []
for p in fin_files:
    d = json.load(open(p, encoding="utf-8"))
    fin_dates.add(d["date"])
    if d["http_status"] != 200:
        bad_http.append(d["date"])
    if d.get("pages", 0) > 55:
        truncated.append(d["date"])
gate("finance_date_completeness", set(ALL_DAYS) <= fin_dates,
     f"{len(fin_dates)}/92 дней, отсутствуют: {sorted(set(ALL_DAYS) - fin_dates) or 'нет'}")
gate("finance_http_ok", not bad_http, f"дней с HTTP != 200: {len(bad_http)}")
gate("finance_pagination_not_truncated", not truncated,
     f"дней, упёршихся в лимит страниц: {len(truncated)}")

# 4. Полнота Performance
perf_dates = {os.path.basename(p)[len("expense_"):-4] for p in
              glob.glob(os.path.join(BF, "performance", "expense_*.csv"))}
perf_iso = {f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in perf_dates}
gate("performance_date_completeness", set(ALL_DAYS) <= perf_iso,
     f"{len(perf_iso)}/92 дней, отсутствуют: {sorted(set(ALL_DAYS) - perf_iso) or 'нет'}")

# 5. Провалившиеся обязательные чанки
errs = []
ep = os.path.join(DATA, "backfill_errors.csv")
if os.path.exists(ep):
    errs = list(csv.DictReader(open(ep, encoding="utf-8")))
req_fail = [e for e in errs if e["required"] == "True"
            and not (e["dataset"] == "finance_realization" and e["chunk"] == "202608")]
gate("failed_required_chunks_zero", not req_fail,
     f"всего ошибок {len(errs)}, из них блокирующих {len(req_fail)}. "
     f"Реализация за 202608 — 404, месяц не закрыт, это ожидаемо")

# 6. Сверка CPC
rec = load("cpc_daily_reconciliation.csv")
mism = [r for r in rec if r["status"] != "EXACT_MATCH"]
gate("cpc_finance_reconciliation", not mism,
     f"{len(rec)} дней со сверкой, расхождений: {len(mism)}")
worst = max((abs(float(r["difference_rub"])) for r in rec), default=0)
gate("cpc_reconciliation_max_abs_diff", worst < 0.005, f"максимальное |разница| = {worst:.4f} руб")

# 7. Сверка SEARCH_PROMO
pm = [r for r in rec if abs(float(r["difference_promo_rub"])) >= 0.005]
gate("search_promo_reconciliation", not pm, f"дней с расхождением по типу 54: {len(pm)}")

# 8. Дубли natural key
fin = load("finance_accrual_fact.csv")
k = Counter((r["operation_id"], r["type_id"], r["ozon_sku"], r["amount"]) for r in fin)
gate("finance_no_duplicate_keys", all(v == 1 for v in k.values()),
     f"дублей ключа (operation_id,type_id,sku,amount): {sum(1 for v in k.values() if v > 1)}",
     critical=False)
ordf = load("orders_fbo_fact.csv")
ko = Counter((r["posting_number"], r["ozon_sku"]) for r in ordf)
gate("orders_no_duplicate_keys", all(v == 1 for v in ko.values()),
     f"дублей (posting_number,sku): {sum(1 for v in ko.values() if v > 1)}", critical=False)
pcd = load("performance_campaign_daily.csv")
kp = Counter((r["date"], r["campaign_id"]) for r in pcd)
gate("performance_no_duplicate_keys", all(v == 1 for v in kp.values()),
     f"дублей (date,campaign_id): {sum(1 for v in kp.values() if v > 1)}")

# 9. Пропуски дат в портфеле
port = load("portfolio_daily.csv")
gate("portfolio_date_completeness", len(port) == 92,
     f"{len(port)}/92 дней в portfolio_daily")

# 10. Количества
badq = [r for r in ordf if not str(r["quantity"]).isdigit() or int(r["quantity"]) <= 0]
gate("orders_quantity_valid", not badq, f"позиций с некорректным quantity: {len(badq)}")
badu = [r for r in ue if int(r["quantity"]) < 0]
gate("unit_econ_quantity_valid", not badu, f"строк экономики с отрицательным quantity: {len(badu)}")

# 11. Валюта
cur = Counter(r["currency"] for r in fin)
gate("currency_single_rub", set(cur) == {"RUB"}, f"валюты в начислениях: {dict(cur)}")

# 12. Утечка credentials
leak = []
try:
    sec = {}
    for n in ("EVETIS_OZON_CLIENT_ID", "EVETIS_OZON_API_KEY",
              "EVETIS_OZON_PERFORMANCE_CLIENT_ID", "EVETIS_OZON_PERFORMANCE_CLIENT_SECRET"):
        sec[n] = subprocess.run(["gcloud", "secrets", "versions", "access", "latest",
                                 f"--secret={n}"], capture_output=True, text=True,
                                check=True).stdout.strip()
    files = glob.glob(os.path.join(BF, "**", "*"), recursive=True)
    files += glob.glob(os.path.join(DATA, "*"))
    for p in files:
        if not os.path.isfile(p):
            continue
        try:
            t = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for n, v in sec.items():
            # Client-Id Ozon сам возвращает как company_id — это не секрет
            if n == "EVETIS_OZON_CLIENT_ID":
                continue
            if v and v in t:
                leak.append((os.path.relpath(p, AUDIT), n))
except Exception as e:                                            # noqa: BLE001
    leak.append(("scan_failed", repr(e)))
gate("no_credential_leak", not leak, f"совпадений секретов в файлах: {len(leak)} {leak[:3]}")

# 13. Bearer-токены в файлах данных.
# Ищем ЗНАЧЕНИЕ токена (JWT-форма), а не имя поля: строка `d["access_token"]`
# в исходниках загрузчика — это код, а не утечка. Скрипты из скана исключены.
import re as _re

JWT = _re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}")
tokleak = []
for p in {os.path.realpath(x) for x in glob.glob(os.path.join(BF, "**", "*"), recursive=True)}:
    if not os.path.isfile(p) or os.path.splitext(p)[1] in (".py", ".sh", ".log"):
        continue
    if JWT.search(open(p, encoding="utf-8", errors="ignore").read(500000)):
        tokleak.append(os.path.relpath(p, AUDIT))
gate("no_token_persisted", not tokleak,
     f"файлов данных с JWT-подобным значением: {len(tokleak)} {tokleak[:3]}")

with open(os.path.join(DATA, "qa_gates.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["gate", "status", "critical", "detail"])
    w.writeheader()
    w.writerows(gates)

crit_fail = [g for g in gates if g["status"] == "FAIL" and g["critical"] == "yes"]
print()
print(f"ИТОГ: {sum(1 for g in gates if g['status'] == 'PASS')}/{len(gates)} PASS, "
      f"критических FAIL: {len(crit_fail)}")
print("VERDICT:", "PASSED" if not crit_fail else "FAILED — выводы не строить")
