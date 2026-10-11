#!/usr/bin/env python3
"""Импорт закрытых доказательств утраты товара на складах WB в wb_ops.WB_INVENTORY_LOSS_* (OWNER DECISION 2026-10-11).

Репозиторий публичный: поштучные доказательства (склад × nm × количество), номера документов WB и данные претензии
лежат только в манифесте ВНЕ Git (--evidence) и после импорта — в закрытых таблицах wb_ops (wb_inventory_loss.tf).
Этот файл содержит правила и проверки, но не данные.

Режимы:
  PLAN (по умолчанию, только чтение через lib.bq_readonly): структура манифеста; ожидаемые владельцем итоги
    (--expect-rows / --expect-units / --expect-writeoff); нет дублей; каждый nm — ровно один SKU WB в wb_raw.REF_SKU_MASTER;
    у каждой позиции ровно один интервал канонической себестоимости (wb_mart.UNITKA_COGS_EFFECTIVE) на дату события;
    количество отчёта = остаток WB склад × nm в снимке на дату отчётов (RAW_WB_STOCKS_T5); каждая компенсация —
    ровно одна строка финотчёта WB с тем же номером документа, датой и суммой, и ни одна выплата «за товары,
    пострадавшие…» финотчёта не осталась без документа; претензия согласована с компенсациями. Печатает итоги и
    sha256-отпечаток (без поштучных данных).
  APPLY (--apply --plan-fingerprint <sha> --manifest-dir <D>): чистое дерево, HEAD = свежий origin/main; таблицы
    существуют и ПУСТЫ; повтор PLAN с тем же отпечатком; одна транзакция BigQuery (INSERT в 3 таблицы, ASSERT числа
    строк, иначе ROLLBACK); перечитывание: содержимое таблиц = манифест, списание по таблицам = ожидаемому.
  ROLLBACK (--rollback --manifest-dir <D> [--apply]): удаление строк ровно этого import_run_id одной транзакцией.

usage:
  python tools/wb_inventory_loss_import.py --project <P> --token-command "gcloud auth print-access-token" \\
      --evidence <manifest.json вне Git> --expect-rows N --expect-units N --expect-writeoff RUB
  … --apply --plan-fingerprint <sha> --manifest-dir <пустой каталог вне Git>
  … --rollback --manifest-dir <D> [--apply]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.bq_readonly import ReadOnlyBigQuery, resolve_token  # noqa: E402

TOOL = "wb_inventory_loss_import/v1"
ROOT = Path(__file__).resolve().parent.parent
PROJECT = "project-fa311fc0-4d87-4781-986"
HOST = "https://www.googleapis.com/bigquery/v2"
T_EVIDENCE = f"`{PROJECT}.wb_ops.WB_INVENTORY_LOSS_EVIDENCE`"
T_COMP = f"`{PROJECT}.wb_ops.WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE`"
T_CLAIM = f"`{PROJECT}.wb_ops.WB_INVENTORY_LOSS_CLAIM`"
TABLES = {"WB_INVENTORY_LOSS_EVIDENCE": T_EVIDENCE, "WB_INVENTORY_LOSS_COMPENSATION_EVIDENCE": T_COMP,
          "WB_INVENTORY_LOSS_CLAIM": T_CLAIM}
REPORT_SNAPSHOT_DATE = "2026-08-26"     # дата снимка остатков, с которым сверены отчёты WB (28–29.08.2026)
COMP_LABEL = "Добровольная выплата за товары, пострадавшие"
ATTRIBUTION = {"BOOKING_MONTH", "DOCUMENT_LINKED"}
FP_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONEY_RE = re.compile(r"^-?\d+(\.\d{1,2})?$")
CENT = Decimal("0.01")


class PlanRefused(Exception):
    pass


def _req(cond: bool, msg: str) -> None:
    if not cond:
        raise PlanRefused(msg)


def money(v) -> Decimal:
    s = str(v)
    _req(bool(MONEY_RE.match(s)), f"сумма {s!r} — не число с копейками")
    return Decimal(s)


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(core: dict) -> str:
    return hashlib.sha256(canonical(core).encode()).hexdigest()


# ───────────────────────────── структура манифеста (без сети) ─────────────────────────────

def load_manifest(path: Path) -> dict:
    p = path.resolve()
    _req(not (p == ROOT or ROOT in p.parents), "манифест доказательств внутри репозитория — публичный Git, отказ")
    return json.loads(p.read_text(encoding="utf-8"))


def normalize(m: dict) -> dict:
    """Строки трёх таблиц в каноническом виде + проверки структуры. Исключение — отказ."""
    _req(m.get("kind") == "wb_inventory_loss_evidence/v1", "kind манифеста ≠ wb_inventory_loss_evidence/v1")
    inc = {}
    for i in m.get("incidents", []):
        for k in ("incident_id", "report_warehouse", "wb_report_ref", "incident_date", "incident_evidence"):
            _req(bool(str(i.get(k, "")).strip()), f"событие без {k}")
        _req(bool(DATE_RE.match(i["incident_date"])), f"{i['incident_id']}: дата события {i['incident_date']!r}")
        _req(i["incident_id"] not in inc, f"дубль события {i['incident_id']}")
        inc[i["incident_id"]] = i
    _req(len({i["report_warehouse"] for i in inc.values()}) == len(inc), "два события на один склад")
    items = []
    for it in m.get("items", []):
        _req(it.get("incident_id") in inc, f"позиция ссылается на неизвестное событие {it.get('incident_id')}")
        _req(isinstance(it.get("nm_id"), int) and it["nm_id"] > 0, f"nm_id {it.get('nm_id')!r}")
        _req(isinstance(it.get("lost_qty"), int) and it["lost_qty"] > 0, f"lost_qty {it.get('lost_qty')!r}")
        e = inc[it["incident_id"]]
        items.append({"incident_id": it["incident_id"], "report_warehouse": e["report_warehouse"],
                      "wb_report_ref": e["wb_report_ref"], "incident_date": e["incident_date"],
                      "incident_evidence": e["incident_evidence"], "nm_id": it["nm_id"], "lost_qty": it["lost_qty"]})
    dup = [k for k, n in Counter((x["incident_id"], x["nm_id"]) for x in items).items() if n > 1]
    _req(not dup, f"дубли позиций событие × nm: {len(dup)}")
    _req({x["incident_id"] for x in items} == set(inc), "есть событие без позиций")
    comps = []
    for c in m.get("compensations", []):
        _req(bool(re.match(r"^\d+$", str(c.get("wb_document", "")))), f"номер документа WB {c.get('wb_document')!r}")
        _req(bool(DATE_RE.match(str(c.get("booking_date", "")))), f"{c['wb_document']}: дата проводки")
        _req(money(c["amount_rub"]) > 0, f"{c['wb_document']}: сумма ≤ 0")
        _req(c.get("attribution_basis") in ATTRIBUTION, f"{c['wb_document']}: attribution_basis {c.get('attribution_basis')!r}")
        linked = c["attribution_basis"] == "DOCUMENT_LINKED"
        am = c.get("attributed_incident_month")
        _req(linked == (am is not None), f"{c['wb_document']}: месяц события задаётся только при DOCUMENT_LINKED")
        if linked:
            _req(bool(DATE_RE.match(am)) and am.endswith("-01"), f"{c['wb_document']}: attributed_incident_month — первое число")
            _req(am in {x["incident_date"][:7] + "-01" for x in items}, f"{c['wb_document']}: в этом месяце нет события")
        _req(bool(str(c.get("evidence", "")).strip()), f"{c['wb_document']}: без доказательства")
        comps.append({"wb_document": str(c["wb_document"]), "booking_date": c["booking_date"],
                      "amount_rub": str(money(c["amount_rub"])), "attribution_basis": c["attribution_basis"],
                      "attributed_incident_month": am, "evidence": c["evidence"]})
    _req(len({c["wb_document"] for c in comps}) == len(comps), "дубль документа компенсации")
    cl = m.get("claim")
    _req(isinstance(cl, dict), "нет блока претензии")
    claim = {"claim_id": str(cl["claim_id"]), "filed_date": cl["filed_date"],
             "claim_documented_goods_cost_rub": str(money(cl["claim_documented_goods_cost_rub"])),
             "compensation_before_claim_rub": str(money(cl["compensation_before_claim_rub"])),
             "claimed_goods_rub": str(money(cl["claimed_goods_rub"])), "claimed_other_rub": str(money(cl["claimed_other_rub"])),
             "status": str(cl["status"]), "evidence": str(cl["evidence"])}
    _req(bool(DATE_RE.match(claim["filed_date"])), "дата претензии")
    before = sum((money(c["amount_rub"]) for c in comps if c["booking_date"] < claim["filed_date"]), Decimal(0))
    _req(money(claim["compensation_before_claim_rub"]) == before,
         f"претензия: выплачено до подачи {claim['compensation_before_claim_rub']} ≠ Σ компенсаций до {claim['filed_date']} ({before})")
    _req(money(claim["claimed_goods_rub"]) == money(claim["claim_documented_goods_cost_rub"]) - before,
         "претензия: заявлено за товар ≠ документальная себестоимость − выплачено до подачи")
    items.sort(key=lambda x: (x["incident_id"], x["nm_id"]))
    comps.sort(key=lambda x: x["wb_document"])
    return {"items": items, "compensations": comps, "claim": claim}


# ───────────────────────────── проверки по живым данным ─────────────────────────────

def check_live(rows: dict, live: dict, expect: dict) -> dict:
    """live: ref {nm: [sku…]}, cogs {(incident_id, nm): [cogs…]}, stock {(warehouse, nm): qty},
    finance [{wb_document, booking_date, amount_rub}] — все выплаты «за товары, пострадавшие…» финотчёта."""
    items = rows["items"]
    _req(len(items) == expect["rows"], f"позиций {len(items)} ≠ ожидаемых {expect['rows']}")
    units = sum(x["lost_qty"] for x in items)
    _req(units == expect["units"], f"единиц {units} ≠ ожидаемых {expect['units']}")
    unknown = sorted({x["nm_id"] for x in items if len(live["ref"].get(x["nm_id"], [])) != 1})
    _req(not unknown, f"nm без ровно одного SKU WB в REF_SKU_MASTER: {len(unknown)}")
    bad = [x for x in items if len(live["cogs"].get((x["incident_id"], x["nm_id"]), [])) != 1]
    _req(not bad, f"позиций без ровно одного интервала себестоимости на дату события: {len(bad)}")
    total = sum((Decimal(str(live["cogs"][(x["incident_id"], x["nm_id"])][0])) * x["lost_qty"] for x in items), Decimal(0))
    by_month: dict[str, Decimal] = {}
    for x in items:
        k = x["incident_date"][:7]
        by_month[k] = by_month.get(k, Decimal(0)) + Decimal(str(live["cogs"][(x["incident_id"], x["nm_id"])][0])) * x["lost_qty"]
    _req(total.quantize(CENT, ROUND_HALF_UP) == expect["writeoff"],
         f"каноническое списание {total.quantize(CENT, ROUND_HALF_UP)} ≠ ожидаемому {expect['writeoff']}")
    stock_diff = sum(abs(x["lost_qty"] - live["stock"].get((x["report_warehouse"], x["nm_id"]), 0)) for x in items)
    extra = sum(q for (w, nm), q in live["stock"].items()
                if w in {x["report_warehouse"] for x in items} and (w, nm) not in {(x["report_warehouse"], x["nm_id"]) for x in items})
    _req(stock_diff == 0 and extra == 0,
         f"отчёт ≠ остатку WB {REPORT_SNAPSHOT_DATE}: расхождение {stock_diff} ед., лишних позиций склада {extra} ед.")
    fin = {(f["wb_document"], f["booking_date"], str(money(f["amount_rub"]))) for f in live["finance"]}
    _req(len(fin) == len(live["finance"]), "в финотчёте дубли выплат за утрату")
    man = {(c["wb_document"], c["booking_date"], c["amount_rub"]) for c in rows["compensations"]}
    _req(man == fin, f"компенсации манифеста ≠ выплатам финотчёта: только в манифесте {len(man - fin)}, только в финотчёте {len(fin - man)}")
    return {"rows": len(items), "units": units, "incidents": len({x["incident_id"] for x in items}),
            "writeoff_rub": str(total.quantize(CENT, ROUND_HALF_UP)),
            "writeoff_by_month": {k: str(v.quantize(CENT, ROUND_HALF_UP)) for k, v in sorted(by_month.items())},
            "compensations": len(man), "compensation_rub": str(sum((money(c[2]) for c in man), Decimal(0))),
            "stock_reconciled": True}


class LiveSource:
    def __init__(self, bq: ReadOnlyBigQuery):
        self.bq = bq

    def fetch(self, rows: dict) -> dict:
        nms = sorted({x["nm_id"] for x in rows["items"]})
        nm_list = ", ".join(str(n) for n in nms)
        ref: dict[int, list] = {}
        for r in self.bq.query(f"SELECT nm_id, ARRAY_AGG(DISTINCT internal_sku) skus FROM `{PROJECT}.wb_raw.REF_SKU_MASTER` "
                               f"WHERE marketplace = 'WB' AND nm_id IN ({nm_list}) GROUP BY 1"):
            ref[int(r["nm_id"])] = list(r["skus"] or [])
        pairs = ", ".join(f"STRUCT('{x['incident_id']}' AS i, {x['nm_id']} AS nm, DATE '{x['incident_date']}' AS d)" for x in rows["items"])
        cogs: dict[tuple, list] = {}
        for r in self.bq.query(
                f"WITH p AS (SELECT * FROM UNNEST([{pairs}])), "
                f"ref AS (SELECT nm_id, ANY_VALUE(internal_sku) sku FROM `{PROJECT}.wb_raw.REF_SKU_MASTER` WHERE marketplace = 'WB' "
                f"GROUP BY 1 HAVING COUNT(DISTINCT internal_sku) = 1) "
                f"SELECT p.i, p.nm, ARRAY_AGG(u.product_cogs_rub IGNORE NULLS) c FROM p LEFT JOIN ref ON ref.nm_id = p.nm "
                f"LEFT JOIN `{PROJECT}.wb_mart.UNITKA_COGS_EFFECTIVE` u ON u.internal_sku = ref.sku "
                f"AND p.d BETWEEN u.effective_from AND IFNULL(u.effective_to, DATE '9999-12-31') GROUP BY 1, 2"):
            cogs[(r["i"], int(r["nm"]))] = [str(v) for v in (r["c"] or [])]
        whs = ", ".join("'" + w.replace("'", "\\'") + "'" for w in sorted({x["report_warehouse"] for x in rows["items"]}))
        stock = {}
        for r in self.bq.query(
                f"SELECT warehouse_name w, nm_id, SUM(quantity) q FROM `{PROJECT}.wb_raw.RAW_WB_STOCKS_T5` "
                f"WHERE row_type = 'WAREHOUSE' AND _snapshot_date = DATE '{REPORT_SNAPSHOT_DATE}' AND warehouse_name IN ({whs}) "
                f"AND snapshot_id = (SELECT MAX(snapshot_id) FROM `{PROJECT}.wb_raw.RAW_WB_STOCKS_T5` WHERE _snapshot_date = DATE '{REPORT_SNAPSHOT_DATE}') "
                f"GROUP BY 1, 2 HAVING q != 0"):
            stock[(r["w"], int(r["nm_id"]))] = int(r["q"])
        finance = [{"wb_document": r["doc"], "booking_date": str(r["d"]), "amount_rub": str((-Decimal(str(r["ded"]))).quantize(CENT))}
                   for r in self.bq.query(
                       f"SELECT REGEXP_EXTRACT(JSON_VALUE(raw_json, '$.bonusTypeName'), r'документ №\\s*(\\d+)') doc, _rr_date d, deduction ded "
                       f"FROM `{PROJECT}.wb_raw.V_WB_FINANCE_CANONICAL` WHERE supplier_oper_name = 'Удержание' "
                       f"AND STARTS_WITH(JSON_VALUE(raw_json, '$.bonusTypeName'), '{COMP_LABEL}')")]
        return {"ref": ref, "cogs": cogs, "stock": stock, "finance": finance}

    def table_rows(self) -> dict:
        def q(sql):
            return self.bq.query(sql)
        items = [{**{k: r[k] for k in ("incident_id", "report_warehouse", "wb_report_ref", "incident_evidence")},
                  "incident_date": str(r["incident_date"]), "nm_id": int(r["nm_id"]), "lost_qty": int(r["lost_qty"]),
                  "_run": r["import_run_id"]} for r in q(f"SELECT * FROM {T_EVIDENCE}")]
        comps = [{"wb_document": r["wb_document"], "booking_date": str(r["booking_date"]), "amount_rub": str(money(Decimal(str(r["amount_rub"])).quantize(CENT))),
                  "attribution_basis": r["attribution_basis"],
                  "attributed_incident_month": str(r["attributed_incident_month"]) if r["attributed_incident_month"] else None,
                  "evidence": r["evidence"], "_run": r["import_run_id"]} for r in q(f"SELECT * FROM {T_COMP}")]
        claims = [{**{k: r[k] for k in ("claim_id", "status", "evidence")}, "filed_date": str(r["filed_date"]),
                   **{k: str(Decimal(str(r[k])).quantize(CENT)) for k in ("claim_documented_goods_cost_rub", "compensation_before_claim_rub",
                                                                         "claimed_goods_rub", "claimed_other_rub")},
                   "_run": r["import_run_id"]} for r in q(f"SELECT * FROM {T_CLAIM}")]
        return {"items": items, "compensations": comps, "claims": claims}


def build_plan(m: dict, src, expect: dict) -> dict:
    rows = normalize(m)
    summary = check_live(rows, src.fetch(rows), expect)
    core = {"tool": TOOL, "manifest_sha256": fingerprint(rows), "expect": {k: str(v) for k, v in expect.items()},
            "summary": summary, "claim_id": rows["claim"]["claim_id"]}
    return {"core": core, "fingerprint": fingerprint(core), "rows": rows}


def transaction_sql(run_id: str, manifest_sha: str, n_items: int, n_comp: int) -> str:
    imp = f"'{run_id}', '{manifest_sha}', CURRENT_TIMESTAMP()"
    return f"""BEGIN TRANSACTION;
ASSERT (SELECT COUNT(*) FROM {T_EVIDENCE}) + (SELECT COUNT(*) FROM {T_COMP}) + (SELECT COUNT(*) FROM {T_CLAIM}) = 0
  AS 'WB_LOSS_IMPORT: таблицы доказательств не пусты';
INSERT INTO {T_EVIDENCE} (incident_id, report_warehouse, wb_report_ref, incident_date, incident_evidence, nm_id, lost_qty,
  import_run_id, manifest_sha256, imported_at)
SELECT JSON_VALUE(x, '$.incident_id'), JSON_VALUE(x, '$.report_warehouse'), JSON_VALUE(x, '$.wb_report_ref'),
  DATE(JSON_VALUE(x, '$.incident_date')), JSON_VALUE(x, '$.incident_evidence'), CAST(JSON_VALUE(x, '$.nm_id') AS INT64),
  CAST(JSON_VALUE(x, '$.lost_qty') AS INT64), {imp}
FROM UNNEST(JSON_QUERY_ARRAY(@items)) x;
INSERT INTO {T_COMP} (wb_document, booking_date, amount_rub, attribution_basis, attributed_incident_month, evidence,
  import_run_id, manifest_sha256, imported_at)
SELECT JSON_VALUE(x, '$.wb_document'), DATE(JSON_VALUE(x, '$.booking_date')), CAST(JSON_VALUE(x, '$.amount_rub') AS NUMERIC),
  JSON_VALUE(x, '$.attribution_basis'), DATE(JSON_VALUE(x, '$.attributed_incident_month')), JSON_VALUE(x, '$.evidence'), {imp}
FROM UNNEST(JSON_QUERY_ARRAY(@compensations)) x;
INSERT INTO {T_CLAIM} (claim_id, filed_date, claim_documented_goods_cost_rub, compensation_before_claim_rub, claimed_goods_rub,
  claimed_other_rub, status, evidence, import_run_id, manifest_sha256, imported_at)
SELECT JSON_VALUE(@claim, '$.claim_id'), DATE(JSON_VALUE(@claim, '$.filed_date')),
  CAST(JSON_VALUE(@claim, '$.claim_documented_goods_cost_rub') AS NUMERIC), CAST(JSON_VALUE(@claim, '$.compensation_before_claim_rub') AS NUMERIC),
  CAST(JSON_VALUE(@claim, '$.claimed_goods_rub') AS NUMERIC), CAST(JSON_VALUE(@claim, '$.claimed_other_rub') AS NUMERIC),
  JSON_VALUE(@claim, '$.status'), JSON_VALUE(@claim, '$.evidence'), {imp};
ASSERT (SELECT COUNT(*) FROM {T_EVIDENCE} WHERE import_run_id = '{run_id}') = {n_items} AS 'WB_LOSS_IMPORT: позиции';
ASSERT (SELECT COUNT(*) FROM {T_COMP} WHERE import_run_id = '{run_id}') = {n_comp} AS 'WB_LOSS_IMPORT: компенсации';
ASSERT (SELECT COUNT(*) FROM {T_CLAIM} WHERE import_run_id = '{run_id}') = 1 AS 'WB_LOSS_IMPORT: претензия';
COMMIT TRANSACTION;"""


def rollback_sql(run_id: str) -> str:
    return "BEGIN TRANSACTION;\n" + "\n".join(f"DELETE FROM {t} WHERE import_run_id = '{run_id}';" for t in TABLES.values()) + \
        "\n" + "\n".join(f"ASSERT (SELECT COUNT(*) FROM {t} WHERE import_run_id = '{run_id}') = 0 AS 'WB_LOSS_ROLLBACK: {n}';"
                         for n, t in TABLES.items()) + "\nCOMMIT TRANSACTION;"


def strip_run(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "_run"} for r in rows]


def verify_tables(src, rows: dict, run_id: str) -> dict:
    t = src.table_rows()
    key_i = lambda x: (x["incident_id"], x["nm_id"])  # noqa: E731
    ok_items = sorted(strip_run(t["items"]), key=key_i) == sorted(rows["items"], key=key_i)
    ok_comp = sorted(strip_run(t["compensations"]), key=lambda x: x["wb_document"]) == rows["compensations"]
    ok_claim = strip_run(t["claims"]) == [rows["claim"]]
    runs = {r["_run"] for part in t.values() for r in part}
    return {"ok": ok_items and ok_comp and ok_claim and runs == {run_id}, "items_match": ok_items,
            "compensations_match": ok_comp, "claim_match": ok_claim, "import_runs": sorted(runs)}


# ───────────────────────────── запись (только APPLY / ROLLBACK --apply) ─────────────────────────────

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


def run_script(sql: str, token: str, label: str, params: dict | None = None) -> dict:
    q = {"query": sql, "useLegacySql": False}
    if params:
        q["parameterMode"] = "NAMED"
        q["queryParameters"] = [{"name": k, "parameterType": {"type": "STRING"}, "parameterValue": {"value": v}}
                                for k, v in params.items()]
    body = {"configuration": {"query": q, "labels": {"purpose": "wb-inventory-loss-import", "plan_fp": label}},
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


def tables_exist(token: str) -> dict:
    out = {}
    for name in TABLES:
        code, t = _call("GET", f"{HOST}/projects/{PROJECT}/datasets/wb_ops/tables/{name}?fields=type,numRows", token)
        out[name] = (code == 200 and t.get("type") == "TABLE", int(t.get("numRows") or 0) if code == 200 else None)
    return out


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


def outside_repo(raw: str | None, what: str) -> Path:
    if not raw:
        raise SystemExit(f"нужен {what}")
    p = Path(raw).resolve()
    if p == ROOT or ROOT in p.parents:
        raise SystemExit(f"{what} внутри репозитория — публичный Git, отказ")
    return p


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--token-command")
    p.add_argument("--token-env", default="BQ_DEPLOY_ACCESS_TOKEN")
    p.add_argument("--evidence", help="манифест доказательств (JSON) вне Git")
    p.add_argument("--expect-rows", type=int)
    p.add_argument("--expect-units", type=int)
    p.add_argument("--expect-writeoff", help="ожидаемое каноническое списание, ₽ с копейками")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--rollback", action="store_true")
    p.add_argument("--plan-fingerprint")
    p.add_argument("--manifest-dir", help="каталог записи манифеста импорта вне Git (пустой для APPLY)")
    a = p.parse_args(argv)
    if a.project != PROJECT:
        raise SystemExit(f"проект {a.project!r} не совпадает с {PROJECT!r}")
    if a.rollback:
        return rollback(a)
    if a.expect_rows is None or a.expect_units is None or a.expect_writeoff is None:
        raise SystemExit("нужны --expect-rows, --expect-units, --expect-writeoff (итоги, утверждённые владельцем)")
    expect = {"rows": a.expect_rows, "units": a.expect_units, "writeoff": money(a.expect_writeoff)}
    evidence = outside_repo(a.evidence, "--evidence")
    if a.apply and not (a.plan_fingerprint and FP_RE.match(a.plan_fingerprint)):
        raise SystemExit("--apply: нужен --plan-fingerprint (sha256 из PLAN)")
    mdir = outside_repo(a.manifest_dir, "--manifest-dir") if a.apply else None
    head = require_merged_head() if a.apply else git("rev-parse", "HEAD")
    if a.apply and mdir.exists() and any(mdir.iterdir()):
        raise SystemExit(f"{mdir}: каталог манифеста импорта не пуст — отказ перезаписи")
    token = resolve_token(a.token_env, a.token_command, os.environ)
    bq = ReadOnlyBigQuery(project=PROJECT, token=token, label_purpose="wb-inventory-loss-import-plan")
    src = LiveSource(bq)
    print(f"git HEAD {head} · режим {'APPLY' if a.apply else 'PLAN'}")
    try:
        plan = build_plan(load_manifest(evidence), src, expect)
    except PlanRefused as e:
        print(f"STOP: {e}")
        return 2
    print(json.dumps(plan["core"], ensure_ascii=False, indent=2, sort_keys=True))
    print(f"FINGERPRINT {plan['fingerprint']}")
    if not a.apply:
        print(f"PLAN OK · запросов {bq.queries_issued} · записи не было")
        return 0
    if plan["fingerprint"] != a.plan_fingerprint:
        raise SystemExit(f"отпечаток плана {plan['fingerprint']} ≠ --plan-fingerprint — данные изменились, отказ")
    state = tables_exist(token)
    if not all(ok for ok, _ in state.values()):
        raise SystemExit(f"таблицы доказательств не созданы (wb_inventory_loss.tf): {state}")
    if any(n for _, n in state.values()):
        raise SystemExit(f"таблицы доказательств не пусты: {state} — повторный импорт запрещён")
    run_id = "wbloss_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + plan["fingerprint"][:8]
    rows = plan["rows"]
    sql = transaction_sql(run_id, plan["core"]["manifest_sha256"], len(rows["items"]), len(rows["compensations"]))
    mdir.mkdir(parents=True, exist_ok=True)
    write_json(mdir / "plan.json", {**plan["core"], "fingerprint": plan["fingerprint"], "git_head": head, "import_run_id": run_id,
                                    "evidence_path": str(evidence), "apply_sql": sql, "rollback_sql": rollback_sql(run_id),
                                    "written_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    params = {"items": json.dumps(rows["items"], ensure_ascii=False), "compensations": json.dumps(rows["compensations"], ensure_ascii=False),
              "claim": json.dumps(rows["claim"], ensure_ascii=False)}
    job = run_script(sql, token, plan["fingerprint"][:16], params)
    jid = job["jobReference"]["jobId"]
    print(f"DONE транзакция {jid} · import_run_id {run_id}")
    rb = verify_tables(src, rows, run_id)
    write_json(mdir / "readback.json", {"job_id": jid, **rb})
    print(f"readback {'MATCH' if rb['ok'] else 'MISMATCH'} {json.dumps(rb, ensure_ascii=False)} → {mdir}")
    return 0 if rb["ok"] else 3


def rollback(a) -> int:
    mdir = outside_repo(a.manifest_dir, "--manifest-dir")
    stored = json.loads((mdir / "plan.json").read_text(encoding="utf-8"))
    run_id = stored["import_run_id"]
    print(f"ROLLBACK import_run_id {run_id}")
    sql = rollback_sql(run_id)
    if not a.apply:
        print(sql)
        print("ROLLBACK PLAN · записи не было")
        return 0
    require_merged_head()
    token = resolve_token(a.token_env, a.token_command, os.environ)
    job = run_script(sql, token, stored["fingerprint"][:16])
    left = tables_exist(token)
    write_json(mdir / "rollback.json", {"job_id": job["jobReference"]["jobId"], "tables_after": {k: v[1] for k, v in left.items()}})
    print(f"DONE {job['jobReference']['jobId']} · строк осталось {json.dumps({k: v[1] for k, v in left.items()})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
