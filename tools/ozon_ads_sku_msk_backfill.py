#!/usr/bin/env python3
"""Ozon D2 — ограниченная догрузка RAW_OZON_ADS_SKU_DAILY полными сутками МСК: план, проверка, запись, откат.

Зачем. До D2 путь EVETIS заказывал отчёт по SKU моментами UTC (d0T00:00Z..d1T00:00Z), а «День» отчёта —
сутки МСК; MERGE оставлял значение последнего прогона. Края чанков бэкфилла и каждые сутки, ставшие d0
суточного прогона (с 2026-08-28), остались без расхода 00:00–02:59 МСК (TECH_DEBT P2-5). Код исправлен
(`entities.msk_day_report_window`), а уже записанные сутки сами не исправятся — их вне окна 7 суток.

Что делает (по шагам, каждый отдельной командой):
  plan      Только чтение. Источник — строки отчёта, полученные каноническим окном суток МСК (формат строк
            runtime: date, campaign_id, sku, attributed_spend_rub, …), против текущих строк RAW. В план
            попадают только затронутые пары «сутки × кампания» (расход хотя бы одного SKU отличается или
            строки нет); в паре — строки, у которых отличается любое поле. По каждой строке — образ ДО
            (для отката) и ПОСЛЕ, по суткам — свод, по плану — sha256-отпечаток. Строка RAW, которой нет в
            источнике, в затронутой паре — отказ (удаление вне полномочий upsert).
  check     Только чтение. SELECT, который считает расхождения живого RAW с образом ДО (должно быть 0).
  apply     Запись. Без --confirm APPLY-<12 знаков отпечатка> — только печать SQL (dry-run). Отпечаток
            файла пересчитывается и обязан совпасть с --fingerprint. Скрипт BigQuery в транзакции:
            ASSERT «RAW = образ ДО» → MERGE по (date, campaign_id, sku) → ASSERT «RAW = образ ПОСЛЕ».
  rollback  Запись, зеркально: ASSERT «RAW = образ ПОСЛЕ» → вернуть образ ДО (UPDATE) и удалить строки,
            которых до записи не было. Без --confirm ROLLBACK-<12 знаков> — только печать SQL.

Идемпотентность: план после применения пуст (simulate_apply + plan, тест), ключ плана уникален; повтор apply
упирается в ASSERT образа ДО и ничего не меняет.

usage:
  python tools/ozon_ads_sku_msk_backfill.py plan --source SRC.json --raw RAW.json --sku-map MAP.json \
      --from 2025-05-05 --to 2026-10-10 --out MANIFEST.json
  python tools/ozon_ads_sku_msk_backfill.py check --manifest MANIFEST.json
  python tools/ozon_ads_sku_msk_backfill.py apply --manifest MANIFEST.json --fingerprint <sha256> \
      [--project P --token-command "gcloud auth print-access-token" --confirm APPLY-<fp12>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

VERSION = "ozon-d2-ads-sku-msk-backfill-v1"
TABLE = "ozon_raw.RAW_OZON_ADS_SKU_DAILY"
KEY = ("date", "campaign_id", "sku")
MONEY = ("attributed_spend_rub", "revenue_promo_rub", "ordered_total_rub")
PCT = ("drr_promo_pct", "drr_total_pct")
COUNTS = ("impressions", "clicks", "cart_adds", "orders")
TEXT = ("attribution_status", "source_endpoint", "source_payload_hash")
COMPARED = MONEY + PCT + COUNTS + ("attribution_status",)
WRITTEN = MONEY + PCT + COUNTS + TEXT + ("extracted_at", "ingestion_run_id")
SOURCE_ENDPOINT = "POST /api/client/statistics (async report)"


class PlanError(RuntimeError):
    """План не может быть построен честно (удаление, дубль ключа, чужие сутки)."""


def _dec(v, q="0.01"):
    if v is None or v == "":
        return None
    return Decimal(str(v)).quantize(Decimal(q), ROUND_HALF_UP)


def _norm(row: dict) -> dict:
    out = {"date": str(row["date"])[:10], "campaign_id": str(row["campaign_id"]), "sku": str(row["sku"])}
    for k in MONEY:
        out[k] = _dec(row.get(k))
    for k in PCT:
        out[k] = _dec(row.get(k), "0.0001")
    for k in COUNTS:
        out[k] = int(row.get(k) or 0)
    for k in TEXT + ("extracted_at", "ingestion_run_id"):
        out[k] = None if row.get(k) is None else str(row[k])
    return out


def _key(r):
    return tuple(r[k] for k in KEY)


def _same(a: dict, b: dict) -> bool:
    return all(a.get(k) == b.get(k) for k in COMPARED)


def source_payload_hash(date, campaign_id, sku) -> str:
    """Как common.h(iso, cid, sku) runtime: ключевой хэш строки RAW."""
    return hashlib.sha256("|".join(map(str, (date, campaign_id, sku))).encode()).hexdigest()[:32]


def _index(rows, what):
    out = {}
    for r in rows:
        n = _norm(r)
        if _key(n) in out:
            raise PlanError(f"{what}: ключ {_key(n)} встречается дважды")
        out[_key(n)] = n
    return out


def _jsonable(v):
    if isinstance(v, Decimal):
        return format(v, "f")
    return v


def fingerprint(manifest: dict) -> str:
    body = {k: manifest[k] for k in ("version", "table", "scope", "rows")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     default=_jsonable).encode()).hexdigest()


def build_plan(source_rows, raw_rows, sku_map, d0: str, d1: str, *, extracted_at: str) -> dict:
    """Чистая функция: источник × RAW → манифест. Никакого ввода-вывода."""
    src = {k: v for k, v in _index(source_rows, "источник").items() if d0 <= k[0] <= d1}
    raw = {k: v for k, v in _index(raw_rows, "RAW").items() if d0 <= k[0] <= d1}
    mapped = defaultdict(set)
    for m in sku_map:
        if m.get("marketplace", "OZON") != "OZON":
            continue
        mapped[str(m["marketplace_sku"])].add(str(m["internal_sku"]))
    pairs = defaultdict(set)
    for k in set(src) | set(raw):
        pairs[k[:2]].add(k)
    affected = []
    for pair, keys in sorted(pairs.items()):
        if any((src[k]["attributed_spend_rub"] if k in src else None) !=
               (raw[k]["attributed_spend_rub"] if k in raw else None) for k in keys):
            affected.append(pair)
    rows = []
    for pair in affected:
        for k in sorted(pairs[pair]):
            s, r = src.get(k), raw.get(k)
            if s is None:
                raise PlanError(f"строка RAW {k} без строки источника в затронутой паре — нужно удаление")
            if r is not None and _same(s, r):
                continue
            after = {c: s[c] for c in MONEY + PCT + COUNTS}
            after.update(attribution_status=s["attribution_status"] or "ATTRIBUTED_ACTUAL",
                         source_endpoint=SOURCE_ENDPOINT, source_payload_hash=source_payload_hash(*k),
                         extracted_at=extracted_at)
            before = None if r is None else {c: r[c] for c in WRITTEN}
            rows.append({"date": k[0], "campaign_id": k[1], "sku": k[2], "before": before, "after": after})
    content = hashlib.sha256(json.dumps(rows, sort_keys=True, default=_jsonable).encode()).hexdigest()
    run_id = f"d2-msk-backfill-{content[:12]}"
    for x in rows:
        x["after"]["ingestion_run_id"] = run_id
    # свод по суткам: всё RAW/источник за сутки и только изменения плана
    day = defaultdict(lambda: {"raw_attributed_rub": Decimal(0), "source_attributed_rub": Decimal(0),
                               "delta_rub": Decimal(0), "delta_mapped_rub": Decimal(0),
                               "delta_unmapped_rub": Decimal(0), "pairs": set(), "skus": set(), "rows": 0})
    for k, v in raw.items():
        day[k[0]]["raw_attributed_rub"] += v["attributed_spend_rub"] or 0
    for k, v in src.items():
        day[k[0]]["source_attributed_rub"] += v["attributed_spend_rub"] or 0
    for x in rows:
        d = day[x["date"]]
        delta = (x["after"]["attributed_spend_rub"] or 0) - ((x["before"] or {}).get("attributed_spend_rub") or 0)
        d["delta_rub"] += delta
        d["delta_mapped_rub" if len(mapped.get(x["sku"], ())) == 1 else "delta_unmapped_rub"] += delta
        d["pairs"].add(x["campaign_id"])
        if delta:
            d["skus"].add(x["sku"])
        d["rows"] += 1
    days = [{"date": k, **{c: v[c] for c in ("raw_attributed_rub", "source_attributed_rub", "delta_rub",
                                              "delta_mapped_rub", "delta_unmapped_rub")},
             "affected_campaigns": len(v["pairs"]), "affected_skus": len(v["skus"]), "rows": v["rows"]}
            for k, v in sorted(day.items()) if v["rows"]]
    manifest = {"version": VERSION, "table": TABLE, "scope": {"from": d0, "to": d1, "key": list(KEY)},
                "rows": rows, "days": days,
                "totals": {"rows": len(rows), "inserts": sum(1 for x in rows if x["before"] is None),
                           "updates": sum(1 for x in rows if x["before"] is not None),
                           "affected_pairs": len(affected),
                           "delta_rub": sum((d["delta_rub"] for d in days), Decimal(0)),
                           "delta_unmapped_rub": sum((d["delta_unmapped_rub"] for d in days), Decimal(0))}}
    keys = [_key(x) for x in rows]
    assert len(keys) == len(set(keys)), "ключ плана не уникален"
    manifest["fingerprint"] = fingerprint(json.loads(json.dumps(manifest, default=_jsonable)))
    return manifest


def simulate_apply(raw_rows, manifest) -> list[dict]:
    """RAW после записи плана (в памяти) — для доказательства идемпотентности и сверок с кандидатом."""
    out = {_key(_norm(r)): dict(r) for r in raw_rows}
    for x in manifest["rows"]:
        k = (x["date"], x["campaign_id"], x["sku"])
        out[k] = {"date": k[0], "campaign_id": k[1], "sku": k[2], **x["after"]}
    return list(out.values())


# ─────────────────────────────────────────────────────────────── SQL
def _lit(v, kind):
    if v is None:
        return f"CAST(NULL AS {kind})"
    if kind == "NUMERIC":
        return f"NUMERIC '{Decimal(str(v))}'"
    if kind == "INT64":
        return str(int(v))
    if kind == "DATE":
        return f"DATE '{_safe(v)}'"
    if kind == "TIMESTAMP":
        return f"TIMESTAMP '{_safe(v)}'"
    return f"'{_safe(v)}'"


def _safe(s):
    s = str(s)
    if any(c in s for c in "'\\\n\r`"):
        raise PlanError(f"недопустимый символ в значении {s!r}")
    return s


TYPES = {**{c: "NUMERIC" for c in MONEY + PCT}, **{c: "INT64" for c in COUNTS},
         **{c: "STRING" for c in TEXT + ("ingestion_run_id",)}, "extracted_at": "TIMESTAMP"}


def _rows_cte(manifest, image):
    out = []
    for x in manifest["rows"]:
        img = x[image]
        cols = [f"{_lit(x['date'], 'DATE')} AS date", f"{_lit(x['campaign_id'], 'STRING')} AS campaign_id",
                f"{_lit(x['sku'], 'STRING')} AS sku", f"{'TRUE' if img is not None else 'FALSE'} AS present"]
        cols += [f"{_lit((img or {}).get(c), TYPES[c])} AS {c}" for c in WRITTEN]
        out.append("STRUCT(" + ", ".join(cols) + ")")
    return "SELECT * FROM UNNEST([\n  " + ",\n  ".join(out) + "])"


def _mismatch_sql(project, cte_name):
    t = f"`{project}.{TABLE}`"
    diff = " OR ".join(f"t.{c} IS DISTINCT FROM r.{c}" for c in ("attributed_spend_rub", "ingestion_run_id",
                                                                   "extracted_at"))
    return (f"(SELECT COUNT(*) FROM {cte_name} r LEFT JOIN {t} t USING (date, campaign_id, sku) "
            f"WHERE (r.present AND (t.sku IS NULL OR {diff})) OR (NOT r.present AND t.sku IS NOT NULL))")


def check_sql(manifest, project) -> str:
    """Только чтение: число ключей, где живой RAW ≠ образ ДО (ожидается 0)."""
    return (f"WITH d2_before AS ({_rows_cte(manifest, 'before')})\n"
            f"SELECT {_mismatch_sql(project, 'd2_before')} AS mismatches, {len(manifest['rows'])} AS planned_rows")


def apply_sql(manifest, project, *, rollback=False) -> str:
    t = f"`{project}.{TABLE}`"
    pre, post = ("after", "before") if rollback else ("before", "after")
    n = len(manifest["rows"])
    setter = ", ".join(f"{c} = s.{c}" for c in WRITTEN)
    cols = ", ".join(KEY + WRITTEN)
    vals = ", ".join(f"s.{c}" for c in KEY + WRITTEN)
    tag = "откат D2" if rollback else "догрузка D2"
    return "\n".join([
        f"-- {tag}: {VERSION}, отпечаток {manifest['fingerprint']}, строк {n}",
        f"CREATE TEMP TABLE d2_pre AS {_rows_cte(manifest, pre)};",
        f"CREATE TEMP TABLE d2_post AS {_rows_cte(manifest, post)};",
        "BEGIN TRANSACTION;",
        f"ASSERT {_mismatch_sql(project, 'd2_pre')} = 0 AS '{tag}: RAW не совпал с ожидаемым образом — перестроить план';",
        f"MERGE {t} t USING (SELECT * FROM d2_post WHERE present) s "
        f"ON t.date = s.date AND t.campaign_id = s.campaign_id AND t.sku = s.sku "
        f"WHEN MATCHED THEN UPDATE SET {setter} "
        f"WHEN NOT MATCHED THEN INSERT ({cols}) VALUES ({vals});",
        f"DELETE FROM {t} t WHERE EXISTS (SELECT 1 FROM d2_post s WHERE NOT s.present "
        f"AND t.date = s.date AND t.campaign_id = s.campaign_id AND t.sku = s.sku);",
        f"ASSERT {_mismatch_sql(project, 'd2_post')} = 0 AS '{tag}: после записи RAW не равен образу';",
        "COMMIT TRANSACTION;",
    ])


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _verified(manifest, fp):
    actual = fingerprint(manifest)
    if actual != manifest.get("fingerprint") or actual != fp:
        raise SystemExit(f"отпечаток не совпал: файл {actual}, ожидался {fp}")
    return actual


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("plan")
    a.add_argument("--source", required=True)
    a.add_argument("--raw", required=True)
    a.add_argument("--sku-map", required=True)
    a.add_argument("--from", dest="d0", required=True)
    a.add_argument("--to", dest="d1", required=True)
    a.add_argument("--extracted-at", required=True, help="момент получения источника, UTC ISO")
    a.add_argument("--out", required=True)
    for name in ("check", "apply", "rollback"):
        b = sub.add_parser(name)
        b.add_argument("--manifest", required=True)
        b.add_argument("--project", default="project-fa311fc0-4d87-4781-986")
        if name != "check":
            b.add_argument("--fingerprint", required=True)
            b.add_argument("--confirm")
            b.add_argument("--token-command")
    args = p.parse_args(argv)
    if args.cmd == "plan":
        m = build_plan(_load(args.source), _load(args.raw), _load(args.sku_map), args.d0, args.d1,
                       extracted_at=args.extracted_at)
        Path(args.out).write_text(json.dumps(m, ensure_ascii=False, indent=1, default=_jsonable), encoding="utf-8")
        print(json.dumps({"fingerprint": m["fingerprint"], **m["totals"]}, default=_jsonable, ensure_ascii=False))
        return 0
    m = _load(args.manifest)
    if args.cmd == "check":
        print(check_sql(m, args.project))
        return 0
    fp = _verified(m, args.fingerprint)
    sql = apply_sql(m, args.project, rollback=args.cmd == "rollback")
    need = f"{'ROLLBACK' if args.cmd == 'rollback' else 'APPLY'}-{fp[:12]}"
    if args.confirm != need:
        print(sql)
        print(f"-- dry-run: запись не выполнена; для записи нужен --confirm {need}", file=sys.stderr)
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import os
    from lib.bq_readonly import resolve_token
    from spp_daily_view_deploy import run_job
    token = resolve_token("BQ_DEPLOY_ACCESS_TOKEN", args.token_command, os.environ)
    run_job(args.project, sql, token, dry=False)
    print(f"записано: {len(m['rows'])} строк, отпечаток {fp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
