"""Аудит production-мутаций со стороны BigQuery. Сам аудит — только чтение.

Доказательство «AE не менял production» берётся не из слов агента, а из источников GCP (см. run_audit):
все задания BigQuery проекта во всех регионах (jobs.list), журналы Admin Activity и Data Access с цепочкой
делегирования и живая самопроверка IAM идентичности AE. Для AE v1 число мутаций обязано быть 0;
недоказанное — не ноль.
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.bq_readonly import ReadOnlyBigQuery, resolve_token  # noqa: E402

# Версия источника доказательства. Доказательство другой версии (в т.ч. прежнего INFORMATION_SCHEMA по одному
# региону EU, который не видел LOAD/COPY/EXTRACT, SELECT с записью в таблицу и задания вне EU) нулём не считается.
AUDIT_SOURCE = "jobs_list+audit_logs+iam_selftest+iam_history/v4"

API = "https://www.googleapis.com"
CRM = "https://cloudresourcemanager.googleapis.com/v1"
LOGGING = "https://logging.googleapis.com/v2"
IAM = "https://iam.googleapis.com/v1"
ANON_DATASET = re.compile(r"^_[0-9a-f]{40}$")
FEDERATED = ("principal://", "principalSet://")
SETTLE_SECONDS = 300            # от READY_FOR_PR (после ревьюера, т.е. после всех job'ов с GCP) до чтения журналов
WATERMARK_TRIES, WATERMARK_SLEEP = 12, 10
MAX_PAGES = 500
ADMIN_ACTIVITY_RETENTION_DAYS = 400   # бакет _Required: не отключается, не исключается, 400 дней
JOBS_RETENTION_DAYS = 180             # INFORMATION_SCHEMA.JOBS: история DCL GRANT доказуема только в этом окне
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

    def __init__(self, project: str, token: str, http=None, sleep=None, now=None, bq=None):
        import time
        from datetime import datetime, timezone
        self.project, self.token = project, token
        self.http = http or self._http
        self.sleep = sleep or time.sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.bq = bq or (lambda sql, location: ReadOnlyBigQuery(project=project, token=token, location=location,
                                                                label_purpose="autonomy-audit").query(sql))

    def _http(self, method: str, url: str, body: dict | None = None) -> dict:
        import json
        import urllib.request
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")

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

    def project_number(self) -> str:
        return str(self.http("GET", f"{CRM}/projects/{self.project}")["projectNumber"])

    def datasets(self) -> dict[str, dict]:
        r = self.http("GET", f"{API}/bigquery/v2/projects/{self.project}/datasets?all=true&maxResults=1000")
        if r.get("nextPageToken") or r.get("unreachable"):
            raise RuntimeError("список датасетов усечён или недоступны регионы")
        ids = [d["datasetReference"]["datasetId"] for d in r.get("datasets", [])]
        return {i: self.http("GET", f"{API}/bigquery/v2/projects/{self.project}/datasets/{i}") for i in ids}

    def grant_jobs(self, location: str, since_iso: str) -> int:
        """Успешные DCL GRANT в регионе за окно (INFORMATION_SCHEMA; GRANT меняет IAM объекта)."""
        if not re.fullmatch(r"[A-Za-z0-9-]{2,30}", location):
            raise RuntimeError(f"неожиданная локация {location!r}")
        sql = (f"SELECT COUNT(*) AS n FROM `{self.project}.region-{location.lower()}.INFORMATION_SCHEMA."
               f"JOBS_BY_PROJECT` WHERE creation_time >= TIMESTAMP('{since_iso}') AND statement_type = 'GRANT' "
               f"AND error_result IS NULL")
        return int(self.bq(sql, location)[0]["n"])


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


def policy_problems(state: dict, identities: list[str]) -> list[str]:
    """Привязки, которые дают AE (или кому угодно) доступ, не видимый самопроверкой AE и журналами аудита:
    публичный доступ, группы/домены (членство не проверить), AE на отдельном ресурсе, прямой федеративный
    принципал вне SA. Публичную точку входа журналы аудита не видят вовсе — поэтому это отказ, а не ноль."""
    mine = {f"serviceAccount:{i}" for i in identities}
    out = []
    for (svc, rn), binds in sorted(state.items()):
        project_policy = svc == "cloudresourcemanager.googleapis.com"
        for role, m in sorted(binds):
            if m.startswith("deleted:"):
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
         датасетах, нет updateData ни на одной таблице с собственной IAM-политикой, и в окне хранения
         INFORMATION_SCHEMA не было DCL GRANT.
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
    iam += broken + policy_problems(state, identities)
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
    for ds, table in sorted(tables):
        if src.table_can_write(ds, table):
            iam.append(f"{ds}.{table}: updateData у идентичности AE (IAM-политика таблицы)")
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
    # DCL GRANT меняет IAM объекта; документация не гарантирует его запись как SetIamPolicy — проверяем сами задания.
    if age > timedelta(days=JOBS_RETENTION_DAYS):
        iam.append("проект старше хранения INFORMATION_SCHEMA.JOBS — отсутствие DCL GRANT не доказуемо")
    for loc in sorted({"EU"} | {m.get("location") or "EU" for m in datasets.values()}):
        n = src.grant_jobs(loc, created)
        if n:
            iam.append(f"в регионе {loc} выполнялись DCL GRANT ({n}) — права объектов не доказуемы")
    total = sum(by_type.values())
    status = "FAIL" if total else "BLOCKED" if iam else "PASS"
    return {"status": status, "mutations": total if status != "BLOCKED" else None, "by_type": by_type,
            "iam_invariant": iam, "jobs_listed": len(jobs), "ae_jobs": len(ae_jobs), "admin_activity": len(activity),
            "data_access": len(access), "service_accounts_checked": len(sas), "table_policies_checked": len(tables),
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
        src = GcpAuditSource(project, token)

        def watermark() -> str:
            label = f"autonomy-audit-wm-{secrets.token_hex(6)}"
            ReadOnlyBigQuery(project=project, token=token, label_purpose=label).query("SELECT 1 AS audit_watermark")
            return label
        return run_audit(src, since_iso, identities, watermark, settle_from)
    except Exception as e:  # noqa: BLE001 — сбой любого источника = ноль не доказан
        return {"status": "BLOCKED", "mutations": None, "source": AUDIT_SOURCE,
                "error": f"{type(e).__name__}: {str(e)[:300]}"}
