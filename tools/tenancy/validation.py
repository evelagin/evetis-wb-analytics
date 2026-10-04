"""Проверка арендаторов: схема tenant.v1, инварианты и поиск учётных данных (Tenancy T1).

Три линии, по убыванию силы:

1. СХЕМА. Полей для значений секретов в контракте нет вовсе, additionalProperties=false
   на каждом уровне: значение ключа просто некуда положить. Это главная гарантия.
2. ИНВАРИАНТЫ. То, что схема выразить не может: соответствие имён canonical-выводу
   (naming.py), защита EVETIS, согласованность модулей и сущностей, реестр целиком.
3. ПОИСК УЧЁТНЫХ ДАННЫХ. Детерминированные признаки известных форматов (UUID-ключ
   Seller API, client_id Performance API, JWT-токены WB, PEM, длинные случайные
   строки) во ВСЕХ строковых значениях. Это страховка, а не DLP: он ловит очевидную
   ошибку «вставил ключ вместо имени», и только.

Сообщения никогда не содержат подозрительное значение — только путь и правило.

T2.1: документ арендатора читается ТОЛЬКО через load_tenant_document /
parse_tenant_json — строгий разбор без повторяющихся ключей и без NaN/Infinity.
Шаблоны схемы проверяются ПОЛНЫМ совпадением (strict_pattern_findings): общий
валидатор AE ищет шаблон через re.search, где «$» совпадает и перед «\\n» в конце.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.autonomy.schema import check_schema, validate as schema_validate  # noqa: E402
from tools.tenancy import naming as N  # noqa: E402

SCHEMA_PATH = REPO / "tenants" / "_schema" / "tenant.schema.json"
OZON_ENTITIES_PY = REPO / "pipelines" / "ozon" / "runtime" / "entities.py"

# Модули, которые v1 поддерживает для выделенного арендатора. Остальное — только EVETIS.
DEDICATED_SUPPORTED_MODULES = frozenset({"ozon_core", "ozon_ads"})
# Схемы продаж, которые Ozon runtime реально загружает (заказы — только FBO).
# FBS/rFBS не поддерживаются, пока не принято решение D3.
SUPPORTED_FULFILLMENT_SCHEMES = frozenset({"FBO"})
ADS_ENTITIES = frozenset({"ads_campaigns", "ads_expense_daily", "ads_sku_daily"})
PROMO_ENTITIES = frozenset({"promo"})
LEGACY_ONLY_VALUES = {
    "product_master_source": "LEGACY_EVETIS_REF",
    "cogs_policy": "LEGACY_EVETIS",
    "tax_policy": "LEGACY_EVETIS",
    "scheduler_state": "NOT_MANAGED",
}


@dataclass(frozen=True)
class Finding:
    source: str          # файл или «registry»
    path: str            # JSON-путь внутри документа
    rule: str            # машинный код правила
    message: str         # без подозрительных значений

    def __str__(self) -> str:
        return f"{self.source}: {self.path}: [{self.rule}] {self.message}"


# ─────────────────────────────────────────── строгий разбор JSON (T2.1/L2)
class TenantDocumentError(ValueError):
    """Документ нельзя разобрать однозначно. В сообщении нет содержимого документа."""


_SAFE_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _reject_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            # Имя ключа показываем, только если оно похоже на имя поля контракта:
            # ключом по ошибке мог оказаться сам секрет.
            shown = repr(key) if _SAFE_KEY.fullmatch(key) else "<ключ скрыт>"
            raise TenantDocumentError(
                f"повторяющийся ключ {shown} в объекте JSON: разные парсеры берут разное "
                "значение (Python — последнее, другие — первое или ошибку)")
        obj[key] = value
    return obj


def _reject_constant(name):
    raise TenantDocumentError(f"нестандартная константа JSON {name} недопустима")


def parse_tenant_json(text: str):
    """ЕДИНСТВЕННЫЙ разбор документа арендатора (и его схемы).

    Отвергает повторяющиеся ключи на любой вложенности (даже с одинаковым
    значением) и NaN/Infinity, которые json.loads по умолчанию принимает, а
    стандартный JSON — нет. Иначе два потребителя одного файла могли бы увидеть
    разные значения.
    """
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys,
                          parse_constant=_reject_constant)
    except json.JSONDecodeError as e:
        raise TenantDocumentError(f"не JSON: строка {e.lineno}, столбец {e.colno}") from None


def load_tenant_document(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise TenantDocumentError("файл не в UTF-8") from None
    return parse_tenant_json(text)


# ─────────────────────────────────────────────────────────────── схема
def load_schema() -> dict:
    schema = load_tenant_document(SCHEMA_PATH)
    check_schema(schema)            # схема не может требовать того, что валидатор не проверяет
    return schema


def strict_pattern_findings(instance, schema: dict, source: str, path: str = "$") -> list[Finding]:
    """Каждый `pattern` схемы — полным совпадением строки (T2.1/L5).

    Общий валидатор AE (tools/autonomy/schema.py) ищет шаблон через re.search, а в
    Python «$» совпадает и перед завершающим «\\n»: "client_001\\n" проходил бы
    `^...$`. По стандарту JSON Schema (ECMA-262) такая строка шаблону НЕ
    соответствует. Здесь семантика восстановлена без изменения общего валидатора.
    """
    out = []
    if isinstance(instance, str) and "pattern" in schema:
        if not re.fullmatch(schema["pattern"], instance):
            out.append(Finding(source, path, "schema_exact",
                               f"значение не совпадает целиком с шаблоном {schema['pattern']}"))
    if isinstance(instance, dict):
        for key, value in instance.items():
            if key in schema.get("properties", {}):
                out += strict_pattern_findings(value, schema["properties"][key], source, f"{path}.{key}")
    if isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(instance):
            out += strict_pattern_findings(item, schema["items"], source, f"{path}[{i}]")
    return out


def load_ozon_entities() -> frozenset[str]:
    """Имена сущностей из REGISTRY Ozon runtime — через ast, без импорта облачных библиотек."""
    tree = ast.parse(OZON_ENTITIES_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "REGISTRY" for t in node.targets):
            return frozenset(ast.literal_eval(k) for k in node.value.keys)
    raise RuntimeError("REGISTRY не найден в pipelines/ozon/runtime/entities.py")


# ───────────────────────────────────────────── поиск учётных данных
CREDENTIAL_PATTERNS = (
    # Api-Key Seller API Ozon — UUID.
    ("uuid_api_key", re.compile(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")),
    # client_id Performance API Ozon.
    ("ozon_performance_client_id", re.compile(r"\d+-\d+@advertising\.performance\.ozon\.ru")),
    # JWT: токены WB API и любые подписанные токены.
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.")),
    ("pem_block", re.compile(r"-----BEGIN [A-Z ]+-----")),
    ("bearer_header", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("gcp_sa_key_json", re.compile(r'"private_key(_id)?"')),
)
# Длинная случайная строка: ≥32 символов алфавита ключей, есть и буквы, и цифры.
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
# Значение целиком из цифр: так выглядит Client-Id Seller API.
_BARE_NUMBER = re.compile(r"^\d{5,12}$")
SUSPICIOUS_KEY = re.compile(r"(?i)(api[_-]?key|passw|token|private[_-]?key|credential|secret)")
SECRET_REFS_PATH = re.compile(r"^\$\.marketplaces\.[a-z]+\.secret_refs$")


def _walk(node, path="$"):
    if isinstance(node, dict):
        for k, v in node.items():
            yield path, k, None
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, None, node


def detect_credential_material(doc, source: str) -> list[Finding]:
    """Очевидные учётные данные в документе арендатора. Значения в сообщения не попадают."""
    out = []
    for path, key, value in _walk(doc):
        if key is not None:
            # Ключ-«секрет» допустим только внутри secret_refs, где значения — ИМЕНА
            # и сами проверяются ниже. В остальных местах такой ключ — признак того,
            # что кто-то пытается хранить значение (схема его и так отвергнет).
            is_refs_container = key == "secret_refs"
            if (SUSPICIOUS_KEY.search(key) and not is_refs_container
                    and not SECRET_REFS_PATH.match(path)):
                out.append(Finding(source, f"{path}.{key}", "suspicious_key",
                                   "имя поля похоже на поле для значения секрета"))
            continue
        for rule, rx in CREDENTIAL_PATTERNS:
            if rx.search(value):
                out.append(Finding(source, path, f"credential:{rule}",
                                   "значение похоже на учётные данные; в Git хранится только "
                                   "ИМЯ секрета Secret Manager"))
        for m in _LONG_TOKEN.finditer(value):
            tok = m.group(0)
            if re.search(r"\d", tok) and re.search(r"[A-Za-z]", tok):
                out.append(Finding(source, path, "credential:long_random_token",
                                   f"длинная строка из букв и цифр ({len(tok)} симв.) похожа на ключ"))
                break
        if _BARE_NUMBER.match(value):
            out.append(Finding(source, path, "credential:bare_number",
                               "значение из одних цифр похоже на Client-Id"))
    return out


# ─────────────────────────────────────────────────── инварианты арендатора
def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def validate_tenant(doc, source: str, schema: dict | None = None,
                    ozon_entities: frozenset[str] | None = None) -> list[Finding]:
    """Все нарушения одного документа арендатора. Пустой список — документ валиден."""
    schema = schema or load_schema()
    out = detect_credential_material(doc, source)
    schema_errors = schema_validate(doc, schema)
    out += [Finding(source, e.split(":", 1)[0], "schema", e.split(":", 1)[-1].strip())
            for e in schema_errors]
    exact_errors = strict_pattern_findings(doc, schema, source)
    out += exact_errors
    if schema_errors or exact_errors:
        return out          # инварианты ниже рассчитаны на форму, которую дала схема

    tid = doc["tenant_id"]
    db, ozon, mods = doc["data_boundary"], doc["marketplaces"]["ozon"], doc["modules"]
    legacy = db["kind"] == "legacy_evetis_project"

    def err(path, rule, msg):
        out.append(Finding(source, path, rule, msg))

    try:
        N.check_tenant_id(tid, legacy=legacy)
    except N.NamingError as e:
        err("$.tenant_id", "naming", str(e))
        return out

    # ── связь вида границы, авторитета и ID ─────────────────────────────
    if legacy != (doc["config_authority"] == "LEGACY_EXISTING_CONFIG"):
        err("$.config_authority", "authority",
            "LEGACY_EXISTING_CONFIG допустим только для legacy_evetis_project и обязателен для него")

    if legacy:
        _legacy_evetis_invariants(doc, err)
    else:
        _dedicated_invariants(doc, err, ozon_entities or load_ozon_entities())

    # ── общие: секреты Ozon ──────────────────────────────────────────────
    refs = ozon.get("secret_refs", {})
    if ozon["enabled"]:
        for role in ("seller_client_id", "seller_api_key"):
            if role not in refs:
                err(f"$.marketplaces.ozon.secret_refs.{role}", "secret_ref_missing",
                    "Ozon включён: нужна ссылка на секрет Seller API")
        if mods["ozon_ads"]:
            for role in ("performance_client_id", "performance_client_secret"):
                if role not in refs:
                    err(f"$.marketplaces.ozon.secret_refs.{role}", "secret_ref_missing",
                        "модуль ozon_ads включён: нужна ссылка на секрет Performance API")
    elif refs:
        err("$.marketplaces.ozon.secret_refs", "secret_ref_unused", "Ozon выключен, ссылки на секреты лишние")
    if mods["ozon_core"] and not ozon["enabled"]:
        err("$.modules.ozon_core", "module", "модуль ozon_core требует marketplaces.ozon.enabled")
    if mods["wb"] != doc["marketplaces"]["wb"]["enabled"]:
        err("$.modules.wb", "module", "modules.wb и marketplaces.wb.enabled расходятся")

    # ── BI ──────────────────────────────────────────────────────────────
    bi = doc["bi_access"]
    if bi["mode"] == "ANALYTICS_SHARE_VIEWER" and not bi.get("principals"):
        err("$.bi_access.principals", "bi", "ANALYTICS_SHARE_VIEWER без получателей доступа")
    if bi["mode"] != "ANALYTICS_SHARE_VIEWER" and bi.get("principals"):
        err("$.bi_access.principals", "bi", "получатели доступа заданы, но режим доступа их не использует")

    # ── жизненный цикл ──────────────────────────────────────────────────
    lc, status = doc["lifecycle"], doc["status"]
    if status == "SUSPENDED" and "suspended_at" not in lc:
        err("$.lifecycle.suspended_at", "lifecycle", "статус SUSPENDED без suspended_at")
    if status == "OFFBOARDED" and "offboarded_at" not in lc:
        err("$.lifecycle.offboarded_at", "lifecycle", "статус OFFBOARDED без offboarded_at")
    for k in ("status_changed_at", "suspended_at", "offboarded_at"):
        if k in lc and _dt(lc[k]) < _dt(lc["created_at"]):
            err(f"$.lifecycle.{k}", "lifecycle", f"{k} раньше created_at")
    return out


def _legacy_evetis_invariants(doc, err) -> None:
    """EVETIS описывается, а не управляется: любое расхождение с зафиксированным фактом
    означает, что кто-то пытается изменить production EVETIS через реестр."""
    L = N.LEGACY_EVETIS
    db, ozon = doc["data_boundary"], doc["marketplaces"]["ozon"]
    msg = ("дескриптор EVETIS только описывает существующую конфигурацию; менять её "
           "нужно через Terraform и живые ресурсы, а не через реестр")
    if db["gcp_project_id"] != L["gcp_project_id"]:
        err("$.data_boundary.gcp_project_id", "evetis_protected", msg)
    if db["bq_location"] != L["bq_location"]:
        err("$.data_boundary.bq_location", "evetis_protected", msg)
    if db["datasets"] != L["datasets"]:
        err("$.data_boundary.datasets", "evetis_protected", msg)
    if ozon.get("secret_refs", {}) != L["ozon_secret_refs"]:
        err("$.marketplaces.ozon.secret_refs", "evetis_protected", msg)
    for k in ("entities", "history_request", "seller_inventory_model"):
        if k in ozon:
            err(f"$.marketplaces.ozon.{k}", "evetis_protected",
                "состав job'ов и история EVETIS заданы в Terraform, не в реестре")
    if doc["scheduler_state"] != "NOT_MANAGED":
        err("$.scheduler_state", "evetis_protected", "расписаниями EVETIS управляет Terraform: NOT_MANAGED")
    if doc["status"] != "ACTIVE":
        err("$.status", "evetis_protected", "EVETIS — действующий production-арендатор: ACTIVE")
    if "project_id_revision" in db:
        err("$.data_boundary.project_id_revision", "evetis_protected",
            "проект EVETIS не выводится из пространства имён арендаторов: ревизии у него нет")


def _dedicated_invariants(doc, err, ozon_entities: frozenset[str]) -> None:
    tid = doc["tenant_id"]
    db, ozon, mods = doc["data_boundary"], doc["marketplaces"]["ozon"], doc["modules"]
    L = N.LEGACY_EVETIS

    # проект: ровно канонический вывод из (tenant_id, ревизия) и не проект EVETIS
    pid = db["gcp_project_id"]
    if pid == L["gcp_project_id"]:
        err("$.data_boundary.gcp_project_id", "evetis_protected",
            "выделенный арендатор не может указывать на проект EVETIS")
    else:
        try:
            expected = N.derive_project_id(tid, db.get("project_id_revision"))
            if pid != expected:
                err("$.data_boundary.gcp_project_id", "naming",
                    f"ID проекта обязан быть каноническим выводом из tenant_id и ревизии: "
                    f"{expected} (tools/tenancy/naming.py)")
        except N.NamingError as e:
            err("$.data_boundary.gcp_project_id", "naming", str(e))

    if db["datasets"] != N.expected_datasets(tid):
        err("$.data_boundary.datasets", "naming",
            f"датасеты выделенного арендатора фиксированы: {N.expected_datasets(tid)}")

    refs = ozon.get("secret_refs", {})
    expected = N.expected_ozon_secret_ids(tid)
    for role, name in refs.items():
        if name in L["ozon_secret_refs"].values() or name.upper().startswith("EVETIS"):
            err(f"$.marketplaces.ozon.secret_refs.{role}", "evetis_protected",
                "выделенный арендатор не может ссылаться на секреты EVETIS")
        elif name != expected[role]:
            err(f"$.marketplaces.ozon.secret_refs.{role}", "naming",
                f"имя секрета фиксировано: {expected[role]} (tools/tenancy/naming.py)")

    for field, value in LEGACY_ONLY_VALUES.items():
        if doc[field] == value:
            err(f"$.{field}", "legacy_only", f"значение {value} допустимо только для EVETIS")
    if doc["bi_access"]["mode"] == "LEGACY_EVETIS_METABASE":
        err("$.bi_access.mode", "legacy_only",
            "Metabase EVETIS не является клиентским доступом (ADR-08: только analytics_share)")

    for m, on in mods.items():
        if on and m not in DEDICATED_SUPPORTED_MODULES:
            err(f"$.modules.{m}", "module_unsupported",
                f"модуль {m} в v1 не поддерживается для выделенного арендатора")
    if doc["marketplaces"]["wb"]["enabled"]:
        err("$.marketplaces.wb.enabled", "module_unsupported", "WB для выделенного арендатора в v1 не поддерживается")

    for scheme in ozon.get("fulfillment_schemes", []):
        if scheme not in SUPPORTED_FULFILLMENT_SCHEMES:
            err("$.marketplaces.ozon.fulfillment_schemes", "fulfillment_unsupported",
                f"{scheme}: Ozon runtime грузит заказы только FBO; решение D3 не принято")

    entities = set(ozon.get("entities", []))
    if ozon["enabled"] and mods["ozon_core"] and not entities:
        err("$.marketplaces.ozon.entities", "entities", "Ozon включён, а список сущностей пуст")
    unknown = sorted(entities - ozon_entities)
    if unknown:
        err("$.marketplaces.ozon.entities", "entities",
            f"неизвестные сущности Ozon runtime: {unknown}")
    if entities & ADS_ENTITIES and not mods["ozon_ads"]:
        err("$.marketplaces.ozon.entities", "entities", "рекламные сущности без модуля ozon_ads")
    if entities & PROMO_ENTITIES and not mods["ozon_promo"]:
        err("$.marketplaces.ozon.entities", "entities", "сущность promo без модуля ozon_promo")

    # T4: история запрашивается политикой, а не зашитой датой. Фактические границы по
    # сущностям — результат обнаружения возможностей (tenant_ops.HISTORY_BOUNDARIES).
    hr = ozon.get("history_request")
    if ozon["enabled"] and hr is None:
        err("$.marketplaces.ozon.history_request", "history",
            "Ozon включён: history_request обязателен (EARLIEST_AVAILABLE или FROM_DATE)")
    elif hr is not None:
        if hr["mode"] == "FROM_DATE" and "from_date" not in hr:
            err("$.marketplaces.ozon.history_request.from_date", "history",
                "режим FROM_DATE требует from_date")
        if hr["mode"] == "EARLIEST_AVAILABLE" and "from_date" in hr:
            err("$.marketplaces.ozon.history_request.from_date", "history",
                "EARLIEST_AVAILABLE не задаёт дату: границы определяет обнаружение возможностей")
        if "from_date" in hr:
            try:
                requested = datetime.strptime(hr["from_date"], "%Y-%m-%d").date()
            except ValueError:
                err("$.marketplaces.ozon.history_request.from_date", "history",
                    "from_date не является календарной датой")
            else:
                if requested > datetime.now().date():
                    err("$.marketplaces.ozon.history_request.from_date", "history",
                        "from_date в будущем: запрашивать можно только прошлое")


# ───────────────────────────────────────────────────── инварианты реестра
def validate_registry(docs: list[tuple[str, str, object]]) -> list[Finding]:
    """Нарушения реестра целиком. docs = [(source, имя_каталога, документ)]."""
    schema = load_schema()
    entities = load_ozon_entities()
    out: list[Finding] = []
    valid = []
    for source, dirname, doc in docs:
        found = validate_tenant(doc, source, schema, entities)
        out += found
        if isinstance(doc, dict) and doc.get("tenant_id") != dirname:
            out.append(Finding(source, "$.tenant_id", "registry",
                               "tenant_id должен совпадать с именем каталога tenants/<tenant_id>/"))
        if not found:
            valid.append((source, doc))

    def dup(kind, key_fn):
        seen = {}
        for source, doc in valid:
            key = key_fn(doc)
            if key is None:
                continue
            if key in seen:
                out.append(Finding(source, "$", "collision",
                                   f"{kind} совпадает с арендатором из {seen[key]}"))
            else:
                seen[key] = source

    dup("tenant_id", lambda d: d["tenant_id"])
    dup("GCP-проект", lambda d: d["data_boundary"]["gcp_project_id"])
    dup("префикс state Terraform",
        lambda d: None if d["data_boundary"]["kind"] == "legacy_evetis_project"
        else N.terraform_state_prefix(d["tenant_id"]))

    legacy = [s for s, d in valid if d["data_boundary"]["kind"] == "legacy_evetis_project"]
    if len(legacy) > 1:
        out.append(Finding("registry", "$", "registry", "унаследованный арендатор может быть только один"))
    all_ids = {d.get("tenant_id") for _, _, d in docs if isinstance(d, dict)}
    if N.LEGACY_TENANT_ID not in all_ids:
        out.append(Finding("registry", "$", "registry",
                           "нет дескриптора EVETIS (tenants/evetis/tenant.json)"))
    return out
