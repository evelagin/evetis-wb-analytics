#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PR-Mart3a — применение интеграций heartbeat-журнала в три загрузчика Apps Script.

ЗАЧЕМ ЭТОТ СКРИПТ. Правки вносятся в конкретные точки трёх больших файлов; ручная вставка
легко попадает в середину многострочного вызова console.log(...) и ломает синтаксис.
Скрипт делает это детерминированно: восстанавливает исходник из бэкапа, применяет все
патчи по уникальным якорям, проверяет результат и печатает контекст для ревью.

ЗАПУСК (из корня репозитория evetis-wb-analytics):
    python3 tools/apply_mart3a_patches.py            # применить (создаст .pre_mart3a.bak при первом запуске)
    python3 tools/apply_mart3a_patches.py --check    # только проверить текущее состояние, ничего не менять
    python3 tools/apply_mart3a_patches.py --restore  # откатить файлы из .pre_mart3a.bak

Идемпотентен: повторный запуск не дублирует вставки. Всегда начинает с чистого исходника
(из .pre_mart3a.bak), поэтому корректен даже если предыдущая попытка оставила файлы «наполовину».
"""
import io
import os
import sys
import shutil
import subprocess

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'apps-script')
BASE = os.path.normpath(BASE)
SUFFIX = '.pre_mart3a.bak'

FILES = ['WbOrdersLoader', 'WbSalesIncremental.gs', 'WbAdsDaily.gs']

# (файл, якорь, вставка, before|after, маркер идемпотентности)
PATCHES = [
    # ── ORDERS ───────────────────────────────────────────────────────────────
    ('WbOrdersLoader',
     "  try {\n    wbOrdersIncrementalCore_(r);",
     "  // PR-Mart3a: heartbeat-журнал. Открываем ПОСЛЕ захвата lock — пропуск по локу это не ран.\n"
     "  var ingestRunIdOrders = ingestRunStart_('orders', ingestClosedDayMsk_(), 'SCHEDULED');\n",
     'before', 'ingestRunIdOrders ='),

    # ВАЖНО: якорь — ПОСЛЕДНЯЯ строка многострочного console.log(...). Вставка идёт ПОСЛЕ неё,
    # иначе код попадёт внутрь вызова console.log и сломает синтаксис.
    ('WbOrdersLoader',
     "    (r.error_message ? ' | ' + r.error_message : ''));\n",
     "  // PR-Mart3a: финализация по whitelist (OK/OK_NO_CHANGES = успех; PARTIAL/прочее -> ERROR).\n"
     "  ingestFinalizeByStatus_(ingestRunIdOrders, 'orders', r.status, r.api_rows_received, r.rows_appended, r.error_message);\n",
     'after', 'ingestFinalizeByStatus_(ingestRunIdOrders'),

    # ── SALES ────────────────────────────────────────────────────────────────
    ('WbSalesIncremental.gs',
     "  try {\n    wbSalesIncrementalCore_(r);",
     "  // PR-Mart3a: heartbeat-журнал. Ранний SKIPPED_LOCKED выше — там ран не открываем.\n"
     "  var ingestRunIdSales = ingestRunStart_('sales', ingestClosedDayMsk_(), 'SCHEDULED');\n",
     'before', 'ingestRunIdSales ='),

    ('WbSalesIncremental.gs',
     "    ' written=' + r.rows_written + (r.error_message ? ' | ' + r.error_message : ''));\n",
     "  // PR-Mart3a: whitelist (OK/OK_NO_CHANGES = успех; SKIPPED_RATE_LIMIT/PARTIAL/прочее -> ERROR).\n"
     "  ingestFinalizeByStatus_(ingestRunIdSales, 'sales', r.status, r.api_rows_received, r.rows_written, r.error_message);\n",
     'after', 'ingestFinalizeByStatus_(ingestRunIdSales'),

    # ── ADS ──────────────────────────────────────────────────────────────────
    ('WbAdsDaily.gs',
     "  var rng = null;\n",
     "  var ingestRunIdAds = null;   // PR-Mart3a: id строки heartbeat-журнала\n",
     'after', 'ingestRunIdAds = null'),

    ('WbAdsDaily.gs',
     "      wbAdsDailyWriteStatus_(runId, '', '', 'ERROR', 'WB_ADS_BQ_SINK выключен');\n",
     "      // PR-Mart3a: early-return — фиксируем неуспех явно (точный период API ещё не вычислен,\n"
     "      // берём закрытый день; в норме он совпадает с rng.to).\n"
     "      ingestRunIdAds = ingestRunStart_('ads', ingestClosedDayMsk_(), 'SCHEDULED');\n"
     "      ingestRunError_(ingestRunIdAds, 'ADS_SINK_OFF', 'WB_ADS_BQ_SINK выключен');\n",
     'after', 'ADS_SINK_OFF'),

    ('WbAdsDaily.gs',
     "    rng = wbAdsLast7Range_();\n",
     "    // PR-Mart3a: logical_period = ЦЕЛЕВОЙ ПЕРИОД API (period_to), а не дата запуска.\n"
     "    ingestRunIdAds = ingestRunStart_('ads', rng.to, 'SCHEDULED');\n",
     'after', "ingestRunStart_('ads', rng.to"),

    ('WbAdsDaily.gs',
     "    return { status: overall, run_id: runId, results: results,",
     "    // PR-Mart3a: whitelist (OK/STALE = успех; PARTIAL/ERROR -> ERROR-heartbeat).\n"
     "    var adsRowsTotal = results.reduce(function (a, x) { return a + ((x && x.rows) || 0); }, 0);\n"
     "    ingestFinalizeByStatus_(ingestRunIdAds, 'ads', overall, adsRowsTotal, adsRowsTotal, note);\n",
     'before', 'ingestFinalizeByStatus_(ingestRunIdAds'),

    ('WbAdsDaily.gs',
     "    return { status: 'ERROR', run_id: runId, error_message: em };",
     "    ingestRunError_(ingestRunIdAds, 'ADS_EXCEPTION', em);   // PR-Mart3a: catch-ветка\n",
     'before', 'ADS_EXCEPTION'),

    # ── ADS REV5 (fail-open в расчёте overall) ────────────────────────────────
    # Блокер аудита REV4: results.some(x && x.status && ...) НЕ считал null/{}/без-status
    # ошибкой -> overall становился OK/STALE ДО whitelist -> ложный COMPLETE-heartbeat.
    # Правка: чистая функция wbAdsOverallStatus_(results, stale) со строгой валидацией числа
    # результатов и наличия status у каждого + offline self-test wbAdsSelfTestOverallStatus().
    ('WbAdsDaily.gs',
     "var WB_ADS_DAILY_STALE_DAYS_    = 2;       // fullstats за вчера — норма; > N дней = устаревание\n",
     "\n"
     "// PR-Mart3a REV5: fail-closed расчёт итогового статуса рекламного прогона.\n"
     "var WB_ADS_EXPECTED_SOURCES_ = 3;  // campaigns + costs + fullstats\n"
     "\n"
     "/**\n"
     " * Чистый расчёт итогового статуса рекламного прогона (без API/BQ — тестируется offline).\n"
     " * Fail-closed: отсутствующий/битый результат, нет поля status, неизвестный статус ИЛИ\n"
     " * неверное число результатов => ERROR. Иначе null/{}/сырой статус проскочил бы ДО whitelist\n"
     " * на этапе overall и дал бы ложный OK/STALE => ложный COMPLETE-heartbeat => Mart3 собрал бы\n"
     " * витрину на неполной рекламе.\n"
     " *   OK+OK+OK               => OK (или STALE, если stale===true)\n"
     " *   >=1 PARTIAL без ошибок => PARTIAL\n"
     " *   null / {} / нет status / неизвестный статус / не 3 результата / не массив => ERROR\n"
     " * @param {Array} results  результаты трёх per-source загрузчиков\n"
     " * @param {boolean|null} stale  признак устаревания fullstats (true|false|null)\n"
     " * @return {string} 'OK' | 'PARTIAL' | 'STALE' | 'ERROR'\n"
     " */\n"
     "function wbAdsOverallStatus_(results, stale) {\n"
     "  var invalid =\n"
     "    !Array.isArray(results) ||\n"
     "    results.length !== WB_ADS_EXPECTED_SOURCES_ ||\n"
     "    results.some(function (x) { return !x || !x.status; });\n"
     "  if (invalid) return 'ERROR';\n"
     "\n"
     "  var badStatus = results.some(function (x) {\n"
     "    var st = String(x.status).toUpperCase();\n"
     "    return st !== 'OK' && st !== 'PARTIAL';\n"
     "  });\n"
     "  if (badStatus) return 'ERROR';\n"
     "\n"
     "  var anyPartial = results.some(function (x) {\n"
     "    return String(x.status).toUpperCase() === 'PARTIAL';\n"
     "  });\n"
     "  if (anyPartial) return 'PARTIAL';\n"
     "\n"
     "  return stale === true ? 'STALE' : 'OK';\n"
     "}\n"
     "\n"
     "/**\n"
     " * Регрессионный self-тест wbAdsOverallStatus_ (offline, без API/BQ).\n"
     " * Запускать из редактора Apps Script. Бросает Error при первом расхождении.\n"
     " */\n"
     "function wbAdsSelfTestOverallStatus() {\n"
     "  function r(status) { return { source: 's', status: status, rows: 0 }; }\n"
     "  var cases = [\n"
     "    { n: 'OK+OK+OK',              in: [r('OK'), r('OK'), r('OK')],          stale: false, exp: 'OK' },\n"
     "    { n: 'OK+OK+OK stale=true',   in: [r('OK'), r('OK'), r('OK')],          stale: true,  exp: 'STALE' },\n"
     "    { n: 'OK+OK+OK stale=null',   in: [r('OK'), r('OK'), r('OK')],          stale: null,  exp: 'OK' },\n"
     "    { n: 'один PARTIAL',          in: [r('OK'), r('PARTIAL'), r('OK')],     stale: false, exp: 'PARTIAL' },\n"
     "    { n: 'PARTIAL важнее STALE',  in: [r('OK'), r('PARTIAL'), r('OK')],     stale: true,  exp: 'PARTIAL' },\n"
     "    { n: 'ERROR перебивает всё',  in: [r('OK'), r('ERROR'), r('OK')],       stale: true,  exp: 'ERROR' },\n"
     "    { n: 'null-результат',        in: [r('OK'), null, r('OK')],             stale: false, exp: 'ERROR' },\n"
     "    { n: 'пустой объект {}',      in: [r('OK'), {}, r('OK')],               stale: false, exp: 'ERROR' },\n"
     "    { n: 'нет поля status',       in: [r('OK'), { source: 's' }, r('OK')],  stale: false, exp: 'ERROR' },\n"
     "    { n: 'неизвестный статус',    in: [r('OK'), r('WAT'), r('OK')],         stale: false, exp: 'ERROR' },\n"
     "    { n: 'мало результатов (2)',  in: [r('OK'), r('OK')],                   stale: false, exp: 'ERROR' },\n"
     "    { n: 'много результатов (4)', in: [r('OK'), r('OK'), r('OK'), r('OK')], stale: false, exp: 'ERROR' },\n"
     "    { n: 'не массив',             in: null,                                  stale: false, exp: 'ERROR' },\n"
     "    { n: 'регистр ok/partial',    in: [r('ok'), r('partial'), r('OK')],     stale: false, exp: 'PARTIAL' }\n"
     "  ];\n"
     "  var failed = 0;\n"
     "  for (var i = 0; i < cases.length; i++) {\n"
     "    var c = cases[i];\n"
     "    var got = wbAdsOverallStatus_(c.in, c.stale);\n"
     "    var ok = got === c.exp;\n"
     "    if (!ok) failed++;\n"
     "    Logger.log((ok ? 'OK   ' : 'FAIL ') + c.n + ' => ожид. ' + c.exp + ', получено ' + got);\n"
     "  }\n"
     "  if (failed > 0) throw new Error('wbAdsSelfTestOverallStatus: провалено ' + failed + ' из ' + cases.length);\n"
     "  Logger.log('wbAdsSelfTestOverallStatus: все ' + cases.length + ' кейсов прошли OK');\n"
     "  return { passed: cases.length, failed: 0 };\n"
     "}\n",
     'after', 'function wbAdsOverallStatus_'),

    ('WbAdsDaily.gs',
     "    // Иерархия статусов: ошибка любого источника → ERROR; неполнота → PARTIAL;\n"
     "    // всё успешно, но данные устарели → STALE; иначе OK.\n"
     "    var hasError = results.some(function (x) {\n"
     "      return x && x.status && x.status !== 'OK' && x.status !== 'PARTIAL';\n"
     "    });\n"
     "    var hasPartial = results.some(function (x) { return x && x.status === 'PARTIAL'; });\n"
     "\n"
     "    var fresh = wbAdsDailyFreshness_();\n"
     "    var overall = hasError ? 'ERROR' : (hasPartial ? 'PARTIAL' : (fresh.stale === true ? 'STALE' : 'OK'));\n",
     "    // PR-Mart3a REV5: итог вынесен в чистую wbAdsOverallStatus_ (fail-closed, offline self-test).\n"
     "    // null/{}/без-status/неизвестный статус/не 3 результата => ERROR (не проскочит в OK до whitelist).\n"
     "    var fresh = wbAdsDailyFreshness_();\n"
     "    var overall = wbAdsOverallStatus_(results, fresh.stale);\n",
     'replace', 'wbAdsOverallStatus_(results, fresh.stale)'),
]


def path_of(name):
    return os.path.join(BASE, name)


def read(p):
    with io.open(p, encoding='utf-8') as fh:
        return fh.read()


def write(p, t):
    with io.open(p, 'w', encoding='utf-8') as fh:
        fh.write(t)


def ensure_backups():
    for f in FILES:
        src, bak = path_of(f), path_of(f) + SUFFIX
        if not os.path.exists(src):
            print('!! нет файла: %s' % src)
            sys.exit(2)
        if not os.path.exists(bak):
            shutil.copy2(src, bak)
            print('   бэкап создан: %s%s' % (f, SUFFIX))


def restore():
    for f in FILES:
        bak = path_of(f) + SUFFIX
        if os.path.exists(bak):
            shutil.copy2(bak, path_of(f))
            print('   восстановлен из бэкапа: %s' % f)
        else:
            print('   бэкапа нет, пропуск: %s' % f)


def syntax_check():
    """node --check, если node доступен (файлы .gs — обычный JS)."""
    try:
        subprocess.run(['node', '--version'], capture_output=True, check=True)
    except Exception:
        print('   node недоступен — синтаксис не проверен (проверьте в редакторе Apps Script)')
        return True
    ok = True
    for f in FILES:
        tmp = '/tmp/_syncheck_%s.js' % f.replace('.', '_')
        shutil.copy2(path_of(f), tmp)
        r = subprocess.run(['node', '--check', tmp], capture_output=True, text=True)
        if r.returncode == 0:
            print('   синтаксис OK: %s' % f)
        else:
            ok = False
            print('!! СИНТАКСИС СЛОМАН: %s\n%s' % (f, r.stderr.strip()[:500]))
    return ok


def check_state():
    print('Состояние интеграций:')
    all_ok = True
    for f in FILES:
        t = read(path_of(f))
        markers = [m for (fn, _a, _i, _p, m) in PATCHES if fn == f]
        missing = [m for m in markers if m not in t]
        status = 'применено полностью' if not missing else ('НЕ применено: %s' % ', '.join(missing))
        if missing:
            all_ok = False
        print('  %-26s %s' % (f, status))
    return all_ok


def apply_all():
    ensure_backups()
    print('Восстанавливаю исходники из бэкапов (гарантия чистого старта):')
    restore()
    print('Применяю патчи:')
    failed = []
    for fname, anchor, insert, pos, marker in PATCHES:
        p = path_of(fname)
        t = read(p)
        if marker in t:
            print('   = уже есть: %s -> %s' % (fname, marker))
            continue
        n = t.count(anchor)
        if n != 1:
            failed.append((fname, marker, 'якорь найден %d раз (нужно ровно 1)' % n))
            print('   ! %s -> %s: якорь найден %d раз' % (fname, marker, n))
            continue
        if pos == 'before':
            new = insert + anchor
        elif pos == 'after':
            new = anchor + insert
        elif pos == 'replace':
            new = insert
        else:
            failed.append((fname, marker, 'неизвестная позиция %r' % pos))
            print('   ! %s -> %s: неизвестная позиция %r' % (fname, marker, pos))
            continue
        t = t.replace(anchor, new, 1)
        write(p, t)
        print('   + %s -> %s' % (fname, marker))
    print('Проверка синтаксиса:')
    ok = syntax_check()
    if failed or not ok:
        print('\nРЕЗУЛЬТАТ: ЕСТЬ ПРОБЛЕМЫ — файлы можно откатить: python3 tools/apply_mart3a_patches.py --restore')
        sys.exit(1)
    print('\nРЕЗУЛЬТАТ: OK. Проверьте diff (git diff apps-script/) и перенесите правки в Apps Script.')
    print('После ревью удалите бэкапы: rm apps-script/*%s' % SUFFIX)


if __name__ == '__main__':
    arg = sys.argv[1] if len(sys.argv) > 1 else ''
    if arg == '--restore':
        restore()
    elif arg == '--check':
        check_state()
        syntax_check()
    else:
        apply_all()
