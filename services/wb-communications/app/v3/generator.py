"""WP6 — controlled generator. The LLM is a language generator, not a truth engine.

It receives the strategy, the approved customer wording of every allowed fact, required
templates (verbatim), forbidden topics and style rules — never a raw knowledge dump, never
historical answers (few_shot_from_history = false). Template-only strategies are rendered
deterministically by the planner and never reach this module.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from app.v3.planner import Plan
from app.v3.snapshot import KnowledgeSnapshot

GENERATOR_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"text": {"type": "string"}, "used_fact_ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["text", "used_fact_ids"],
}

GENERATOR_SYSTEM = """Ты пишешь короткий ответ покупателю косметики EVETIS на Wildberries от лица бренда.
СТРОГИЕ ПРАВИЛА:
1. Используй ТОЛЬКО утверждения из allowed_facts (их смысл и числа) и required_templates (дословно).
   Если нужного утверждения там нет — не добавляй его. Не используй свои знания о косметике.
2. Никаких советов по применению, частоте, сочетаниям, SPF, тестам на коже, «постепенно», «вечером»,
   сроках результата — если этого нет в allowed_facts.
3. Никаких обещаний эффекта, рекламы свойств, медицинских слов, «гипоаллергенно», «безопасно»,
   «подходит», «можно беременным/детям».
4. Никаких обещаний возврата, замены, компенсации, сроков; не придумывай каналы связи
   (не пиши «напишите нам», email, телефон, чат).
5. Не называй другие товары и не советуй купить что-то ещё.
6. Не указывай числа, которых нет в allowed_facts. Не используй названия ароматов, которых нет в allowed_facts.
7. Если strategy = UNKNOWN_FACT: честно скажи, что этой информации в документации производителя нет
   или мы не можем её подтвердить, и дай только то, что есть в allowed_facts.
8. Если strategy = CLARIFICATION_REQUIRED: вежливо попроси уточнить, что именно не понравилось,
   ничего не предполагая о причине.
9. Если strategy = ACKNOWLEDGEMENT: коротко и по-человечески отреагируй на отзыв, без фактов о составе.
Стиль: русский, спокойно, уважительно, конкретно, 1–3 предложения, без канцелярита, без шаблонного
начала «Благодарим за отзыв», без восклицательных знаков подряд. Обращайся по имени, только если оно дано.
Верни JSON: {"text": "...", "used_fact_ids": [...]}"""


@dataclass
class GenerationOutcome:
    text: Optional[str]
    mode: str                      # deterministic | llm | none
    used_fact_ids: list = field(default_factory=list)
    model: Optional[str] = None
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    error: Optional[str] = None


def build_payload(plan: Plan, snapshot: KnowledgeSnapshot, msg: dict) -> dict:
    products = []
    for pid in plan.product_ids:
        pr = snapshot.product(pid) or {}
        products.append({"internal_sku": pid, "name": pr.get("customer_name_ru"),
                         "components": [(snapshot.product(c) or {}).get("customer_name_ru")
                                        for c in pr.get("components") or []]})
    allowed = []
    for r in plan.allowed:
        if r.customer_value_ru and not r.template_id:
            allowed.append({"fact_ids": r.fact_ids, "fact_type": r.fact_type,
                            "product": (snapshot.product(r.product_id) or {}).get("customer_name_ru"),
                            "customer_value": r.customer_value_ru, "sources": r.source_ids})
    unknown = sorted({r.fact_type for r in plan.resolved if r.state == "UNKNOWN" and not r.template_id})
    return {
        "strategy": plan.strategy, "tone": plan.tone,
        "buyer_name": plan.buyer_name,
        "entity_type": msg.get("entity_type"),
        "customer_message": {"text": msg.get("text") or "", "pros": msg.get("pros") or "",
                             "cons": msg.get("cons") or ""},
        "product": products,
        "allowed_facts": allowed,
        "required_templates": plan.rendered_templates,
        "unknown_facts": unknown,
        "approved_guidance": [],
        "forbidden": ["advice without guidance_id", "cosmetic or medical claims", "service promises",
                      "invented contact channels", "other products", "numbers not in allowed_facts",
                      "trademark fragrance names"],
        "style": {"language": "ru", "concise": True, "non_defensive": True, "non_advertising": True},
    }


def generate(plan: Plan, snapshot: KnowledgeSnapshot, msg: dict, llm) -> GenerationOutcome:
    if plan.strategy in ("HUMAN_REVIEW",):
        return GenerationOutcome(text=None, mode="none")
    if plan.deterministic_text:
        return GenerationOutcome(text=plan.deterministic_text, mode="deterministic",
                                 used_fact_ids=sorted({i for r in plan.allowed for i in r.fact_ids}))
    if llm is None:
        return GenerationOutcome(text=None, mode="none", error="GENERATION_DISABLED")
    payload = build_payload(plan, snapshot, msg)
    try:
        out, usage, latency_ms, model = llm.structured_timed(
            GENERATOR_SYSTEM, json.dumps(payload, ensure_ascii=False), "v3_answer", GENERATOR_SCHEMA)
    except Exception as exc:  # noqa: BLE001 — explicit GENERATION_ERROR, never a silent fallback
        return GenerationOutcome(text=None, mode="llm", error=type(exc).__name__)
    text = (out or {}).get("text") or ""
    return GenerationOutcome(text=text.strip() or None, mode="llm",
                             used_fact_ids=list((out or {}).get("used_fact_ids") or []),
                             model=model, usage=usage or {}, latency_ms=latency_ms,
                             error=None if text.strip() else "EMPTY_GENERATION")
