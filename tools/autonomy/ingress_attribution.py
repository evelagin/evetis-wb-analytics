"""F-18: attribution of mutations through a PUBLIC Cloud Run ingress.

A public service (allUsers/allAuthenticatedUsers → roles/run.invoker, or IAM invoker check disabled) can be invoked
by anyone, so IAM audit logs cannot say who caused a write. A public ingress is therefore neither proof of a
vulnerability by itself nor a harmless fact: for the audit window the audit must PROVE, from immutable sources, one
of two classes per service that was public at ANY time in the window:

  NO_TRAFFIC   the platform request log (run.googleapis.com/requests, written by Cloud Run for every request) has
               no request that could run inside the window, there is no security event in the window, the very same
               filter is proven to match real requests of this service (positive control).
  ATTRIBUTED   every platform request that could run inside the window is attributed by the service's security
               events (contract wbcomm.security.v1, services/wb-communications/SECURITY_AUDIT_EVENTS.md) joined by
               trace id — never by time proximity — and every security event in the window belongs to such a request.

Both classes also need: the spec proof (Admin Activity: request-based CPU, no minimum or manual instances, for every
applied spec of the service since creation — no code runs without a request) and no logging routing change from
before the window up to the audit. Anything else is BLOCKED.

What ATTRIBUTED proves: every write of the service — external (WB, Telegram) and internal (its Firestore/BigQuery
state) — followed the service's OWN authentication of the same request (scheduler secret, Telegram secret +
allow-list for WB writes, admin token). It does not prove who held the secret: that rests on the audited identity not
having those secrets (F-19 / D-19a), and on it not being able to forge log entries (FORBIDDEN_PROJECT_PERMISSIONS).

Time: a request can run up to 3600 s (Cloud Run maximum; the service timeout is not readable by the audit identity).
A request counts if [ts − latency, ts + latency] overlaps the window (receive- or completion-stamped — not relied on).
The window must be settled: now ≥ until + 3600 s + ingestion margin.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone

SCHEMA = "wbcomm.security.v1"
PLATFORM_MAX_REQUEST_TIMEOUT_S = 3600      # Cloud Run services: maximum request timeout 60 min
INGESTION_MARGIN_S = 300                   # platform log is written at completion and ingested with a delay
INGRESS_SETTLE_S = PLATFORM_MAX_REQUEST_TIMEOUT_S + INGESTION_MARGIN_S
POSITIVE_CONTROL_DAYS = 7                  # the request filter must match real traffic before the window
PUBLIC = ("allUsers", "allAuthenticatedUsers")
INVOKER_ROLE = "roles/run.invoker"
RUN_SERVICE = re.compile(r"^(?:projects|namespaces)/([^/]+)(?:/locations/([^/]+))?/services/([^/]+)$")
INVOKER_IAM_DISABLED = "run.googleapis.com/invoker-iam-disabled"

# ---- producer contract (services/wb-communications/app/utils/audit_events.py, 1.6.0) ----
EVENT_TYPES = {"request_start", "request_done", "auth_ok", "auth_denied", "authz_ok", "authz_denied",
               "mutation_attempt", "mutation_success", "mutation_failure"}
ALLOWED_KEYS = {"severity", "message", "security_schema", "event_type", "event_id", "ts", "trace_id", "alt_trace_id",
                "span_id", "correlation_id", "route", "method", "service", "revision", "auth_mechanism",
                "principal_class", "result", "reason", "mutation_id", "mutation_class", "target_system", "target_ref",
                "error_class", "auth_event_id", "authz_event_id", "status_code", "mutation_attempts", "duration_ms",
                "allowlist_configured", "seq"}
ROUTES = {"/poll", "/telegram-webhook", "/health", "/admin/test-openai", "/admin/test-wb", "/admin/test-telegram",
          "other"}
AUTH_MECHANISMS = {"scheduler_secret", "telegram_webhook_secret", "admin_token"}
AUTHZ_MECHANISM = "telegram_allowlist"
EXTERNAL_TARGETS = {"wb", "telegram"}
INTERNAL_TARGETS = {"firestore", "bigquery"}
MUTATION_RESULTS = {"success", "rejected", "error", "outcome_unknown"}
KNOWN_OUTCOMES = {"success", "rejected", "error"}
# Which authentication a mutating request on a route must have passed. Any other route cannot mutate.
ROUTE_AUTH = {"/poll": "scheduler_secret", "/telegram-webhook": "telegram_webhook_secret",
              "/admin/test-openai": "admin_token", "/admin/test-wb": "admin_token",
              "/admin/test-telegram": "admin_token"}
SAFE_VALUE = re.compile(r"[A-Za-z0-9_.:/@+=-]{0,160}")
HEX32 = re.compile(r"[0-9a-f]{32}")
HEX16 = re.compile(r"[0-9a-f]{16}")
SECRET_SHAPES = (re.compile(r"\d{6,}(?::|%3[Aa])[A-Za-z0-9_-]{30,}"),        # Telegram bot token
                 re.compile(r"eyJ[A-Za-z0-9._\-]{20,}"),                      # JWT
                 re.compile(r"(?i)bearer"), re.compile(r"(?i)secret-token"))
LOGGING_CONFIG_METHODS = ("Sink", "Exclusion", "Bucket", "Settings", "CmekSettings", "View", "DeleteLog")

_TS = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)")


def _ts(s: str) -> datetime:
    """RFC 3339 with 0–9 fraction digits and Z or ±hh:mm; anything else raises ValueError."""
    m = _TS.fullmatch(str(s or ""))
    if not m:
        raise ValueError(f"не RFC 3339: {str(s)[:40]!r}")
    frac = (m.group(2) or "").ljust(6, "0")[:6]
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return datetime.fromisoformat(f"{m.group(1)}.{frac}{tz}")


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _latency_s(v) -> float | None:
    if isinstance(v, str) and v.endswith("s"):
        try:
            x = float(v[:-1])
            return x if x >= 0 else None
        except ValueError:
            return None
    return None


def _hex(v, rx) -> bool:
    return isinstance(v, str) and rx.fullmatch(v) is not None


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ------------------------------------------------------------------------------ which services ---
def public_run_services(state: dict) -> dict[str, tuple[str, str | None, str]]:
    """{resource_name: (project, location, service)} of Cloud Run services whose roles/run.invoker is public in
    one reconstructed IAM `state`. Only this exact role is eligible; every other public binding stays a problem."""
    out = {}
    for (svc, rn), binds in state.items():
        if svc != "run.googleapis.com":
            continue
        if any(role == INVOKER_ROLE and m in PUBLIC for role, m in binds):
            m = RUN_SERVICE.match(rn)
            if m:
                out[rn] = (m.group(1), m.group(2), m.group(3))
    return out


def _spec_of(pp: dict) -> dict | None:
    req = pp.get("request") or {}
    svc = req.get("service")
    return svc if isinstance(svc, dict) else None


def _is_applied(e: dict) -> bool:
    pp = e.get("protoPayload") or {}
    req = pp.get("request") or {}
    return not (pp.get("status") or {}).get("code") and not req.get("validateOnly") and not req.get("dryRun")


def iam_disabled_services(spec_events: list[dict], until: datetime
                          ) -> tuple[dict[str, tuple[str, str | None, str]], list[str]]:
    """Services whose Cloud Run invoker IAM check was disabled (public with NO allUsers binding) in any applied spec
    up to `until`, and problems (an applied spec event that cannot be placed in time is never skipped silently)."""
    out, problems = {}, []
    for e in spec_events:
        if not _is_applied(e):
            continue
        try:
            if _ts(e["timestamp"]) > until:
                continue
        except (ValueError, KeyError):
            problems.append("событие спецификации Cloud Run без корректного времени — публичность не проверена")
            continue
        svc = _spec_of(e.get("protoPayload") or {}) or {}
        ann = ((svc.get("metadata") or {}).get("annotations")) or {}
        if str(ann.get(INVOKER_IAM_DISABLED, "")).lower() == "true" or svc.get("invokerIamDisabled") is True:
            m = RUN_SERVICE.match((e.get("protoPayload") or {}).get("resourceName", ""))
            if not m:
                problems.append("сервис с выключенной IAM-проверкой вызова не распознан")
                continue
            loc = m.group(2) or ((e.get("resource") or {}).get("labels") or {}).get("location")
            out[f"projects/{m.group(1)}/locations/{loc}/services/{m.group(3)}"] = (m.group(1), loc, m.group(3))
    return out, problems


# ----------------------------------------------------------------------------------- filters ---
def request_filter(project: str, service: str, start: datetime, end: datetime, location: str | None = None) -> str:
    loc = f' AND resource.labels.location="{location}"' if location else ""
    return (f'logName="projects/{project}/logs/run.googleapis.com%2Frequests" AND resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{service}"{loc} AND timestamp>="{_iso(start)}" '
            f'AND timestamp<="{_iso(end)}"')


def event_filter(project: str, service: str, start: datetime, end: datetime, location: str | None = None) -> str:
    loc = f' AND resource.labels.location="{location}"' if location else ""
    return (f'logName="projects/{project}/logs/run.googleapis.com%2Fstdout" AND resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{service}"{loc} AND jsonPayload.security_schema="{SCHEMA}" '
            f'AND timestamp>="{_iso(start)}" AND timestamp<="{_iso(end)}"')


def spec_filter(project: str, created: str, service: str | None = None) -> str:
    """Applied spec changes of one service (exact resource names, v1 and v2 forms) or of ALL services."""
    who = (f' AND (protoPayload.resourceName="namespaces/{project}/services/{service}" OR '
           f'protoPayload.resourceName=~"^projects/{project}/locations/[^/]+/services/{service}$")') if service else ""
    return (f'logName="projects/{project}/logs/cloudaudit.googleapis.com%2Factivity" AND '
            f'protoPayload.serviceName="run.googleapis.com"{who} AND timestamp>="{created}" AND '
            f'(protoPayload.methodName:"ReplaceService" OR protoPayload.methodName:"CreateService" OR '
            f'protoPayload.methodName:"UpdateService")')


def routing_change_filter(project: str, start: datetime, end: datetime) -> str:
    methods = " OR ".join(f'protoPayload.methodName:"{m}"' for m in LOGGING_CONFIG_METHODS)
    return (f'logName="projects/{project}/logs/cloudaudit.googleapis.com%2Factivity" AND '
            f'protoPayload.serviceName="logging.googleapis.com" AND ({methods}) AND '
            f'timestamp>="{_iso(start)}" AND timestamp<="{_iso(end)}"')


# -------------------------------------------------------------------------------- spec proof ---
_MANUAL_V1 = ("run.googleapis.com/scalingMode", "run.googleapis.com/manualInstanceCount")


def _spec_problems(svc: dict) -> list[str]:
    """Request-based CPU and no minimum/manual instances in ONE applied service spec (v1 or v2). Unknown → problem."""
    out = []
    if "spec" in svc or "metadata" in svc:                                   # Cloud Run Admin API v1 (Knative)
        svc_ann = (svc.get("metadata") or {}).get("annotations") or {}
        tmpl = (svc.get("spec") or {}).get("template")
        if not isinstance(tmpl, dict):
            return ["в спецификации v1 нет шаблона ревизии"]
        ann = ((tmpl.get("metadata") or {}).get("annotations")) or {}
        if str(ann.get("run.googleapis.com/cpu-throttling", "true")).lower() != "true":
            out.append("CPU выделяется вне запросов (cpu-throttling=false)")
        for a in (ann, svc_ann):
            for k in ("autoscaling.knative.dev/minScale", "run.googleapis.com/minScale"):
                if str(a.get(k, "0")) not in ("0", ""):
                    out.append(f"минимум экземпляров {k}={a.get(k)}")
            if str(a.get(_MANUAL_V1[0], "automatic")).lower() not in ("automatic", "") or a.get(_MANUAL_V1[1]):
                out.append("ручное масштабирование (экземпляры без запросов)")
    elif "template" in svc:                                                   # v2
        tmpl = svc.get("template") or {}
        for c in tmpl.get("containers") or []:
            res = c.get("resources")
            # proto3 JSON drops false: once `resources` is present, only an explicit cpuIdle=true is request-based
            if res and res.get("cpuIdle") is not True:
                out.append("CPU выделяется вне запросов (resources без cpuIdle=true)")
        for s in (tmpl.get("scaling") or {}, svc.get("scaling") or {}):
            try:
                if int(s.get("minInstanceCount") or 0) > 0:
                    out.append(f"минимум экземпляров {s.get('minInstanceCount')}")
                if int(s.get("manualInstanceCount") or 0) > 0 or \
                        str(s.get("scalingMode", "AUTOMATIC")).upper() not in ("AUTOMATIC", "SCALING_MODE_UNSPECIFIED", ""):
                    out.append("ручное масштабирование (экземпляры без запросов)")
            except (TypeError, ValueError):
                out.append("масштабирование не читается")
    else:
        out.append("спецификация сервиса не восстанавливается из события")
    return out


def spec_proof(spec_events: list[dict], start: datetime, end: datetime, project: str, location: str | None,
               service: str) -> list[str]:
    """EVERY applied spec of the exact service (project + location) from creation up to `end` must be request-based
    CPU without minimum/manual instances: older revisions stay invocable through traffic tags/splits, and a rollout
    that failed after the API accepted a clean spec leaves the previous revision serving."""
    if not location:
        return ["регион сервиса неизвестен — спецификацию нельзя привязать к сервису"]
    mine = []
    for e in spec_events:
        pp = e.get("protoPayload") or {}
        m = RUN_SERVICE.match(pp.get("resourceName", ""))
        loc = (m.group(2) if m else None) or ((e.get("resource") or {}).get("labels") or {}).get("location")
        if not m or m.group(3) != service or m.group(1) != project or loc != location:
            continue
        if _is_applied(e):
            mine.append(e)
    try:
        mine.sort(key=lambda e: _ts(e["timestamp"]))
    except (ValueError, KeyError):
        return ["событие спецификации без корректного времени"]
    history = [e for e in mine if _ts(e["timestamp"]) <= end]
    if not [e for e in history if _ts(e["timestamp"]) <= start]:
        return ["нет события Admin Activity со спецификацией сервиса до начала окна"]
    out = []
    for e in history:
        out += [f"{e['timestamp'][:19]}: {p}" for p in _spec_problems(_spec_of(e.get("protoPayload") or {}) or {})]
    return out


def control_problems(control: list[dict] | None, project: str, service: str) -> list[str]:
    """Positive control for NO_TRAFFIC: an EMPTY request list is evidence only if the very same filter is proven to
    match real, well-formed request logs of this service (days before the window)."""
    if control is None:
        return ["позитивный контроль фильтра не выполнен"]
    good = [r for r in control if (r.get("httpRequest") or {}).get("status") is not None
            and str(r.get("trace") or "").startswith(f"projects/{project}/traces/")
            and ((r.get("resource") or {}).get("labels") or {}).get("service_name") == service]
    if not good:
        return [f"фильтр журнала запросов не доказан: за {POSITIVE_CONTROL_DAYS} дн. до окна он не нашёл ни одного "
                f"запроса сервиса — пустой результат в окне ничего не доказывает"]
    return []


# ------------------------------------------------------------------------------ event schema ---
def _payload_problems(p: dict) -> list[str]:
    """Closed field set, safe values, and the REQUIRED fields of each event type (None never matches anything)."""
    out = []
    extra = set(p) - ALLOWED_KEYS
    if extra:
        out.append(f"поля вне контракта: {sorted(extra)[:5]}")
    for k, v in p.items():
        if isinstance(v, str) and k != "message":
            if v == "<invalid>" or not SAFE_VALUE.fullmatch(v):
                out.append(f"недопустимое значение поля {k}")
        if isinstance(v, str) and any(r.search(v) for r in SECRET_SHAPES):
            out.append(f"похоже на секрет в поле {k}")
    try:
        _ts(p.get("ts"))
    except ValueError:
        out.append("ts не RFC 3339")
    et, cid = p.get("event_type"), p.get("correlation_id")
    if not _hex(p.get("event_id"), HEX32):
        out.append("event_id не 32 hex")
    if cid == "none":
        if p.get("seq") != 0 or p.get("trace_id") is not None or p.get("route") != "none":
            out.append("событие вне запроса с полями запроса")
    else:
        if not _hex(cid, HEX32):
            out.append("correlation_id не 32 hex")
        if not _int(p.get("seq")) or p.get("seq") < 1:
            out.append("нет порядкового seq")
        if p.get("route") not in ROUTES:
            out.append("маршрут вне контракта")
    for k, rx in (("trace_id", HEX32), ("alt_trace_id", HEX32), ("span_id", HEX16)):
        if p.get(k) is not None and not _hex(p.get(k), rx):
            out.append(f"{k} не hex")
    if not isinstance(p.get("revision"), str) or not isinstance(p.get("service"), str):
        out.append("нет service/revision")
    if et in ("auth_ok", "auth_denied") and p.get("auth_mechanism") not in AUTH_MECHANISMS:
        out.append("неизвестный механизм аутентификации")
    if et in ("authz_ok", "authz_denied") and (p.get("auth_mechanism") != AUTHZ_MECHANISM
                                               or not isinstance(p.get("allowlist_configured"), bool)):
        out.append("событие allow-list вне контракта")
    if et and et.startswith("mutation"):
        if not _hex(p.get("mutation_id"), HEX32) or not isinstance(p.get("mutation_class"), str):
            out.append("мутация без mutation_id/mutation_class")
        if p.get("target_system") not in EXTERNAL_TARGETS | INTERNAL_TARGETS:
            out.append("неизвестная целевая система мутации")
        if et == "mutation_attempt":
            for k in ("auth_event_id", "authz_event_id"):
                if p.get(k) is not None and not _hex(p.get(k), HEX32):
                    out.append(f"{k} не 32 hex")
        elif p.get("result") not in MUTATION_RESULTS:
            out.append("исход мутации вне контракта")
    if et == "request_done" and (not _int(p.get("status_code")) or not _int(p.get("mutation_attempts"))
                                 or p.get("result") not in ("ok", "denied", "none")):
        out.append("request_done вне контракта")
    return out


def _complete_order(pl: list[dict]) -> str | None:
    """seq is exactly 1..n, request_start first, request_done last, one of each. None if fine."""
    seqs = [p.get("seq") for p in pl]
    if any(not _int(s) for s in seqs) or sorted(seqs) != list(range(1, len(pl) + 1)):
        return "порядок событий (seq) не 1..n"
    pl = sorted(pl, key=lambda p: p["seq"])
    starts = [p for p in pl if p["event_type"] == "request_start"]
    dones = [p for p in pl if p["event_type"] == "request_done"]
    if len(starts) != 1 or len(dones) != 1 or pl[0] is not starts[0] or pl[-1] is not dones[0]:
        return "нет ровно одной пары request_start/request_done в начале и конце"
    return None


def _non_mutating(pl: list[dict], unauthenticated: bool) -> bool:
    """No mutation event and request_done.mutation_attempts == 0; `unauthenticated` additionally requires that the
    request never passed authentication (trace-less pairing is allowed only for such requests)."""
    done = next((p for p in pl if p["event_type"] == "request_done"), {})
    if any(p["event_type"].startswith("mutation") for p in pl) or done.get("mutation_attempts") != 0:
        return False
    if unauthenticated:
        return (not any(p["event_type"] in ("auth_ok", "authz_ok") for p in pl)
                and done.get("result") in ("denied", "none"))
    return True


# -------------------------------------------------------------------------------- attribution ---
def evaluate(service: str, requests: list[dict], events: list[dict], spec_events: list[dict],
             since: datetime, until: datetime, now: datetime, project: str, control: list[dict] | None = None,
             location: str | None = None, routing_changes: list[dict] | None = None) -> dict:
    """Verdict for ONE public service. `requests` must cover [since - lookback, until + lookback], `events`
    [since - lookback, now]; `control` = the same request filter over the POSITIVE_CONTROL_DAYS before the lookback;
    `now` is sampled BEFORE the reads; `routing_changes` = logging configuration changes from before the window to
    now (must be empty)."""
    problems: list[str] = []
    if now < until + timedelta(seconds=INGRESS_SETTLE_S):
        problems.append(f"окно не устоялось: запросы, идущие сейчас, ещё без журнала — повторить аудит после "
                        f"{_iso(until + timedelta(seconds=INGRESS_SETTLE_S))}")
    if routing_changes is None:
        problems.append("история маршрутизации журналов не прочитана")
    elif routing_changes:
        problems.append(f"маршрутизация журналов менялась около окна ({len(routing_changes)} изм.) — полнота не доказана")
    lookback = timedelta(seconds=PLATFORM_MAX_REQUEST_TIMEOUT_S)

    def platform_trace(e: dict) -> str | None:
        t = e.get("trace") or ""
        tid = t.rsplit("/", 1)[-1] if t.startswith(f"projects/{project}/traces/") else None
        return tid if _hex(tid, HEX32) else None

    # 1. platform requests that could run inside the window
    in_window, by_trace = [], {}
    for r in requests:
        try:
            ts = _ts(r["timestamp"])
        except (ValueError, KeyError):
            problems.append("запрос платформы без корректного времени")
            continue
        lat = _latency_s((r.get("httpRequest") or {}).get("latency"))
        lat = timedelta(seconds=lat if lat is not None else PLATFORM_MAX_REQUEST_TIMEOUT_S)
        if ts - lat <= until and ts + lat >= since:
            in_window.append(r)
        t = platform_trace(r)
        if t:
            by_trace.setdefault(t, []).append(r)

    # 2. security events: schema, global integrity, indexing
    valid: list[dict] = []
    in_win_ids: set[str] = set()
    for e in events:
        p = e.get("jsonPayload") or {}
        if p.get("security_schema") != SCHEMA or p.get("event_type") not in EVENT_TYPES:
            problems.append("событие безопасности вне контракта")
            continue
        bad = _payload_problems(p)
        problems += [f"{p.get('event_type')}: {b}" for b in bad]
        try:
            et_ts = _ts(e["timestamp"])
        except (ValueError, KeyError):
            problems.append("событие без корректного времени")
            continue
        pt = platform_trace(e)
        if pt is not None and pt != p.get("trace_id"):
            problems.append(f"{p.get('event_type')}: трасса записи ≠ trace_id события")
            continue
        if bad:
            continue
        valid.append(p)
        if since <= et_ts <= until:
            in_win_ids.add(p["event_id"])
    dup_ids = [i for i, n in Counter(p["event_id"] for p in valid).items() if n > 1]
    if dup_ids:
        problems.append(f"повтор event_id ({len(dup_ids)})")
    by_cid: dict[str, list[dict]] = {}
    by_cand: dict[str, set[str]] = {}
    for p in valid:
        if p["correlation_id"] == "none":
            continue
        by_cid.setdefault(p["correlation_id"], []).append(p)
        for c in {p.get("trace_id"), p.get("alt_trace_id")} - {None}:
            by_cand.setdefault(c, set()).add(p["correlation_id"])
    for cid, pl in by_cid.items():
        traces = {(p.get("trace_id"), p.get("alt_trace_id")) for p in pl}
        if len(traces) != 1:
            problems.append(f"correlation_id {cid[:8]}… на разных трассах")

    # 3. attribute every in-window platform request
    validated: set[str] = set()
    owner: dict[str, str] = {}
    unmatched: list[dict] = []
    mutations = 0
    done_traces: set[str] = set()
    for r in in_window:
        t = platform_trace(r)
        rid = f"{str(r.get('timestamp', '?'))[:19]} {((r.get('httpRequest') or {}).get('requestMethod') or '?')}"
        if not t:
            problems.append(f"запрос {rid} без трассы — не атрибутируется")
            continue
        if t in done_traces:
            continue
        done_traces.add(t)
        reqs_t, cids = by_trace.get(t, []), sorted(by_cand.get(t, set()))
        if not cids:
            unmatched.extend(reqs_t)          # may still pair with a trace-less non-mutating request (4.)
            continue
        claimed = [c for c in cids if owner.setdefault(c, t) != t]
        if claimed:
            problems.append(f"запрос {rid}: correlation_id уже принадлежит другому запросу")
            continue
        groups = [by_cid[c] for c in cids]
        if len(reqs_t) > 1 or len(cids) > 1:
            # a client can send any trace header: a shared trace is tolerated ONLY for complete non-mutating requests,
            # one per platform request, statuses and revisions matching
            if (len(cids) != len(reqs_t) or any(_complete_order(g) or not _non_mutating(g, False) for g in groups)
                    or Counter((q.get("httpRequest") or {}).get("status") for q in reqs_t)
                    != Counter(next(p for p in g if p["event_type"] == "request_done")["status_code"] for g in groups)
                    or Counter(((q.get("resource") or {}).get("labels") or {}).get("revision_name") for q in reqs_t)
                    != Counter(g[0].get("revision") for g in groups)):
                problems.append(f"трасса {t[:8]}… у {len(reqs_t)} запросов / {len(cids)} событий — неоднозначно")
                continue
            validated.update(p["event_id"] for g in groups for p in g)
            continue
        pl = groups[0]
        order = _complete_order(pl)
        if order:
            problems.append(f"запрос {rid}: {order}")
            continue
        pl = sorted(pl, key=lambda p: p["seq"])
        validated.update(p["event_id"] for p in pl)
        start, done = pl[0], pl[-1]
        rev = ((r.get("resource") or {}).get("labels") or {}).get("revision_name")
        if any(p.get("revision") != rev for p in pl):
            problems.append(f"запрос {rid}: ревизия событий не совпадает с ревизией запроса")
        if done.get("status_code") != (r.get("httpRequest") or {}).get("status"):
            problems.append(f"запрос {rid}: статус request_done {done.get('status_code')} ≠ платформы")
        route = start.get("route")
        by_id = {p["event_id"]: p for p in pl}
        attempts = [p for p in pl if p["event_type"] == "mutation_attempt"]
        outcomes: dict[str, list[dict]] = {}
        for p in pl:
            if p["event_type"] in ("mutation_success", "mutation_failure"):
                outcomes.setdefault(p["mutation_id"], []).append(p)
        if done.get("mutation_attempts") != len(attempts):
            problems.append(f"запрос {rid}: request_done.mutation_attempts ≠ числу mutation_attempt")
        if attempts and ROUTE_AUTH.get(route) is None:
            problems.append(f"запрос {rid}: мутация на маршруте {route}, где аутентификации нет")
        for a in attempts:
            mutations += 1
            kind, system = a.get("mutation_class"), a.get("target_system")
            auth = by_id.get(a.get("auth_event_id")) if a.get("auth_event_id") else None
            if (auth is None or auth["event_type"] != "auth_ok" or auth["seq"] >= a["seq"]
                    or auth.get("auth_mechanism") != ROUTE_AUTH.get(route)):
                problems.append(f"запрос {rid}: мутация {kind} без предшествующего auth_ok механизма маршрута {route}")
            if system == "wb" and route == "/telegram-webhook":
                z = by_id.get(a.get("authz_event_id")) if a.get("authz_event_id") else None
                if (z is None or z["event_type"] != "authz_ok" or z.get("allowlist_configured") is not True
                        or z["seq"] >= a["seq"]):
                    problems.append(f"запрос {rid}: запись в WB из Telegram без authz_ok настроенного allow-list")
            outs = outcomes.get(a["mutation_id"], [])
            if len(outs) != 1:
                problems.append(f"запрос {rid}: у мутации {kind} {len(outs)} исходов вместо 1")
            elif outs[0]["seq"] <= a["seq"]:
                problems.append(f"запрос {rid}: исход мутации {kind} раньше попытки")
            elif system in EXTERNAL_TARGETS and outs[0].get("result") not in KNOWN_OUTCOMES:
                problems.append(f"запрос {rid}: исход внешней мутации {kind} неизвестен ({outs[0].get('result')})")
        if set(outcomes) - {a["mutation_id"] for a in attempts}:
            problems.append(f"запрос {rid}: исход без mutation_attempt")

    # 4. platform requests with no events on their trace: only pairable with trace-less, complete, NON-mutating
    #    requests (a client with 3+ conflicting trace headers gets trace_id=null) — same revision and status
    traceless = [g for cid, g in by_cid.items() if cid not in owner and all(p.get("trace_id") is None for p in g)
                 and any(p["event_id"] in in_win_ids for p in g)]
    if unmatched:
        eligible = [g for g in traceless if not _complete_order(g) and _non_mutating(g, True)]
        want = Counter((((q.get("resource") or {}).get("labels") or {}).get("revision_name"),
                        (q.get("httpRequest") or {}).get("status")) for q in unmatched)
        have = Counter((g[0].get("revision"), next(p for p in g if p["event_type"] == "request_done")["status_code"])
                       for g in eligible)
        if want == have and len(eligible) == len(traceless):
            validated.update(p["event_id"] for g in eligible for p in g)
        else:
            problems.append(f"{len(unmatched)} запросов без событий — инструментирование не доказано")

    # 5. every security event inside the window belongs to an attributed request (by event, not by cid)
    stray = in_win_ids - validated
    if stray:
        problems.append(f"{len(stray)} событий в окне вне атрибутированных запросов")

    problems += [f"spec: {p}" for p in spec_proof(spec_events, since - lookback, until, project, location, service)]
    if not in_window:
        klass = "NO_TRAFFIC"
        problems += [f"NO_TRAFFIC: {p}" for p in control_problems(control, project, service)]
    else:
        klass = "ATTRIBUTED"
    return {"service": service, "class": klass, "status": "BLOCKED" if problems else "PROVEN",
            "requests_in_window": len(in_window), "mutation_attempts": mutations,
            "events_in_window": len(in_win_ids), "problems": problems[:50],
            "window": {"since": _iso(since), "until": _iso(until), "lookback_s": PLATFORM_MAX_REQUEST_TIMEOUT_S,
                       "settle_s": INGRESS_SETTLE_S}}


def attribute(src, services: dict[str, tuple[str, str | None, str]], since: datetime, until: datetime,
              created: str) -> tuple[dict[str, dict], list[str]]:
    """Evaluate every service that was public at any time in the window. Returns ({resource_name: verdict},
    problems). A public binding is exempted from the policy problem ONLY when its verdict is PROVEN."""
    now = src.now()                                          # sampled BEFORE the reads (settle rule)
    verdicts, problems = {}, []
    lookback = timedelta(seconds=PLATFORM_MAX_REQUEST_TIMEOUT_S)
    control_start = since - lookback - timedelta(days=POSITIVE_CONTROL_DAYS)
    routing = src.logs(routing_change_filter(src.project, control_start, now)) if services else []
    for rn, (project, loc, service) in sorted(services.items()):
        if project not in (src.project, src.project_number()):
            problems.append(f"публичный сервис чужого проекта {rn[:80]} — вне модели")
            continue
        reqs = src.logs(request_filter(src.project, service, since - lookback, until + lookback, loc))
        evs = src.logs(event_filter(src.project, service, since - lookback, now, loc))
        spec = src.logs(spec_filter(src.project, created, service))
        page = getattr(src, "logs_page", None)
        cflt = request_filter(src.project, service, control_start, since - lookback, loc)
        control = page(cflt) if page else src.logs(cflt)
        v = evaluate(service, reqs, evs, spec, since, until, now, src.project, control, loc, routing)
        verdicts[rn] = v
        if v["status"] != "PROVEN":
            problems.append(f"F-18 {service}: публичный вход не атрибутирован ({v['class']}): "
                            + "; ".join(v["problems"][:5]))
    return verdicts, problems
