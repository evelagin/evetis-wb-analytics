"""Мишень ввода AE v1 в эксплуатацию (docs/architecture/AE_V1_RUNBOOK.md §3).

Намеренно маленькая чистая функция без потребителей: синтетическая цель просит инженера
добавить к ней регрессионный тест граничных случаев, НЕ меняя саму функцию. Изменение
только в тестах, проверяется объективно (pytest), production не затрагивает.
"""
from __future__ import annotations

import math


def clamp_percent(value: float) -> float:
    """Вернуть value, ограниченный отрезком [0, 100]. NaN — ValueError: процент не определён."""
    if isinstance(value, float) and math.isnan(value):
        raise ValueError("процент не определён: NaN")
    return min(100.0, max(0.0, float(value)))
