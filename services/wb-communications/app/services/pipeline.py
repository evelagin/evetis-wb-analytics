"""Orchestration for /poll and the Telegram webhook.

All business logic lives here as plain functions over a ``Deps`` bundle, so it
is fully unit-testable with in-memory / fake dependencies (no GCP, no network).
Routes are thin adapters that build ``Deps`` and call these functions.

Transient failures (OpenAI/WB/Telegram/Firestore infra) propagate as exceptions
so the webhook route can return 5xx and let Telegram redeliver. Business
outcomes (published, skipped, stale, unauthorized, publish disabled) return a
dict and are final (HTTP 200).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.domain.exceptions import (
    FirestoreTransientError,
    InvalidTransition,
    NotFound,
    WBApiError,
    WBPublishOutcomeUnknown,
    is_transient,
)
from app.communication_engine.constants import CommunicationType
from app.domain.models import Question, Review
from app.domain.statuses import EventType, Status
from app.utils.logging import audit_event, get_logger, log_event, redact
from app.utils.security import is_allowed
from app.utils.text import (
    TELEGRAM_MSG_SOFT_LIMIT,
    WB_ANSWER_MAX_LEN,
    clean_answer,
    escape_html,
    is_within_wb_limit,
    truncate,
)

logger = get_logger(__name__)

ACTIONS = {"pub", "p31", "s31", "sv2", "u2", "pa", "sa", "edit", "regen", "skip", "show", "ov", "oc",
           "ta", "tr", "tm", "tv", "tx", "tl", "tq"}
# callback token -> state-machine action name
_ACTION_MAP = {"pub": "publish", "edit": "edit", "regen": "regenerate", "skip": "skip", "show": "show"}
_REVIEW_TEXT_CARD_LIMIT = 700

# Human-readable operator hints per WB publish status code. Keeps diagnosis fast:
# the operator sees WHY it failed, not just a number.
_WB_PUBLISH_HINTS = {
    400: "Неверный формат запроса к WB.",
    401: "API-токен WB недействителен или истёк.",
    403: "Недостаточно прав API-токена (нужен доступ на запись «Вопросы и отзывы»).",
    404: "Отзыв не найден на стороне WB (возможно, устарел или удалён).",
    409: "Конфликт: отзыв уже обрабатывается на стороне WB.",
    422: "Отзыв уже имеет ответ, либо текст не прошёл проверку WB.",
    429: "Превышен лимит запросов WB. Повторите через минуту.",
}


# Question-specific overrides where the reason differs from reviews.
_WB_QUESTION_HINTS = {
    404: "Вопрос не найден или уже обработан.",
    422: "Вопрос уже имеет ответ, либо текст не прошёл проверку WB.",
    409: "Конфликт: вопрос уже обрабатывается на стороне WB.",
}


def _wb_publish_error_message(exc, *, is_question: bool = False) -> str:
    """Build a detailed, operator-friendly Telegram message for a WB publish
    failure: the HTTP code plus a plain-language reason."""
    code = getattr(exc, "status_code", None)
    hint = (_WB_QUESTION_HINTS.get(code) if is_question else None) or _WB_PUBLISH_HINTS.get(code)
    if hint is None and isinstance(code, int) and code >= 500:
        hint = "Временная ошибка на стороне WB. Можно повторить позже."
    header = f"❌ Wildberries вернул {code}" if code else "❌ Ошибка публикации в WB"
    return f"{header}\n\n{hint}" if hint else f"{header}\n\nМожно повторить."


@dataclass
class Deps:
    settings: Any
    repo: Any
    wb: Any
    openai: Any
    telegram: Any
    bq: Any
    prompts: Any
    # Communication Engine v2. `engine` is the PRIMARY generator (set only when
    # V2_PRIMARY=true); `shadow_engine`/`shadow_repo` are the shadow comparison
    # (set only in shadow mode). All None by default -> unchanged reviews_v1.
    engine: Any = None
    shadow_engine: Any = None
    shadow_repo: Any = None
    # Reviews & Q&A v3 SHADOW runtime (app.v3.shadow.V3Runtime) — None unless
    # V3_SHADOW_ENABLED. Observation only: no WB, no Telegram, no v2 doc writes.
    v3: Any = None
    publication_validator: Any = None  # None => mandatory production policy adapter


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Telegram card rendering / callback parsing
# --------------------------------------------------------------------------- #
def parse_callback(data: str) -> tuple[Optional[str], Optional[str]]:
    if not data:
        return None, None
    if ":" in data:
        action, _, doc_id = data.partition(":")
    else:  # legacy n8n format action_reviewid
        action, _, doc_id = data.partition("_")
        action = {"send": "pub"}.get(action, action)
    action = action.strip()
    doc_id = doc_id.strip()
    if action not in ACTIONS or not doc_id:
        return None, None
    return action, doc_id


def build_keyboard(doc_id: str, *, show_full: bool = False, retry: bool = False, generation=None) -> dict:
    rows = [
        [
            {"text": "✅ Опубликовать", "callback_data": f"pub:{doc_id}" + (f":{generation}" if generation is not None else "")},
            {"text": "✏️ Изменить", "callback_data": f"edit:{doc_id}"},
        ],
        [
            {"text": "🔄 Перегенерировать", "callback_data": f"regen:{doc_id}"},
            {"text": "⏭ Пропустить", "callback_data": f"skip:{doc_id}"},
        ],
    ]
    if show_full:
        rows.append([{"text": "📋 Показать полностью", "callback_data": f"show:{doc_id}"}])
    return {"inline_keyboard": rows}



def _recovery_enabled(deps):
    return getattr(deps.settings, "v31_operator_recovery_enabled", False)


# --------------------------------------------------------------------------- #
# R2 operator assist: a v3.1E draft next to the v2 draft (never auto-published)
# --------------------------------------------------------------------------- #
_POLL_STARTED = contextvars.ContextVar("poll_started", default=None)
# R2.4A operator identity: the Telegram actor and the action of the update being handled.
_ACTOR = contextvars.ContextVar("telegram_actor", default=None)
_ACTION_NAMES = {"pub": "publish", "pa": "publish_as_is", "oc": "red_confirmation", "ov": "red_confirmation_request",
                 "edit": "edit_start", "regen": "regenerate", "sa": "safe_alternative", "skip": "skip",
                 "show": "show", "p31": "publish_v31", "s31": "show_v31", "sv2": "show_v2", "u2": "use_v2"}


def _operator_draft_enabled(deps):
    return getattr(deps.settings, "v31_operator_draft_enabled", False) or _primary_enabled(deps)


def _primary_enabled(deps):
    """R2.1: v3.1E is the operator's primary draft (V2 only a secondary fallback)."""
    return getattr(deps.settings, "v31_primary_operator_enabled", False) or _v31_only_enabled(deps)


def _v31_only_enabled(deps):
    """R2.2: v3.1E is the only normal operator answer; V2 is legacy rollback/diagnostics."""
    return getattr(deps.settings, "v31_only_operator_enabled", False)


def _v31_generation(draft):
    from app.domain.models import GenerationResult
    from app.response_quality import VERSION
    return GenerationResult(text=draft["text"], model="v3.1E:" + str(draft.get("model") or ""),
                            prompt_version=VERSION, usage={}, latency_ms=0, request_id="")


def _v31_draft(deps, doc_id, doc, *, budgeted=True) -> dict | None:
    """Prepare and store the v3.1E draft for this communication. One model call at most;
    any failure leaves the v2 card unaffected. Inside a poll a time budget applies."""
    if not _operator_draft_enabled(deps):
        return None
    started = _POLL_STARTED.get()
    if budgeted and started is not None and time.monotonic() - started > float(
            getattr(deps.settings, "v31_operator_draft_budget_seconds", 100)):
        draft = {"status": "SKIPPED_BUDGET", "created_at": _now_iso()}
        deps.repo.update(doc_id, {"v31_draft": draft})
        return draft
    t0 = time.monotonic()
    try:
        from app.v3.snapshot import load_snapshot
        from app.v3.shadow import message_from_doc
        from app.response_quality.core import prepare
        from app.response_quality.language import renderer_for_client
        from app.response_quality import VERSION
        snap = load_snapshot(getattr(deps.settings, "v3_knowledge_snapshot_id", None))
        renderer = renderer_for_client(deps.openai) if hasattr(deps.openai, "structured") else None
        msg = message_from_doc(doc_id, doc)
        result = prepare(msg, doc.get("ai_answer") or "", snap, render=renderer, force_generation=True,
                         moderate_safety=_primary_enabled(deps), safe_gaps=_v31_only_enabled(deps))
        usable = result.status == "READY" and bool(result.text) and result.final_policy.get("verdict") != "BLOCK"
        human = getattr(result.plan, "human_reason", None)
        draft = {"status": "READY" if usable else "HUMAN_REVIEW", "text": result.text if usable else None,
                 "text_sha256": hashlib.sha256(result.text.encode()).hexdigest() if usable else None,
                 "route": result.plan.route, "hard_verdict": result.final_policy.get("verdict"),
                 "quality_verdict": result.quality.verdict, "version": VERSION,
                 "model": renderer.model if renderer and renderer.calls else "deterministic",
                 "llm_calls": renderer.calls if renderer else 0,
                 "attention": getattr(result.plan, "attention", None), "human_reason": human,
                 "restriction": "SERIOUS_SAFETY" if human == "SAFETY_ESCALATION" else None,
                 "latency_ms": int((time.monotonic() - t0) * 1000), "created_at": _now_iso()}
    except Exception as exc:  # noqa: BLE001 — the v2 card must still go out
        log_event(logger, "warning", "v3.1 operator draft failed (isolated)", error_class=type(exc).__name__)
        draft = {"status": "ERROR", "error_class": type(exc).__name__, "created_at": _now_iso()}
    deps.repo.update(doc_id, {"v31_draft": draft})
    log_event(logger, "info", "v3.1 operator draft", communication_id=doc_id, status=draft["status"],
              route=draft.get("route"), hard_verdict=draft.get("hard_verdict"), llm_calls=draft.get("llm_calls"))
    return draft


_V31_CARD_LABEL = "✨ <b>Вариант 3.1E:</b>\n"
_V31_FULL_LABEL = "✨ <b>Вариант 3.1E полностью:</b>\n"


def _v31_visible(message_text, draft_text) -> bool:
    """The exact, complete 3.1E draft sits under its own label in the message being sent
    (not cut by card truncation, not merely somewhere else in the card)."""
    shown = escape_html(str(draft_text))
    return bool(message_text) and any(label + shown in message_text
                                      for label in (_V31_CARD_LABEL, _V31_FULL_LABEL))


def _v31_section(doc) -> list[str]:
    draft = doc.get("v31_draft") or {}
    if draft.get("text"):
        return ["", _V31_CARD_LABEL + escape_html(str(draft["text"]))]
    if draft.get("status") == "HUMAN_REVIEW":
        return ["", "✨ <i>Вариант 3.1E не предложен: нужна проверка человеком.</i>"]
    return []


# --------------------------------------------------------------------------- #
# R2.1: v3.1E as the primary operator draft
# --------------------------------------------------------------------------- #
_PRIMARY = "v31_primary"
_V31_ONLY = "v31_only"          # R2.2: V2 never becomes a publishable answer
_V31_SOURCES = {"v31e", "manual"}
_HUMAN_REASON_RU = {
    "HUMAN_REVIEW_DOMAIN": "юридический вопрос, подлинность или требования регулятора",
    "UNCLASSIFIED_QUESTION": "вопрос не распознан",
    "SERVICE_INSTRUCTION_NOT_VERIFIED": "сервисный вопрос без утверждённой инструкции",
    "REQUIRED_FACT_UNKNOWN": "нет подтверждённых данных для ответа",
    "QUESTION_EXCEEDS_INFORMATION_BUDGET": "вопрос из нескольких частей",
    "KNOWLEDGE_CONFLICT": "противоречивые данные о товаре",
    "POLICY_RESTRICTED_CLAIM": "ограниченное утверждение",
    "KNOWN_RESTRICTED_NO_ABSTRACTION": "ограниченные сведения",
    "PRODUCT_NOT_VERIFIED": "товар не опознан",
}
_V2_LABEL = "🗂 <b>Старый вариант V2</b> (резервный, правила V2):\n"
_FALLBACK_REASON = {"ERROR": "3.1E временно недоступен",
                    "SKIPPED_BUDGET": "3.1E не успел подготовиться — нажмите «🔄 Перегенерировать»",
                    "HUMAN_REVIEW": "3.1E: нужна проверка человеком"}


def _is_primary(doc) -> bool:
    return (doc or {}).get("operator_mode") in (_PRIMARY, _V31_ONLY)


def _is_v31_only(doc) -> bool:
    return (doc or {}).get("operator_mode") == _V31_ONLY


def _no_v31_answer(doc) -> bool:
    """R2.2: the active answer is not a 3.1E or operator text (e.g. only the stored V2 draft)."""
    return _is_v31_only(doc) and _answer_source(doc) not in _V31_SOURCES


def _answer_source(doc):
    """Source of the active answer. An owner-confirmed version keeps the source of the exact
    version it confirmed (an operator text stays «manual», a 3.1E answer «v31e»)."""
    versions = (doc or {}).get("answer_versions") or []
    if not versions:
        return None
    last = versions[-1]
    if last.get("source") == "owner_override":
        confirmed = [v for v in versions if v.get("generation_number") == last.get("source_version")]
        return confirmed[0].get("source") if confirmed else "owner_override"
    return last.get("source")


def answer_lineage(doc) -> str:
    """Policy family of the active answer: the latest machine draft it descends from.
    A manual edit inherits it — an edited 3.1E answer stays on the 3.1E policy, an
    edited V2 answer on LIVE_V2."""
    for version in reversed((doc or {}).get("answer_versions") or []):
        if version.get("source") != "manual":
            return "v31" if version.get("source") == "v31e" else "live_v2"
    return "live_v2"


def _serious(doc) -> bool:
    """A primary-mode (R2.1) SERIOUS_SAFETY communication. R2 cards keep R2 behaviour."""
    return _is_primary(doc) and ((doc or {}).get("v31_draft") or {}).get("restriction") == "SERIOUS_SAFETY"


def _serious_unreviewed(doc) -> bool:
    """Serious safety: no machine answer may be published; only the operator's own text."""
    return _serious(doc) and _answer_source(doc) != "manual"


def publication_mode_for(doc) -> str:
    """Policy context of the active answer. A serious-safety answer the operator wrote
    through the edit flow gets the v3.1E policy with the human-review routing satisfied;
    otherwise the answer's lineage decides."""
    if _serious(doc) and _answer_source(doc) == "manual":
        return "v31_human_safety"
    if _is_v31_only(doc):
        return "v31"            # R2.2: every publishable answer is 3.1E or operator text
    return answer_lineage(doc)


def _primary_answer_block(doc) -> str | None:
    text = doc.get("final_answer") or doc.get("ai_answer")
    if _serious_unreviewed(doc) or _no_v31_answer(doc) or not text:
        return None
    source = _answer_source(doc)
    if source == "v31e":
        label = "✨ <b>Рекомендуемый ответ 3.1E:</b>"
    elif source == "manual":
        label = "✏️ <b>Ответ оператора</b> (правила " + {"v31": "3.1E", "v31_human_safety": "3.1E, проверен человеком"}.get(
            publication_mode_for(doc), "V2") + "):"
    elif source == "v2_fallback":
        label = "✍️ <b>Вариант V2</b> (выбран оператором):"
    else:
        reason = _FALLBACK_REASON.get((doc.get("v31_draft") or {}).get("status"), "3.1E недоступен")
        label = f"✍️ <b>Резервный ответ V2</b> ({reason}):"
    return label + "\n" + escape_html(str(text))


def _primary_badges(doc) -> list[str]:
    draft = doc.get("v31_draft") or {}
    if draft.get("restriction") == "SERIOUS_SAFETY":
        return ["⛔ <b>Нужна проверка человеком:</b> покупатель сообщает о возможной серьёзной реакции. "
                "Готовый ответ не публикуется — напишите свой через «✏️ Изменить».", ""]
    if draft.get("attention") == "MODERATE_DISCOMFORT":
        return ["⚠️ <b>Требует внимания оператора:</b> покупатель связывает самочувствие с ароматом. "
                "Проверьте ответ перед публикацией.", ""]
    if draft.get("attention") == "SAFETY_TEMPLATE":
        return ["⚠️ <b>Требует внимания оператора:</b> реакция кожи, утверждённый ответ безопасности.", ""]
    if _no_v31_answer(doc):
        if draft.get("status") == "HUMAN_REVIEW":
            reason = _HUMAN_REASON_RU.get(draft.get("human_reason"), "нужна проверка")
            return [f"⛔ <b>Нужна проверка человеком:</b> {reason}. Напишите ответ через «✏️ Написать вручную».", ""]
        return ["⚠️ <b>Не удалось подготовить ответ 3.1E.</b> Нажмите «🔄 Повторить» или напишите ответ вручную.", ""]
    return []


def _build_primary_card(doc, doc_id, *, full=False, question=False) -> tuple[str, bool]:
    """3.1E leads. The answer is never cut: customer fields shrink to fit Telegram."""
    def e(value) -> str:
        return escape_html("" if value is None else str(value))
    answer = _primary_answer_block(doc)

    def compose(cap):
        cut = False

        def fit(value):
            nonlocal cut
            value = str(value or "")
            if cap is not None and len(value) > cap:
                cut = True
                return truncate(value, cap)
            return value
        lines = ["<b>EVETIS / WB — новый вопрос</b>" if question else "<b>EVETIS / WB — новый отзыв</b>", "",
                 *_primary_badges(doc),
                 f"<b>Товар:</b> {e(doc.get('product_name') or '—')}",
                 f"<b>Артикул продавца:</b> {e(doc.get('supplier_article') or '—')}",
                 f"<b>nmId:</b> {e(doc.get('nm_id') or '—')}"]
        if question:
            lines += [f"<b>Дата:</b> {e(doc.get('source_created_at') or '—')}", "",
                      f"❓ <b>Вопрос покупателя:</b>\n{e(fit(doc.get('text')) or '—')}"]
        else:
            lines += [f"<b>Оценка:</b> {e(doc.get('rating') or '—')} / 5",
                      f"<b>Покупатель:</b> {e(doc.get('buyer_name') or 'Не указано')}",
                      f"<b>Дата:</b> {e(doc.get('source_created_at') or '—')}",
                      f"<b>Достоинства:</b> {e(fit(doc.get('pros')) or '—')}",
                      f"<b>Недостатки:</b> {e(fit(doc.get('cons')) or '—')}",
                      *([f"<b>Теги покупателя:</b> {e(', '.join(doc.get('bables')))}"] if doc.get('bables') else []),
                      f"<b>Комментарий:</b> {e(fit(doc.get('text')) or '—')}"]
        lines += ["", answer or "✨ <i>Ответ не предложен.</i>", *_preflight_lines(doc), "",
                  f"<i>модель: {e(doc.get('openai_model') or '—')} · id: {e(doc_id)}</i>"]
        return "\n".join(lines), cut

    cap = None if full else _REVIEW_TEXT_CARD_LIMIT
    text, cut = compose(cap)
    while len(text) > TELEGRAM_MSG_SOFT_LIMIT and (cap is None or cap > 40):
        cap = int((cap or max(len(str(doc.get(k) or "")) for k in ("text", "pros", "cons"))) * 0.7)
        text, cut = compose(cap)
    return truncate(text, TELEGRAM_MSG_SOFT_LIMIT), cut


# --------------------------------------------------------------------------- #
# R2.3: proactive publication preflight — the finding is on the card BEFORE the owner acts
# --------------------------------------------------------------------------- #
# RED: stronger (two-step) confirmation. Serious safety and a policy outage are RED too.
_RED_RULES = {"V-RESTRICTED", "V-CONFLICT", "V-SAFETY", "V-SAFETY-ROUTE", "V-MEDICAL"}
_FINDING_RU = {
    "V-CLAIM": "свойство товара, не подтверждённое документами",
    "V-FACT": "сведения о составе, не подтверждённые документами",
    "V-GENERAL": "совет или утверждение, не утверждённое брендом",
    "V-RESTRICTED": "закрытые сведения (ограниченное значение)",
    "V-CONFLICT": "противоречие проверенным данным",
    "V-NUM": "число или процент без подтверждения",
    "V-MEDICAL": "медицинское утверждение",
    "V-SAFETY": "небезопасная формулировка о реакции",
    "V-SAFETY-ROUTE": "случай безопасности требует проверки человеком",
    "V-SERVICE-PREMISE": "сервисная ситуация, о которой покупатель не сообщал",
    "PRODUCT_NOT_VERIFIED": "товар не опознан",
}


def _preflight_text(doc):
    if not _is_v31_only(doc) or _answer_source(doc) not in _V31_SOURCES:
        return None
    return clean_answer(doc.get("final_answer") or "") or None


def _current_preflight(doc):
    """The stored preflight, only if it describes exactly the active text and generation."""
    pf = (doc or {}).get("publication_preflight") or {}
    text = _preflight_text(doc)
    if not pf or not text or pf.get("generation") != doc.get("generation_number", 0) \
            or pf.get("text_sha256") != hashlib.sha256(text.encode()).hexdigest():
        return None
    return pf


def _ensure_preflight(deps, doc_id, doc, *, force=False):
    """Run the current publication policy on the active answer and persist the result."""
    text = _preflight_text(doc)
    if not text or (not force and _current_preflight(doc)):
        return doc
    from app.services.owner_override import fingerprint
    from app.services.publication_policy import error_policy
    mode = publication_mode_for(doc)
    validator = _publication_validator(deps, mode)
    try:
        policy = (validator(text, doc, deps.settings) if deps.publication_validator else
                  validator(text, doc, deps.settings, include_spans=True))
        if not isinstance(policy, dict) or policy.get("verdict") not in ("PASS", "INFO", "WARNING", "BLOCK"):
            raise ValueError("invalid policy verdict")
    except Exception as exc:  # noqa: BLE001
        policy = error_policy(text, exc)
    rules = sorted({v.get("rule_id") for v in policy.get("violations") or [] if v.get("rule_id")})
    red = (["POLICY_UNAVAILABLE"] if policy["verdict"] == "ERROR" else []) + (
        (["SERIOUS_SAFETY"] if _serious(doc) else []) + (["HIGH_RISK_RULE"] if set(rules) & _RED_RULES else [])
        if policy["verdict"] == "BLOCK" else [])
    pf = {"text_sha256": hashlib.sha256(text.encode()).hexdigest(), "generation": doc.get("generation_number", 0),
          "mode": mode, "policy_version": policy.get("version"), "snapshot": policy.get("snapshot"),
          "verdict": policy["verdict"], "rules": rules,
          "spans": [str(s.get("span")) for s in policy.get("violation_spans") or [] if s.get("span")][:5],
          "fingerprint": fingerprint(policy), "red": bool(red), "red_reasons": red, "checked_at": _now_iso()}
    deps.repo.update(doc_id, {"publication_preflight": pf})
    return {**doc, "publication_preflight": pf}


def _preflight_lines(doc) -> list[str]:
    pf = _current_preflight(doc)
    if not pf or pf["verdict"] in ("PASS", "INFO"):
        return []
    if pf["verdict"] == "ERROR":
        return ["", "⛔ <b>Проверка правил 3.1E сейчас недоступна.</b> Публикация — только решением владельца "
                    "в два шага."]
    findings = escape_html("; ".join(_FINDING_RU.get(r, r) for r in pf["rules"]) or "замечание правил")
    if pf["verdict"] == "WARNING":
        return ["", f"ℹ️ Замечание правил 3.1E: {findings}."]
    spans = "; ".join("«" + escape_html(s) + "»" for s in pf.get("spans") or [])
    head = ("⛔ <b>Высокий риск — система не рекомендует публикацию:</b>" if pf["red"] else
            "⚠️ <b>Система не рекомендует публикацию без проверки:</b>")
    return ["", f"{head} {findings}." + (f"\nФрагмент: {spans}" if spans else "")]


def _owner_decision_available(deps, doc) -> bool:
    """R2.3: an owner may publish this card's exact answer past a content BLOCK / policy outage."""
    if not _is_v31_only(doc) or doc.get("status") not in ("policy_blocked", "policy_check_failed"):
        return False
    from app.services.owner_override import offer
    return offer(doc, deps.settings) is not None


def _v31_only_keyboard(deps, doc_id, doc, *, show_full=False, card_text=None):
    """R2.2: no V2 anywhere. With an answer: the normal four; without: retry/write/skip."""
    gen = doc.get("generation_number", 0)
    answer = _primary_answer_block(doc)
    edit = {"text": "✏️ Изменить", "callback_data": f"edit:{doc_id}"}
    skip = {"text": "⏭ Пропустить", "callback_data": f"skip:{doc_id}"}
    if answer:
        visible = bool(card_text and answer in card_text)
        other = {"text": "🔄 Другой вариант", "callback_data": f"regen:{doc_id}"}
        pf = _current_preflight(doc)
        if pf and pf["verdict"] in ("BLOCK", "ERROR"):
            # The finding is already on the card: the owner decides here, not after a surprise.
            from app.services.owner_override import eligible
            first = []
            if eligible(doc, deps.settings) and visible:
                source = doc["answer_versions"][-1]["generation_number"]
                first.append({"text": "⚠️ Решение владельца: опубликовать", "callback_data": f"ov:{doc_id}:{gen}:{source}"}
                             if pf["red"] else {"text": "✅ Опубликовать как есть", "callback_data": f"pa:{doc_id}:{gen}"})
            elif not visible:
                first.append({"text": "📋 Показать полностью", "callback_data": f"show:{doc_id}"})
            first.append({"text": "✨ Безопасная альтернатива", "callback_data": f"sa:{doc_id}:{gen}"})
            return {"inline_keyboard": [first, [edit, other], [skip]]}
        rows = [[{"text": "✅ Опубликовать", "callback_data": f"pub:{doc_id}:{gen}"} if visible else
                 {"text": "📋 Показать полностью", "callback_data": f"show:{doc_id}"}, edit],
                [other, skip]]
        if show_full and visible:
            rows.append([{"text": "📋 Показать полностью", "callback_data": f"show:{doc_id}"}])
        if _owner_decision_available(deps, doc):
            # Shown to the chat, usable only by the listed owner (checked on the tap).
            rows.append([{"text": "⚠️ Решение владельца: опубликовать",
                          "callback_data": f"ov:{doc_id}:{gen}:{doc['answer_versions'][-1]['generation_number']}"}])
        return {"inline_keyboard": rows}
    write = {"text": "✏️ Написать вручную", "callback_data": f"edit:{doc_id}"}
    draft = doc.get("v31_draft") or {}
    if draft.get("status") in ("ERROR", "SKIPPED_BUDGET"):
        return {"inline_keyboard": [[{"text": "🔄 Повторить", "callback_data": f"regen:{doc_id}"}, write], [skip]]}
    return {"inline_keyboard": [[write, skip]]}


def _primary_keyboard(deps, doc_id, doc, *, show_full=False, card_text=None):
    if _is_v31_only(doc):
        return _v31_only_keyboard(deps, doc_id, doc, show_full=show_full, card_text=card_text)
    gen = doc.get("generation_number", 0)
    serious = _serious_unreviewed(doc)
    answer = _primary_answer_block(doc)
    visible = bool(answer and card_text and answer in card_text)
    first = []
    if not serious:
        # Never a publish button for an answer the operator cannot see in full.
        first.append({"text": "✅ Опубликовать", "callback_data": f"pub:{doc_id}:{gen}"} if visible else
                     {"text": "📋 Показать полностью", "callback_data": f"show:{doc_id}"})
    first.append({"text": "✏️ Изменить", "callback_data": f"edit:{doc_id}"})
    second = ([] if serious else [{"text": "🔄 Перегенерировать", "callback_data": f"regen:{doc_id}"}]) + \
        [{"text": "⏭ Пропустить", "callback_data": f"skip:{doc_id}"}]
    rows = [first, second]
    if show_full and (serious or visible):
        rows.append([{"text": "📋 Показать полностью", "callback_data": f"show:{doc_id}"}])
    if doc.get("ai_answer") and (serious or answer_lineage(doc) == "v31"):
        rows.append([{"text": "🗂 Старый вариант V2", "callback_data": f"sv2:{doc_id}:{gen}"}])
    return {"inline_keyboard": rows}


def _prepare_response(deps, doc, text):
    if not _recovery_enabled(deps):
        return None, None
    from app.v3.snapshot import load_snapshot
    from app.v3.shadow import message_from_doc
    from app.response_quality.core import prepare
    from app.domain.models import GenerationResult
    from app.response_quality import VERSION
    try:
        snap = load_snapshot(getattr(deps.settings, "v3_knowledge_snapshot_id", None))
        from app.response_quality.language import renderer_for_client
        renderer = renderer_for_client(deps.openai) if hasattr(deps.openai,'structured') else None
        result = prepare(message_from_doc("", doc), text, snap, render=renderer)
        meta = result.metadata()
        meta['language_generation'] = ({'model':renderer.model,'calls':renderer.calls} if renderer else
                                      {'model':'deterministic-fallback','calls':0})
        gen = (GenerationResult(text=result.text, model=(renderer.model if renderer and renderer.calls else "deterministic-human-voice"),
               prompt_version=VERSION, usage=(renderer.usage if renderer else {}),
               latency_ms=(renderer.latency_ms if renderer else 0), request_id="")
               if result.status == "READY" and result.repaired else None)
        return gen, meta
    except Exception as exc:
        # Never expose an unchecked candidate if snapshot/repair fails.
        return None, {"status": "HUMAN_REVIEW", "repaired": False,
                      "reasons": ["REPAIR_ERROR:" + type(exc).__name__]}


def _operator_keyboard(deps, doc_id, doc, *, show_full=False, retry=False, card_text=None):
    if _is_primary(doc):
        return _primary_keyboard(deps, doc_id, doc, show_full=show_full, card_text=card_text)
    bind = _recovery_enabled(deps) or _operator_draft_enabled(deps)
    k = build_keyboard(doc_id, show_full=show_full, retry=retry,
                       generation=doc.get("generation_number", 0) if bind else None)
    draft_text = (doc.get("v31_draft") or {}).get("text")
    if _operator_draft_enabled(deps) and draft_text:
        # Never offer to publish text the operator cannot see in full in this message.
        gen = doc.get("generation_number", 0)
        k["inline_keyboard"].insert(1, [{"text": "✨ Опубликовать 3.1E", "callback_data": f"p31:{doc_id}:{gen}"}]
                                    if _v31_visible(card_text, draft_text) else
                                    [{"text": "✨ Показать 3.1E полностью", "callback_data": f"s31:{doc_id}:{gen}"}])
    if _recovery_enabled(deps) and doc.get("response_review_required"):
        k["inline_keyboard"][0] = [k["inline_keyboard"][0][1]]
    from app.services.owner_override import enabled as override_enabled
    if override_enabled(deps.settings):
        from app.services.owner_override import offer
        button=offer(doc,deps.settings)
        if button:
            k['inline_keyboard'].append([{'text':'⚠️ Опубликовать исходный всё равно',
                'callback_data':f"ov:{doc_id}:{doc.get('generation_number',0)}:{button['source_version']}"}])
            if (doc.get('response_recovery') or {}).get('repaired'):
                k['inline_keyboard'][0][0]['text']='✅ Опубликовать исправленный'
    return k


def _recovery_note(text, doc):
    meta = doc.get("response_recovery") or {}
    if meta.get("operator_state") == "BLOCK":
        note = "⛔ Ответ нарушает правила. Исправленный вариант недоступен: нужна редактура оператора."
    elif meta.get("status") == "HUMAN_REVIEW":
        note = "⛔ Нужна проверка оператора. Публикация не выполнялась."
    elif meta.get("repaired"):
        note = "Черновик автоматически скорректирован перед публикацией."
    elif meta.get('operator_state')=='WARNING':
        note = "⚠️ Ответ безопасен по правилам, но требует редакторского внимания."
    else:
        return text
    reasons = meta.get("reasons") or []
    if reasons:
        names = {"V-CLAIM": "неподтверждённое свойство", "V-FACT": "ингредиент",
                 "V-RESTRICTED": "закрытые сведения", "V-GENERAL": "неутверждённый совет"}
        summary = "; ".join(dict.fromkeys(names.get(r, r) for r in reasons))
        note += " Причина: " + summary[:220]
    block = meta.get('original_block') or {}
    if block.get('violation_spans'):
        note += " Исходный текст: " + "; ".join(v['rule_id']+": «"+v['span']+"»" for v in block['violation_spans'])[:600]
    # Keep the ready answer/customer context as the main card; never replace it with a generic error.
    return truncate(text + "\n\n<i>" + escape_html(note) + "</i>", TELEGRAM_MSG_SOFT_LIMIT)


def build_card(doc: dict, doc_id: str, *, full: bool = False) -> tuple[str, bool]:
    if _is_primary(doc):
        return _build_primary_card(doc, doc_id, full=full)

    def e(value) -> str:
        return escape_html("" if value is None else str(value))

    review_text = doc.get("text") or "—"
    truncated = False
    if not full and len(review_text) > _REVIEW_TEXT_CARD_LIMIT:
        review_text = truncate(review_text, _REVIEW_TEXT_CARD_LIMIT)
        truncated = True

    answer = doc.get("final_answer") or doc.get("ai_answer") or "—"
    lines = [
        "<b>EVETIS / WB — новый отзыв</b>", "",
        f"<b>Товар:</b> {e(doc.get('product_name') or '—')}",
        f"<b>Артикул продавца:</b> {e(doc.get('supplier_article') or '—')}",
        f"<b>nmId:</b> {e(doc.get('nm_id') or '—')}",
        f"<b>Оценка:</b> {e(doc.get('rating') or '—')} / 5",
        f"<b>Покупатель:</b> {e(doc.get('buyer_name') or 'Не указано')}",
        f"<b>Дата:</b> {e(doc.get('source_created_at') or '—')}",
        f"<b>Достоинства:</b> {e(doc.get('pros') or '—')}",
        f"<b>Недостатки:</b> {e(doc.get('cons') or '—')}",
        *([f"<b>Теги покупателя:</b> {e(', '.join(doc.get('bables')))}"] if doc.get('bables') else []),
        f"<b>Комментарий:</b> {e(review_text)}", "",
        f"✍️ <b>Проект ответа:</b>\n{e(answer)}", *_v31_section(doc), "",
        f"<i>модель: {e(doc.get('openai_model') or '—')} · id: {e(doc_id)}</i>",
    ]
    return truncate("\n".join(lines), TELEGRAM_MSG_SOFT_LIMIT), truncated


def build_question_card(doc: dict, doc_id: str, *, full: bool = False) -> tuple[str, bool]:
    """Telegram card for a WB buyer question — no rating/pros/cons; the buyer's
    question and the draft answer."""
    if _is_primary(doc):
        return _build_primary_card(doc, doc_id, full=full, question=True)

    def e(value) -> str:
        return escape_html("" if value is None else str(value))

    q_text = doc.get("text") or "—"
    truncated = False
    if not full and len(q_text) > _REVIEW_TEXT_CARD_LIMIT:
        q_text = truncate(q_text, _REVIEW_TEXT_CARD_LIMIT)
        truncated = True

    answer = doc.get("final_answer") or doc.get("ai_answer") or "—"
    lines = [
        "<b>EVETIS / WB — новый вопрос</b>", "",
        f"<b>Товар:</b> {e(doc.get('product_name') or '—')}",
        f"<b>Артикул продавца:</b> {e(doc.get('supplier_article') or '—')}",
        f"<b>nmId:</b> {e(doc.get('nm_id') or '—')}",
        f"<b>Дата:</b> {e(doc.get('source_created_at') or '—')}", "",
        f"❓ <b>Вопрос покупателя:</b>\n{e(q_text)}", "",
        f"✍️ <b>Проект ответа:</b>\n{e(answer)}", *_v31_section(doc), "",
        f"<i>модель: {e(doc.get('openai_model') or '—')} · id: {e(doc_id)}</i>",
    ]
    return truncate("\n".join(lines), TELEGRAM_MSG_SOFT_LIMIT), truncated


def _card_builder_for(doc: dict | None):
    """Pick the right card renderer for a doc based on its entity_type."""
    return build_question_card if (doc or {}).get("entity_type") == "question" else build_card


def _review_from_doc(doc: dict) -> Review:
    return Review(
        platform=(doc.get("channel") or "wb").upper(),
        review_id=doc.get("source_id", ""),
        rating=doc.get("rating"),
        created_date=doc.get("source_created_at", "") or "",
        text=doc.get("text", "") or "",
        pros=doc.get("pros", "") or "",
        cons=doc.get("cons", "") or "",
        bables=list(doc.get("bables") or []),
        user_name=doc.get("buyer_name", "") or "",
        product_name=doc.get("product_name", "") or "",
        supplier_article=doc.get("supplier_article", "") or "",
        nm_id=doc.get("nm_id", "") or "",
        imt_id=doc.get("imt_id", "") or "",
        brand_name=doc.get("brand_name", "") or "",
    )


def _question_from_doc(doc: dict) -> Question:
    return Question(
        platform=(doc.get("channel") or "wb").upper(),
        question_id=doc.get("source_id", ""),
        created_date=doc.get("source_created_at", "") or "",
        text=doc.get("text", "") or "",
        product_name=doc.get("product_name", "") or "",
        supplier_article=doc.get("supplier_article", "") or "",
        nm_id=doc.get("nm_id", "") or "",
        imt_id=doc.get("imt_id", "") or "",
        brand_name=doc.get("brand_name", "") or "",
        state=doc.get("wb_state", "") or "",
    )


def _subject_from_doc(doc: dict):
    """Reconstruct the engine subject + its communication type from a stored doc."""
    if (doc or {}).get("entity_type") == "question":
        return _question_from_doc(doc), CommunicationType.QUESTION
    return _review_from_doc(doc), CommunicationType.REVIEW


# --------------------------------------------------------------------------- #
# BigQuery events (deterministic id + Firestore-guarded exactly-once) & current
# --------------------------------------------------------------------------- #
def _emit_event(deps: Deps, doc: dict, doc_id: str, event_type: EventType,
                status_before: str = "", status_after: str = "", *, best_effort: bool = True,
                **extra) -> None:
    """Enqueue an event into the outbox.

    best_effort=True (default, used by /poll): a transient enqueue is logged and
    swallowed so it never breaks the flow or reverts an already-committed state.
    best_effort=False (webhook outcomes): a transient enqueue is re-raised so the
    webhook returns 5xx and Telegram redelivers instead of silently dropping it.
    """
    answer_version = extra.get("answer_version") or doc.get("generation_number") or ""
    attempt = extra.get("attempt", "")
    # Deterministic across retries of the SAME logical event -> BQ dedup key.
    event_id = hashlib.sha1(
        "|".join(str(x) for x in (
            doc.get("channel", "wb"), doc.get("entity_type", "review"),
            doc.get("source_id", ""), event_type.value,
            answer_version, status_after, attempt,
        )).encode()
    ).hexdigest()
    usage = doc.get("openai_usage") or {}
    actor = _actor()
    if actor:
        extra.setdefault("telegram_user_id", actor["user_id"])
        extra["payload"] = {**(extra.get("payload") or {}), "actor": actor}
    payload = {
        "event_id": event_id, "event_at": _now().isoformat(),
        "channel": doc.get("channel", "wb"), "entity_type": doc.get("entity_type", "review"),
        "source_id": doc.get("source_id", ""), "event_type": event_type.value,
        "status_before": status_before, "status_after": status_after,
        "telegram_user_id": str(extra.get("telegram_user_id") or ""),
        "openai_model": doc.get("openai_model", ""), "prompt_version": doc.get("prompt_version", ""),
        "answer_version": answer_version or None, "latency_ms": doc.get("openai_latency_ms"),
        "token_input": usage.get("input_tokens"), "token_output": usage.get("output_tokens"),
        "error_code": (str(extra["error_code"]) if extra.get("error_code") is not None else None),
        "error_message": redact(extra.get("error_message") or "")[:500] or None,
        "payload_json": (json.dumps(extra["payload"], ensure_ascii=False, default=str)
                         if extra.get("payload") else None),
    }
    # Persist to the Firestore outbox; delivery to BigQuery happens in flush_events.
    try:
        deps.repo.enqueue_event(payload)
    except FirestoreTransientError:
        if best_effort:
            log_event(logger, "warning", "event enqueue transient (best-effort skip)",
                      event_type=event_type.value, doc_id=doc_id)
            return
        raise


def flush_events(deps: Deps, limit: int = 100) -> dict:
    """Deliver pending outbox events to BigQuery. At-least-once, deduped by
    event_id. Each event is CLAIMED (pending->sending, leased) before the BQ
    write so parallel flushes never send the same event at once; a failed write
    returns it to pending for the next flush."""
    delivered = failed = skipped = 0
    for rec in deps.repo.list_pending_events(limit=limit):
        if not deps.repo.claim_outbox_event(rec["event_id"]):
            skipped += 1
            continue
        try:
            ok = deps.bq.insert_event(rec["payload"])
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            deps.repo.mark_event_delivered(rec["event_id"])
            delivered += 1
        else:
            deps.repo.mark_event_failed(rec["event_id"])
            failed += 1
    return {"delivered": delivered, "failed": failed, "skipped": skipped}


def _current_row(doc: dict) -> dict:
    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "channel": doc.get("channel", "wb"), "entity_type": doc.get("entity_type", "review"),
        "source_id": doc.get("source_id", ""), "status": doc.get("status", ""),
        "rating": as_int(doc.get("rating")), "product_name": doc.get("product_name"),
        "supplier_article": doc.get("supplier_article"), "nm_id": doc.get("nm_id"),
        "brand_name": doc.get("brand_name"), "buyer_name": doc.get("buyer_name"),
        "has_photo": bool(doc.get("has_photo")), "has_video": bool(doc.get("has_video")),
        "ai_answer": doc.get("ai_answer"), "final_answer": doc.get("final_answer"),
        "openai_model": doc.get("openai_model"), "prompt_version": doc.get("prompt_version"),
        "generation_number": as_int(doc.get("generation_number")),
        "first_seen_at": doc.get("first_seen_at"), "published_at": doc.get("published_at"),
        "updated_at": doc.get("updated_at"),
    }


def _sync_current(deps: Deps, doc_id: str, fallback: dict | None = None) -> dict:
    doc = deps.repo.get(doc_id) or fallback or {}
    deps.bq.upsert_current(_current_row(doc))
    return doc


# --------------------------------------------------------------------------- #
# Answer generation — reviews_v1 or Communication Engine v2 (primary)
# --------------------------------------------------------------------------- #
def _generate_reviews_v1(deps: Deps, review: Review):
    """The original production generator (static reviews_v1 prompt)."""
    system = deps.prompts.system_prompt()
    user = deps.prompts.render_user_prompt(review)
    return deps.openai.generate_answer(system, user)


def _v2_flags(deps: Deps, subject, communication_type, bundle, gen) -> list[str]:
    """Human-facing warnings from the v2 classification + validators. Empty when
    the product resolved and every validator passed."""
    flags: list[str] = []
    classification = bundle.classification
    if classification.needs_manual_moderation:
        flags.append("товар/площадка не распознаны — проверьте вручную")
    try:
        context = deps.engine.build_context(subject, communication_type)
        result = deps.engine.validate(gen.text, context)
        if not result.ok:
            details = "; ".join(sorted({(i.detail or i.message) for i in result.errors}))
            flags.append(f"валидатор: {details}")
    except Exception as exc:  # noqa: BLE001 — a validator fault must not block the draft
        log_event(logger, "warning", "v2 validation error (non-blocking)",
                  error=type(exc).__name__)
    return flags


def _generate_answer(deps: Deps, subject, communication_type=CommunicationType.REVIEW):
    """Return ``(gen, v2_meta)``. ``v2_meta`` is None for reviews_v1, else a dict
    with the flags to surface on the card.

    V2 is LEGACY_FALLBACK / ROLLBACK ONLY: under R2.2 (V31_ONLY_OPERATOR_ENABLED) it is not
    called for new communications at all; it serves R2.1 rollback and legacy cards.

    Reviews use v2 only when it is primary (else reviews_v1 — the explicit
    config rollback path); questions ALWAYS use v2 (reviews_v1 has no question
    template). A v2 prompt-build failure is an item error retried on the next
    poll — there is NO silent fallback to reviews_v1 (WP11: its prompt carries
    unsourced product facts and the «обострение — нормальная реакция» advice).
    OpenAI errors propagate as before.
    """
    is_question = communication_type == CommunicationType.QUESTION
    use_v2 = deps.engine is not None and (
        is_question or getattr(deps.settings, "communication_engine_v2_primary", False)
    )
    if not use_v2:
        if is_question:
            raise RuntimeError("questions require the v2 engine (COMMUNICATION_ENGINE_V2_ENABLED)")
        return _generate_reviews_v1(deps, subject), None

    bundle = deps.engine.build_prompt(subject, communication_type)  # failure -> item error
    gen = deps.openai.generate_answer(bundle.system, bundle.user)  # OpenAI errors propagate
    gen = replace(gen, prompt_version=bundle.prompt_version)  # record the ENGINE version
    return gen, {"flags": _v2_flags(deps, subject, communication_type, bundle, gen)}


def _manual_text_flags(deps: Deps, doc: dict, text: str) -> list[str]:
    """Run the SAME validators the AI draft gets over operator-typed text (WP11).
    Advisory for now (surfaced on the card), exactly like the AI draft flags."""
    if deps.engine is None or _is_v31_only(doc):
        return []  # R2.2: the v3.1E policy is the only check on operator text
    try:
        subject, communication_type = _subject_from_doc(doc)
        context = deps.engine.build_context(subject, communication_type)
        result = deps.engine.validate(text, context)
        if not result.ok:
            details = "; ".join(sorted({(i.detail or i.message) for i in result.errors}))
            return [f"валидатор (ручной текст): {details}"]
    except Exception as exc:  # noqa: BLE001 — a validator fault must not block the edit
        log_event(logger, "warning", "manual-text validation error (non-blocking)",
                  error=type(exc).__name__)
    return []


def _card_with_flags(card_text: str, v2_meta: Optional[dict]) -> str:
    """Prepend a visible ⚠️ warning line when v2 flagged the draft."""
    if not v2_meta or not v2_meta.get("flags"):
        return card_text
    warn = "⚠️ <b>Проверка v2:</b> " + escape_html("; ".join(v2_meta["flags"]))
    return truncate(f"{warn}\n\n{card_text}", TELEGRAM_MSG_SOFT_LIMIT)


# --------------------------------------------------------------------------- #
# Communication Engine v2 — shadow run (observation only)
# --------------------------------------------------------------------------- #
def _run_shadow(deps: Deps, review: Review, doc: dict, doc_id: str) -> None:
    """Run Communication Engine v2 in shadow alongside reviews_v1.

    Contract:
    * No-op unless COMMUNICATION_ENGINE_V2_ENABLED=true and the shadow components
      are wired — so with the flag off the pipeline behaves exactly as before.
    * Fully isolated: EVERY failure is logged and swallowed here, so a v2 problem
      can never break the reviews_v1 batch, the Telegram card, or WB publishing.
    * v2 NEVER publishes to WB and NEVER sends a second Telegram message; it only
      records a comparison row to the separate shadow table.
    """
    settings = deps.settings
    if not getattr(settings, "communication_engine_v2_enabled", False) or _is_v31_only(doc):
        return  # R2.2 cards: no V2 work at all
    if deps.shadow_engine is None or deps.shadow_repo is None:
        return
    try:
        row = deps.shadow_engine.evaluate(
            review,
            old_answer=doc.get("final_answer") or doc.get("ai_answer") or "",
            old_prompt_version=doc.get("prompt_version") or settings.prompt_version,
            run_version=doc.get("generation_number"),
        )
        persisted = deps.shadow_repo.insert_shadow(row)
        if not persisted:
            # The row was built but BigQuery rejected/failed the insert. Surface it
            # explicitly (never silently) — but do NOT break reviews_v1.
            log_event(logger, "warning", "communication_engine_v2 shadow persistence failed",
                      doc_id=doc_id, review_id=row.get("review_id"),
                      shadow_id=row.get("shadow_id"))
    except Exception as exc:  # noqa: BLE001 — shadow must never break reviews_v1
        log_event(logger, "warning", "communication_engine_v2 shadow failed (isolated)",
                  doc_id=doc_id, error=type(exc).__name__)


# --------------------------------------------------------------------------- #
# /poll
# --------------------------------------------------------------------------- #
def _draft_and_send(deps: Deps, subject, doc: dict, doc_id: str, communication_type, card_builder) -> dict:
    """Generate a draft, send the Telegram card, move the record to
    PENDING_APPROVAL, emit events. Shared by reviews and questions so both behave
    identically; the caller owns the surrounding try/except."""
    _emit_event(deps, doc, doc_id, EventType.FIRST_SEEN, status_after=Status.PROCESSING.value)
    if _v31_only_enabled(deps):
        # R2.2: the V2 generator is NOT called (LEGACY_FALLBACK / ROLLBACK ONLY). 3.1E prepares
        # from the communication itself; a 3.1E failure yields the retry/write/skip card.
        deps.repo.update(doc_id, {"operator_mode": _V31_ONLY})
        draft = _v31_draft(deps, doc_id, deps.repo.get(doc_id) or doc)
        if draft and draft.get("status") == "READY" and draft.get("text"):
            deps.repo.save_generation(doc_id, _v31_generation(draft), source="v31e", set_pending=False)
        doc, v2_meta = deps.repo.get(doc_id), None
        return _send_card(deps, doc, doc_id, card_builder, v2_meta)
    gen, v2_meta = _generate_answer(deps, subject, communication_type)
    prepared_gen, recovery = _prepare_response(deps, doc, gen.text)
    # keep the record leased (PROCESSING) until the card is really sent
    doc = deps.repo.save_generation(doc_id, gen, source="ai", set_pending=False)
    if prepared_gen is not None:
        doc = deps.repo.save_generation(doc_id, prepared_gen, source="policy_repair", set_pending=False)
    if recovery is not None:
        deps.repo.update(doc_id, {"response_recovery": recovery,
                                "response_review_required": recovery["status"] != "READY"})
        doc = deps.repo.get(doc_id)
    if _operator_draft_enabled(deps):
        draft = _v31_draft(deps, doc_id, doc)
        if _primary_enabled(deps):
            # R2.1: a READY 3.1E answer becomes the active answer; V2 stays as fallback.
            deps.repo.update(doc_id, {"operator_mode": _V31_ONLY if _v31_only_enabled(deps) else _PRIMARY})
            if _v31_only_enabled(deps):
                v2_meta = None  # R2.2: the V2 draft is never shown or promoted
            if draft and draft.get("status") == "READY" and draft.get("text"):
                deps.repo.save_generation(doc_id, _v31_generation(draft), source="v31e", set_pending=False)
                v2_meta = None  # V2 flags describe a draft the card no longer leads with
        doc = deps.repo.get(doc_id)
    return _send_card(deps, doc, doc_id, card_builder, v2_meta)


def _send_card(deps: Deps, doc: dict, doc_id: str, card_builder, v2_meta) -> dict:
    doc = _ensure_preflight(deps, doc_id, doc)
    _emit_event(deps, doc, doc_id, EventType.AI_GENERATED,
                status_after=Status.PROCESSING.value, answer_version=doc.get("generation_number"))

    card_text, truncated = card_builder(doc, doc_id)
    card_text = _card_with_flags(card_text, v2_meta)
    card_text = _recovery_note(card_text, doc)
    keyboard = _operator_keyboard(deps, doc_id, doc, show_full=truncated, card_text=card_text)
    msg = deps.telegram.send_message(deps.settings.telegram_chat_id, card_text, keyboard)
    deps.repo.update(doc_id, {
        "telegram_message_id": str(msg.get("message_id", "")),
        "telegram_chat_id": str(deps.settings.telegram_chat_id),
        "status": Status.POLICY_BLOCKED.value if doc.get("response_review_required") else Status.PENDING_APPROVAL.value,
        "lock_expires_at": None,
    })
    doc = _sync_current(deps, doc_id, doc)
    _emit_event(deps, doc, doc_id, EventType.SENT_TO_TELEGRAM,
                status_before=Status.PROCESSING.value, status_after=doc.get("status", Status.PENDING_APPROVAL.value))
    return doc


def run_poll(deps: Deps) -> dict:
    """One scheduled poll. The poll start bounds the R2 operator-draft time budget."""
    token = _POLL_STARTED.set(time.monotonic())
    try:
        return _run_poll(deps)
    finally:
        _POLL_STARTED.reset(token)


def _run_poll(deps: Deps) -> dict:
    poll_started = time.monotonic()
    feedbacks = deps.wb.iter_unanswered_feedbacks()
    fetched = len(feedbacks)
    processed = skipped = errors = 0
    legacy_feedback_ids = []

    for fb in feedbacks:
        review = Review.from_wb_feedback(fb)
        if not review.review_id:
            continue
        should_process, doc_id, doc = deps.repo.claim_review(review)
        if not should_process:
            if doc.get("status") == "published" and not doc.get("verified_at"):
                legacy_feedback_ids.append(doc_id)
            skipped += 1
            continue
        try:
            doc = _draft_and_send(deps, review, doc, doc_id, CommunicationType.REVIEW, build_card)
            processed += 1
            # Shadow-only: run Communication Engine v2 for observation. Fully
            # isolated — never affects the card just sent or WB publishing.
            _run_shadow(deps, review, doc, doc_id)
        except Exception as exc:  # noqa: BLE001 — one bad review must not kill the batch
            deps.repo.mark_error(doc_id, f"{type(exc).__name__}: {exc}")
            _sync_current(deps, doc_id, doc)
            _emit_event(deps, deps.repo.get(doc_id) or doc, doc_id, EventType.FAILED,
                        status_after=Status.ERROR.value, error_code=type(exc).__name__, error_message=str(exc))
            log_event(logger, "error", "poll item failed", doc_id=doc_id, error=type(exc).__name__)
            errors += 1

    flush_events(deps)
    summary = {"fetched": fetched, "processed": processed, "skipped": skipped, "errors": errors}
    from app.services.feedback_publication import reconcile
    summary["feedback_verification"] = reconcile(deps, legacy_feedback_ids)
    # WB buyer questions — separate entity, after reviews, behind its own flag.
    if getattr(deps.settings, "wb_questions_enabled", False):
        summary["questions"] = _run_questions(deps)
    # v3 SHADOW — strictly after all v2 work of this poll, time-boxed, never raises.
    if getattr(deps.settings, "v3_shadow_enabled", False) and deps.v3 is not None:
        from app.v3.shadow import run_shadow_isolated
        summary["v3_shadow"] = run_shadow_isolated(deps.v3, poll_started=poll_started)
    logger.info("poll done %s", summary)
    return summary


def _run_questions(deps: Deps) -> dict:
    """Poll and draft answers for WB buyer questions (entity_type=question).

    Runs only when WB_QUESTIONS_ENABLED. Caps NEW cards per poll at
    ``wb_questions_first_run_max`` so the first run does not flood Telegram with
    the historical backlog — unprocessed questions stay unanswered on WB and are
    picked up on later polls (newest first)."""
    legacy = _reconcile_legacy_published_questions(deps)
    reverified = _reverify_accepted_questions(deps)
    reverified["legacy"] = legacy
    reverified["recovery_cards"] = _restore_recovery_cards(deps)
    questions = deps.wb.iter_unanswered_questions()
    fetched = len(questions)
    processed = skipped = errors = 0
    cap = deps.settings.wb_questions_first_run_max
    capped = False

    for raw in questions:
        if processed >= cap:
            capped = True
            break
        question = Question.from_wb_question(raw)
        if not question.question_id:
            continue
        should_process, doc_id, doc = deps.repo.claim_question(question)
        if not should_process:
            skipped += 1
            continue
        try:
            _draft_and_send(deps, question, doc, doc_id, CommunicationType.QUESTION, build_question_card)
            processed += 1
        except Exception as exc:  # noqa: BLE001 — one bad question must not kill the batch
            deps.repo.mark_error(doc_id, f"{type(exc).__name__}: {exc}")
            _sync_current(deps, doc_id, doc)
            _emit_event(deps, deps.repo.get(doc_id) or doc, doc_id, EventType.FAILED,
                        status_after=Status.ERROR.value, error_code=type(exc).__name__, error_message=str(exc))
            log_event(logger, "error", "poll question failed", doc_id=doc_id, error=type(exc).__name__)
            errors += 1

    if capped:
        log_event(logger, "info", "WB questions per-poll cap reached; remainder next poll",
                  cap=cap, fetched=fetched)
    flush_events(deps)
    summary = {"fetched": fetched, "processed": processed, "skipped": skipped,
               "errors": errors, "capped": capped, "reverified": reverified}
    logger.info("poll questions done %s", summary)
    return summary


def _reconcile_legacy_published_questions(deps: Deps, limit: int = 20) -> dict:
    """Reconcile questions marked ``published`` BEFORE 1.5.0 against WB.

    Until 1.5.0 a question became ``published`` on any 2xx, and the request body
    was wrong, so such a status is not evidence of an answer on WB. A legacy doc
    is a question in ``published`` with no ``publication_state`` (set by every
    1.5.0 outcome). One read-only GET each; never writes to WB:

    * our text on WB    -> stays ``published``, gains ``verified_at`` (silent);
    * another text      -> ``answered_externally`` (Telegram card corrected);
    * no answer on WB   -> ``publish_unknown`` + «Опубликовать» button, so the
      operator publishes through the corrected path (which reads WB first).
    """
    counts = {"checked": 0, "verified": 0, "external": 0, "unknown": 0, "errors": 0}
    for doc_id, doc in deps.repo.list_by_status(Status.PUBLISHED.value, limit,
                                                entity_type="question"):
        if doc.get("entity_type") != "question" or doc.get("publication_state"):
            continue
        counts["checked"] += 1
        text = clean_answer(doc.get("final_answer") or doc.get("ai_answer"))
        trace = _new_trace(doc_id, doc, phase="legacy_reconcile",
                           state_before=Status.PUBLISHED.value)
        try:
            outcome = _verify_question(deps, doc["source_id"], text, trace, attempts=1)
            if outcome == "verify_error":
                counts["errors"] += 1
                continue  # WB unreadable now: leave as is, retry next poll
            if outcome == "verified":
                now = _now_iso()
                trace.update(final_publication_state=Status.PUBLISHED.value,
                             local_state_after=Status.PUBLISHED.value, finished_at=now)
                deps.repo.record_publication(
                    doc_id, Status.PUBLISHED.value,
                    {"publication_state": Status.PUBLISHED.value, "verified_at": now,
                     "legacy_reconciled_at": now}, trace)
                counts["verified"] += 1
                continue
            status = _finish_question(deps, doc_id, doc, text, outcome, trace,
                                      chat=doc.get("telegram_chat_id"),
                                      message_id=doc.get("telegram_message_id"),
                                      user_id=None, accepted=False)
            counts["external" if status == Status.ANSWERED_EXTERNALLY.value else "unknown"] += 1
        except Exception as exc:  # noqa: BLE001 — one item must not break the poll
            counts["errors"] += 1
            log_event(logger, "warning", "legacy question reconciliation failed",
                      doc_id=doc_id, error=type(exc).__name__)
    return counts


def _reverify_accepted_questions(deps: Deps, limit: int = 20) -> dict:
    """Re-read questions whose answer WB ACCEPTED but did not yet show (WB
    pre-moderates answers). Read-only towards WB — never re-sends. Resolves each
    to PUBLISHED (answer visible and ours), ANSWERED_EXTERNALLY (a different
    answer is there) or, after the verification window, PUBLISH_UNKNOWN."""
    counts = {"checked": 0, "verified": 0, "external": 0, "unknown": 0, "pending": 0, "errors": 0}
    window = timedelta(hours=float(getattr(deps.settings, "wb_question_verify_window_hours", 48)))
    for doc_id, doc in deps.repo.list_by_status(Status.PUBLISH_ACCEPTED.value, limit):
        if doc.get("entity_type") != "question":
            continue
        counts["checked"] += 1
        text = clean_answer(doc.get("final_answer") or doc.get("ai_answer"))
        trace = _new_trace(doc_id, doc, phase="reverify", state_before=Status.PUBLISH_ACCEPTED.value)
        try:
            outcome = _verify_question(deps, doc["source_id"], text, trace, attempts=1)
            if outcome == "not_visible":
                # Older / recovered docs may lack publish_accepted_at: fall back to
                # the last known publish/update time; none at all = window expired.
                accepted_at = _parse_ts(doc.get("publish_accepted_at")
                                        or doc.get("published_at") or doc.get("updated_at"))
                if accepted_at is not None and _now() - accepted_at < window:
                    counts["pending"] += 1
                    continue
                outcome = "window_expired"
            status = _finish_question(deps, doc_id, doc, text, outcome, trace,
                                      chat=doc.get("telegram_chat_id"),
                                      message_id=doc.get("telegram_message_id"), user_id=None,
                                      accepted=True)
            counts[{"published": "verified", "answered_externally": "external"}.get(status, "unknown")] += 1
        except Exception as exc:  # noqa: BLE001 — one item must not break the poll
            counts["errors"] += 1
            log_event(logger, "warning", "question re-verification failed",
                      doc_id=doc_id, error=type(exc).__name__)
    return counts


def _restore_recovery_cards(deps: Deps, limit: int = 20) -> dict:
    """Give every question in ``publish_unknown`` a live «Опубликовать» card.

    A doc that reached ``publish_unknown`` before 1.5.2 (or whose card could not be
    sent) has no ``recovery_card_sent_at``: its stored Telegram message may be a
    superseded card, so the operator sees no button. One read-only GET each:
    answer now on WB -> resolved as usual (no button); no answer -> a NEW actionable
    card. Never writes to WB."""
    counts = {"checked": 0, "sent": 0, "resolved": 0, "errors": 0}
    for doc_id, doc in deps.repo.list_by_status(Status.PUBLISH_UNKNOWN.value, limit,
                                                entity_type="question"):
        if doc.get("entity_type") != "question" or doc.get("recovery_card_sent_at"):
            continue
        counts["checked"] += 1
        text = clean_answer(doc.get("final_answer") or doc.get("ai_answer"))
        trace = _new_trace(doc_id, doc, phase="recovery_card",
                           state_before=Status.PUBLISH_UNKNOWN.value)
        try:
            outcome = _verify_question(deps, doc["source_id"], text, trace, attempts=1)
            if outcome == "verify_error":
                counts["errors"] += 1
                continue  # WB unreadable now: retry next poll
            if outcome in ("verified", "answered_externally"):
                _finish_question(deps, doc_id, doc, text, outcome, trace,
                                 chat=doc.get("telegram_chat_id"),
                                 message_id=doc.get("telegram_message_id"),
                                 user_id=None, accepted=False)
                counts["resolved"] += 1
                continue
            _send_recovery_card(deps, doc_id)
            counts["sent"] += 1
        except Exception as exc:  # noqa: BLE001 — one item must not break the poll
            counts["errors"] += 1
            log_event(logger, "warning", "question recovery card failed",
                      doc_id=doc_id, error=type(exc).__name__)
    return counts


_Q_RECOVERY_HEAD = ("⚠️ <b>Публикация не подтверждена на Wildberries</b>\n\n"
                    "Ответа на вопросе на WB нет, повторная отправка не выполнялась. Нажмите "
                    "«Опубликовать»: сервис сначала проверит вопрос на WB и отправит ответ, "
                    "только если его там нет.\n\n")
_CARD_SUPERSEDED = "ℹ️ Карточка устарела — актуальная отправлена ниже."


MIGRATION_EXCLUDED = ("published", "publishing", "publish_accepted", "publish_unknown",
                      "answered_externally", "skipped", "editing", "regenerating")


def migration_plan(doc) -> str:
    """R2.3: why an OLD pending card may (or may not) move into the R2.2 operator flow."""
    from app.domain.statuses import DRAFTABLE_FROM
    if _is_v31_only(doc):
        return "ALREADY_V31_ONLY"
    if doc.get("status") not in DRAFTABLE_FROM:
        return "EXCLUDED_STATUS:" + str(doc.get("status"))
    if doc.get("verified_at") or doc.get("published_at") or doc.get("wb_answer_verified_at"):
        return "EXCLUDED_ALREADY_ANSWERED"
    return "ELIGIBLE"


def migrate_to_v31_only(deps: Deps, doc_id: str) -> dict:
    """R2.3: move ONE old pending card (R2/R2.1) into the R2.2 flow. Owner-run only (no route
    is wired). Never touches a published/publishing/unknown/skipped record, never calls WB or
    V2, holds the regenerate lease for the switch, then sends a fresh card and retires the old.
    Publication afterwards is the normal verified publisher (read-before-write, one write)."""
    doc = deps.repo.get(doc_id)
    if not doc:
        return {"status": "not_found"}
    plan = migration_plan(doc)
    if plan != "ELIGIBLE":
        return {"status": "not_eligible", "reason": plan}
    try:
        doc, token = deps.repo.begin_regenerate(doc_id)
    except InvalidTransition:
        return {"status": "not_eligible", "reason": "LOCKED_OR_CHANGED"}
    deps.repo.update(doc_id, {"operator_mode": _V31_ONLY})
    draft = _v31_draft(deps, doc_id, deps.repo.get(doc_id) or doc, budgeted=False) or {}
    if draft.get("status") == "READY" and draft.get("text"):
        doc = deps.repo.commit_regenerate(doc_id, _v31_generation(draft), token, source="v31e")
    else:
        deps.repo.cancel_draft(doc_id, token)
        doc = deps.repo.get(doc_id) or doc
    _sync_current(deps, doc_id, doc)
    doc = _ensure_preflight(deps, doc_id, doc)
    chat = deps.settings.telegram_chat_id
    card_text, truncated = _card_builder_for(doc)(doc, doc_id)
    msg = deps.telegram.send_message(chat, card_text,
        _operator_keyboard(deps, doc_id, doc, show_full=truncated, card_text=card_text))
    new_id = str((msg or {}).get("message_id", ""))
    if new_id:
        deps.repo.update(doc_id, {"telegram_chat_id": str(chat), "telegram_message_id": new_id})
        _retire_card(deps, doc.get("telegram_chat_id") or chat, doc.get("telegram_message_id"), new_id)
    return {"status": "migrated", "v31_status": draft.get("status"), "generation_number": doc.get("generation_number")}


def _retire_card(deps: Deps, chat, old_message_id, new_message_id) -> None:
    """Strip the buttons from a superseded card (best effort: the old message may
    be gone or already identical — the new card is what matters)."""
    if not chat or not old_message_id or str(old_message_id) == str(new_message_id):
        return
    try:
        deps.telegram.edit_message_text(chat, old_message_id, _CARD_SUPERSEDED, None)
    except Exception as exc:  # noqa: BLE001
        log_event(logger, "info", "superseded card not retired", error=type(exc).__name__)


def _send_recovery_card(deps: Deps, doc_id: str) -> str:
    """Send a NEW «Опубликовать» card for a question in ``publish_unknown`` and make it
    the doc's card. A background transition cannot trust the stored message id: it
    may point to a card that was superseded (manual edit) or no longer editable."""
    doc = deps.repo.get(doc_id) or {}
    chat = doc.get("telegram_chat_id") or deps.settings.telegram_chat_id
    card_text, truncated = _card_builder_for(doc)(doc, doc_id)
    msg = deps.telegram.send_message(chat, _Q_RECOVERY_HEAD + card_text,
                                     _operator_keyboard(deps, doc_id, doc, show_full=truncated, retry=True,
                                                        card_text=_Q_RECOVERY_HEAD + card_text))
    new_id = str((msg or {}).get("message_id", ""))
    deps.repo.update(doc_id, {"telegram_chat_id": str(chat), "telegram_message_id": new_id,
                              "recovery_card_sent_at": _now_iso()})
    _retire_card(deps, chat, doc.get("telegram_message_id"), new_id)
    log_event(logger, "info", "question recovery card sent", doc_id=doc_id,
              telegram_message_id=new_id)
    return new_id


# --------------------------------------------------------------------------- #
# Telegram webhook  (dedup handled by the route; this raises on transient infra)
# --------------------------------------------------------------------------- #
def handle_update(deps: Deps, update: dict) -> dict:
    if "callback_query" in update:
        cq = update["callback_query"]
        action, _ = parse_callback(cq.get("data", ""))
        token = _set_actor(deps, ((cq.get("message") or {}).get("chat") or {}).get("id"),
                           (cq.get("from") or {}).get("id"), _ACTION_NAMES.get(action, action))
        try:
            return _handle_callback(deps, cq)
        finally:
            _ACTOR.reset(token)
    if "message" in update:
        message = update["message"]
        token = _set_actor(deps, (message.get("chat") or {}).get("id"), (message.get("from") or {}).get("id"),
                           "edit_commit")
        try:
            return _handle_message(deps, message)
        finally:
            _ACTOR.reset(token)
    return {"status": "ignored"}


def _set_actor(deps, chat_id, user_id, action):
    """Who acts. Resolved lazily, only when an event or trace is actually written."""
    return _ACTOR.set({"chat_id": chat_id, "user_id": user_id, "action": action, "_deps": deps})


def _actor() -> dict | None:
    current = _ACTOR.get()
    if not current or not current.get("user_id"):
        return None
    if "resolved" not in current:
        from app.services.team_access import actor_for
        try:
            current["resolved"] = actor_for(current["_deps"], current["chat_id"], current["user_id"])
        except Exception:  # noqa: BLE001 — identity metadata must never break moderation
            current["resolved"] = {"user_id": str(current["user_id"]), "role": None, "source": "unknown"}
    return {**current["resolved"], "action": current.get("action")}


def _allowed(deps: Deps, chat_id, user_id) -> bool:
    from app.services import team_access
    if team_access.enabled(deps.settings):
        # R2.4A: ONE source (bootstrap ∪ ACTIVE team members of the moderation chat). Never the
        # legacy allowlist, which fails OPEN on empty lists. Unknown/REVOKED/UNAVAILABLE: deny.
        principal = team_access.resolve(deps, chat_id, user_id)
        audit_event("auth_ok" if principal.can_moderate else "auth_denied", route="/telegram-webhook",
                    mechanism="telegram_team",
                    principal_class=principal.source if principal.can_moderate else "unknown",
                    result="ok" if principal.can_moderate else principal.status.lower())
        return principal.can_moderate
    ok = is_allowed(
        chat_id, user_id, deps.settings.allowed_chat_ids, deps.settings.telegram_allowed_user_ids
    )
    if not deps.settings.allowed_chat_ids and not deps.settings.telegram_allowed_user_ids:
        # is_allowed fails OPEN with no lists configured: record it as such, never as a real allow-list pass
        audit_event("auth_ok", route="/telegram-webhook", mechanism="telegram_allowlist",
                    principal_class="unrestricted", result="open_no_allowlist")
    else:
        full = bool(deps.settings.allowed_chat_ids) and bool(deps.settings.telegram_allowed_user_ids)
        audit_event("auth_ok" if ok else "auth_denied", route="/telegram-webhook", mechanism="telegram_allowlist",
                    principal_class="allowlisted_user" if ok else "unknown",
                    # only chat AND user lists make a full allow-list pass; one list is recorded as partial
                    result=("ok" if full else "ok_partial_allowlist") if ok else "not_allowlisted")
    return ok


def _handle_callback(deps: Deps, cq: dict) -> dict:
    cq_id = cq.get("id")
    message = cq.get("message", {}) or {}
    chat = (message.get("chat", {}) or {}).get("id")
    message_id = message.get("message_id")
    user_id = (cq.get("from", {}) or {}).get("id")

    if not _allowed(deps, chat, user_id):
        if cq_id:
            deps.telegram.answer_callback_query(cq_id, "Нет доступа")
        return {"status": "unauthorized"}

    action, doc_id = parse_callback(cq.get("data", ""))
    if not action:
        if cq_id:
            deps.telegram.answer_callback_query(cq_id, "Неизвестная команда")
        return {"status": "bad_request"}

    if cq_id:
        deps.telegram.answer_callback_query(cq_id)  # stop the spinner

    if action in {'ov','oc'}:
        from app.services.owner_override import handle
        return handle(deps,action,doc_id,chat,message_id,user_id)
    from app.services.team_access import TEAM_ACTIONS, handle_callback as team_callback
    if action in TEAM_ACTIONS:
        return team_callback(deps, action, doc_id, chat, message_id, user_id)
    if action == "pub":
        expected_generation = None
        if ":" in doc_id:
            doc_id, _, version = doc_id.partition(":")
            if not version.isdigit() or len(version) > 9:
                return {"status": "bad_request"}
            expected_generation = int(version)
        return _publish(deps, doc_id, chat, message_id, user_id, expected_generation=expected_generation)
    if action in {"p31", "s31"}:
        doc_id, _, version = doc_id.partition(":")
        if not version.isdigit() or len(version) > 9:
            return {"status": "bad_request"}
        if action == "s31":
            return _show_v31(deps, doc_id, int(version), chat)
        return _publish_v31(deps, doc_id, int(version), chat, message_id, user_id)
    if action == "pa":
        from app.services.owner_override import publish_as_is
        return publish_as_is(deps, doc_id, chat, message_id, user_id)
    if action == "sa":
        doc_id, _, version = doc_id.partition(":")
        if not version.isdigit() or len(version) > 9:
            return {"status": "bad_request"}
        return _safe_alternative(deps, doc_id, int(version), chat, message_id)
    if action in {"sv2", "u2"}:
        doc_id, _, version = doc_id.partition(":")
        if not version.isdigit() or len(version) > 9:
            return {"status": "bad_request"}
        if action == "sv2":
            return _show_v2(deps, doc_id, int(version), chat)
        return _use_v2(deps, doc_id, int(version), chat)
    if action == "skip":
        return _skip(deps, doc_id, chat, message_id)
    if action == "regen":
        return _regenerate(deps, doc_id, chat, message_id)
    if action == "edit":
        return _start_edit(deps, doc_id, chat, user_id)
    if action == "show":
        return _show_full(deps, doc_id, chat, message_id)
    return {"status": "ignored"}


def _publish_v31(deps: Deps, doc_id, expected_generation: int, chat, message_id, user_id) -> dict:
    """«Опубликовать 3.1E»: adopt the stored v3.1E draft as the next answer version (atomic,
    bound to the card's generation), then the unchanged verified publisher takes over:
    current policy → one WB write → read-back → published only on a verified match."""
    if not _operator_draft_enabled(deps):
        return _stale(deps, chat, "⚠️ Вариант 3.1E сейчас отключён.")
    doc = deps.repo.get(doc_id)
    if doc is None:
        deps.telegram.send_message(chat, "⚠️ Запись не найдена.")
        return {"status": "not_found"}
    text = (doc.get("v31_draft") or {}).get("text")
    if not text:
        return _stale(deps, chat, "⚠️ Для этого отзыва нет варианта 3.1E.")
    # The publish gate is checked before the draft is adopted: a closed gate changes nothing.
    gate_open = (deps.settings.wb_question_publish_enabled if doc.get("entity_type") == "question"
                 else deps.settings.wb_publish_enabled)
    if not gate_open:
        deps.telegram.send_message(chat, "🚫 Публикация в WB сейчас отключена.")
        return {"status": "publish_disabled"}
    # Current v3.1E policy on the exact stored text BEFORE anything changes. A block leaves
    # the answer, generation, status and WB untouched. The publisher re-checks before writing.
    try:
        pre = _publication_validator(deps, "v31")(text, doc, deps.settings)
        verdict = pre.get("verdict") if isinstance(pre, dict) else None
    except Exception as exc:  # noqa: BLE001
        log_event(logger, "warning", "v3.1 publish preflight failed", error_class=type(exc).__name__)
        verdict = "ERROR"
    if verdict not in ("PASS", "INFO", "WARNING"):
        deps.telegram.send_message(chat, "⛔ Вариант 3.1E сейчас не проходит правила публикации и не опубликован. "
                                         "Ответ в карточке не изменён: отредактируйте его или перегенерируйте.")
        return {"status": "v31_preflight_blocked" if verdict == "BLOCK" else "v31_preflight_failed"}
    from app.domain.models import GenerationResult
    from app.response_quality import VERSION
    gen = GenerationResult(text=text, model="v3.1E:" + str((doc.get("v31_draft") or {}).get("model") or ""),
                           prompt_version=VERSION, usage={}, latency_ms=0, request_id="")
    try:
        adopted = deps.repo.adopt_v31_draft(doc_id, expected_generation, gen)
    except InvalidTransition:
        return _stale(deps, chat, "⚠️ Карточка устарела или отзыв уже обрабатывается. Откройте актуальную карточку.")
    _emit_event(deps, adopted, doc_id, EventType.MANUALLY_EDITED, best_effort=True,
                answer_version=adopted.get("generation_number"))
    return _publish(deps, doc_id, chat, message_id, user_id, expected_generation=adopted.get("generation_number"),
                    publication_mode="v31")


def _show_v31(deps: Deps, doc_id, expected_generation: int, chat) -> dict:
    """Full exact 3.1E text in its own message; only there the publish button appears."""
    doc = deps.repo.get(doc_id)
    text = ((doc or {}).get("v31_draft") or {}).get("text")
    if not doc or not text or not _operator_draft_enabled(deps) or doc.get("generation_number", 0) != expected_generation:
        return _stale(deps, chat, "⚠️ Карточка устарела. Откройте актуальную карточку.")
    message = f"{_V31_FULL_LABEL}{escape_html(text)}\n\n<i>id: {escape_html(doc_id)}</i>"
    # Only the 3.1E action here: this message shows the 3.1E text, not the v2 draft.
    keyboard = {"inline_keyboard": [[{"text": "✨ Опубликовать 3.1E",
                                      "callback_data": f"p31:{doc_id}:{expected_generation}"}]]
                if _v31_visible(message, text) else []}
    deps.telegram.send_message(chat, message, keyboard)
    return {"status": "v31_shown"}


def _passes_policy(deps, doc, text) -> bool:
    try:
        policy = _publication_validator(deps, "v31")(text, doc, deps.settings)
        return isinstance(policy, dict) and policy.get("verdict") in ("PASS", "INFO", "WARNING")
    except Exception:  # noqa: BLE001
        return False


def _safe_alternative(deps: Deps, doc_id, expected_generation: int, chat, message_id) -> dict:
    """R2.3 «✨ Безопасная альтернатива»: a 3.1E answer that passes the current policy — the
    stored 3.1E draft if it differs and passes, else a fresh 3.1E draft. Never V2."""
    doc = deps.repo.get(doc_id)
    if not doc or not _is_v31_only(doc) or doc.get("generation_number", 0) != expected_generation:
        return _stale(deps, chat, "⚠️ Карточка устарела. Откройте актуальную карточку.")
    try:
        doc, token = deps.repo.begin_regenerate(doc_id)
    except InvalidTransition:
        return _stale(deps, chat)
    current = clean_answer(doc.get("final_answer") or "")
    draft = doc.get("v31_draft") or {}
    text = clean_answer(draft.get("text") or "")
    if not (text and text != current and not _serious(doc) and _passes_policy(deps, doc, text)):
        draft = _v31_draft(deps, doc_id, doc, budgeted=False) or {}
        text = clean_answer(draft.get("text") or "") if draft.get("status") == "READY" else ""
        if text == current:
            text = ""
    if not text:
        deps.repo.cancel_draft(doc_id, token)
        deps.telegram.send_message(chat, "⚠️ Безопасной альтернативы сейчас нет: отредактируйте ответ "
                                         "или выберите «🔄 Другой вариант».")
        return {"status": "no_alternative"}
    try:
        doc = deps.repo.commit_regenerate(doc_id, _v31_generation({**draft, "text": text}), token, source="v31e")
    except InvalidTransition:
        return _stale(deps, chat, "⚠️ Карточка устарела. Откройте актуальную карточку.")
    result = _after_regenerate(deps, doc_id, doc, chat, message_id, None)
    return {**result, "status": "alternative"}


def _show_v2(deps: Deps, doc_id, expected_generation: int, chat) -> dict:
    """R2.1 secondary diagnostic: the old V2 draft in its own message."""
    doc = deps.repo.get(doc_id)
    text = (doc or {}).get("ai_answer")
    if not doc or not text or _is_v31_only(doc) or doc.get("generation_number", 0) != expected_generation:
        return _stale(deps, chat, "⚠️ Карточка устарела. Откройте актуальную карточку.")
    message = f"{_V2_LABEL}{escape_html(text)}\n\n<i>id: {escape_html(doc_id)}</i>"
    rows = [] if _serious_unreviewed(doc) or _V2_LABEL + escape_html(text) not in message else \
        [[{"text": "↩️ Использовать вариант V2", "callback_data": f"u2:{doc_id}:{expected_generation}"}]]
    deps.telegram.send_message(chat, message, {"inline_keyboard": rows})
    return {"status": "v2_shown"}


def _use_v2(deps: Deps, doc_id, expected_generation: int, chat) -> dict:
    """The operator explicitly switches to the fallback V2 draft (LIVE_V2 policy lineage)."""
    from app.domain.models import GenerationResult
    doc = deps.repo.get(doc_id)
    if not doc or not doc.get("ai_answer") or _serious_unreviewed(doc) or _is_v31_only(doc):
        return _stale(deps, chat, "⚠️ Карточка устарела. Откройте актуальную карточку.")
    model = ((doc.get("answer_versions") or [{}])[0] or {}).get("openai_model") or "v2"
    gen = GenerationResult(text=doc["ai_answer"], model=model, prompt_version=doc.get("prompt_version") or "",
                           usage={}, latency_ms=0, request_id="")
    try:
        doc = deps.repo.adopt_v2_fallback(doc_id, expected_generation, gen)
    except InvalidTransition:
        return _stale(deps, chat, "⚠️ Карточка устарела или отзыв уже обрабатывается. Откройте актуальную карточку.")
    _sync_current(deps, doc_id, doc)
    _emit_event(deps, doc, doc_id, EventType.MANUALLY_EDITED, best_effort=True,
                answer_version=doc.get("generation_number"))
    card_text, truncated = _card_builder_for(doc)(doc, doc_id)
    msg = deps.telegram.send_message(chat, card_text,
        _operator_keyboard(deps, doc_id, doc, show_full=truncated, card_text=card_text))
    new_id = str((msg or {}).get("message_id", ""))
    if new_id:
        deps.repo.update(doc_id, {"telegram_chat_id": str(chat), "telegram_message_id": new_id})
        _retire_card(deps, chat, doc.get("telegram_message_id"), new_id)
    return {"status": "v2_adopted", "generation_number": doc.get("generation_number")}


def _stale(deps: Deps, chat, message: str = "⚠️ Действие недоступно: отзыв уже обработан."):
    deps.telegram.send_message(chat, message)
    return {"status": "stale"}


def _publication_validator(deps, publication_mode: str):
    """Policy for one publication context. «live_v2» (✅ / manual edit): the production-
    compatible live gate (unless V31_ENFORCE_LIVE_PUBLICATION_POLICY migrates everything).
    «v31» (✨ Опубликовать 3.1E): the v3.1E policy that produced the draft."""
    if deps.publication_validator:
        return deps.publication_validator
    from app.services.publication_policy import validator_for
    return validator_for(publication_mode, deps.settings)


def _publish(deps: Deps, doc_id, chat, message_id, user_id, *, expected_generation=None, override_confirmation=None,
             publication_mode: str | None = None) -> dict:
    # Entity-aware, independent fail-closed publish gates. Questions and reviews
    # each have their own WB_*_PUBLISH_ENABLED flag and their own WB endpoint.
    peek = deps.repo.get(doc_id)
    if not override_confirmation and _recovery_enabled(deps) and (peek or {}).get("response_recovery"):
        if (peek or {}).get("response_review_required") or expected_generation is None:
            return _stale(deps, chat, "⚠️ Прочитайте обновлённую карточку. Нужен Publish именно этого варианта.")
    if _recovery_enabled(deps) and expected_generation is None and peek:
        # Bind even a legacy button to the generation observed before claiming the lease.
        expected_generation = peek.get("generation_number", 0)
    is_question = (peek or {}).get("entity_type") == "question"
    if is_question:
        gate_open = deps.settings.wb_question_publish_enabled
        disabled_msg = ("🚫 Публикация ответов на вопросы отключена "
                        "(WB_QUESTION_PUBLISH_ENABLED=false).")
    else:
        gate_open = deps.settings.wb_publish_enabled
        disabled_msg = ("🚫 Публикация в WB отключена (WB_PUBLISH_ENABLED=false). "
                        "Включите только после сверки endpoint по live Swagger.")
    if not gate_open:
        deps.telegram.send_message(chat, disabled_msg)
        return {"status": "publish_disabled"}
    if _serious_unreviewed(peek):
        # Serious safety: only the operator's own text may go out, never a machine draft.
        deps.telegram.send_message(chat, "⛔ Нужна проверка человеком: напишите ответ через «✏️ Изменить». "
                                         "Ничего не опубликовано.")
        return {"status": "human_review_required"}
    if _no_v31_answer(peek):
        # R2.2: a stored V2 draft is never published; only a 3.1E or operator answer.
        deps.telegram.send_message(chat, "⛔ Нет ответа 3.1E для публикации: нажмите «🔄 Повторить» или "
                                         "напишите ответ через «✏️ Написать вручную». Ничего не опубликовано.")
        return {"status": "no_v31_answer"}
    try:
        if override_confirmation:
            from app.services.owner_override import allowed, safety_fixed
            # R2.2 cards: a listed owner, and on any safety case only operator-written text.
            unsafe = (safety_fixed(peek, deps.settings) and _answer_source(peek) != "manual"
                      if _is_v31_only(peek) else safety_fixed(peek, deps.settings))
            if not allowed(deps, chat, user_id, peek if _is_v31_only(peek) else None) or unsafe:
                return {'status':'unauthorized'}
            doc = deps.repo.begin_publish(doc_id, expected_generation=expected_generation,
                                         override_confirmation=override_confirmation)
        else:
            doc = (deps.repo.begin_publish(doc_id, expected_generation=expected_generation)
               if expected_generation is not None else deps.repo.begin_publish(doc_id))  # atomic lease + generation
    except InvalidTransition:
        return _stale(deps, chat, "⚠️ Этот отзыв уже обрабатывается или опубликован.")
    except NotFound:
        deps.telegram.send_message(chat, "⚠️ Запись не найдена.")
        return {"status": "not_found"}

    text = clean_answer(doc.get("final_answer") or doc.get("ai_answer"))
    if not is_within_wb_limit(text):
        deps.repo.mark_publish_failed(doc_id, "invalid_length", "empty or over WB limit")
        _sync_current(deps, doc_id, doc)
        deps.telegram.edit_message_text(
            chat, message_id,
            "⚠️ Ответ пустой или длиннее лимита WB (1000). Отредактируйте и попробуйте снова.",
            _operator_keyboard(deps, doc_id, deps.repo.get(doc_id) or doc, retry=True),
        )
        return {"status": "invalid_length"}

    trace = _new_trace(doc_id, doc, phase="publish", state_before=Status.PUBLISHING.value)
    trace.update(write_attempted=False, write="not_attempted")
    # The policy follows the answer's lineage (an explicit p31 says v31), never a flag alone.
    publication_mode = publication_mode or publication_mode_for(doc)
    trace["publication_mode"] = publication_mode
    try:
        policy = _publication_validator(deps, publication_mode)(text, doc, deps.settings)
        if not isinstance(policy, dict) or policy.get("verdict") not in ("PASS", "INFO", "WARNING", "BLOCK"):
            raise ValueError("invalid policy verdict")
    except Exception as exc:
        from app.services.publication_policy import error_policy
        policy = error_policy(text, exc)
    # The real verdict is always recorded, also when the owner publishes past it.
    trace["policy"] = policy
    status = {"BLOCK": "policy_blocked", "ERROR": "policy_check_failed"}.get(policy["verdict"])
    if override_confirmation:
        from app.services.owner_override import fingerprint
        if fingerprint(policy)!=override_confirmation['policy_fingerprint'] or policy.get('text_sha256')!=override_confirmation['answer_hash']:
            status='policy_check_failed'
        else:
            status=None
            trace['owner_override']={k:override_confirmation[k] for k in ('override_by','override_at',
                'answer_hash','source_version','policy_version','knowledge_snapshot')}
            trace['owner_override']['override']=True
            trace['owner_override']['override_confirmed_at']=_now_iso()
    if not override_confirmation and _recovery_enabled(deps) and status is None:
        _, route_check = _prepare_response(deps, doc, text)
        if route_check and route_check["status"] == "HUMAN_REVIEW":
            status = "policy_blocked"
            trace["hard_route"] = route_check["reasons"]
    if status:
        if not override_confirmation and status == "policy_blocked" and _recovery_enabled(deps):
            prepared_gen, recovery = _prepare_response(deps, doc, text)
            if prepared_gen is not None and recovery["status"] == "READY":
                version = doc.get("generation_number", 0) + 1
                fields = {"status": Status.PENDING_APPROVAL.value, "publication_state": "policy_repaired",
                    "generation_number": version, "final_answer": prepared_gen.text,
                    "answer_versions": (doc.get("answer_versions") or []) + [{"text": prepared_gen.text,
                        "source": "policy_repair", "generation_number": version, "created_at": _now_iso(),
                        "prompt_version": prepared_gen.prompt_version}], "response_recovery": recovery,
                    "response_review_required": False}
                trace.update(final_publication_state="policy_repaired", local_state_after="pending_approval",
                             write_attempted=False, write="not_attempted", finished_at=_now_iso())
                after = deps.repo.publication_update(doc_id, doc["lock_token"], fields, trace, release=True)
                if not after:
                    return _stale(deps, chat)
                after = _sync_current(deps, doc_id, after)
                _emit_event(deps, after, doc_id, EventType.REGENERATED, best_effort=False,
                            answer_version=version, payload=trace)
                card, trunc = _card_builder_for(after)(after, doc_id)
                card = _recovery_note(card, after)
                deps.telegram.edit_message_text(chat, message_id, card,
                    _operator_keyboard(deps, doc_id, after, show_full=trunc, card_text=card))
                return {"status": "policy_repaired", "requires_new_publish": True}
        trace.update(final_publication_state=status, local_state_after=status,
                     telegram_state=status, finished_at=_now_iso())
        deps.repo.publication_update(doc_id, doc["lock_token"],
            {"status": status, "publication_state": status}, trace, release=True)
        after = _sync_current(deps, doc_id, doc)
        _emit_event(deps, after, doc_id, EventType.FAILED, best_effort=False,
                    status_after=status, attempt=trace["publication_attempt_id"], payload=trace)
        message = ("⛔ Ответ нельзя опубликовать.\nОтвет создан по устаревшей политике и требует обновления."
                   if status == "policy_blocked" else
                   "⛔ Проверка ответа временно недоступна. Публикация не выполнялась.")
        if _is_v31_only(after):
            rules = "; ".join(sorted({v.get("rule_id", "") for v in trace["policy"].get("violations") or []})) or "—"
            message = (f"⛔ Ответ не прошёл правила 3.1E ({rules}). Ничего не опубликовано.\n"
                       "Отредактируйте ответ" + ("; владелец может опубликовать его под свою ответственность."
                                                if _owner_decision_available(deps, after) else ".")
                       if status == "policy_blocked" else
                       "⛔ Проверка правил 3.1E сейчас недоступна. Ничего не опубликовано." +
                       (" Владелец может опубликовать ответ под свою ответственность."
                        if _owner_decision_available(deps, after) else ""))
        keyboard = _operator_keyboard(deps, doc_id, after if _is_v31_only(after) else doc)
        if _is_v31_only(after):
            # The full card with the current finding, so the owner decides on the text itself.
            after = _ensure_preflight(deps, doc_id, after, force=True)
            card, _ = _card_builder_for(after)(after, doc_id)
            message = card + "\n\n<i>" + escape_html(message.split("\n")[0]) + "</i>"
            keyboard = _operator_keyboard(deps, doc_id, after, card_text=card)
        keyboard["inline_keyboard"][0] = [b for b in keyboard["inline_keyboard"][0]
                                          if not b["callback_data"].startswith(("pub:", "show:"))]
        if _recovery_enabled(deps):
            card, _ = _card_builder_for(after)(after, doc_id)
            message = card + "\n\n" + escape_html("⛔ Требуется проверка оператора. " +
                       "; ".join(v.get("rule_id", "ERROR") for v in trace["policy"].get("violations", [])))
        deps.telegram.edit_message_text(chat, message_id, message, keyboard)
        return {"status": status}
    deps.repo.publication_update(doc_id, doc["lock_token"], {"publication_policy": policy})
    # The locked document is exactly the text checked above; shadow is never consulted.
    if is_question:
        return _publish_question(deps, doc_id, doc, text, chat, message_id, user_id, trace=trace)
    from app.services.feedback_publication import publish
    return publish(deps, doc_id, doc, text, trace, chat, message_id, user_id)


# --------------------------------------------------------------------------- #
# Question publication: read-before-write, write, bounded read-back
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return _now().isoformat()


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def _wb_answer_text(question: dict) -> Optional[str]:
    """The answer text currently on WB, or None when the question is unanswered."""
    answer = (question or {}).get("answer")
    if isinstance(answer, dict) and _norm(answer.get("text")):
        return answer.get("text")
    return None


def _new_trace(doc_id: str, doc: dict, *, phase: str, state_before: str) -> dict:
    """One publication/verification record. Holds hashes and safe excerpts only:
    never the auth header, never a token (redacted), never the full answer text."""
    return {
        "publication_attempt_id": uuid.uuid4().hex,
        "phase": phase,
        "communication_id": doc_id,
        "entity_type": doc.get("entity_type", "review"),
        "wb_source_id": doc.get("source_id", ""),
        "local_state_before": state_before,
        "started_at": _now_iso(),
        "verification_attempts": 0,
        "verification_result": None,
        "final_publication_state": None,
        "error_class": None,
        "actor": _actor(),
    }


def _verify_question(deps: Deps, question_id: str, text: str, trace: dict,
                     attempts: Optional[int] = None) -> str:
    """Read the question back from WB (bounded; reads only, never writes).

    Returns ``verified`` (our answer is on WB), ``answered_externally`` (another
    answer is there), ``not_visible`` (no answer yet — e.g. WB pre-moderation) or
    ``verify_error`` (WB could not be read)."""
    if attempts is None:
        attempts = max(1, int(getattr(deps.settings, "wb_question_verify_attempts", 3)))
    delay = float(getattr(deps.settings, "wb_question_verify_delay_seconds", 2.0))
    result = "not_visible"
    for i in range(attempts):
        if i and delay > 0:
            time.sleep(delay)
        trace["verification_attempts"] += 1
        try:
            current = deps.wb.get_question(question_id)
        except WBApiError as exc:
            result = "verify_error"
            trace["verify_error_code"] = exc.status_code
            continue
        on_wb = _wb_answer_text(current)
        if on_wb is None:
            result = "not_visible"
            continue
        result = "verified" if _norm(on_wb) == _norm(text) else "answered_externally"
        break
    trace["verification_result"] = result
    return result


# Operator-facing texts. Only VERIFIED may say «Опубликовано».
_Q_MSG = {
    "published": "✅ <b>Опубликовано</b> — ответ подтверждён на Wildberries\n\n{text}",
    "publish_accepted": ("⏳ <b>Отправлено в WB, публикация ещё не подтверждена</b>\n\n"
                         "WB принял ответ, но на вопросе его пока нет (ответы продавцов проходят "
                         "модерацию WB). Статус проверяется автоматически при каждом опросе.\n\n{text}"),
    "publish_unknown": ("⚠️ <b>Публикация не подтверждена</b>\n\nОтвет на WB не найден, повторная "
                        "отправка не выполнялась. Нажмите «Опубликовать»: сервис сначала проверит "
                        "вопрос на WB и отправит ответ, только если его там нет.\n\n{text}"),
    "answered_externally": ("ℹ️ <b>На вопрос уже есть другой ответ на WB</b> (например, из кабинета). "
                            "Наш текст не отправлялся.\n\n{text}"),
}
_Q_EVENT = {
    "published": EventType.PUBLISHED,
    "publish_accepted": EventType.PUBLISH_ACCEPTED,
    "publish_unknown": EventType.PUBLISH_UNKNOWN,
    "answered_externally": EventType.ANSWERED_EXTERNALLY,
}


def _finish_question(deps: Deps, doc_id: str, doc: dict, text: str, outcome: str, trace: dict,
                     *, chat, message_id, user_id, accepted: bool) -> str:
    """Map a verification outcome to the final state, persist it with the trace,
    emit the event and tell the operator. Returns the new status."""
    if outcome == "verified":
        status = Status.PUBLISHED.value
    elif outcome == "answered_externally":
        status = Status.ANSWERED_EXTERNALLY.value
    elif accepted and outcome in ("not_visible", "verify_error"):
        status = Status.PUBLISH_ACCEPTED.value
    else:  # write outcome unknown and not visible, or accepted window expired
        status = Status.PUBLISH_UNKNOWN.value

    now = _now_iso()
    fields: dict = {"publication_state": status}
    if status == Status.PUBLISHED.value:
        fields.update(published_at=now, verified_at=now)
        if user_id is not None:
            fields["published_by_telegram_user_id"] = str(user_id)
    elif status == Status.PUBLISH_ACCEPTED.value:
        fields["publish_accepted_at"] = doc.get("publish_accepted_at") or now
        if user_id is not None:
            fields["published_by_telegram_user_id"] = str(user_id)
    interactive = user_id is not None and chat and message_id
    if interactive:
        # the card the operator pressed is the live one: keep Firestore pointing at it
        fields.update(telegram_chat_id=str(chat), telegram_message_id=str(message_id))
        if status == Status.PUBLISH_UNKNOWN.value:
            fields["recovery_card_sent_at"] = now  # this very card gets the button
    trace.update(final_publication_state=status, local_state_after=status, finished_at=now)
    deps.repo.record_publication(doc_id, status, fields, trace)
    after = _sync_current(deps, doc_id, doc)
    _emit_event(deps, after, doc_id, _Q_EVENT[status], best_effort=False,
                status_before=trace.get("local_state_before", ""), status_after=status,
                telegram_user_id=user_id, attempt=trace["publication_attempt_id"], payload=trace)
    if interactive:
        keyboard = _operator_keyboard(deps, doc_id, deps.repo.get(doc_id) or doc, retry=True) if status == Status.PUBLISH_UNKNOWN.value else None
        deps.telegram.edit_message_text(chat, message_id,
                                        _Q_MSG[status].format(text=escape_html(text)), keyboard)
    elif status == Status.PUBLISH_UNKNOWN.value:
        # Background (poll) transition: the stored card may be superseded, so editing
        # it can leave the operator without a button. Send a fresh actionable card;
        # if Telegram fails, the next poll's _restore_recovery_cards retries.
        try:
            _send_recovery_card(deps, doc_id)
        except Exception as exc:  # noqa: BLE001 — state is already persisted
            log_event(logger, "warning", "question recovery card failed",
                      doc_id=doc_id, error=type(exc).__name__)
    elif chat and message_id:
        try:
            deps.telegram.edit_message_text(chat, message_id,
                                            _Q_MSG[status].format(text=escape_html(text)), None)
        except Exception as exc:  # noqa: BLE001 — state is already persisted
            log_event(logger, "info", "question card not updated", doc_id=doc_id,
                      error=type(exc).__name__)
    log_event(logger, "info", "question publication outcome", doc_id=doc_id, status=status,
              attempt=trace["publication_attempt_id"], verification=trace.get("verification_result"),
              http_status=trace.get("http_status"))
    return status


def _publish_question(deps: Deps, doc_id: str, doc: dict, text: str, chat, message_id, user_id, trace=None) -> dict:
    """Publish a WB question answer without false success and without duplicates.

    1. read WB first — already answered? then no write (idempotent re-tap,
       crashed-lease recovery, answers typed in the WB cabinet);
    2. PATCH with the official body (``answer.text``); 2xx = ACCEPTED only;
    3. bounded read-back — only a visible, matching answer is PUBLISHED.
    A write whose outcome is unknown is never repeated automatically.
    """
    qid = doc["source_id"]
    trace = trace or _new_trace(doc_id, doc, phase="publish", state_before=Status.PUBLISHING.value)
    trace.update(endpoint=deps.settings.wb_questions_path,
                 method=deps.settings.wb_question_answer_method)
    try:
        current = deps.wb.get_question(qid)
    except WBApiError as exc:
        # Cannot see WB -> do not write blind. Stays actionable.
        trace.update(precheck="error", error_class=type(exc).__name__, http_status=exc.status_code,
                     final_publication_state=Status.PUBLISH_FAILED.value)
        deps.repo.record_publication(doc_id, Status.PUBLISH_FAILED.value,
                                     {"last_error_code": exc.status_code,
                                      "last_error_message": f"precheck failed: {exc}"[:500]}, trace)
        after = _sync_current(deps, doc_id, doc)
        _emit_event(deps, after, doc_id, EventType.FAILED, best_effort=False,
                    status_before=Status.PUBLISHING.value, status_after=Status.PUBLISH_FAILED.value,
                    telegram_user_id=user_id, error_code=exc.status_code, error_message=str(exc),
                    attempt=trace["publication_attempt_id"], payload=trace)
        deps.telegram.edit_message_text(chat, message_id,
                                        _wb_publish_error_message(exc, is_question=True),
                                        _operator_keyboard(deps, doc_id, deps.repo.get(doc_id) or doc, retry=True))
        return {"status": "publish_failed", "code": exc.status_code}

    on_wb = _wb_answer_text(current)
    if on_wb is not None:
        trace.update(precheck="already_answered", write="skipped")
        outcome = "verified" if _norm(on_wb) == _norm(text) else "answered_externally"
        trace["verification_result"] = outcome
        status = _finish_question(deps, doc_id, doc, text, outcome, trace,
                                  chat=chat, message_id=message_id, user_id=user_id, accepted=False)
        return {"status": status, "write": "skipped"}

    deps.repo.publication_update(doc_id, doc["lock_token"], {})
    trace.update(precheck="unanswered", request_started_at=_now_iso(), write_attempted=True)
    accepted = False
    try:
        res = deps.wb.publish_question_answer(qid, text)
        res = res if isinstance(res, dict) else {}
        body_excerpt = redact(json.dumps(res.get("response"), ensure_ascii=False))[:200]
        trace.update(write="sent", http_status=res.get("status_code"),
                     request_payload_sha256=res.get("request_sha256"),
                     response_excerpt=body_excerpt,
                     response_sha256=hashlib.sha256(body_excerpt.encode()).hexdigest())
        accepted = True
    except WBPublishOutcomeUnknown as exc:
        trace.update(write="outcome_unknown", http_status=exc.status_code,
                     error_class=type(exc).__name__,
                     request_payload_sha256=getattr(exc, "request_sha256", None))
    except WBApiError as exc:
        trace.update(write="rejected", http_status=exc.status_code, error_class=type(exc).__name__,
                     response_excerpt=redact(str(exc.body or ""))[:200],
                     final_publication_state=Status.PUBLISH_FAILED.value)
        deps.repo.record_publication(doc_id, Status.PUBLISH_FAILED.value,
                                     {"last_error_code": exc.status_code,
                                      "last_error_message": str(exc)[:500]}, trace)
        after = _sync_current(deps, doc_id, doc)
        _emit_event(deps, after, doc_id, EventType.FAILED, best_effort=False,
                    status_before=Status.PUBLISHING.value, status_after=Status.PUBLISH_FAILED.value,
                    telegram_user_id=user_id, error_code=exc.status_code, error_message=str(exc),
                    attempt=trace["publication_attempt_id"], payload=trace)
        deps.telegram.edit_message_text(chat, message_id,
                                        _wb_publish_error_message(exc, is_question=True),
                                        _operator_keyboard(deps, doc_id, deps.repo.get(doc_id) or doc, retry=True))
        return {"status": "publish_failed", "code": exc.status_code}

    outcome = _verify_question(deps, qid, text, trace)
    status = _finish_question(deps, doc_id, doc, text, outcome, trace,
                              chat=chat, message_id=message_id, user_id=user_id, accepted=accepted)
    return {"status": status, "write": trace.get("write")}


def _skip(deps: Deps, doc_id, chat, message_id) -> dict:
    try:
        deps.repo.begin_action(doc_id, "skip")
    except InvalidTransition:
        return _stale(deps, chat)
    except NotFound:
        deps.telegram.send_message(chat, "⚠️ Запись не найдена.")
        return {"status": "not_found"}
    after = _sync_current(deps, doc_id)
    _emit_event(deps, after, doc_id, EventType.SKIPPED, best_effort=False, status_after=Status.SKIPPED.value)
    deps.telegram.edit_message_text(chat, message_id, "⏭ <b>Пропущено</b>", None)
    return {"status": "skipped"}


def _regenerate(deps: Deps, doc_id, chat, message_id) -> dict:
    # Lock the record in REGENERATING with a token; a concurrent publish/skip/
    # second-regenerate is rejected while we call OpenAI.
    try:
        doc, token = deps.repo.begin_regenerate(doc_id)
    except InvalidTransition:
        return _stale(deps, chat)
    except NotFound:
        deps.telegram.send_message(chat, "⚠️ Запись не найдена.")
        return {"status": "not_found"}

    subject, communication_type = _subject_from_doc(doc)
    primary_draft = None
    if _is_primary(doc) and _primary_enabled(deps):
        # R2.1: regenerate the primary 3.1E answer; V2 only when 3.1E has nothing usable.
        primary_draft = _v31_draft(deps, doc_id, doc, budgeted=False)
        if primary_draft and primary_draft.get("status") == "READY" and primary_draft.get("text"):
            try:
                doc = deps.repo.commit_regenerate(doc_id, _v31_generation(primary_draft), token, source="v31e")
            except InvalidTransition:
                return _stale(deps, chat, "⚠️ Перегенерация неактуальна: отзыв уже изменён.")
            return _after_regenerate(deps, doc_id, doc, chat, message_id, None)
        if _is_v31_only(doc):
            # R2.2: no V2 lottery. Release the lock; the card states that 3.1E has no answer.
            deps.repo.cancel_draft(doc_id, token)
            doc = deps.repo.get(doc_id) or doc
            card_text, truncated = _card_builder_for(doc)(doc, doc_id)
            deps.telegram.edit_message_text(chat, message_id, card_text,
                _operator_keyboard(deps, doc_id, doc, show_full=truncated, card_text=card_text))
            return {"status": "regen_no_v31", "v31_status": (primary_draft or {}).get("status")}
    try:
        gen, v2_meta = _generate_answer(deps, subject, communication_type)
    except Exception as exc:  # noqa: BLE001
        deps.repo.cancel_draft(doc_id, token)  # unlock back to pending_approval
        if is_transient(exc):
            raise  # webhook returns 5xx, Telegram redelivers
        deps.telegram.send_message(chat, f"⚠️ Не удалось перегенерировать: {type(exc).__name__}")
        return {"status": "regen_failed"}

    try:
        prepared_gen, recovery = _prepare_response(deps, doc, gen.text)
        doc = (deps.repo.commit_regenerate(doc_id, gen, token, prepared_gen=prepared_gen, recovery=recovery)
               if recovery is not None else deps.repo.commit_regenerate(doc_id, gen, token))
    except InvalidTransition:
        return _stale(deps, chat, "⚠️ Перегенерация неактуальна: отзыв уже изменён.")
    if _operator_draft_enabled(deps) and primary_draft is None and not (doc.get("v31_draft") or {}).get("text"):
        _v31_draft(deps, doc_id, doc, budgeted=False)  # e.g. skipped by the poll budget
        doc = deps.repo.get(doc_id) or doc
    return _after_regenerate(deps, doc_id, doc, chat, message_id, v2_meta)


def _after_regenerate(deps, doc_id, doc, chat, message_id, v2_meta) -> dict:
    doc = _ensure_preflight(deps, doc_id, doc)
    _sync_current(deps, doc_id, doc)
    _emit_event(deps, doc, doc_id, EventType.REGENERATED, best_effort=False,
                answer_version=doc.get("generation_number"))
    card_text, truncated = _card_builder_for(doc)(doc, doc_id)
    card_text = _recovery_note(_card_with_flags(card_text, v2_meta), doc)
    deps.telegram.edit_message_text(chat, message_id, card_text,
        _operator_keyboard(deps, doc_id, doc, show_full=truncated, card_text=card_text))
    return {"status": "regenerated", "generation_number": doc.get("generation_number")}


def _start_edit(deps: Deps, doc_id, chat, user_id) -> dict:
    # Lock the record in EDITING with a token + capture the current version.
    try:
        edit_doc, token, expected_gen = deps.repo.begin_edit(doc_id)
    except InvalidTransition:
        return _stale(deps, chat)
    except NotFound:
        deps.telegram.send_message(chat, "⚠️ Запись не найдена.")
        return {"status": "not_found"}
    _emit_event(deps, edit_doc or {}, doc_id, EventType.EDIT_STARTED, best_effort=True,
                answer_version=expected_gen, telegram_user_id=user_id)

    # If prompting or persisting the session fails, the record must NOT stay
    # stuck in EDITING — roll the lock back to pending_approval, then re-raise so
    # the webhook returns 5xx and Telegram redelivers (a later tap can edit again).
    try:
        prompt = deps.telegram.send_force_reply(
            chat, "✏️ Ответьте на это сообщение новым текстом ответа.\n/cancel — отмена."
        )
        expires = (_now() + timedelta(seconds=deps.settings.telegram_edit_timeout_seconds)).isoformat()
        deps.repo.set_editing_session(user_id, doc_id, expires,
                                      prompt_message_id=prompt.get("message_id"),
                                      lock_token=token, expected_generation=expected_gen)
    except Exception:
        deps.repo.cancel_draft(doc_id, token)  # release the EDITING lock
        raise
    return {"status": "editing_started"}


def _show_full(deps: Deps, doc_id, chat, message_id) -> dict:
    doc = deps.repo.get(doc_id)
    if not doc:
        return {"status": "not_found"}
    doc = _ensure_preflight(deps, doc_id, doc)
    card_text, _ = _card_builder_for(doc)(doc, doc_id, full=True)
    card_text = _recovery_note(card_text, doc)
    deps.telegram.edit_message_text(chat, message_id, card_text, _operator_keyboard(deps, doc_id, doc, card_text=card_text))
    return {"status": "shown"}


def _v3_manual_shadow_check(deps: Deps, doc_id: str, doc: dict, text: str) -> None:
    """Shadow-only: record the v3 verifier verdict for a manual edit. Never affects the card,
    the state machine or publication (enforcement is a separate, owner-gated flag)."""
    if deps.v3 is None:
        return
    try:
        from app.v3.shadow import check_text
        check_text(deps.v3, doc_id, doc, text, run_kind="manual_edit_shadow")
    except Exception as exc:  # noqa: BLE001 — shadow must never break the edit flow
        log_event(logger, "warning", "v3 manual-edit shadow check failed (isolated)", error=type(exc).__name__)


def _handle_message(deps: Deps, message: dict) -> dict:
    chat = (message.get("chat", {}) or {}).get("id")
    user_id = (message.get("from", {}) or {}).get("id")
    text = (message.get("text") or "").strip()
    reply_to = ((message.get("reply_to_message") or {}) or {}).get("message_id")

    from app.services import team_access
    if team_access.enabled(deps.settings):
        # /apply and /me work for an unknown user of the moderation chat; /team, /requests: OWNER.
        handled = team_access.handle_command(deps, message)
        if handled is not None:
            return handled

    if not _allowed(deps, chat, user_id):
        return {"status": "unauthorized"}

    session = deps.repo.get_editing_session(user_id)

    if text == "/cancel":
        if session:
            deps.repo.cancel_draft(session["doc_id"], session.get("lock_token"))
        deps.repo.clear_editing_session(user_id)
        deps.telegram.send_message(chat, "Редактирование отменено.")
        return {"status": "cancelled"}

    if not session:
        return {"status": "ignored"}

    # Only accept the new answer as a reply to the specific ForceReply prompt.
    prompt_id = session.get("prompt_message_id")
    if prompt_id is not None and reply_to != prompt_id:
        return {"status": "ignored"}

    if session.get("expires_at") and _now().isoformat() > session["expires_at"]:
        deps.repo.cancel_draft(session["doc_id"], session.get("lock_token"))
        deps.repo.clear_editing_session(user_id)
        deps.telegram.send_message(chat, "⌛ Время редактирования истекло. Нажмите «Изменить» ещё раз.")
        return {"status": "expired"}

    new_text = clean_answer(text)
    if not new_text:
        deps.telegram.send_message(chat, "Пустой текст — редактирование не применено.")
        return {"status": "empty"}
    if len(new_text) > WB_ANSWER_MAX_LEN:
        # keep the session open so the user can resend a shorter version
        deps.telegram.send_message(
            chat, f"⚠️ Слишком длинно: {len(new_text)} символов при лимите WB {WB_ANSWER_MAX_LEN}. "
                  "Пришлите более короткий вариант или /cancel."
        )
        return {"status": "too_long"}

    doc_id = session["doc_id"]
    try:
        # transactional: status still EDITING, our token, unchanged version
        current = deps.repo.get(doc_id) or {}
        prepared_gen, recovery = _prepare_response(deps, current, new_text)
        kw = {"prepared_gen": prepared_gen, "recovery": recovery} if recovery is not None else {}
        doc = deps.repo.commit_manual_answer(doc_id, new_text, session.get("lock_token"),
                                             session.get("expected_generation"), **kw)
    except InvalidTransition:
        deps.repo.clear_editing_session(user_id)
        deps.telegram.send_message(chat, "⚠️ Правка неактуальна: отзыв уже изменён или обработан.")
        return {"status": "stale_edit"}
    except NotFound:
        deps.repo.clear_editing_session(user_id)
        deps.telegram.send_message(chat, "⚠️ Запись не найдена.")
        return {"status": "not_found"}
    deps.repo.clear_editing_session(user_id)
    _sync_current(deps, doc_id, doc)
    _emit_event(deps, doc, doc_id, EventType.MANUALLY_EDITED, best_effort=False,
                telegram_user_id=user_id, answer_version=doc.get("generation_number"))
    doc = _ensure_preflight(deps, doc_id, doc)
    card_text, truncated = _card_builder_for(doc)(doc, doc_id)
    card_text = _recovery_note(_card_with_flags(card_text, {"flags": _manual_text_flags(deps, doc, new_text)}), doc)
    msg = deps.telegram.send_message(chat, card_text,
        _operator_keyboard(deps, doc_id, doc, show_full=truncated, card_text=card_text))
    _v3_manual_shadow_check(deps, doc_id, doc, new_text)
    # The new card supersedes the stored one: without this, later background updates
    # (reconcile / re-verify) edit the OLD message and the operator never sees them.
    new_id = str((msg or {}).get("message_id", ""))
    if new_id:
        deps.repo.update(doc_id, {"telegram_chat_id": str(chat), "telegram_message_id": new_id})
        _retire_card(deps, chat, doc.get("telegram_message_id"), new_id)
    return {"status": "edited"}
