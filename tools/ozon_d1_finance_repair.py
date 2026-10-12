#!/usr/bin/env python3
"""Ozon D1: план ремонта истории RAW_OZON_FINANCE_ACCRUAL — экономические блоки без services (сторно реализации).

До D1 `_finance_rows` терял начисление, где у продукта есть блок commission, а services нет: сторно
реализации при возврате покупателя не попадало в RAW вовсе. Исправленный разбор пишет такой блок строкой
с синтетическим type_id (FIN_ECON_*, отрицательный). Суточная загрузка перечитывает только 30 суток,
поэтому старые сторно сами не появятся — этот инструмент строит и (по отдельному решению) применяет
ограниченный ремонт: ТОЛЬКО ДОБАВЛЕНИЕ доказанных строк, без удалений и без замены окон.

Репозиторий публичный: поштучные строки (accrual_id, номера отправлений, суммы) пишутся только в каталог
вне Git (--out-dir / --manifest-dir). Сам файл содержит правила и проверки, но не данные.

Источник истины — сохранённые ответы POST /v1/finance/accrual/by-day (индекс JSONL: date, page, last_id_out,
final, evidence_file, sha256), снятые read-only клиентом. Строки строит ЕДИНСТВЕННЫЙ сборщик —
pipelines/ozon/runtime/entities._finance_rows (тот же код, что и в суточной загрузке).

Режимы:
  PLAN (по умолчанию, BigQuery только чтение через lib.bq_readonly). Область доказательства — сутки
    ≤ --through (по умолчанию сегодня МСК − 31): старше окна суточной загрузки (30 суток), там RAW больше
    не меняется, и поздние публикации Ozon не роняют план. Свежие сутки не чинятся: их перезагрузит
    исправленный разбор после деплоя (или следующий план). Отпечаток считается только по области.
    · страницы: sha256 совпадает с индексом, у каждого дня последняя страница терминальна, дни подряд;
    · контроль денег каждого дня: Σ total_amount = Σ разобранного (иначе отказ);
    · живой RAW за окно читается помесячно; обратная проверка: каждый ключ RAW есть в источнике;
      совпадающие ключи равны по деньгам (amount_rub) и экономика пары (начисление × SKU) равна;
    · ДОБАВИТЬ = строки FIN_ECON_*, которых нет в RAW; прочие строки источника вне RAW (поздняя публикация)
      не чинятся, только показываются — их заберёт обычная загрузка;
    · сторно (FIN_ECON_RETURN) обязано иметь в RAW исходную продажу той же пары отправление × SKU;
    · сохранение денег по дням: ИСТОЧНИК = RAW + ДОБАВИТЬ + ПОЗДНИЕ + ПРЕДЕЛ_КОЛОНОК, остаток 0,00;
    · итоги по месяцам (₽, строки, SKU), отпечаток sha256 плана (включает состояние живого RAW).
  APPLY (--apply --plan-fingerprint <sha> --manifest-dir <пустой каталог вне Git>): чистое дерево и HEAD =
    origin/main; не в окне суточной загрузки (03:15–04:30 UTC); повтор PLAN с тем же отпечатком; одна
    транзакция: MERGE … WHEN NOT MATCHED THEN INSERT (совпавший ключ не перезаписывается) + ASSERT числа
    вставленных строк. Перечитывание: строки прогона = план.
  ROLLBACK (--rollback --manifest-dir <D> [--apply]): удаление ровно ключей плана с ingestion_run_id ремонта.

usage:
  python tools/ozon_d1_finance_repair.py --project <P> --token-command "gcloud auth print-access-token" \\
      --pages-index <вне Git>/by_day_index.jsonl --types <вне Git>/accrual_types.json --out-dir <вне Git>
  … --apply --plan-fingerprint <sha> --manifest-dir <пустой каталог вне Git>
  … --rollback --manifest-dir <D> [--apply]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import types as pytypes
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.bq_readonly import ReadOnlyBigQuery, resolve_token  # noqa: E402

TOOL = "ozon_d1_finance_repair/v1"
ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "pipelines" / "ozon" / "runtime"
PROJECT = "project-fa311fc0-4d87-4781-986"
DATASET = "ozon_raw"
TABLE = "RAW_OZON_FINANCE_ACCRUAL"
T_RAW = f"`{PROJECT}.{DATASET}.{TABLE}`"
HOST = "https://www.googleapis.com/bigquery/v2"
KEYS = ("accrual_id", "type_id", "sku")
COLUMNS = ("event_date", "accrual_id", "type_id", "operation_name", "accrued_category", "unit_number",
           "unit_number_meaning", "posting_number", "sku", "amount_rub", "currency", "seller_base_price_rub",
           "buyer_paid_price_rub", "ozon_bonus_rub", "ozon_coinvestment_rub", "commission_rub", "commission_ratio",
           "quantity", "extracted_at", "source_endpoint", "ingestion_run_id", "source_payload_hash")
NUMERIC = {"amount_rub", "seller_base_price_rub", "buyer_paid_price_rub", "ozon_bonus_rub",
           "ozon_coinvestment_rub", "commission_rub", "commission_ratio"}
RUN_PREFIX = "ozd1-repair-"
FP_RE = re.compile(r"^[0-9a-f]{64}$")
INGESTION_WINDOW_UTC = ((3, 15), (4, 30))     # суточная загрузка ozon-runtime-daily — 03:30 UTC
LOADING_WINDOW_DAYS = 30                      # REGISTRY["finance_accrual"]: окно ретроспективы суток
MSK = dt.timezone(dt.timedelta(hours=3))
CENT = Decimal("0.01")


class PlanRefused(Exception):
    pass


def _req(cond: bool, msg: str) -> None:
    if not cond:
        raise PlanRefused(msg)


def dec(v) -> Decimal:
    return Decimal(str(v)) if v not in (None, "") else Decimal(0)


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha(obj) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def key(r) -> tuple:
    return (int(r["accrual_id"]), int(r["type_id"]), None if r["sku"] is None else str(r["sku"]))


# ───────────────────────────── единственный сборщик строк ─────────────────────────────

def load_builder():
    """entities из pipelines/ozon/runtime — только чистая функция разбора, без сети и облака.

    Модуль common при импорте требует GCP_PROJECT_ID и google.cloud. Для разбора они не нужны: проект
    подменяется заведомо не-production значением на время импорта, при отсутствии библиотек ставятся
    заглушки имён (как в офлайн-тестах pipelines/ozon/tests/conftest.py). Сетевые функции модуля
    инструмент не вызывает.
    """
    if "entities" in sys.modules and hasattr(sys.modules["entities"], "_finance_rows"):
        return sys.modules["entities"]
    try:
        from google.cloud import bigquery, secretmanager  # noqa: F401
    except ImportError:
        bq = pytypes.ModuleType("google.cloud.bigquery")
        sm = pytypes.ModuleType("google.cloud.secretmanager")
        google = sys.modules.setdefault("google", pytypes.ModuleType("google"))
        cloud = pytypes.ModuleType("google.cloud")
        cloud.bigquery, cloud.secretmanager, google.cloud = bq, sm, cloud
        sys.modules.update({"google.cloud": cloud, "google.cloud.bigquery": bq, "google.cloud.secretmanager": sm})
    saved = {k: os.environ.get(k) for k in ("GCP_PROJECT_ID", "STRICT_PAGE_CAPS")}
    os.environ["GCP_PROJECT_ID"] = "offline-repair-plan"
    os.environ.pop("STRICT_PAGE_CAPS", None)
    sys.path.insert(0, str(RUNTIME))
    try:
        import entities as E
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return E


# ───────────────────────────── источник: сохранённые страницы ─────────────────────────────

def outside_repo(raw: str | None, what: str) -> Path:
    if not raw:
        raise SystemExit(f"нужен {what}")
    p = Path(raw).resolve()
    if p == ROOT or ROOT in p.parents:
        raise SystemExit(f"{what} внутри репозитория — публичный Git, отказ")
    return p


def load_pages(index_path: Path) -> dict:
    """{day: {"accruals": [...], "pages": [{file, sha256, fetched_at}]}}; проверки полноты обхода."""
    base = index_path.parent
    days = collections.defaultdict(list)
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            days[r["date"]].append(r)
    out = {}
    for d in sorted(days):
        pages = sorted(days[d], key=lambda r: r["page"])
        _req([p["page"] for p in pages] == list(range(1, len(pages) + 1)), f"{d}: страницы не подряд")
        _req(pages[-1]["final"] and all(not p["final"] for p in pages[:-1]), f"{d}: обход last_id не завершён")
        for a, b in zip(pages, pages[1:]):
            _req(a["last_id_out"] == b["last_id_in"], f"{d}: разрыв курсора last_id")
        acc, meta = [], []
        for p in pages:
            f = base / p["evidence_file"]
            raw = f.read_bytes()
            _req(hashlib.sha256(raw).hexdigest() == p["sha256"], f"{d}: sha256 страницы {p['page']} не совпал")
            acc.extend(json.loads(raw)["accruals"] or [])
            meta.append({"file": p["evidence_file"], "sha256": p["sha256"], "fetched_at": p["fetched_at"]})
        out[d] = {"accruals": acc, "pages": meta}
    ds = sorted(out)
    _req(bool(ds), "индекс страниц пуст")
    span = (dt.date.fromisoformat(ds[-1]) - dt.date.fromisoformat(ds[0])).days + 1
    _req(span == len(ds), "дни источника не подряд — окно не доказано целиком")
    return out


def build_candidates(E, pages: dict, types: dict) -> dict:
    """Строки исправленным разбором + контроль денег суток. Метаданные строки — из страницы источника."""
    rows, ctl_days, qty, src_day = [], {}, {}, {}
    for d, p in pages.items():
        ctl = {}
        fetched = max(m["fetched_at"] for m in p["pages"])          # ISO UTC, «…Z»
        extracted_at = fetched.replace("T", " ").replace("Z", "") + "+00:00"
        rows.extend(E._finance_rows(p["accruals"], types, d, "ozon-d1-repair", extracted_at, control=ctl))
        _req(not ctl.get("unbalanced"), f"{d}: контроль денег суток не сошёлся — разбор неполон, ремонт запрещён")
        _req(not ctl.get("no_total"), f"{d}: начисление без total_amount — сверить нечем")
        _req(not ctl.get("quarantined"), f"{d}: начисление в карантине (направление блока не доказано) — ремонт запрещён")
        ctl_days[d] = ctl
        src_day[d] = ctl.get("source", Decimal(0))
        for a in p["accruals"]:
            for pr in (a.get("posting") or {}).get("products") or []:
                qty[(int(a["accrual_id"]), str(pr.get("sku")))] = pr.get("quantity")
    keys = [key(r) for r in rows]
    _req(len(set(keys)) == len(keys), "повтор ключа слияния в строках источника (режим reject)")
    return {"rows": rows, "control": ctl_days, "qty": qty, "source_by_day": src_day}


# ───────────────────────────── живой RAW (только чтение) ─────────────────────────────

class LiveSource:
    def __init__(self, bq: ReadOnlyBigQuery):
        self.bq = bq

    def raw_rows(self, d0: str, d1: str) -> list[dict]:
        out, m = [], dt.date.fromisoformat(d0).replace(day=1)
        end = dt.date.fromisoformat(d1)
        while m <= end:
            nxt = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            lo, hi = max(m, dt.date.fromisoformat(d0)), min(nxt - dt.timedelta(days=1), end)
            out += self.bq.query(
                "SELECT CAST(event_date AS STRING) event_date, accrual_id, type_id, sku, posting_number, "
                "CAST(amount_rub AS STRING) amount_rub, CAST(seller_base_price_rub AS STRING) sp, "
                "CAST(commission_rub AS STRING) cm, ingestion_run_id "
                f"FROM {T_RAW} WHERE event_date BETWEEN DATE '{lo}' AND DATE '{hi}'", max_results=20000)
            m = nxt
        return out

    def run_rows(self, run_id: str) -> list[dict]:
        _req(bool(re.fullmatch(RUN_PREFIX + r"[0-9a-f]{16}", run_id)), "недопустимый ingestion_run_id ремонта")
        cols = ", ".join(f"CAST({c} AS STRING) {c}" for c in COLUMNS if c != "extracted_at")
        return self.bq.query(f"SELECT {cols} FROM {T_RAW} WHERE ingestion_run_id = '{run_id}'", max_results=20000)


def default_through(now: dt.datetime) -> str:
    return str(now.astimezone(MSK).date() - dt.timedelta(days=LOADING_WINDOW_DAYS + 1))


def scope_pages(pages: dict, through: str, now: dt.datetime) -> dict:
    """Сутки ≤ through. through внутри окна загрузки запрещён: там RAW ещё меняется."""
    _req(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", through)), "--through: дата YYYY-MM-DD")
    _req(through <= default_through(now), f"--through {through} внутри окна загрузки "
         f"{LOADING_WINDOW_DAYS} сут.: RAW там ещё меняется, план не воспроизводим")
    _req(min(pages) <= through <= max(pages), "--through вне сохранённых доказательств")
    return {d: p for d, p in pages.items() if d <= through}


def tail_report(E, tail: dict, types: dict) -> dict:
    """Сутки после through — только для журнала: ни отпечаток, ни решение плана от них не зависят.

    Здесь нет _req: несошедшийся контроль, карантин или поздняя публикация в свежих сутках —
    нормальное состояние окна загрузки и не блокирует ремонт истории.
    """
    out = {"days": len(tail), "synthetic_rows": 0, "unbalanced_days": 0, "quarantined": 0, "no_total": 0}
    for d, p in tail.items():
        ctl = {}
        rows = E._finance_rows(p["accruals"], types, d, "ozon-d1-repair", "1970-01-01 00:00:00+00:00", control=ctl)
        out["synthetic_rows"] += sum(1 for r in rows if r["type_id"] < 0)
        out["unbalanced_days"] += 1 if ctl.get("unbalanced") else 0
        out["quarantined"] += ctl.get("quarantined", 0)
        out["no_total"] += ctl.get("no_total", 0)
    return out


def readback_diff(got: list[dict], plan_rows: list[dict]) -> list:
    """Строки прогона ремонта против плана: ключи И содержимое (кроме extracted_at)."""
    norm = lambda c, v: (None if v is None else  # noqa: E731
                         dec(v) if c in NUMERIC else int(v) if c in ("accrual_id", "type_id", "quantity")
                         else str(v))
    want = {key(r): {c: norm(c, r[c]) for c in COLUMNS if c != "extracted_at"} for r in plan_rows}
    have = {key(r): {c: norm(c, r.get(c)) for c in COLUMNS if c != "extracted_at"} for r in got}
    diff = sorted(set(want) ^ set(have))
    diff += [k for k in set(want) & set(have) for c in want[k] if want[k][c] != have[k][c]]
    return diff


def econ_money(row, qty) -> Decimal:
    """Деньги экономического блока так, как их видит FCT: цена продавца × кол-во + комиссия."""
    q = qty.get((int(row["accrual_id"]), str(row["sku"])))
    return dec(row["sp"]) * (q if isinstance(q, int) else 1) + dec(row["cm"])


def build_plan(E, pages: dict, types: dict, live: list[dict]) -> dict:
    cand = build_candidates(E, pages, types)
    synthetic = set(E.FIN_ECON_TYPE_NAMES)
    ck = {key(r): r for r in cand["rows"]}
    lk = {}
    for r in live:
        k = key(r)
        _req(k not in lk, "в живом RAW повтор ключа слияния")
        lk[k] = r
    window = (min(pages), max(pages))
    # обратная проверка: каждая строка RAW окна есть в источнике и равна ему по сумме услуги
    raw_only = sorted(set(lk) - set(ck))
    _req(not raw_only, f"в RAW {len(raw_only)} ключ(ей), которых нет в источнике — окно не доказано")
    amount_diff = [k for k in set(lk) & set(ck) if dec(lk[k]["amount_rub"]) != dec(ck[k]["amount_rub"])]
    _req(not amount_diff, f"{len(amount_diff)} ключ(ей) RAW расходятся с источником по amount_rub")
    # экономика пары (начисление × SKU): суммы по строкам равны (блок может висеть на другой услуге —
    # порядок services в ответе Ozon не гарантирован)
    pe_raw = collections.defaultdict(lambda: [Decimal(0), Decimal(0)])
    pe_src = collections.defaultdict(lambda: [Decimal(0), Decimal(0)])
    for k, r in lk.items():
        if r["sp"] is not None:
            pe_raw[(k[0], k[2])][0] += dec(r["sp"])
            pe_raw[(k[0], k[2])][1] += dec(r["cm"])
    for k, r in ck.items():
        if k in lk and r["seller_base_price_rub"] is not None:
            pe_src[(k[0], k[2])][0] += dec(round(r["seller_base_price_rub"], 4))
            pe_src[(k[0], k[2])][1] += dec(round(r["commission_rub"], 4))
    _req(dict(pe_raw) == dict(pe_src), "экономический блок пары начисление × SKU в RAW расходится с источником")
    add = sorted(k for k in set(ck) - set(lk) if k[1] in synthetic)
    late = sorted(k for k in set(ck) - set(lk) if k[1] not in synthetic)
    present = sorted(k for k in set(ck) & set(lk) if k[1] in synthetic)
    sale_pairs = {(r["posting_number"], str(r["sku"])) for r in live if r["sp"] is not None and dec(r["sp"]) > 0}
    unpaired = [k for k in add if k[1] == E.FIN_ECON_RETURN_TYPE_ID
                and (ck[k]["posting_number"], k[2]) not in sale_pairs]
    _req(not unpaired, f"{len(unpaired)} сторно без исходной продажи той же пары отправление × SKU в RAW")
    # сохранение денег по дням
    qty, days = cand["qty"], {}
    srow = lambda r: {"amount_rub": r["amount_rub"], "sp": r["seller_base_price_rub"],  # noqa: E731
                      "cm": r["commission_rub"], "accrual_id": r["accrual_id"], "sku": r["sku"]}
    money = lambda r: dec(r["amount_rub"]) + (econ_money(r, qty) if r["sp"] is not None else 0)  # noqa: E731
    for d in pages:
        days[d] = collections.defaultdict(Decimal)
        days[d]["source"] = cand["source_by_day"][d]
        days[d]["repr_gap"] = cand["control"][d].get("repr_gap_rub", Decimal(0))
    for k, r in lk.items():
        days[r["event_date"]]["raw"] += money(r)
    for name, ks in (("add", add), ("late", late)):
        for k in ks:
            r = ck[k]
            days[str(r["event_date"])][name] += money(srow(r))
    bad = []
    for d, v in days.items():
        v["residual"] = v["source"] - v["raw"] - v["add"] - v["late"] - v["repr_gap"]
        if v["residual"] != 0:
            bad.append(d)
    _req(not bad, f"сохранение денег не сошлось за {len(bad)} сут., первые {bad[:3]}")
    months = collections.defaultdict(lambda: collections.defaultdict(Decimal))
    for d, v in days.items():
        for c in ("source", "raw", "add", "late", "repr_gap", "residual"):
            months[d[:7]][c] += v[c]
    add_rows = []
    for k in add:
        r = dict(ck[k])
        add_rows.append({c: r[c] for c in COLUMNS})
    by_month = collections.defaultdict(lambda: {"rows": 0, "seller_price_x_qty_rub": Decimal(0),
                                                "commission_rub": Decimal(0), "money_rub": Decimal(0)})
    by_month_sku = collections.defaultdict(lambda: {"rows": 0, "money_rub": Decimal(0)})
    for r in add_rows:
        m, s = str(r["event_date"])[:7], srow(r)
        q = qty.get((int(r["accrual_id"]), str(r["sku"])))
        by_month[m]["rows"] += 1
        by_month[m]["seller_price_x_qty_rub"] += dec(r["seller_base_price_rub"]) * (q if isinstance(q, int) else 1)
        by_month[m]["commission_rub"] += dec(r["commission_rub"])
        by_month[m]["money_rub"] += money(s)
        by_month_sku[(m, r["sku"])]["rows"] += 1
        by_month_sku[(m, r["sku"])]["money_rub"] += money(s)
    q2 = lambda x: str(Decimal(x).quantize(CENT))  # noqa: E731
    core = {
        "tool": TOOL, "table": f"{PROJECT}.{DATASET}.{TABLE}", "merge_keys": list(KEYS),
        "window": {"from": window[0], "to": window[1], "days": len(pages)},
        "source_pages_sha256": sha(sorted((d, m["sha256"]) for d, p in pages.items() for m in p["pages"])),
        "live_raw": {"rows": len(lk), "state_sha256": sha(sorted(
            [list(map(str, k)), lk[k]["amount_rub"], lk[k]["sp"], lk[k]["cm"]] for k in lk))},
        "candidate_rows": len(ck), "rows_after_repair": len(lk) + len(add),
        "add": {"rows": len(add), "type_ids": dict(collections.Counter(k[1] for k in add)),
                "earliest_event_date": str(ck[add[0]]["event_date"]) if add else None,
                "latest_event_date": max(str(ck[k]["event_date"]) for k in add) if add else None,
                "money_rub": q2(sum((by_month[m]["money_rub"] for m in by_month), Decimal(0))),
                "rows_sha256": sha(add_rows)},
        "deletes": 0,
        "already_present_synthetic": len(present),
        "late_published_not_repaired": {"rows": len(late), "money_rub": q2(sum((days[d]["late"] for d in days), Decimal(0)))},
        "representation_gap_rub": q2(sum((days[d]["repr_gap"] for d in days), Decimal(0))),
        "duplicate_key_proof": {"plan_keys_unique": len(set(add)) == len(add), "plan_keys_in_live_raw": 0},
        "conservation": {m: {c: q2(v[c]) for c in v} for m, v in sorted(months.items())},
        "monthly_delta": {m: {k2: (q2(v) if isinstance(v, Decimal) else v) for k2, v in x.items()}
                          for m, x in sorted(by_month.items())},
        "monthly_sku_delta": {f"{m}|sku#{i}": {"rows": v["rows"], "money_rub": q2(v["money_rub"])}
                              for i, ((m, _s), v) in enumerate(sorted(by_month_sku.items()))},
    }
    fp = sha(core)
    for r in add_rows:
        r["ingestion_run_id"] = RUN_PREFIX + fp[:16]
    return {"core": core, "fingerprint": fp, "rows": add_rows,
            "monthly_sku_delta_itemized": {f"{m}|{s}": {"rows": v["rows"], "money_rub": q2(v["money_rub"])}
                                           for (m, s), v in sorted(by_month_sku.items())}}


# ───────────────────────────── SQL записи (только APPLY / ROLLBACK --apply) ─────────────────────────────

_SAFE = {"posting_number": r"[0-9]+(-[0-9]+)*", "unit_number": r"[0-9]+(-[0-9]+)*", "sku": r"[0-9]+",
         "accrued_category": r"[A-Z_]+", "currency": r"[A-Z]{3}", "unit_number_meaning": r"[a-z_]+",
         "source_endpoint": r"POST /v1/finance/accrual/by-day", "ingestion_run_id": RUN_PREFIX + r"[0-9a-f]{16}",
         "source_payload_hash": r"[0-9a-f]{32}"}


def literal(col: str, v, op_names: set) -> str:
    if v is None:
        return "NULL"
    if col == "event_date":
        _req(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v))), "дата строки")
        return f"DATE '{v}'"
    if col == "extracted_at":
        _req(bool(re.fullmatch(r"[0-9T:.+\-Z ]{10,40}", str(v))), "extracted_at строки")
        return f"TIMESTAMP '{v}'"
    if col in ("accrual_id", "type_id", "quantity"):
        _req(isinstance(v, int) and not isinstance(v, bool), f"{col} — не целое")
        return str(v)
    if col in NUMERIC:
        return f"NUMERIC '{dec(round(float(v), 4))}'"
    if col == "operation_name":
        _req(v in op_names, "operation_name вне синтетического справочника")
        return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
    _req(bool(re.fullmatch(_SAFE[col], str(v))), f"{col}: значение вне допустимого шаблона")
    return f"'{v}'"


def apply_sql(rows: list[dict], run_id: str, op_names: set) -> str:
    structs = ",\n  ".join("STRUCT(" + ", ".join(f"{literal(c, r[c], op_names)} AS {c}" for c in COLUMNS) + ")"
                           for r in rows)
    on = " AND ".join(f"COALESCE(CAST(T.{k} AS STRING),'\\x00')=COALESCE(CAST(S.{k} AS STRING),'\\x00')" for k in KEYS)
    return (f"BEGIN TRANSACTION;\n"
            f"MERGE {T_RAW} T USING (SELECT * FROM UNNEST([\n  {structs}\n])) S ON {on}\n"
            f"WHEN NOT MATCHED THEN INSERT ({', '.join(COLUMNS)}) VALUES ({', '.join('S.' + c for c in COLUMNS)});\n"
            f"ASSERT (SELECT COUNT(*) FROM {T_RAW} WHERE ingestion_run_id = '{run_id}') = {len(rows)} "
            f"AS 'OZD1_REPAIR: вставлено не ровно {len(rows)} строк';\n"
            f"COMMIT TRANSACTION;")


def rollback_sql(keys: list, run_id: str) -> str:
    _req(bool(re.fullmatch(RUN_PREFIX + r"[0-9a-f]{16}", run_id)), "недопустимый ingestion_run_id")
    lits = ", ".join(f"STRUCT({int(a)} AS a, {int(t)} AS t, '{s}' AS s)" for a, t, s in keys
                     if re.fullmatch(r"[0-9]+", str(s)))
    _req(len(keys) and lits.count("STRUCT") == len(keys), "ключи отката вне шаблона")
    match = (f"ingestion_run_id = '{run_id}' AND type_id < 0 "
             f"AND STRUCT(accrual_id AS a, type_id AS t, sku AS s) IN UNNEST([{lits}])")
    return (f"BEGIN TRANSACTION;\n"
            f"ASSERT (SELECT COUNT(*) FROM {T_RAW} WHERE {match}) = {len(keys)} "
            f"AS 'OZD1_ROLLBACK: по ключам плана не ровно {len(keys)} строк — откат не выполняется';\n"
            f"DELETE FROM {T_RAW} WHERE {match};\n"
            f"ASSERT (SELECT COUNT(*) FROM {T_RAW} WHERE ingestion_run_id = '{run_id}') = 0 AS 'OZD1_ROLLBACK';\n"
            f"COMMIT TRANSACTION;")


def _call(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}


def run_script(sql: str, token: str, label: str) -> dict:
    body = {"configuration": {"query": {"query": sql, "useLegacySql": False},
                              "labels": {"purpose": "ozon-d1-finance-repair", "plan_fp": label}},
            "jobReference": {"projectId": PROJECT, "location": "EU"}}
    code, job = _call("POST", f"{HOST}/projects/{PROJECT}/jobs", token, body)
    if code != 200:
        raise SystemExit(f"jobs.insert HTTP {code}: {(job.get('error') or {}).get('message', '')[:300]}")
    jid = job["jobReference"]["jobId"]
    for _ in range(300):
        if job.get("status", {}).get("state") == "DONE":
            break
        time.sleep(1)
        _, job = _call("GET", f"{HOST}/projects/{PROJECT}/jobs/{jid}?location=EU", token)
    if job.get("status", {}).get("state") != "DONE":
        raise SystemExit(f"задание {jid} не завершилось — проверить вручную, повтор не запускать")
    err = job.get("status", {}).get("errorResult")
    if err:
        raise SystemExit(f"задание {jid} завершилось ошибкой (транзакция откатана): {err.get('message', '')[:300]}")
    return job


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True, cwd=ROOT).stdout.strip()


def require_merged_head() -> str:
    git("fetch", "--quiet", "origin", "main")
    if git("status", "--porcelain"):
        raise SystemExit("--apply: рабочее дерево не чистое")
    head = git("rev-parse", "HEAD")
    if head != git("rev-parse", "origin/main"):
        raise SystemExit("--apply: HEAD не совпадает с origin/main — исполняется только слитый код")
    return head


def outside_ingestion_window(now: dt.datetime) -> bool:
    (h0, m0), (h1, m1) = INGESTION_WINDOW_UTC
    t = now.astimezone(dt.timezone.utc)
    return not (h0 * 60 + m0 <= t.hour * 60 + t.minute < h1 * 60 + m1)


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--token-command")
    p.add_argument("--token-env", default="BQ_READONLY_ACCESS_TOKEN")
    p.add_argument("--pages-index", help="индекс страниц accrual/by-day (JSONL) вне Git")
    p.add_argument("--types", help="ответ /v1/finance/accrual/types (JSON) вне Git")
    p.add_argument("--out-dir", help="каталог вне Git для поштучного плана (PLAN)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--rollback", action="store_true")
    p.add_argument("--plan-fingerprint")
    p.add_argument("--through", help="последние сутки области доказательства (по умолчанию сегодня МСК − 31)")
    p.add_argument("--manifest-dir", help="каталог вне Git для манифеста APPLY (пустой) / ROLLBACK")
    a = p.parse_args(argv)
    if a.project != PROJECT:
        raise SystemExit(f"проект {a.project!r} не совпадает с {PROJECT!r}")
    if a.rollback:
        return rollback(a)
    if a.apply and not (a.plan_fingerprint and FP_RE.match(a.plan_fingerprint)):
        raise SystemExit("--apply: нужен --plan-fingerprint (sha256 из PLAN)")
    index = outside_repo(a.pages_index, "--pages-index")
    types_path = outside_repo(a.types, "--types")
    mdir = outside_repo(a.manifest_dir, "--manifest-dir") if a.apply else None
    out = outside_repo(a.out_dir, "--out-dir") if not a.apply else None
    if a.apply:
        if not outside_ingestion_window(dt.datetime.now(dt.timezone.utc)):
            raise SystemExit("--apply: окно суточной загрузки Ozon (03:15–04:30 UTC) — отказ")
        if mdir.exists() and any(mdir.iterdir()):
            raise SystemExit(f"{mdir}: каталог манифеста не пуст — отказ перезаписи")
    head = require_merged_head() if a.apply else git("rev-parse", "HEAD")
    token = resolve_token(a.token_env, a.token_command, os.environ)
    bq = ReadOnlyBigQuery(project=PROJECT, token=token, label_purpose="ozon-d1-finance-repair-plan")
    src = LiveSource(bq)
    E = load_builder()
    print(f"git HEAD {head} · режим {'APPLY' if a.apply else 'PLAN'}")
    try:
        now = dt.datetime.now(dt.timezone.utc)
        through = a.through or default_through(now)
        all_pages = load_pages(index)
        pages = scope_pages(all_pages, through, now)
        types = {x["id"]: x["description"] for x in json.loads(types_path.read_text(encoding="utf-8"))["accrual_types"]}
        plan = build_plan(E, pages, types, src.raw_rows(min(pages), max(pages)))
        tail = tail_report(E, {d: p for d, p in all_pages.items() if d > through}, types)
    except PlanRefused as e:
        print(f"STOP: {e}")
        return 2
    print(json.dumps(plan["core"], ensure_ascii=False, indent=2, sort_keys=True))
    print(f"FINGERPRINT {plan['fingerprint']}")
    print(f"вне области (сутки > {through}, окно загрузки), только журнал: {json.dumps(tail, ensure_ascii=False)} — "
          "синтетические строки там загрузит исправленный разбор после деплоя или следующий план")
    run_id = RUN_PREFIX + plan["fingerprint"][:16]
    keys = [list(key(r)) for r in plan["rows"]]
    if not a.apply:
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / "repair_plan.json", {**plan["core"], "fingerprint": plan["fingerprint"], "git_head": head,
                                              "ingestion_run_id": run_id, "rows": plan["rows"],
                                              "monthly_sku_delta_itemized": plan["monthly_sku_delta_itemized"],
                                              "rollback_keys": keys, "through": through,
                                              "after_through": tail})
        if plan["rows"]:
            (out / "apply_preview.sql").write_text(apply_sql(plan["rows"], run_id, set(E.FIN_ECON_TYPE_NAMES.values())))
            (out / "rollback_preview.sql").write_text(rollback_sql(keys, run_id))
        print(f"PLAN OK · запросов {bq.queries_issued} · записи не было · план → {out}")
        return 0
    if plan["fingerprint"] != a.plan_fingerprint:
        raise SystemExit(f"отпечаток плана {plan['fingerprint']} ≠ --plan-fingerprint — данные изменились, отказ")
    if not plan["rows"]:
        print("нечего добавлять")
        return 0
    mdir.mkdir(parents=True, exist_ok=True)
    write_json(mdir / "plan.json", {**plan["core"], "fingerprint": plan["fingerprint"], "git_head": head,
                                    "ingestion_run_id": run_id, "rollback_keys": keys,
                                    "written_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    job = run_script(apply_sql(plan["rows"], run_id, set(E.FIN_ECON_TYPE_NAMES.values())), token, plan["fingerprint"][:16])
    got = src.run_rows(run_id)
    diff = readback_diff(got, plan["rows"])
    ok = not diff and len(got) == len(plan["rows"])
    write_json(mdir / "readback.json", {"job_id": job["jobReference"]["jobId"], "rows": len(got), "ok": ok,
                                        "mismatched_keys": [list(map(str, k)) for k in diff]})
    print(f"readback {'MATCH' if ok else 'MISMATCH'} · строк {len(got)} → {mdir}")
    return 0 if ok else 3


def rollback(a) -> int:
    mdir = outside_repo(a.manifest_dir, "--manifest-dir")
    stored = json.loads((mdir / "plan.json").read_text(encoding="utf-8"))
    run_id, keys = stored["ingestion_run_id"], [tuple(k) for k in stored["rollback_keys"]]
    sql = rollback_sql(keys, run_id)
    print(f"ROLLBACK {run_id}: {len(keys)} ключ(ей)")
    if not a.apply:
        print(sql)
        print("ROLLBACK PLAN · записи не было")
        return 0
    require_merged_head()
    token = resolve_token(a.token_env, a.token_command, os.environ)
    job = run_script(sql, token, stored["fingerprint"][:16])
    write_json(mdir / "rollback.json", {"job_id": job["jobReference"]["jobId"]})
    print(f"DONE {job['jobReference']['jobId']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
