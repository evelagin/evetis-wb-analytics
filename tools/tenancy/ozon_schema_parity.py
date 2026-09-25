#!/usr/bin/env python3
"""Паритет схем Ozon: снимок в Git ↔ DDL в Git ↔ живой EVETIS (Tenancy T3.2).

  python tools/tenancy/ozon_schema_parity.py            # офлайн (CI)
  python tools/tenancy/ozon_schema_parity.py --live     # + живые метаданные EVETIS (только чтение)

Снимки pipelines/ozon/schema/<dataset>/<TABLE>.json — источник таблиц арендатора.
Офлайн: каждый столбец CREATE TABLE из sql/** (где DDL есть) обязан быть в снимке с
тем же типом и режимом; партиционирование и кластеризация — те же. Лишние столбцы
снимка допустимы: их добавляли позже ALTER'ами, живая таблица — истина.
--live: снимок обязан совпасть с живой таблицей EVETIS целиком (имена, типы, режимы,
вложенность, партиционирование, кластеризация). Строки не читаются никогда —
только tables.get метаданных через www.googleapis.com.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.tenancy import ozon_contract as OC  # noqa: E402
from tools.tenancy import platform as PL  # noqa: E402
from tools.tenancy.validation import parse_tenant_json  # noqa: E402

DDL_FILES = ("sql/ozon/stage3_1_ozon_foundation.sql", "sql/promotions/pr_promo1_raw.sql",
             "sql/ref/stage3_1_shared_reference.sql")
LIVE_DATASET = {"ozon_raw": "ozon_raw", "ref": "evetis_ref"}
_TYPES = {"INT64": "INTEGER", "FLOAT64": "FLOAT", "BOOL": "BOOLEAN", "STRUCT": "RECORD"}
_CREATE = re.compile(r"CREATE TABLE IF NOT EXISTS `[^`]*\.(\w+)\.(\w+)`\s*\((.*?)\)\s*\n"
                     r"(?:PARTITION BY ([^\n]*?))?\s*(?:CLUSTER BY ([^\n]*?))?\s*(?:OPTIONS|;)", re.S)


def all_tables() -> list[tuple[str, str]]:
    """Все таблицы, которые runtime может потребовать (все сущности контракта)."""
    return sorted((ds, t) for ds, names in OC.tables_for(OC.ENTITY_TABLES).items() for t in names)


def _split_top(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        depth += ch in "<("
        depth -= ch in ">)"
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return [c.strip() for c in out + [cur]
            if c.strip() and not re.match(r"(PRIMARY|FOREIGN|CONSTRAINT)\b", c.strip(), re.I)]


def _ddl_column(col: str) -> tuple[str, str, str]:
    name, rest = col.split(None, 1)
    required = "NOT NULL" in rest.upper()
    t = re.sub(r"\s+NOT NULL.*", "", rest, flags=re.I).strip()
    t = re.sub(r"\s+(DEFAULT|OPTIONS)\b.*", "", t, flags=re.I).strip()
    mode = "REQUIRED" if required else "NULLABLE"
    if t.upper().startswith("ARRAY<"):
        t, mode = t[6:-1], "REPEATED"
    base = re.match(r"\w+", t).group(0).upper()
    return name, _TYPES.get(base, base), mode


def ddl_tables() -> dict[tuple[str, str], dict]:
    out = {}
    for f in DDL_FILES:
        text = re.sub(r"--[^\n]*", "", (REPO / f).read_text(encoding="utf-8"))
        for ds, table, cols, part, clus in _CREATE.findall(text):
            key = ("ref" if ds == "evetis_ref" else ds, table)
            out[key] = {"file": f, "columns": [_ddl_column(c) for c in _split_top(cols)],
                        "partition": part.strip() or None,
                        "clustering": [c.strip() for c in clus.split(",")] if clus else []}
    return out


def _flat(fields, pre="") -> dict[str, tuple[str, str]]:
    out = {}
    for f in fields:
        out[pre + f["name"]] = (f["type"], f.get("mode", "NULLABLE"))
        out.update(_flat(f.get("fields", []), pre + f["name"] + "."))
    return out


def offline_findings() -> list[str]:
    findings, ddl = [], ddl_tables()
    for ds, t in all_tables():
        try:
            spec = OC.load_table_spec(ds, t)
        except OC.ContractError as e:
            findings.append(str(e))
            continue
        blob = json.dumps(spec)
        for m in PL.EVETIS_FORBIDDEN_MARKERS:
            if m in blob:
                findings.append(f"{ds}.{t}: снимок содержит идентификатор EVETIS {m!r}")
        if (ds, t) not in ddl:
            continue
        d, have = ddl[(ds, t)], _flat(spec["schema"])
        for name, typ, mode in d["columns"]:
            if name not in have:
                findings.append(f"{ds}.{t}: столбца {name} из {d['file']} нет в снимке")
            elif have[name] != (typ, mode):
                findings.append(f"{ds}.{t}.{name}: DDL {typ}/{mode} ≠ снимок {have[name]}")
        part = (spec.get("time_partitioning") or {}).get("field")
        ddl_part = d["partition"]
        if ddl_part and re.sub(r"^DATE\((\w+)\)$", r"\1", ddl_part) != part:
            findings.append(f"{ds}.{t}: партиционирование DDL {ddl_part!r} ≠ снимок {part!r}")
        if d["clustering"] and d["clustering"] != spec.get("clustering", []):
            findings.append(f"{ds}.{t}: кластеризация DDL {d['clustering']} ≠ снимок {spec.get('clustering')}")
    return findings


def live_findings() -> list[str]:
    token = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True,
                           check=True).stdout.strip()
    findings = []
    for ds, t in all_tables():
        url = (f"https://www.googleapis.com/bigquery/v2/projects/{PL.EVETIS_PROJECT_ID}/datasets/"
               f"{LIVE_DATASET[ds]}/tables/{t}?fields=schema,timePartitioning,clustering")
        live = parse_tenant_json(urllib.request.urlopen(urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"})).read().decode("utf-8"))
        spec = OC.load_table_spec(ds, t)
        if OC.normalized_fields(live["schema"]["fields"]) != OC.normalized_fields(spec["schema"]):
            a, b = _flat(live["schema"]["fields"]), _flat(spec["schema"])
            findings.append(f"{ds}.{t}: схема расходится с live: только live {sorted(set(a) - set(b))}, "
                            f"только снимок {sorted(set(b) - set(a))}, типы {[k for k in a if k in b and a[k] != b[k]]}")
        if (live.get("timePartitioning") or {}).get("field") != (spec.get("time_partitioning") or {}).get("field"):
            findings.append(f"{ds}.{t}: партиционирование расходится с live")
        if (live.get("clustering") or {}).get("fields", []) != spec.get("clustering", []):
            findings.append(f"{ds}.{t}: кластеризация расходится с live")
    return findings


def main(argv: list[str]) -> int:
    findings = offline_findings()
    checked = f"офлайн: таблиц {len(all_tables())}, с DDL в Git {len(set(ddl_tables()) & set(all_tables()))}"
    if "--live" in argv:
        findings += live_findings()
        checked += f"; live EVETIS: таблиц {len(all_tables())}"
    for f in findings:
        print(f"FAIL {f}")
    print(f"{checked}; нарушений {len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
