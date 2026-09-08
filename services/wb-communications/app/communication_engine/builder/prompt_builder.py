"""PromptBuilder: assembles the final system + user prompts from a PromptContext.

Contains NO brand knowledge — only structure and ordering. The user prompt is a
fixed template chosen by communication_type (review / question / chat). When the
buyer's name is unknown the address is neutral — the engine never invents a name
like «Покупательница». The prompt_version is a deterministic hash of the block
versions + template version, so every generation is reproducible.
"""
from __future__ import annotations

import hashlib

from app.communication_engine.config import EngineConfig
from app.communication_engine.constants import CommunicationType
from app.communication_engine.models.classification import ReviewInput
from app.communication_engine.models.prompt_context import PromptBundle, PromptContext

_TASK_BY_TYPE: dict[CommunicationType, str] = {
    CommunicationType.REVIEW: (
        "Задача: напиши готовый публичный ответ бренда EVETIS на этот отзыв, строго по "
        "правилам бренда и знаниям о товаре выше. Верни только текст ответа."
    ),
    CommunicationType.QUESTION: (
        "Задача: покупатель задал публичный вопрос о товаре. Дай точный полезный ответ "
        "по знаниям о товаре выше. Если нужных данных нет — не выдумывай, ответь общими "
        "словами и предложи уточнить. Верни только текст ответа."
    ),
    CommunicationType.CHAT: (
        "Задача: это личный чат с покупателем. Ответь помогающе, по делу и в тоне бренда. "
        "Если вопрос про брак/замену — подскажи следующий шаг. Верни только текст ответа."
    ),
}

_HEADER = """Тип обращения: {ctype}
Оценка: {rating}
Площадка: {marketplace}
Товар: {product_name}
Артикул: {supplier_article}
Сценарий: {scenario}
Обращение: {address}

Текст обращения:
{text}
Достоинства:
{pros}
Недостатки:
{cons}
"""


class PromptBuilder:
    def __init__(self, config: EngineConfig):
        self._config = config

    def _system(self, context: PromptContext) -> str:
        parts: list[str] = []
        for b in context.blocks:
            section = f"### {b.title}\n{b.body}"
            if b.constraints:
                section += "\nНельзя заявлять: " + "; ".join(b.constraints)
            parts.append(section)
        return "\n\n".join(parts).strip()

    def _address(self, review: ReviewInput) -> str:
        name = (review.user_name or "").strip()
        if name:
            return f"по имени «{name}»"
        return "нейтральное, без имени (имя/пол неизвестны — по имени не обращаться)"

    def _user(self, context: PromptContext, review: ReviewInput) -> str:
        cls = context.classification
        header = _HEADER.format(
            ctype=cls.communication_type.value,
            rating=review.rating if review.rating is not None else "не указана",
            marketplace=cls.marketplace.value,
            product_name=review.product_name or "товар EVETIS",
            supplier_article=review.supplier_article or review.nm_id or "",
            scenario=", ".join(cls.scenario) or "общий",
            address=self._address(review),
            text=review.text or "",
            pros=review.pros or "",
            cons=review.cons or "",
        )
        task = _TASK_BY_TYPE.get(cls.communication_type, _TASK_BY_TYPE[CommunicationType.REVIEW])
        return f"{header}\n{task}"

    def _version(self, context: PromptContext) -> str:
        signature = "|".join(f"{b.ref}:{b.version}" for b in context.blocks)
        ctype = context.classification.communication_type.value
        digest = hashlib.sha1(
            f"{signature}#t{self._config.user_template_version}#{ctype}".encode()
        ).hexdigest()[:10]
        return f"{self._config.prompt_version_prefix}:{digest}"

    def build(self, context: PromptContext, review: ReviewInput) -> PromptBundle:
        return PromptBundle(
            system=self._system(context),
            user=self._user(context, review),
            prompt_version=self._version(context),
            classification=context.classification,
        )
