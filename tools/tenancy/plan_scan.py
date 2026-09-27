#!/usr/bin/env python3
"""Сканер плана Terraform арендатора (Tenancy T3.2, после ревью H1/M1/M2/H2).

  python tools/tenancy/plan_scan.py <plan.json> <tenant_id>

Вход — `terraform show -json <plan>` и контракт арендатора из реестра. Это НЕ общий
сканер Terraform: он принуждает маленький поддерживаемый контракт `infra/tenant` и
отвергает всё, что в него не входит. Позиция по умолчанию — запрет.

Правила (любое нарушение — отказ):
  S. Структура: только разрешённые типы ресурсов, адреса (корень или module.ozon[0]),
     провайдеры (hashicorp/google и встроенный terraform), источники данных (ровно два
     guard'а); ни одного provisioner (local-exec, remote-exec, file); отложенное чтение
     источника данных в resource_changes — отказ.
  A. Действия только create / update / no-op. delete, replace, forget — отказ.
  U. Значения, от которых зависит безопасность (project, member, role, image, env,
     service_account, uri, paused, …), обязаны быть известны на этапе плана.
  P. Принципалы: для каждого IAM-ресурса ровно разрешённая тройка (ресурс, роль,
     принципал), выведенная из контракта. Всё прочее — allUsers, allAuthenticatedUsers,
     user:/group:/domain:, principal://, principalSet://, SA чужого проекта, платформы,
     EVETIS — отказ. Никакого правила «любой SA допустим».
  I. Никакого права вызова Cloud Run ни у кого: ни одной привязки job-IAM, ни одной роли,
     дающей run.jobs.run; SA планировщика не получает ни одной роли (модель H2).
  R. Ссылки: каждая ссылка на проект в любой форме — projects/<id>, projects/<номер>,
     домен SA, <номер>-compute@, service-<номер>@<агент>, pkg.dev/<id>, gcr.io/<id>,
     в том числе URL-кодированные, — только на проект арендатора (ID и номер из плана).
     Любая ссылка на бакет (gs://, storage.googleapis.com/…/b/…) — отказ.
  M. Маркеры EVETIS — нигде в плане; маркеры платформы (ID, номер, бакет state) — нигде в
     значениях ресурсов, кроме ровно утверждённого образа в поле image job'а.
  V. Расписания paused = true, URI — ровно run-вызов своего job'а, OAuth SA — свой
     планировщик. Образ job'а — ровно утверждённый digest контракта.
  D. ACL датасетов (T3.3) — авторитетный google_bigquery_dataset.access: известен на плане
     и ровно равен контракту (projectOwners OWNER + runtime SA WRITER/READER на свои
     датасеты). Провижионер, SA планировщика, публичные, внешние и чужие принципалы —
     отказ. Отдельных google_bigquery_dataset_iam_* быть не может.
  E. Исключение для google_service_account.member (вычисляемое поле провайдера 7.x,
     «serviceAccount:<свой email>»): допустимо ТОЛЬКО это поле, ровно свой email, и только
     для SA из контракта. Это не выдача прав и не список разрешённых привязок.
  F. Вычисляемые метаданные job'а Cloud Run после refresh (T3.3): creator и last_modifier
     (в схеме провайдера только computed) равны email провижионера, выполнявшего apply. Это
     не ссылка на платформу, если одновременно: тип ровно google_cloud_run_v2_job, поле ровно
     верхнего уровня creator/last_modifier, значение ровно PL.PROVISIONER_SA, и поле НЕ задано
     в конфигурации ресурса. Всё прочее — env, annotations, labels, образ, SA, URI, IAM, другие
     типы, другой email, вложенные строки — по-прежнему отказ.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.tenancy import platform as PL  # noqa: E402

# ── S. Структура ─────────────────────────────────────────────────────────────
ALLOWED_MANAGED_TYPES = frozenset({
    "terraform_data",
    "google_project_service",
    "google_bigquery_dataset", "google_bigquery_table",
    "google_project_iam_member",
    "google_service_account",
    "google_secret_manager_secret", "google_secret_manager_secret_iam_member",
    "google_cloud_run_v2_job",
    "google_cloud_scheduler_job",
})
ALLOWED_DATA_SOURCES = {"data.google_project.tenant": "google_project",
                        "data.google_projects.tenant_active": "google_projects"}
ALLOWED_MODULE_PREFIXES = ("", "module.ozon[0].")
ALLOWED_PROVIDERS = frozenset({"registry.terraform.io/hashicorp/google", "terraform.io/builtin/terraform"})
ALLOWED_ACTIONS = ({"create"}, {"update"}, {"no-op"})

# ── U. Поля, которые обязаны быть известны на плане ──────────────────────────
W = "*"
CRITICAL_FIELDS = {
    "google_project_service": [("project",), ("service",)],
    "google_bigquery_dataset": [("project",), ("dataset_id",), ("access",)],
    "google_bigquery_table": [("project",), ("dataset_id",), ("table_id",), ("schema",)],
    "google_project_iam_member": [("project",), ("role",), ("member",)],
    "google_service_account": [("project",), ("account_id",), ("email",), ("member",)],
    "google_secret_manager_secret": [("project",), ("secret_id",)],
    "google_secret_manager_secret_iam_member": [("project",), ("secret_id",), ("role",), ("member",)],
    "google_cloud_run_v2_job": [("project",), ("name",), ("location",),
                                ("template", W, "template", W, "service_account"),
                                ("template", W, "template", W, "containers", W, "image"),
                                ("template", W, "template", W, "containers", W, "env")],
    "google_cloud_scheduler_job": [("project",), ("name",), ("paused",), ("http_target", W, "uri"),
                                   ("http_target", W, "oauth_token", W, "service_account_email")],
    "terraform_data": [],
}

# ── F. Вычисляемые (computed-only в схеме провайдера) поля с identity исполнителя apply ──
COMPUTED_APPLIER_FIELDS = {"google_cloud_run_v2_job": frozenset({"creator", "last_modifier"})}

# ── I. Роли, дающие вызов Cloud Run ──────────────────────────────────────────
RUN_INVOKING_ROLES = frozenset({"roles/run.invoker", "roles/run.developer", "roles/run.admin",
                                "roles/run.jobsExecutor", "roles/run.jobsExecutorWithOverrides",
                                "roles/owner", "roles/editor"})

# ── R. Разбор ссылок ─────────────────────────────────────────────────────────
_NUM = r"[0-9]{6,20}"
_PID = r"[a-z][a-z0-9-]{4,28}[a-z0-9]"
_REF_PATTERNS = (
    ("project", re.compile(rf"projects/({_PID}|{_NUM})(?=$|[/?#:%\s\"'])")),
    ("number", re.compile(rf"(?<![0-9])({_NUM})-compute@developer\.gserviceaccount\.com")),
    ("number", re.compile(rf"(?<![0-9])({_NUM})@(?:cloudbuild|cloudservices)\.gserviceaccount\.com")),
    ("number", re.compile(rf"service-({_NUM})@[a-z0-9-]+\.iam\.gserviceaccount\.com")),
    ("sa_domain", re.compile(rf"@({_PID})\.iam\.gserviceaccount\.com")),
    ("project", re.compile(rf"\.pkg\.dev/({_PID})(?=/)")),
    ("project", re.compile(rf"(?:^|[/.])gcr\.io/({_PID})(?=/)")),
)
_SERVICE_AGENT_DOMAIN = re.compile(r"^(gcp-sa-[a-z0-9-]+|serverless-robot-prod|cloud-ml|compute-system|"
                                   r"container-engine-robot|dataflow-service-producer-prod|"
                                   r"containerregistry|firebase-rules|gs-project-accounts)$")
_BUCKET_REF = re.compile(r"gs://|storage\.googleapis\.com/(?:storage/v1/b/|upload/storage/v1/b/)?[a-z0-9]|"
                         r"storage\.cloud\.google\.com/")
_PRINCIPAL_PREFIX = re.compile(r"^(user|group|domain|serviceAccount|principal|principalSet|deleted|"
                               r"projectOwner|projectEditor|projectViewer):|^(allUsers|allAuthenticatedUsers)$")


def _strings(node, path="$"):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def _decoded_forms(s: str) -> list[str]:
    """Строка и её URL-декодированные формы (до трёх уровней: %252F → %2F → /)."""
    forms, cur = [s], s
    for _ in range(3):
        nxt = urllib.parse.unquote(cur)
        if nxt == cur:
            break
        forms.append(nxt)
        cur = nxt
    return forms


def _references(s: str) -> tuple[set[str], set[str], bool]:
    """(ID проектов, номера проектов, есть ли ссылка на бакет) во всех формах строки."""
    ids, nums, bucket = set(), set(), False
    for form in _decoded_forms(s):
        low = form.lower()
        for kind, rx in _REF_PATTERNS:
            for m in rx.findall(low):
                if kind == "number" or (kind == "project" and m.isdigit()):
                    nums.add(m)
                elif kind == "sa_domain":
                    if not _SERVICE_AGENT_DOMAIN.match(m):
                        ids.add(m)
                else:
                    ids.add(m)
        bucket = bucket or bool(_BUCKET_REF.search(low))
    return ids, nums, bucket


def _unknown_at(unknown, path) -> bool:
    if not path:
        return unknown is True or (isinstance(unknown, (dict, list)) and _any_true(unknown))
    if unknown is True:
        return True
    head, rest = path[0], path[1:]
    if head == W:
        return isinstance(unknown, list) and any(_unknown_at(u, rest) for u in unknown)
    return isinstance(unknown, dict) and head in unknown and _unknown_at(unknown[head], rest)


def _any_true(node) -> bool:
    if node is True:
        return True
    if isinstance(node, dict):
        return any(_any_true(v) for v in node.values())
    if isinstance(node, list):
        return any(_any_true(v) for v in node)
    return False


def _walk_config_modules(module: dict, prefix: str = ""):
    yield prefix, module
    for name, call in (module.get("module_calls") or {}).items():
        yield from _walk_config_modules(call.get("module") or {}, f"{prefix}module.{name}.")


def _tenant_number(plan: dict) -> str | None:
    for mod in _walk_state_modules((plan.get("prior_state") or {}).get("values", {}).get("root_module", {})):
        for r in mod.get("resources", []):
            if r.get("address") == "data.google_project.tenant":
                n = (r.get("values") or {}).get("number")
                return n if isinstance(n, str) and re.fullmatch(_NUM, n) else None
    return None


def _configured_attributes(plan: dict) -> dict[tuple[str, str], set[str]]:
    """(модуль, адрес ресурса в конфигурации) → атрибуты, заданные в конфигурации (expressions)."""
    out = {}
    for prefix, module in _walk_config_modules((plan.get("configuration") or {}).get("root_module") or {}):
        for r in module.get("resources", []) or []:
            out[(prefix, r.get("address", ""))] = set((r.get("expressions") or {}).keys())
    return out


def _computed_applier_identity(rtype, addr, path, s, configured) -> bool:
    """Правило F: вычисляемое creator/last_modifier job'а = провижионер, поле не задано в конфигурации.

    configured is None — конфигурация ресурса в плане не найдена: доказать, что поле не задано,
    нельзя, исключение не действует (fail closed).
    """
    if configured is None:
        return False
    fields = COMPUTED_APPLIER_FIELDS.get(rtype, frozenset())
    field = path[len(addr) + 1:] if path.startswith(addr + ".") else None
    return field in fields and s == PL.PROVISIONER_SA and field not in configured


def _walk_state_modules(module: dict):
    yield module
    for child in module.get("child_modules", []) or []:
        yield from _walk_state_modules(child)


def expected_iam(contract: dict) -> set[tuple[str, str, str, str]]:
    """Ровно разрешённые тройки (тип, цель, роль, принципал) — из контракта."""
    p, ozon = contract["project_id"], contract["marketplaces"].get("ozon")
    allowed = set()
    if ozon:
        runtime = f"serviceAccount:{ozon['service_accounts']['runtime']}@{p}.iam.gserviceaccount.com"
        allowed.add(("google_project_iam_member", p, "roles/bigquery.jobUser", runtime))
        for sid in ozon["secret_ids"].values():
            allowed.add(("google_secret_manager_secret_iam_member", sid, "roles/secretmanager.secretAccessor",
                         runtime))
    return allowed


def expected_dataset_access(contract: dict) -> dict[str, set[tuple[str, str, str]]]:
    """ACL каждого датасета — ровно (роль, вид принципала, принципал) из контракта (правило D)."""
    p, ozon = contract["project_id"], contract["marketplaces"].get("ozon")
    acl = {ds: {("OWNER", "special_group", "projectOwners")} for ds in contract["datasets"].values()}
    if ozon:
        runtime = f"{ozon['service_accounts']['runtime']}@{p}.iam.gserviceaccount.com"
        acl[contract["datasets"][ozon["raw_dataset_key"]]].add(("WRITER", "user_by_email", runtime))
        acl[contract["datasets"][ozon["ref_dataset_key"]]].add(("READER", "user_by_email", runtime))
    return acl


_ACCESS_PRINCIPAL_KEYS = ("user_by_email", "group_by_email", "domain", "special_group", "iam_member")
_ACCESS_NESTED_KEYS = ("view", "dataset", "routine", "condition")


def _access_entries(access) -> tuple[set[tuple[str, str, str]], list[str]]:
    """Нормализованные записи ACL датасета и описания записей, которые нормализовать нельзя."""
    entries, bad = set(), []
    for i, a in enumerate(access if isinstance(access, list) else []):
        if not isinstance(a, dict):
            bad.append(f"access[{i}] не объект")
            continue
        who = [(k, a.get(k)) for k in _ACCESS_PRINCIPAL_KEYS if a.get(k)]
        nested = [k for k in _ACCESS_NESTED_KEYS if a.get(k)]
        if nested or len(who) != 1:
            bad.append(f"access[{i}] {sorted(k for k, _v in who) + nested}")
            continue
        entries.add((str(a.get("role")), who[0][0], str(who[0][1])))
    return entries, bad


def _iam_target(rtype: str, after: dict, project: str) -> str | None:
    """Цель привязки в форме контракта.

    После refresh провайдер записывает secret_id полным именем «projects/<проект>/secrets/<id>»
    (живой план восстановления T3.3). Это та же привязка: полная форма сводится к <id> ТОЛЬКО
    для проекта арендатора; чужой проект, номер вместо ID, лишние сегменты — не сводятся и
    дают отказ (тройки нет в разрешённых), ссылки на чужие проекты ловит и правило R.
    """
    if rtype == "google_secret_manager_secret_iam_member":
        sid = after.get("secret_id")
        if isinstance(sid, str):
            m = re.fullmatch(r"projects/([^/]+)/secrets/([A-Za-z0-9_-]+)", sid)
            if m and m.group(1) == project:
                return m.group(2)
        return sid
    return {"google_project_iam_member": after.get("project")}.get(rtype)


def dataset_acl_findings(addr, after, unknown, dataset_acl, scheduler_email) -> list[str]:
    """Правило D для одного датасета: ACL ровно равен контракту."""
    out = []
    want = dataset_acl.get(after.get("dataset_id"))
    got, bad = _access_entries(after.get("access"))
    for b in bad:
        out.append(f"{addr}: запись ACL вне контракта ({b})")
    if want is None:
        out.append(f"{addr}: датасет {after.get('dataset_id')!r} вне контракта")
    elif not _unknown_at(unknown, ("access",)):
        for role, kind, who in sorted(got - want):
            out.append(f"{addr}: ACL датасета — лишняя запись {role} {kind}={who!r}{_acl_hint(who, scheduler_email)}")
        for role, kind, who in sorted(want - got):
            out.append(f"{addr}: ACL датасета — нет обязательной записи {role} {kind}={who!r}")
    return out


def _own_sa_member(addr, path, s, after, project, contract_sa_emails) -> bool:
    """Правило E: вычисляемое member сервисного аккаунта = он сам, и SA ожидается контрактом."""
    email = after.get("email")
    return (path == f"{addr}.member" and isinstance(email, str)
            and email == f"{after.get('account_id')}@{project}.iam.gserviceaccount.com"
            and email in contract_sa_emails and s == f"serviceAccount:{email}")


def _acl_hint(who: str, scheduler_email: str | None) -> str:
    low = who.lower()
    if PL.PROVISIONER_SA in low or "sa-tenant-provisioner" in low:
        return " (провижионер не может быть в ACL данных арендатора)"
    if scheduler_email and scheduler_email in low:
        return " (SA планировщика не получает доступа к данным)"
    if who in ("allUsers", "allAuthenticatedUsers"):
        return " (публичный доступ)"
    return ""


def invocation_grants(plan: dict) -> list[str]:
    """Адреса ресурсов плана, которые дали бы кому-либо право вызвать Cloud Run (модель H2).

    Пустой список означает: даже если задание Scheduler создано ENABLED или пауза не
    встала, вызвать job'ы арендатора некому — загрузка не начнётся.
    """
    out = []
    for rc in plan.get("resource_changes", []):
        rtype = rc.get("type", "")
        after = (rc.get("change") or {}).get("after") or {}
        if re.match(r"^google_cloud_run(_v2)?_(job|service)_iam_", rtype):
            out.append(rc.get("address", "?"))
        elif re.search(r"_iam_(member|binding|policy)$", rtype) and after.get("role") in RUN_INVOKING_ROLES:
            out.append(rc.get("address", "?"))
    return out


# ── T. Таблицы: определение объекта, который не является таблицей контракта ──────
TABLE_NON_TABLE_BLOCKS = ("view", "materialized_view", "external_data_configuration",
                          "table_replication_info")


def table_findings(addr: str, after: dict, unknown: dict, contract_tables: set) -> list[str]:
    """google_bigquery_table: пара (датасет, таблица) из контракта; блоков представления,
    материализованного представления, внешнего источника и репликации нет и не может появиться
    после apply (known after apply — тоже нарушение)."""
    out = []
    key = (after.get("dataset_id"), after.get("table_id"))
    if key not in contract_tables:
        out.append(f"{addr}: таблица {key[0]}.{key[1]} не входит в контракт")
    for block in TABLE_NON_TABLE_BLOCKS:
        if after.get(block) or (unknown or {}).get(block):
            out.append(f"{addr}: блок {block} запрещён — пакет SQL, а не Terraform, создаёт представления")
    return out


def scan_plan(plan: dict, contract: dict) -> list[str]:
    findings: list[str] = []
    project = contract["project_id"]
    ozon = contract["marketplaces"].get("ozon") or {}
    approved_image = ozon.get("runtime_image")
    region = contract["region"]
    runtime_email = f"{ozon['service_accounts']['runtime']}@{project}.iam.gserviceaccount.com" if ozon else None
    scheduler_email = f"{ozon['service_accounts']['scheduler']}@{project}.iam.gserviceaccount.com" if ozon else None
    platform_markers = (PL.PLATFORM_PROJECT_ID, PL.PLATFORM_PROJECT_NUMBER, PL.STATE_BUCKET)
    iam_allowed = expected_iam(contract)
    dataset_acl = expected_dataset_access(contract)
    contract_sa_emails = {e for e in (runtime_email, scheduler_email) if e}
    contract_tables = {(contract["datasets"][x["dataset_key"]], x["table_id"]) for x in contract.get("tables", [])}

    # M. EVETIS — нигде: configuration, prior_state, переменные, значения.
    for path, s in _strings(plan):
        for form in _decoded_forms(s):
            for marker in PL.EVETIS_FORBIDDEN_MARKERS:
                if marker.lower() in form.lower():
                    findings.append(f"{path}: идентификатор EVETIS {marker!r}")

    # S. Конфигурация: провайдеры, типы, источники данных, provisioners — на всей глубине модулей.
    config = plan.get("configuration") or {}
    for key, pc in (config.get("provider_config") or {}).items():
        if pc.get("full_name") not in ALLOWED_PROVIDERS:
            findings.append(f"configuration.provider_config.{key}: провайдер {pc.get('full_name')!r} не разрешён")
    for prefix, module in _walk_config_modules(config.get("root_module") or {}):
        if prefix not in ALLOWED_MODULE_PREFIXES and prefix != "module.ozon.":
            findings.append(f"configuration: модуль {prefix!r} не входит в контракт")
        for r in module.get("resources", []) or []:
            addr = prefix + r.get("address", "?")
            if r.get("provisioners"):
                findings.append(f"configuration {addr}: provisioner запрещён "
                                f"({[p.get('type') for p in r['provisioners']]})")
            if r.get("mode") == "data":
                if addr not in ALLOWED_DATA_SOURCES or ALLOWED_DATA_SOURCES[addr] != r.get("type"):
                    findings.append(f"configuration {addr}: источник данных вне контракта")
            elif r.get("type") not in ALLOWED_MANAGED_TYPES:
                findings.append(f"configuration {addr}: тип {r.get('type')} вне контракта")
            pk = r.get("provider_config_key", "")
            if pk and pk.split(":")[-1] not in ("google", "terraform"):
                findings.append(f"configuration {addr}: провайдер {pk!r} не разрешён")

    # S. Прочитанные на плане источники данных — только два guard'а.
    for mod in _walk_state_modules((plan.get("prior_state") or {}).get("values", {}).get("root_module", {})):
        for r in mod.get("resources", []) or []:
            if r.get("mode") == "data" and ALLOWED_DATA_SOURCES.get(r.get("address")) != r.get("type"):
                findings.append(f"prior_state {r.get('address')}: источник данных вне контракта")

    tenant_number = _tenant_number(plan)
    if tenant_number is None:
        findings.append("номер проекта арендатора (data.google_project.tenant.number) не известен на плане")
    elif tenant_number in (PL.EVETIS_PROJECT_NUMBER, PL.PLATFORM_PROJECT_NUMBER):
        findings.append(f"номер проекта арендатора {tenant_number} принадлежит EVETIS или платформе")

    config_attrs = _configured_attributes(plan)
    for rc in plan.get("resource_changes", []):
        addr, rtype, mode = rc.get("address", "?"), rc.get("type", ""), rc.get("mode")
        change = rc.get("change", {})
        actions = set(change.get("actions", []))
        after = change.get("after") or {}
        unknown = change.get("after_unknown") or {}
        # Модуль — из module_address плана; без него — из адреса до «<type>.».
        m_addr = rc.get("module_address")
        if m_addr is not None:
            module_prefix = f"{m_addr}."
        else:
            idx = addr.find(f"{rtype}.") if rtype else -1
            module_prefix = addr[:idx] if idx >= 0 else addr
        if mode == "data":
            findings.append(f"{addr}: отложенное чтение источника данных (known after apply) запрещено")
            continue
        if module_prefix not in ALLOWED_MODULE_PREFIXES:
            findings.append(f"{addr}: адрес вне корня и module.ozon[0]")
        if rtype not in ALLOWED_MANAGED_TYPES:
            findings.append(f"{addr}: тип {rtype} не входит в разрешённые для арендатора")
        if actions not in ALLOWED_ACTIONS:
            findings.append(f"{addr}: действие {sorted(actions)} запрещено (delete/replace/forget — отдельные ворота)")

        # U. Критичные значения известны.
        for fpath in CRITICAL_FIELDS.get(rtype, []):
            if _unknown_at(unknown, fpath):
                findings.append(f"{addr}: {'.'.join(fpath)} неизвестно на плане — проверить нельзя")

        # R/M. Ссылки и маркеры платформы.
        cfg_key = (re.sub(r"\[[^\]]*\]", "", module_prefix), f"{rtype}.{rc.get('name', '')}")
        configured = config_attrs.get(cfg_key)
        for path, s in _strings(after, addr):
            if approved_image and s == approved_image and rtype == "google_cloud_run_v2_job" \
                    and path.endswith(".image"):
                continue
            if _computed_applier_identity(rtype, addr, path, s, configured):
                continue
            for marker in platform_markers:
                if any(marker in f for f in _decoded_forms(s)):
                    findings.append(f"{path}: ссылка на платформу ({marker}) вне утверждённого образа")
            ids, nums, bucket = _references(s)
            for pid in sorted(ids - {project}):
                findings.append(f"{path}: ссылка на чужой проект {pid!r}")
            for num in sorted(nums - ({tenant_number} if tenant_number else set())):
                findings.append(f"{path}: ссылка на проект по номеру {num}")
            if bucket:
                findings.append(f"{path}: ссылка на бакет Cloud Storage запрещена")
            if rtype != "google_cloud_run_v2_job" and ".pkg.dev/" in s:
                findings.append(f"{path}: ссылка на реестр образов вне job'а")

        if "project" in after and after["project"] != project:
            findings.append(f"{addr}: project={after['project']!r}, ожидается {project!r}")

        # P/I. Принципалы и роли.
        principals = [(p, s) for p, s in _strings(after, addr) if _PRINCIPAL_PREFIX.match(s)]
        if rtype.endswith("_iam_member"):
            triple = (rtype, _iam_target(rtype, after, project), after.get("role"), after.get("member"))
            if triple not in iam_allowed:
                findings.append(f"{addr}: привязка {triple[1:]!r} не входит в разрешённые контрактом")
            if after.get("role") in RUN_INVOKING_ROLES:
                findings.append(f"{addr}: роль {after.get('role')} даёт вызов Cloud Run — запрещено до активации")
            if scheduler_email and scheduler_email in str(after.get("member", "")):
                findings.append(f"{addr}: SA планировщика не получает ролей до ворот активации")
        else:
            for path, s in principals:
                if rtype == "google_service_account" and _own_sa_member(addr, path, s, after, project,
                                                                          contract_sa_emails):
                    continue
                findings.append(f"{path}: принципал {s!r} вне IAM-ресурса контракта")

        # D. ACL датасета — известен и ровно равен контракту.
        if rtype == "google_bigquery_dataset":
            findings += dataset_acl_findings(addr, after, unknown, dataset_acl, scheduler_email)

        # T. Таблица — только таблица контракта и только таблица: не представление, не внешняя.
        if rtype == "google_bigquery_table":
            findings += table_findings(addr, after, unknown, contract_tables)

        if rtype == "google_cloud_run_v2_job":
            for path, s in _strings(after, addr):
                if path.endswith(".image") and s != approved_image:
                    findings.append(f"{path}: образ {s!r} не равен утверждённому digest")
                if path.endswith(".service_account") and s != runtime_email:
                    findings.append(f"{path}: job исполняется не от runtime SA арендатора ({s!r})")
        if rtype == "google_cloud_scheduler_job":
            if after.get("paused") is not True:
                findings.append(f"{addr}: расписание не на паузе")
            jobs = ozon.get("jobs", {})
            allowed_uris = {f"https://{region}-run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{j}:run"
                            for j in jobs}
            for path, s in _strings(after, addr):
                if path.endswith(".uri") and s not in allowed_uris:
                    findings.append(f"{path}: цель расписания {s!r} — не run-вызов job'а арендатора")
                if path.endswith(".service_account_email") and s != scheduler_email:
                    findings.append(f"{path}: расписание подписывается не SA планировщика ({s!r})")

    for addr in invocation_grants(plan):
        findings.append(f"{addr}: право вызова Cloud Run до ворот активации запрещено (ADR-08 И2)")

    for oc_name, oc in (plan.get("output_changes") or {}).items():
        for path, s in _strings(oc.get("after"), f"output.{oc_name}"):
            if approved_image and s == approved_image:
                continue
            ids, nums, bucket = _references(s)
            if ids - {project} or (nums - ({tenant_number} if tenant_number else set())) or bucket:
                findings.append(f"{path}: вывод ссылается за пределы проекта арендатора")
    return findings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 3
    from tools.tenancy.tenant_infra import contract_for
    from tools.tenancy.validation import parse_tenant_json   # единый строгий разборщик JSON
    plan = parse_tenant_json(Path(argv[0]).read_text(encoding="utf-8"))
    findings = scan_plan(plan, contract_for(argv[1]))
    for f in findings:
        print(f"FAIL {f}")
    print(f"plan-scan: нарушений {len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
