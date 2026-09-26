"""Человекочитаемый итог прогона. Короткий: для PR, job summary и оповещения."""
from __future__ import annotations

import json
from pathlib import Path


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _diagnostics_lines(run: dict) -> list[str]:
    """Диагностика вызовов агента (только перечисления, числа и sha — без свободного текста)."""
    rows = [(u.get("role"), u["diagnostics"]) for u in run.get("usage", []) if isinstance(u.get("diagnostics"), dict)]
    if not rows:
        return []
    out = ["### Диагностика агента"]
    for role, d in rows:
        if "rejected" in d:
            out.append(f"- `{role}`: диагностика недоверенного job'а **отвергнута**")
            continue
        api = f"{d.get('api_error_status') or '—'}/{d.get('api_error_type') or '—'}"
        sha = (d.get("artifact_sha256") or d.get("sha256") or "")[:16]
        out.append(f"- `{role}`: {d.get('outcome')} · стадия **{d.get('failure_stage')}** · класс "
                   f"**{d.get('failure_class')}** · код выхода {d.get('exit_code')} · API {api} · "
                   f"Messages API {d.get('messages_api_reached')} · sha256 `{sha}` · `{d.get('file')}`")
    return out + [""]


def render_report(run: dict, art_dir: Path) -> str:
    obj = _load(art_dir / "objective.json") or {}
    rep = _load(art_dir / "engineer_report.json") or {}
    ev = _load(art_dir / "evidence.json") or {}
    rev = _load(art_dir / "review.json")
    gate = _load(art_dir / "gate.json") or {}
    cost = sum(float(u.get("total_cost_usd") or 0) for u in run.get("usage", []))
    lines = [
        f"## AE v1 · {obj.get('title', run['objective_id'])}",
        "",
        f"**Прогон** `{run['run_id']}` · состояние **{run['state']}** · итераций {run['iteration']} · "
        f"циклов ревью {run['review_cycles']}",
        f"**Гейткипер:** {gate.get('verdict', '—')} · **Ревьюер:** {(rev or {}).get('verdict', '—')}",
        f"**Production-мутаций:** {run['production_mutations']} (AE v1 обязан держать 0)",
        "",
        "### Первопричина",
        (rep.get("root_cause") or "не установлена") + ("" if rep.get("root_cause_established") else
                                                          "  \n_Первопричина не доказана._"),
        "",
        "### Изменённые файлы",
        *(f"- `{f}`" for f in ev.get("changed_files", [])), *(["_нет_"] if not ev.get("changed_files") else []),
        "",
        "### Доказательства (вычислены системой, не агентом)",
        *(f"- {t['name']}: **{t['status']}**" for t in ev.get("tests", [])),
        f"- validate_current_sql: **{ev.get('sql_validation', {}).get('status', '—')}**",
        f"- runtime-доступ: **{ev.get('runtime_access', {}).get('status', '—')}**",
        f"- parity: **{ev.get('parity', {}).get('status', '—')}**",
        *(f"- набор `{s}`: **{d.get('verdict')}**" for s, d in ev.get("data_suites", {}).items()),
        f"- устранение исходного сигнала: **{ev.get('objective_resolution', '—')}**",
        "",
        "### Причины вердикта",
        *(f"- **{k}**: {'; '.join(v)[:600]}" for k, v in gate.get("reasons", {}).items()),
        *(f"- ℹ️ {i[:300]}" for i in gate.get("informational", [])),
        "",
        "### Остающаяся неопределённость",
        *(f"- {u}" for u in rep.get("uncertainty", [])), *(["_не заявлена_"] if not rep.get("uncertainty") else []),
        "",
        *_diagnostics_lines(run),
        f"_Стоимость моделей по данным рантайма: ${cost:.2f}. Слияние — только владельцем._",
    ]
    return "\n".join(lines) + "\n"
