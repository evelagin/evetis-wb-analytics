#!/usr/bin/env python3
"""Runtime ingestion домена Ozon. Точка входа Cloud Run Job.

Изоляция отказов: сбой одной сущности не останавливает остальные.
Каждая сущность пишет свою строку в OZON_INGESTION_RUNS.

ENV:
  ENTITIES     список через запятую; по умолчанию все
  SINCE/UNTIL  явное окно YYYY-MM-DD; по умолчанию lookback сущности
  LOOKBACK_OVERRIDE  переопределить окно ретроспективы в днях
"""
import os, sys, uuid
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from entities import REGISTRY


def main():
    run_id = os.environ.get("INGESTION_RUN_ID") or f"rt-{uuid.uuid4()}"
    want = [e for e in os.environ.get("ENTITIES", "").split(",") if e] or list(REGISTRY)
    since = os.environ.get("SINCE"); until = os.environ.get("UNTIL")
    lb_over = os.environ.get("LOOKBACK_OVERRIDE")
    today = C.now_msk().date()
    ts = C.now_msk().isoformat()
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
            C.record_run(run_id, name, started, frm, to, {}, "FAILED", error=repr(e),
                         requests_n=C.STATS["requests"] - r0,
                         retries=C.STATS["retries"] - t0)
            C.log(event="entity_failed", entity=name, error=repr(e)[:400])
            failed += 1
    C.log(event="run_end", ingestion_run_id=run_id, entities_ok=ok,
          entities_failed=failed, total_requests=C.STATS["requests"],
          total_retries=C.STATS["retries"],
          status="OK" if not failed else ("PARTIAL" if ok else "FAILED"))
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
