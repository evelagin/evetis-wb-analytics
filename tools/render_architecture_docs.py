#!/usr/bin/env python3
"""Сгенерировать SYSTEM_INVENTORY.md и DATA_LINEAGE.md из system_inventory.json.

Документация, которую пишут руками, устаревает молча. Эти два файла — производные
от снимка production: их не правят, их пересобирают. Источник — только JSON,
BigQuery здесь не вызывается.

  python tools/architecture_baseline.py --project ... --with-gcloud   # снять снимок
  python tools/render_architecture_docs.py                            # пересобрать .md
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ARCH = REPO / "docs" / "architecture"
BANNER = ("<!-- СГЕНЕРИРОВАНО tools/render_architecture_docs.py из "
          "docs/architecture/system_inventory.json. Правки в этом файле будут затёрты: "
          "меняй генератор или снимок. -->")


def human_bytes(n) -> str:
    if not n:
        return "—"
    n = int(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def render_inventory(inv: dict) -> str:
    s, L = inv["summary"], []
    L += [f"# Инвентарь платформы\n", BANNER, "",
          f"**Снимок:** {inv['generated_at']} · **Проект:** `{inv['project']}` · "
          f"**Регион:** {inv['location']} · собран только чтением "
          f"({s['bigquery_queries_issued']} запроса к BigQuery).\n"]
    L += ["## Итого\n", "| Величина | Значение |", "|---|---|"]
    for k, label in [
        ("datasets", "Датасетов"), ("objects_total", "Объектов BigQuery"),
        ("base_tables", "— таблиц"), ("views", "— вью"),
        ("routines", "Процедур и функций"), ("dependency_edges", "Рёбер зависимостей (из тел вью)"),
        ("views_under_canonical_sql", "Вью с каноническим Git-определением (`sql/current`)"),
        ("views_without_canonical_sql", "Вью **без** канонического определения"),
        ("terraform_resources", "Ресурсов Terraform"),
        ("apps_script_files", "Файлов Apps Script (production, вне CI)"),
    ]:
        L.append(f"| {label} | {s[k]} |")
    L.append("")

    L += ["## Датасеты\n", "| Датасет | Домен | Таблиц | Вью |", "|---|---|---:|---:|"]
    for ds, d in inv["datasets"].items():
        L.append(f"| `{ds}` | {d['domain']} | {d.get('BASE TABLE', 0)} | {d.get('VIEW', 0)} |")
    L.append("")

    L += ["## Таблицы-носители данных\n",
          "Только базовые таблицы; вью данных не хранят. `изменена` — момент последней записи "
          "в хранилище, он же простейший индикатор свежести.\n"]
    tables = [o for o in inv["objects"].values() if o["type"] == "BASE TABLE"]
    for domain in ("wb", "ozon", "shared"):
        rows = sorted((o for o in tables if o["domain"] == domain), key=lambda x: x["object"])
        if not rows:
            continue
        L += [f"### Домен `{domain}`\n", "| Объект | Слой | Строк | Размер | Изменена |",
              "|---|---|---:|---:|---|"]
        for o in rows:
            L.append(f"| `{o['object']}` | {o['layer']} | "
                     f"{o['row_count'] if o['row_count'] is not None else '—'} | "
                     f"{human_bytes(o['size_bytes'])} | {o['last_modified'] or '—'} |")
        L.append("")

    L += ["## Оркестрация\n"]
    orch = inv.get("orchestration") or {}
    if not orch:
        L.append(f"Не собрана: {inv.get('orchestration_error') or 'запуск без --with-gcloud'}.\n")
    else:
        L += ["### Cloud Scheduler\n",
              "| Задание | Состояние | Расписание | Зона | Цель |", "|---|---|---|---|---|"]
        for j in orch["cloud_scheduler"]:
            tgt = j["target_kind"]
            if j.get("bigquery_statement"):
                tgt += f" · `{j['bigquery_statement'].strip()[:70]}`"
            L.append(f"| `{j['scheduler']}` | {j['state']} | `{j['schedule']}` | {j['timezone']} | {tgt} |")
        L += ["", "### Cloud Run Jobs\n",
              "| Job | Аргументы | Образ закреплён по digest |", "|---|---|---|"]
        for j in orch["cloud_run_jobs"]:
            L.append(f"| `{j['job']}` | `{' '.join(j['args']) or '—'}` | "
                     f"{'да' if j['image_digest_pinned'] else '**НЕТ**'} |")
        L += ["", "### Cloud Run Services\n", "| Сервис | Регион | Ingress |", "|---|---|---|"]
        for svc in orch["cloud_run_services"]:
            L.append(f"| `{svc['service']}` | {svc['region']} | {svc['ingress']} |")
        L.append("")

    L += ["## Процедуры BigQuery\n", "| Процедура | Тип | Изменена | Читает/пишет |", "|---|---|---|---|"]
    for r in inv["routines"]:
        refs = ", ".join(f"`{x}`" for x in r["references"][:6]) or "—"
        if len(r["references"]) > 6:
            refs += f" … (+{len(r['references']) - 6})"
        L.append(f"| `{r['routine']}` | {r['type']} | {r['last_altered'] or '—'} | {refs} |")
    L.append("")

    L += ["## Вью без канонического Git-определения\n",
          "Для этих объектов авторитетным определением остаётся production, а не репозиторий: "
          "изменение в BigQuery мимо Git не будет замечено. Покрытие расширяется по "
          "`sql/current/README.md`.\n"]
    by_ds = defaultdict(list)
    for o in inv["views_without_canonical_sql"]:
        by_ds[o.split(".")[0]].append(o)
    for ds in sorted(by_ds):
        L.append(f"- **`{ds}`** ({len(by_ds[ds])}): " + ", ".join(f"`{x.split('.')[1]}`" for x in sorted(by_ds[ds])))
    L.append("")
    return "\n".join(L)


def render_lineage(inv: dict) -> str:
    objects = inv["objects"]
    L = ["# Происхождение данных (lineage)\n", BANNER, "",
         f"**Снимок:** {inv['generated_at']}. Рёбра выведены из тел вью в "
         "`INFORMATION_SCHEMA.VIEWS`, а не из имён файлов: это то, что BigQuery исполняет.\n",
         "Обозначения: `T` — таблица (носитель данных), `V` — вью (вычисление поверх других "
         "объектов). Стрелка читается «зависит от».\n"]

    # Источники: таблицы, которые никто не собирает вью, но которые читают.
    L += ["## Источники и потребление\n",
          "Наиболее читаемые объекты платформы — те, чья поломка распространяется дальше всего.\n",
          "| Объект | Тип | Прямых потребителей | Транзитивно |", "|---|---|---:|---:|"]

    def transitive(start: str) -> set[str]:
        seen, stack = set(), [start]
        while stack:
            cur = stack.pop()
            for c in objects.get(cur, {}).get("consumed_by", []):
                if c not in seen:
                    seen.add(c)
                    stack.append(c)
        return seen

    ranked = sorted(objects.values(), key=lambda o: -len(o["consumed_by"]))[:25]
    for o in ranked:
        if not o["consumed_by"]:
            continue
        L.append(f"| `{o['object']}` | {'V' if o['type'] == 'VIEW' else 'T'} | "
                 f"{len(o['consumed_by'])} | {len(transitive(o['object']))} |")
    L.append("")

    # Глубина цепочек.
    depth_cache: dict[str, int] = {}

    def depth(n: str, path=()) -> int:
        if n in path:
            return 0  # цикл: не углубляемся, факт цикла виден отдельной проверкой
        if n in depth_cache:
            return depth_cache[n]
        d = 0
        for dep in objects.get(n, {}).get("depends_on", []):
            if dep in objects:
                d = max(d, 1 + depth(dep, path + (n,)))
        depth_cache[n] = d
        return d

    L += ["## Самые длинные цепочки вычисления\n",
          "Глубина — число уровней вью между объектом и ближайшей таблицей. Длинная цепочка "
          "означает, что причина неверного числа на дашборде может лежать на много уровней ниже.\n",
          "| Глубина | Объект |", "|---:|---|"]
    for o in sorted((o for o in objects.values() if o["type"] == "VIEW"),
                    key=lambda x: (-depth(x["object"]), x["object"]))[:15]:
        L.append(f"| {depth(o['object'])} | `{o['object']}` |")
    L.append("")

    # Путь от площадки до потребителя.
    L += ["## Цепочки по доменам\n"]
    for domain, title in (("wb", "Wildberries"), ("ozon", "Ozon"), ("shared", "Общие и нейтральные")):
        L.append(f"### {title}\n")
        leaves = sorted(
            o["object"] for o in objects.values()
            if o["domain"] == domain and o["type"] == "VIEW" and not o["consumed_by"]
        )
        L.append(f"Конечных объектов (никто не читает изнутри BigQuery — это выход к Metabase, "
                 f"листам или агентам, либо мёртвый объект): **{len(leaves)}**.\n")
        for leaf in leaves:
            deps = objects[leaf]["depends_on"]
            L.append(f"- `{leaf}` ← " + (", ".join(f"`{d}`" for d in deps) if deps else "_нет зависимостей_"))
        L.append("")

    L += ["## Полный граф\n",
          "Машиночитаемая форма — `system_inventory.json`, поля `depends_on` и `consumed_by` "
          "у каждого объекта. Здесь — только разрезы, которые читает человек.\n"]
    return "\n".join(L)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--inventory", default=str(ARCH / "system_inventory.json"))
    p.add_argument("--out-dir", default=str(ARCH))
    args = p.parse_args(argv)

    inv = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    out = Path(args.out_dir)
    for name, text in (("SYSTEM_INVENTORY.md", render_inventory(inv)),
                       ("DATA_LINEAGE.md", render_lineage(inv))):
        (out / name).write_text(text.rstrip() + "\n", encoding="utf-8")
        print(f"{out / name}: {len(text.splitlines())} строк")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
