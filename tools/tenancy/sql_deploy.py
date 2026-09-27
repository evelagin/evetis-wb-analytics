"""Tenancy T4 — развёртывание пакета SQL арендатора через BigQuery Tables API (без query jobs).

Идентичность — sa-tenant-provisioner через WIF (tenant-infra.yml): у него есть
bigquery.tables.create/update/get/list и НЕТ bigquery.jobs.create, поэтому DDL не исполняется;
представление пишется ресурсом таблицы с блоком view.

  verify-package <tenant_id> <rendered_dir>   — только локально: хеши и форма объектов
  plan-live      <tenant_id> <rendered_dir>   — только чтение: что будет создано/обновлено
  deploy         <tenant_id> <rendered_dir>   — tables.insert / tables.update, чтение назад
  verify-live    <tenant_id> <rendered_dir>   — только чтение: живые представления = пакет

Ожидаемые хеши — во входах EXPECTED_MANIFEST_SHA256 и EXPECTED_PACKAGE_SHA256; отрендеренный
пакет должен совпасть с ними до любого обращения к BigQuery. Порядок — только манифест рендера.
Создаются и обновляются только объекты пакета и только как VIEW; таблицы, материализованные и
внешние объекты, ACL датасетов, authorized views, IAM и query jobs не затрагиваются. Любое
расхождение — SqlDeployError, выход 1, без попыток «починить».
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.tenancy import sql_package as SP  # noqa: E402

MANIFEST = "RENDER_MANIFEST.json"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
VIEW_RE = re.compile(
    r"\ACREATE OR REPLACE VIEW `(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])\.(?P<dataset>[a-z_]+)\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)`\n"
    r"OPTIONS\(description = '(?P<description>(?:[^'\\\n]|\\.)*)'\)\n"
    r"AS\n(?P<query>.+?);\s*\Z", re.S)
BQ = "https://www.googleapis.com/bigquery/v2"      # тот же API; хост bigquery.* блокируется частью сетей


class SqlDeployError(RuntimeError):
    """Проверка пакета или живого состояния не пройдена."""


def _fail(msg: str):
    raise SqlDeployError(msg)


def _json(text):
    from tools.tenancy.validation import parse_tenant_json   # единый строгий разборщик JSON
    return parse_tenant_json(text.decode("utf-8") if isinstance(text, bytes) else text)


def rendered_package_sha256(root: Path) -> str:
    """sha256 по отсортированным .sql: относительный путь, \\0, байты, \\0."""
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*.sql")):
        h.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def canonical_query(q: str) -> str:
    """Сравнение определения: переводы строк, хвостовые пробелы и завершающая `;` не значимы."""
    lines = [ln.rstrip() for ln in q.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip().rstrip(";").strip()


@dataclass(frozen=True)
class ViewSpec:
    order: int
    dataset: str
    name: str
    description: str
    query: str

    def resource(self, project: str) -> dict:
        return {"tableReference": {"projectId": project, "datasetId": self.dataset, "tableId": self.name},
                "description": self.description,
                "view": {"query": self.query, "useLegacySql": False}}


def _strip_header_comments(sql: str) -> str:
    lines = sql.split("\n")
    while lines and lines[0].startswith("--"):
        lines.pop(0)
    return "\n".join(lines)


def load_package(tenant_id: str, root: Path, contract: dict, env: dict) -> list[ViewSpec]:
    expected_manifest = env.get("EXPECTED_MANIFEST_SHA256", "")
    expected_package = env.get("EXPECTED_PACKAGE_SHA256", "")
    if not (SHA256_RE.fullmatch(expected_manifest) and SHA256_RE.fullmatch(expected_package)):
        _fail("нет ожидаемых хешей манифеста и пакета")
    mpath = root / MANIFEST
    if not mpath.is_file():
        _fail("нет манифеста рендера")
    if hashlib.sha256(mpath.read_bytes()).hexdigest() != expected_manifest:
        _fail("хеш манифеста рендера не равен ожидаемому")
    if rendered_package_sha256(root) != expected_package:
        _fail("хеш отрендеренного пакета не равен ожидаемому")
    m = _json(mpath.read_bytes())
    project = contract["project_id"]
    if m.get("tenant_id") != tenant_id or m.get("project_id") != project:
        _fail("манифест рендера для другого арендатора или проекта")
    allowed = {contract["datasets"][k] for k in SP.PACKAGE_DATASETS}
    objs = sorted(m.get("objects") or [], key=lambda o: o.get("order", -1))
    files = {str(p.relative_to(root)) for p in root.rglob("*.sql")}
    declared = {f"{o.get('dataset')}/{o.get('name')}.sql" for o in objs}
    if files != declared:
        _fail(f"файлы пакета не совпадают с манифестом: лишние {sorted(files - declared)}, нет {sorted(declared - files)}")
    if [o.get("order") for o in objs] != list(range(len(objs))):
        _fail("порядок манифеста не сплошной")
    package_objects = {f"{o.get('dataset_key')}.{o.get('name')}" for o in objs}
    seen: set = set()
    specs = []
    for o in objs:
        f = root / o["dataset"] / f"{o['name']}.sql"
        data = f.read_bytes()
        if hashlib.sha256(data).hexdigest() != o.get("sql_sha256"):
            _fail(f"{o['dataset']}.{o['name']}: хеш файла не равен манифесту")
        for dep in o.get("depends_on") or []:
            if dep in package_objects and dep not in seen:
                _fail(f"{o['dataset']}.{o['name']}: зависимость {dep} не раньше по порядку")
        mt = VIEW_RE.match(_strip_header_comments(data.decode("utf-8")))
        if not mt:
            _fail(f"{o['dataset']}.{o['name']}: не единичное CREATE OR REPLACE VIEW канонической формы")
        if (mt["project"], mt["dataset"], mt["name"]) != (project, contract["datasets"][o["dataset_key"]], o["name"]):
            _fail(f"{o['dataset']}.{o['name']}: идентификатор представления не равен манифесту")
        if mt["dataset"] not in allowed:
            _fail(f"{mt['dataset']}: датасет вне пакета")
        query = mt["query"]
        if re.search(r"(?i)\b(CREATE|DROP|ALTER|INSERT|UPDATE|DELETE|MERGE|TRUNCATE|GRANT|REVOKE|EXPORT|CALL|EXECUTE)\b\s", query):
            _fail(f"{o['dataset']}.{o['name']}: в теле представления есть не-SELECT оператор")
        for p in set(re.findall(r"`([a-z][a-z0-9-]+)\.[a-z_]+\.[A-Za-z_0-9]+`", query)):
            if p != project:
                _fail(f"{o['dataset']}.{o['name']}: ссылка на чужой проект {p}")
        desc = mt["description"].replace("\\'", "'").replace("\\\\", "\\")
        specs.append(ViewSpec(o["order"], mt["dataset"], o["name"], desc, query))
        seen.add(f"{o['dataset_key']}.{o['name']}")
    return specs


class BigQueryTables:
    """Только эндпоинты tables.get / tables.list / tables.insert / tables.update."""

    def __init__(self, token: str, project: str):
        self.token, self.project = token, project

    def _call(self, method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
            "x-goog-user-project": self.project})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, _json(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (_json(raw) if raw.strip().startswith(b"{") else {})

    def _t(self, dataset: str, name: str = "") -> str:
        base = f"{BQ}/projects/{self.project}/datasets/{urllib.parse.quote(dataset)}/tables"
        return f"{base}/{urllib.parse.quote(name)}" if name else base

    def get(self, dataset, name):
        return self._call("GET", self._t(dataset, name))

    def list(self, dataset):
        code, body = self._call("GET", self._t(dataset) + "?maxResults=1000")
        if code != 200:
            _fail(f"tables.list {dataset}: HTTP {code}")
        return [t["tableReference"]["tableId"] for t in body.get("tables") or []], \
               {t["tableReference"]["tableId"]: t.get("type") for t in body.get("tables") or []}

    def insert(self, dataset, resource):
        return self._call("POST", self._t(dataset), resource)

    def update(self, dataset, name, resource):
        return self._call("PUT", self._t(dataset, name), resource)


def _matches(live: dict, spec: ViewSpec) -> bool:
    return (live.get("type") == "VIEW"
            and canonical_query((live.get("view") or {}).get("query", "")) == canonical_query(spec.query)
            and (live.get("view") or {}).get("useLegacySql") is False
            and (live.get("description") or "") == spec.description)


def plan_live(specs: list[ViewSpec], bq) -> list[tuple[str, ViewSpec]]:
    ops = []
    for s in specs:
        code, live = bq.get(s.dataset, s.name)
        if code == 404:
            ops.append(("insert", s))
        elif code == 200:
            if live.get("type") != "VIEW":
                _fail(f"{s.dataset}.{s.name}: существует и это не VIEW ({live.get('type')}) — не трогаем")
            ops.append(("noop" if _matches(live, s) else "update", s))
        else:
            _fail(f"tables.get {s.dataset}.{s.name}: HTTP {code}")
    return ops


def deploy(specs: list[ViewSpec], bq, project: str) -> list[tuple[str, str]]:
    done = []
    for op, s in plan_live(specs, bq):
        if op == "insert":
            code, _ = bq.insert(s.dataset, s.resource(project))
        elif op == "update":
            code, _ = bq.update(s.dataset, s.name, s.resource(project))
        else:
            code = 200
        if code not in (200,):
            _fail(f"{op} {s.dataset}.{s.name}: HTTP {code}")
        code, live = bq.get(s.dataset, s.name)
        if code != 200 or not _matches(live, s):
            _fail(f"{s.dataset}.{s.name}: определение после записи не совпало с пакетом")
        done.append((op, f"{s.dataset}.{s.name}"))
        print(f"{op:6} {s.order:2} {s.dataset}.{s.name}")
    return done


def verify_live(specs: list[ViewSpec], bq) -> None:
    """Живые датасеты пакета содержат ровно объекты пакета, все — VIEW с тем же определением."""
    by_ds: dict[str, set] = {}
    for s in specs:
        by_ds.setdefault(s.dataset, set()).add(s.name)
    for ds, names in by_ds.items():
        live, types = bq.list(ds)
        if set(live) != names:
            _fail(f"{ds}: живые объекты {sorted(set(live) ^ names)} расходятся с пакетом")
        bad = {n: t for n, t in types.items() if t != "VIEW"}
        if bad:
            _fail(f"{ds}: не-VIEW объекты {bad}")
    for s in specs:
        code, live = bq.get(s.dataset, s.name)
        if code != 200 or not _matches(live, s):
            _fail(f"{s.dataset}.{s.name}: живое определение расходится с пакетом")
    print(f"живые представления = пакет: {len(specs)}")


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ("verify-package", "plan-live", "deploy", "verify-live"):
        print(__doc__, file=sys.stderr)
        return 3
    from tools.tenancy.tenant_infra import contract_for
    try:
        cmd, tenant_id, root = argv[0], argv[1], Path(argv[2])
        contract = contract_for(tenant_id)
        specs = load_package(tenant_id, root, contract, dict(os.environ))
        print(f"пакет проверен: {len(specs)} представлений, порядок манифеста")
        if cmd == "verify-package":
            return 0
        token = os.environ.get("GCP_ACCESS_TOKEN", "")
        if not token:
            _fail("нет GCP_ACCESS_TOKEN (идентичность провижионера через WIF)")
        bq = BigQueryTables(token, contract["project_id"])
        if cmd == "plan-live":
            for op, s in plan_live(specs, bq):
                print(f"{op:6} {s.order:2} {s.dataset}.{s.name}")
        elif cmd == "deploy":
            deploy(specs, bq, contract["project_id"])
            verify_live(specs, bq)
        else:
            verify_live(specs, bq)
        return 0
    except SqlDeployError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
