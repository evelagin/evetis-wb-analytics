"""Диагностика job'а агента при ЛЮБОМ исходе — отредактированная, по схеме, проверяемая доверенным job'ом.

Инцидент M6 Phase 1 (2026-09-25): Claude CLI отказал за ≤3 с, а причина осталась только в черновом
состоянии раннера: артефакт грузился лишь при непустых хешах вывода. Здесь каждый вызов агента
оставляет запись, а job — один файл `<job>_diagnostics.json` (engineer | reviewer), который всегда
попадает в хеши и в артефакт.

Что различается и чего различить нельзя (CLI 2.1.251 не отдаёт request-id Messages API и не
сообщает об обмене токена федерации):
  agent invocation   — запись в `invocations` (вызов адаптера оркестратором);
  CLI process        — `process_started`: подпроцесс claude создан;
  WIF exchange       — CLI его не сообщает. Видны только `scope_guard` (отдельный обмен сторожа) и
                       `pre_invoke_succeeded` (свежий OIDC-файл). Обмен самого CLI — история Console;
  Messages request   — `messages_api_reached`: true, если CLI вернул `api_error_status` или время API
                       (`duration_api_ms` > 0) / токены; false, если CLI вернул JSON без времени API и
                       без статуса; null, если вывода-JSON нет;
  inference          — `model_response_began`: время API > 0 или output_tokens > 0.

Редакция: все поля проходят `redact_obj`, затем схему, затем `safe_dumps` (повторный поиск). Любой
сбой — документ REDACTION_BLOCKED без свободного текста (перечисления и числа остаются).
"""
from __future__ import annotations

import copy
import json
import re

from tools.autonomy.redact import RedactionError, find_secrets, redact_obj, redact_tail, redact_text, safe_dumps, safe_text
from tools.autonomy.schema import load_schema, validate

SCHEMA_VERSION = 1
MAX_BYTES = 256 * 1024
TAIL_OUT, TAIL_ERR, SUMMARY, REASON = 2000, 1000, 300, 500
TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504, 529}
ARTIFACTS = {"engineer_plan": ["engineer_plan.json"],
             "engineer_implement": ["engineer_implement.json", "engineer_implement.patch"],
             "reviewer": ["reviewer.json"]}
JOB_OF_ROLE = {"engineer_plan": "engineer", "engineer_implement": "engineer", "reviewer": "reviewer"}
FREE_TEXT = ("summary", "stdout_tail", "stderr_tail")
LIMITATIONS = (
    "Claude CLI не сообщает request-id запроса Messages API и факт обмена токена федерации.",
    "messages_api_reached и model_response_began выводятся из api_error_status, duration_api_ms и usage CLI.",
    "Обмен токена самим CLI виден только в истории аутентификации Claude Console.",
    "scope_guard — вывод сторожа в том же недоверенном job'е, не доверенное доказательство.",
)
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class DiagnosticsRejected(ValueError):
    """Недоверенная диагностика не прошла проверку доверенной стороны — в состояние не попадает."""


def new_invocation(role: str) -> dict:
    return {"role": role, "attempt": 1, "failure_stage": "NONE", "failure_class": "NONE",
            "process_started": False, "pre_invoke_succeeded": None, "exit_code": None, "exception_type": None,
            "elapsed_ms": None, "output_format": None, "is_error": None, "subtype": None,
            "api_error_status": None, "api_error_type": None, "duration_ms": None, "duration_api_ms": None,
            "num_turns": None, "total_cost_usd": None, "usage": None, "messages_api_reached": None,
            "model_response_began": None, "summary": "", "stdout_tail": "", "stderr_tail": ""}


def _int(v, lo=None, hi=None):
    if type(v) is not int or (lo is not None and v < lo) or (hi is not None and v > hi):
        return None
    return v


def parse_cli_json(inv: dict, doc: dict) -> None:
    """Поля результата CLI `--output-format json`. Строки — только по шаблонам схемы."""
    inv["is_error"] = doc.get("is_error") if isinstance(doc.get("is_error"), bool) else None
    st = doc.get("subtype")
    inv["subtype"] = st if isinstance(st, str) and re.fullmatch(r"[a-z_]{1,40}", st) else None
    inv["api_error_status"] = _int(doc.get("api_error_status"), 100, 599)
    inv["duration_ms"] = _int(doc.get("duration_ms"), 0)
    inv["duration_api_ms"] = _int(doc.get("duration_api_ms"), 0)
    inv["num_turns"] = _int(doc.get("num_turns"), 0)
    cost = doc.get("total_cost_usd")
    inv["total_cost_usd"] = float(cost) if type(cost) in (int, float) and 0 <= cost <= 1000 else None
    u = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
    usage = {k: u[k] for k in USAGE_KEYS if _int(u.get(k), 0) is not None}
    inv["usage"] = usage or None
    text = str(doc.get("result") or "")
    m = re.search(r'"type"\s*:\s*"([a-z_]{1,60}_error)"', text) or re.search(r"\b([a-z_]{3,50}_error)\b", text)
    inv["api_error_type"] = m.group(1) if m else None
    if inv["is_error"]:
        inv["summary"] = redact_text(text)[:SUMMARY]
    tokens = (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0)
    api_ms = inv["duration_api_ms"]
    inv["messages_api_reached"] = bool(inv["api_error_status"] or (api_ms and api_ms > 0) or tokens > 0)
    inv["model_response_began"] = bool((api_ms and api_ms > 0) or (usage.get("output_tokens") or 0) > 0)


def classify(inv: dict, transient_markers=()) -> None:
    """Класс по стадии. На решение о повторе (AgentResult.transient) не влияет — только отчёт и
    выбор FAILED (INFRA) / BLOCKED у доверенной стороны."""
    stage = inv["failure_stage"]
    tail = inv["stdout_tail"] + inv["stderr_tail"] + inv["summary"]
    infra = (inv["api_error_status"] in TRANSIENT_STATUSES) or any(m in tail for m in transient_markers)
    if stage == "NONE":
        cls = "NONE"
    elif stage in ("PRE_INVOKE", "TIMEOUT"):
        cls = "INFRA"
    elif stage in ("NO_STRUCTURED_OUTPUT", "SCHEMA_INVALID"):
        cls = "AGENT_OUTPUT"
    elif stage in ("WRAPPER_EXCEPTION", "PROCESS_SPAWN"):
        cls = "WRAPPER"
    elif stage == "CLI_REPORTED_ERROR":
        cls = "INFRA" if infra else ("API" if (inv["api_error_status"] or inv["api_error_type"]) else "CLI")
    else:   # BINARY_MISSING, OUTPUT_NOT_JSON, PROCESS_EXIT_NONZERO
        cls = "INFRA" if infra and stage != "BINARY_MISSING" else "CLI"
    inv["failure_class"] = cls


def set_tails(inv: dict, stdout: str | None, stderr: str | None) -> None:
    inv["stdout_tail"] = redact_tail(stdout or "", TAIL_OUT)
    inv["stderr_tail"] = redact_tail(stderr or "", TAIL_ERR)


def _scope_guard(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    dec = raw.get("decision") if raw.get("decision") in ("PASS", "PASS_WITH_EXCEPTION", "FAIL") else "UNKNOWN"
    scope = raw.get("scope") if isinstance(raw.get("scope"), str) and re.fullmatch(
        r"[a-z_:]{1,60}( [a-z_:]{1,60}){0,5}", raw.get("scope")) else None
    rid = raw.get("request_id") if isinstance(raw.get("request_id"), str) and re.fullmatch(
        r"req_[A-Za-z0-9]{8,64}", raw.get("request_id")) else None
    return {"provenance": "untrusted_job_guard_stdout", "decision": dec, "scope": scope,
            "expires_in": _int(raw.get("expires_in"), 0, 86400), "request_id": rid}


def bundle(job_role: str, run_id: str, invocations: list[dict], *, scratch_state: str | None = None,
           scratch_reason: str = "", scope_guard=None, expected: list[str] | None = None,
           created: list[str] | None = None, env: dict | None = None,
           wrapper_exception: str | None = None) -> dict:
    invs = [dict(i, attempt=min(n, 12)) for n, i in enumerate(invocations[-12:], 1)]
    exp = expected if expected is not None else sorted({a for i in invs for a in ARTIFACTS[i["role"]]})
    got = sorted(set(created or []))
    failed = [i for i in invs if i["failure_stage"] != "NONE"]
    last_bad = invs[-1] if invs and invs[-1]["failure_stage"] != "NONE" else (failed[-1] if failed else None)
    if wrapper_exception:
        stage, cls = "WRAPPER_EXCEPTION", "WRAPPER"
    elif not invs:
        stage, cls = "NO_INVOCATION", "WRAPPER"
    elif invs[-1]["failure_stage"] != "NONE":
        stage, cls = last_bad["failure_stage"], last_bad["failure_class"]
    else:
        stage, cls = "NONE", "NONE"
    missing = bool(set(exp) - set(got))
    ok = stage == "NONE" and not missing
    if stage == "NONE" and missing:
        stage, cls = "NO_STRUCTURED_OUTPUT", "AGENT_OUTPUT"
    env = env or {}
    reason = redact_text(scratch_reason or "")[:REASON]
    if wrapper_exception:
        reason = (f"исключение обёртки: {wrapper_exception}. " + reason)[:REASON]
    return {"schema_version": SCHEMA_VERSION, "kind": "agent_diagnostics", "job_role": job_role, "run_id": run_id,
            "outcome": "SUCCESS" if ok else "FAILURE", "failure_stage": stage, "failure_class": cls,
            "claude_code_version": env.get("claude_code_version"), "node_version": env.get("node_version"),
            "scope_guard": _scope_guard(scope_guard), "scratch_state": scratch_state
            if isinstance(scratch_state, str) and re.fullmatch(r"[A-Z_]{3,30}", scratch_state) else None,
            "scratch_reason": reason, "expected_artifacts": sorted(exp), "actually_created_artifacts": got,
            "invocations": invs, "limitations": list(LIMITATIONS), "redaction": "CLEAN"}


def _blocked(doc: dict) -> dict:
    """Структура без свободного текста: перечисления и числа остаются, текст — нет."""
    d = copy.deepcopy(doc) if isinstance(doc, dict) else {}
    out = {"schema_version": SCHEMA_VERSION, "kind": "agent_diagnostics",
           "job_role": d.get("job_role") if d.get("job_role") in ("engineer", "reviewer") else "engineer",
           "run_id": d.get("run_id") if isinstance(d.get("run_id"), str) and re.fullmatch(
               r"run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", d.get("run_id")) else "run-00000000T000000Z-00000000",
           "outcome": "FAILURE", "failure_stage": "REDACTION_BLOCKED", "failure_class": "REDACTION",
           "claude_code_version": None, "node_version": None, "scope_guard": None, "scratch_state": None,
           "scratch_reason": "", "expected_artifacts": [], "actually_created_artifacts": [], "invocations": [],
           "limitations": list(LIMITATIONS), "redaction": "REDACTION_BLOCKED"}
    schema = load_schema("agent_diagnostics")
    for key in ("expected_artifacts", "actually_created_artifacts"):
        if not validate(d.get(key), schema["properties"][key]):
            out[key] = d[key]
    inv_schema = schema["properties"]["invocations"]["items"]
    for inv in d.get("invocations", [])[:12] if isinstance(d.get("invocations"), list) else []:
        if not isinstance(inv, dict):
            continue
        keep = {**new_invocation("engineer_plan"), **{k: v for k, v in inv.items() if k in inv_schema["properties"]}}
        keep.update({k: "" for k in FREE_TEXT})
        if not validate(keep, inv_schema):
            out["invocations"].append(keep)
    return out


def finalize(doc: dict) -> tuple[str, dict]:
    """Текст для записи и итоговый документ. Никогда не бросает: сбой редакции/схемы → REDACTION_BLOCKED."""
    try:
        clean = redact_obj(doc)
        errs = validate(clean, load_schema("agent_diagnostics"))
        if errs:
            raise RedactionError("диагностика не соответствует схеме: " + "; ".join(errs[:3]))
        return safe_dumps(clean, indent=2), clean
    except (RedactionError, TypeError, ValueError):
        blocked = _blocked(doc)
        return json.dumps(blocked, ensure_ascii=False, indent=2), blocked


def verify_untrusted(text: str, job_role: str) -> dict:
    """Доверенная сторона: размер → JSON → схема → роль job'а → константы → повторный поиск секретов."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise DiagnosticsRejected(f"диагностика больше {MAX_BYTES} байт")
    try:
        doc = json.loads(text)
    except ValueError:
        raise DiagnosticsRejected("диагностика — не JSON") from None
    errs = validate(doc, load_schema("agent_diagnostics"))
    if errs:
        raise DiagnosticsRejected("диагностика нарушает схему: " + "; ".join(errs[:3]))
    if doc["job_role"] != job_role:
        raise DiagnosticsRejected(f"диагностика job'а {doc['job_role']} подана как {job_role}")
    if any(i["role"] != "reviewer" for i in doc["invocations"]) if job_role == "reviewer" else \
            any(i["role"] == "reviewer" for i in doc["invocations"]):
        raise DiagnosticsRejected("вызовы чужой роли в диагностике")
    if set(doc["limitations"]) - set(LIMITATIONS):
        raise DiagnosticsRejected("неизвестный текст в limitations")
    if find_secrets(text) or find_secrets(json.dumps(doc, ensure_ascii=False)):
        raise DiagnosticsRejected("секретоподобное в диагностике — в состояние не попадает")
    return doc


def summary_line(doc: dict, sha: str | None = None) -> str:
    last = doc["invocations"][-1] if doc.get("invocations") else {}
    parts = [f"роль {doc['job_role']}", f"исход **{doc['outcome']}**", f"стадия {doc['failure_stage']}",
             f"класс {doc['failure_class']}", f"код выхода {last.get('exit_code')}",
             f"API {last.get('api_error_status') or '—'}/{last.get('api_error_type') or '—'}",
             f"Messages API {'да' if last.get('messages_api_reached') else ('нет' if last.get('messages_api_reached') is False else '?')}",
             f"CLI {doc.get('claude_code_version') or '?'}", f"Node {doc.get('node_version') or '?'}"]
    if sha:
        parts.append(f"sha256 `{sha[:16]}`")
    return safe_text("Диагностика агента: " + " · ".join(parts))


def expected_for_state(state: str | None) -> list[str]:
    """Что инженер обязан был отдать, если вызова агента не было вовсе (например, исключение до него)."""
    if state == "PLANNING":
        return ["engineer_plan.json"]
    if state in ("IMPLEMENTING", "FIXING"):
        return ["engineer_implement.json", "engineer_implement.patch"]
    return []


def failure_class_of(diag) -> str | None:
    return diag.get("failure_class") if isinstance(diag, dict) else None
