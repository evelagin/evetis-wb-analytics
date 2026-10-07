"""Phase 1A (2026-10-07): инструмент развёртывания доказанных отказов вне Orders API — порядок, тела, откат."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import unitka_refusals_deploy as dep  # noqa: E402

COLS = ("refusal_counted_qty", "V_UNITKA_REFUSAL_DAILY")


def test_forward_order_follows_dependency_graph():
    names = [n for _, n, _, _ in dep.plan(False)]
    assert names == ["V_UNITKA_REFUSAL_EVIDENCE", "V_UNITKA_REFUSAL_DAILY", "V_UNITKA_DAILY_FACT",
                     "V_UNITKA_INTEGRITY", "V_UNITKA_RECON_FACT", "V_UNITKA_RECON_INTEGRITY"]
    for ds, _, stmt, _ in dep.plan(False):
        assert ds == "wb_mart"
        assert not dep.FORBIDDEN.search(stmt.replace("'", "")), stmt[:80]


def test_forward_bodies_carry_refusals_and_rollback_bodies_do_not():
    fwd = {n: b for _, n, _, b in dep.plan(False)}
    for n in ("V_UNITKA_DAILY_FACT", "V_UNITKA_RECON_FACT"):
        assert all(c in fwd[n] for c in COLS), n
    rb = dep.plan(True)
    assert [n for _, n, _, _ in rb] == ["V_UNITKA_RECON_INTEGRITY", "V_UNITKA_RECON_FACT", "V_UNITKA_INTEGRITY", "V_UNITKA_DAILY_FACT"]
    for _, n, _, body in rb:
        assert not any(c in body for c in COLS), f"{n}: тело отката содержит отказы"
        assert "PROXY_FACT_ORDERS+FINANCE_REFUSAL" not in body


def test_statement_body_stops_at_its_own_terminator():
    """Хвостовые комментарии следующей секции не входят в тело: иначе перечитывание из BigQuery не совпадёт."""
    tails = {"V_UNITKA_REFUSAL_EVIDENCE": "WHERE s.order_date_msk IS NOT NULL",
             "V_UNITKA_REFUSAL_DAILY": "WHERE k.excess > 0 OR k.proven > 0 OR k.sold > 0",
             "V_UNITKA_DAILY_FACT": "FROM p", "V_UNITKA_RECON_FACT": "FROM p",
             "V_UNITKA_INTEGRITY": "LEFT JOIN ref USING (nm_id)",
             "V_UNITKA_RECON_INTEGRITY": "LEFT JOIN obs ON obs.nm_id = f.nm_id AND obs.day = f.date_msk"}
    for rollback in (False, True):
        for _, n, stmt, body in dep.plan(rollback):
            assert body.endswith(tails[n]), (rollback, n, body[-80:])
            assert stmt.endswith(tails[n]) and ";" not in stmt[-200:], n
