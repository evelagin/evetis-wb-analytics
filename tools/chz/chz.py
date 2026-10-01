#!/usr/bin/env python3
"""EVETIS × «Честный знак»: локальный CLI (только этот Mac, подпись УКЭП через КриптоПро CSP).

Шаг 1 (только чтение):
  auth    — токены True API (ГИС МТ) и СУЗ через единую аутентификацию
  status  — статусы кодов маркировки через POST /cises/info

Конфигурация: ~/.config/evetis-chz/.env (права 600, вне git).
Контур по умолчанию — песочница; боевой контур только с флагом --env prod.
Никаких документов в ГИС МТ этот шаг не создаёт.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

CONFIG_DIR = os.path.expanduser("~/.config/evetis-chz")
ENV_PATH = os.path.join(CONFIG_DIR, ".env")
JOURNAL_PATH = os.path.join(CONFIG_DIR, "journal.jsonl")
CSPTEST = "/opt/cprocsp/bin/csptest"

CONTOURS = {
    "prod": {
        "true_v3": "https://markirovka.crpt.ru/api/v3/true-api",
        "true_v4": "https://markirovka.crpt.ru/api/v4/true-api",
        "suz": "https://suzgrid.crpt.ru/api/v3",
        "oms_id_key": "CHZ_OMS_ID",
        "oms_conn_key": "CHZ_OMS_CONNECTION",
    },
    "sandbox": {
        "true_v3": "https://markirovka.sandbox.crptech.ru/api/v3/true-api",
        "true_v4": "https://markirovka.sandbox.crptech.ru/api/v4/true-api",
        "suz": "https://suz.sandbox.crptech.ru/api/v3",
        "oms_id_key": "CHZ_SANDBOX_OMS_ID",
        "oms_conn_key": "CHZ_SANDBOX_OMS_CONNECTION",
    },
}
TOKEN_TTL_HOURS = 10  # по документации ЦРПТ: токен действует не более 10 часов


# ---------------------------------------------------------------- конфигурация

def load_env() -> dict:
    if not os.path.exists(ENV_PATH):
        sys.exit(f"нет файла конфигурации {ENV_PATH}")
    env = {}
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def contour(env: dict, name: str) -> dict:
    c = dict(CONTOURS[name])
    c["name"] = name
    c["oms_id"] = env.get(c["oms_id_key"], "")
    c["oms_connection"] = env.get(c["oms_conn_key"], "")
    c["inn"] = env.get("CHZ_INN", "")
    c["thumbprint"] = env.get("CHZ_CERT_THUMBPRINT", "")
    c["pg"] = env.get("CHZ_PRODUCT_GROUP", "chemistry")
    if not c["thumbprint"]:
        sys.exit("в .env не задан CHZ_CERT_THUMBPRINT")
    return c


def journal(event: str, **fields) -> None:
    rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.chmod(JOURNAL_PATH, 0o600)


# ---------------------------------------------------------------- подпись

def sign(data: bytes, thumbprint: str, detached: bool) -> str:
    """CMS-подпись байтов через КриптоПро csptest. Возвращает base64 одной строкой."""
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "data.bin")
        sig = os.path.join(d, "data.sig")
        with open(src, "wb") as f:
            f.write(data)
        cmd = [CSPTEST, "-sfsign", "-sign", "-base64", "-add", "-addsigtime",
               "-my", thumbprint, "-in", src, "-out", sig]
        if detached:
            cmd.insert(3, "-detached")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0 or not os.path.exists(sig):
            sys.exit(f"подпись не удалась (csptest rc={r.returncode}):\n{r.stdout[-800:]}{r.stderr[-800:]}")
        b64 = open(sig, encoding="ascii").read()
    return re.sub(r"\s+", "", b64)


# ---------------------------------------------------------------- HTTP

def http(method: str, url: str, body=None, headers: dict | None = None, timeout: int = 60):
    data = None
    hdrs = {"accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = text
    journal("http", method=method, url=url.split("?")[0], status=status)
    return status, parsed


# ---------------------------------------------------------------- токены

def mask(r):
    """Скрыть значения токенов в диагностике."""
    if isinstance(r, dict):
        return {k: ("***" if "oken" in k.lower() else v) for k, v in r.items()}
    return r


def token_path(c: dict, kind: str) -> str:
    return os.path.join(CONFIG_DIR, f"token_{c['name']}_{kind}.json")


def load_token(c: dict, kind: str) -> str | None:
    p = token_path(c, kind)
    if not os.path.exists(p):
        return None
    t = json.load(open(p))
    if dt.datetime.fromisoformat(t["expires"]) - dt.datetime.now() < dt.timedelta(minutes=10):
        return None
    return t["token"]


def save_token(c: dict, kind: str, token: str, expires: dt.datetime) -> None:
    p = token_path(c, kind)
    with open(p, "w") as f:
        json.dump({"token": token, "expires": expires.isoformat(timespec="seconds")}, f)
    os.chmod(p, 0o600)


def auth_key(c: dict) -> tuple[str, str]:
    st, r = http("GET", f"{c['true_v3']}/auth/key")
    if st != 200 or not isinstance(r, dict) or "uuid" not in r:
        sys.exit(f"auth/key: HTTP {st}: {r}")
    return r["uuid"], r["data"]


def get_true_token(c: dict, force: bool = False) -> str:
    if not force:
        t = load_token(c, "true")
        if t:
            return t
    uuid, data = auth_key(c)
    signed = sign(data.encode("utf-8"), c["thumbprint"], detached=False)
    body = {"uuid": uuid, "data": signed, "unitedToken": True}
    st, r = http("POST", f"{c['true_v3']}/auth/simpleSignIn", body)
    # На бою (30.09.2026) ответ содержит только uuidToken + expireDate; поле token из документации отсутствует
    token = (r.get("uuidToken") or r.get("token")) if isinstance(r, dict) else None
    if st != 200 or not token:
        sys.exit(f"simpleSignIn (True API): HTTP {st}: {mask(r)}")
    expires = dt.datetime.now() + dt.timedelta(hours=TOKEN_TTL_HOURS)
    if r.get("expireDate"):
        try:
            expires = dt.datetime.fromisoformat(r["expireDate"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        except ValueError:
            pass
    save_token(c, "true", token, expires)
    journal("auth_true", contour=c["name"], expires=expires.isoformat(timespec="seconds"),
            format="uuid" if r.get("uuidToken") else "jwt")
    return token


def get_suz_token(c: dict, force: bool = False) -> str:
    if not c["oms_connection"]:
        sys.exit(f"в .env не задан {c['oms_conn_key']} для контура {c['name']}")
    if not force:
        t = load_token(c, "suz")
        if t:
            return t
    uuid, data = auth_key(c)
    signed = sign(data.encode("utf-8"), c["thumbprint"], detached=False)
    body = {"uuid": uuid, "data": signed}
    st, r = http("POST", f"{c['true_v3']}/auth/simpleSignIn/{c['oms_connection']}", body)
    token = (r.get("token") or r.get("uuidToken") or r.get("clientToken")) if isinstance(r, dict) else None
    if st != 200 or not token:
        sys.exit(f"simpleSignIn (СУЗ): HTTP {st}: {mask(r)}")
    expires = dt.datetime.now() + dt.timedelta(hours=TOKEN_TTL_HOURS)
    if isinstance(r, dict) and r.get("expireDate"):
        try:
            expires = dt.datetime.fromisoformat(r["expireDate"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        except ValueError:
            pass
    save_token(c, "suz", token, expires)
    journal("auth_suz", contour=c["name"], expires=expires.isoformat(timespec="seconds"))
    return token


# ---------------------------------------------------------------- коды

def read_codes(args) -> list[str]:
    codes: list[str] = []
    for c in args.codes or []:
        codes.append(c)
    if args.file:
        p = args.file
        if p.lower().endswith(".xlsx"):
            codes += codes_from_xlsx(p)
        elif p.lower().endswith(".json"):
            codes += codes_from_json(json.load(open(p, encoding="utf-8")))
        else:
            codes += [ln.strip() for ln in open(p, encoding="utf-8") if ln.strip()]
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def codes_from_json(obj) -> list[str]:
    found: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("uit_code", "unitSerialNumber", "cis") and isinstance(v, str):
                    found.append(v)
                elif k == "sntins" and isinstance(v, list):
                    found.extend(x for x in v if isinstance(x, str))
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(obj)
    return found


def codes_from_xlsx(path: str) -> list[str]:
    """Выгрузка «Коды маркировки» из ЛК: первая колонка «Код», первая строка — фильтр."""
    M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as z:
        ss = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            ss = ["".join(t.text or "" for t in si.iter(M + "t")) for si in root.findall(M + "si")]
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    out = []
    for row in sheet.iter(M + "row"):
        cells = row.findall(M + "c")
        if not cells:
            continue
        c0 = cells[0]
        v = c0.find(M + "v")
        isv = c0.find(M + "is")
        if v is not None:
            val = ss[int(v.text)] if c0.get("t") == "s" else v.text
        elif isv is not None:
            val = "".join(t.text or "" for t in isv.iter(M + "t"))
        else:
            continue
        if val and val.startswith("01") and len(val) >= 18 and not val.startswith("Фильтр"):
            out.append(val)
    return out


def cmd_status(c: dict, args) -> None:
    codes = read_codes(args)
    if not codes:
        sys.exit("укажите коды аргументами или --file (txt/json/xlsx)")
    token = get_true_token(c)
    rows = []
    for i in range(0, len(codes), 1000):
        chunk = codes[i:i + 1000]
        st, r = http("POST", f"{c['true_v3']}/cises/info?pg={c['pg']}", chunk,
                     headers={"Authorization": f"Bearer {token}"})
        if st != 200 or not isinstance(r, list):
            sys.exit(f"cises/info: HTTP {st}: {r}")
        rows += r
    summary: dict[str, int] = {}
    print(f"{'код (хвост)':<14} {'GTIN':<15} {'статус':<12} {'эмиссия':<8} {'владелец ИНН':<13} произведено  товар")
    for item in rows:
        info = item.get("cisInfo") or {}
        if not info:
            print(f"{'?':<14} ошибка: {item.get('errorMessage') or item}")
            continue
        cis = info.get("requestedCis") or info.get("cis") or ""
        status = info.get("status", "")
        summary[status] = summary.get(status, 0) + 1
        print(f"{cis[-13:]:<14} {info.get('gtin',''):<15} {status:<12} {info.get('emissionType',''):<8} "
              f"{info.get('ownerInn',''):<13} {(info.get('producedDate') or '')[:10]:<11} {(info.get('productName') or '')[:40]}")
    print("итого:", ", ".join(f"{k}={v}" for k, v in sorted(summary.items())), f"(всего {len(rows)})")
    journal("status", contour=c["name"], n=len(rows), summary=summary)


# ---------------------------------------------------------------- СУЗ: заказы и коды

GS = "\x1d"  # разделитель групп внутри кода маркировки
TEMPLATE_ID_CHEMISTRY = 46  # шаблон КМ косметики: 01 GTIN + 21 SERIAL(6) + 91(4) + 92(44); пример из API СУЗ 4.4.1.1.26
CODES_DIR = os.path.join(CONFIG_DIR, "codes")


def ki(code: str) -> str:
    """Код идентификации без криптохвоста (до первого GS) — так его принимает True API."""
    return code.split(GS, 1)[0]


def suz_get(c: dict, path: str, params: dict):
    """GET к СУЗ: подпись не требуется, только clientToken."""
    token = get_suz_token(c)
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return http("GET", f"{c['suz']}{path}?omsId={c['oms_id']}&{qs}", headers={"clientToken": token})


def suz_post(c: dict, path: str, body: dict, dry_run: bool):
    """POST к СУЗ: тело сериализуется один раз, подписывается отсоединённо (X-Signature), отправляются те же байты."""
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    print("тело запроса:", raw.decode("utf-8")[:1500])
    if dry_run:
        print("DRY-RUN: запрос не отправлен")
        return None, None
    token = get_suz_token(c)
    sig = sign(raw, c["thumbprint"], detached=True)
    st, r = http("POST", f"{c['suz']}{path}?omsId={c['oms_id']}", raw,
                 headers={"clientToken": token, "X-Signature": sig, "Content-Type": "application/json"})
    return st, r


def confirm_prod(c: dict, args, what: str) -> None:
    if c["name"] == "prod" and not getattr(args, "confirm", False):
        sys.exit(f"боевой контур: {what} требует явного флага --confirm")


def cmd_orders(c: dict, args) -> None:
    st, r = suz_get(c, "/order/list", {})
    if st != 200 or not isinstance(r, dict):
        sys.exit(f"order/list: HTTP {st}: {r}")
    infos = sorted(r.get("orderInfos", []), key=lambda o: o.get("createdTimestamp", 0))
    print(f"заказов: {len(infos)}")
    for o in infos[-args.last:]:
        ts = dt.datetime.fromtimestamp(o.get("createdTimestamp", 0) / 1000).strftime("%Y-%m-%d %H:%M")
        for b in o.get("buffers", []) or [{}]:
            print(f"{ts} {o['orderId']} {o.get('orderStatus', ''):<8} gtin={b.get('gtin', '')} tpl={b.get('templateId', '')} "
                  f"всего={b.get('totalCodes', '')} осталось={b.get('leftInBuffer', '')} буфер={b.get('bufferStatus', '')}")


def cmd_product(c: dict, args) -> None:
    token = get_true_token(c)
    st, r = http("POST", f"{c['true_v4']}/product/info", {"gtins": args.gtins, "rdInfo": True},
                 headers={"Authorization": f"Bearer {token}"})
    if st != 200 or not isinstance(r, dict):
        sys.exit(f"product/info: HTTP {st}: {r}")
    res = r.get("results", [])
    if not res:
        print(f"карточек не найдено на контуре {c['name']}")
    for p in res:
        print(f"{p.get('gtin')} | {p.get('name')} | упаковка={p.get('packageType')} | к обороту={p.get('goodTurnFlag')} "
              f"к маркировке={p.get('goodMarkFlag')} | ИНН={p.get('inn')}")
        if args.raw:
            print(json.dumps(p, ensure_ascii=False, indent=1)[:3000])


def cmd_order(c: dict, args) -> None:
    confirm_prod(c, args, "заказ кодов")
    if args.qty < 1 or args.qty > 150000:
        sys.exit("qty вне диапазона 1..150000")
    body = {
        "productGroup": c["pg"],
        "products": [{
            "gtin": args.gtin,
            "quantity": args.qty,
            "serialNumberType": "OPERATOR",
            "templateId": args.template,
            "cisType": "SET" if args.set else "UNIT",
        }],
        "attributes": {"releaseMethodType": "IMPORT" if args.imported else "PRODUCTION"},
    }
    st, r = suz_post(c, "/order", body, args.dry_run)
    if st is None:
        return
    if st != 200 or not isinstance(r, dict) or "orderId" not in r:
        sys.exit(f"order: HTTP {st}: {r}")
    eta = int(r.get("expectedCompleteTimestamp", 0)) / 1000
    print(f"заказ создан: orderId={r['orderId']}, ожидание ≈ {eta:.0f} с")
    journal("order", contour=c["name"], order_id=r["orderId"], gtin=args.gtin, qty=args.qty,
            cis_type=body["products"][0]["cisType"], release=body["attributes"]["releaseMethodType"])


def cmd_order_status(c: dict, args) -> None:
    params = {"orderId": args.order}
    if args.gtin:
        params["gtin"] = args.gtin
    st, r = suz_get(c, "/order/status", params)
    if st != 200:
        sys.exit(f"order/status: HTTP {st}: {r}")
    for b in (r if isinstance(r, list) else [r]):
        print(f"gtin={b.get('gtin')} буфер={b.get('bufferStatus')} всего={b.get('totalCodes')} доступно={b.get('availableCodes')} "
              f"осталось={b.get('leftInBuffer')} tpl={b.get('templateId')} {('ошибка: ' + str(b.get('rejectionReason'))) if b.get('rejectionReason') else ''}")


def cmd_codes(c: dict, args) -> None:
    confirm_prod(c, args, "получение кодов (списывает их из буфера)")
    st, r = suz_get(c, "/codes", {"orderId": args.order, "gtin": args.gtin, "quantity": args.qty})
    if st != 200 or not isinstance(r, dict) or "codes" not in r:
        sys.exit(f"codes: HTTP {st}: {r}")
    codes = r["codes"]
    os.makedirs(CODES_DIR, exist_ok=True)
    os.chmod(CODES_DIR, 0o700)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    path = os.path.join(CODES_DIR, f"{c['name']}_{args.gtin}_{args.order[:8]}_{stamp}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(codes) + "\n")
    os.chmod(path, 0o600)
    print(f"получено кодов: {len(codes)}, blockId={r.get('blockId')}, сохранено: {path}")
    for code in codes[:3]:
        print("  ", ki(code), "…")
    journal("codes", contour=c["name"], order_id=args.order, gtin=args.gtin, n=len(codes),
            block_id=r.get("blockId"), file=path)


def cmd_utilisation(c: dict, args) -> None:
    confirm_prod(c, args, "отчёт о нанесении")
    codes = [ln.rstrip("\n") for ln in open(args.file, encoding="utf-8") if ln.strip()]
    if not codes:
        sys.exit("файл кодов пуст")
    bad = [k for k in codes if GS not in k]
    if bad:
        sys.exit(f"{len(bad)} кодов без криптохвоста (GS): отчёт о нанесении требует полные коды из /codes; пример: {bad[0][:30]}")
    for d in (args.production_date, args.expiration_date):
        dt.date.fromisoformat(d)
    if args.production_date > dt.date.today().isoformat():
        sys.exit("дата производства в будущем")
    if args.expiration_date <= dt.date.today().isoformat():
        sys.exit("срок годности должен быть позже сегодняшней даты")
    body = {
        "productGroup": c["pg"],
        "sntins": codes,
        "attributes": {
            "productionDate": args.production_date,
            "expirationDate": args.expiration_date,
            "alcoholVolume": args.alcohol,
        },
    }
    st, r = suz_post(c, "/utilisation", body, args.dry_run)
    if st is None:
        return
    if st != 200 or not isinstance(r, dict) or "reportId" not in r:
        sys.exit(f"utilisation: HTTP {st}: {r}")
    print(f"отчёт принят: reportId={r['reportId']} — проверьте обработку: report-status --report {r['reportId']}")
    journal("utilisation", contour=c["name"], report_id=r["reportId"], n=len(codes),
            production_date=args.production_date, expiration_date=args.expiration_date, file=args.file)


def cmd_report_status(c: dict, args) -> None:
    st, r = suz_get(c, "/report/info", {"reportId": args.report})
    if st != 200:
        sys.exit(f"report/info: HTTP {st}: {r}")
    print(json.dumps(r, ensure_ascii=False))


# ---------------------------------------------------------------- True API: документы

def true_document(c: dict, doc_type: str, body: dict, dry_run: bool):
    """POST /lk/documents/create: документ сериализуется один раз, подписывается отсоединённо, кодируется base64."""
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    print(f"документ {doc_type}:", raw.decode("utf-8")[:2500])
    if dry_run:
        print("DRY-RUN: документ не отправлен")
        return None, None
    token = get_true_token(c)
    sig = sign(raw, c["thumbprint"], detached=True)
    envelope = {"document_format": "MANUAL", "product_document": base64.b64encode(raw).decode("ascii"),
                "type": doc_type, "signature": sig}
    st, r = http("POST", f"{c['true_v3']}/lk/documents/create?pg={c['pg']}", envelope,
                 headers={"Authorization": f"Bearer {token}"})
    return st, r


def cises_info(c: dict, codes: list[str]) -> dict:
    token = get_true_token(c)
    out = {}
    for i in range(0, len(codes), 1000):
        st, r = http("POST", f"{c['true_v3']}/cises/info?pg={c['pg']}", codes[i:i + 1000],
                     headers={"Authorization": f"Bearer {token}"})
        if st != 200 or not isinstance(r, list):
            sys.exit(f"cises/info: HTTP {st}: {r}")
        for item in r:
            info = item.get("cisInfo") or {}
            key = info.get("requestedCis") or info.get("cis")
            if key:
                out[key] = info
    return out


def cmd_set(c: dict, args) -> None:
    """Формирование наборов документом AGGREGATION_DOCUMENT (aggregationType=SET).

    Вход: JSON вида {"participantId":..., "aggregationUnits":[{"unitSerialNumber":..., "sntins":[...]}]}
    (формат экспорта ЛК подходит; поле aggregationType перезаписывается).
    Перед отправкой проверяются правила ЧЗ для косметики: вложения все APPLIED (один тип эмиссии, набор APPLIED
    того же типа) либо все INTRODUCED (набор APPLIED и LOCAL).
    """
    confirm_prod(c, args, "формирование набора")
    src = json.load(open(args.file, encoding="utf-8"))
    units = src.get("aggregationUnits") or []
    if not units:
        sys.exit("в файле нет aggregationUnits")
    all_codes = [ki(u["unitSerialNumber"]) for u in units] + [ki(s) for u in units for s in u.get("sntins", [])]
    info = cises_info(c, all_codes)
    problems = []
    for u in units:
        set_code = ki(u["unitSerialNumber"])
        si = info.get(set_code, {})
        children = [info.get(ki(s), {}) for s in u.get("sntins", [])]
        ch_status = {x.get("status") for x in children}
        ch_emission = {x.get("emissionType") for x in children}
        if si.get("status") != "APPLIED":
            problems.append(f"{set_code[-6:]}: набор в статусе {si.get('status')}, нужен APPLIED")
        if len(ch_status) != 1:
            problems.append(f"{set_code[-6:]}: вложения в разных статусах {ch_status}")
        elif ch_status == {"INTRODUCED"} and si.get("emissionType") != "LOCAL":
            problems.append(f"{set_code[-6:]}: вложения INTRODUCED, но набор {si.get('emissionType')}, нужен LOCAL")
        elif ch_status == {"APPLIED"} and (len(ch_emission) != 1 or ch_emission != {si.get("emissionType")}):
            problems.append(f"{set_code[-6:]}: вложения APPLIED, типы эмиссии {ch_emission} ≠ набор {si.get('emissionType')}")
        elif ch_status not in ({"INTRODUCED"}, {"APPLIED"}):
            problems.append(f"{set_code[-6:]}: вложения в статусе {ch_status}")
        gtins = sorted(x.get("gtin", "?") for x in children)
        print(f"набор {set_code[-8:]} [{si.get('status')}/{si.get('emissionType')}] ← {len(children)} вложений {gtins} "
              f"[{'/'.join(sorted(s or '?' for s in ch_status))}]")
    if problems:
        print("\n".join("ПРОБЛЕМА " + p for p in problems))
        if not args.force:
            sys.exit("документ не отправлен: исправьте статусы или используйте --force")
    body = {
        "participantId": src.get("participantId") or c["inn"],
        "aggregationUnits": [{
            "unitSerialNumber": u["unitSerialNumber"],
            "unitSerialNumberType": "PRODUCT_SET",
            "aggregationType": "SET",
            "sntins": u["sntins"],
        } for u in units],
    }
    st, r = true_document(c, "AGGREGATION_DOCUMENT", body, args.dry_run)
    if st is None:
        return
    if st not in (200, 201) or not r:
        sys.exit(f"documents/create: HTTP {st}: {r}")
    doc_id = r if isinstance(r, str) else (r.get("value") or r.get("id") or r)
    print(f"документ принят: {doc_id} — статус: doc-status --doc {doc_id}")
    journal("set", contour=c["name"], doc_id=str(doc_id), sets=len(units), file=args.file)


def cmd_doc_status(c: dict, args) -> None:
    token = get_true_token(c)
    st, r = http("GET", f"{c['true_v4']}/doc/{args.doc}/info?pg={c['pg']}&body=false",
                 headers={"Authorization": f"Bearer {token}"})
    if st != 200:
        sys.exit(f"doc/info: HTTP {st}: {r}")
    items = r if isinstance(r, list) else [r]   # v4 отвечает списком из одного документа
    for d in items:
        if not isinstance(d, dict):
            print(d)
            continue
        keep = {k: v for k, v in d.items() if k in
                ("number", "docDate", "type", "status", "errors", "docErrors", "downloadStatus", "total", "senderName")}
        print(json.dumps(keep, ensure_ascii=False, indent=1)[:3000])
        print("итог:", "УСПЕХ (CHECKED_OK)" if d.get("status") == "CHECKED_OK" else d.get("status"))


def cmd_auth(c: dict, args) -> None:
    t = get_true_token(c, force=args.force)
    exp = json.load(open(token_path(c, "true")))["expires"]
    print(f"True API [{c['name']}]: токен получен, длина {len(t)}, действует до {exp}")
    if args.suz:
        s = get_suz_token(c, force=args.force)
        exp = json.load(open(token_path(c, "suz")))["expires"]
        print(f"СУЗ [{c['name']}]: clientToken получен, длина {len(s)}, действует до {exp}")
    else:
        print("СУЗ: пропущено (добавьте --suz). Внимание: первый динамический токен навсегда отключает статические токены устройства.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", choices=list(CONTOURS), help="контур; по умолчанию CHZ_ENV из .env (sandbox)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auth", help="получить токены")
    a.add_argument("--suz", action="store_true", help="также получить clientToken СУЗ")
    a.add_argument("--force", action="store_true", help="не использовать кэш токенов")
    s = sub.add_parser("status", help="статусы кодов через cises/info")
    s.add_argument("codes", nargs="*", help="коды маркировки")
    s.add_argument("--file", help="txt (по строке), json (uit_code/sntins) или xlsx-выгрузка ЛК")
    o = sub.add_parser("orders", help="список заказов КМ в СУЗ (чтение)")
    o.add_argument("--last", type=int, default=15)
    p = sub.add_parser("product", help="карточки НК по GTIN (чтение)")
    p.add_argument("gtins", nargs="+")
    p.add_argument("--raw", action="store_true")
    od = sub.add_parser("order", help="ЗАПИСЬ: заказ кодов в СУЗ")
    od.add_argument("--gtin", required=True)
    od.add_argument("--qty", type=int, required=True)
    od.add_argument("--set", action="store_true", help="коды набора (cisType=SET), иначе UNIT")
    od.add_argument("--imported", action="store_true", help="releaseMethodType=IMPORT вместо PRODUCTION")
    od.add_argument("--template", type=int, default=TEMPLATE_ID_CHEMISTRY)
    od.add_argument("--dry-run", action="store_true")
    od.add_argument("--confirm", action="store_true", help="обязателен на боевом контуре")
    os_ = sub.add_parser("order-status", help="статус буфера кодов заказа (чтение)")
    os_.add_argument("--order", required=True)
    os_.add_argument("--gtin")
    cd = sub.add_parser("codes", help="ЗАПИСЬ: забрать коды из буфера заказа в файл")
    cd.add_argument("--order", required=True)
    cd.add_argument("--gtin", required=True)
    cd.add_argument("--qty", type=int, required=True)
    cd.add_argument("--confirm", action="store_true")
    u = sub.add_parser("utilisation", help="ЗАПИСЬ: отчёт о нанесении по файлу кодов")
    u.add_argument("--file", required=True, help="файл из команды codes")
    u.add_argument("--production-date", required=True, help="YYYY-MM-DD, с упаковки")
    u.add_argument("--expiration-date", required=True, help="YYYY-MM-DD, с упаковки")
    u.add_argument("--alcohol", default="0", help="объёмная доля этилового спирта, %%; 0 если нет")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--confirm", action="store_true")
    rs = sub.add_parser("report-status", help="статус обработки отчёта СУЗ (чтение)")
    rs.add_argument("--report", required=True)
    se = sub.add_parser("set", help="ЗАПИСЬ: формирование наборов (AGGREGATION_DOCUMENT) из JSON")
    se.add_argument("--file", required=True, help="JSON с participantId и aggregationUnits (экспорт ЛК подходит)")
    se.add_argument("--dry-run", action="store_true")
    se.add_argument("--force", action="store_true", help="отправить даже при найденных проблемах статусов")
    se.add_argument("--confirm", action="store_true")
    ds = sub.add_parser("doc-status", help="статус документа ГИС МТ (чтение)")
    ds.add_argument("--doc", required=True)
    args = ap.parse_args()

    env = load_env()
    name = args.env or env.get("CHZ_ENV", "sandbox")
    c = contour(env, name)
    if name == "prod":
        print("контур: БОЕВОЙ (markirovka.crpt.ru)", file=sys.stderr)
    handlers = {"auth": cmd_auth, "status": cmd_status, "orders": cmd_orders, "product": cmd_product,
                "order": cmd_order, "order-status": cmd_order_status, "codes": cmd_codes,
                "utilisation": cmd_utilisation, "report-status": cmd_report_status,
                "set": cmd_set, "doc-status": cmd_doc_status}
    handlers[args.cmd](c, args)


if __name__ == "__main__":
    main()
