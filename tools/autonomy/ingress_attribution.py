"""F-18: attribution of mutations through a PUBLIC Cloud Run ingress.

A public service (allUsers/allAuthenticatedUsers → roles/run.invoker, or IAM invoker check disabled) can be invoked
by anyone, so IAM audit logs cannot say who caused a write. A public ingress is therefore neither proof of a
vulnerability by itself nor a harmless fact: for the audit window the audit must PROVE, from immutable sources, one
of two classes per service that was public at ANY time in the window:

  NO_TRAFFIC   the platform request log (run.googleapis.com/requests, written by Cloud Run for every request) has
               no request that could run inside the window, there is no security event in the window, the very same
               filter is proven to match real requests of this service (positive control), and log routing did not
               change around the window.
  ATTRIBUTED   every platform request that could run inside the window is attributed by the service's security
               events (contract wbcomm.security.v1, services/wb-communications/SECURITY_AUDIT_EVENTS.md) joined by
               trace id — never by time proximity.

Both classes also need the spec proof: Admin Activity shows request-based CPU and no minimum instances for the whole
window (no code runs without a request, so no mutation escapes the per-request view). Anything else is BLOCKED.

What ATTRIBUTED proves: every external mutation followed the service's OWN authentication of the same request
(scheduler secret, Telegram secret + allow-list, admin token). It does not prove who held the secret: security
rests on the audited identity not having those secrets (see F-19 / D-19a).

Time: a request can run up to 3600 s (Cloud Run maximum; the service timeout is not readable by the audit
identity). Whether the request log timestamp is the receive or the completion time is not relied on: a request
counts if [ts − latency, ts + latency] overlaps the window. The window must be settled (now ≥ until + 3600 s +
ingestion margin): a request still in flight has no platform log yet.
"""
from __future__ import annotations

import re
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

EVENT_TYPES = {"request_start", "request_done", "auth_ok", "auth_denied", "authz_ok", "authz_denied",
               "mutation_attempt", "mutation_success", "mutation_failure"}
ALLOWED_KEYS = {"severity", "message", "security_schema", "event_type", "event_id", "ts", "trace_id", "alt_trace_id",
                "span_id", "correlation_id", "route", "method", "service", "revision", "auth_mechanism",
                "principal_class", "result", "reason", "mutation_id", "mutation_class", "target_system", "target_ref",
                "error_class", "auth_event_id", "authz_event_id", "status_code", "mutation_attempts", "duration_ms",
                "allowlist_configured", "seq"}
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/@+=-]{0,160}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
SECRET_SHAPES = (re.compile(r"\d{6,}(?::|%3[Aa])[A-Za-z0-9_-]{30,}"),        # Telegram bot token
                 re.compile(r"eyJ[A-Za-z0-9._\-]{20,}"),                      # JWT
                 re.compile(r"(?i)bearer"), re.compile(r"(?i)secret-token"))
KNOWN_OUTCOMES = {"success", "rejected", "error"}
# Which authentication a mutating request on a route must have passed. Any other route cannot mutate.
ROUTE_AUTH = {"/poll": "scheduler_secret", "/telegram-webhook": "telegram_webhook_secret",
              "/admin/test-openai": "admin_token", "/admin/test-wb": "admin_token",
              "/admin/test-telegram": "admin_token"}
LOGGING_CONFIG_METHODS = ("Sink", "Exclusion", "Bucket", "Settings", "CmekSettings", "View")

_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)$")


def _ts(s: str) -> datetime:
    """RFC 3339 with 0–9 fraction digits and Z or ±hh:mm; anything else raises ValueError."""
    m = _TS.match(str(s or ""))
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


def iam_disabled_services(spec_events: list[dict], until: datetime) -> dict[str, tuple[str, str | None, str]]:
    """Services whose Cloud Run invoker IAM check was disabled (public with NO allUsers binding) in any applied spec
    up to `until`. Conservative: once seen, the service is evaluated."""
    out = {}
    for e in spec_events:
        if not _is_applied(e):
            continue
        try:
            if _ts(e["timestamp"]) > until:
                continue
        except (ValueError, KeyError):
            continue
        svc = _spec_of(e.get("protoPayload") or {}) or {}
        ann = ((svc.get("metadata") or {}).get("annotations")) or {}
        if str(ann.get(INVOKER_IAM_DISABLED, "")).lower() == "true" or svc.get("invokerIamDisabled") is True:
            m = RUN_SERVICE.match((e.get("protoPayload") or {}).get("resourceName", ""))
            if m:
                loc = m.group(2) or ((e.get("resource") or {}).get("labels") or {}).get("location")
                out[f"projects/{m.group(1)}/locations/{loc}/services/{m.group(3)}"] = (m.group(1), loc, m.group(3))
    return out


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
def _spec_problems(svc: dict) -> list[str]:
    """Request-based CPU and no minimum instances in ONE applied service spec (v1 or v2). Unknown shape → problem."""
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
    elif "template" in svc:                                                   # v2
        tmpl = svc.get("template") or {}
        for c in tmpl.get("containers") or []:
            res = c.get("resources") or {}
            if res.get("cpuIdle") is False or ("cpuIdle" not in res and res.get("limits")):
                out.append("CPU выделяется вне запросов (cpuIdle не true при заданных limits)")
        for s in (tmpl.get("scaling") or {}, svc.get("scaling") or {}):
            try:
                if int(s.get("minInstanceCount") or 0) > 0:
                    out.append(f"минимум экземпляров {s.get('minInstanceCount')}")
            except (TypeError, ValueError):
                out.append("минимум экземпляров не читается")
    else:
        out.append("спецификация сервиса не восстанавливается из события")
    return out


def _latest_only(svc: dict) -> bool:
    """Traffic 100 % to the latest revision and no tags: older revisions (with older templates) are not invocable."""
    traffic = (svc.get("spec") or {}).get("traffic") if "spec" in svc else svc.get("traffic")
    if not isinstance(traffic, list) or len(traffic) != 1:
        return False
    t = traffic[0]
    latest = t.get("latestRevision") is True or t.get("type") == "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    try:
        percent = int(t.get("percent") or 0)
    except (TypeError, ValueError):
        return False
    return latest and percent == 100 and not t.get("tag")


def spec_proof(spec_events: list[dict], start: datetime, end: datetime, project: str, location: str | None,
               service: str) -> list[str]:
    """The spec in force at `start` and every applied change up to `end` must be request-based CPU without minimum
    instances; if the specs do not route 100 % to the latest untagged revision, EVERY applied spec since creation
    must be clean (older revisions stay invocable). Events are re-checked for the exact service and location."""
    mine = []
    for e in spec_events:
        pp = e.get("protoPayload") or {}
        m = RUN_SERVICE.match(pp.get("resourceName", ""))
        loc = (m.group(2) if m else None) or ((e.get("resource") or {}).get("labels") or {}).get("location")
        if not m or m.group(3) != service or m.group(1) != project or (location and loc != location):
            continue
        if _is_applied(e):
            mine.append(e)
    try:
        mine.sort(key=lambda e: _ts(e["timestamp"]))
    except (ValueError, KeyError):
        return ["событие спецификации без корректного времени"]
    before = [e for e in mine if _ts(e["timestamp"]) <= start]
    during = [e for e in mine if start < _ts(e["timestamp"]) <= end]
    if not before:
        return ["нет события Admin Activity со спецификацией сервиса до начала окна"]
    latest_only = all(_latest_only(_spec_of(e.get("protoPayload") or {}) or {}) for e in [before[-1], *during])
    check = [before[-1], *during] if latest_only else [*before, *during]
    out = []
    for e in check:
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


# -------------------------------------------------------------------------------- attribution ---
def _payload_problems(p: dict) -> list[str]:
    out = []
    extra = set(p) - ALLOWED_KEYS
    if extra:
        out.append(f"поля вне контракта: {sorted(extra)[:5]}")
    for k, v in p.items():
        if isinstance(v, str) and k != "message":
            if v == "<invalid>" or not SAFE_VALUE.match(v):
                out.append(f"недопустимое значение поля {k}")
        if isinstance(v, str) and any(r.search(v) for r in SECRET_SHAPES):
            out.append(f"похоже на секрет в поле {k}")
    try:
        _ts(p.get("ts"))
    except ValueError:
        out.append("ts не RFC 3339")
    seq = p.get("seq")
    if (not isinstance(seq, int) or isinstance(seq, bool) or seq < 1) and p.get("correlation_id") != "none":
        out.append("нет порядкового seq")
    return out


def evaluate(service: str, requests: list[dict], events: list[dict], spec_events: list[dict],
             since: datetime, until: datetime, now: datetime, project: str, control: list[dict] | None = None,
             location: str | None = None, routing_changes: list[dict] | None = None) -> dict:
    """Verdict for ONE public service. `requests`/`events` must cover [since - lookback, until + lookback];
    `control` = the same request filter over the POSITIVE_CONTROL_DAYS before the lookback; `now` is sampled BEFORE
    the reads; `routing_changes` = logging configuration changes around the window (must be empty)."""
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
        return tid if tid and HEX32.match(tid) else None

    # platform requests that could run inside the window (receive- or completion-stamped, either way)
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

    # security events, indexed under each candidate trace (trace_id and alt_trace_id)
    by_cand: dict[str, list[dict]] = {}
    window_events = []
    for e in events:
        p = e.get("jsonPayload") or {}
        if p.get("security_schema") != SCHEMA or p.get("event_type") not in EVENT_TYPES:
            problems.append("событие безопасности вне контракта")
            continue
        for pr in _payload_problems(p):
            problems.append(f"{p.get('event_type')}: {pr}")
        try:
            in_win = since <= _ts(e["timestamp"]) <= until
        except (ValueError, KeyError):
            problems.append("событие без корректного времени")
            continue
        if in_win:
            window_events.append(p)
        cands = [c for c in (p.get("trace_id"), p.get("alt_trace_id")) if isinstance(c, str) and HEX32.match(c)]
        et = platform_trace(e)
        if not cands or (et is not None and et != p.get("trace_id")):
            if in_win:
                problems.append(f"{p.get('event_type')} без трассы запроса — не атрибутируется")
            continue
        for c in dict.fromkeys(cands):
            by_cand.setdefault(c, []).append(p)

    attributed_cids: set[str] = set()
    cid_owner: dict[str, str] = {}
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
        reqs_t = by_trace.get(t, [])
        evs = by_cand.get(t, [])
        by_cid: dict[str, list[dict]] = {}
        for p in evs:
            by_cid.setdefault(p.get("correlation_id"), []).append(p)
        if len(reqs_t) > 1:
            # a client can send any trace header: duplicates are tolerated ONLY for non-mutating requests with one
            # complete event pair per platform request and matching statuses
            dones = [p for p in evs if p["event_type"] == "request_done"]
            if (any(p["event_type"].startswith("mutation") for p in evs) or len(by_cid) != len(reqs_t)
                    or len(dones) != len(reqs_t) or any(d.get("mutation_attempts") != 0 for d in dones)
                    or sorted(d.get("status_code") or 0 for d in dones)
                    != sorted((q.get("httpRequest") or {}).get("status") or 0 for q in reqs_t)):
                problems.append(f"трасса {t[:8]}… у {len(reqs_t)} запросов — атрибуция неоднозначна")
                continue
            for cid in by_cid:
                attributed_cids.add(cid)
                if cid_owner.setdefault(cid, t) != t:
                    problems.append(f"correlation_id {str(cid)[:8]}… у нескольких запросов")
            continue
        if len(by_cid) != 1:
            problems.append(f"запрос {rid}: событий {len(by_cid)} запросов на одной трассе — инструментирование "
                            f"не доказано" if by_cid else f"запрос {rid}: нет событий — инструментирование не доказано")
            continue
        cid, pl = next(iter(by_cid.items()))
        if cid_owner.setdefault(cid, t) != t:
            problems.append(f"correlation_id {str(cid)[:8]}… у нескольких запросов")
            continue
        attributed_cids.add(cid)
        seqs = [p.get("seq") for p in pl]
        if any(not isinstance(s, int) or isinstance(s, bool) for s in seqs) or len(set(seqs)) != len(seqs):
            problems.append(f"запрос {rid}: порядок событий (seq) не доказан")
            continue
        pl = sorted(pl, key=lambda p: p["seq"])
        starts = [p for p in pl if p["event_type"] == "request_start"]
        dones = [p for p in pl if p["event_type"] == "request_done"]
        if len(starts) != 1 or len(dones) != 1 or pl[0] is not starts[0] or pl[-1] is not dones[0]:
            problems.append(f"запрос {rid}: нет ровно одной пары request_start/request_done в начале и конце")
            continue
        rev = ((r.get("resource") or {}).get("labels") or {}).get("revision_name")
        if any(p.get("revision") != rev for p in pl):
            problems.append(f"запрос {rid}: ревизия событий не совпадает с ревизией запроса")
        status = (r.get("httpRequest") or {}).get("status")
        if dones[0].get("status_code") != status:
            problems.append(f"запрос {rid}: статус request_done {dones[0].get('status_code')} ≠ {status}")
        route = starts[0].get("route")
        by_id = {p.get("event_id"): p for p in pl}
        attempts = [p for p in pl if p["event_type"] == "mutation_attempt"]
        outcomes: dict[str, list[dict]] = {}
        for p in pl:
            if p["event_type"] in ("mutation_success", "mutation_failure"):
                outcomes.setdefault(p.get("mutation_id"), []).append(p)
        if dones[0].get("mutation_attempts") != len(attempts):
            problems.append(f"запрос {rid}: request_done.mutation_attempts ≠ числу mutation_attempt")
        for a in attempts:
            mutations += 1
            kind = a.get("mutation_class")
            auth = by_id.get(a.get("auth_event_id"))
            if (auth is None or auth["event_type"] != "auth_ok" or auth["seq"] >= a["seq"]
                    or auth.get("auth_mechanism") != ROUTE_AUTH.get(route)):
                problems.append(f"запрос {rid}: мутация {kind} без предшествующего auth_ok механизма маршрута {route}")
            if a.get("target_system") == "wb" and route == "/telegram-webhook":
                z = by_id.get(a.get("authz_event_id"))
                if (z is None or z["event_type"] != "authz_ok" or z.get("allowlist_configured") is not True
                        or z["seq"] >= a["seq"]):
                    problems.append(f"запрос {rid}: запись в WB из Telegram без authz_ok настроенного allow-list")
            outs = outcomes.get(a.get("mutation_id"), [])
            if len(outs) != 1:
                problems.append(f"запрос {rid}: у мутации {kind} {len(outs)} исходов вместо 1")
            elif outs[0].get("result") not in KNOWN_OUTCOMES:
                problems.append(f"запрос {rid}: исход мутации {kind} неизвестен ({outs[0].get('result')})")
            elif outs[0]["seq"] <= a["seq"]:
                problems.append(f"запрос {rid}: исход мутации {kind} раньше попытки")
        if set(outcomes) - {a.get("mutation_id") for a in attempts}:
            problems.append(f"запрос {rid}: исход без mutation_attempt")

    # every security event inside the window must belong to an attributed in-window request
    stray = [p for p in window_events if p.get("correlation_id") not in attributed_cids]
    if stray:
        problems.append(f"{len(stray)} событий в окне вне атрибутированных запросов (напр. {stray[0].get('event_type')})")

    problems += [f"spec: {p}" for p in spec_proof(spec_events, since - lookback, until, project, location, service)]
    if not in_window:
        klass = "NO_TRAFFIC"
        problems += [f"NO_TRAFFIC: {p}" for p in control_problems(control, project, service)]
    else:
        klass = "ATTRIBUTED"
    return {"service": service, "class": klass, "status": "BLOCKED" if problems else "PROVEN",
            "requests_in_window": len(in_window), "mutation_attempts": mutations,
            "events_in_window": len(window_events), "problems": problems[:50],
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
    routing = src.logs(routing_change_filter(src.project, control_start, until + lookback)) if services else []
    for rn, (project, loc, service) in sorted(services.items()):
        if project not in (src.project, src.project_number()):
            problems.append(f"публичный сервис чужого проекта {rn[:80]} — вне модели")
            continue
        reqs = src.logs(request_filter(src.project, service, since - lookback, until + lookback, loc))
        evs = src.logs(event_filter(src.project, service, since - lookback, until + lookback, loc))
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
