"""Аудит production-мутаций со стороны BigQuery. Сам аудит — только чтение.

Доказательство «AE не менял production» берётся не из слов агента, а из журнала заданий:
`INFORMATION_SCHEMA.JOBS_BY_PROJECT`, задания идентичности AE (или метки purpose=autonomy-*)
с `statement_type` ≠ SELECT за окно прогона. Для AE v1 это число обязано быть 0.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.bq_readonly import BigQueryError, ReadOnlyBigQuery, resolve_token  # noqa: E402

AUDIT_SQL = """
SELECT statement_type, COUNT(*) AS n
FROM `{project}.region-eu.INFORMATION_SCHEMA.JOBS_BY_PROJECT`
WHERE creation_time BETWEEN TIMESTAMP('{since}') AND CURRENT_TIMESTAMP()
  AND (user_email IN UNNEST({identities})
       OR EXISTS (SELECT 1 FROM UNNEST(labels) l WHERE l.key = 'purpose' AND STARTS_WITH(l.value, 'autonomy')))
  AND statement_type IS NOT NULL AND statement_type != 'SELECT'
GROUP BY 1
"""


def zero_mutations_proven(run: dict) -> tuple[bool, str]:
    """Машинно проверяемое «0 production-мутаций»: доверенный аудит PASS с начала прогона, покрывающий
    ВСЕ записанные вызовы агента. Нет доказательства, NOT_APPLICABLE, BLOCKED, устаревшее — не ноль."""
    ev = run.get("audit_evidence")
    if not isinstance(ev, dict):
        return False, "нет доверенного аудита production-мутаций"
    if run.get("audit_status") != "PASS" or ev.get("status") != "PASS":
        return False, f"аудит {run.get('audit_status')}, а не PASS"
    if run.get("production_mutations") != 0 or ev.get("mutations") != 0:
        return False, "зафиксированы production-мутации"
    if ev.get("since") != run.get("created_at"):
        return False, "окно аудита начинается не с создания прогона"
    if ev.get("usage_count") != len(run.get("usage", [])):
        return False, "аудит не покрывает последний вызов агента"
    return True, "доверенный аудит: 0 production-мутаций за всё окно прогона"


def count_mutations(project: str, token_command: str, since_iso: str, identities: list[str]) -> dict:
    """Вернуть {"status": PASS|FAIL|BLOCKED, "mutations": int, "by_type": {...}}."""
    try:
        token = resolve_token(None, token_command, {})
        bq = ReadOnlyBigQuery(project=project, token=token, label_purpose="autonomy-audit")
        ids = "[" + ", ".join(f"'{i}'" for i in identities if "'" not in i) + "]" if identities else "ARRAY<STRING>[]"
        rows = bq.query(AUDIT_SQL.format(project=project, since=since_iso.replace("Z", "+00:00"),
                                         identities=ids))
    except BigQueryError as e:
        return {"status": "BLOCKED", "mutations": None, "error": str(e)[:300]}
    by_type = {r["statement_type"]: int(r["n"]) for r in rows}
    total = sum(by_type.values())
    return {"status": "PASS" if total == 0 else "FAIL", "mutations": total, "by_type": by_type}
