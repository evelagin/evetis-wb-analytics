#!/usr/bin/env python3
"""Исправление атрибута 23821 «Количество маркированных товаров в наборе» в карточках НК.

ГИС МТ при формировании набора сверяет фактическое число вложений с этим атрибутом
(ошибка «…не соответствует справочному количеству товаров в Наборе»). Скрипт читает
карточки наборов через feed-product, находит расхождения с суммой set_gtins и
отправляет обновление через /v3/feed (good_id + один атрибут, moderation=1).
После модерации карточку нужно подписать: nk_sign.py --env prod --confirm <gtin…>.

По умолчанию dry-run. Боевая отправка только с --confirm.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import chz  # noqa: E402

NK = {
    "sandbox": "https://api.nk.sandbox.crptech.ru",
    "prod": "https://" + "апи.национальный-каталог.рф".encode("idna").decode(),
}
ATTR_MARKED_COUNT = 23821


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=list(NK), default="prod")
    ap.add_argument("gtins", nargs="+", help="GTIN наборов для проверки")
    ap.add_argument("--confirm", action="store_true", help="отправить исправление (иначе dry-run)")
    args = ap.parse_args()

    env = chz.load_env()
    c = chz.contour(env, args.env)
    H = {"Authorization": f"Bearer {chz.get_true_token(c)}"}
    base = NK[args.env]

    feed = []
    for g in args.gtins:
        g14 = g.zfill(14)
        st, r = chz.http("GET", f"{base}/v3/feed-product?gtin={g14}", headers=H)
        it = (r.get("result") or [None])[0] if isinstance(r, dict) else None
        if not it:
            print(f"{g14}: карточка не найдена (HTTP {st})")
            continue
        if not it.get("is_set"):
            print(f"{g14}: не набор, пропуск")
            continue
        total = sum(int(s["quantity"]) for s in it.get("set_gtins", []))
        cur = next((x["attr_value"] for x in it.get("good_attrs", []) if x["attr_id"] == ATTR_MARKED_COUNT), None)
        if cur is not None and int(cur) == total:
            print(f"{g14}: OK (23821={cur}, вложений {total})")
            continue
        print(f"{g14}: good_id={it['good_id']} 23821={cur} → {total}  [{it.get('good_status')}]")
        feed.append({"good_id": it["good_id"], "good_attrs": [{"attr_id": ATTR_MARKED_COUNT, "attr_value": str(total)}], "moderation": 1})

    if not feed:
        print("исправлять нечего")
        return
    print("фид:", json.dumps(feed, ensure_ascii=False))
    if not args.confirm:
        print("DRY-RUN: не отправлено (добавьте --confirm)")
        return
    st, r = chz.http("POST", f"{base}/v3/feed", feed, headers=H)
    print("feed:", st, json.dumps(r, ensure_ascii=False)[:500])
    if st != 200:
        sys.exit(1)
    feed_id = (r.get("result") or {}).get("feed_id")
    chz.journal("nk_set_count_fix", contour=args.env, feed_id=feed_id, good_ids=[e["good_id"] for e in feed])
    for _ in range(6):
        time.sleep(5)
        st, r = chz.http("GET", f"{base}/v3/feed-status?feed_id={feed_id}&verbose=true", headers=H)
        res = r.get("result", {}) if isinstance(r, dict) else {}
        print("feed-status:", res.get("status"), json.dumps(res.get("error_details"), ensure_ascii=False)[:800])
        if res.get("status") not in ("Received", "Processing", None):
            break


if __name__ == "__main__":
    main()
