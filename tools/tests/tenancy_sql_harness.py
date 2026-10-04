"""Исполнение представлений пакета SQL арендатора в sqlite на синтетических строках.

Шаблон разбирается как BigQuery, проект и датасет отбрасываются (таблицы — по имени объекта),
запрос транспилируется в sqlite. Проверяется логика одного представления над его прямыми
входами: входы — заглушки-таблицы или результаты ранее исполненных представлений
(`materialize`). DATE_SUB(d, INTERVAL n DAY) исполняется функцией стенда. Конструкции без
аналога в sqlite (UNNEST литералов, DATE(ts, tz)) в проверяемые представления не входят —
их входы подаются заглушками.
"""
from __future__ import annotations

import sqlite3
import re
import json
from datetime import date, timedelta
from pathlib import Path

import sqlglot
from sqlglot import exp

REPO = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO / "sql" / "tenant" / "ozon"


def view_query(ds: str, name: str) -> str:
    sql = (PACKAGE_DIR / ds / f"{name}.sql").read_text(encoding="utf-8")
    q = sqlglot.parse_one(sql, read="bigquery").expression
    for t in q.find_all(exp.Table):
        t.set("catalog", None)
        t.set("db", None)
    for d in list(q.find_all(exp.DateSub)):
        assert (d.unit.name if d.unit else "DAY").upper() == "DAY", d.sql()
        d.replace(exp.Anonymous(this="DATE_SUB_DAYS", expressions=[d.this, d.expression]))
    for j in list(q.find_all(exp.JSONExtract)):
        j.replace(exp.Anonymous(this="BQ_JSON_QUERY", expressions=[j.this, exp.Literal.string(j.expression.sql(dialect='bigquery').strip("'"))]))
    for j in list(q.find_all(exp.JSONExtractScalar)):
        j.replace(exp.Anonymous(this="BQ_JSON_VALUE", expressions=[j.this, exp.Literal.string(j.expression.sql(dialect='bigquery').strip("'"))]))
    return q.sql(dialect="sqlite")


def _date_sub_days(d, n):
    return None if d is None else (date.fromisoformat(d) - timedelta(days=int(n))).isoformat()


def database(tables: dict[str, tuple[list[str], list[tuple]]]) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.create_function("DATE_SUB_DAYS", 2, _date_sub_days)
    db.create_function("regexp", 2, lambda pattern, value: value is not None and re.search(pattern, value) is not None)
    # Preserve BigQuery JSON_QUERY's distinction between boolean true and string "true".
    def json_query(value, path):
        try:
            obj = json.loads(value)
            for key in path.removeprefix('$.').split('.'):
                obj = obj[key]
            return json.dumps(obj, separators=(',', ':')) if obj is not None else None
        except (ValueError, TypeError, KeyError):
            return None
    db.create_function("BQ_JSON_QUERY", 2, json_query)
    def json_value(value, path):
        text = json_query(value, path)
        if text is None:
            return None
        obj = json.loads(text)
        if isinstance(obj, (dict, list)):
            return None
        return obj if isinstance(obj, str) else text
    db.create_function("BQ_JSON_VALUE", 2, json_value)
    for tname, (cols, rows) in tables.items():
        db.execute(f'CREATE TABLE "{tname}" ({", ".join(cols)})')
        if rows:
            db.executemany(f'INSERT INTO "{tname}" VALUES ({", ".join("?" * len(cols))})', rows)
    return db


def query(db: sqlite3.Connection, ds: str, name: str) -> list[dict]:
    cur = db.execute(view_query(ds, name))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def materialize(db: sqlite3.Connection, ds: str, name: str) -> None:
    db.execute(f'CREATE TABLE "{name}" AS {view_query(ds, name)}')


def run(ds: str, name: str, tables: dict[str, tuple[list[str], list[tuple]]]) -> list[dict]:
    return query(database(tables), ds, name)
