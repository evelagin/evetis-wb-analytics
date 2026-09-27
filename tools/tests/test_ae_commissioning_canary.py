"""Регрессионный тест граничных случаев clamp_percent (мишень ввода AE v1 в эксплуатацию).

AE-COMMISSIONING-ITERATION-OK
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

from commissioning.ae_canary import clamp_percent  # noqa: E402


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-5.0, 0.0),      # ниже 0
        (150.0, 100.0),   # выше 100
        (0.0, 0.0),       # ровно 0
        (100.0, 100.0),   # ровно 100
        (42.5, 42.5),     # дробное внутри отрезка
    ],
)
def test_clamp_percent_boundaries(value, expected):
    assert clamp_percent(value) == expected


def test_clamp_percent_nan_raises():
    with pytest.raises(ValueError):
        clamp_percent(float("nan"))
