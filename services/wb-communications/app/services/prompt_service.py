"""Loads the EVETIS prompt files and renders the user prompt for one review.

The prompt text is NOT embedded in Python — it lives in prompts/reviews/*.txt so
it can be versioned independently (prompt_version) and edited without touching
code. Defaults for empty fields replicate the exact `|| "…"` fallbacks the n8n
workflow used, so generations stay identical after migration.
"""
from __future__ import annotations

import os
from functools import lru_cache

from app.domain.models import Review

# Field -> default, mirroring the n8n user-template `{{ $json.x || "…" }}`.
_DEFAULTS = {
    "user_name": "Покупательница",
    "rating": "не указана",
    "platform": "WB",
    "product_name": "товар EVETIS",
    "supplier_article": "",
    "nm_id": "",
    "brand_name": "EVETIS",
    "created_date": "",
    "text": "",
    "pros": "",
    "cons": "",
}


@lru_cache(maxsize=8)
def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class PromptService:
    def __init__(self, prompts_dir: str, prompt_version: str):
        self.prompts_dir = prompts_dir
        self.prompt_version = prompt_version
        self._reviews_dir = os.path.join(prompts_dir, "reviews")

    def system_prompt(self) -> str:
        return _read(os.path.join(self._reviews_dir, "system_v1.txt"))

    def _user_template(self) -> str:
        return _read(os.path.join(self._reviews_dir, "user_v1.txt"))

    def render_user_prompt(self, review: Review, *, regenerate_hint: str = "") -> str:
        values = {}
        for key, default in _DEFAULTS.items():
            raw = getattr(review, key, "")
            # brand_name: fall back to EVETIS when empty (matches n8n)
            values[key] = str(raw).strip() if str(raw).strip() else default
        template = self._user_template()
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", value)
        if regenerate_hint:
            rendered += (
                "\n\nВАЖНО: это повторная генерация. Дай ДРУГОЙ вариант ответа"
                f" с учётом пожелания: «{regenerate_hint}». Не повторяй предыдущий вариант дословно."
            )
        return rendered
