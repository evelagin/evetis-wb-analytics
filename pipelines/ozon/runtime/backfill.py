"""Bounded tenant backfill. Uses reviewed public transports and existing RAW MERGE.

Runtime writes only its own RAW journal. Owner/control publishes tenant_ops coverage.
A progress execution may exit normally without claiming full plan completion.
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from datetime import date, datetime, timedelta

import backfill_core as B
import common as C
import entities as E
import quota as Q
from google.cloud import bigquery


def export_budget():
    """Conservative existing T5 quota; own durable intents count even after ambiguous POST.

    Exclusive account export slot remains an owner/T5 lease prerequisite. Unknown
    ordinary exports in the trailing day cannot be assumed zero. No quota reset.
    """
    table = f"{C.PROJECT}.{C.DATASET}.{C.RUNS_TABLE}"
    config = bigquery.QueryJobConfig(use_legacy_sql=False, maximum_bytes_billed=1073741824)
    rows = list(C.bq().query(f"""SELECT
      (SELECT COUNT(*) FROM `{table}` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
       AND entity = 'ads_sku_daily' AND backfill_plan_id IS NULL) AS unknown_runs,
      COALESCE(SUM(exports), 0) AS used FROM (
       SELECT backfill_plan_id, backfill_sequence,
         MAX(SAFE_CAST(JSON_VALUE(backfill_detail_json, '$.exports_reserved') AS INT64)) AS exports
       FROM `{table}` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
        AND entity = 'ads_sku_daily' AND backfill_sequence IS NOT NULL AND status = 'OK'
       GROUP BY backfill_plan_id, backfill_sequence)""", job_config=config, location=C.LOCATION).result())
    if len(rows) != 1 or rows[0]["unknown_runs"]:
        raise B.EvidenceError("PERFORMANCE_QUOTA_UNPROVEN: ordinary exports not accounted")
    used = rows[0]["used"]
    if type(used) is not int or used < 0:
        raise B.EvidenceError("invalid export reservation accounting")
    return max(Q.budget(None, [], C.now_msk())["usable"] - used, 0)


class Engine:
    def __init__(self, p, run_id, ts, state, request_budget=400, unit_budget=20):
        self.p, self.run_id, self.ts = p, run_id, ts
        self.state = B.validate(p, copy.deepcopy(state))
        self.budget, self.units = request_budget, unit_budget
        self.requests = self.pages = self.order_batches = 0
        self.max_order_batches = 1
        self.units_remaining = unit_budget
        self.deadline = time.monotonic() + 900
        self.types = None
        self.export_allowance = None
        self.exports_reserved = 0

    def call(self, path, body=None, *, performance=False, text=True):
        if self.requests >= self.budget or time.monotonic() >= self.deadline:
            raise B.BudgetReached("bounded execution budget reached")
        self.requests += 1
        if performance:
            code, value = C.perf_get(path, raw_text=text) if body is None else C.perf_post(path, body)
        else:
            code, value = C.seller_post(path, body)
        if code != 200:
            # Never emit API bodies or identifiers in failure diagnostics.
            raise B.EvidenceError(f"SOURCE_HTTP_{code}: {path.split('?')[0]}")
        return value

    def merge(self, table, rows, keys, *, finance=False):
        return C.merge_rows(table, rows, keys, self.run_id,
                            on_duplicate_key="reject" if finance else "collapse_identical", use_dml_stats=True)

    def fbo(self, s):
        pr = s["progress"]
        window = pr["pending"][0]
        a, b = window
        if b - a > self.p["window_days"] * 86400000:
            left, right = B.split(window, self.p["minimum_ms"])
            pr["pending"][:1] = [left, right]
            return {}, {"action": "SPLIT_CONFIGURED_WINDOW"}
        rows, cursor, seen, postings = [], "", set(), 0
        cap = self.p["fbo_page_cap"]
        for _ in range(cap):
            d = self.call("/v3/posting/fbo/list", {"cursor": cursor,
                          "filter": {"since": B.stamp(a), "to": B.stamp(b - 1)},
                          "limit": 100, "with": {"analytics_data": True, "financial_data": True}})
            page = d.get("postings")
            if not isinstance(page, list) or len(page) > 100:
                raise B.EvidenceError("FBO source page shape invalid")
            self.pages += 1
            for item in page:
                try:
                    dt = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                    if dt.tzinfo is None or not a <= int(dt.timestamp() * 1000) < b:
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    raise B.EvidenceError("FBO record outside requested UTC interval") from None
                if not item.get("products") or any(product.get("sku") in (None,"",0,"0") for product in item["products"]):
                    raise B.EvidenceError("FBO posting without stable product grain")
            rows.extend(E._fbo_rows(page, self.run_id, self.ts))
            postings += len(page)
            terminal, nxt = B.source_terminal(d, "cursor", seen, require_flag=True)
            if terminal:
                out = self.merge("RAW_OZON_POSTINGS_FBO", rows, ["posting_number", "sku"])
                pr["pending"].pop(0)
                pr["completed_to"] = b
                s["complete"] = not pr["pending"]
                day_counts = {}
                for row in rows:
                    day = str(datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).astimezone(B.MSK).date())
                    day_counts.setdefault(day, set()).add((row["posting_number"], row["sku"]))
                return out, {"action": "WINDOW_COMPLETE", "by_moscow_day": {d: len(k) for d, k in day_counts.items()}, "utc_from": B.stamp(a),
                             "utc_until_exclusive": B.stamp(b), "postings": postings,
                             "product_rows": len(rows), "source_terminal": True}
            if not page:
                raise B.EvidenceError("FBO nonterminal empty page")
            cursor = nxt
        # No source partial rows were written. Persist the split, then resume smaller leaves.
        left, right = B.split(window, self.p["minimum_ms"])
        pr["pending"][:1] = [left, right]
        return {}, {"action": "SPLIT_PAGE_CAP", "discarded_parent_rows": len(rows),
                    "source_terminal": False, "utc_from": B.stamp(a), "utc_until_exclusive": B.stamp(b)}

    def finance(self, s):
        day = s["progress"]["next_day"]
        if self.types is None:
            payload = self.call("/v1/finance/accrual/types", {})
            values = payload.get("accrual_types")
            if not isinstance(values, list):
                raise B.EvidenceError("finance taxonomy shape invalid")
            self.types = {}
            for x in values:
                key, label = str(x["id"]), x.get("description")
                if not isinstance(label, str) or not label.strip():
                    continue # Missing source label remains UNKNOWN in RAW/DQ.
                if key in self.types and self.types[key] != label:
                    raise B.EvidenceError("FINANCE_TAXONOMY_CONFLICT")
                self.types[key] = label
                if re.fullmatch(r"-?[0-9]{1,19}", key):
                    self.types[int(key)] = label
        rows, cursor, seen = [], "", set()
        for _ in range(E.FINANCE_ACCRUAL_MAX_PAGES_PER_DAY):
            d = self.call("/v1/finance/accrual/by-day", {"date": day, "last_id": cursor})
            chunk = d.get("accruals")
            if not isinstance(chunk, list):
                raise B.EvidenceError("finance source page shape invalid")
            self.pages += 1
            rows.extend(E._finance_rows(chunk, self.types, day, self.run_id, self.ts))
            terminal, nxt = B.source_terminal(d, "last_id", seen)
            if terminal:
                if any(row["sku"] in ("None", "", "0") for row in rows):
                    raise B.EvidenceError("finance source SKU malformed; never substitute identity")
                out = self.merge("RAW_OZON_FINANCE_ACCRUAL", rows,
                                 ["accrual_id", "type_id", "sku"], finance=True)
                s["progress"]["next_day"] = str(date.fromisoformat(day) + timedelta(days=1))
                s["complete"] = s["progress"]["next_day"] > self.p["to"]
                unknown = sorted({int(r["type_id"]) for r in rows if r["operation_name"] == "UNKNOWN"})
                return out, {"action": "DAY_COMPLETE", "day": day, "source_terminal": True,
                             "unknown_type_ids": unknown, "type84_in_source_taxonomy": 84 in self.types,
                             "type84_source_label": C.safe_error_text(self.types[84], 256) if 84 in self.types else None,
                             "expected_unique_rows": len(rows), "economic_finality": "UNPROVEN", "refresh_generation": self.p["generation"]}
            if not chunk:
                raise B.EvidenceError("finance nonterminal empty page")
            cursor = nxt
        raise B.EvidenceError("FINANCE_DAY_PAGE_CAP: no complete source traversal, no partial MERGE")

    def catalog(self, s):
        pr = s["progress"]
        snapshot_date = str(C.now_msk().date())
        if pr.get("snapshot_date", snapshot_date) != snapshot_date:
            raise B.EvidenceError("catalog snapshot date changed; fresh generation required")
        pr["snapshot_date"] = snapshot_date
        visibility = pr.get("visibility", "ALL")
        d = self.call("/v3/product/list", {"filter": {"visibility": visibility},
                      "last_id": pr.get("cursor", ""), "limit": 1000}).get("result") or {}
        self.pages += 1
        total = E._list_total(d)
        items = d.get("items")
        if total is None or not isinstance(items, list):
            raise B.EvidenceError("catalog completeness missing total/items")
        if "total" in pr and total != pr["total"]:
            raise B.EvidenceError("catalog source changed during traversal; fresh generation required")
        ids = [i["product_id"] for i in items]
        previous = pr.get("seen", [])
        if len(set(ids)) != len(ids) or set(ids) & set(previous):
            raise B.EvidenceError("catalog repeated product IDs")
        count = len(previous) + len(ids)
        if count > total:
            raise B.EvidenceError("catalog count exceeds source total")
        info = []
        if ids:
            info = self.call("/v3/product/info/list", {"product_id": ids, "offer_id": [], "sku": []}).get("items") or []
            if len(info) != len(ids) or {i.get("id") for i in info} != set(ids):
                raise B.EvidenceError("catalog detail IDs incomplete")
            if any(i.get("sku") in (None, "", 0, "0") for i in info):
                raise B.EvidenceError("catalog product lacks stable SKU; identity universe incomplete")
        sku_owner = dict(pr.get("sku_owner", {}))
        for item in info:
            sku = str(item["sku"])
            if sku in sku_owner:
                raise B.EvidenceError("catalog SKU repeated across pages/visibilities; source changed")
            sku_owner[sku] = item["id"]
        pr["sku_owner"] = sku_owner
        terminal = count == total
        evidence = {"action": "CATALOG_PAGE", "visibility": visibility, "products": len(ids),
                    "source_total": total, "source_terminal": terminal, "historical_status": "UNPROVEN"}
        if terminal:
            if visibility == "ALL":
                pr.clear(); pr.update({"visibility": "ARCHIVED", "done": False, "all_count": count,
                                      "snapshot_date": snapshot_date, "sku_owner": sku_owner})
            else:
                pr["done"] = s["complete"] = True
                evidence["all_count"] = pr.get("all_count", 0)
                evidence["archived_count"] = count
        else:
            nxt = d.get("last_id")
            seen = set(pr.get("cursors", []))
            if not ids or not isinstance(nxt, str) or not nxt or nxt in seen:
                raise B.EvidenceError("catalog nonprogress cursor")
            if len(seen) >= E.PRODUCT_LIST_MAX_PAGES - 1:
                raise B.EvidenceError("catalog page cap")
            seen.add(nxt)
            pr.update(cursor=nxt, cursors=sorted(seen), seen=previous + ids, total=total)
        # Reject oversized continuation before this source unit can write RAW.
        B.validate(self.p, s, reserve=128)
        out = self.merge("RAW_OZON_CATALOG", E._catalog_rows(info, self.run_id, self.ts), ["snapshot_date", "sku"])
        return out, evidence

    def supplies(self, s):
        pr = s["progress"]
        if pr["bundle"] is not None:
            b = pr["bundle"]
            bid, oid, sid = b["link"]
            d = self.call("/v1/supply-order/bundle", {"bundle_ids": [bid], "limit": 100, "last_id": b["cursor"]})
            self.pages += 1
            items = d.get("items")
            total = d.get("total_count")
            if not isinstance(items, list) or type(total) is not int or total < 0:
                raise B.EvidenceError("bundle page shape/total missing")
            if b.get("total", total) != total:
                raise B.EvidenceError("bundle source changed during traversal")
            terminal, nxt = B.source_terminal(d, "last_id", set(b["cursors"]), require_flag=True)
            if any(item.get("sku") in (None, "", 0, "0") for item in items):
                raise B.EvidenceError("bundle content lacks stable SKU")
            brows = E._bundle_rows(items, bid, oid, sid, self.run_id, self.ts)
            seen = dict(b["seen"])
            for row in brows:
                key = str(row["sku"])
                business = {k: v for k, v in row.items() if k not in {"ingestion_run_id", "loaded_at", "load_ts", "source_payload_hash"}}
                # Mapper _meta carries ingested_at; exclude all observation metadata explicitly.
                for k in E._meta("", "", ""):
                    business.pop(k, None)
                fp = B.digest(business)
                if key in seen:
                    raise B.EvidenceError("bundle repeated SKU across pages; grain completeness unproven")
                seen[key] = fp
            count = b["count"] + len(items)
            if count > total or (terminal and count != total) or (not terminal and not items):
                raise B.EvidenceError("bundle terminal/count contradiction")
            if not terminal and b["pages"] + 1 >= self.p["bundle_page_cap"]:
                raise B.EvidenceError("bundle page cap; partial batch remains incomplete")
            if terminal:
                pr["bundle"] = None
                s["bundles"] += 1
            else:
                b.update(cursor=nxt, cursors=b["cursors"] + [nxt], seen=seen,
                         count=count, pages=b["pages"] + 1, total=total)
            s["complete"] = pr["list_done"] and not pr["pending"] and pr["bundle"] is None
            B.validate(self.p, s, reserve=128)
            out = self.merge("RAW_OZON_SUPPLY_BUNDLES", brows, ["bundle_id", "sku"])
            return out, {"action": "BUNDLE_PAGE", "bundle_complete": terminal,
                         "source_total": total, "source_items": count}
        if pr["pending"]:
            link = pr["pending"].pop(0)
            pr["bundle"] = {"link": link, "cursor": "", "cursors": [], "seen": {}, "count": 0, "pages": 0}
            return {}, {"action": "BUNDLE_STARTED"}
        if self.order_batches >= self.max_order_batches:
            raise B.BudgetReached("bounded supply order batch budget reached")
        self.order_batches += 1
        d = self.call("/v3/supply-order/list", {"filter": {"states": E.CPC_STATES},
                      "limit": self.p["order_batch"], "sort_by": "ORDER_CREATION",
                      "sort_dir": "DESC", "last_id": pr["cursor"]})
        self.pages += 1
        ids = d.get("order_ids")
        if not isinstance(ids, list) or len(ids) > self.p["order_batch"]:
            raise B.EvidenceError("supply order page shape invalid")
        if len(set(ids)) != len(ids) or set(ids) & set(pr["seen_orders"]):
            raise B.EvidenceError("supply repeated order IDs/source changed")
        terminal, nxt = B.source_terminal(d, "last_id", set(pr["cursors"]))
        if not ids and not terminal:
            raise B.EvidenceError("supply nonterminal empty page")
        orders = self.call("/v3/supply-order/get", {"order_ids": ids}).get("orders") or [] if ids else []
        if len(orders) != len(ids) or {o.get("order_id") for o in orders} != set(ids):
            raise B.EvidenceError("supply order detail set incomplete")
        o_rows, s_rows = E._supply_rows(orders, self.run_id, self.ts)
        for row in s_rows:
            bid = row.get("bundle_id")
            if bid:
                link = [bid, row["order_id"], row["supply_id"]]
                old = pr["bundle_links"].get(str(bid))
                if old is not None and old != link:
                    raise B.EvidenceError("bundle natural key has ambiguous supply ownership")
                if old is None:
                    pr["pending"].append(link)
                    pr["bundle_links"][str(bid)] = link
        missing = sum(not row.get("bundle_id") for row in s_rows)
        pr["missing_bundle_links"] = pr.get("missing_bundle_links", 0) + missing
        pr.update(cursor=nxt, list_done=terminal, seen_orders=pr["seen_orders"] + ids,
                  cursors=pr["cursors"] + ([nxt] if not terminal else []))
        s["orders"] += len(o_rows); s["supplies"] += len(s_rows)
        s["complete"] = terminal and not pr["pending"]
        B.validate(self.p, s, reserve=128)
        r1 = self.merge("RAW_OZON_SUPPLY_ORDERS", o_rows, ["order_id"])
        r2 = self.merge("RAW_OZON_SUPPLIES", s_rows, ["order_id", "supply_id"])
        out = {k: r1.get(k, 0) + r2.get(k, 0) for k in ("received", "inserted", "updated")}
        return out, {"action": "ORDER_BATCH", "orders": len(o_rows), "supplies": len(s_rows),
                     "bundles_pending": len(pr["pending"]), "source_terminal": terminal,
                     "content_unavailable_supplies": missing}

    def campaigns(self):
        items, seen, total = [], set(), None
        for page in range(1, 101):
            d = json.loads(self.call(f"/api/client/campaign?page={page}&pageSize=100", performance=True))
            values = d.get("list")
            try:
                n = int(d["total"])
            except (KeyError, ValueError, TypeError):
                raise B.EvidenceError("campaign total missing") from None
            if not isinstance(values, list) or n < 0 or (total is not None and n != total):
                raise B.EvidenceError("campaign source shape/total changed")
            total = n; self.pages += 1
            ids = [str(c["id"]) for c in values]
            if len(set(ids)) != len(ids) or set(ids) & seen or len(items) + len(values) > total:
                raise B.EvidenceError("campaign repeated IDs/count contradiction")
            seen.update(ids); items.extend(values)
            if len(items) == total:
                return items
            if not values:
                raise B.EvidenceError("campaign nonterminal empty page")
        raise B.EvidenceError("campaign page cap")

    def campaign_snapshot(self, s):
        items = self.campaigns()
        out = self.merge("RAW_OZON_ADS_CAMPAIGNS", E._campaign_rows(items, self.run_id, self.ts),
                         ["snapshot_date", "campaign_id"])
        s["progress"]["done"] = s["complete"] = True
        return out, {"action": "CAMPAIGNS_COMPLETE", "campaigns": len(items), "source_terminal": True}

    def expense(self, s):
        day = s["progress"]["next_day"]
        txt = self.call(f"/api/client/statistics/expense?dateFrom={day}&dateTo={day}", performance=True)
        daily = self.call(f"/api/client/statistics/daily?dateFrom={day}&dateTo={day}", performance=True)
        header = (txt.lstrip("\ufeff").splitlines() or [""])[0].split(";")
        if any(k not in header for k in E.EXPENSE_CSV_REQUIRED):
            raise B.EvidenceError("expense CSV source schema unproven")
        stats = {r["ID"]: r for r in E._csv_rows(daily) if r.get("ID")}
        rows = []
        for r in E._csv_rows(txt):
            if not r.get("ID"):
                continue
            x = stats.get(r["ID"])
            if x is None or E._rub(r.get("Расход")) is None:
                raise B.EvidenceError("expense/daily campaign coverage mismatch")
            rows.append(dict(date=day, campaign_id=r["ID"], campaign_title=r.get("Название"),
                expense_rub=E._rub(r.get("Расход")), bonus_expense_rub=E._rub(r.get("Расход бонусов")),
                subscription_expense_rub=E._rub(r.get("Расход с абонентского счета")),
                impressions=int(E._rub(x.get("Показы"))), clicks=int(E._rub(x.get("Клики"))),
                orders=int(E._rub(x.get("Заказы, шт."))), revenue_rub=E._rub(x.get("Заказы, ₽")),
                source_payload_hash=E.h(day, r["ID"]),
                **E._meta("GET /api/client/statistics/expense + /daily", self.run_id, self.ts)))
        out = self.merge("RAW_OZON_ADS_EXPENSE_DAILY", rows, ["date", "campaign_id"])
        self.advance_day(s)
        return out, {"action": "DAY_COMPLETE", "day": day, "source_terminal": True,
                     "expected_unique_rows": len({r["campaign_id"] for r in rows}),
                     "retention_completeness": "UNPROVEN_EMPTY" if not rows else "OBSERVED"}

    def advance_day(self, s):
        day = s["progress"]["next_day"]
        s["progress"] = {"next_day": str(date.fromisoformat(day) + timedelta(days=1))}
        s["complete"] = s["progress"]["next_day"] > self.p["to"]

    def sku(self, s):
        pr = s["progress"]; day = pr["next_day"]
        if "pending" not in pr:
            types = {str(c["id"]): c.get("advObjectType") for c in self.campaigns()}
            txt = self.call(f"/api/client/statistics/expense?dateFrom={day}&dateTo={day}", performance=True)
            header = (txt.lstrip("\ufeff").splitlines() or [""])[0].split(";")
            if any(k not in header for k in E.EXPENSE_CSV_REQUIRED):
                raise B.EvidenceError("SKU expense schema invalid")
            need, skipped = set(), set()
            for r in E._csv_rows(txt):
                if not r.get("ID"):
                    continue
                spend = E._rub(r.get("Расход")); cid = r["ID"]
                if spend is None:
                    raise B.EvidenceError("SKU expense not numeric")
                if not spend:
                    continue
                kind = types.get(cid)
                if kind in E.SKU_REPORT_ADV_TYPES:
                    need.add(cid)
                elif kind in E.NO_SKU_REPORT_ADV_TYPES:
                    skipped.add(cid)
                else:
                    raise B.EvidenceError("SKU active campaign outside supported cohort")
            pr.update(pending=sorted(need), skipped=len(skipped), report=None)
            if not need:
                self.advance_day(s)
            return {}, {"action": "SKU_COHORT", "day": day, "required_campaigns": len(need),
                        "not_applicable_campaigns": len(skipped), "source_terminal": not need,
                        "retention_completeness": "UNPROVEN_EMPTY" if not need else "OBSERVED"}
        report = pr["report"]
        if report is None:
            if self.export_allowance is None:
                self.export_allowance = export_budget()
            remaining = self.export_allowance - self.exports_reserved
            if remaining <= 0 or self.units_remaining < 2 or self.requests >= self.budget or time.monotonic() + 30 >= self.deadline:
                raise B.BudgetReached("performance quota/execution budget deferred before async intent")
            batch = pr["pending"][:min(10, remaining)]
            self.exports_reserved += len(batch)
            pr["report"] = {"phase": "INTENT", "batch": batch, "execution": self.run_id}
            return {}, {"action": "REPORT_INTENT", "exports_reserved": len(batch),
                        "quota_basis": "existing_T5_conservative_floor", "day": day}
        if report["phase"] == "INTENT":
            if report["execution"] != self.run_id:
                raise B.EvidenceError("REPORT_SUBMISSION_AMBIGUOUS: do not create duplicate async report")
            d = self.call("/api/client/statistics", {"campaigns": report["batch"], "dateFrom": day,
                          "dateTo": day, "groupBy": "DATE"}, performance=True)
            uuid = d.get("UUID")
            if not isinstance(uuid, str) or not re.fullmatch(r"[0-9A-Za-z-]{1,64}", uuid):
                raise B.EvidenceError("report UUID missing/unsafe")
            report.update(phase="POLL", uuid=uuid)
            return {}, {"action": "REPORT_SUBMITTED"}
        d = self.call(f"/api/client/statistics/{report['uuid']}", performance=True, text=False)
        if d.get("state") == "ERROR":
            raise B.EvidenceError("SOURCE_REPORT_FAILED")
        if d.get("state") != "OK":
            time.sleep(10)
            return {}, {"action": "REPORT_PENDING"}
        blob = self.call(f"/api/client/statistics/report?UUID={report['uuid']}", performance=True)
        rows, files = [], set()
        for cid, txt in E._sku_report_files(blob, report["batch"]):
            if cid not in report["batch"] or cid in files:
                raise B.EvidenceError("SKU report unexpected/duplicate campaign file")
            files.add(cid)
            got = E._sku_rows_from_csv(cid, txt, self.run_id, self.ts)
            if not got or any(r["date"] != day for r in got):
                raise B.EvidenceError("SKU report empty/outside requested day")
            rows.extend(got)
        if files != set(report["batch"]):
            raise B.EvidenceError("SKU report missing campaign file")
        out = self.merge("RAW_OZON_ADS_SKU_DAILY", rows, ["date", "campaign_id", "sku"])
        pr["pending"] = pr["pending"][len(report["batch"]):]; pr["report"] = None
        if not pr["pending"]:
            self.advance_day(s)
        return out, {"action": "SKU_BATCH_COMPLETE", "day": day, "campaigns": len(files),
                    "expected_unique_rows": len({(r["campaign_id"], r["sku"]) for r in rows}),
                    "source_terminal": "pending" not in s["progress"]}

    def run(self, persist):
        handlers = {"fbo_postings": self.fbo, "finance_accrual": self.finance,
                    "catalog": self.catalog, "supplies": self.supplies,
                    "ads_campaigns": self.campaign_snapshot, "ads_expense_daily": self.expense,
                    "ads_sku_daily": self.sku}
        if self.p["entity"] not in handlers:
            raise B.EvidenceError("snapshot history has no source backfill contract")
        total = {"received": 0, "inserted": 0, "updated": 0}
        for unit in range(self.units):
            self.units_remaining = self.units - unit
            if self.state["complete"]:
                break
            candidate = copy.deepcopy(self.state)
            r0, pg0 = self.requests, self.pages
            transport0, retry0 = C.STATS["requests"], C.STATS["retries"]
            try:
                result, detail = handlers[self.p["entity"]](candidate)
            except B.BudgetReached:
                break  # persisted state remains prior to this unit; replay is idempotent.
            candidate["sequence"] += 1
            candidate["rows"] += result.get("received", 0)
            candidate["requests"] += self.requests - r0
            candidate["pages"] += self.pages - pg0
            B.validate(self.p, candidate)
            detail["logical_requests"] = self.requests - r0
            detail["transport_requests"] = C.STATS["requests"] - transport0
            detail["transport_retries"] = C.STATS["retries"] - retry0
            evidence = {"version": B.VERSION, "plan": self.p, "state": candidate, "detail": detail}
            # Journal ACK precedes accepting progress. Lost ACK never claims a completed window.
            persist(result, evidence)
            self.state = candidate
            for k in total:
                total[k] += result.get(k, 0)
        total["evidence"] = {"version": B.VERSION, "plan": self.p, "state": self.state,
                             "execution_requests": self.requests, "execution_pages": self.pages,
                             "complete": self.state["complete"], "replay": self.state["complete"] and self.requests == 0}
        return total


def resume(p):
    table = f"{C.PROJECT}.{C.DATASET}.{C.RUNS_TABLE}"
    config = bigquery.QueryJobConfig(use_legacy_sql=False, maximum_bytes_billed=1073741824,
        query_parameters=[bigquery.ScalarQueryParameter("pid", "STRING", p["plan_id"]),
                          bigquery.ScalarQueryParameter("origin", "TIMESTAMP", p["origin"])])
    config.query_parameters.append(bigquery.ScalarQueryParameter("entity", "STRING", p["entity"]))
    # Read cheap typed columns first; do not repeatedly scan every large continuation JSON.
    latest = list(C.bq().query(f"""SELECT MAX(backfill_sequence) AS sequence FROM `{table}`
        WHERE started_at >= @origin AND entity = @entity AND status = 'OK'
          AND backfill_plan_id = @pid""", job_config=config, location=C.LOCATION).result())
    seq = latest[0]["sequence"] if latest else None
    if seq is None:
        return B.initial(p)
    config.query_parameters.append(bigquery.ScalarQueryParameter("seq", "INT64", seq))
    rows = list(C.bq().query(f"""SELECT DISTINCT evidence_json FROM `{table}`
        WHERE started_at >= @origin AND entity = @entity AND status = 'OK'
          AND backfill_plan_id = @pid AND backfill_sequence = @seq LIMIT 2""",
        job_config=config, location=C.LOCATION).result())
    if not rows:
        raise B.EvidenceError("journal checkpoint disappeared; never reset progress")
    docs = [json.loads(r["evidence_json"]) for r in rows]
    if any(d.get("plan") != p for d in docs):
        raise B.EvidenceError("journal plan differs from execution contract")
    if len(docs) == 2 and docs[0]["state"]["sequence"] == docs[1]["state"]["sequence"] and docs[0]["state"] != docs[1]["state"]:
        raise B.EvidenceError("journal checkpoint sequence conflict")
    return B.validate(p, docs[0]["state"])


def run_backfill(p, run_id, ts):
    state = resume(p)
    engine = Engine(p, run_id, ts, state,
                    request_budget=B.integer(os.environ, "BACKFILL_MAX_REQUESTS", 400, 2, 500),
                    unit_budget=B.integer(os.environ, "BACKFILL_MAX_UNITS", 20, 1, 100))
    def persist(result, evidence):
        C.record_run(f"{run_id}-u{evidence['state']['sequence']}", p["entity"], C.now_msk(),
                     p["from"], p["to"], dict(result, evidence=evidence), "OK",
                     requests_n=evidence["detail"]["transport_requests"],
                     retries=evidence["detail"]["transport_retries"])
    return engine.run(persist)
