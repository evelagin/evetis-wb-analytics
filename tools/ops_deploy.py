#!/usr/bin/env python3
"""Stage B · evetis_ops — развёртывание и проверки через BigQuery jobs API.

Хост www.googleapis.com (bigquery.googleapis.com в сети владельца блокируется).
Токен — `gcloud auth print-access-token`.

  apply  FILE... [--sandbox]            выполнить DDL/DML по одному оператору (стоп на первой ошибке)
  tests  FILE [--sandbox] [--only IDS]  выполнить блоки `-- @@TEST <id>`, каждый отдельным скриптом
  sql    "SQL" [--sandbox]              выполнить скрипт и напечатать строки результата
  drop-sandbox                          удалить объекты evetis_ops.ZZTEST_* (только их)

--sandbox переписывает каждое имя `project.evetis_ops.X` в `project.evetis_ops.ZZTEST_X`:
тот же код, другие объекты. Production-объекты в режиме песочницы не упоминаются.
Все задания помечены label stage=stage-b-ops (и sandbox=true/false).
"""
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

P = 'project-fa311fc0-4d87-4781-986'
API = f'https://www.googleapis.com/bigquery/v2/projects/{P}'
OPS_REF = re.compile(r'`' + re.escape(P) + r'\.evetis_ops\.(\w+)`')
_tok = {'v': None, 't': 0.0}


def token():
    if not _tok['v'] or time.time() - _tok['t'] > 1500:
        _tok['v'] = subprocess.run(['gcloud', 'auth', 'print-access-token'],
                                   capture_output=True, text=True, check=True).stdout.strip()
        _tok['t'] = time.time()
    return _tok['v']


def call(method, url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={'Authorization': 'Bearer ' + token(), 'Content-Type': 'application/json'})
    try:
        return json.load(urllib.request.urlopen(req, timeout=180))
    except urllib.error.HTTPError as e:
        try:
            return {'error': json.load(e).get('error')}
        except Exception:
            return {'error': {'message': f'HTTP {e.code}'}}


def sandboxed(sql):
    return OPS_REF.sub(lambda m: m.group(0) if m.group(1).startswith('ZZTEST_')
                       else f'`{P}.evetis_ops.ZZTEST_{m.group(1)}`', sql)


def run(sql, sandbox=False, label='job', fetch=False):
    """jobs.insert + опрос. Возвращает (ok, message, rows)."""
    if sandbox:
        sql = sandboxed(sql)
    r = call('POST', f'{API}/jobs', {
        'configuration': {'query': {'query': sql, 'useLegacySql': False},
                          'labels': {'stage': 'stage-b-ops', 'sandbox': 'true' if sandbox else 'false'}},
        'jobReference': {'projectId': P, 'location': 'EU'}})
    if 'error' in r:
        return False, 'INSERT: ' + (r['error'] or {}).get('message', '?'), None
    jid = r['jobReference']['jobId']
    t0 = time.time()
    while True:
        j = call('GET', f'{API}/jobs/{jid}?location=EU')
        st = j.get('status', {})
        if st.get('state') == 'DONE':
            break
        time.sleep(2)
    if st.get('errorResult'):
        return False, f'{jid}: {st["errorResult"].get("message", "")}', None
    stats = j.get('statistics', {})
    msg = (f'{jid} {time.time() - t0:.0f}s type={stats.get("query", {}).get("statementType")} '
           f'child_jobs={stats.get("numChildJobs")}')
    rows = None
    if fetch:
        res = call('GET', f'{API}/queries/{jid}?location=EU&maxResults=2000')
        fields = [f['name'] for f in res.get('schema', {}).get('fields', [])]
        rows = [fields] + [[c.get('v') for c in row['f']] for row in res.get('rows', [])]
    return True, msg, rows


def statements(path):
    """Операторы файла DDL: граница — строка, начинающаяся с CREATE / MERGE / INSERT / UPDATE в колонке 0."""
    text = open(path, encoding='utf-8').read()
    parts = re.split(r'(?m)^(?=(?:CREATE|MERGE|INSERT|UPDATE) )', text)
    out = []
    for p in parts:
        body = re.sub(r'(?m)^\s*--.*$', '', p).strip()
        if body:
            out.append(p.rstrip())
    return out


def head(stmt):
    for line in stmt.splitlines():
        if line and not line.startswith('--'):
            return line[:110]
    return '?'


def tests(path):
    text = open(path, encoding='utf-8').read()
    blocks = re.split(r'(?m)^-- @@TEST ', text)[1:]
    out = []
    for b in blocks:
        first, _, body = b.partition('\n')
        tid, _, desc = first.strip().partition(' ')
        out.append((tid, desc, body.strip()))
    return out


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    sandbox = '--sandbox' in args
    args = [a for a in args if a != '--sandbox']
    only = None
    if '--only' in args:
        i = args.index('--only')
        only = set(args[i + 1].split(','))
        args = args[:i] + args[i + 2:]
    tag = 'SANDBOX' if sandbox else 'PROD'

    if cmd == 'apply':
        n = 0
        for path in args:
            for s in statements(path):
                ok, msg, _ = run(s, sandbox, fetch=False)
                n += 1
                print(f'[{tag}] {"OK  " if ok else "FAIL"} {path.split("/")[-1]} · {head(s if not sandbox else sandboxed(s))} · {msg}',
                      flush=True)
                if not ok:
                    return 1
        print(f'[{tag}] applied {n} statements')
        return 0

    if cmd == 'tests':
        path = args[0]
        passed = failed = 0
        for tid, desc, body in tests(path):
            if only and tid not in only:
                continue
            ok, msg, _ = run(body, sandbox, label=tid)
            print(f'[{tag}] {"PASS" if ok else "FAIL"} {tid} {desc}' + ('' if ok else f'\n       → {msg}'), flush=True)
            passed += ok
            failed += not ok
        print(f'[{tag}] {passed} PASS / {failed} FAIL')
        return 0 if failed == 0 else 1

    if cmd == 'sql':
        ok, msg, rows = run(args[0], sandbox, fetch=True)
        if not ok:
            print('FAIL', msg)
            return 1
        for r in rows or []:
            print('\t'.join('' if v is None else str(v) for v in r))
        return 0

    if cmd == 'drop-sandbox':
        ok, msg, rows = run(f"""
          SELECT table_name, table_type FROM `{P}.evetis_ops.INFORMATION_SCHEMA.TABLES` WHERE STARTS_WITH(table_name, 'ZZTEST_')
          UNION ALL
          SELECT routine_name, 'PROCEDURE' FROM `{P}.evetis_ops.INFORMATION_SCHEMA.ROUTINES` WHERE STARTS_WITH(routine_name, 'ZZTEST_')""",
                            fetch=True)
        if not ok:
            print('FAIL', msg)
            return 1
        objs = rows[1:]
        # сначала процедуры и представления (зависят от таблиц), затем таблицы
        order = {'PROCEDURE': 0, 'VIEW': 1, 'BASE TABLE': 2}
        for name, typ in sorted(objs, key=lambda x: order.get(x[1], 9)):
            assert name.startswith('ZZTEST_')
            kind = {'PROCEDURE': 'PROCEDURE', 'VIEW': 'VIEW'}.get(typ, 'TABLE')
            ok, msg, _ = run(f'DROP {kind} `{P}.evetis_ops.{name}`')
            print(f'{"OK  " if ok else "FAIL"} DROP {kind} {name} · {msg}', flush=True)
            if not ok:
                return 1
        print(f'dropped {len(objs)} sandbox objects')
        return 0

    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
