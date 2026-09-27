"""Аудит production-мутаций со стороны BigQuery. Сам аудит — только чтение.

Доказательство «AE не менял production» берётся не из слов агента, а из трёх источников GCP (см. run_audit):
все задания BigQuery проекта во всех регионах (jobs.list), журнал Admin Activity и живая самопроверка IAM
идентичности AE. Для AE v1 число мутаций обязано быть 0; недоказанное — не ноль.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.bq_readonly import ReadOnlyBigQuery, resolve_token  # noqa: E402

# Версия источника доказательства. Доказательство другой версии (в т.ч. прежнего INFORMATION_SCHEMA по одному
# региону EU, который не видел LOAD/COPY/EXTRACT, SELECT с записью в таблицу и задания вне EU) нулём не считается.
AUDIT_SOURCE = "jobs_list+admin_activity+iam_selftest/v2"

API = "https://www.googleapis.com"
CRM = "https://cloudresourcemanager.googleapis.com/v1"
LOGGING = "https://logging.googleapis.com/v2"
ANON_DATASET = re.compile(r"^_[0-9a-f]{40}$")
FEDERATED = ("principal://", "principalSet://")
SETTLE_SECONDS = 300            # от READY_FOR_PR (после ревьюера, т.е. после всех job'ов с GCP) до чтения журналов
WATERMARK_TRIES, WATERMARK_SLEEP = 12, 10
MAX_PAGES = 500
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
    "bigquery.reservations.delete", "storage.objects.create", "storage.objects.delete", "storage.buckets.create",
    "resourcemanager.projects.setIamPolicy", "iam.serviceAccounts.actAs", "iam.serviceAccounts.getAccessToken",
]
READ_ONLY_ACL = {"READER", "roles/bigquery.dataViewer", "roles/bigquery.metadataViewer"}
PROJECT_GROUPS = {"projectReaders", "projectWriters", "projectOwners"}


def is_ae_job(job: dict, identities: list[str]) -> bool:
    email = job.get("user_email") or ""
    purpose = ((job.get("configuration") or {}).get("labels") or {}).get("purpose", "")
    return email in identities or email.startswith(FEDERATED) or purpose.startswith("autonomy")


def job_mutation(job: dict, project: str) -> str | None:
    """None — задание только читает; иначе класс изменения. Незнакомое — изменение (fail closed)."""
    cfg = job.get("configuration") or {}
    jt = cfg.get("jobType")
    st = ((job.get("statistics") or {}).get("query") or {}).get("statementType")
    status = job.get("status") or {}
    if jt == "QUERY" and st == "SELECT":
        dest = (cfg.get("query") or {}).get("destinationTable")
        if not dest or (dest.get("projectId") == project and ANON_DATASET.match(dest.get("datasetId") or "")):
            return None                  # результат в собственном анонимном кэше, не в production
        return f"QUERY:SELECT->{dest.get('datasetId')}.{dest.get('tableId')}"
    if jt == "QUERY" and st is None and status.get("state") == "DONE" and status.get("errorResult"):
        return None                      # не разобран — не исполнялся (одиночное задание атомарно)
    return f"{jt}:{st}"


def acl_problems(ds: str, access: list[dict], sa: str) -> list[str]:
    """Записи ACL датасета, которые могут дать идентичности AE запись. Членство в группе/домене не
    проверяемо — роль выше чтения у такой записи уже отказ."""
    out = []
    for a in access:
        role = a.get("role", "")
        if role in READ_ONLY_ACL or "view" in a or "routine" in a or "dataset" in a:
            continue
        if a.get("userByEmail") == sa or a.get("iamMember") == f"serviceAccount:{sa}":
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


class GcpAuditSource:
    """Только чтение: jobs.list (все регионы), Admin Activity, testIamPermissions/ACL. Любая ошибка — исключение."""

    def __init__(self, project: str, token: str, http=None, sleep=None, now=None):
        import time
        from datetime import datetime, timezone
        self.project, self.token = project, token
        self.http = http or self._http
        self.sleep = sleep or time.sleep
        self.now = now or (lambda: datetime.now(timezone.utc))

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
            out += r.get("jobs", [])
            token = r.get("nextPageToken")
            if not token:
                return out
        raise RuntimeError("jobs.list: превышен лимит страниц — частичный список не доказательство")

    def activity(self, flt: str) -> list[dict]:
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
        raise RuntimeError("Admin Activity: превышен лимит страниц")

    def granted(self, perms: list[str]) -> list[str]:
        r = self.http("POST", f"{CRM}/projects/{self.project}:testIamPermissions", {"permissions": perms})
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

    def datasets(self) -> dict[str, list[dict]]:
        r = self.http("GET", f"{API}/bigquery/v2/projects/{self.project}/datasets?all=true&maxResults=1000")
        if r.get("nextPageToken"):
            raise RuntimeError("список датасетов усечён")
        ids = [d["datasetReference"]["datasetId"] for d in r.get("datasets", [])]
        return {i: self.http("GET", f"{API}/bigquery/v2/projects/{self.project}/datasets/{i}").get("access", [])
                for i in ids}


def _ts(s: str):
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def run_audit(src: GcpAuditSource, since_iso: str, identities: list[str], watermark, settle_from: str | None = None
              ) -> dict:
    """Решение аудита. PASS только если ВСЕ три источника прочитаны полностью и чисты:
      1) jobs.list без location — задания ВСЕХ регионов и типов (LOAD/COPY/EXTRACT, SELECT с записью, SCRIPT);
         полнота окна доказана зондом-отметкой, созданным после всех недоверенных job'ов;
      2) Admin Activity (не отключается и не исключается) — любые изменения метаданных/IAM идентичностью AE
         или прямым федеративным принципалом, в любом сервисе, без заданий BigQuery;
      3) самопроверка IAM: у AE нет прав записи на проекте (testIamPermissions, вкл. организацию/группы), ни одной
         записи ACL выше чтения на видимых датасетах (датасет с любым грантом AE виден ему), и нет updateData ни на
         одной таблице, которой за жизнь проекта ставили собственную IAM-политику (Admin Activity), — значит записи
         вне заданий (insertAll, Storage Write) и из заданий чужих проектов невозможны.
    Нарушение инварианта IAM — BLOCKED (ноль не доказан), найденная мутация — FAIL."""
    from datetime import timedelta
    if settle_from:
        wait = (_ts(settle_from) + timedelta(seconds=SETTLE_SECONDS) - src.now()).total_seconds()
        if wait > 0:
            src.sleep(min(wait, SETTLE_SECONDS))
    since = _ts(since_iso)
    since_ms = int(since.timestamp() * 1000)
    label = watermark()                                    # SELECT с уникальной меткой: отметка «журнал догнал»
    for attempt in range(WATERMARK_TRIES):
        jobs = src.jobs(since_ms)
        if any(((j.get("configuration") or {}).get("labels") or {}).get("purpose") == label for j in jobs):
            break
        if attempt == WATERMARK_TRIES - 1:
            return {"status": "BLOCKED", "mutations": None, "source": AUDIT_SOURCE,
                    "error": "jobs.list не показал зонд аудита — полнота окна не доказана"}
        src.sleep(WATERMARK_SLEEP)
    by_type: dict[str, int] = {}
    ae_jobs = [j for j in jobs if is_ae_job(j, identities)]
    for j in ae_jobs:
        m = job_mutation(j, src.project)
        if m:
            by_type[m] = by_type.get(m, 0) + 1
    who = " OR ".join(f'protoPayload.authenticationInfo.principalEmail="{i}"' for i in identities if '"' not in i)
    fed = " OR ".join(f'protoPayload.authenticationInfo.principalSubject:"{f}"' for f in FEDERATED)
    activity = src.activity(f'logName="projects/{src.project}/logs/cloudaudit.googleapis.com%2Factivity" AND '
                            f'timestamp>="{since_iso}" AND ({" OR ".join(x for x in (who, fed) if x)})')
    for e in activity:
        m = "ADMIN:" + str((e.get("protoPayload") or {}).get("methodName"))
        by_type[m] = by_type.get(m, 0) + 1
    iam: list[str] = []
    granted = src.granted(FORBIDDEN_PROJECT_PERMISSIONS)
    if granted:
        iam.append(f"права записи на проекте: {granted}")
    for ds, access in src.datasets().items():
        iam += acl_problems(ds, access, identities[0] if identities else "")
    created = src.project_created()
    if src.now() - _ts(created) > timedelta(days=ADMIN_ACTIVITY_RETENTION_DAYS):
        iam.append("проект старше хранения Admin Activity — отсутствие IAM-политик таблиц не доказуемо")
    # Политики уровня таблиц дают запись в обход ACL датасета и не видны в списке датасетов. Каждая такая
    # политика за всю жизнь проекта есть в Admin Activity; на каждой ещё существующей таблице — самопроверка.
    # Политики процедур данных не пишут (процедура исполняется с правами вызывающего), их изменения — Admin Activity.
    fine = src.activity(f'logName="projects/{src.project}/logs/cloudaudit.googleapis.com%2Factivity" AND '
                        f'timestamp>="{created}" AND protoPayload.serviceName="bigquery.googleapis.com" AND '
                        f'protoPayload.methodName:"SetIamPolicy"')
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
    total = sum(by_type.values())
    status = "FAIL" if total else "BLOCKED" if iam else "PASS"
    return {"status": status, "mutations": total if status != "BLOCKED" else None, "by_type": by_type,
            "iam_invariant": iam, "jobs_listed": len(jobs), "ae_jobs": len(ae_jobs), "admin_activity": len(activity),
            "table_policies_checked": len(tables),
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
