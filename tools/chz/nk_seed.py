#!/usr/bin/env python3
"""Копирование карточек НК из боевого контура в песочницу (только sandbox, только для тестов).

Читает сохранённый JSON боевых карточек (feed-product) и создаёт в НК песочницы
те же карточки через POST /v3/feed, затем опрашивает /v3/feed-status.
Бренд в песочнице отсутствует, поэтому подставляется значение из --brand.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import chz  # noqa: E402

NK_SANDBOX = "https://api.nk.sandbox.crptech.ru"
UNIT_CATEGORIES = [31052]           # «Уходовая косметика»; 238867 в песочнице «не входит в диапазон разрешённых»
SET_CATEGORIES = [31052]            # категории «Наборы» (237169) в песочнице нет
SKIP_ATTRS = {2504, 3959}           # 2504 «Товарный знак» задаётся полем brand; 3959 «Группа ТНВЭД» песочница отвергает


def entry_from_card(card: dict, brand: str, tech_gtin: bool, gtin_map: dict) -> dict:
    gtin13 = card["identified_by"][0]["value"]
    gtin = gtin13.zfill(14)
    attrs = []
    for a in card.get("good_attrs", []):
        if a["attr_id"] in SKIP_ATTRS:
            continue
        item = {"attr_id": a["attr_id"], "attr_value": a["attr_value"]}
        if a.get("attr_value_type"):
            item["attr_value_type"] = a["attr_value_type"]
        attrs.append(item)
    attrs.append({"attr_id": 2504, "attr_value": brand})
    e = {
        "good_name": card["good_name"],
        "brand": brand,
        "tnved": "3304990000",
        "good_attrs": attrs,
        "moderation": 1,
    }
    if tech_gtin:
        e["is_tech_gtin"] = True   # НК сам присвоит код товара с префиксом 029 (без проверки ГС1 РУС)
    else:
        e["is_tech_gtin"] = False
        e["gtin"] = gtin
        e["identified_by"] = [{"value": gtin, "type": "gtin", "multiplier": 1, "level": "trade-unit", "unit": "шт"}]
    if card.get("is_set"):
        e["is_set"] = True
        e["set_gtins"] = [{"gtin": gtin_map.get(s["gtin"].zfill(14), s["gtin"].zfill(14)), "quantity": s["quantity"]}
                          for s in card["set_gtins"]]
        # для наборов НК отвергает «categories» (код 170) и «level» в identified_by (код 120)
        e.pop("categories", None)
        if "identified_by" in e:
            e["identified_by"] = [{k: v for k, v in i.items() if k != "level"} for i in e["identified_by"]]
    else:
        e["is_set"] = False
        e["categories"] = UNIT_CATEGORIES
    return e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", required=True, help="JSON из feed-product боевого контура")
    ap.add_argument("--brand", default="EVETIS")
    ap.add_argument("--only", nargs="*", help="GTIN для отправки (по умолчанию все)")
    ap.add_argument("--tech-gtin", action="store_true", help="создавать с техническими GTIN (029…) вместо реальных")
    ap.add_argument("--gtin-map", default="{}", help='JSON {"боевой GTIN": "технический GTIN"} для состава набора')
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    gtin_map = json.loads(args.gtin_map)

    env = chz.load_env()
    c = chz.contour(env, "sandbox")
    token = chz.get_true_token(c)
    H = {"Authorization": f"Bearer {token}"}

    raw = json.load(open(args.cards, encoding="utf-8"))
    cards = []
    for g, r in raw.items():
        item = (r.get("result") or [r])[0] if isinstance(r, dict) else r
        if args.only and g not in args.only:
            continue
        cards.append(item)
    # единицы раньше набора: набор ссылается на их GTIN
    cards.sort(key=lambda x: bool(x.get("is_set")))
    feed = [entry_from_card(cd, args.brand, args.tech_gtin, gtin_map) for cd in cards]
    print(json.dumps(feed, ensure_ascii=False, indent=1)[:4000])
    if args.dry_run:
        print("DRY-RUN: фид не отправлен")
        return
    st, r = chz.http("POST", f"{NK_SANDBOX}/v3/feed", feed, headers=H)
    print("feed:", st, json.dumps(r, ensure_ascii=False)[:600])
    if st != 200 or not isinstance(r, dict):
        sys.exit(1)
    feed_id = (r.get("result") or {}).get("feed_id") if isinstance(r.get("result"), dict) else r.get("feed_id")
    if not feed_id:
        sys.exit(f"нет feed_id в ответе: {r}")
    chz.journal("nk_feed", contour="sandbox", feed_id=feed_id, names=[e["good_name"][:40] for e in feed])
    for _ in range(6):
        time.sleep(5)
        st, r = chz.http("GET", f"{NK_SANDBOX}/v3/feed-status?feed_id={feed_id}&verbose=true", headers=H)
        print("feed-status:", st, json.dumps(r, ensure_ascii=False)[:1500])
        status = (r.get("result") or {}).get("status") if isinstance(r, dict) and isinstance(r.get("result"), dict) else None
        if status and status not in ("Received", "Processing"):
            break


if __name__ == "__main__":
    main()
