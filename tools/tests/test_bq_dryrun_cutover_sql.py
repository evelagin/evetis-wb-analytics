"""Этап C, ревью #426: офлайн-проверки скрипта dry-run SQL координатора (сеть не используется).

Сам прогон через парсер BigQuery — ручной (нужен токен владельца): tools/bq_dryrun_cutover_sql.py,
результат — docs/ops/evidence/INGEST_CUTOVER_SQL_DRYRUN_2026-11-11.txt.
"""
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "tools/bq_dryrun_cutover_sql.py"
spec = importlib.util.spec_from_file_location("bq_dryrun_cutover_sql", SRC)
dr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dr)
DDL = (REPO / "sql/ops/ingest_writer_ownership.sql").read_text(encoding="utf-8")


def test_only_dry_run_ever():
    text = SRC.read_text(encoding="utf-8")
    assert text.count('"dryRun": True') == 1 and 'assert body["dryRun"] is True' in text
    assert 'ENDPOINT = f"https://www.googleapis.com/bigquery/v2/projects/{PROJECT}/queries"' in text
    assert "https://bigquery.googleapis.com" not in text
    assert not re.search(r"jobs\.insert|/jobs\b|insertAll", text)


def test_schemas_come_from_ddl():
    s = dr.ddl_schemas(DDL)
    assert "transition_at TIMESTAMP" in s["INGEST_CUTOVER_LOG"] and "verdict_at TIMESTAMP" in s["INGEST_QUALIFICATION"]
    assert "checks_since TIMESTAMP" in s["INGEST_WRITER_OWNERSHIP"]


def test_plan_modes_and_proxies():
    stmts = [
        {"name": "sel", "sql": "SELECT owner FROM `p.wb_ops.INGEST_WRITER_OWNERSHIP` WHERE stream = @s", "params": []},
        {"name": "raw", "sql": "SELECT 1 FROM `p.wb_raw.LOADER_RUNS`", "params": []},
        {"name": "cas", "sql": "BEGIN\n  MERGE `p.wb_ops.INGEST_WRITER_OWNERSHIP` T\n  USING (SELECT @stream AS stream) S\n  ON T.stream = S.stream\n"
                               "  WHEN MATCHED AND T.epoch = @epoch THEN UPDATE SET\n    owner = @owner, state = @to_state;\n"
                               "  INSERT INTO `p.wb_ops.INGEST_CUTOVER_LOG` (transition_id, stream) VALUES (@t, @stream);\nEND", "params": []},
    ]
    jobs = {(n, m) for n, m, _, _ in dr.plan(stmts, DDL)}
    assert ("sel", "CTE") in jobs and ("raw", "FULL") in jobs and ("cas", "SCRIPT") in jobs
    assert ("cas#proxy1", "PROXY") in jobs and ("cas#proxy2", "PROXY") in jobs
    sql = {n: q for n, _, q, _ in dr.plan(stmts, DDL)}
    assert "UNNEST(ARRAY<STRUCT<stream STRING" in sql["sel"] and "wb_ops.INGEST" not in sql["sel"]
    assert sql["cas#proxy1"].startswith("SELECT @owner AS owner, @to_state AS state FROM")
    assert any(n.startswith("ddl.create.") for n, _ in jobs) and ("ddl.file", "SCRIPT") in jobs


def test_schema_problems_catch_unknown_columns():
    bad = "INSERT INTO `p.wb_ops.INGEST_CUTOVER_LOG` (transition_id, at) VALUES (1, 2);"
    assert dr.schema_problems(DDL, bad) == ["INGEST_CUTOVER_LOG: нет колонки at"]
    assert dr.schema_problems(DDL, "MERGE `p.wb_ops.INGEST_WRITER_OWNERSHIP` T USING x ON y WHEN MATCHED THEN UPDATE SET nope = 1;") == [
        "INGEST_WRITER_OWNERSHIP: нет колонки nope"]


def test_requires_token_and_never_prints_it(tmp_path):
    f = tmp_path / "s.json"
    f.write_text("[]")
    r = subprocess.run([sys.executable, str(SRC), str(f)], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 2 and "BQ_TOKEN" in r.stderr
