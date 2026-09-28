#!/usr/bin/env python3
"""Runtime ingestion домена Ozon. Точка входа Cloud Run Job.

Изоляция отказов: сбой одной сущности не останавливает остальные.
Каждая сущность пишет свою строку в OZON_INGESTION_RUNS.

ENV:
  ENTITIES     список через запятую; по умолчанию все
  SINCE/UNTIL  явное окно YYYY-MM-DD; по умолчанию lookback сущности
  LOOKBACK_OVERRIDE  переопределить окно ретроспективы в днях

  Конфигурация арендатора (common.resolve_config, Tenancy T2):
  GCP_PROJECT_ID     ОБЯЗАТЕЛЕН; без него процесс не стартует
  BQ_RAW_DATASET     по умолчанию ozon_raw
  BQ_REF_DATASET     по умолчанию evetis_ref (переходно, T2)
  BQ_LOCATION        по умолчанию EU
  OZON_SECRET_SELLER_CLIENT_ID / OZON_SECRET_SELLER_API_KEY /
  OZON_SECRET_PERF_CLIENT_ID / OZON_SECRET_PERF_CLIENT_SECRET
                     ИМЕНА секретов Secret Manager; по умолчанию имена EVETIS
  STRICT_PAGE_CAPS   1 — упор в потолок страниц/окна API роняет сущность с
                     диагностикой (режим бэкфилла); 0 или не задана — как раньше
"""
import os, sys, uuid
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from entities import REGISTRY

# Tenancy T2.2 (L3): любое необработанное исключение печатается через границу
# безопасности — трассировка сохраняется, вырезаются только секреты. Ставится
# сразу после импорта common: раньше секреты загрузиться не могут.
sys.excepthook = C.safe_excepthook


def validate_window(since, until, today):
    """Строгий режим: текст ошибки окна SINCE/UNTIL или None.

    Даты — ровно YYYY-MM-DD; SINCE ≤ UNTIL; ни одна не позже сегодняшних суток МСК.
    """
    parsed = {}
    for name, v in (("SINCE", since), ("UNTIL", until)):
        if v is None:
            continue
        try:
            if len(v) != 10:
                raise ValueError
            parsed[name] = date.fromisoformat(v)
        except ValueError:
            return f"{name} не дата YYYY-MM-DD"
        if parsed[name] > today:
            return f"{name} позже сегодняшних суток МСК"
    if "SINCE" in parsed and "UNTIL" in parsed and parsed["SINCE"] > parsed["UNTIL"]:
        return "SINCE позже UNTIL"
    return None


def main():
    run_id = os.environ.get("INGESTION_RUN_ID") or f"rt-{uuid.uuid4()}"
    want = [e for e in os.environ.get("ENTITIES", "").split(",") if e] or list(REGISTRY)
    since = os.environ.get("SINCE"); until = os.environ.get("UNTIL")
    lb_over = os.environ.get("LOOKBACK_OVERRIDE")
    today = C.now_msk().date()
    ts = C.now_msk().isoformat()
    if C.STRICT_PAGE_CAPS:
        # T5: окно проверяется до первого обращения к API. SINCE > UNTIL раньше давал
        # пустой прогон со статусом OK; будущая дата — «полноту» ещё не наступивших суток.
        window_error = validate_window(since, until, today)
        if window_error:
            C.log(event="run_rejected", ingestion_run_id=run_id, reason=window_error)
            sys.exit(2)
    C.log(event="run_start", ingestion_run_id=run_id, marketplace="OZON",
          entities=want, since=since, until=until)
    ok = failed = 0
    for name in want:
        if name not in REGISTRY:
            C.log(event="entity_skipped", entity=name, reason="неизвестная сущность")
            failed += 1
            continue
        fn, lookback, _cad = REGISTRY[name]
        lb = int(lb_over) if lb_over else lookback
        frm = since or str(today - timedelta(days=lb))
        to = until or str(today)
        started = C.now_msk()
        r0, t0 = C.STATS["requests"], C.STATS["retries"]
        try:
            res = fn(run_id, ts, frm, to)
            C.record_run(run_id, name, started, frm, to, res, "OK",
                         requests_n=C.STATS["requests"] - r0,
                         retries=C.STATS["retries"] - t0)
            ok += 1
        except Exception as e:                                   # изоляция отказов
            try:
                C.record_run(run_id, name, started, frm, to, {}, "FAILED", error=repr(e),
                             requests_n=C.STATS["requests"] - r0,
                             retries=C.STATS["retries"] - t0)
            except Exception as journal_error:                   # noqa: BLE001
                # Поток управления прежний: сбой журнала обрывает прогон. Меняется
                # только текст: вместо неявной цепочки с сырым текстом исходной
                # ошибки — очищенная сводка обеих ошибок (L3).
                raise C.JournalWriteError(name, e, journal_error) from None
            C.log(event="entity_failed", entity=name, error=C.safe_error_text(repr(e)))
            failed += 1
    C.log(event="run_end", ingestion_run_id=run_id, entities_ok=ok,
          entities_failed=failed, total_requests=C.STATS["requests"],
          total_retries=C.STATS["retries"],
          status="OK" if not failed else ("PARTIAL" if ok else "FAILED"))
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
