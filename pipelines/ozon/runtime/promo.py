#!/usr/bin/env python3
"""Наблюдатель акций Ozon (PR-PROMO-1). ТОЛЬКО READ-методы Seller API.

Что делает: четыре раза в сутки снимает состояние акций Ozon и дописывает его в
append-only историю. Ничего не рекомендует, ничего не меняет, ничего не считает.

🔴 ГРАНИЦА ЗАПИСИ. Ключ EVETIS имеет роль Admin, поэтому мутирующие методы акций
ему технически доступны. Единственная настоящая граница — программная:
ALLOWED_PATHS ниже, через который собирается КАЖДЫЙ запрос. Путь вне списка
роняет вызов до выхода в сеть. Список запрещённых путей живёт в тесте
(pipelines/ozon/tests/test_promo_security.py), а не здесь: наблюдатель не должен
содержать мутирующий путь даже в виде константы.

🔴 КАНДИДАТ ≠ АВТОДОБАВЛЕНИЕ. «Площадка МОЖЕТ добавить» (/candidates) и
«площадка ДОБАВИТ» (/auto-add/products/list) — разные состояния. Замер 2026-09-22
по акции 4253043: ELIGIBLE содержал 17 товаров с падением вклада портфеля на
41 %, а SCHEDULED — один товар по цене ВЫШЕ текущей. Схлопывание этих состояний
создаёт ложную экономику, поэтому они разведены колонками membership и list_kind.

🔴 ПДн НЕ ЗАГРУЖАЮТСЯ. Метод «список заявок на скидку» возвращает имя, фамилию,
отчество и e-mail покупателя. Решение владельца (фаза 0.5):
DEFERRED_PRIVACY_SENSITIVE_SOURCE — в V1 он не вызывается и не упоминается здесь
даже строкой. Перечень запрещённых источников и его проверка —
в pipelines/ozon/tests/test_promo_security.py.
"""
import json
import os
import urllib.request

import common as C
from common import (DATASET, PROJECT, REF_DATASET, append_rows, bq, h, log, now_msk,
                    promo_load_job_id, promo_observation_id, promo_slot, seller_headers)

# ── Разрешённые пути. Закрытый список. ──────────────────────────────────────
P_ACTIONS = "/v1/actions"
P_PRODUCTS = "/v1/actions/products"
P_CANDIDATES = "/v1/actions/candidates"
P_AUTO_ADD_LIST = "/v1/actions/auto-add/products/list"
P_AUTO_ADD_CANDIDATES = "/v1/actions/auto-add/products/candidates"
P_PRICES = "/v5/product/info/prices"

ALLOWED_PATHS = (P_ACTIONS, P_PRODUCTS, P_CANDIDATES,
                 P_AUTO_ADD_LIST, P_AUTO_ADD_CANDIDATES, P_PRICES)

# Потолки страниц — защита от бесконечного цикла, а не ожидаемый объём.
PAGE_LIMIT = 100                 # контракт Ozon: limit 1..100 у методов акций
MAX_PAGES = 200                  # 20 000 позиций в одной акции

TBL_ACTIONS = "RAW_OZON_PROMO_ACTIONS"
TBL_PRODUCTS = "RAW_OZON_PROMO_PRODUCTS"
TBL_AUTO_ADD = "RAW_OZON_PROMO_AUTO_ADD"
TBL_MARKETING = "RAW_OZON_PROMO_PRODUCT_MARKETING"
TBL_PRODUCT_ACTION = "RAW_OZON_PROMO_PRODUCT_ACTION"
TBL_OBSERVATIONS = "OZON_PROMO_OBSERVATIONS"

# Поля ответов, известные контракту. Незнакомое поле не роняет прогон (Ozon их
# добавляет), но обязано быть видно в schema_unknown_fields манифеста.
KNOWN_ACTION_FIELDS = {
    "id", "title", "action_type", "description", "date_start", "date_end",
    "auto_add_dates", "freeze_date", "potential_products_count",
    "participating_products_count", "is_participating", "is_voucher_action",
    "banned_products_count", "with_targeting", "order_amount", "discount_type",
    "discount_value",
}
KNOWN_PRODUCT_FIELDS = {
    "id", "price", "action_price", "max_action_price", "alert_max_action_price",
    "alert_max_action_price_failed", "add_mode", "stock", "min_stock",
    "current_boost", "price_min_elastic", "price_max_elastic", "min_boost", "max_boost",
}
KNOWN_AUTO_ADD_FIELDS = {
    "product_id", "offer_id", "sku", "name", "price", "base_price",
    "max_discount_price", "min_seller_price", "marketplace_seller_price",
    "action_price_to_auto_add", "min_action_quantity", "quantity_to_auto_add",
    "currency", "add_mode",
}


class PromoPathDenied(RuntimeError):
    """Запрошен путь вне закрытого списка наблюдателя акций."""


def promo_call(path, body=None):
    """Единственная точка выхода в Seller API из наблюдателя акций.

    Путь обязан быть из ALLOWED_PATHS. GET при body=None, иначе POST —
    так же, как это делает существующий seller_post, но с проверкой пути.
    """
    if path not in ALLOWED_PATHS:
        raise PromoPathDenied(f"путь {path} не входит в разрешённый список наблюдателя акций")
    data = json.dumps(body).encode() if body is not None else None
    # Учётные данные — по ИМЕНАМ секретов из конфигурации процесса (Tenancy T2).
    req = urllib.request.Request(
        C.SELLER + path, data=data, headers=seller_headers(),
        method="POST" if data is not None else "GET")
    return C._request(req)


# ────────────────────────────────────────────────────────────── утилиты
def _num(v):
    """Число или None. Пустая строка и None → None. НИКОГДА не 0-подстановка."""
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    n = _num(v)
    return None if n is None else int(n)


def _ts(v):
    """Метка времени источника. Пустая строка Ozon → None, а не эпоха 1970."""
    if v in (None, ""):
        return None
    return str(v)


def _unknown(items, known, prefix):
    out = set()
    for it in items:
        out |= {f"{prefix}.{k}" for k in it.keys() if k not in known}
    return out


def _meta(endpoint, obs, slot, env, run_id, ts, *hash_parts):
    return {"observed_at": ts, "observation_bucket": slot, "observation_id": obs,
            "environment": env, "run_id": run_id, "source_endpoint": endpoint,
            "source_payload_hash": h(obs, *hash_parts),
            "ingested_at": now_msk().isoformat()}


def _sku_maps():
    """product_id → internal_sku и offer_id → internal_sku из справочника каналов.

    Изоляция маркетплейсов соблюдена: читается только справочный датасет
    (BQ_REF_DATASET, у EVETIS — evetis_ref), ничего из wb_*.
    """
    q = (f"SELECT marketplace_product_id, offer_id, internal_sku "
         f"FROM `{PROJECT}.{REF_DATASET}.REF_SKU_CHANNEL_MAP` "
         f"WHERE marketplace='OZON' AND is_current")
    by_pid, by_offer = {}, {}
    for r in bq().query(q, location=C.LOCATION).result():
        if r["marketplace_product_id"] not in (None, ""):
            try:
                by_pid[int(r["marketplace_product_id"])] = r["internal_sku"]
            except (TypeError, ValueError):
                pass
        if r["offer_id"] not in (None, ""):
            by_offer[str(r["offer_id"])] = r["internal_sku"]
    return by_pid, by_offer


def _resolve(by_pid, by_offer, product_id, offer_id):
    """(internal_sku, статус резолва). Нерезолвленный товар НЕ отбрасывается."""
    if product_id is not None and product_id in by_pid:
        return by_pid[product_id], "RESOLVED_BY_PRODUCT_ID"
    if offer_id is not None and str(offer_id) in by_offer:
        return by_offer[str(offer_id)], "RESOLVED_BY_OFFER_ID"
    return None, "UNRESOLVED"


def _paged_products(path, action_id):
    """Постраничный сбор products[] с пагинацией last_id.

    offset у этих методов помечен Ozon как устаревший, актуален last_id.
    Упор в потолок страниц роняет сущность, а не обрезает данные молча.
    """
    out, last_id = [], ""
    for _ in range(MAX_PAGES):
        body = {"action_id": action_id, "limit": PAGE_LIMIT}
        if last_id:
            body["last_id"] = last_id
        code, r = promo_call(path, body)
        if code != 200:
            raise RuntimeError(f"{path} action_id={action_id} → {code}: {str(r)[:200]}")
        res = r.get("result") or {}
        items = res.get("products") or []
        out.extend(items)
        nxt = res.get("last_id") or ""
        if len(items) < PAGE_LIMIT or not nxt or nxt == last_id:
            return out
        last_id = nxt
    raise RuntimeError(f"{path} action_id={action_id}: упор в потолок страниц")


def _paged_auto_add(path, action_id, auto_add_date):
    """Постраничный сбор автодобавления. Здесь пагинация по offset (контракт метода)."""
    out, offset = [], 0
    for _ in range(MAX_PAGES):
        code, r = promo_call(path, {"action_id": action_id, "auto_add_date": auto_add_date,
                                    "limit": PAGE_LIMIT, "offset": offset})
        if code != 200:
            raise RuntimeError(f"{path} action_id={action_id} → {code}: {str(r)[:200]}")
        res = r.get("result", r) or {}
        items = res.get("products") or []
        out.extend(items)
        if len(items) < PAGE_LIMIT:
            return out
        offset += PAGE_LIMIT
    raise RuntimeError(f"{path} action_id={action_id}: упор в потолок страниц")


# ────────────────────────────────────────────────────────── сущность promo
def promo(run_id, ts, _f, _t):
    """Один снимок состояния акций Ozon. Append-only, идемпотентен по слоту."""
    env = os.environ.get("ENVIRONMENT", "prod")
    slot = promo_slot()
    obs = promo_observation_id(env, slot)
    started = now_msk()
    r0, t0 = C.STATS["requests"], C.STATS["retries"]

    # Открытие строки манифеста идемпотентно по observation_id: безусловный INSERT
    # дал бы вторую строку на тот же снимок при повторе слота, и проверка «дублей
    # грейна нет» падала бы на собственной телеметрии. Дефект пойман валидацией
    # 2026-09-22: два прогона одного слота оставили две строки на один observation_id.
    bq().query(
        f"INSERT INTO `{PROJECT}.{DATASET}.{TBL_OBSERVATIONS}` "
        f"(observation_id, observation_bucket, environment, run_id, started_at, status) "
        f"SELECT '{obs}', '{slot}', '{env}', '{run_id}', "
        f"TIMESTAMP('{started.isoformat()}'), 'STARTED' "
        f"FROM UNNEST([1]) "
        f"WHERE NOT EXISTS (SELECT 1 FROM `{PROJECT}.{DATASET}.{TBL_OBSERVATIONS}` "
        f"WHERE observation_id='{obs}')",
        location=C.LOCATION).result()

    try:
        by_pid, by_offer = _sku_maps()
        unknown = set()

        # ── 1. Список акций ────────────────────────────────────────────────
        code, r = promo_call(P_ACTIONS)
        if code != 200:
            raise RuntimeError(f"{P_ACTIONS} → {code}: {str(r)[:200]}")
        actions = r.get("result") or []
        observed_at = now_msk().isoformat()
        unknown |= _unknown(actions, KNOWN_ACTION_FIELDS, "action")

        action_rows = []
        for a in actions:
            aid = _int(a.get("id"))
            dates = a.get("auto_add_dates") or []
            action_rows.append(dict(
                action_id=aid, title=a.get("title"), action_type=a.get("action_type"),
                date_start=_ts(a.get("date_start")), date_end=_ts(a.get("date_end")),
                freeze_at=_ts(a.get("freeze_date")),
                auto_add_dates_csv=",".join(str(d) for d in dates) or None,
                auto_add_dates_count=len(dates),
                is_participating=a.get("is_participating"),
                potential_products_count=_int(a.get("potential_products_count")),
                participating_products_count=_int(a.get("participating_products_count")),
                banned_products_count=_int(a.get("banned_products_count")),
                is_voucher_action=a.get("is_voucher_action"),
                with_targeting=a.get("with_targeting"),
                order_amount=_num(a.get("order_amount")),
                discount_type=a.get("discount_type"),
                discount_value=_num(a.get("discount_value")),
                description=a.get("description"),
                raw_action_json=json.dumps(a, ensure_ascii=False, sort_keys=True),
                **_meta(f"GET {P_ACTIONS}", obs, slot, env, run_id, observed_at, aid)))

        # ── 2. Товары акций: участники и кандидаты ─────────────────────────
        product_rows = []
        for a in actions:
            aid = _int(a.get("id"))
            for path, membership in ((P_PRODUCTS, "PARTICIPATING"), (P_CANDIDATES, "CANDIDATE")):
                items = _paged_products(path, aid)
                unknown |= _unknown(items, KNOWN_PRODUCT_FIELDS, "product")
                for it in items:
                    pid = _int(it.get("id"))
                    sku, status = _resolve(by_pid, by_offer, pid, None)
                    product_rows.append(dict(
                        action_id=aid, membership=membership, product_id=pid,
                        internal_sku=sku, sku_resolution_status=status,
                        price_rub=_num(it.get("price")),
                        action_price_rub=_num(it.get("action_price")),
                        max_action_price_rub=_num(it.get("max_action_price")),
                        alert_max_action_price_rub=_num(it.get("alert_max_action_price")),
                        alert_max_action_price_failed=it.get("alert_max_action_price_failed"),
                        add_mode=it.get("add_mode"),
                        stock=_int(it.get("stock")), min_stock=_int(it.get("min_stock")),
                        current_boost_pct=_num(it.get("current_boost")),
                        min_boost_pct=_num(it.get("min_boost")),
                        max_boost_pct=_num(it.get("max_boost")),
                        price_min_elastic_rub=_num(it.get("price_min_elastic")),
                        price_max_elastic_rub=_num(it.get("price_max_elastic")),
                        raw_item_json=json.dumps(it, ensure_ascii=False, sort_keys=True),
                        **_meta(f"POST {path}", obs, slot, env, run_id, observed_at,
                                aid, membership, pid)))

        # ── 3. Автодобавление: запланированное и возможное ────────────────
        auto_rows, auto_pairs = [], 0
        for a in actions:
            aid = _int(a.get("id"))
            for d in (a.get("auto_add_dates") or []):
                auto_pairs += 1
                for path, kind in ((P_AUTO_ADD_LIST, "SCHEDULED"),
                                   (P_AUTO_ADD_CANDIDATES, "ELIGIBLE")):
                    items = _paged_auto_add(path, aid, d)
                    unknown |= _unknown(items, KNOWN_AUTO_ADD_FIELDS, "auto_add")
                    for it in items:
                        pid = _int(it.get("product_id"))
                        offer = it.get("offer_id")
                        sku, status = _resolve(by_pid, by_offer, pid, offer)
                        auto_rows.append(dict(
                            action_id=aid, auto_add_at=_ts(d), list_kind=kind,
                            product_id=pid, offer_id=(str(offer) if offer is not None else None),
                            ozon_sku=_int(it.get("sku")), internal_sku=sku,
                            sku_resolution_status=status, product_name=it.get("name"),
                            price_rub=_num(it.get("price")),
                            base_price_rub=_num(it.get("base_price")),
                            max_discount_price_rub=_num(it.get("max_discount_price")),
                            min_seller_price_rub=_num(it.get("min_seller_price")),
                            marketplace_seller_price_rub=_num(it.get("marketplace_seller_price")),
                            action_price_to_auto_add_rub=_num(it.get("action_price_to_auto_add")),
                            min_action_quantity=_int(it.get("min_action_quantity")),
                            quantity_to_auto_add=_int(it.get("quantity_to_auto_add")),
                            currency_code=it.get("currency"), add_mode=it.get("add_mode"),
                            raw_item_json=json.dumps(it, ensure_ascii=False, sort_keys=True),
                            **_meta(f"POST {path}", obs, slot, env, run_id, observed_at,
                                    aid, d, kind, pid)))

        # ── 4. marketing_actions на уровне товара ──────────────────────────
        # Закрывает пробел фазы 0: блок приходит в боевой загрузчик цен и там
        # отбрасывается, кроме одного флага. RAW_OZON_PRICES НЕ трогаем —
        # существующая семантика остаётся прежней, наблюдение живёт здесь.
        mk_rows, act_rows, cursor = [], [], ""
        while True:
            code, r = promo_call(P_PRICES, {"cursor": cursor, "limit": PAGE_LIMIT,
                                            "filter": {"offer_id": [], "product_id": [],
                                                       "visibility": "ALL"}})
            if code != 200:
                raise RuntimeError(f"{P_PRICES} → {code}: {str(r)[:200]}")
            items = r.get("items") or []
            for i in items:
                offer = str(i.get("offer_id"))
                pid = _int(i.get("product_id"))
                sku, status = _resolve(by_pid, by_offer, pid, offer)
                ma = i.get("marketing_actions") or {}
                acts = ma.get("actions") or []
                p = i.get("price") or {}
                mk_rows.append(dict(
                    offer_id=offer, product_id=pid, internal_sku=sku,
                    sku_resolution_status=status,
                    # ⚠️ ДОСЛОВНО и НЕ авторитетно: наблюдался FALSE при непустом actions[].
                    source_actions_exist_flag=ma.get("ozon_actions_exist"),
                    current_period_from=_ts(ma.get("current_period_from")),
                    current_period_to=_ts(ma.get("current_period_to")),
                    actions_count=len(acts),
                    price_rub=_num(p.get("price")), min_price_rub=_num(p.get("min_price")),
                    marketing_seller_price_rub=_num(p.get("marketing_seller_price")),
                    old_price_rub=_num(p.get("old_price")),
                    auto_action_enabled=p.get("auto_action_enabled"),
                    auto_add_to_ozon_actions_enabled=p.get("auto_add_to_ozon_actions_list_enabled"),
                    marketing_actions_json=json.dumps(ma, ensure_ascii=False, sort_keys=True),
                    **_meta(f"POST {P_PRICES}", obs, slot, env, run_id, observed_at, offer)))
                for n, act in enumerate(acts):
                    act_rows.append(dict(
                        offer_id=offer, product_id=pid, internal_sku=sku, action_ordinal=n,
                        action_title=act.get("title"),
                        # value разнороден по единице измерения — не нормализуем.
                        action_value_num=_num(act.get("value")),
                        action_date_from=_ts(act.get("date_from")),
                        action_date_to=_ts(act.get("date_to")),
                        raw_item_json=json.dumps(act, ensure_ascii=False, sort_keys=True),
                        **_meta(f"POST {P_PRICES}", obs, slot, env, run_id, observed_at, offer, n)))
            cursor = r.get("cursor") or ""
            if not cursor or not items:
                break

        # ── 5. Запись: append-only, детерминированный job_id ───────────────
        written, outcomes = 0, []
        for table, rows in ((TBL_ACTIONS, action_rows), (TBL_PRODUCTS, product_rows),
                            (TBL_AUTO_ADD, auto_rows), (TBL_MARKETING, mk_rows),
                            (TBL_PRODUCT_ACTION, act_rows)):
            outcome, n = append_rows(table, rows, promo_load_job_id(env, slot, table))
            outcomes.append(outcome)
            written += n

        # ── 6. Манифест ───────────────────────────────────────────────────
        src_ids = {r["product_id"] for r in product_rows if r["product_id"] is not None}
        src_ids |= {r["product_id"] for r in auto_rows if r["product_id"] is not None}
        src_ids |= {r["product_id"] for r in mk_rows if r["product_id"] is not None}
        unmapped = sorted(
            {str(r["product_id"]) for r in product_rows + auto_rows + mk_rows
             if r["sku_resolution_status"] == "UNRESOLVED" and r["product_id"] is not None})
        mapped = len(src_ids) - len(unmapped)
        cov = round(mapped / len(src_ids) * 100, 2) if src_ids else None
        reused = bool(outcomes) and all(o == "REUSED" for o in outcomes)

        bq().query(
            f"UPDATE `{PROJECT}.{DATASET}.{TBL_OBSERVATIONS}` SET "
            f"status='{'REUSED' if reused else 'COMPLETE'}', "
            f"observed_at=TIMESTAMP('{observed_at}'), completed_at=CURRENT_TIMESTAMP(), "
            f"requests={C.STATS['requests'] - r0}, retries={C.STATS['retries'] - t0}, "
            f"actions_total={len(action_rows)}, "
            f"actions_participating={sum(1 for a in action_rows if a['is_participating'])}, "
            f"products_participating={sum(1 for p in product_rows if p['membership'] == 'PARTICIPATING')}, "
            f"products_candidate={sum(1 for p in product_rows if p['membership'] == 'CANDIDATE')}, "
            f"auto_add_scheduled_rows={sum(1 for a in auto_rows if a['list_kind'] == 'SCHEDULED')}, "
            f"auto_add_eligible_rows={sum(1 for a in auto_rows if a['list_kind'] == 'ELIGIBLE')}, "
            f"auto_add_pairs={auto_pairs}, marketing_products={len(mk_rows)}, "
            f"marketing_action_rows={len(act_rows)}, source_products={len(src_ids)}, "
            f"mapped_products={mapped}, unmapped_products={len(unmapped)}, "
            f"unmapped_ids={'NULL' if not unmapped else repr(','.join(unmapped))}, "
            f"mapping_coverage_pct={'NULL' if cov is None else cov}, "
            f"rows_written={written}, "
            f"schema_status='{'DRIFT_NEW_FIELDS' if unknown else 'OK'}', "
            f"schema_unknown_fields={'NULL' if not unknown else repr(','.join(sorted(unknown)))} "
            f"WHERE observation_id='{obs}'",
            location=C.LOCATION).result()

        log(event="ozon_promo_complete", observation_id=obs, slot=slot, reused=reused,
            actions=len(action_rows), products=len(product_rows), auto_add=len(auto_rows),
            marketing=len(mk_rows), marketing_actions=len(act_rows),
            unmapped=len(unmapped), mapping_coverage_pct=cov,
            schema_status="DRIFT_NEW_FIELDS" if unknown else "OK")
        return {"received": written, "inserted": 0 if reused else written,
                "updated": written if reused else 0}

    except Exception as e:                                            # noqa: BLE001
        msg = repr(e)[:400].replace("'", "")
        try:
            bq().query(
                f"UPDATE `{PROJECT}.{DATASET}.{TBL_OBSERVATIONS}` SET "
                f"status='ERROR', completed_at=CURRENT_TIMESTAMP(), "
                f"error_code='{type(e).__name__}', error_message='{msg}' "
                f"WHERE observation_id='{obs}'", location=C.LOCATION).result()
        except Exception as e2:                                       # noqa: BLE001
            log(event="ozon_promo_manifest_finalize_failed", error=repr(e2)[:200])
        raise
