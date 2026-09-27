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
AUDIT_SOURCE = "jobs_list+audit_logs+iam_selftest/v3"

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
    return ai.get("principalEmail") or ai.get("principalSubject") or "", ai.get("serviceAccountDelegationInfo") or []


def _ae_principal(p: str, identities: list[str]) -> bool:
    return p in identities or p.startswith(FEDERATED)


def delegated_from_ae(entry: dict, identities: list[str]) -> bool:
    """Цепочка делегирования начинается у AE (SA AE или федеративный принципал), а действует ДРУГАЯ идентичность."""
    eff, chain = _auth(entry)
    if _ae_principal(eff, identities):
        return False
    for d in chain:
        first = (d.get("firstPartyPrincipal") or {}).get("principalEmail") or ""
        if first in identities or (d.get("principalSubject") or "").startswith(FEDERATED):
            return True
    return False


def data_access_violation(entry: dict, identities: list[str]) -> str | None:
    """Запись Data Access, относящаяся к AE: None — чтение; иначе класс нарушения (незнакомое — нарушение)."""
    pp = entry.get("protoPayload") or {}
    svc, method = pp.get("serviceName", ""), str(pp.get("methodName"))
    eff, _ = _auth(entry)
    if delegated_from_ae(entry, identities):
        return f"DELEGATED:{svc}:{eff}"             # AE получил и использовал чужую идентичность
    if not _ae_principal(eff, identities):
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
                "exclusions": self.http("GET", f"{LOGGING}/projects/{self.project}/exclusions").get("exclusions", [])}

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


def routing_problems(routing: dict) -> list[str]:
    """Data Access обязан доходить до _Default без исключений — иначе его отсутствие ничего не доказывает."""
    out = []
    sink = routing.get("sink") or {}
    if sink.get("disabled"):
        out.append("sink _Default выключен")
    if "data_access" in (sink.get("filter") or ""):
        out.append("фильтр _Default касается data_access")
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
    for e in activity:
        add("ADMIN:" + str((e.get("protoPayload") or {}).get("methodName")))
    access = src.logs(f'{_logname(src.project, "data_access")} AND {window}')
    for e in access:
        v = data_access_violation(e, identities)
        if v:
            add(v)
    iam: list[str] = routing_problems(src.routing())
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
    for e in src.logs(f'{_logname(src.project, "activity")} AND timestamp>="{created}" AND '
                      f'protoPayload.methodName="google.iam.admin.v1.CreateServiceAccount"'):
        pp = e.get("protoPayload") or {}
        email = (pp.get("response") or {}).get("email")
        if email:
            sas.add(email)
        elif not (pp.get("status") or {}).get("code"):
            iam.append("событие создания SA без email — перечень SA неполон")
    for email in sorted(sas):
        got = src.sa_granted(email, FORBIDDEN_SA_PERMISSIONS)
        if got:
            iam.append(f"{email}: {got} у идентичности AE")
    # Политики уровня таблиц дают запись в обход ACL датасета и не видны в списке датасетов. Каждая такая
    # политика за всю жизнь проекта есть в Admin Activity; на каждой ещё существующей таблице — самопроверка.
    # Политики процедур данных не пишут (процедура исполняется с правами вызывающего), их изменения — Admin Activity.
    fine = src.logs(f'{_logname(src.project, "activity")} AND timestamp>="{created}" AND '
                    f'protoPayload.serviceName="bigquery.googleapis.com" AND protoPayload.methodName:"SetIamPolicy"')
    tables: set[tuple[str, str]] = set()
    for e in fine:
        rn = (e.get("protoPayload") or {}).get("resourceName", "")
        m = re.fullmatch(rf"projects/{re.escape(src.project)}/datasets/([^/]+)/tables/([^/]+)", rn)
        if m:
            tables.add((m.group(1), m.group(2)))
        elif not re.fullmatch(rf"projects/{re.escape(src.project)}/datasets/[^/]+/routines/[^/]+", rn):
            iam.append(f"IAM-политика на незнакомом объекте BigQuery: {rn[:120]!r}")
    for ds, table in sorted(tables):
        if src.table_can_write(ds, table):
            iam.append(f"{ds}.{table}: updateData у идентичности AE (IAM-политика таблицы)")
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
