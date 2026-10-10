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
import os, re, sys, uuid
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
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):   # не ISO-неделя «2026-W39-1»
                raise ValueError
            parsed[name] = date.fromisoformat(v)
        except ValueError:
            return f"{name} не дата YYYY-MM-DD"
        if parsed[name] > today:
            return f"{name} позже сегодняшних суток МСК"
    if "SINCE" in parsed and "UNTIL" in parsed and parsed["SINCE"] > parsed["UNTIL"]:
        return "SINCE позже UNTIL"
    return None


def entity_window(since, until, lb_over, lookback, today):
    """Фактическое окно сущности — ровно так, как его строит main()."""
    lb = int(lb_over) if lb_over else lookback
    return (since or str(today - timedelta(days=lb))), (until or str(today))


def validate_plan(want, since, until, lb_over, today):
    """Строгий режим: ошибка фактического окна любой сущности (а не только сырых SINCE/UNTIL).

    UNTIL без SINCE или отрицательный LOOKBACK_OVERRIDE давали окно «начало позже конца» —
    пустой прогон со статусом OK.
    """
    err = validate_window(since, until, today)
    if err:
        return err
    if lb_over and not re.fullmatch(r"\d{1,4}", lb_over):     # пустая строка = не задана, как в main()
        return "LOOKBACK_OVERRIDE не целое неотрицательное число"
    for name in want:
        # Окно имеет смысл только у сущностей с ретроспективой (история); снимки его не читают.
        if name not in REGISTRY or REGISTRY[name][1] == 0:
            continue
        frm, to = entity_window(since, until, lb_over, REGISTRY[name][1], today)
        if date.fromisoformat(frm) > date.fromisoformat(to):
            return f"{name}: начало окна {frm} позже конца {to}"
    return None


ADS_ENTITIES = ("ads_campaigns", "ads_expense_daily", "ads_sku_daily")


def binding_gate(want, now, *, metadata_only=False):
    """TENANT_BINDING_REQUIRED=1 (арендатор, T5): загрузка только в подтверждённый кабинет.

    Истина — таблицы-маркеры владельца в ref (tables.list/tables.get консистентны; у runtime READER
    на ref, писать туда он не может):
      * ref.OPH_<n> — стоп-кран владельца: последний suspend — отказ;
      * ref.OPB_<api>_<n> — события привязки; действует последнее (номер, не часы). Отзыв виден сразу.
    Затем runtime сам снимает отпечаток кабинета в ЭТОМ прогоне (/v1/seller/info + Client-Id) и
    сверяет его с подтверждённым. Нет подтверждения, отзыв, несовпадение отпечатка — (статус, причина)
    с отказом до записи данных. Для рекламных сущностей так же проверяется Performance.
    """
    import identity as I
    import lifecycle_core as LCORE
    client = C.bq()
    ref = f"{C.PROJECT}.{C.REF_DATASET}"
    names = [t.table_id for t in client.list_tables(ref)]
    holds = [n for n in names if LCORE.HOLD_MARKER_RE.match(n)]
    series = {"OPH": [int(LCORE.HOLD_MARKER_RE.match(n).group(1)) for n in holds]}
    for api in (I.SELLER, I.PERFORMANCE):
        series[api] = [int(m.group(2)) for n in names if (m := I.BINDING_MARKER_RE.match(n)) and m.group(1) == api]
    if any(LCORE.series_gap(v) for v in series.values()):
        return "tenant:INVALID_OWNER_SIGNS", "серия знаков владельца в ref с пропуском — отказ"
    if holds:
        last = max(holds)
        if LCORE.hold_active([(last, getattr(client.get_table(f"{ref}.{last}"), "labels", None))]):
            return "tenant:SUSPENDED", "стоп-кран владельца (ref.OPH_*)"
    need = [I.SELLER] + ([I.PERFORMANCE] if any(e in ADS_ENTITIES for e in want) else [])
    items = []
    for api in need:
        mine = [n for n in names if (m := I.BINDING_MARKER_RE.match(n)) and m.group(1) == api]
        if mine:
            t = client.get_table(f"{ref}.{max(mine)}")
            items.append((max(mine), getattr(t, "labels", None), getattr(t, "description", None)))
    bindings = {api: I.binding_from_markers(items, api, now) for api in need}
    for api in need:
        # Нет действующего подтверждения — отказ ДО секретов и до Ozon (новый арендатор, отзыв).
        if bindings[api].status != "CONFIRMED":
            return f"{api}:{bindings[api].status}", bindings[api].reason
    if metadata_only:
        return None, 'stored binding metadata only; no source identity call'
    code, si = C.seller_post("/v1/seller/info", {})
    fp = None
    if code == 200:
        company = (si or {}).get("company") or {}
        try:
            fp = I.seller_fingerprint(C.seller_client_id(), company.get("inn"), company.get("ogrn"),
                                      version=I.fingerprint_version(bindings[I.SELLER].fingerprint))
        except I.IdentityError:
            fp = None
    status, reason = I.live_status(bindings[I.SELLER], fp, company.get("ogrn") if code == 200 else None)
    if status != I.BOUND:
        return f"seller:{status}", reason
    if I.PERFORMANCE in need:
        if I.fingerprint_version(fp) == I.SELLER_V2 and bindings[I.PERFORMANCE].seller_binding_fingerprint != fp:
            return "performance:INVALID_BINDING", "cross-API Seller linkage unproven"
        pfp = I.performance_fingerprint(C.perf_client_id())
        status, reason = I.live_status(bindings[I.PERFORMANCE], pfp)
        if status != I.BOUND:
            return f"performance:{status}", reason
    return None, "ok"


def ingestion_execution_contract(want, env):
    """No environment omission may turn an external-tenant execution into legacy mode."""
    if not want or len(set(want)) != len(want) or any(e not in REGISTRY for e in want):
        raise C.ConfigError("unknown/empty ingestion entity contract")
    external = C.PROJECT != C.LEGACY_INGESTION_PROJECT
    if external and env.get("TENANT_BINDING_REQUIRED") != "1":
        raise C.ConfigError("external ingestion requires binding contract")
    if external and "promo" in want:
        raise C.ConfigError("promo outside external-tenant ingestion contract")
    C._seller_execution_scope = frozenset({"runtime"} if external or "promo" not in want else {"runtime", "promo"})
    return external or env.get("TENANT_BINDING_REQUIRED") == "1"


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
        window_error = validate_plan(want, since, until, lb_over, today)
        if window_error:
            C.log(event="run_rejected", ingestion_run_id=run_id, reason=window_error)
            sys.exit(2)
    try:
        binding_required = ingestion_execution_contract(want, os.environ)
    except C.ConfigError:
        C.log(event="run_rejected", ingestion_run_id=run_id, reason="ingestion execution contract denied")
        sys.exit(2)
    backfill_plan = None
    if "BACKFILL_MODE" in os.environ:
        import backfill_core as B
        try:
            if len(want) != 1 or not binding_required or C.PROJECT == C.LEGACY_INGESTION_PROJECT:
                raise B.EvidenceError("backfill requires isolated tenant and one entity")
            backfill_plan = B.plan(os.environ, want[0], C.PROJECT, C.DATASET, C.REF_DATASET, today)
        except B.EvidenceError as error:
            C.log(event="run_rejected", reason=C.safe_error_text(error))
            sys.exit(2)
    if 'TENANCY_INTERNAL_PREFLIGHT' in os.environ:
        if os.environ['TENANCY_INTERNAL_PREFLIGHT']!='1' or not binding_required or backfill_plan is None or want!=['ads_sku_daily'] or C.PROJECT==C.LEGACY_INGESTION_PROJECT:
            C.log(event='internal_preflight_denied');sys.exit(2)
        from datetime import datetime,timezone
        denied,_=binding_gate(want,datetime.now(timezone.utc),metadata_only=True)
        if denied:C.log(event='internal_preflight_denied');sys.exit(3)
        import backfill as F
        try:
            result=F.internal_preflight(backfill_plan,source_binding=False)
        except Exception:
            C.log(event='internal_preflight_failed');sys.exit(1)
        C.log(event='internal_preflight_pass',**result,source_calls=0,journal_writes=0,raw_writes=0)
        return
    if binding_required:
        from datetime import datetime, timezone
        denied, reason = binding_gate(want, datetime.now(timezone.utc))
        if denied:
            # Значения identity в журнал не попадают — только статус и причина.
            C.log(event="run_rejected", ingestion_run_id=run_id, binding=denied, reason=reason)
            sys.exit(3)
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
            if backfill_plan is not None:
                import backfill
                res = backfill.run_backfill(backfill_plan, run_id, ts)
            else:
                res = fn(run_id, ts, frm, to)
            entity_status = "IN_PROGRESS" if backfill_plan and not res["evidence"]["complete"] else "OK"
            C.record_run(run_id, name, started, frm, to, res, entity_status,
                         requests_n=C.STATS["requests"] - r0,
                         retries=C.STATS["retries"] - t0)
            ok += 1
        except Exception as e:                                   # изоляция отказов
            try:
                failure = {}
                if backfill_plan is not None:
                    import backfill
                    failure={'evidence':getattr(e,'backfill_failure_evidence',None) or backfill.failure_evidence(backfill_plan)}
                C.record_run(run_id, name, started, frm, to, failure, "FAILED", error=repr(e),
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
