"""Человекочитаемый итог прогона. Короткий: для PR, job summary и оповещения.

AE-R1 / B3: итог публичен (issue, тело PR, job summary). В нём только метаданные: состояния,
вердикты, пути файлов, имена тестов и статусы, счётчики, отпечатки. Свободный текст инженера —
только отпечатком (он и хранится отпечатком); всё остальное маскируется `mask_data`, а итоговый
текст проходит `ensure_public` (остаток данных — отказ записи)."""
from __future__ import annotations

import json
from pathlib import Path


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _usage_cost(u: dict, art_dir: Path) -> float:
    """Стоимость записи usage: своя, иначе — из доверенной копии диагностики (только вызовы ЭТОЙ роли)."""
    from tools.autonomy.audit import finite_cost
    if finite_cost(u.get("total_cost_usd")) is not None:
        return finite_cost(u["total_cost_usd"])
    f = (u.get("diagnostics") or {}).get("file")
    doc = _load(art_dir / f) if isinstance(f, str) and f.startswith("diagnostics/") and ".." not in f else None
    if not isinstance(doc, dict):
        return 0.0
    return sum(finite_cost(i.get("total_cost_usd")) or 0.0 for i in doc.get("invocations", [])
               if i.get("role") == u.get("role"))


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
                   f"Messages API {d.get('messages_api_reached')} · `sha256:{sha}` · `{d.get('file')}`")
    return out + [""]


def render_report(run: dict, art_dir: Path) -> str:
    from tools.autonomy.output_policy import ensure_public, mask_data
    return ensure_public("\n".join(mask_data(line, 1200) for line in _render(run, art_dir).split("\n")))


def _render(run: dict, art_dir: Path) -> str:
    obj = _load(art_dir / "objective.json") or {}
    rep = _load(art_dir / "engineer_report.json") or {}
    ev = _load(art_dir / "evidence.json") or {}
    rev = _load(art_dir / "review.json")
    gate = _load(art_dir / "gate.json") or {}
    cost = sum(_usage_cost(u, art_dir) for u in run.get("usage", []))
    lines = [
        f"## AE v1 · {obj.get('title', run['objective_id'])}",
        "",
        f"**Прогон** `{run['run_id']}` · состояние **{run['state']}** · итераций {run['iteration']} · "
        f"циклов ревью {run['review_cycles']}",
        f"**Гейткипер:** {gate.get('verdict', '—')} · **Ревьюер:** {(rev or {}).get('verdict', '—')}",
        f"**Production-мутаций:** {run['production_mutations']} (AE v1 обязан держать 0) · доверенный аудит: "
        f"**{(run.get('audit_evidence') or {}).get('status', 'нет')}**"
        f"{' (' + str((run.get('audit_evidence') or {}).get('audited_at')) + ')' if run.get('audit_evidence') else ''}",
        "",
        f"**Класс задачи:** {obj.get('task_class') or '—'} · **доказательства тестов:** "
        f"{ev.get('test_provenance', '—')} · **сеть на время тестов:** {ev.get('network_isolation', '—')}",
        "",
        "### Первопричина",
        ("установлена" if rep.get("root_cause_established") else "_не доказана_")
        + f" · отпечаток текста инженера `{rep.get('root_cause') or '—'}` (текст не публикуется — политика B3)",
        "",
        "### Изменённые файлы",
        *(f"- `{f}`" for f in ev.get("changed_files", [])), *(["_нет_"] if not ev.get("changed_files") else []),
        "",
        "### Доказательства (вычислены системой, не агентом)",
        *(f"- {t['name']}: **{t['status']}**" + (f" (JUnit {t['junit']['tests']} тестов)" if t.get("junit") else "")
          for t in ev.get("tests", [])),
        *(f"- ⚠️ расхождение с независимым retest: {d}" for d in ev.get("evidence_disagreement", [])[:5]),
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
        f"- заявлено пунктов: {len(rep.get('uncertainty', []))} (текст не публикуется — политика B3)",
        "",
        *_diagnostics_lines(run),
        f"_Стоимость моделей по данным CLI (для CI-вызовов — из проверенной диагностики недоверенного job'а): "
        f"${cost:.2f}. Слияние — только владельцем._",
    ]
    return "\n".join(lines) + "\n"
