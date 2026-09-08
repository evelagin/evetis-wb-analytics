"""A/B comparison harness: run the SAME EVETIS prompt through the current model
and one or more candidate models, side by side. Nothing is published to WB.

Usage:
    EVETIS_OPENAI_API_KEY=sk-... \
    python -m scripts.compare_openai_models --models gpt-4.1-mini,gpt-5.6-terra

Output: model_compare_<timestamp>.md with all answers side by side for review
against the checklist (соблюдение правил, конкретность, премиальность, отсутствие
запрещённых/медицинских формулировок, длина, стоимость, скорость).

IMPORTANT: confirm the exact candidate model ids against the live OpenAI models
list before running — ids evolve.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt

from app.config import Settings
from app.domain.models import Review
from app.services.openai_client import OpenAIClient
from app.services.prompt_service import PromptService

# Representative review set (per the migration spec §7.3).
SAMPLES = [
    Review(review_id="s1", rating=5, text="", pros="Всё супер", user_name="Ольга",
           product_name="Крем для лица увлажняющий", supplier_article="438775437"),
    Review(review_id="s2", rating=5, text="Кожа стала мягкой и увлажнённой, аромат приятный",
           user_name="Мария", product_name="Крем для тела Lost Cherry", supplier_article="593111986"),
    Review(review_id="s3", rating=3, text="Ожидала большего, эффект не сразу",
           user_name="Ирина", product_name="Сыворотка анти-акне", supplier_article="305101361"),
    Review(review_id="s4", rating=1, text="Появилось раздражение и покраснение",
           user_name="Света", product_name="Тоник анти-акне", supplier_article="535581675"),
    Review(review_id="s5", rating=2, text="У меня аллергия началась после применения",
           user_name="Наташа", product_name="Сыворотка увлажняющая", supplier_article="305101272"),
    Review(review_id="s6", rating=2, text="Флакон пришёл повреждённый, помпа не работает",
           user_name="Юля", product_name="Крем для лица анти-акне", supplier_article="438775617"),
    Review(review_id="s7", rating=3, text="Запах слишком резкий на мой вкус",
           user_name="Вера", product_name="Крем для тела Amber Vanilla", supplier_article="593111985"),
    Review(review_id="s8", rating=4, text="Подскажите, можно ли сочетать с витамином С?",
           user_name="Алина", product_name="Тоник увлажняющий", supplier_article="535581674"),
    Review(review_id="s9", rating=5, text="Отличный набор, пользуюсь всем сразу",
           user_name="Ксения", product_name="Набор пудра + тоник + сыворотка + крем анти-акне",
           supplier_article="868597351"),
    Review(review_id="s10", rating=5, text="Прыщи прошли за неделю, кожа чистая!",
           user_name="Дарья", product_name="Энзимная пудра", supplier_article="535580776"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="gpt-4.1-mini",
                    help="comma-separated model ids, first is the baseline")
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    base = Settings()
    prompts = PromptService(base.prompts_dir, base.prompt_version)
    system = prompts.system_prompt()

    out = [f"# EVETIS — сравнение моделей OpenAI\n\nМодели: {', '.join(models)}\n"]
    for review in SAMPLES:
        user = prompts.render_user_prompt(review)
        out.append(f"\n## {review.review_id} · {review.rating}★ · {review.product_name}")
        out.append(f"> {review.text or review.pros or '(без текста)'}\n")
        for model in models:
            settings = dataclasses.replace(base, openai_model=model)
            client = OpenAIClient(settings, base.secrets.openai_api_key)
            try:
                gen = client.generate_answer(system, user)
                out.append(f"**{model}** ({gen.latency_ms} ms, "
                           f"out={gen.usage.get('output_tokens')} tok):\n\n{gen.text}\n")
            except Exception as exc:  # noqa: BLE001
                out.append(f"**{model}**: ОШИБКА — {type(exc).__name__}\n")

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"model_compare_{stamp}.md"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("written:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
