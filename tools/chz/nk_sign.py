#!/usr/bin/env python3
"""Подписание карточек НК УКЭП (после модерации): feed-product-document → csptest → feed-product-sign-pkcs.

По умолчанию — песочница. Боевой контур только с --env prod --confirm.
"""
import argparse
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import chz  # noqa: E402

NK = {
    "sandbox": "https://api.nk.sandbox.crptech.ru",
    "prod": "https://" + "апи.национальный-каталог.рф".encode("idna").decode(),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=list(NK), default="sandbox")
    ap.add_argument("gtins", nargs="+")
    ap.add_argument("--confirm", action="store_true")
    args = ap.parse_args()
    if args.env == "prod" and not args.confirm:
        sys.exit("боевой контур: подписание карточек требует --confirm")

    env = chz.load_env()
    c = chz.contour(env, args.env)
    H = {"Authorization": f"Bearer {chz.get_true_token(c)}"}
    base = NK[args.env]

    st, r = chz.http("POST", f"{base}/v3/feed-product-document", {"gtins": args.gtins, "publicationAgreement": False}, headers=H)
    if st != 200 or not isinstance(r, dict):
        sys.exit(f"feed-product-document: HTTP {st}: {r}")
    res = r.get("result") or {}
    for e in res.get("errors") or []:
        print("ошибка:", json.dumps(e, ensure_ascii=False))
    xmls = res.get("xmls") or []
    if not xmls:
        sys.exit("нет XML для подписи (карточки ещё на модерации?)")
    payload = []
    for x in xmls:
        xml_bytes = x["xml"].encode("utf-8")
        sig = chz.sign(xml_bytes, c["thumbprint"], detached=True)
        payload.append({"goodId": x["goodId"], "base64Xml": base64.b64encode(xml_bytes).decode("ascii"), "signature": sig})
        print(f"подписано: goodId={x['goodId']}, xml {len(xml_bytes)} байт")
    st, r = chz.http("POST", f"{base}/v3/feed-product-sign-pkcs", payload, headers=H)
    print("sign-pkcs:", st, json.dumps(r, ensure_ascii=False)[:1200])
    chz.journal("nk_sign", contour=args.env, gtins=args.gtins, status=st,
                signed=(r.get("result") or {}).get("signed") if isinstance(r, dict) else None)


if __name__ == "__main__":
    main()
