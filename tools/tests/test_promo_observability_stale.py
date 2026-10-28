"""V_PROMO_OBSERVABILITY_CURRENT: устаревший снимок не показывается как наблюдаемый (инцидент 14–28.10.2026).

Ozon отдавал OBSERVED/COMPLETE при снимке 14.10 (возраст 19 956 мин). Порог STALE обязан совпадать с FAIL
контракта DRO-1 для ozon_promo и wb_promo — иначе здоровье данных и наблюдаемость акций разойдутся.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
VIEW = (ROOT / "sql/current/evetis_mart/V_PROMO_OBSERVABILITY_CURRENT.sql").read_text(encoding="utf-8")
DRO = (ROOT / "sql/health/dro1_01_contract.sql").read_text(encoding="utf-8")


def dro_fail_minutes(pipeline: str) -> int:
    m = re.search(rf"\('{pipeline}',.*?'AGE', '[^']*', \d+, NULL, (\d+), (\d+),", DRO, re.S)
    assert m, pipeline
    return int(m.group(2))


def case_branches() -> list[str]:
    body = VIEW[VIEW.index("  CASE\n"):VIEW.index("END AS capability_status")]
    return re.findall(r"WHEN (.*?) THEN '([A-Z_]+)'", body)


def test_stale_threshold_equals_dro1_fail_for_both_marketplaces():
    (cond, status), = [(c, s) for c, s in case_branches() if s == "STALE"]
    threshold = int(re.search(r"MINUTE\) > (\d+)$", cond).group(1))
    assert threshold == dro_fail_minutes("ozon_promo") == dro_fail_minutes("wb_promo") == 780


def test_stale_precedes_every_observed_status_but_not_source_absence():
    order = [s for _, s in case_branches()]
    assert order[0] == "NOT_PROVIDED_BY_SOURCE"     # отсутствие источника от возраста не зависит
    assert order[1:3] == ["NO_VALID_OBSERVATION", "STALE"]  # нет снимка вовсе — раньше, чем «снимок старый»
    for s in ("NO_UNITS_IN_LATEST_OBSERVATION", "OBSERVED", "NOT_OBSERVABLE"):
        assert order.index(s) > order.index("STALE")
    assert "PARTIALLY_OBSERVED" in VIEW.split("END AS capability_status")[0]


def test_stale_uses_same_age_as_reported_column():
    (cond, _), = [(c, s) for c, s in case_branches() if s == "STALE"]
    assert cond.startswith("TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), observed_at, MINUTE)")
    assert "TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), observed_at, MINUTE) AS observation_age_minutes" in VIEW


def test_marketplace_without_any_valid_observation_is_reported_not_dropped():
    # Каркас площадок: без годного снимка строки площадки остаются (live-симуляция 28.10: прежняя версия — 0 строк Ozon).
    assert "FROM UNNEST(['WB', 'OZON']) AS marketplace" in VIEW
    assert "FROM markets k\n  LEFT JOIN mp ON mp.marketplace = k.marketplace" in VIEW
    caps = VIEW[VIEW.index("caps AS ("):VIEW.index("SELECT\n  marketplace,\n  capability,")]
    assert "mp.marketplace" not in caps.replace("LEFT JOIN mp ON mp.marketplace = k.marketplace", "")
    order = [s for _, s in case_branches()]
    assert order[:3] == ["NOT_PROVIDED_BY_SOURCE", "NO_VALID_OBSERVATION", "STALE"]
    (cond, _), = [(c, s) for c, s in case_branches() if s == "NO_VALID_OBSERVATION"]
    assert cond == "observed_at IS NULL"   # иначе NULL-возраст проваливался бы в NO_UNITS_IN_LATEST_OBSERVATION
