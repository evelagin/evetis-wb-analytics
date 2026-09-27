#!/usr/bin/env python3
"""AIE V1 — рендер запросов к Advertising Intelligence Engine до развёртывания и для replay.

Зачем. Представления AIE (wb_mart.V_AIE_WB_*, ozon_mart.V_AIE_OZON_*, evetis_mart.V_AIE_DECISION_CURRENT)
в production не развёрнуты (развёртывание — отдельный ACK). Проверять их и воспроизводить историю нужно
ТЕМИ ЖЕ телами из Git: вторая реализация логики на Python ничего не доказывает. Инструмент — чистое
преобразование текста; сеть нужна только режимам run / replay и только через tools/lib/bq_readonly
(SELECT / WITH, всё прочее отклоняется до отправки).

Режимы:

  predeploy <query.sql>        ссылки на AIE-представления заменяются их телами из Git (рекурсивно),
                               остальные объекты production остаются живыми. Файл с блоками
                               `-- @check` рендерится поблочно.
  run predeploy <checks.sql>   исполнить блоки `-- @check` после predeploy-рендера. 0 — все PASS,
                               1 — FAIL/EMPTY, 3 — ошибка.

Replay (используется tools/aie_backtest.py) — render_replay(): в телах AIE заменяются
  1) CTE между маркерами `@aie:clock` — as_of_date = D, knowledge_ts = D+1 09:00 МСК, run_mode = 'REPLAY';
  2) CTE между маркерами `@aie:policy` — сетка кандидатов политик (не решение владельца);
  3) CTE между маркерами `@aie:diag` — множители диагностики (P15, чувствительность);
  4) wb_mart.FACT_ADS_SKU_DAILY и wb_raw.V_ADV_CAMPAIGN_STATS — та же логика сборки
     (sql/mart/pr_mart1_facts.sql §1.5 и живое тело дедуп-вью) поверх RAW с load_ts ≤ knowledge_ts;
     load_ts пишется Apps Script по МСК без пояса;
  5) цепочка запросов Ads-4 (V_ADS_FUNNEL_QUERY_28D → _DAILY → V_ADV_QUERY_STATS / V_ADV_QUERY_BIDS) —
     живые тела с RAW, отфильтрованным по load_ts ≤ knowledge_ts, и днём ≤ as_of;
  6) объекты только текущего состояния — та же схема, ноль строк (`WHERE FALSE`).

usage:
  python tools/aie_render.py predeploy <query.sql>
  python tools/aie_render.py run predeploy <checks.sql> --project <P> --token-command "gcloud auth print-access-token"
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "project-fa311fc0-4d87-4781-986"
FACTS_SQL = ROOT / "sql" / "mart" / "pr_mart1_facts.sql"

# Порядок = порядок зависимостей. Файлы, которых ещё нет (поэтапная реализация), пропускаются.
OBJECTS: list[tuple[str, str, str]] = [
    ("evetis_ref", "V_AIE_POLICY", "sql/ads_intel/evetis_ref"),
    ("wb_mart", "V_AIE_WB_PAIR_EVIDENCE", "sql/ads_intel/wb_mart"),
    ("wb_mart", "V_AIE_WB_ECON_GUARD", "sql/ads_intel/wb_mart"),
    ("wb_mart", "V_AIE_WB_QUERY_CLASS", "sql/ads_intel/wb_mart"),
    ("ozon_mart", "V_AIE_OZON_PAIR_EVIDENCE", "sql/current/ozon_mart"),
    ("ozon_mart", "V_AIE_OZON_ECON_GUARD", "sql/current/ozon_mart"),
    ("evetis_mart", "V_AIE_DECISION_CURRENT", "sql/current/evetis_mart"),
]

# Объекты, которые существуют только в текущем состоянии: в replay — та же схема, ноль строк.
CURRENT_ONLY: list[str] = [
    "wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT",
    "wb_mart.V_DATA_FRESHNESS",
    "ozon_mart.V_OZON_AGENT_DECISION_INPUT",
    "ozon_mart.V_OZON_MART_FRESHNESS",
]

# Цепочка запросов Ads-4: живые тела подставляются в replay (см. docstring, п. 5).
QUERY_CHAIN: list[str] = [
    "wb_mart.V_ADS_FUNNEL_QUERY_28D",
    "wb_mart.V_ADS_FUNNEL_QUERY_DAILY",
    "wb_raw.V_ADV_QUERY_STATS",
    "wb_raw.V_ADV_QUERY_BIDS",
]
# RAW, которые только дописываются и несут load_ts: фильтр «известно к knowledge_ts».
PIT_RAW: list[str] = [
    "wb_raw.RAW_WB_ADV_CAMPAIGN_STATS",
    "wb_raw.RAW_WB_ADV_QUERY_STATS",
    "wb_raw.RAW_WB_ADV_QUERY_STATS_RUNS",
    "wb_raw.RAW_WB_ADV_QUERY_BIDS",
    "wb_raw.RAW_WB_ADV_QUERY_BIDS_RUNS",
]

MARKER = re.compile(r"-- @aie:(?P<name>clock|policy|diag):begin\n.*?-- @aie:(?P=name):end\n", re.S)


def ref(dataset_object: str) -> str:
    return f"`{PROJECT}.{dataset_object}`"


def view_body(sql: str) -> str:
    head, sep, body = sql.partition("\nAS\n")
    if not sep or "CREATE OR REPLACE VIEW" not in head:
        raise SystemExit("не найдено тело CREATE OR REPLACE VIEW … AS")
    return body.strip().rstrip(";").strip()


def aie_bodies(root: Path = ROOT) -> dict[str, str]:
    """{`project.dataset.object`: тело из Git} для каждого существующего файла AIE."""
    out = {}
    for dataset, name, folder in OBJECTS:
        path = root / folder / f"{name}.sql"
        if path.exists():
            out[ref(f"{dataset}.{name}")] = view_body(path.read_text(encoding="utf-8"))
    return out


def inline(sql: str, bodies: dict[str, str]) -> str:
    for _ in range(len(bodies) + 2):
        hits = [r for r in bodies if r in sql]
        if not hits:
            return sql
        for r in hits:
            sql = sql.replace(r, f"(\n{bodies[r]}\n)")
    raise SystemExit("объекты ссылаются друг на друга циклически")


def render_predeploy(sql: str) -> str:
    return inline(sql, aie_bodies())


# ── блоки проверок ─────────────────────────────────────────────────────────────
def split_blocks(text: str) -> list[tuple[str, str]]:
    parts = re.split(r"^-- @check ", text, flags=re.M)
    out = []
    for part in parts[1:]:
        cid, _, rest = part.partition("\n")
        lines = rest.strip().splitlines()
        while lines and lines[0].startswith("--"):
            lines.pop(0)
        out.append((cid.strip(), "\n".join(lines).strip().rstrip(";").strip()))
    return out


def render_predeploy_file(text: str) -> str:
    blocks = split_blocks(text)
    if not blocks:
        return render_predeploy(text)
    return "\n\n".join(f"-- @check {cid}\n{render_predeploy(sql)};" for cid, sql in blocks) + "\n"


# ── replay ─────────────────────────────────────────────────────────────────────
def knowledge_ts(as_of: dt.date) -> str:
    """Решение по дню D принимается D+1 в 09:00 МСК: реклама загружена в 05:11, витрина — в 07:00."""
    return f"TIMESTAMP('{as_of + dt.timedelta(days=1)} 09:00:00', 'Europe/Moscow')"


def clock_cte(as_of: dt.date) -> str:
    return (
        "-- @aie:clock:begin\n"
        "aie_clock AS (\n"
        f"  SELECT DATE '{as_of}' AS as_of_date,\n"
        f"         {knowledge_ts(as_of)} AS knowledge_ts,\n"
        "         'REPLAY' AS run_mode\n"
        "),\n"
        "-- @aie:clock:end\n"
    )


def replace_marker(sql: str, name: str, replacement: str) -> str:
    pat = re.compile(rf"-- @aie:{name}:begin\n.*?-- @aie:{name}:end\n", re.S)
    if not pat.search(sql):
        return sql
    return pat.sub(lambda _m: replacement, sql)


def pit_raw(sql: str, as_of: dt.date) -> str:
    """RAW с load_ts (МСК без пояса) ≤ knowledge_ts."""
    k = knowledge_ts(as_of)
    for raw in PIT_RAW:
        sql = sql.replace(
            ref(raw),
            f"(SELECT * FROM {ref(raw)} WHERE TIMESTAMP(SAFE_CAST(load_ts AS DATETIME), 'Europe/Moscow') <= {k})",
        )
    return sql


def fact_ads_build_select() -> str:
    """Тело сборки FACT_ADS_SKU_DAILY из sql/mart/pr_mart1_facts.sql §1.5 — источник истины, не копия."""
    text = FACTS_SQL.read_text(encoding="utf-8")
    m = re.search(
        r"CREATE OR REPLACE TABLE `wb_mart\.FACT_ADS_SKU_DAILY__BUILD`.*?\bAS\n(?P<body>.*?)\n\s*\"\"\"",
        text, re.S)
    if not m:
        raise SystemExit("не найдена сборка FACT_ADS_SKU_DAILY в sql/mart/pr_mart1_facts.sql")
    body = m.group("body")
    body = body.replace("@run_id AS mart_run_id, @built_at AS built_at",
                        "CAST(NULL AS STRING) AS mart_run_id, CAST(NULL AS TIMESTAMP) AS built_at")
    if "@" in body:
        raise SystemExit("в сборке FACT_ADS_SKU_DAILY остались параметры процедуры")
    return body


def pit_campaign_stats(live: dict[str, str], as_of: dt.date) -> str:
    return "(\n" + pit_raw(live["wb_raw.V_ADV_CAMPAIGN_STATS"], as_of) + "\n)"


def pit_fact_ads(live: dict[str, str], as_of: dt.date) -> str:
    body = fact_ads_build_select().replace("`wb_raw.V_ADV_CAMPAIGN_STATS`", pit_campaign_stats(live, as_of))
    return "(\n" + body + "\n)"


def pit_query_chain(live: dict[str, str], as_of: dt.date) -> dict[str, str]:
    """Цепочка Ads-4 по запросам на дату: RAW по load_ts, день ≤ as_of."""
    stats = "(\n" + pit_raw(live["wb_raw.V_ADV_QUERY_STATS"], as_of) + "\n)"
    bids = "(\n" + pit_raw(live["wb_raw.V_ADV_QUERY_BIDS"], as_of) + "\n)"
    daily_body = live["wb_mart.V_ADS_FUNNEL_QUERY_DAILY"].replace(ref("wb_raw.V_ADV_QUERY_STATS"), stats)
    daily = f"(\nSELECT * FROM (\n{daily_body}\n) WHERE day <= DATE '{as_of}'\n)"
    q28 = live["wb_mart.V_ADS_FUNNEL_QUERY_28D"]
    q28 = q28.replace(ref("wb_mart.V_ADS_FUNNEL_QUERY_DAILY"), daily).replace(ref("wb_raw.V_ADV_QUERY_BIDS"), bids)
    return {ref("wb_mart.V_ADS_FUNNEL_QUERY_28D"): "(\n" + q28 + "\n)"}


def render_replay(sql: str, as_of: dt.date, live: dict[str, str], policy_cte: str | None = None,
                  diag_cte: str | None = None) -> str:
    """Запрос к AIE на дату as_of без заглядывания вперёд (см. docstring).

    Источники «на дату» вынесены в CTE верхнего уровня (aie_pit_*): тяжёлый текст сборки FACT не
    размножается по встроенным представлениям, и планировщик BigQuery укладывается в лимит.
    """
    if re.match(r"\s*WITH\b", sql, re.I):
        raise SystemExit("render_replay ожидает запрос без собственного WITH верхнего уровня")
    bodies = {}
    for r, b in aie_bodies().items():
        b = replace_marker(b, "clock", clock_cte(as_of))
        if policy_cte is not None:
            b = replace_marker(b, "policy", policy_cte)
        if diag_cte is not None:
            b = replace_marker(b, "diag", diag_cte)
        bodies[r] = b
    out = inline(sql, bodies)
    ctes = {
        "aie_pit_fact_ads_sku_daily": (ref("wb_mart.FACT_ADS_SKU_DAILY"), pit_fact_ads(live, as_of)),
        "aie_pit_adv_campaign_stats": (ref("wb_raw.V_ADV_CAMPAIGN_STATS"), pit_campaign_stats(live, as_of)),
    }
    for r, body in pit_query_chain(live, as_of).items():
        ctes["aie_pit_ads_funnel_query_28d"] = (r, body)
    used = []
    for name, (r, body) in ctes.items():
        if r in out:
            out = out.replace(r, name)
            used.append(f"{name} AS {body}")
    for obj in CURRENT_ONLY:
        out = out.replace(ref(obj), f"(SELECT * FROM {ref(obj)} WHERE FALSE)")
    return ("WITH " + ",\n".join(used) + "\n" + out) if used else out


POLICY_FILE = ROOT / "sql" / "ads_intel" / "evetis_ref" / "V_AIE_POLICY.sql"
POLICY_KEYS = {"policy_id": "policy_id", "p1_reserve_share": "p1", "p2_band_pp": "p2", "p3_confidence": "p3",
               "p5_low_cover_days": "p5_low", "p5_overstock_cover_days": "p5_overstock",
               "p7_price_change_pct": "p7", "p13_cooldown_days": "p13", "k_inactive_days": "k"}


def git_policy() -> dict:
    """Утверждённая политика из Git (блок @aie:policy в V_AIE_POLICY): ключи p1…k, NULL → None."""
    text = POLICY_FILE.read_text(encoding="utf-8")
    m = re.search(r"-- @aie:policy:begin\n(.*?)-- @aie:policy:end\n", text, re.S)
    if not m:
        raise SystemExit("в V_AIE_POLICY нет блока @aie:policy")
    out = {}
    for value, kind, name in re.findall(r"CAST\((.*?) AS (FLOAT64|STRING|INT64)\) AS (\w+)", m.group(1)):
        v = value.strip()
        if v == "NULL":
            val = None
        elif kind == "STRING":
            val = v.strip("'")
        else:
            val = int(v) if kind == "INT64" else float(v)
        out[POLICY_KEYS[name]] = val
    if set(out) != set(POLICY_KEYS.values()):
        raise SystemExit(f"состав политики не совпадает с контрактом: {sorted(out)}")
    return out


def policy_grid_cte(policies: list[dict]) -> str:
    """CTE сетки кандидатов политик для replay (заменяет блок политики V_AIE_POLICY).

    Анализ чувствительности, не решение владельца. Незаданный ключ — NULL, как в V_AIE_POLICY:
    скрытых значений по умолчанию нет.
    """
    cols = [("policy_id", "policy_id", "STRING"), ("p1", "p1_reserve_share", "FLOAT64"),
            ("p2", "p2_band_pp", "FLOAT64"), ("p3", "p3_confidence", "FLOAT64"),
            ("p5_low", "p5_low_cover_days", "FLOAT64"), ("p5_overstock", "p5_overstock_cover_days", "FLOAT64"),
            ("p7", "p7_price_change_pct", "FLOAT64"), ("p13", "p13_cooldown_days", "INT64"),
            ("k", "k_inactive_days", "INT64")]

    def lit(v, kind):
        if v is None:
            return f"CAST(NULL AS {kind})"
        if kind == "STRING":
            return f"'{v}'"
        return str(int(v)) if kind == "INT64" else repr(float(v))
    rows = ",\n    ".join("(" + ", ".join(lit(p.get(k), kind) for k, _, kind in cols) + ")" for p in policies)
    struct = ", ".join(f"{name} {kind}" for _, name, kind in cols)
    return (
        "-- @aie:policy:begin\n"
        "aie_policy AS (\n"
        f"  SELECT * FROM UNNEST(ARRAY<STRUCT<{struct}>>[\n"
        f"    {rows}\n"
        "  ])\n"
        "),\n"
        "-- @aie:policy:end\n"
    )


def diag_grid_cte(pairs: list[tuple[float, float]]) -> str:
    rows = ", ".join(f"({lo!r}, {hi!r}, 'SENSITIVITY_P15')" for lo, hi in pairs)
    return (
        "-- @aie:diag:begin\n"
        "aie_diag_params AS (\n"
        "  SELECT * FROM UNNEST(ARRAY<STRUCT<below_mult FLOAT64, above_mult FLOAT64, mult_source STRING>>["
        f"{rows}])\n"
        "),\n"
        "-- @aie:diag:end\n"
    )


LIVE_OBJECTS = ["wb_raw.V_ADV_CAMPAIGN_STATS", *QUERY_CHAIN]


def fetch_live_bodies(bq) -> dict[str, str]:
    """Живые тела объектов, которые replay подставляет (read-only INFORMATION_SCHEMA.VIEWS)."""
    out = {}
    for ds in sorted({o.split(".")[0] for o in LIVE_OBJECTS}):
        names = ", ".join(f"'{o.split('.')[1]}'" for o in LIVE_OBJECTS if o.startswith(ds + "."))
        rows = bq.query(f"SELECT table_name, view_definition FROM `{PROJECT}.{ds}.INFORMATION_SCHEMA.VIEWS` "
                        f"WHERE table_name IN ({names})")
        for r in rows:
            out[f"{ds}.{r['table_name']}"] = r["view_definition"]
    missing = [o for o in LIVE_OBJECTS if o not in out]
    if missing:
        raise SystemExit(f"нет живых тел: {missing}")
    return out


# ── манифест sql/current (pending_deploy) ─────────────────────────────────────
BQ_TYPE = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}


def dry_run_schema(bq, sql: str) -> list[dict]:
    """Схема выхода по dry-run (read-only). Это заявление автора до развёртывания: unverified."""
    import json as _json
    import urllib.request
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lib.bq_readonly import assert_read_only  # noqa: E402
    assert_read_only(sql)
    body = {"query": sql, "useLegacySql": False, "location": bq.location, "dryRun": True,
            "labels": {"purpose": bq.label_purpose}}
    req = urllib.request.Request(
        f"https://{bq.host}/bigquery/v2/projects/{bq.project}/queries",
        data=_json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {bq.token}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=bq.timeout) as resp:
        payload = _json.loads(resp.read())
    cols = []
    for i, f in enumerate(payload["schema"]["fields"], start=1):
        dtype = BQ_TYPE.get(f["type"], f["type"])
        if f.get("mode") == "REPEATED":
            dtype = f"ARRAY<{dtype}>"
        cols.append({"column_name": f["name"], "data_type": dtype, "is_nullable": "YES", "ordinal_position": i})
    return cols


def manifest_entry(bq, dataset: str, name: str, source: str) -> dict:
    """Запись MANIFEST.json для нового объекта: pending_deploy, снимок production = null."""
    import validate_current_sql as V
    path = ROOT / "sql" / "current" / dataset / f"{name}.sql"
    sql = path.read_text(encoding="utf-8")
    findings: list = []
    facts = V.analyze_sql(sql, name, findings)
    if findings:
        raise SystemExit(f"{name}: {[f.reason for f in findings]}")
    refs = sorted(facts.references)
    deps = sorted({n for (_p, d, n) in refs if d == dataset})
    ext = sorted({f"{d}.{n}" for (_p, d, n) in refs if d != dataset})
    schema = dry_run_schema(bq, render_predeploy(f"SELECT * FROM {ref(f'{dataset}.{name}')}"))
    return {
        "dataset": dataset, "object_name": name, "object_type": "VIEW",
        "canonical_path": f"sql/current/{dataset}/{name}.sql", "sync_state": "pending_deploy",
        "dependencies": deps, "external_dependencies": ext, "dependency_level": None,
        "capture_main_sha": None, "captured_at": None,
        "provenance": {"source": source, "historical_migration": None, "preflight_parity": None,
                       "superseded_historical_definitions": []},
        "live_body_sha256_at_capture": None, "live_schema_sha256_at_capture": None,
        "live_description_sha256_at_capture": None, "live_schema_at_capture": None,
        "canonical_body_sha256": V.sha256_text(facts.body),
        "canonical_description_sha256": V.sha256_text(facts.description),
        "canonical_schema_sha256": V.schema_sha256(schema),
        "canonical_schema_verification": "unverified",
        "canonical_column_count": len(schema),
        "canonical_schema": schema,
    }


# ── исполнение ─────────────────────────────────────────────────────────────────
def make_bq(project: str, token_command: str | None, token_env: str | None, purpose: str):
    import os
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lib.bq_readonly import ReadOnlyBigQuery, resolve_token  # noqa: E402
    return ReadOnlyBigQuery(project=project, token=resolve_token(token_env, token_command, os.environ),
                            label_purpose=purpose, timeout=300)


def run_blocks(blocks: list[tuple[str, str]], bq) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lib.bq_readonly import BigQueryError  # noqa: E402
    worst = 0
    for cid, sql in blocks:
        try:
            rows = bq.query(sql)
        except BigQueryError as e:
            print(f"{cid}\tERROR\t{str(e)[:400]}")
            worst = 3
            continue
        bad = [r for r in rows if r.get("status") != "PASS"]
        verdict = "EMPTY" if not rows else ("PASS" if not bad else "FAIL")
        print(f"{cid}\t{verdict}\t{len(rows)} rows")
        for r in bad[:5]:
            print("   ", {k: v for k, v in r.items()})
        if verdict != "PASS":
            worst = max(worst, 1)
    print(f"queries={bq.queries_issued} bytes_processed={bq.bytes_billed}")
    return worst


def _opt(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "predeploy":
        sys.stdout.write(render_predeploy_file(Path(argv[1]).read_text(encoding="utf-8")))
        return 0
    if len(argv) >= 3 and argv[0] == "run" and argv[1] == "predeploy":
        project = _opt(argv, "--project")
        if not project:
            sys.stderr.write("run требует --project\n")
            return 2
        blocks = split_blocks(render_predeploy_file(Path(argv[2]).read_text(encoding="utf-8")))
        only = [a for i, a in enumerate(argv) if i > 0 and argv[i - 1] == "--check"]
        if only:
            blocks = [b for b in blocks if b[0] in only]
        bq = make_bq(project, _opt(argv, "--token-command"), _opt(argv, "--token-env"), "aie-predeploy-checks")
        return run_blocks(blocks, bq)
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
