"""Проба формы ответов v2: только закрытый список чтения, сухой режим без сети и без секретов."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[3] / "tools" / "ozon" / "promo_v2_probe.py"
spec = importlib.util.spec_from_file_location("promo_v2_probe", TOOL)
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

MUTATING = ["/v1/actions/products/update", "/v1/actions/products/activate", "/v1/actions/products/deactivate",
            "/v2/actions/products/deactivate", "/v2/actions/auto-add/products/update",
            "/v2/actions/auto-add/products/delete", "/v1/actions/auto-add/products/update"]


@pytest.mark.parametrize("path", MUTATING)
def test_mutating_paths_refused_before_network(path, monkeypatch):
    monkeypatch.setattr(P.urllib.request, "urlopen", lambda *a, **k: pytest.fail("сеть"))
    with pytest.raises(P.ProbePathDenied):
        P.call("POST", path, {}, {})


def test_read_paths_only_with_their_method():
    with pytest.raises(P.ProbePathDenied):
        P.check("/v1/actions", "POST")
    with pytest.raises(P.ProbePathDenied):
        P.check("/v2/actions/products", "GET")


def test_dry_run_uses_no_network_and_no_secrets(monkeypatch, capsys):
    monkeypatch.setattr(P.urllib.request, "urlopen", lambda *a, **k: pytest.fail("сеть"))
    monkeypatch.setattr(P, "secret", lambda n: pytest.fail("секрет"))
    assert P.main([]) == 0
    assert "СУХОЙ РЕЖИМ" in capsys.readouterr().out


def test_plan_is_bounded_and_read_only():
    acts = [{"id": i, "auto_add_dates": ["2026-11-01T21:00:00Z", "b", "c"]} for i in range(10)]
    plan = P.plan(acts)
    assert len({b["action_id"] for _, _, b in plan}) == P.MAX_ACTIONS
    assert all(P.READ_ONLY[p] == m for m, p, _ in plan)
    assert all(b["limit"] <= P.PROBE_LIMIT for _, _, b in plan)
    assert sum(p.endswith("/list") for _, p, _ in plan) == P.MAX_ACTIONS * P.MAX_DATES


def test_shape_reports_keys_not_values():
    s = P.shape({"products": [{"id": 1, "price": {"amount": "10", "currency": "RUB"}}], "total": 1})
    assert s == {"top_keys": ["products", "total"], "wrapped_in_result": False, "items": 1,
                 "item_keys": ["id", "price"], "money_fields": ["price"]}
