#!/usr/bin/env python3
"""Пакет SQL выделенного арендатора Ozon (Tenancy T4-b): шаблоны → рендер → манифест.

  python tools/tenancy/sql_package.py validate                    # шаблоны пакета (офлайн)
  python tools/tenancy/sql_package.py render <tenant_id> <out_dir>
  python tools/tenancy/sql_package.py dryrun <tenant_id>          # живой dry-run BigQuery (0 байт)

Шаблоны живут в sql/tenant/ozon/<dataset_key>/<OBJECT>.sql и ссылаются на объекты ТОЛЬКО как
`__tenant__.<dataset_key>.<OBJECT>` (один шаблонный проект, ключи датасетов контракта). Проект и
имена датасетов подставляет рендер из контракта реестра (registry.terraform_inputs) — вызывающий
их не передаёт. Один и тот же пакет рендерится для любого арендатора; ветвлений по tenant_id нет.

Проверки (любое нарушение — отказ, запрет по умолчанию):
  P1. Файл = ровно один CREATE OR REPLACE VIEW с OPTIONS(description); без DML/скриптов, без
      `SELECT *` в выходе (контракт колонок явный) — разбор общий с validate_current_sql.
  P2. Имя представления = путь файла; объект объявлен в PACKAGE.json, лишних файлов нет; объект
      пакета не совпадает по имени с таблицей контракта (представление не подменяет таблицу).
  P3. Каждая ссылка — `__tenant__.<ключ>.<объект>`; другого проекта нет; ключ — датасет контракта.
  P4. Политика чтения слоёв: ozon_mart ← {ozon_raw, ref, ozon_mart}; tenant_ops ← {+tenant_ops};
      analytics_share ← {ozon_mart, tenant_ops, analytics_share} — клиентский слой не читает RAW.
  P5. Ссылка указывает на объект пакета или таблицу контракта; колонки существуют (sqlglot qualify
      по схемам контракта и выходам ранее проверенных представлений).
  P6. Граф ацикличен; порядок пересборки — топологический.
  P7. Семантический класс объекта: MARKETPLACE_FACT | GENERIC_DERIVED | TENANT_CONFIG.
      Эмпирические допущения EVETIS в пакет не допускаются.
  P8. Запрещённые литералы: идентификаторы EVETIS и платформы, tenant_id, проекты арендаторов,
      артикулы EVT-*, номера отправлений, ИНН/ОГРН-подобные строки, календарные даты 19xx/20xx
      (бизнес-даты — только через конфигурацию арендатора), известные калибровки EVETIS.
      Дополнительно по дереву разбора (любые кавычки, любой регистр): числовой литерал — только
      целое до трёх знаков (идентификаторы типов, версии, 0/1); строка-число — так же; строка-
      дата — только граница открытого интервала '9999-12-31'. Калибровки и даты — конфигурация.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402
from sqlglot.errors import OptimizeError, SqlglotError  # noqa: E402
from sqlglot.optimizer.qualify import qualify  # noqa: E402

from tools import validate_current_sql as VC  # noqa: E402
from tools.tenancy import platform as PL  # noqa: E402

PACKAGE_DIR = REPO / "sql" / "tenant" / "ozon"
MANIFEST_FILE = "PACKAGE.json"
TEMPLATE_PROJECT = "__tenant__"
DIALECT = VC.DIALECT

PACKAGE_DATASETS = ("ozon_mart", "tenant_ops", "analytics_share")
READ_POLICY = {
    "ozon_mart": frozenset({"ozon_raw", "ref", "ozon_mart"}),
    "tenant_ops": frozenset({"ozon_raw", "ref", "ozon_mart", "tenant_ops"}),
    "analytics_share": frozenset({"ozon_mart", "tenant_ops", "analytics_share"}),
}
SEMANTIC_CLASSES = frozenset({"MARKETPLACE_FACT", "GENERIC_DERIVED", "TENANT_CONFIG"})

TEMPLATE_REF_RE = re.compile(r"`__tenant__\.([a-z_]+)\.([A-Za-z_][A-Za-z0-9_]*)`")
ANY_TEMPLATE_TOKEN_RE = re.compile(r"__tenant__")

BANNED: tuple[tuple[str, re.Pattern], ...] = tuple(
    [(f"идентификатор EVETIS {m!r}", re.compile(re.escape(m), re.I)) for m in PL.EVETIS_FORBIDDEN_MARKERS]
    + [
        ("упоминание EVETIS", re.compile(r"evetis", re.I)),
        ("идентификатор платформы", re.compile(re.escape(PL.PLATFORM_PROJECT_ID) + "|" + PL.PLATFORM_PROJECT_NUMBER)),
        ("tenant_id в коде пакета", re.compile(r"\bclient_\d+", re.I)),
        ("проект арендатора в коде пакета", re.compile(r"mpa-t\d?-", re.I)),
        ("артикул EVETIS", re.compile(r"\bEVT-", re.I)),
        ("номер отправления", re.compile(r"'\d{5,}-\d{3,}-\d+'")),
        ("ИНН/ОГРН-подобная строка", re.compile(r"'(?:\d{10}|\d{12}|\d{13}|\d{15})'")),
        ("календарная дата в коде (бизнес-даты — только конфигурация)", re.compile(r"'(?:19|20)\d\d-\d\d-\d\d")),
        ("выкуп СНГ EVETIS", re.compile(r"CIS_BUYOUT", re.I)),
        ("калибровка EVETIS 0.558", re.compile(r"\b0\.558\b")),
        ("калибровка EVETIS 0.18/0.52", re.compile(r"NUMERIC\s*'0\.(?:18|52)'")),
    ]
)


class PackageError(ValueError):
    """Пакет или рендер невалиден."""


@dataclass
class PackageObject:
    dataset_key: str
    name: str
    path: Path
    sql: str
    semantic_class: str
    description: str = ""
    refs: set = field(default_factory=set)          # {(dataset_key, name)}
    output_columns: list | None = None
    parsed_ok: bool = True                          # P1–P3 без нарушений: можно проверять колонки


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO))
    except ValueError:
        return str(p)


def load_manifest(root: Path) -> dict:
    from tools.tenancy.validation import parse_tenant_json   # единый строгий разборщик JSON
    p = root / MANIFEST_FILE
    if not p.is_file():
        raise PackageError(f"нет {MANIFEST_FILE} в {_rel(root)}")
    doc = parse_tenant_json(p.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("objects"), list) or not isinstance(doc.get("version"), int):
        raise PackageError(f"{MANIFEST_FILE}: ожидаются version (int) и objects (list)")
    return doc


def contract_schema(contract: dict) -> dict[tuple[str, str], dict[str, str]]:
    """{(ключ датасета, таблица): {колонка: тип}} — таблицы контракта (ozon_raw, ref, tenant_ops)."""
    from tools.tenancy.validation import parse_tenant_json   # единый строгий разборщик JSON
    out = {}
    for t in contract["tables"]:
        cols = {f["name"]: f["type"] for f in parse_tenant_json(t["schema_json"])}
        out[(t["dataset_key"], t["table_id"])] = cols
    return out


def load_package(root: Path = PACKAGE_DIR, contract: dict | None = None) -> tuple[list[PackageObject], list[str]]:
    """Объекты пакета в порядке пересборки и нарушения. contract — для P5 (таблицы и колонки)."""
    findings: list[str] = []
    manifest = load_manifest(root)
    declared = {}
    for i, o in enumerate(manifest["objects"]):
        key = (o.get("dataset"), o.get("name"))
        if key in declared:
            findings.append(f"{MANIFEST_FILE}: объект {key} объявлен дважды")
        if o.get("dataset") not in PACKAGE_DATASETS:
            findings.append(f"{MANIFEST_FILE}[{i}]: датасет {o.get('dataset')!r} не входит в пакет {PACKAGE_DATASETS}")
        if o.get("semantic_class") not in SEMANTIC_CLASSES:
            findings.append(f"{MANIFEST_FILE}[{i}] {key}: семантический класс {o.get('semantic_class')!r} "
                            f"не допускается (P7: {sorted(SEMANTIC_CLASSES)})")
        declared[key] = o
    files = {(p.parent.name, p.stem): p for p in root.glob("*/*.sql")}
    for key in sorted(set(files) - set(declared)):
        findings.append(f"{_rel(files[key])}: файл не объявлен в {MANIFEST_FILE} (P2)")
    for key in sorted(set(declared) - set(files)):
        findings.append(f"{MANIFEST_FILE}: {key} объявлен, а файла нет (P2)")

    objects: dict[tuple[str, str], PackageObject] = {}
    for key in sorted(set(declared) & set(files)):
        ds, name = key
        path = files[key]
        sql = path.read_text(encoding="utf-8")
        subject = _rel(path)
        for reason, rx in BANNED:
            if rx.search(sql):
                findings.append(f"{subject}: запрещённый литерал — {reason} (P8)")
        findings += _literal_findings(sql, subject)
        before = len(findings)
        vf: list = []
        facts = VC.analyze_sql(sql, subject, vf)
        findings += [f"{f.subject}: {f.check} {f.reason} (P1/P3)" for f in vf]
        if facts.target != (TEMPLATE_PROJECT, ds, name):
            findings.append(f"{subject}: цель {facts.target} != ({TEMPLATE_PROJECT!r}, {ds!r}, {name!r}) (P2)")
        if facts.output_columns is None:
            findings.append(f"{subject}: выход представления не выводится (SELECT * запрещён, P1)")
        obj = PackageObject(ds, name, path, sql, declared[key].get("semantic_class", ""),
                            facts.description or "", set(), facts.output_columns)
        for (proj, rds, rname) in sorted(facts.references or ()):
            if proj != TEMPLATE_PROJECT:
                findings.append(f"{subject}: ссылка на проект {proj!r}, допустим только шаблонный "
                                f"{TEMPLATE_PROJECT!r} (P3)")
                continue
            if ds in READ_POLICY and rds not in READ_POLICY[ds]:
                findings.append(f"{subject}: слой {ds} не может читать {rds}.{rname} (P4: {sorted(READ_POLICY[ds])})")
            obj.refs.add((rds, rname))
        # каждое вхождение шаблонного проекта — в канонической форме `__tenant__.<ключ>.<объект>`
        if len(ANY_TEMPLATE_TOKEN_RE.findall(sql)) != len(TEMPLATE_REF_RE.findall(sql)):
            findings.append(f"{subject}: шаблонный проект встречается не в форме `__tenant__.<ключ>.<объект>` (P3)")
        obj.parsed_ok = len(findings) == before
        objects[key] = obj

    order = _topological(objects, findings)
    if contract is not None:
        _check_references_and_columns(order, contract, findings)
    return order, findings


SMALL_INT_RE = re.compile(r"[0-9]{1,3}")
NUMBER_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
DATE_LIKE_RE = re.compile(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}")
OPEN_INTERVAL_END = "9999-12-31"


def _literal_findings(sql: str, subject: str) -> list[str]:
    """P8 по дереву разбора: литерал, который мог бы нести калибровку, бизнес-дату или
    идентификатор, независимо от кавычек и регистра."""
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except SqlglotError:
        return []                      # синтаксис разбирает P1
    out = []
    for lit in tree.find_all(exp.Literal):
        v = str(lit.this).strip()
        if not lit.is_string:
            if not SMALL_INT_RE.fullmatch(v):
                out.append(f"{subject}: числовой литерал {v!r} — допустимы только целые до трёх знаков (P8)")
        elif NUMBER_RE.fullmatch(v) and not SMALL_INT_RE.fullmatch(v):
            out.append(f"{subject}: число в строке {v!r} (P8)")
        elif DATE_LIKE_RE.match(v) and v != OPEN_INTERVAL_END:
            out.append(f"{subject}: дата в строке {v!r} — бизнес-даты только из конфигурации (P8)")
    return out


def _topological(objects: dict, findings: list) -> list[PackageObject]:
    deps = {k: {r for r in o.refs if r in objects} for k, o in objects.items()}
    done, order, visiting = set(), [], set()

    def visit(k, stack):
        if k in done:
            return
        if k in visiting:
            findings.append(f"цикл зависимостей: {' → '.join(map(str, stack + [k]))} (P6)")
            return
        visiting.add(k)
        for d in sorted(deps[k]):
            visit(d, stack + [k])
        visiting.discard(k)
        done.add(k)
        order.append(objects[k])

    for k in sorted(objects):
        visit(k, [])
    return order


def _check_references_and_columns(order: list[PackageObject], contract: dict, findings: list) -> None:
    tables = contract_schema(contract)
    known: dict[tuple[str, str], dict[str, str]] = dict(tables)
    for o in order:
        subject = _rel(o.path)
        if (o.dataset_key, o.name) in tables:
            findings.append(f"{subject}: объект пакета совпадает по имени с таблицей контракта "
                            f"{o.dataset_key}.{o.name} — представление не подменяет таблицу (P2)")
        missing = [r for r in sorted(o.refs) if r not in known]
        for r in missing:
            findings.append(f"{subject}: ссылка на {r[0]}.{r[1]} — нет ни в пакете выше по порядку, ни в "
                            f"таблицах контракта (P5)")
        if not missing and o.parsed_ok:
            schema: dict = {}
            for (ds, name), cols in known.items():
                schema.setdefault(TEMPLATE_PROJECT, {}).setdefault(ds, {})[name] = cols
            try:
                tree = sqlglot.parse_one(o.sql, read=DIALECT)
                qualify(tree.expression.copy(), schema=schema, dialect=DIALECT,
                        validate_qualify_columns=True, quote_identifiers=False)
            except (OptimizeError, SqlglotError) as e:
                findings.append(f"{subject}: колонки не сходятся со схемой контракта: {VC._first_line(e)} (P5)")
        known[(o.dataset_key, o.name)] = {c: "STRING" for c in (o.output_columns or [])}


def template_sha256(root: Path = PACKAGE_DIR) -> str:
    h = hashlib.sha256()
    for p in sorted([root / MANIFEST_FILE, *root.glob("*/*.sql")]):
        h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def render_sql(sql: str, contract: dict) -> str:
    project, datasets = contract["project_id"], contract["datasets"]

    def sub(m):
        key = m.group(1)
        if key not in datasets:
            raise PackageError(f"ключ датасета {key!r} отсутствует в контракте")
        return f"`{project}.{datasets[key]}.{m.group(2)}`"
    out = TEMPLATE_REF_RE.sub(sub, sql)
    if TEMPLATE_PROJECT in out:
        raise PackageError("после рендера остался шаблонный проект")
    return out


def render(tenant_id: str, out_dir: Path, root: Path = PACKAGE_DIR) -> dict:
    """Рендер пакета для арендатора из реестра. Возвращает манифест рендера."""
    from tools.tenancy import registry as R

    contract = R.terraform_inputs(tenant_id)          # единственный источник проекта и датасетов
    order, findings = load_package(root, contract)
    if findings:
        raise PackageError("пакет невалиден:\n  " + "\n  ".join(findings))
    project = contract["project_id"]
    rendered_objects = []
    for i, o in enumerate(order):
        sql = render_sql(o.sql, contract)
        for m in PL.EVETIS_FORBIDDEN_MARKERS + PL.PLATFORM_MARKERS:
            if m in sql:
                raise PackageError(f"{o.name}: после рендера найден маркер {m!r}")
        projects = {m.group(1) for m in re.finditer(r"`([a-z][a-z0-9-]{4,28}[a-z0-9])\.[A-Za-z_]", sql)}
        if projects != {project}:
            raise PackageError(f"{o.name}: после рендера проекты {projects} вместо {{{project!r}}}")
        dst = out_dir / contract["datasets"][o.dataset_key] / f"{o.name}.sql"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(sql, encoding="utf-8")
        rendered_objects.append({
            "order": i, "dataset_key": o.dataset_key, "dataset": contract["datasets"][o.dataset_key],
            "name": o.name, "semantic_class": o.semantic_class,
            "depends_on": sorted(f"{d}.{n}" for d, n in o.refs),
            "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
        })
    manifest = {
        "render_contract": "vts.tenant-sql-render.v1",
        "tenant_id": contract["tenant_id"], "project_id": project,
        "package_version": load_manifest(root)["version"],
        "template_sha256": template_sha256(root),
        "objects": rendered_objects,
    }
    (out_dir / "RENDER_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


_BQ_TYPES = {"STRING": "STRING", "INTEGER": "INT64", "INT64": "INT64", "NUMERIC": "NUMERIC", "BIGNUMERIC": "BIGNUMERIC",
             "BOOL": "BOOL", "BOOLEAN": "BOOL", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "DATETIME": "DATETIME",
             "FLOAT": "FLOAT64", "FLOAT64": "FLOAT64", "JSON": "JSON"}


def compile_queries(contract: dict, live_tables: set[tuple[str, str]],
                    root: Path = PACKAGE_DIR) -> list[tuple[str, str]]:
    """[(dataset.object, SELECT)] для dry-run BigQuery: зависимости пакета — CTE, таблицы контракта,
    которых ещё нет вживую, — пустые типизированные CTE. Ничего не создаёт и не читает данных."""
    order, findings = load_package(root, contract)
    if findings:
        raise PackageError("пакет невалиден:\n  " + "\n  ".join(findings))
    by_key = {(o.dataset_key, o.name): o for o in order}
    bodies = {k: sqlglot.parse_one(o.sql, read=DIALECT).expression.sql(DIALECT) for k, o in by_key.items()}
    schema = contract_schema(contract)
    project = contract["project_id"]
    alias = lambda ds, n: f"__{ds}__{n}"  # noqa: E731
    out = []
    for o in order:
        need, seen = [], set()

        def walk(k):
            for r in sorted(by_key[k].refs):
                if r in bodies and r not in seen:
                    seen.add(r)
                    walk(r)
                    need.append(r)
        walk((o.dataset_key, o.name))
        stubs = {rr for k in [*need, (o.dataset_key, o.name)] for rr in by_key[k].refs
                 if rr not in bodies and rr not in live_tables}
        ctes = [f"{alias(*r)} AS (SELECT " + ", ".join(f"CAST(NULL AS {_BQ_TYPES[t]}) AS {c}"
                                                      for c, t in schema[r].items()) + " LIMIT 0)"
                for r in sorted(stubs)]
        ctes += [f"{alias(*r)} AS ({bodies[r]})" for r in need]

        def resolve(m):
            ds, n = m.group(1), m.group(2)
            if (ds, n) in bodies or (ds, n) in stubs:
                return alias(ds, n)
            return f"`{project}.{contract['datasets'][ds]}.{n}`"
        q = (("WITH " + ",\n".join(ctes) + "\n") if ctes else "") + f"SELECT * FROM ({bodies[(o.dataset_key, o.name)]})"
        q = re.sub(r"`__tenant__`\.`([a-z_]+)`\.`([A-Za-z0-9_]+)`", resolve, q)
        q = TEMPLATE_REF_RE.sub(resolve, q)
        if TEMPLATE_PROJECT in q:
            raise PackageError(f"{o.name}: в запросе dry-run остался шаблонный проект")
        out.append((f"{o.dataset_key}.{o.name}", q))
    return out


def dryrun(tenant_id: str) -> int:
    """Dry-run BigQuery каждого объекта пакета в проекте арендатора (только компиляция, 0 байт).
    Живая операция чтения метаданных: учётные данные — gcloud оператора. В CI не выполняется."""
    import subprocess
    import urllib.request
    from tools.tenancy import registry as R
    from tools.tenancy.validation import parse_tenant_json   # единый строгий разборщик JSON

    def body_of(resp) -> dict:
        return parse_tenant_json(resp.read().decode("utf-8"))

    contract = R.terraform_inputs(tenant_id)
    project = contract["project_id"]
    tok = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True,
                         check=True).stdout.strip()
    headers = {"Authorization": f"Bearer {tok}", "x-goog-user-project": project, "Content-Type": "application/json"}
    live = set()
    for ds_key, ds in contract["datasets"].items():
        url = f"https://www.googleapis.com/bigquery/v2/projects/{project}/datasets/{ds}/tables?maxResults=1000"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                live |= {(ds_key, t["tableReference"]["tableId"]) for t in body_of(r).get("tables", [])}
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    failures = 0
    for name, q in compile_queries(contract, live):
        body = json.dumps({"configuration": {"dryRun": True, "query": {"query": q, "useLegacySql": False}}}).encode()
        req = urllib.request.Request(f"https://www.googleapis.com/bigquery/v2/projects/{project}/jobs",
                                     data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                print(f"OK    {name}  bytes={body_of(r)['statistics'].get('totalBytesProcessed')}")
        except urllib.error.HTTPError as e:
            failures += 1
            print(f"FAIL  {name}: {body_of(e).get('error', {}).get('message', '')[:300]}")
    print(f"dry-run {project}: неудач {failures}")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["dryrun"] and len(argv) == 2:
        return dryrun(argv[1])
    if argv[:1] == ["validate"] and len(argv) == 1:
        from tools.tenancy import synthetic as SY
        _order, findings = load_package(PACKAGE_DIR, SY.fixture_contract("client_001"))
        for f in findings:
            print(f"FAIL {f}")
        print(f"пакет {_rel(PACKAGE_DIR)}: объектов {len(_order)}, нарушений {len(findings)}")
        return 1 if findings else 0
    if argv[:1] == ["render"] and len(argv) == 3:
        try:
            m = render(argv[1], Path(argv[2]))
        except (PackageError, ValueError) as e:
            print(f"FAIL {e}", file=sys.stderr)
            return 1
        print(f"отрендерено {len(m['objects'])} объектов для {m['tenant_id']} ({m['project_id']})")
        return 0
    print(__doc__, file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
