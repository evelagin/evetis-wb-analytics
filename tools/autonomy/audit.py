"""Аудит production-мутаций со стороны BigQuery. Сам аудит — только чтение.

Доказательство «AE не менял production» берётся не из слов агента, а из источников GCP (см. run_audit):
все задания BigQuery проекта во всех регионах (jobs.list), журналы Admin Activity и Data Access с цепочкой
делегирования и живая самопроверка IAM идентичности AE. Для AE v1 число мутаций обязано быть 0;
недоказанное — не ноль.
"""
from __future__ import annotations

import collections
import json
import math
import re
import urllib.parse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.bq_readonly import ReadOnlyBigQuery, resolve_token  # noqa: E402

# Версия источника доказательства. Доказательство другой версии (в т.ч. прежнего INFORMATION_SCHEMA по одному
# региону EU, который не видел LOAD/COPY/EXTRACT, SELECT с записью в таблицу и задания вне EU) нулём не считается.
AUDIT_SOURCE = "jobs_list+audit_logs+iam_selftest+iam_history+ingress_attribution/v5"

API = "https://www.googleapis.com"
CRM = "https://cloudresourcemanager.googleapis.com/v1"
LOGGING = "https://logging.googleapis.com/v2"
IAM = "https://iam.googleapis.com/v1"
ANON_DATASET = re.compile(r"^_[0-9a-f]{40}$")
SCRIPT_DATASET = re.compile(r"^_script[0-9a-f]{40}$")   # временные таблицы скриптов (живут 24 ч), не production
FEDERATED = ("principal://", "principalSet://")
SETTLE_SECONDS = 300            # от READY_FOR_PR (после ревьюера, т.е. после всех job'ов с GCP) до чтения журналов
WATERMARK_TRIES, WATERMARK_SLEEP = 12, 10
MAX_PAGES = 500
# Повтор только временных ошибок источника (429/5xx/сеть): конечное число попыток, фиксированная пауза.
# Исчерпание — исключение, т.е. BLOCKED; ошибка источника никогда не превращается в PASS.
TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})
RETRY_ATTEMPTS = 4
RETRY_BACKOFF = (2, 4, 8)
ADMIN_ACTIVITY_RETENTION_DAYS = 400   # бакет _Required: не отключается, не исключается, 400 дней
# Права, которых у идентичности AE не должно быть НИ НА ПРОЕКТЕ (вкл. наследование от организации/папок, группы,
# пользовательские роли). Самопроверка testIamPermissions — живая, в момент аудита; комментарию не доверяем.
FORBIDDEN_PROJECT_PERMISSIONS = [
    "bigquery.tables.create", "bigquery.tables.update", "bigquery.tables.updateData", "bigquery.tables.delete",
    "bigquery.tables.setIamPolicy", "bigquery.tables.setCategory", "bigquery.tables.restoreSnapshot",
    "bigquery.tables.createSnapshot", "bigquery.tables.deleteSnapshot", "bigquery.tables.export",
    "bigquery.tables.replicateData", "bigquery.tables.createIndex", "bigquery.tables.deleteIndex",
    "bigquery.datasets.create", "bigquery.datasets.update", "bigquery.datasets.delete",
    "bigquery.datasets.setIamPolicy", "bigquery.routines.create", "bigquery.routines.update",
    "bigquery.routines.delete", "bigquery.models.create", "bigquery.models.updateData",
    "bigquery.models.updateMetadata", "bigquery.models.delete", "bigquery.rowAccessPolicies.create",
    "bigquery.rowAccessPolicies.update", "bigquery.rowAccessPolicies.delete", "bigquery.transfers.update",
    "bigquery.connections.create", "bigquery.connections.update", "bigquery.connections.delete",
    "bigquery.connections.use", "bigquery.reservations.create", "bigquery.reservations.update",
    "bigquery.reservations.delete", "bigquery.jobs.delete", "storage.objects.create", "storage.objects.delete",
    "storage.buckets.create", "resourcemanager.projects.setIamPolicy", "iam.serviceAccounts.actAs",
    "iam.serviceAccounts.getAccessToken", "iam.serviceAccounts.signJwt", "iam.serviceAccounts.signBlob",
    "iam.serviceAccounts.implicitDelegation", "iam.serviceAccounts.getOpenIdToken", "iam.serviceAccountKeys.create",
    "run.jobs.run", "run.jobs.runWithOverrides", "run.routes.invoke", "pubsub.topics.publish",
    "cloudscheduler.jobs.run", "workflows.executions.create", "cloudfunctions.functions.call",
    "dataform.repositories.create", "dataform.workflowInvocations.create",
    # F-18: путь к секретам аутентификации публичного входа и к подделке журналов
    "secretmanager.versions.access", "cloudscheduler.jobs.get", "cloudscheduler.jobs.list", "run.services.get",
    "run.services.list", "run.revisions.get", "run.revisions.list", "storage.objects.get", "storage.objects.list",
    "logging.logEntries.create",
]
# На каждом SA проекта: право стать им или подписать от его имени (самопроверка iam testIamPermissions).
FORBIDDEN_SA_PERMISSIONS = ["iam.serviceAccounts.getAccessToken", "iam.serviceAccounts.signJwt",
                            "iam.serviceAccounts.signBlob", "iam.serviceAccounts.implicitDelegation",
                            "iam.serviceAccounts.getOpenIdToken", "iam.serviceAccounts.actAs",
                            "iam.serviceAccountKeys.create"]
READ_ONLY_ACL = {"READER", "roles/bigquery.dataViewer", "roles/bigquery.metadataViewer"}
PROJECT_GROUPS = {"projectReaders", "projectWriters", "projectOwners"}
# Data Access BigQuery: методы и метаданные чтения (калибровка по живому журналу 2026-09-27). Остальное — изменение.
BQ_READ_METHOD = re.compile(r"(?:^|[./])(Get\w*|List\w*|Query|InsertJob|TestIamPermissions|ReadRows|"
                            r"CreateReadSession|SplitReadStream|jobcompleted|getqueryresults|query|insert|list|get)$")
BQ_READ_META = {"@type", "jobInsertion", "jobChange", "tableDataRead"}
READ_PERMISSION_TYPES = {"DATA_READ", "ADMIN_READ"}


def is_ae_job(job: dict, identities: list[str]) -> bool:
    email = job.get("user_email") or ""
    purpose = ((job.get("configuration") or {}).get("labels") or {}).get("purpose", "")
    return email in identities or email.startswith(FEDERATED) or purpose.startswith("autonomy")


def job_mutation(job: dict, project: str) -> str | None:
    """None — задание только читает; иначе класс изменения. Незнакомое — изменение (fail closed)."""
    cfg = job.get("configuration") or {}
    jt = cfg.get("jobType")
    stats = job.get("statistics") or {}
    st = (stats.get("query") or {}).get("statementType")
    status = job.get("status") or {}
    if jt == "QUERY" and st == "SELECT":
        dest = (cfg.get("query") or {}).get("destinationTable")
        if not dest or (dest.get("projectId") == project and ANON_DATASET.match(dest.get("datasetId") or "")):
            return None                  # результат в собственном анонимном кэше, не в production
        return f"QUERY:SELECT->{dest.get('datasetId')}.{dest.get('tableId')}"
    if (jt == "QUERY" and st is None and status.get("state") == "DONE" and status.get("errorResult")
            and not int(stats.get("numChildJobs") or 0)):
        return None                      # не разобран и без дочерних — не исполнялся (одиночное задание атомарно)
    return f"{jt}:{st}"


def acl_problems(ds: str, access: list[dict], identities: list[str]) -> list[str]:
    """Записи ACL датасета, которые могут дать идентичности AE запись. Членство в группе/домене не
    проверяемо — роль выше чтения у такой записи уже отказ."""
    mine = set(identities) | {f"serviceAccount:{i}" for i in identities}
    out = []
    for a in access:
        role = a.get("role", "")
        if role in READ_ONLY_ACL or "view" in a or "routine" in a or "dataset" in a:
            continue
        if a.get("userByEmail") in mine or a.get("iamMember") in mine:
            if role == "OWNER" and ANON_DATASET.match(ds):
                continue                 # собственный анонимный датасет результатов
            out.append(f"{ds}: {role} у идентичности AE")
        elif a.get("specialGroup") in PROJECT_GROUPS:
            continue                     # членство — роль на проекте; её закрывает testIamPermissions
        elif "userByEmail" in a or a.get("iamMember", "").startswith(("serviceAccount:", "user:")):
            continue                     # конкретный другой принципал
        else:                            # группа, домен, allAuthenticatedUsers, principalSet — членство не проверить
            out.append(f"{ds}: {role} у {sorted(k for k in a if k != 'role')} — членство не проверяемо")
    return out


def _auth(entry: dict) -> tuple[str, list[dict]]:
    ai = (entry.get("protoPayload") or {}).get("authenticationInfo") or {}
    chain = list(ai.get("serviceAccountDelegationInfo") or [])
    if ai.get("principalEmail") and str(ai.get("principalSubject", "")).startswith(FEDERATED):
        chain.append({"principalSubject": ai["principalSubject"]})   # федеративный субъект под именем SA
    return ai.get("principalEmail") or ai.get("principalSubject") or "", chain


def _ae_principal(p: str, identities: list[str]) -> bool:
    return p in identities or p.startswith(FEDERATED)


def attribution(entry: dict, identities: list[str]) -> str | None:
    """Чья это запись: "ae" — действует AE (или прямой федеративный принципал); "delegated_ae" — SA AE в начале
    цепочки, действует другая идентичность (AE получил чужой токен); "federated_other" — цепочка от федеративного
    принципала к НЕ-AE SA: по WIF (проверено оценщиком S1) это доверенный workflow, но атрибуция по журналу
    неоднозначна; None — к AE не относится."""
    eff, chain = _auth(entry)
    if _ae_principal(eff, identities):
        return "ae"
    if any(((d.get("firstPartyPrincipal") or {}).get("principalEmail") or "") in identities for d in chain):
        return "delegated_ae"
    if any((d.get("principalSubject") or "").startswith(FEDERATED) for d in chain):
        return "federated_other"
    return None


def delegated_from_ae(entry: dict, identities: list[str]) -> bool:
    return attribution(entry, identities) == "delegated_ae"


def data_access_violation(entry: dict, identities: list[str]) -> str | None:
    """Запись Data Access, относящаяся к AE: None — чтение; иначе класс нарушения (незнакомое — нарушение)."""
    pp = entry.get("protoPayload") or {}
    svc, method = pp.get("serviceName", ""), str(pp.get("methodName"))
    eff, _ = _auth(entry)
    who = attribution(entry, identities)
    if who == "delegated_ae":
        return f"DELEGATED:{svc}:{eff}"             # AE получил и использовал чужую идентичность
    if who != "ae":
        return None
    if svc == "bigquery.googleapis.com":
        meta = set((pp.get("metadata") or {}).keys())
        if BQ_READ_METHOD.search(method) and meta <= BQ_READ_META:
            return None
        return f"DATA:{method}:{sorted(meta - BQ_READ_META)}"
    if svc == "secretmanager.googleapis.com" and "AccessSecretVersion" in method:
        return f"SECRET_READ:{str(pp.get('resourceName', '?')).split('/secrets/')[-1][:60]}"
    if svc == "iamcredentials.googleapis.com":
        target = ((pp.get("request") or {}).get("name") or "").rsplit("/", 1)[-1]
        return None if target in identities else f"IMPERSONATION:{target or '?'}"
    if method.endswith("TestIamPermissions"):
        return None                                 # самопроверка прав: без разрешения, ничего не меняет
    perms = pp.get("authorizationInfo") or []
    if perms and all(p.get("permissionType") in READ_PERMISSION_TYPES for p in perms):
        return None
    return f"DATA:{svc}:{method}"


class GcpAuditSource:
    """Только чтение: jobs.list (все регионы), журналы аудита, testIamPermissions/ACL. Любая ошибка — исключение."""

    def __init__(self, project: str, token: str, http=None, sleep=None, now=None, bq=None, refresh=None):
        import time
        from datetime import datetime, timezone
        self.project, self.token, self.refresh = project, token, refresh
        self.http = http or self._http
        self.sleep = sleep or time.sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.bq = bq or (lambda sql, location: ReadOnlyBigQuery(project=project, token=token, location=location,
                                                                label_purpose="autonomy-audit").query(sql))

    def _http(self, method: str, url: str, body: dict | None = None, _retry: bool = True) -> dict:
        import json
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        for attempt in range(RETRY_ATTEMPTS):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                if e.code == 401 and _retry and self.refresh:      # токен истёк посреди долгого аудита — один раз обновить
                    self.token = self.refresh()
                    return self._http(method, url, body, _retry=False)
                if e.code not in TRANSIENT_HTTP or attempt == RETRY_ATTEMPTS - 1:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == RETRY_ATTEMPTS - 1:
                    raise
            self.sleep(RETRY_BACKOFF[attempt])             # детерминированно; исчерпание — исключение → BLOCKED
        raise RuntimeError("недостижимо")

    def jobs(self, since_ms: int) -> list[dict]:
        from urllib.parse import urlencode
        out, token = [], None
        for _ in range(MAX_PAGES):
            q = {"allUsers": "true", "projection": "full", "maxResults": "1000", "minCreationTime": str(since_ms)}
            if token:
                q["pageToken"] = token
            r = self.http("GET", f"{API}/bigquery/v2/projects/{self.project}/jobs?{urlencode(q)}")
            if r.get("unreachable"):
                raise RuntimeError(f"jobs.list: недоступны регионы {r['unreachable']} — список неполон")
            out += r.get("jobs", [])
            token = r.get("nextPageToken")
            if not token:
                return out
        raise RuntimeError("jobs.list: превышен лимит страниц — частичный список не доказательство")

    def logs(self, flt: str) -> list[dict]:
        out, token = [], None
        for _ in range(MAX_PAGES):
            body = {"resourceNames": [f"projects/{self.project}"], "filter": flt, "pageSize": 1000}
            if token:
                body["pageToken"] = token
            r = self.http("POST", f"{LOGGING}/entries:list", body)
            out += r.get("entries", [])
            token = r.get("nextPageToken")
            if not token:
                return out
        raise RuntimeError("журнал аудита: превышен лимит страниц")

    activity = logs          # прежнее имя

    def routing(self) -> dict:
        """Маршрутизация журналов: sink _Default и исключения уровня проекта."""
        return {"sink": self.http("GET", f"{LOGGING}/projects/{self.project}/sinks/_Default"),
                "exclusions": self.http("GET", f"{LOGGING}/projects/{self.project}/exclusions").get("exclusions", []),
                "bucket": self.http("GET", f"{LOGGING}/projects/{self.project}/locations/global/buckets/_Default")}

    def granted(self, perms: list[str]) -> list[str]:
        r = self.http("POST", f"{CRM}/projects/{self.project}:testIamPermissions", {"permissions": perms})
        return sorted(r.get("permissions", []))

    def sa_granted(self, email: str, perms: list[str]) -> list[str] | None:
        """Самопроверка на конкретном SA. None — SA больше нет."""
        import urllib.error
        from urllib.parse import quote
        try:
            r = self.http("POST", f"{IAM}/projects/-/serviceAccounts/{quote(email)}:testIamPermissions",
                          {"permissions": perms})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        return sorted(r.get("permissions", []))

    def table_can_write(self, dataset: str, table: str) -> bool | None:
        """Самопроверка на конкретной таблице: есть ли у вызывающего updateData. None — таблицы больше нет."""
        import urllib.error
        from urllib.parse import quote
        url = f"{API}/bigquery/v2/projects/{self.project}/datasets/{quote(dataset)}/tables/{quote(table)}:testIamPermissions"
        try:
            r = self.http("POST", url, {"permissions": ["bigquery.tables.updateData"]})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        return "bigquery.tables.updateData" in r.get("permissions", [])

    def project_created(self) -> str:
        return self.http("GET", f"{CRM}/projects/{self.project}")["createTime"]

    def secret_granted(self, name: str) -> list[str] | None:
        """Самопроверка на конкретном секрете: может ли вызывающий прочитать значение. None — секрета нет."""
        import urllib.error
        try:
            r = self.http("POST", f"https://secretmanager.googleapis.com/v1/projects/{self.project}/secrets/{name}"
                          ":testIamPermissions", {"permissions": ["secretmanager.versions.access"]})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        return sorted(r.get("permissions", []))

    def bucket_granted(self, bucket: str, perms: list[str]) -> list[str] | None:
        """Самопроверка на конкретном бакете (IAM уровня бакета). None — бакета нет."""
        import urllib.error
        from urllib.parse import urlencode
        q = urlencode([("permissions", x) for x in perms])
        try:
            r = self.http("GET", f"https://storage.googleapis.com/storage/v1/b/{bucket}/iam/testPermissions?{q}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        return sorted(r.get("permissions", []))

    def probe_service(self, url: str, trace: str) -> int:
        """Контрольная отметка журналов сервиса: GET /health (только чтение) со своим trace."""
        import urllib.request
        req = urllib.request.Request(url, headers={"traceparent": f"00-{trace}-{'1' * 16}-01"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status

    def project_number(self) -> str:
        return str(self.http("GET", f"{CRM}/projects/{self.project}")["projectNumber"])

    def datasets(self) -> dict[str, dict]:
        r = self.http("GET", f"{API}/bigquery/v2/projects/{self.project}/datasets?all=true&maxResults=1000")
        if r.get("nextPageToken") or r.get("unreachable"):
            raise RuntimeError("список датасетов усечён или недоступны регионы")
        ids = [d["datasetReference"]["datasetId"] for d in r.get("datasets", [])]
        return {i: self.http("GET", f"{API}/bigquery/v2/projects/{self.project}/datasets/{i}") for i in ids}


def _ts(s: str):
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _order(entry: dict) -> tuple:
    """Порядок событий по времени: дробная часть секунд журнала бывает 0–9 знаков — сравниваем числами."""
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z?", str(entry.get("timestamp", "")))
    if not m:
        return ("", 0)
    return (m.group(1), int((m.group(2) or "").ljust(9, "0")))


def _logname(project: str, kind: str) -> str:
    return f'logName="projects/{project}/logs/cloudaudit.googleapis.com%2F{kind}"'


def _ae_filter(identities: list[str]) -> str:
    """AE — действующий принципал ИЛИ начало цепочки делегирования."""
    parts = []
    for i in identities:
        if '"' not in i:
            parts += [f'protoPayload.authenticationInfo.principalEmail="{i}"',
                      f'protoPayload.authenticationInfo.serviceAccountDelegationInfo.firstPartyPrincipal.principalEmail="{i}"']
    parts += ['protoPayload.authenticationInfo.principalSubject:"principal"',
              'protoPayload.authenticationInfo.serviceAccountDelegationInfo.principalSubject:"principal"']
    return "(" + " OR ".join(parts) + ")"


PUBLIC_MEMBERS = ("allUsers", "allAuthenticatedUsers")
WIF_ROLES = {"roles/iam.workloadIdentityUser", "roles/iam.serviceAccountTokenCreator"}
PROVIDER_FIELDS = {"attributeCondition", "attributeMapping", "displayName", "description", "oidc"}
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
UNPROVABLE_MEMBERS = ("group:", "domain:")
READ_ROLE = re.compile(r"(?i)(viewer|reader|metadataViewer)$")


def _event_policy(pp: dict) -> tuple[str, object]:
    """('full', bindings) | ('delta', bindingDeltas) | ('none', None) — IAM-политика из события Admin Activity."""
    req = pp.get("request") or {}
    if isinstance(req.get("policy"), dict):
        return "full", req["policy"].get("bindings", [])
    md = pp.get("metadata") or {}
    for change, obj in (("tableChange", "table"), ("routineChange", "routine"), ("datasetChange", "dataset")):
        pol = ((md.get(change) or {}).get(obj) or {}).get("policy")
        if isinstance(pol, dict):
            return "full", pol.get("bindings", [])
    for holder in (pp.get("serviceData") or {}, md):
        deltas = (holder.get("policyDelta") or {}).get("bindingDeltas")
        if isinstance(deltas, list):
            return "delta", deltas
    return "none", None


def iam_policy_state(events: list[dict]) -> tuple[dict[tuple[str, str], set[tuple[str, str]]], list[str]]:
    """Текущие привязки (роль, участник) каждого ресурса, восстановленные по ВСЕЙ истории SetIamPolicy
    (Admin Activity). Событие, из которого политику не восстановить, — отказ доказательства."""
    state: dict[tuple[str, str], set[tuple[str, str]]] = {}
    problems: list[str] = []
    for e in sorted(events, key=_order):
        pp = e.get("protoPayload") or {}
        if (pp.get("status") or {}).get("code"):
            continue                                   # отклонённое изменение ничего не поменяло
        key = (pp.get("serviceName", ""), pp.get("resourceName", ""))
        kind, data = _event_policy(pp)
        if kind == "full":
            state[key] = {(b.get("role", ""), m) for b in data for m in b.get("members", [])}
            if any(b.get("condition") for b in data):
                problems.append(f"{key[1][-80:]}: IAM-условия в политике — не моделируются")
        elif kind == "delta":
            cur = state.setdefault(key, set())
            for d in data:
                pair = (d.get("role", ""), d.get("member", ""))
                (cur.add if d.get("action") == "ADD" else cur.discard)(pair)
                if d.get("condition"):
                    problems.append(f"{key[1][-80:]}: IAM-условие в изменении — не моделируется")
        else:
            problems.append(f"{key[1][-80:]}: политику из события {pp.get('methodName')} не восстановить")
    return state, problems


def policy_problems(state: dict, identities: list[str], exempt: frozenset = frozenset()) -> list[str]:
    """Привязки, которые дают AE (или кому угодно) доступ, не видимый самопроверкой AE и журналами аудита:
    публичный доступ, группы/домены (членство не проверить), AE на отдельном ресурсе, прямой федеративный
    принципал вне SA. Публичную точку входа журналы аудита не видят вовсе — поэтому это отказ, а не ноль."""
    mine = {f"serviceAccount:{i}" for i in identities}
    out = []
    for (svc, rn), binds in sorted(state.items()):
        project_policy = svc == "cloudresourcemanager.googleapis.com"
        for role, m in sorted(binds):
            if m.startswith("deleted:") or (svc, rn, role, m) in exempt:
                continue
            where = f"{svc.split('.')[0]}:{rn.rsplit('/', 1)[-1][:60]} {role} → {m[:70]}"
            if m in PUBLIC_MEMBERS or m.startswith(UNPROVABLE_MEMBERS):
                if not (role.startswith("roles/") and READ_ROLE.search(role)):   # имя своей роли не доказательство
                    out.append(f"публичный/групповой доступ, невидимый аудиту: {where}")
            elif m in mine and not project_policy:
                out.append(f"AE на отдельном ресурсе: {where}")
            elif m.startswith(FEDERATED) and svc != "iam.googleapis.com":
                out.append(f"прямой федеративный доступ: {where}")
    return out


# ── F-18: публичный вход evetis-wb-communications (D-19b) ──────────────────────────────────────────
# `allUsers → run.invoker` на ЭТОМ сервисе не считается отказом только если за окно аудита есть полная
# машинная цепочка: журнал запроса Cloud Run (платформа) → auth_ok приложения → mutation_* — всё по ОДНОМУ
# trace, на ревизиях, где инструментирование доказано. Ни IP, ни HTTP-код доказательством не считаются.
F18_SERVICE = "evetis-wb-communications"
F18_REGION = "europe-west1"
F18_SA = "evetis-wb-comms@{project}.iam.gserviceaccount.com"
F18_INGRESS = "all"
F18_SCHEMA = "wbc-audit/1"
# маршрут → механизм аутентификации приложения; изменения разрешены только на этих маршрутах
F18_ROUTES = {"/poll": "scheduler_shared_secret", "/telegram-webhook": "telegram_secret_token",
              "/admin": "admin_token"}
F18_UNAUTHENTICATED = {"/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}  # только чтение
F18_AUTH_SECRETS = ("EVETIS_TELEGRAM_WEBHOOK_SECRET", "EVETIS_SCHEDULER_SECRET", "EVETIS_ADMIN_TOKEN",
                    "EVETIS_TELEGRAM_BOT_TOKEN", "EVETIS_WB_API_TOKEN")
F18_MAX_TIMEOUT = 300  # запрос, начатый до READY_FOR_PR, заканчивается до чтения журналов (SETTLE_SECONDS)
# изменение WB — только из webhook после секрета И allow-list; Telegram — из любого аутентифицированного маршрута
F18_TARGET_RULES = {"wildberries": {"/telegram-webhook": {"telegram_secret_token", "telegram_allowlist"}},
                    "telegram": {r: {m} for r, m in F18_ROUTES.items()}}
F18_EXPECTED_SA_BINDINGS = {
    ("cloudresourcemanager.googleapis.com", "project", "roles/bigquery.jobUser"),
    ("cloudresourcemanager.googleapis.com", "project", "roles/datastore.user"),
    *{("secretmanager.googleapis.com", n, "roles/secretmanager.secretAccessor") for n in (
        "EVETIS_ADMIN_TOKEN", "EVETIS_OPENAI_API_KEY", "EVETIS_SCHEDULER_SECRET", "EVETIS_TELEGRAM_BOT_TOKEN",
        "EVETIS_TELEGRAM_WEBHOOK_SECRET", "EVETIS_WB_API_TOKEN")}}
F18_EVENT_FIELDS = {"audit_event", "audit_schema", "service", "revision", "trace_id", "request_id", "route", "mechanism",
                    "principal_class", "result", "mutation_class", "target_system", "target_ref", "http_status",
                    "mutation_attempts",
                    "error_class", "severity", "message", "logger", "correlation_id",
                    "logging.googleapis.com/trace", "logging.googleapis.com/spanId"}
_SECRET_SHAPES = re.compile(r"\d{6,}(?::|%3[Aa])[A-Za-z0-9_-]{30,}|\beyJ[A-Za-z0-9._\-]{20,}|(?i:bearer\s+\S{8,})")


def _route(path: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlparse(path or "").path or path or "")   # как маршрутизирует Starlette
    return "/admin" if path.startswith("/admin") else path


def _secs(ts: str) -> float:
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z?", str(ts or ""))
    if not m:
        return -1.0
    from datetime import datetime, timezone
    base = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    return base + (int((m.group(2) or "0").ljust(9, "0")) / 1e9)


def _latency(v) -> float:
    try:
        return float(str(v or "0s").rstrip("s"))
    except ValueError:
        return -1.0


def f18_service_spec(events: list[dict], since: str = "") -> tuple[dict | None, list[str]]:
    """Текущие SA/ingress/timeout сервиса по истории Create/ReplaceService (Admin Activity). Непонятное — отказ.
    Каждое изменение ВНУТРИ окна тоже обязано быть в ожидаемом виде (переключить SA и вернуть — не пройдёт)."""
    spec, problems, in_window = None, [], []
    for e in sorted(events, key=_order):
        pp = e.get("protoPayload") or {}
        if (pp.get("status") or {}).get("code"):
            continue
        method = str(pp.get("methodName", "")).rsplit(".", 1)[-1]
        svc = (pp.get("request") or {}).get("service")
        if method in ("CreateService", "ReplaceService") and isinstance(svc, dict):
            tpl = ((svc.get("spec") or {}).get("template") or {}).get("spec") or {}
            spec = {"sa": tpl.get("serviceAccountName"), "timeout": tpl.get("timeoutSeconds"),
                    "ingress": ((svc.get("metadata") or {}).get("annotations") or {}).get("run.googleapis.com/ingress")}
            if since and _secs(e.get("timestamp")) >= _secs(since):
                in_window.append(spec)
        elif method in ("DeleteService", "UpdateService", "CreateService", "ReplaceService"):
            problems.append(f"изменение сервиса {method} в форме, которую аудит не моделирует")
    if spec is not None:
        spec = {**spec, "in_window": in_window}
    return spec, problems


def f18_sa_problems(state: dict, project: str, sa: str) -> list[str]:
    member = f"serviceAccount:{sa}"
    got = set()
    for (svc, rn), binds in state.items():
        for role, m in binds:
            if m == member:
                name = "project" if svc == "cloudresourcemanager.googleapis.com" else rn.rsplit("/", 1)[-1]
                got.add((svc, name, role))
    extra, missing = got - F18_EXPECTED_SA_BINDINGS, F18_EXPECTED_SA_BINDINGS - got
    return ([f"права runtime SA шире ожидаемых: {sorted(extra)[:3]}"] if extra else []) + \
           ([f"привязки runtime SA не восстановлены: {len(missing)}"] if missing else [])


def f18_attribution(requests: list[dict], events: list[dict], ready_revisions: set[str]) -> tuple[list[str], list[str], dict]:
    """(blocked, failed, stats) по цепочке запрос → auth_ok → mutation_*. Любая неоднозначность — не PASS."""
    blocked: list[str] = []
    failed: list[str] = []
    by_trace: dict[str, list[dict]] = {}
    for r in requests:
        t = (r.get("trace") or "").rsplit("/", 1)[-1]
        h = r.get("httpRequest") or {}
        route, status = _route(h.get("requestUrl", "")), int(h.get("status") or 0)
        rev = ((r.get("resource") or {}).get("labels") or {}).get("revision_name", "")
        if rev not in ready_revisions:
            # окно без инструментирования не доказуемо — только BLOCKED, без выдуманных «мутаций»
            blocked.append(f"ревизия {rev or '?'} обслуживала запросы без доказанного инструментирования")
            continue
        if not t:
            if 200 <= status < 300 and route not in F18_UNAUTHENTICATED:
                blocked.append(f"успешный запрос {route[:40]} без trace — не привязать")
            continue
        by_trace.setdefault(t, []).append({"route": route, "status": status, "rev": rev, "ts": _secs(r.get("timestamp")),
                                           "latency": _latency(h.get("latency")),
                                           "inst": (r.get("labels") or {}).get("instanceId", "")})
    for t, rs in by_trace.items():
        if len(rs) > 1:
            blocked.append(f"trace {t[:8]}… у {len(rs)} запросов — неоднозначно")
    ev_by_trace: dict[str, list[dict]] = {}
    for e in events:
        jp = e.get("jsonPayload") or {}
        kind = jp.get("audit_event")
        if not kind:
            continue
        if set(jp) - F18_EVENT_FIELDS:
            failed.append(f"событие {kind} с неожиданными полями {sorted(set(jp) - F18_EVENT_FIELDS)[:3]}")
        if _SECRET_SHAPES.search(json.dumps(jp)):
            failed.append(f"событие {kind} содержит форму секрета")
        if jp.get("audit_schema") != F18_SCHEMA:
            blocked.append(f"событие {kind} неизвестной схемы {jp.get('audit_schema')!r}")
            continue
        if kind == "instrumentation_ready":
            continue
        t = (e.get("trace") or "").rsplit("/", 1)[-1] or jp.get("trace_id") or ""
        if not t:
            (failed if kind.startswith("mutation_") else blocked).append(f"{kind} без trace — вне запроса")
            continue
        ev_by_trace.setdefault(t, []).append({**jp, "_ts": _secs(e.get("timestamp")),
                                              "_inst": (e.get("labels") or {}).get("instanceId", ""),
                                              "_rev": ((e.get("resource") or {}).get("labels") or {}).get("revision_name", "")})
    # полнота: у каждого запроса ровно один request_end, и его счётчик попыток = числу mutation_attempt в trace
    for t, rs in by_trace.items():
        ends = [ev for ev in ev_by_trace.get(t, []) if ev.get("audit_event") == "request_end"]
        if len(ends) != 1:
            blocked.append(f"trace {t[:8]}…: {len(ends)} request_end — полнота событий не доказана")
        else:
            attempts = sum(1 for ev in ev_by_trace.get(t, []) if ev.get("audit_event") == "mutation_attempt")
            if ends[0].get("mutation_attempts") != attempts:
                blocked.append(f"trace {t[:8]}…: request_end сообщает {ends[0].get('mutation_attempts')} попыток, "
                               f"в журнале {attempts} — события потеряны")
            if ends[0].get("http_status") != rs[0]["status"]:
                blocked.append(f"trace {t[:8]}…: статус request_end ≠ статусу журнала запроса")
    mutations = 0
    for t, evs in ev_by_trace.items():
        rs = by_trace.get(t)
        if not rs or len(rs) != 1:
            blocked.append(f"события trace {t[:8]}… не сопоставлены ровно одному запросу")
            continue
        req = rs[0]
        rids = {ev.get("request_id") for ev in evs}
        if len(rids) != 1 or not next(iter(rids)):
            blocked.append(f"trace {t[:8]}…: события без единого серверного request_id — несколько запросов под одним trace")
        for ev in evs:
            if ev["_rev"] != req["rev"] or ev.get("revision") != req["rev"]:
                blocked.append(f"trace {t[:8]}…: ревизия события ≠ ревизии запроса")
            if req["inst"] and ev["_inst"] and ev["_inst"] != req["inst"]:
                blocked.append(f"trace {t[:8]}…: экземпляр события ≠ экземпляру запроса")
            if req["ts"] < 0 or ev["_ts"] < 0 or req["latency"] < 0 or \
                    not (req["ts"] - 2 <= ev["_ts"] <= req["ts"] + req["latency"] + 10):
                blocked.append(f"trace {t[:8]}…: событие вне интервала своего запроса")
        # «доступ разрешён» считается только по auth_ok с result=ok; открытый allow-list — не доступ
        auth_at = {}
        for ev in evs:
            if ev.get("audit_event") == "auth_ok" and ev.get("result") == "ok":
                auth_at[ev.get("mechanism")] = min(auth_at.get(ev.get("mechanism"), ev["_ts"]), ev["_ts"])
        denied = {ev.get("mechanism") for ev in evs if ev.get("audit_event") == "auth_denied"}
        muts = [ev for ev in evs if str(ev.get("audit_event", "")).startswith("mutation_")]
        mutations += sum(1 for ev in muts if ev["audit_event"] == "mutation_attempt")
        for ev in muts:
            need = (F18_TARGET_RULES.get(ev.get("target_system")) or {}).get(req["route"])
            if need is None:
                failed.append(f"изменение {ev.get('mutation_class')} на маршруте {req['route']} вне политики")
            elif not need <= set(auth_at):
                failed.append(f"изменение {ev.get('mutation_class')} без аутентификации {sorted(need - set(auth_at))}")
            elif ev.get("target_system") == "wildberries" and denied:
                failed.append(f"изменение WB в trace с отказом аутентификации {sorted(denied)}")
            elif any(ev["_ts"] < auth_at[m] for m in need):
                failed.append(f"изменение {ev.get('mutation_class')} раньше аутентификации")
            if ev.get("result") == "outcome_unknown":
                blocked.append(f"trace {t[:8]}…: исход изменения {ev.get('mutation_class')} неизвестен — нужна сверка чтением")
        att = collections.Counter((ev.get("mutation_class"), ev.get("target_ref")) for ev in muts
                                  if ev["audit_event"] == "mutation_attempt")
        done = collections.Counter((ev.get("mutation_class"), ev.get("target_ref")) for ev in muts
                                   if ev["audit_event"] in ("mutation_success", "mutation_failure"))
        if att != done:
            blocked.append(f"trace {t[:8]}…: результат изменения неизвестен (attempt ≠ success/failure)")
    for t, rs in by_trace.items():
        if len(rs) == 1 and rs[0]["route"] in F18_ROUTES and 200 <= rs[0]["status"] < 300:
            mech = F18_ROUTES[rs[0]["route"]]
            if not any(ev.get("audit_event") == "auth_ok" and ev.get("mechanism") == mech for ev in ev_by_trace.get(t, [])):
                failed.append(f"успешный {rs[0]['route']} без auth_ok {mech} в том же trace")
        elif len(rs) == 1 and rs[0]["route"] not in F18_ROUTES and rs[0]["route"] not in F18_UNAUTHENTICATED \
                and 200 <= rs[0]["status"] < 300:
            failed.append(f"успешный запрос к неизвестному маршруту {rs[0]['route'][:40]}")
    return sorted(set(blocked)), sorted(set(failed)), {"requests": len(requests), "traces": len(by_trace),
                                                       "events": sum(len(v) for v in ev_by_trace.values()),
                                                       "mutation_attempts": mutations}


def wif_from_logs(wip_events: list[dict], state: dict, uid_to_account: dict[str, str], project: str = "",
                  number: str = "") -> tuple[dict | None, list[str]]:
    """Текущая конфигурация WIF (провайдер + привязки principalSet на SA), восстановленная по Admin Activity —
    вход того же оценщика, что и `wif_check --live`, но без прав на чтение политик SA."""
    provider: dict | None = None
    problems: list[str] = []
    # Пул и провайдер — ПОЛНЫМ путём ресурса (по id и по номеру проекта): одноимённые в другом пуле/проекте не наши.
    pools = {f"projects/{x}/locations/global/workloadIdentityPools/github-pool" for x in (project, number) if x}
    member_prefix = f"://iam.googleapis.com/projects/{number}/locations/global/workloadIdentityPools/github-pool/"
    for e in sorted(wip_events, key=_order):
        pp = e.get("protoPayload") or {}
        method = str(pp.get("methodName", "")).rsplit(".", 1)[-1]
        req = pp.get("request") or {}
        if not req or (pp.get("status") or {}).get("code"):
            continue                                   # парная запись завершения операции / отказ
        if method == "CreateWorkloadIdentityPool" and req.get("workloadIdentityPoolId") == "github-pool" and \
                f"{req.get('parent')}/workloadIdentityPools/github-pool" in pools:
            continue
        if method == "CreateWorkloadIdentityPoolProvider" and req.get("workloadIdentityPoolProviderId") == "github-provider" \
                and req.get("parent") in pools:
            provider = dict(req.get("workloadIdentityPoolProvider") or {})
            if set(provider) - PROVIDER_FIELDS:
                problems.append(f"поля провайдера, которые аудит не моделирует: {sorted(set(provider) - PROVIDER_FIELDS)}")
        elif method == "UpdateWorkloadIdentityPoolProvider" and provider is not None and \
                str((req.get("workloadIdentityPoolProvider") or {}).get("name", "")) in {f"{x}/providers/github-provider"
                                                                                       for x in pools}:
            for f in str(req.get("updateMask", "")).split(","):
                f = f.strip()
                if f not in PROVIDER_FIELDS:
                    problems.append(f"путь updateMask {f!r} не моделируется")
                    continue
                provider[f] = (req.get("workloadIdentityPoolProvider") or {}).get(f)
        else:
            problems.append(f"изменение WIF, которое аудит не моделирует: {method}")
    if provider is None:
        return None, problems + ["провайдер github-provider не восстановлен из Admin Activity"]
    if (provider.get("oidc") or {}).get("issuerUri") != GITHUB_ISSUER or provider.get("disabled"):
        problems.append("провайдер не OIDC GitHub (issuer) или выключен — оценщик S1 его не моделирует")
    sab: dict[str, list[dict]] = {}
    for (svc, rn), binds in state.items():
        if svc != "iam.googleapis.com":
            continue
        uid = rn.rsplit("/", 1)[-1]
        fed = [(r, m) for r, m in binds if m.startswith(FEDERATED)]
        if not fed:
            continue
        from tools.autonomy.wif_check import _parse_member
        for r, m in fed:
            if not number or not m.startswith(("principal" + member_prefix, "principalSet" + member_prefix)):
                problems.append(f"SA {uid}: федеративный принципал другого пула — не моделируется: {m[:90]}")
            elif _parse_member(m) is None:
                problems.append(f"SA {uid}: федеративный участник вне модели S1 (напр. весь пул): {m[:90]}")
            if r not in WIF_ROLES:
                problems.append(f"SA {uid}: роль {r} у федеративного принципала — оценщик S1 её не учитывает")
        acc = uid_to_account.get(uid)
        if not acc:
            problems.append(f"SA {uid}: не сопоставлен с событием создания")
            continue
        roles: dict[str, list[str]] = {}
        for r, m in fed:
            roles.setdefault(r, []).append(m)
        sab[acc] = [{"role": r, "members": sorted(ms)} for r, ms in sorted(roles.items())]
    return {"provider": {"attributeCondition": provider.get("attributeCondition"),
                         "attributeMapping": provider.get("attributeMapping") or {}},
            "service_account_bindings": sab}, problems


# Фильтр sink _Default по умолчанию (живой, 2026-09-27): исключает только журналы, которые идут в _Required.
DEFAULT_SINK_FILTER = ('NOT LOG_ID("cloudaudit.googleapis.com/activity") AND NOT LOG_ID("externalaudit.googleapis.com/activity") '
                       'AND NOT LOG_ID("cloudaudit.googleapis.com/system_event") AND NOT LOG_ID("externalaudit.googleapis.com/system_event") '
                       'AND NOT LOG_ID("cloudaudit.googleapis.com/access_transparency") AND NOT LOG_ID("externalaudit.googleapis.com/access_transparency")')


def table_inventory(events: list[dict], project: str) -> set[tuple[str, str]]:
    """Существующие таблицы проекта по истории Admin Activity: последнее событие по имени — создание, а не удаление.
    Анонимные датасеты результатов (`_<hex>`) — не production. Истёкшие без события таблицы дадут 404 (пропуск)."""
    rx = re.compile(rf"projects/{re.escape(project)}/datasets/([^/]+)/tables/([^/@$]+)$")
    alive: dict[tuple[str, str], bool] = {}
    for e in sorted(events, key=_order):
        pp = e.get("protoPayload") or {}
        if (pp.get("status") or {}).get("code"):
            continue
        md = pp.get("metadata") or {}
        gone = "tableDeletion" in md or str(pp.get("methodName", "")).endswith("DeleteTable")
        names = [pp.get("resourceName", "")] + [((md.get(k) or {}).get("table") or {}).get("tableName", "")
                                               for k in ("tableCreation", "tableDeletion")]
        for n in names:
            m = rx.match(n or "")
            if m:
                alive[(m.group(1), m.group(2))] = not gone
    return {k for k, v in alive.items() if v and not ANON_DATASET.match(k[0]) and not SCRIPT_DATASET.match(k[0])}


def routing_problems(routing: dict, window=None) -> list[str]:
    """Data Access обязан доходить до _Default целиком и храниться дольше окна прогона — иначе его отсутствие
    ничего не доказывает. Фильтр — только точный фильтр по умолчанию (любое сужение могло бы скрыть записи)."""
    out = []
    sink = routing.get("sink") or {}
    if sink.get("disabled"):
        out.append("sink _Default выключен")
    if " ".join((sink.get("filter") or "").split()) != " ".join(DEFAULT_SINK_FILTER.split()):
        out.append("фильтр _Default отличается от фильтра по умолчанию")
    if not str(sink.get("destination", "")).endswith("/buckets/_Default") and "destination" in sink:
        out.append("sink _Default пишет не в бакет _Default")
    days = (routing.get("bucket") or {}).get("retentionDays")
    if window is not None and (not isinstance(days, int) or days * 86400 <= window.total_seconds() + 86400):
        out.append(f"хранение _Default ({days} дн.) не покрывает окно прогона с запасом в сутки")
    for ex in (sink.get("exclusions") or []) + (routing.get("exclusions") or []):
        if not ex.get("disabled"):
            out.append(f"активное исключение журнала {ex.get('name')!r}")
    return out


def f18_check(src, state: dict, since_iso: str, by_type: dict, created: str) -> tuple[frozenset, list[str]]:
    """Атрибуция публичного входа evetis-wb-communications за окно. PASS цепочки → исключение ровно одной
    привязки (allUsers → run.invoker на этом сервисе); иначе BLOCKED/FAIL, и привязка остаётся отказом."""
    rn = f"projects/{src.project}/locations/{F18_REGION}/services/{F18_SERVICE}"
    key = ("run.googleapis.com", rn)
    if ("roles/run.invoker", "allUsers") not in state.get(key, set()):
        return frozenset(), []
    blocked: list[str] = []
    others = {b for b in state.get(key, set()) if b != ("roles/run.invoker", "allUsers")
              and (b[1] in PUBLIC_MEMBERS or b[1].startswith(UNPROVABLE_MEMBERS))}
    if others:
        blocked.append(f"F-18: на сервисе ещё публичные/групповые привязки {sorted(others)[:2]}")
    spec, sp = f18_service_spec(src.logs(
        f'{_logname(src.project, "activity")} AND timestamp>="{created}" AND protoPayload.serviceName="run.googleapis.com" '
        f'AND protoPayload.resourceName:"services/{F18_SERVICE}" AND protoPayload.methodName:"Service"'), since_iso)
    sa = F18_SA.format(project=src.project)
    blocked += [f"F-18: {x}" for x in sp]

    def expected(sp_: dict) -> bool:
        t_ = sp_.get("timeout")
        return (sp_.get("sa") == sa and sp_.get("ingress") == F18_INGRESS
                and isinstance(t_, int) and 0 < t_ <= F18_MAX_TIMEOUT)
    if not spec or not expected(spec) or not all(expected(x) for x in spec.get("in_window", [])):
        blocked.append(f"F-18: SA/ingress/timeout сервиса не совпадают с ожидаемыми (в т.ч. внутри окна) или не восстановлены")
    blocked += [f"F-18: {x}" for x in f18_sa_problems(state, src.project, sa)]
    # AE не должна уметь получить ни один секрет аутентификации публичного входа (иначе «аутентифицирован» ≠ «не AE»)
    for name in F18_AUTH_SECRETS:
        got = src.secret_granted(name)
        if got is None:     # секрета нет под ожидаемым именем — куда делся, не доказать
            blocked.append(f"F-18: секрет аутентификации {name} не найден — самопроверка невозможна")
        elif got:
            blocked.append(f"F-18: идентичность AE может читать секрет {name}")
    # Terraform state хранит живые заголовки планировщика (X-Scheduler-Secret) — AE не должна читать бакет state
    tf_bucket = f"evetis-wb-tfstate-{src.project_number()}"
    got = src.bucket_granted(tf_bucket, ["storage.objects.get", "storage.objects.list"])
    if got is None:         # бакет переименован/перенесён — новый может быть читаем, молча не проходим
        blocked.append(f"F-18: бакет Terraform state {tf_bucket} не найден — самопроверка невозможна")
    elif got:
        blocked.append(f"F-18: идентичность AE может читать Terraform state ({tf_bucket})")
    base = f'resource.type="cloud_run_revision" AND resource.labels.service_name="{F18_SERVICE}"'
    stdout = (f'{base} AND logName="projects/{src.project}/logs/run.googleapis.com%2Fstdout" '
              f'AND jsonPayload.logger="app.audit"')           # события пишет только логгер приложения в stdout
    # контрольная отметка журналов сервиса: наш GET /health со своим trace должен появиться и в журнале запросов,
    # и как request_end — только тогда окно журналов сервиса считается догнавшим
    import secrets as _secrets
    wm_trace = _secrets.token_hex(16)
    url = f"https://{F18_SERVICE}-{src.project_number()}.{F18_REGION}.run.app/health"
    src.probe_service(url, wm_trace)
    for attempt in range(WATERMARK_TRIES):
        seen_req = src.logs(f'{base} AND logName="projects/{src.project}/logs/run.googleapis.com%2Frequests" '
                            f'AND trace="projects/{src.project}/traces/{wm_trace}"')
        seen_end = src.logs(f'{stdout} AND jsonPayload.audit_event="request_end" AND jsonPayload.trace_id="{wm_trace}"')
        if seen_req and seen_end:
            break
        if attempt == WATERMARK_TRIES - 1:
            blocked.append("F-18: контрольная отметка журналов сервиса не появилась — полнота окна не доказана")
        src.sleep(WATERMARK_SLEEP)
    requests = [r for r in src.logs(f'{base} AND logName="projects/{src.project}/logs/run.googleapis.com%2Frequests" '
                                    f'AND timestamp>="{since_iso}"')
                if not str(r.get("trace", "")).endswith(wm_trace)]
    events = [e for e in src.logs(f'{stdout} AND jsonPayload.audit_event:* AND timestamp>="{since_iso}"')
              if (e.get("jsonPayload") or {}).get("trace_id") != wm_trace]
    revisions = {((r.get("resource") or {}).get("labels") or {}).get("revision_name", "") for r in requests}
    from datetime import timedelta
    marker_from = (src.now() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")   # хранение _Default
    ready = set()
    for rev in sorted(x for x in revisions if x):
        marks = src.logs(f'{stdout} AND resource.labels.revision_name="{rev}" AND timestamp>="{marker_from}" AND '
                         f'jsonPayload.audit_event="instrumentation_ready" AND jsonPayload.audit_schema="{F18_SCHEMA}"')
        if any(set((m.get("jsonPayload") or {})) <= F18_EVENT_FIELDS
               and (m.get("jsonPayload") or {}).get("revision") == rev for m in marks):
            ready.add(rev)
    b, f, stats = f18_attribution(requests, events, ready)
    blocked += [f"F-18: {x}" for x in b]
    for x in f:
        by_type["F18:" + x[:200]] = by_type.get("F18:" + x[:200], 0) + 1
    if blocked or f:
        return frozenset(), blocked
    return frozenset({("run.googleapis.com", rn, "roles/run.invoker", "allUsers")}), []


def run_audit(src: GcpAuditSource, since_iso: str, identities: list[str], watermark, settle_from: str | None = None
              ) -> dict:
    """Решение аудита. PASS только если ВСЕ источники прочитаны полностью и чисты:
      1) jobs.list без location — задания ВСЕХ регионов и типов (LOAD/COPY/EXTRACT, SELECT с записью, SCRIPT);
         `unreachable` — отказ; полнота окна доказана зондом-отметкой, созданным после всех недоверенных job'ов;
      2) Admin Activity (не отключается и не исключается) — изменения метаданных/IAM, где AE действует сам
         ИЛИ стоит в начале цепочки делегирования (имперсонация другого SA), в любом сервисе;
      3) Data Access (маршрутизация без исключений доказана, зонд виден и здесь): записи данных вне заданий
         (insertAll, Storage Write), выпуск токенов ДРУГИХ SA, любое использование чужой идентичности из AE;
      4) самопроверка IAM: нет прав записи/имперсонации на проекте (вкл. организацию/группы), ни на одном SA
         проекта (все SA — из событий создания в Admin Activity), нет записи ACL выше чтения на видимых
         датасетах, нет updateData ни на одной существующей таблице проекта (перечень по истории Admin Activity),
         текущие IAM-политики всех ресурсов и конфигурация WIF (оценщик S1) восстановлены по той же истории.
    Нарушение инварианта IAM или маршрутизации — BLOCKED (ноль не доказан), найденное изменение — FAIL."""
    from datetime import timedelta
    if settle_from:
        wait = (_ts(settle_from) + timedelta(seconds=SETTLE_SECONDS) - src.now()).total_seconds()
        if wait > 0:
            src.sleep(min(wait, SETTLE_SECONDS))
    since = _ts(since_iso)
    since_ms = int(since.timestamp() * 1000)
    label = watermark()                                    # SELECT с уникальной меткой: отметка «журнал догнал»
    probe = f'protoPayload.metadata.jobChange.job.jobConfig.labels.purpose="{label}"'
    for attempt in range(WATERMARK_TRIES):
        jobs = src.jobs(since_ms)
        seen_jobs = any(((j.get("configuration") or {}).get("labels") or {}).get("purpose") == label for j in jobs)
        seen_logs = seen_jobs and bool(src.logs(f'{_logname(src.project, "data_access")} AND {probe}'))
        if seen_logs:
            break
        if attempt == WATERMARK_TRIES - 1:
            return {"status": "BLOCKED", "mutations": None, "source": AUDIT_SOURCE,
                    "error": "jobs.list или Data Access не показал зонд аудита — полнота окна не доказана"}
        src.sleep(WATERMARK_SLEEP)
    by_type: dict[str, int] = {}

    def add(kind: str) -> None:
        by_type[kind] = by_type.get(kind, 0) + 1
    ae_jobs = [j for j in jobs if is_ae_job(j, identities)]
    for j in ae_jobs:
        m = job_mutation(j, src.project)
        if m:
            add(m)
    window = f'timestamp>="{since_iso}" AND {_ae_filter(identities)}'
    activity = src.logs(f'{_logname(src.project, "activity")} AND {window}')
    unattributed = 0
    for e in activity:
        who = attribution(e, identities)
        if who in ("federated_other", None):          # совпало с фильтром AE, но не приписано — не игнорируем
            unattributed += 1
        elif who:
            add("ADMIN:" + str((e.get("protoPayload") or {}).get("methodName")))
    access = src.logs(f'{_logname(src.project, "data_access")} AND {window}')
    for e in access:
        if attribution(e, identities) in ("federated_other", None):
            unattributed += 1
            continue
        v = data_access_violation(e, identities)
        if v:
            add(v)
    iam: list[str] = routing_problems(src.routing(), src.now() - since)
    if unattributed:
        iam.append(f"в окне прогона действовали другие WIF-workflow ({unattributed} записей) — атрибуция "
                   f"неоднозначна; повторить аудит, когда в окне не будет развёртываний")
    granted = src.granted(FORBIDDEN_PROJECT_PERMISSIONS)
    if granted:
        iam.append(f"права записи/имперсонации на проекте: {granted}")
    datasets = src.datasets()
    for ds, meta in datasets.items():
        iam += acl_problems(ds, meta.get("access", []), identities)
    created = src.project_created()
    age = src.now() - _ts(created)
    if age > timedelta(days=ADMIN_ACTIVITY_RETENTION_DAYS):
        iam.append("проект старше хранения Admin Activity — отсутствие IAM-политик объектов не доказуемо")
    # Все SA проекта — из событий создания (Admin Activity за всю жизнь проекта); на каждом — самопроверка.
    sas: set[str] = set()
    uid_to_account: dict[str, str] = {}
    for e in src.logs(f'{_logname(src.project, "activity")} AND timestamp>="{created}" AND '
                      f'protoPayload.methodName="google.iam.admin.v1.CreateServiceAccount"'):
        pp = e.get("protoPayload") or {}
        resp = pp.get("response") or {}
        if resp.get("email"):
            sas.add(resp["email"])
            if resp.get("unique_id"):
                uid_to_account[str(resp["unique_id"])] = resp["email"].split("@", 1)[0]
        elif not (pp.get("status") or {}).get("code"):
            iam.append("событие создания SA без email — перечень SA неполон")
    for email in sorted(sas):
        got = src.sa_granted(email, FORBIDDEN_SA_PERMISSIONS)
        if got:
            iam.append(f"{email}: {got} у идентичности AE")
    # IAM-политики ВСЕХ ресурсов проекта за всю его жизнь (Admin Activity): текущее состояние каждой.
    policy_events = src.logs(f'{_logname(src.project, "activity")} AND timestamp>="{created}" AND '
                             f'(protoPayload.methodName:"SetIamPolicy" OR protoPayload.methodName:"setIamPermissions")')
    state, broken = iam_policy_state(policy_events)
    exempt, f18 = f18_check(src, state, since_iso, by_type, created)
    iam += broken + f18 + policy_problems(state, identities, exempt)
    # SA ДРУГИХ проектов с ролями здесь (политики ресурсов, ACL видимых датасетов) — тоже самопроверка.
    foreign = {m.split(":", 1)[1] for binds in state.values() for _, m in binds if m.startswith("serviceAccount:")}
    foreign |= {a["userByEmail"] for meta in datasets.values() for a in meta.get("access", [])
                if str(a.get("userByEmail", "")).endswith(".gserviceaccount.com")}
    for email in sorted(foreign - sas - set(identities)):
        got = src.sa_granted(email, FORBIDDEN_SA_PERMISSIONS)
        if got:
            iam.append(f"{email}: {got} у идентичности AE")
    # Политики уровня таблиц дают запись в обход ACL датасета и не видны в списке датасетов — на каждой ещё
    # существующей таблице самопроверка. Политики процедур данных не пишут (процедура исполняется с правами
    # вызывающего), их изменения — Admin Activity.
    tables: set[tuple[str, str]] = set()
    for svc, rn in state:
        if svc != "bigquery.googleapis.com":
            continue
        m = re.fullmatch(rf"projects/{re.escape(src.project)}/datasets/([^/]+)/tables/([^/]+)", rn)
        if m:
            tables.add((m.group(1), m.group(2)))
        elif not re.fullmatch(rf"projects/{re.escape(src.project)}/datasets/[^/]+/routines/[^/]+", rn):
            iam.append(f"IAM-политика на незнакомом объекте BigQuery: {rn[:120]!r}")
    # Каждая существующая таблица проекта (перечень — по истории создания/удаления в Admin Activity за всю жизнь
    # проекта): фактическое право updateData не зависит от способа выдачи (SetIamPolicy, DCL GRANT из любого
    # проекта, наследование) — самопроверка на КАЖДОЙ, а не только на таблицах с известной политикой.
    policy_tables = set(tables)
    tables |= table_inventory(src.logs(
        f'{_logname(src.project, "activity")} AND timestamp>="{created}" AND protoPayload.serviceName="bigquery.googleapis.com" '
        f'AND (protoPayload.metadata.tableCreation:* OR protoPayload.metadata.tableDeletion:* OR '
        f'protoPayload.methodName:"InsertTable" OR protoPayload.methodName:"DeleteTable")'), src.project)
    from concurrent.futures import ThreadPoolExecutor
    ordered = sorted(tables)
    with ThreadPoolExecutor(max_workers=8) as pool:           # сотни независимых самопроверок; любой сбой — исключение
        verdicts = list(pool.map(lambda t: src.table_can_write(*t), ordered))
    for (ds, table), can in zip(ordered, verdicts):
        if can:
            iam.append(f"{ds}.{table}: updateData у идентичности AE")
    # WIF: какие SA получает job каждого workflow — тот же оценщик, что `wif_check --live`, на конфигурации,
    # восстановленной из Admin Activity (прав на чтение политик SA у AE нет и не нужно).
    wip = src.logs(f'{_logname(src.project, "activity")} AND timestamp>="{created}" AND '
                   f'protoPayload.serviceName="iam.googleapis.com" AND protoPayload.methodName:"WorkloadIdentityPool"')
    wif_doc, wif_broken = wif_from_logs(wip, state, uid_to_account, src.project, src.project_number())
    iam += wif_broken
    if wif_doc is not None and not wif_broken:
        from tools.autonomy import wif_check
        try:
            rows = wif_check.verify(wif_check.from_snapshot(wif_doc, "admin-activity"))
        except ValueError as e:                        # конфигурация вне модели оценщика — не PASS
            rows = [{"case": "model", "got": str(e)[:120], "status": "FAIL"}]
        bad = [f"{r['case']}→{r['got']}" for r in rows if r["status"] != "PASS"]
        if bad:
            iam.append(f"WIF: инвариант S1 нарушен ({', '.join(bad)[:200]})")
    total = sum(by_type.values())
    status = "FAIL" if total else "BLOCKED" if iam else "PASS"
    return {"status": status, "mutations": total if status != "BLOCKED" else None, "by_type": by_type,
            "iam_invariant": iam, "jobs_listed": len(jobs), "ae_jobs": len(ae_jobs), "admin_activity": len(activity),
            "data_access": len(access), "service_accounts_checked": len(sas), "tables_checked": len(tables),
            "table_policies": len(policy_tables),
            "iam_resources_reconstructed": len(state),
            "source": AUDIT_SOURCE}


def zero_mutations_proven(run: dict) -> tuple[bool, str]:
    """Машинно проверяемое «0 production-мутаций»: доверенный аудит PASS с начала прогона, покрывающий
    ВСЕ записанные вызовы агента. Нет доказательства, NOT_APPLICABLE, BLOCKED, устаревшее — не ноль."""
    ev = run.get("audit_evidence")
    if not isinstance(ev, dict):
        return False, "нет доверенного аудита production-мутаций"
    if run.get("audit_status") != "PASS" or ev.get("status") != "PASS":
        return False, f"аудит {run.get('audit_status')}, а не PASS"
    if run.get("production_mutations") != 0 or ev.get("mutations") != 0:
        return False, "зафиксированы production-мутации"
    if ev.get("since") != run.get("created_at"):
        return False, "окно аудита начинается не с создания прогона"
    if ev.get("source") != AUDIT_SOURCE:
        return False, f"источник аудита {ev.get('source')!r} не покрывает все классы мутаций"
    if ev.get("usage_count") != len(run.get("usage", [])):
        return False, "аудит не покрывает последний вызов агента"
    return True, "доверенный аудит: 0 production-мутаций за всё окно прогона"


def finite_cost(v) -> float | None:
    """Стоимость из диагностики недоверенного job'а: только конечное число (NaN/inf — нет данных)."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def count_mutations(project: str, token_command: str, since_iso: str, identities: list[str],
                    settle_from: str | None = None) -> dict:
    """Вернуть {"status": PASS|FAIL|BLOCKED, "mutations": int|None, ...}. Любой сбой источника — BLOCKED."""
    import secrets
    try:
        if not identities:
            raise RuntimeError("не задана идентичность AE")
        token = resolve_token(None, token_command, {})
        src = GcpAuditSource(project, token, refresh=lambda: resolve_token(None, token_command, {}))

        def watermark() -> str:
            label = f"autonomy-audit-wm-{secrets.token_hex(6)}"
            ReadOnlyBigQuery(project=project, token=token, label_purpose=label).query("SELECT 1 AS audit_watermark")
            return label
        return run_audit(src, since_iso, identities, watermark, settle_from)
    except Exception as e:  # noqa: BLE001 — сбой любого источника = ноль не доказан
        return {"status": "BLOCKED", "mutations": None, "source": AUDIT_SOURCE,
                "error": f"{type(e).__name__}: {str(e)[:300]}"}
