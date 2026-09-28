"""WP9 — operator inspection of a v3 SHADOW decision (text only, no actions, no publish button).

Clearly separated from the production Telegram flow: it is only reachable through the
admin API / BigQuery, never sent to the operator chat automatically.
"""
from __future__ import annotations

import json


def _j(v, default):
    try:
        return json.loads(v) if isinstance(v, str) and v else (v if v is not None else default)
    except ValueError:
        return default


def render(row: dict) -> str:
    """Human-readable summary of one communication_v3_decisions row."""
    resolved = _j(row.get("resolved_facts_json"), [])
    ver = _j(row.get("verifier_json"), {}) or {}
    lines = [
        "🧪 v3 SHADOW (не для публикации, только просмотр)",
        f"Снимок знаний: {row.get('knowledge_snapshot_id')} · движок {row.get('engine_version')}",
        f"Продукт: {row.get('product_id') or '—'} ({row.get('product_resolution_status')})",
        f"Риск: {row.get('risk_level')} · домены: {', '.join(_j(row.get('escalation_domains'), [])) or '—'}",
        f"Стратегия: {row.get('strategy')} → итог: {row.get('final_outcome')}"
        + (f" · причина: {row.get('failure_code')}" if row.get("failure_code") else ""),
    ]
    facts = [r for r in resolved if r.get("state")]
    if facts:
        lines.append("Факты:")
        for r in facts:
            lines.append(f"  • {r.get('fact_type')}{'(' + str(r.get('subject')) + ')' if r.get('subject') else ''}"
                         f" [{r.get('product_id')}]: {r.get('state')}"
                         + (f" — {r.get('reason')}" if r.get("reason") else "")
                         + (f" · источник {', '.join(r.get('source_ids') or [])}" if r.get("source_ids") else ""))
    warns = _j(row.get("operator_warnings"), [])
    if warns:
        lines.append("Предупреждения: " + "; ".join(warns))
    lines.append("Черновик v3: " + (row.get("draft_text") or "— (черновик не создан)"))
    viol = ver.get("violations") or []
    if viol:
        lines.append(f"Проверка: {ver.get('verdict')}")
        for v in viol[:10]:
            lines.append(f"  ⛔ {v.get('rule_id')} [{v.get('severity')}]: {v.get('explanation')} «{v.get('evidence_span')}»")
    elif row.get("verifier_verdict"):
        lines.append(f"Проверка: {row.get('verifier_verdict')}")
    if row.get("v2_ai_verdict"):
        lines.append(f"Черновик v2 по верификатору v3: {row.get('v2_ai_verdict')} {row.get('v2_ai_block_rules')}")
    return "\n".join(lines)
