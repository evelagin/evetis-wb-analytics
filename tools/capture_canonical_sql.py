#!/usr/bin/env python3
"""Снять каноническое определение вью с production в sql/current. ТОЛЬКО ЧТЕНИЕ.

Инструмент не развёртывает и не меняет BigQuery. Он делает ровно одно: переносит в
Git то, что production уже исполняет, вместе с доказательствами (хеши тела, схемы и
описания), чтобы дальнейшее расхождение стало заметным.

Почему не «переписать вью заново по миграции»: исторические миграции уже дважды
расходились с живым объектом (`REF_COST_MAP`, `V_PRODUCT_COGS_EFFECTIVE`). Канон — это
то, что исполняется, а не то, что когда-то задумывалось.

Два независимых чтения обязаны совпасть:
  * тело, описание и схема берутся REST-вызовом `tables.get` (тот же read-only
    транспорт и та же политика путей, что у R2C);
  * хеши считаются функцией `view_body` из валидатора R2B — второй реализации
    canonical_hash_v1 в репозитории нет и не будет.
Если хеш файла не сходится с хешем живого тела, файл не пишется.

  python tools/capture_canonical_sql.py --project <PROJECT> --dataset wb_mart \
      --objects V_DASH_KPI_DAILY V_DASH_SKU_DAILY \
      --token-command "gcloud auth print-access-token"
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import validate_current_sql as contract  # noqa: E402
import verify_current_sql_live as live  # noqa: E402

HEADER = """\
-- ============================================================================
-- CANONICAL CURRENT DEFINITION — {dataset}.{name} (VIEW)
-- Снято с production только чтением {captured_at} из {sha}.
-- Правила слоя: sql/current/README.md. Метаданные и хеши: MANIFEST.json.
--
-- Тело ниже — дословно то, что BigQuery хранит и исполняет сейчас. Его не
-- переформатируют: canonical_hash_v1 считается от байтов, и любая правка формата
-- превращается в расхождение с production.
-- Зависимости внутри датасета: {deps}
-- ============================================================================
"""


def object_url(host: str, project: str, dataset: str, name: str) -> str:
    q = urllib.parse.quote
    return (f"https://{host}{live.API_PREFIX}/projects/{q(project, safe='')}"
            f"/datasets/{q(dataset, safe='')}/tables/{q(name, safe='')}")


def git_sha() -> tuple[str, bool]:
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                                capture_output=True, text=True).stdout.strip())
    return sha, dirty


def render_file(project: str, dataset: str, name: str, description: str, body: str,
                deps: list[str], captured_at: str, sha: str) -> str:
    desc = json.dumps(description, ensure_ascii=False)  # экранирование кавычек и переводов строк
    header = HEADER.format(dataset=dataset, name=name, captured_at=captured_at,
                           sha=sha[:12], deps=", ".join(deps) or "нет")
    return (f"{header}CREATE OR REPLACE VIEW `{project}.{dataset}.{name}`\n"
            f"OPTIONS (description = {desc})\n"
            # Точка с запятой ставится ВПЛОТНУЮ к телу: canonical_hash_v1 сперва
            # обрезает края, а потом снимает ровно одну `;`. Перевод строки перед
            # `;` остался бы внутри тела и сдвинул хеш на один байт.
            f"AS\n{body.strip()};\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--objects", nargs="+", required=True)
    p.add_argument("--api-host", default=live.DEFAULT_API_HOST)
    p.add_argument("--token-env", default=live.DEFAULT_TOKEN_ENV)
    p.add_argument("--token-command")
    p.add_argument("--allowed-external", nargs="*", default=None,
                   help="датасеты, которые объектам этого датасета разрешено читать; "
                        "по умолчанию берутся из политики валидатора")
    p.add_argument("--dry-run", action="store_true", help="ничего не писать, только отчёт")
    args = p.parse_args(argv)

    policy = contract.EXTERNAL_DATASET_POLICY.get(args.dataset)
    if policy is None:
        print(f"ERROR: для датасета {args.dataset!r} нет записи в EXTERNAL_DATASET_POLICY "
              f"валидатора. Политика изоляции — решение ревью, а не следствие снятия.",
              file=sys.stderr)
        return 3
    allowed = set(args.allowed_external) if args.allowed_external is not None else set(policy)
    if not allowed <= set(policy):
        print(f"ERROR: --allowed-external шире политики валидатора {sorted(policy)}", file=sys.stderr)
        return 3

    class _A:
        token_command = args.token_command
        token_env = args.token_env
    try:
        token = live.obtain_token(_A, __import__("os").environ, subprocess.run)
    except live.ToolError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

    guard = live.RequestGuard(args.api_host, args.project, {args.dataset: args.objects})
    client = live.Client(guard, live.UrllibTransport(token, 60))

    captured_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sha, dirty = git_sha()

    captured, problems = {}, []
    for name in args.objects:
        url = object_url(args.api_host, args.project, args.dataset, name)
        try:
            data = client.get_json(url)
        except (live.FetchError, live.ToolError) as e:
            problems.append(f"{name}: чтение не удалось ({e})")
            continue
        if data.get("type") != "VIEW" or "view" not in data:
            problems.append(f"{name}: это не VIEW (type={data.get('type')})")
            continue
        if data["view"].get("useLegacySql"):
            problems.append(f"{name}: legacy SQL — канонический слой такие объекты не описывает")
            continue
        body = data["view"].get("query") or ""
        description = data.get("description")
        if not description:
            problems.append(f"{name}: у объекта нет описания; контракт требует OPTIONS(description)")
            continue
        try:
            schema = live.normalize_schema(data.get("schema") or {})
        except live.FetchError as e:
            problems.append(f"{name}: схема не приведена к контракту ({e.code})")
            continue
        captured[name] = {"body": body, "description": description, "schema": schema}

    if problems:
        for pr in problems:
            print(f"ПРОПУЩЕНО {pr}", file=sys.stderr)

    # Зависимости и уровни — из самих канонических тел, как требует sql/current/README.md.
    files, facts = {}, {}
    for name, cap in captured.items():
        text = render_file(args.project, args.dataset, name, cap["description"], cap["body"],
                           [], captured_at, sha)
        findings: list = []
        f = contract.analyze_sql(text, f"capture:{name}", findings)
        if findings:
            problems.append(f"{name}: канонический файл не проходит разбор: {findings[0]}")
            continue
        # SqlFacts.references — множество кортежей (project, dataset, name).
        refs = set(f.references or ())
        internal = sorted(n for _, ds, n in refs if ds == args.dataset)
        external = sorted(f"{ds}.{n}" for _, ds, n in refs if ds != args.dataset)
        bad = sorted({r.split(".")[0] for r in external} - allowed)
        if bad:
            problems.append(f"{name}: читает датасеты вне политики {bad} — не канонизируется")
            continue
        files[name] = render_file(args.project, args.dataset, name, cap["description"],
                                  cap["body"], internal, captured_at, sha)
        facts[name] = {"internal": internal, "external": external}

    if not files:
        print("ERROR: не снят ни один объект", file=sys.stderr)
        return 3

    # Уровни в графе: 0 — без внутренних зависимостей, N — после всех своих.
    levels: dict[str, int] = {}

    def level(n: str, seen=()) -> int:
        if n in levels:
            return levels[n]
        if n in seen:
            raise SystemExit(f"ERROR: цикл зависимостей вокруг {n}")
        v = 0
        for d in facts.get(n, {}).get("internal", []):
            if d in facts:
                v = max(v, 1 + level(d, seen + (n,)))
        levels[n] = v
        return v

    objects = []
    for name in sorted(files):
        text = files[name]
        body_sha = contract.sha256_text(contract.view_body(text, contract.tokenize(text)))
        live_sha = live.live_body_sha256(args.project, args.dataset, name, captured[name]["body"])
        if body_sha != live_sha:
            # Файл и production разошлись уже на снятии — писать такой канон нельзя.
            problems.append(f"{name}: хеш файла не равен хешу живого тела; файл не записан")
            del files[name]
            continue
        schema = captured[name]["schema"]
        objects.append({
            "dataset": args.dataset, "object_name": name, "object_type": "VIEW",
            "canonical_path": f"sql/current/{args.dataset}/{name}.sql",
            "sync_state": "captured_live",
            "dependencies": facts[name]["internal"],
            "external_dependencies": facts[name]["external"],
            "dependency_level": level(name),
            "capture_main_sha": sha,
            "captured_at": captured_at,
            "provenance": {
                "source": "production tables.get (read-only capture)",
                "historical_migration": None,
                "preflight_parity": "EXACT_TEXT",
                "superseded_historical_definitions": [],
            },
            "live_body_sha256_at_capture": live_sha,
            "live_schema_sha256_at_capture": contract.sha256_text(
                json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            "live_description_sha256_at_capture": contract.sha256_text(captured[name]["description"]),
            "live_schema_at_capture": schema,
            "canonical_body_sha256": body_sha,
            "canonical_description_sha256": contract.sha256_text(captured[name]["description"]),
            "canonical_schema_sha256": contract.sha256_text(
                json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            "canonical_schema": schema,
            "canonical_schema_verification": "bigquery_verified",
            "canonical_column_count": len(schema),
        })

    manifest = {
        "manifest_version": 2,
        "dataset": args.dataset,
        "project": args.project,
        "hash_contract": "canonical_hash_v1",
        "allowed_external_datasets": sorted(allowed),
        "rebuild_order": [o["object_name"] for o in
                          sorted(objects, key=lambda x: (x["dependency_level"], x["object_name"]))],
        "objects": objects,
    }

    print(f"снято объектов: {len(objects)} из {len(args.objects)}; "
          f"коммит {sha[:12]}{' (рабочее дерево грязное)' if dirty else ''}")
    for pr in problems:
        print(f"ПРОПУЩЕНО {pr}", file=sys.stderr)
    if args.dry_run:
        print("--dry-run: файлы не записаны")
        return 0

    ds_dir = REPO / "sql" / "current" / args.dataset
    ds_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (ds_dir / f"{name}.sql").write_text(text, encoding="utf-8")
    (ds_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"записано: {ds_dir.relative_to(REPO)}/ ({len(files)} файлов + MANIFEST.json)")
    print("дальше: python tools/validate_current_sql.py && "
          "python tools/verify_current_sql_live.py --project ... (ожидается MATCH)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
