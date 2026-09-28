"""WP11 (Phase 3): v2 knowledge base must not carry wording the owner decisions forbid.

Only the customer-facing BODY of each KB document is checked; aliases / prohibited_claims
in the front matter are allowed to name the forbidden terms (they are resolution keys and
negative lists)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

KB = Path(__file__).resolve().parents[2] / "app" / "communication_engine" / "knowledge"
FORBIDDEN = {
    "ODR-02 trademark": r"lost\s*cherry|лост\s*черр|oud\s*(&|and)?\s*wood|уд\s*вуд",
    "ODR-09 dispenser count": r"\d\s*[–-]\s*\d\s*раз\w*\s*(нажат|прокач)|прокач\w*\s+\d|прокачать|почти всегда решает",
    "ODR-08 invented contact": r"направить в чат|напишите нам|свяжитесь с нами",
    "ODR-15 ceramide count": r"\b[56]\s+(тип|вид)\w*\s+церамид|церамид\w*\s+именно\s+6",
    "KC-03 hand PAO": r"после вскрытия\s*[—-]\s*24",
}


def _body(text: str) -> str:
    parts = text.split("---")
    return parts[2] if len(parts) >= 3 else text


@pytest.mark.parametrize("path", sorted(p for p in KB.rglob("*.md") if p.parent.name in ("cases", "products")),
                         ids=lambda p: f"{p.parent.name}/{p.name}")
def test_kb_body_respects_owner_decisions(path):
    body = _body(path.read_text(encoding="utf-8")).lower().replace("ё", "е")
    for name, rx in FORBIDDEN.items():
        assert not re.search(rx, body), f"{name} in {path.name}"


def test_hand_cream_is_declared_for_hands_only():
    front = (KB / "products" / "hand_cream.md").read_text(encoding="utf-8").split("---")[1]
    assert "name: Крем для рук парфюмированный" in front and "для тела" in front.split("prohibited_claims")[1]
