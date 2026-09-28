"""F-18: a public Cloud Run ingress is exempted from the policy problem ONLY with a machine proof for the window.

Two proof classes (tools/autonomy/ingress_attribution.py):
  NO_TRAFFIC  — no platform request could run inside the window, positive control of the filter, no routing change;
  ATTRIBUTED  — every request attributed by wbcomm.security.v1 events on the same trace (seq-ordered, causal ids).
Both need the spec proof (request-based CPU, no min instances). Everything else is BLOCKED. Retry of transient
source errors is bounded and never yields PASS.
"""
from __future__ import annotations

import copy
import io
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from tools.autonomy import audit as A
from tools.autonomy import ingress_attribution as I
from tools.tests.test_autonomy_audit_coverage import P, SA, SA_POLICIES, FakeSource, pol

SVC = "evetis-wb-communications"
LOC = "europe-west1"
RN = f"projects/{P}/locations/{LOC}/services/{SVC}"
REV = f"{SVC}-00031-abc"
SINCE = datetime(2026, 9, 27, 12, 12, 35, tzinfo=timezone.utc)
UNTIL = datetime(2026, 9, 27, 12, 55, 0, tzinfo=timezone.utc)
LATER = UNTIL + timedelta(hours=2)          # settled
T1, T2 = "a" * 32, "b" * 32


def iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def spec(ts="2026-07-29T15:08:58Z", ann=None, svc_ann=None, status=None, v2=None, service=SVC, loc=LOC,
         traffic=None, request_extra=None):
    if v2 is not None:
        body, rn = v2, f"projects/{P}/locations/{loc}/services/{service}"
    else:
        body = {"metadata": {"annotations": svc_ann or {}},
                "spec": {"template": {"metadata": {"annotations": ann if ann is not None else
                                                   {"run.googleapis.com/startup-cpu-boost": "true"}}},
                         "traffic": traffic if traffic is not None else [{"latestRevision": True, "percent": 100}]}}
        rn = f"namespaces/{P}/services/{service}"
    return {"timestamp": ts, "resource": {"labels": {"location": loc, "service_name": service}},
            "protoPayload": {"methodName": "google.cloud.run.v1.Services.ReplaceService", "resourceName": rn,
                             "request": {"service": body, **(request_extra or {})},
                             **({"status": status} if status else {})}}


GOOD_SPEC = [spec()]


def req(at: datetime, trace=T1, status=200, latency="1.5s", rev=REV, method="POST", project=P):
    return {"timestamp": iso(at), "trace": f"projects/{project}/traces/{trace}" if trace else None,
            "httpRequest": {"requestMethod": method, "status": status, "latency": latency},
            "resource": {"labels": {"revision_name": rev, "service_name": SVC, "location": LOC}}}


def ev(at: datetime, event_type: str, seq: int, trace=T1, cid="c" * 32, rev=REV, **fields):
    payload = {"security_schema": I.SCHEMA, "event_type": event_type, "event_id": fields.pop("event_id", f"{seq:032x}"),
               "ts": iso(at), "service": SVC, "revision": rev, "correlation_id": cid, "trace_id": trace,
               "alt_trace_id": fields.pop("alt", None), "span_id": "0" * 15 + "1",
               "route": fields.pop("route", "/poll"), "seq": seq,
               "severity": "NOTICE", "message": f"security_event {event_type}", **fields}
    return {"timestamp": iso(at), "trace": f"projects/{P}/traces/{trace}" if trace else None, "jsonPayload": payload}


AUTH_ID = f"{2:032x}"
AUTHZ_ID = f"{3:032x}"


def chain(t0: datetime, trace=T1, cid="c" * 32, mutations=(("tg_send_message", "telegram", "success"),),
          route="/poll", mech="scheduler_secret", status=200, authz=None, alt=None, **over):
    """A complete, valid request chain; `over` lets a test break exactly one invariant."""
    at = lambda k: t0 + timedelta(milliseconds=k)          # noqa: E731
    seq = iter(range(1, 100))
    common = dict(trace=trace, cid=cid, route=route, alt=alt)
    evs = [ev(at(1), "request_start", next(seq), method="POST", **common)]
    evs.append(ev(at(2), "auth_ok", next(seq), auth_mechanism=mech, result="ok", principal_class="cloud_scheduler",
                  **common))
    if authz is not None:
        evs.append(ev(at(3), "authz_ok", next(seq), auth_mechanism="telegram_allowlist", result="ok",
                      principal_class="telegram_user", allowlist_configured=authz, **common))
    for n, (klass, system, result) in enumerate(mutations):
        mid = f"{n:x}".rjust(32, "d")
        evs.append(ev(at(10 + n * 2), "mutation_attempt", next(seq), mutation_id=mid, mutation_class=klass,
                      target_system=system, target_ref="tg_chat:configured", result="attempt",
                      auth_event_id=over.get("auth_event_id", AUTH_ID),
                      authz_event_id=over.get("authz_event_id", AUTHZ_ID if authz is not None else None), **common))
        if result is not None:
            evs.append(ev(at(11 + n * 2), "mutation_success" if result == "success" else "mutation_failure",
                          next(seq), mutation_id=mid, mutation_class=klass, target_system=system,
                          target_ref="tg_chat:configured", result=result, **common))
    evs.append(ev(at(50), "request_done", next(seq), method="POST", status_code=status, result="ok",
                  mutation_attempts=over.get("attempts", len(mutations)), duration_ms=49, **common))
    return evs


def control_req(**kw):
    return req(SINCE - timedelta(days=2), **kw)


CONTROL = [control_req()]


def verdict(requests=(), events=(), specs=GOOD_SPEC, now=LATER, since=SINCE, until=UNTIL, control=CONTROL,
            routing=()):
    return I.evaluate(SVC, list(requests), list(events), list(specs), since, until, now, P, control, LOC,
                      None if routing is None else list(routing))


def without(evs, event_type):
    """Drop one event type and renumber seq 1..n (so only the targeted invariant breaks)."""
    out = [copy.deepcopy(e) for e in evs if e["jsonPayload"]["event_type"] != event_type]
    for i, e in enumerate(out, 1):
        e["jsonPayload"]["seq"] = i
    return out


def _broken(problem_substring, requests=(), events=(), **kw):
    v = verdict(requests, events, **kw)
    assert v["status"] == "BLOCKED" and any(problem_substring in p for p in v["problems"]), v["problems"]


# ------------------------------------------------------------------------------- NO_TRAFFIC ---
def test_no_traffic_with_request_based_cpu_is_proven():
    v = verdict()
    assert v["status"] == "PROVEN" and v["class"] == "NO_TRAFFIC" and v["requests_in_window"] == 0, v["problems"]


def test_window_must_be_settled_including_ingestion_margin():
    _broken("не устоялось", now=UNTIL + timedelta(seconds=3600 + 60))
    assert verdict(now=UNTIL + timedelta(seconds=I.INGRESS_SETTLE_S))["status"] == "PROVEN"


@pytest.mark.parametrize("specs,needle", [
    ([], "нет события"),
    ([spec(ann={"run.googleapis.com/cpu-throttling": "false"})], "cpu-throttling"),
    ([spec(ann={"autoscaling.knative.dev/minScale": "1"})], "минимум"),
    ([spec(svc_ann={"run.googleapis.com/minScale": "2"})], "минимум"),
    ([spec(), spec(ts=iso(SINCE + timedelta(minutes=5)), ann={"run.googleapis.com/cpu-throttling": "false"})], "cpu"),
    ([spec(v2={"template": {"containers": [{"resources": {"cpuIdle": False}}]}})], "cpuIdle"),
    ([spec(v2={"template": {"containers": [{"resources": {"limits": {"cpu": "1"}}}]}})], "cpuIdle"),   # M2
    ([spec(v2={"template": {"containers": [{"resources": {"startupCpuBoost": True}}]}})], "cpuIdle"),  # M-d
    ([spec(v2={"template": {"scaling": {"scalingMode": "MANUAL", "manualInstanceCount": 1}}})], "ручное"),
    ([spec(v2={"scaling": {"scalingMode": "MANUAL"}, "template": {}})], "ручное"),
    ([spec(svc_ann={"run.googleapis.com/scalingMode": "manual"})], "ручное"),
    ([spec(v2={"template": {"scaling": {"minInstanceCount": 1}}})], "минимум"),
    ([{"timestamp": "2026-07-29T15:08:58Z", "resource": {"labels": {"location": LOC}},
       "protoPayload": {"resourceName": f"namespaces/{P}/services/{SVC}", "request": {}}}], "не восстанавливается"),
])
def test_spec_proof_is_required(specs, needle):
    _broken(needle, specs=specs)


def test_spec_proof_is_also_required_for_attributed_requests():                  # M4
    t0 = SINCE + timedelta(minutes=10)
    _broken("cpu-throttling", [req(t0)], chain(t0), specs=[spec(ann={"run.googleapis.com/cpu-throttling": "false"})])


def test_sibling_service_or_other_region_spec_does_not_prove_this_one():          # M1
    bad_own = spec(ann={"run.googleapis.com/cpu-throttling": "false"})
    sibling_clean = spec(ts="2026-08-01T00:00:00Z", service=f"{SVC}-canary")
    other_region = spec(ts="2026-08-02T00:00:00Z", loc="us-central1")
    _broken("cpu-throttling", specs=[bad_own, sibling_clean, other_region])


def test_every_historical_spec_must_be_clean():                                     # M2 / L-a
    old_bad = spec(ts="2026-07-01T00:00:00Z", ann={"run.googleapis.com/cpu-throttling": "false"})
    _broken("cpu-throttling", specs=[old_bad, spec()])     # tags/splits or a failed rollout keep old revisions alive
    assert verdict(specs=[spec(v2={"template": {"containers": [{"resources": {"cpuIdle": True,
                                                                               "limits": {"cpu": "1"}}}]}})])[
        "status"] == "PROVEN"


def test_unknown_location_cannot_bind_a_spec():                                     # L-g
    v = I.evaluate(SVC, [], [], GOOD_SPEC, SINCE, UNTIL, LATER, P, CONTROL, None, [])
    assert v["status"] == "BLOCKED" and any("регион" in p for p in v["problems"])


def test_rejected_or_validate_only_spec_is_not_in_force():                          # M2
    bad = dict(ann={"run.googleapis.com/cpu-throttling": "false"}, ts=iso(SINCE + timedelta(minutes=5)))
    assert verdict(specs=GOOD_SPEC + [spec(status={"code": 3}, **bad)])["status"] == "PROVEN"
    assert verdict(specs=GOOD_SPEC + [spec(request_extra={"validateOnly": True}, **bad)])["status"] == "PROVEN"


@pytest.mark.parametrize("control,needle", [
    (None, "не выполнен"),
    ([], "не доказан"),
    ([control_req(project="other-project")], "не доказан"),
    ([{"timestamp": "2026-09-25T00:00:00Z"}], "не доказан"),
])
def test_no_traffic_needs_a_positive_control_of_the_filter(control, needle):
    _broken(needle, control=control)


def test_routing_change_around_the_window_blocks():                                # L7
    _broken("маршрутизация", routing=[{"protoPayload": {"methodName": "UpdateSink"}}])
    _broken("не прочитана", routing=None)


def test_request_that_ended_before_the_window_is_irrelevant():
    assert verdict(requests=[req(SINCE - timedelta(minutes=70), latency="3.0s")])["class"] == "NO_TRAFFIC"


def test_request_started_before_window_but_running_into_it_counts():
    _broken("инструментирование", [req(SINCE - timedelta(seconds=100), latency="200s")])


def test_completion_stamped_request_ending_after_until_counts():                     # L1
    _broken("инструментирование", [req(UNTIL + timedelta(seconds=30), latency="60s")])


def test_request_without_latency_is_assumed_to_run_the_maximum():
    _broken("инструментирование", [req(SINCE - timedelta(minutes=30), latency=None)])


def test_security_event_in_window_without_in_window_request_blocks():               # M3
    late = UNTIL + timedelta(minutes=20)
    evs = chain(late)
    evs[1]["timestamp"] = iso(UNTIL - timedelta(seconds=1))            # an event stamped inside the window
    _broken("вне атрибутированных", [req(late)], evs)


# ------------------------------------------------------------------------------ ATTRIBUTED ---
T0 = SINCE + timedelta(minutes=10)


def test_fully_attributed_request_is_proven():
    v = verdict([req(T0)], chain(T0))
    assert v["status"] == "PROVEN" and v["class"] == "ATTRIBUTED" and v["mutation_attempts"] == 1, v["problems"]


def test_denied_request_without_mutation_is_proven():
    evs = [ev(T0, "request_start", 1), ev(T0 + timedelta(milliseconds=1), "auth_denied", 2,
                                          auth_mechanism="scheduler_secret", result="denied", reason="mismatch",
                                          principal_class="cloud_scheduler"),
           ev(T0 + timedelta(milliseconds=2), "request_done", 3, status_code=403, result="denied", mutation_attempts=0)]
    assert verdict([req(T0, status=403)], evs)["status"] == "PROVEN"


def test_uninstrumented_request_is_blocked():
    _broken("инструментирование", [req(T0)], [])


def test_mutation_without_auth_ok_is_blocked():
    _broken("без предшествующего auth_ok", [req(T0)], without(chain(T0), "auth_ok"))


def test_mutation_pointing_to_foreign_auth_event_is_blocked():
    _broken("без предшествующего auth_ok", [req(T0)], chain(T0, auth_event_id="f" * 32))


def test_auth_mechanism_must_match_the_route():
    _broken("механизма маршрута", [req(T0)], chain(T0, mech="admin_token"))
    _broken("механизма маршрута", [req(T0)], chain(T0, route="/health"))


def test_order_is_by_seq_not_by_timestamps():
    evs = chain(T0)
    a, b = evs[1]["jsonPayload"], evs[2]["jsonPayload"]              # auth_ok, mutation_attempt
    a["seq"], b["seq"] = b["seq"], a["seq"]
    _broken("без предшествующего auth_ok", [req(T0)], evs)


def test_missing_or_duplicate_seq_is_blocked():
    evs = chain(T0)
    evs[3]["jsonPayload"]["seq"] = evs[2]["jsonPayload"]["seq"]
    _broken("seq", [req(T0)], evs)
    evs = chain(T0)
    del evs[1]["jsonPayload"]["seq"]
    _broken("seq", [req(T0)], evs)


@pytest.mark.parametrize("result,needle", [("outcome_unknown", "неизвестен"), (None, "0 исходов")])
def test_unknown_or_missing_outcome_is_blocked(result, needle):
    _broken(needle, [req(T0)], chain(T0, mutations=(("wb_feedback_answer", "wb", result),)))


def test_duplicate_outcome_is_blocked():
    evs = chain(T0)
    dup = copy.deepcopy(next(e for e in evs if e["jsonPayload"]["event_type"] == "mutation_success"))
    dup["jsonPayload"]["seq"] = evs[-1]["jsonPayload"]["seq"]
    evs[-1]["jsonPayload"]["seq"] += 1
    evs.insert(-1, dup)
    _broken("2 исходов", [req(T0)], evs)


def test_attempt_count_mismatch_is_blocked():
    _broken("mutation_attempts", [req(T0)], chain(T0, attempts=0))


def test_orphan_events_without_platform_request_are_blocked():
    _broken("вне атрибутированных", [], chain(T0))


def test_event_without_trace_inside_window_is_blocked():
    lone = ev(T0, "mutation_attempt", 1, trace=None, mutation_id="d" * 32, mutation_class="tg_send_message",
              target_system="telegram", target_ref="tg:bot", result="attempt")
    _broken("вне атрибутированных", [], [lone])


def test_duplicated_trace_with_mutation_is_ambiguous():
    _broken("неоднозначн", [req(T0), req(T0 + timedelta(seconds=1))], chain(T0))


def test_duplicated_trace_of_non_mutating_requests_is_tolerated():                  # wb-comms review M1
    a = chain(T0, cid="1" * 32, mutations=())
    b = chain(T0 + timedelta(seconds=1), cid="2" * 32, mutations=())
    for e in b:
        e["jsonPayload"]["event_id"] = "9" + e["jsonPayload"]["event_id"][1:]
    v = verdict([req(T0), req(T0 + timedelta(seconds=1))], a + b)
    assert v["status"] == "PROVEN", v["problems"]


def test_alt_trace_attributes_a_request_logged_under_the_other_header():            # wb-comms review M1
    v = verdict([req(T0, trace=T2)], chain(T0, trace=T1, alt=T2))
    assert v["status"] == "PROVEN", v["problems"]


def test_revision_and_status_must_match_the_platform_log():
    _broken("ревизия", [req(T0, rev=f"{SVC}-00025-rq8")], chain(T0))
    _broken("статус", [req(T0, status=500)], chain(T0))


def test_request_log_of_another_project_has_no_usable_trace():
    _broken("без трассы", [req(T0, project="other-project")], chain(T0))


@pytest.mark.parametrize("field,value,needle", [
    ("error_class", "<invalid>", "недопустимое"),
    ("target_ref", "123456789:AAFsyntheticTokenValue_abcdefghijklmnop", "секрет"),
    ("extra_header", "x", "вне контракта"),
    ("route", "/poll?token=abc", "недопустимое"),
    ("ts", "yesterday", "RFC 3339"),                                                   # L4
])
def test_unsafe_event_payload_is_blocked(field, value, needle):
    evs = chain(T0)
    evs[1]["jsonPayload"][field] = value
    _broken(needle, [req(T0)], evs)


def test_wb_write_from_telegram_requires_a_configured_allowlist():
    wb = (("wb_question_answer", "wb", "success"),)
    base = dict(route="/telegram-webhook", mech="telegram_webhook_secret", mutations=wb)
    _broken("allow-list", [req(T0)], chain(T0, **base))                       # no authz event at all
    _broken("allow-list", [req(T0)], chain(T0, authz=False, **base))          # allow-list not configured
    _broken("allow-list", [req(T0)], chain(T0, authz=True, authz_event_id="f" * 32, **base))   # not causal
    assert verdict([req(T0)], chain(T0, authz=True, **base))["status"] == "PROVEN"


def test_timestamp_parser_handles_nanoseconds_and_offsets():                      # L3
    assert I._ts("2026-09-27T12:00:00.123456789Z") == datetime(2026, 9, 27, 12, 0, 0, 123456, tzinfo=timezone.utc)
    assert I._ts("2026-09-27T09:00:00.5-03:00") == datetime(2026, 9, 27, 12, 0, 0, 500000, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        I._ts("2026-09-27 12:00")


def test_contract_matches_the_producer_field_set():
    """The consumer's closed field set is exactly the producer's (services/wb-communications/app/utils/audit_events.py,
    1.6.0). Once both are on one branch this test should import the producer's sets instead of a copy."""
    producer = {"security_schema", "event_type", "event_id", "ts", "trace_id", "alt_trace_id", "span_id",
                "correlation_id", "route", "method", "service", "revision", "auth_mechanism", "principal_class",
                "result", "reason", "mutation_id", "mutation_class", "target_system", "target_ref", "error_class",
                "auth_event_id", "authz_event_id", "status_code", "mutation_attempts", "duration_ms",
                "allowlist_configured", "seq"}
    assert I.ALLOWED_KEYS == producer | {"severity", "message"}


def test_filters_are_exact():
    a, b = SINCE, UNTIL
    assert I.request_filter(P, SVC, a, b, LOC) == (
        f'logName="projects/{P}/logs/run.googleapis.com%2Frequests" AND resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{SVC}" AND resource.labels.location="{LOC}" '
        f'AND timestamp>="2026-09-27T12:12:35.000000Z" AND timestamp<="2026-09-27T12:55:00.000000Z"')
    assert 'jsonPayload.security_schema="wbcomm.security.v1"' in I.event_filter(P, SVC, a, b, LOC)
    assert "run.googleapis.com%2Fstdout" in I.event_filter(P, SVC, a, b, LOC)
    sf = I.spec_filter(P, "2026-07-10T10:26:24Z", SVC)
    assert f'protoPayload.resourceName="namespaces/{P}/services/{SVC}"' in sf and f"services/{SVC}$" in sf
    assert "resourceName:" not in sf                                                  # no substring match (M1)


# ------------------------------------------------------------------- integration with run_audit ---
CONTROL_START = iso(SINCE - timedelta(seconds=I.PLATFORM_MAX_REQUEST_TIMEOUT_S) - timedelta(days=I.POSITIVE_CONTROL_DAYS))


class IngressSource(FakeSource):
    def __init__(self, requests=(), events=(), specs=GOOD_SPEC, now=LATER, control=CONTROL, all_specs=None,
                 routing_changes=(), **kw):
        super().__init__(**kw)
        self._req, self._ev, self._spec, self._now = list(requests), list(events), list(specs), now
        self._control, self._routing_changes = list(control), list(routing_changes)
        self._all_specs = list(all_specs) if all_specs is not None else list(specs)

    def now(self):
        return self._now

    def logs(self, flt):
        self.filters.append(flt)
        if "run.googleapis.com%2Frequests" in flt:
            return self._control if CONTROL_START in flt else self._req
        if "run.googleapis.com%2Fstdout" in flt:
            return self._ev
        if 'serviceName="logging.googleapis.com"' in flt:
            return self._routing_changes
        if "ReplaceService" in flt:
            return self._spec if f"services/{SVC}" in flt else self._all_specs
        return super().logs(flt)


PUBLIC_RUN = pol("run.googleapis.com", RN, [{"role": "roles/run.invoker", "members": ["allUsers"]}])


def run(src, settle_from=iso(UNTIL)):
    def wm():
        src.label = "autonomy-audit-wm-x"
        return src.label
    return A.run_audit(src, iso(SINCE), [SA], wm, settle_from=settle_from)


def test_public_ingress_with_no_traffic_passes_the_audit():
    r = run(IngressSource(iam_events=list(SA_POLICIES) + [PUBLIC_RUN]))
    assert r["status"] == "PASS", r["iam_invariant"]
    v = r["ingress_attribution"][SVC]
    assert v["status"] == "PROVEN" and v["class"] == "NO_TRAFFIC"


def test_public_ingress_with_unattributed_request_blocks_the_audit():
    r = run(IngressSource(requests=[req(T0)], iam_events=list(SA_POLICIES) + [PUBLIC_RUN]))
    assert r["status"] == "BLOCKED" and r["mutations"] is None
    assert any("F-18" in p for p in r["iam_invariant"]) and any("allUsers" in p for p in r["iam_invariant"])


def test_public_ingress_attributed_requests_pass_the_audit():
    r = run(IngressSource(requests=[req(T0)], events=chain(T0), iam_events=list(SA_POLICIES) + [PUBLIC_RUN]))
    assert r["status"] == "PASS", r["iam_invariant"]
    assert r["ingress_attribution"][SVC]["class"] == "ATTRIBUTED"


def test_not_settled_window_blocks_even_with_no_traffic():
    r = run(IngressSource(iam_events=list(SA_POLICIES) + [PUBLIC_RUN], now=UNTIL + timedelta(minutes=10)))
    assert r["status"] == "BLOCKED" and any("не устоялось" in p for p in r["iam_invariant"])


def test_no_traffic_without_positive_control_blocks_the_audit():
    r = run(IngressSource(iam_events=list(SA_POLICIES) + [PUBLIC_RUN], control=[]))
    assert r["status"] == "BLOCKED" and any("не доказан" in p for p in r["iam_invariant"])


def test_routing_change_blocks_the_audit():
    r = run(IngressSource(iam_events=list(SA_POLICIES) + [PUBLIC_RUN],
                          routing_changes=[{"protoPayload": {"methodName": "google.logging.v2.ConfigServiceV2.UpdateSink"}}]))
    assert r["status"] == "BLOCKED" and any("маршрутизация" in p for p in r["iam_invariant"])


def test_binding_public_only_during_the_window_is_still_evaluated():                  # M5
    grant = pol("run.googleapis.com", RN, [{"role": "roles/run.invoker", "members": ["allUsers"]}],
                ts=iso(SINCE + timedelta(minutes=1)))
    revoke = pol("run.googleapis.com", RN, [{"role": "roles/run.invoker", "members": [f"serviceAccount:{SA}"]}],
                 ts=iso(SINCE + timedelta(minutes=2)))
    r = run(IngressSource(requests=[req(T0)], iam_events=list(SA_POLICIES) + [grant, revoke]))
    assert SVC in r["ingress_attribution"] and r["status"] == "BLOCKED"


def test_invoker_iam_disabled_service_is_evaluated_as_public():                      # M5
    disabled = spec(svc_ann={"run.googleapis.com/invoker-iam-disabled": "true"})
    r = run(IngressSource(requests=[req(T0)], all_specs=[disabled], iam_events=list(SA_POLICIES)))
    assert SVC in r["ingress_attribution"] and r["status"] == "BLOCKED"
    r = run(IngressSource(all_specs=[disabled], iam_events=list(SA_POLICIES)))
    assert r["status"] == "PASS", r["iam_invariant"]                                  # proven NO_TRAFFIC


def test_other_public_bindings_are_never_exempted():
    extra = pol("run.googleapis.com", RN, [{"role": "roles/run.invoker", "members": ["allUsers"]},
                                           {"role": "roles/run.developer", "members": ["allUsers"]}])
    r = run(IngressSource(iam_events=list(SA_POLICIES) + [extra]))
    assert r["status"] == "BLOCKED" and any("run.developer" in p for p in r["iam_invariant"])
    bucket = pol("storage.googleapis.com", "projects/_/buckets/b", [{"role": "roles/storage.objectAdmin",
                                                                     "members": ["allUsers"]}])
    r = run(IngressSource(iam_events=list(SA_POLICIES) + [PUBLIC_RUN, bucket]))
    assert r["status"] == "BLOCKED" and any("objectAdmin" in p for p in r["iam_invariant"])


def test_public_service_of_another_project_is_blocked():
    foreign = pol("run.googleapis.com", "projects/other/locations/x/services/y",
                  [{"role": "roles/run.invoker", "members": ["allUsers"]}])
    assert run(IngressSource(iam_events=list(SA_POLICIES) + [foreign]))["status"] == "BLOCKED"


def test_mutations_found_elsewhere_still_fail_regardless_of_ingress():
    from tools.tests.test_autonomy_audit_coverage import job
    r = run(IngressSource(jobs=[job(st="INSERT")], iam_events=list(SA_POLICIES) + [PUBLIC_RUN]))
    assert r["status"] == "FAIL"


# ------------------------------------------------------------------------ bounded retry (Part F) ---
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _src(monkeypatch, outcomes):
    calls, sleeps = [], []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        o = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(o, Exception):
            raise o
        return _Resp(o)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return A.GcpAuditSource(P, "tok", sleep=sleeps.append), calls, sleeps


def _http_error(code):
    return urllib.error.HTTPError("https://logging.googleapis.com/v2/entries:list", code, "x", {}, None)


def test_transient_5xx_is_retried_with_finite_deterministic_backoff(monkeypatch):
    src, calls, sleeps = _src(monkeypatch, [_http_error(503), _http_error(500), b'{"entries": []}'])
    assert src._http("POST", "https://logging.googleapis.com/v2/entries:list", {}) == {"entries": []}
    assert len(calls) == 3 and sleeps == [2, 4]


def test_exhausted_retries_raise_so_the_audit_is_blocked(monkeypatch):
    src, calls, sleeps = _src(monkeypatch, [_http_error(502)])
    with pytest.raises(urllib.error.HTTPError):
        src._http("POST", "https://logging.googleapis.com/v2/entries:list", {})
    assert len(calls) == 4 and sleeps == [2, 4, 8]


@pytest.mark.parametrize("code", [400, 403, 404])
def test_non_transient_errors_are_not_retried(monkeypatch, code):
    src, calls, sleeps = _src(monkeypatch, [_http_error(code)])
    with pytest.raises(urllib.error.HTTPError):
        src._http("GET", "https://logging.googleapis.com/v2/x")
    assert len(calls) == 1 and sleeps == []


def test_network_errors_are_retried_boundedly(monkeypatch):
    src, calls, sleeps = _src(monkeypatch, [urllib.error.URLError("reset"), b"{}"])
    assert src._http("GET", "https://logging.googleapis.com/v2/x") == {}
    assert sleeps == [2]


def test_positive_control_reads_a_single_page(monkeypatch):                          # L5
    src, calls, _ = _src(monkeypatch, [b'{"entries": [{"a": 1}], "nextPageToken": "more"}'])
    assert src.logs_page("f") == [{"a": 1}] and len(calls) == 1


def test_count_mutations_turns_an_exhausted_source_into_blocked(monkeypatch):
    monkeypatch.setattr(A, "resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: (_ for _ in ()).throw(_http_error(503)))
    monkeypatch.setattr("time.sleep", lambda s: None)

    class _BQ:
        def __init__(self, **kw):
            pass

        def query(self, sql):
            return []
    monkeypatch.setattr(A, "ReadOnlyBigQuery", _BQ)
    r = A.count_mutations(P, "echo tok", iso(SINCE), [SA])
    assert r["status"] == "BLOCKED" and r["mutations"] is None and "HTTPError" in r["error"]


# ------------------------------------------------------------------ window end of the trusted audit ---
def test_window_end_is_the_last_ready_for_pr_transition_not_updated_at():
    run_ = {"updated_at": "2026-10-02T09:00:00Z", "transitions": [
        {"to": "READY_FOR_PR", "at": "2026-09-27T12:20:00Z"}, {"to": "FIXING", "at": "2026-09-27T12:38:51Z"},
        {"to": "READY_FOR_PR", "at": "2026-09-27T12:53:58Z"}, {"to": "BLOCKED", "at": "2026-10-02T09:00:00Z"}]}
    assert A.run_window_end(run_) == "2026-09-27T12:53:58Z"
    assert A.run_window_end({"updated_at": "2026-09-27T13:00:00Z", "transitions": []}) == "2026-09-27T13:00:00Z"


def test_window_end_after_resumed_agent_work_is_now():                                # L6
    run_ = {"updated_at": "2026-10-02T09:00:00Z", "transitions": [
        {"to": "READY_FOR_PR", "at": "2026-09-27T12:53:58Z"}, {"to": "BLOCKED", "at": "2026-10-01T00:00:00Z"},
        {"to": "PLANNING", "at": "2026-10-02T08:00:00Z"}]}
    assert A.run_window_end(run_) is None


def test_trusted_audit_step_uses_the_window_end(monkeypatch, tmp_path):
    import argparse

    from tools.autonomy import cli
    seen = {}
    monkeypatch.setattr(A, "count_mutations", lambda project, tc, since, ids, settle_from=None:
                        seen.update(since=since, settle_from=settle_from) or {"status": "BLOCKED"})
    a = argparse.Namespace(project=P, token_command="echo t", audit_identity=[SA], cmd="audit", publish=False,
                           verify_repo=None, sandbox_root=str(tmp_path), engineer="none", reviewer="none",
                           trusted_base_ref=None, dry_run=True, pending_dir=None, expect=None)
    orch = cli._orchestrator(a, None)
    orch.audit({"created_at": "2026-09-27T12:12:35Z", "updated_at": "2026-10-02T09:00:00Z",
                "transitions": [{"to": "READY_FOR_PR", "at": "2026-09-27T12:53:58Z"}]})
    assert seen == {"since": "2026-09-27T12:12:35Z", "settle_from": "2026-09-27T12:53:58Z"}


def test_duplicated_trace_with_hidden_mutation_events_is_ambiguous():
    """Mutation events on a shared trace block even if every request_done claims 0 attempts."""
    a = chain(T0, cid="1" * 32, mutations=())
    b = chain(T0 + timedelta(seconds=1), cid="2" * 32, attempts=0)
    for e in b:
        e["jsonPayload"]["event_id"] = "9" + e["jsonPayload"]["event_id"][1:]
    _broken("неоднозначн", [req(T0), req(T0 + timedelta(seconds=1))], a + b)



# --------------------------------------------------------------- second review round (M-a..M-e, L-*) ---
def test_event_with_attributed_cid_on_another_trace_is_blocked():                   # M-a
    evs = chain(T0)
    rogue = ev(T0 + timedelta(milliseconds=5), "mutation_attempt", 9, trace=T2, event_id="e" * 32,
               mutation_id="f" * 32, mutation_class="wb_feedback_answer", target_system="wb", target_ref="wb:1",
               result="attempt")
    v = verdict([req(T0)], evs + [rogue])
    assert v["status"] == "BLOCKED" and any("разных трассах" in p or "вне атрибутированных" in p
                                            for p in v["problems"]), v["problems"]


@pytest.mark.parametrize("mutate,needle", [
    (lambda evs: evs[1]["jsonPayload"].pop("event_id"), "event_id"),                       # M-b: None never matches
    (lambda evs: evs[1]["jsonPayload"].pop("auth_mechanism"), "механизм"),
    (lambda evs: [e["jsonPayload"].pop("correlation_id") for e in evs], "correlation_id"),
    (lambda evs: evs[1]["jsonPayload"].update(event_id=evs[0]["jsonPayload"]["event_id"]), "повтор event_id"),
    (lambda evs: evs[-1]["jsonPayload"].update(seq=9), "1..n"),                            # gap in seq
    (lambda evs: evs[0]["jsonPayload"].update(route="/brand-new"), "маршрут вне контракта"),
    (lambda evs: evs[2]["jsonPayload"].update(target_system="s3"), "целевая система"),
    (lambda evs: evs[-1]["jsonPayload"].update(status_code="200"), "request_done вне контракта"),
])
def test_required_fields_are_enforced(mutate, needle):
    evs = chain(T0)
    mutate(evs)
    _broken(needle, [req(T0)], evs)


def test_mutation_on_a_route_without_authentication_is_blocked():
    _broken("аутентификации нет", [req(T0)], chain(T0, route="other"))


def test_internal_state_writes_need_auth_but_not_a_known_outcome():                 # M-e
    internal = (("firestore_begin_update", "firestore", "error"),)
    assert verdict([req(T0)], chain(T0, mutations=internal))["status"] == "PROVEN"
    no_auth = without(chain(T0, mutations=internal), "auth_ok")
    _broken("без предшествующего auth_ok", [req(T0)], no_auth)
    _broken("неизвестен", [req(T0)], chain(T0, mutations=(("tg_send_message", "telegram", "outcome_unknown"),)))


def _traceless(t0, cid="7" * 32, mutating=False):
    evs = chain(t0, trace=None, cid=cid, mutations=(("tg_send_message", "telegram", "success"),) if mutating else ())
    for e in evs:
        e["jsonPayload"]["event_id"] = "8" + e["jsonPayload"]["event_id"][1:]
    if not mutating:                                            # an unauthenticated probe: no auth_ok, denied
        evs = [e for e in evs if e["jsonPayload"]["event_type"] != "auth_ok"]
        for i, e in enumerate(evs, 1):
            e["jsonPayload"]["seq"] = i
        evs[-1]["jsonPayload"]["result"] = "none"
    return evs


def test_traceless_non_mutating_request_pairs_with_its_platform_log():               # wb-comms M-A
    v = verdict([req(T0, trace=T2)], _traceless(T0))
    assert v["status"] == "PROVEN", v["problems"]


def test_traceless_mutating_request_is_never_paired():
    _broken("без событий", [req(T0, trace=T2)], _traceless(T0, mutating=True))


def test_traceless_pairing_needs_equal_counts_revision_and_status():
    _broken("без событий", [req(T0, trace=T2), req(T0 + timedelta(seconds=1), trace="c" * 32)], _traceless(T0))
    _broken("без событий", [req(T0, trace=T2, status=404)], _traceless(T0))
    _broken("без событий", [req(T0, trace=T2, rev=f"{SVC}-00025-rq8")], _traceless(T0))


def test_audited_identity_must_not_be_able_to_forge_or_reroute_logs():               # M-c
    need = {"logging.logEntries.create", "logging.logs.delete", "logging.sinks.update", "logging.exclusions.create",
            "logging.buckets.update", "logging.settings.update"}
    assert need <= set(A.FORBIDDEN_PROJECT_PERMISSIONS)
    r = run(IngressSource(iam_events=list(SA_POLICIES) + [PUBLIC_RUN], granted=["logging.logEntries.create"]))
    assert r["status"] == "BLOCKED"


def test_unplaceable_spec_event_is_a_problem_not_a_skip():                            # L-b
    bad = spec(svc_ann={"run.googleapis.com/invoker-iam-disabled": "true"})
    bad["timestamp"] = "not-a-time"
    services, problems = I.iam_disabled_services([bad], UNTIL)
    assert services == {} and problems


def test_routing_changes_are_read_up_to_now_and_include_log_deletion():               # L-c / L-d
    flt = I.routing_change_filter(P, SINCE, LATER)
    assert 'protoPayload.methodName:"DeleteLog"' in flt
    src = IngressSource(iam_events=list(SA_POLICIES) + [PUBLIC_RUN])
    run(src)
    ev_flt = next(f for f in src.filters if "run.googleapis.com%2Fstdout" in f)
    rt_flt = next(f for f in src.filters if 'serviceName="logging.googleapis.com"' in f)
    assert f'timestamp<="{iso(LATER)}"' in ev_flt and f'timestamp<="{iso(LATER)}"' in rt_flt


def test_only_a_sibling_service_spec_does_not_prove_this_one():
    _broken("нет события", specs=[spec(service=f"{SVC}-canary"), spec(loc="us-central1")])


def test_one_correlation_with_inconsistent_trace_pairs_is_blocked():
    evs = chain(T0)
    evs[2]["jsonPayload"]["alt_trace_id"] = T2
    _broken("разных трассах", [req(T0)], evs)


def test_traceless_group_that_passed_authentication_is_never_paired():
    g = _traceless(T0)
    auth = copy.deepcopy(g[0])
    auth["jsonPayload"].update(event_type="auth_ok", event_id="8" * 31 + "e", auth_mechanism="scheduler_secret",
                               principal_class="cloud_scheduler", result="ok", seq=2)
    g[1]["jsonPayload"]["seq"] = 3
    _broken("без событий", [req(T0, trace=T2)], [g[0], auth, g[1]])
