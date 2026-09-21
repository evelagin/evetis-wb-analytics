"""Offline tests for tools/verify_current_sql_live.py (R2C phase 1).

No network, no credentials, no subprocess: BigQuery is a fake transport whose responses are built from
the canonical files and manifest; git is a fake runner. Expected hashes are the literal R2A values.
"""
import argparse
import copy
import hashlib
import json
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import verify_current_sql_live as vl  # noqa: E402

P = "project-fa311fc0-4d87-4781-986"
DS = "ozon_mart"
HOST = "www.googleapis.com"
BASE = f"https://{HOST}/bigquery/v2/projects/{P}/datasets/{DS}/tables"
TOKEN = "ya29.TEST-TOKEN-must-never-appear-0123456789abcdef"
SHA = "a" * 40
LEAF = "V_OZON_TARIFF_CHANGE_LOG"
# The baseline this fixture deploys: a self-consistent ozon_mart whose graph closes. It started as the R2A
# hash set; Gate 5K (2026-09-21) rewrote both monthly P&L views for the CIS buyout model and added their new
# dependency V_OZON_CIS_BUYOUT, so all three are pinned here at their current canonical hashes. The monthly
# views cannot simply be dropped: V_OZON_LIFETIME_PNL and V_OZON_SKU_UNIT_ECONOMICS_CURRENT read them.
# These tests exercise the live-verification tool, not the economics of any object.
BASELINE_BODY_SHA256 = {
    "V_OZON_CIS_BUYOUT": "b233b9ca53d18d8787f2b3ae6b0cca5fe774c0a52ea055869086ba6c871369ba",
    "FCT_OZON_PNL_MONTHLY": "1b6bc3ce452ec5504487669aa765d06353bf67037a6ac0394033202ef6e906fb",
    "FCT_OZON_SKU_PNL_MONTHLY": "b81327e21538a27696b44ecfc7895be770734065c5f1602500c7a40e7c2fb140",
    "V_OZON_MART_FRESHNESS": "c19355f7a86440fe9fd7d48003e9570ca09b713da677f5c3d2141a46b379394d",
    "V_OZON_SKU_CURRENT_TARIFF": "a155684596e703c6e90c40cf8c65bb6da1b3431d40125c990581f3de92530d80",
    "V_OZON_TARIFF_CHANGE_LOG": "47963b384ae9b88d15da7eba5ac8816545218741021d001293094e1d32c81e54",
    "V_OZON_LIFETIME_PNL": "7f415533dc84ae5435e6eb2e25e7077e90b4870c55efe7afd87bf790740e9ee3",
    "V_OZON_SKU_UNIT_ECONOMICS_CURRENT": "d24a4bffae4044a94a236cc874854bb3ccd7c216511e5665833ad8d7111e8309",
    "V_OZON_TARIFF_SOURCE_HEALTH": "5260fc22e1ac82931960c5ebe770d5de7ba0814e23c0e19e2adf1dd7b2e9f384",
    "V_OZON_SKU_FORWARD_ECONOMICS_CURRENT": "21bbf65669b3dda8aba6307ebb7a5a01df157c17b3565fe26f89f52676db7483",
    "V_OZON_AGENT_DECISION_INPUT": "50f364fcd72b24cdec5c4d1e81afac6fd5858df89db52e7e59f1781652ed8ea9",
    "V_OZON_SKU_FBO_FBS_COMPARISON_CURRENT": "51bd07535bc040458c78fa54af5b72c1b604142255b4e096bb545be43c6267cf",
}
API_TYPE = {"INT64": "INTEGER", "FLOAT64": "FLOAT", "BOOL": "BOOLEAN"}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------ fixtures

def manifest(root):
    return json.loads((root / "sql/current/ozon_mart/MANIFEST.json").read_text(encoding="utf-8"))


def save_manifest(root, man):
    (root / "sql/current/ozon_mart/MANIFEST.json").write_text(json.dumps(man, ensure_ascii=False, indent=2) + "\n",
                                                             encoding="utf-8")


def stored_parts(root, name):
    """What BigQuery stores for a canonical file: body trimmed without the final ';' and raw description."""
    text = (root / f"sql/current/ozon_mart/{name}.sql").read_text(encoding="utf-8")
    body = text.split("\nAS\n", 1)[1].strip()
    body = body[:-1] if body.endswith(";") else body
    desc = re.search(r'^OPTIONS \(description = "(.*)"\)$', text, re.M).group(1)
    return body, desc


def api_table(root, obj, etag=None):
    body, desc = stored_parts(root, obj["object_name"])
    fields = []
    for c in obj["canonical_schema"]:
        f = {"name": c["column_name"], "type": API_TYPE.get(c["data_type"], c["data_type"])}
        if c["is_nullable"] == "NO":
            f["mode"] = "REQUIRED"
        fields.append(f)
    return {"kind": "bigquery#table", "id": f"{P}:{DS}.{obj['object_name']}",
            "tableReference": {"projectId": P, "datasetId": DS, "tableId": obj["object_name"]},
            "type": "VIEW", "etag": etag or f"etag-{obj['object_name']}", "lastModifiedTime": "1788525349533",
            "creationTime": "1788525349533", "view": {"query": body, "useLegacySql": False},
            "schema": {"fields": fields}, "description": desc, "location": "EU"}


class FakeTransport:
    """url -> response. A response is a dict (200 JSON), (status, body), an Exception, or a list of these
    consumed one per call (the last one repeats)."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url):
        self.calls.append(("GET", url))
        key = url.split("?")[0]
        r = self.responses.get(key, (404, b""))
        if isinstance(r, list):
            n = sum(1 for _, u in self.calls if u.split("?")[0] == key) - 1
            r = r[min(n, len(r) - 1)]
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):
            return r
        return 200, json.dumps(r).encode()


def live_responses(root, extra_tables=()):
    man = manifest(root)
    resp = {f"{BASE}/{o['object_name']}": api_table(root, o) for o in man["objects"]}
    listed = [{"tableReference": {"projectId": P, "datasetId": DS, "tableId": o["object_name"]}, "type": "VIEW"}
              for o in man["objects"]]
    listed += [{"tableReference": {"projectId": P, "datasetId": DS, "tableId": n}, "type": t} for n, t in extra_tables]
    resp[BASE] = {"kind": "bigquery#tableList", "tables": listed, "totalItems": len(listed)}
    return resp


def fake_git(sha=SHA, dirty=False, reachable=0, head_ok=True):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        assert argv[0] == "git" and kwargs.get("shell") is False
        sub = argv[3]
        if sub == "rev-parse":
            return subprocess.CompletedProcess(argv, 0 if head_ok else 128, sha + "\n" if head_ok else "", "")
        if sub == "status":
            return subprocess.CompletedProcess(argv, 0, " M x.sql\n" if dirty else "", "")
        if sub == "merge-base":
            return subprocess.CompletedProcess(argv, reachable, "", "")
        raise AssertionError(argv)
    runner.calls = calls
    return runner


def args_for(root, **kw):
    base = dict(project=P, dataset=None, root=str(root), token_env="BQ_VERIFY_ACCESS_TOKEN", token_command=None,
                api_host=HOST, timeout=5.0, output=None)
    base.update(kw)
    return argparse.Namespace(**base)


def verify(root, responses=None, runner=None, environ=None, **kw):
    transport = FakeTransport(responses if responses is not None else live_responses(root))
    report, text = vl.run(args_for(root, **kw), transport_factory=lambda tok: transport,
                          environ={"BQ_VERIFY_ACCESS_TOKEN": TOKEN} if environ is None else environ,
                          runner=runner or fake_git())
    assert TOKEN not in text
    return report, text, transport


def by_name(report):
    return {o["object"]: o for o in report["objects"]}


@pytest.fixture
def root(tmp_path):
    """The R2A baseline these tests were written against: one managed dataset (ozon_mart) holding exactly the
    the objects whose hashes are pinned in BASELINE_BODY_SHA256. Everything the repository added or changed later
    (SCALE 1, and the Gate 5K CIS buyout model) is removed from the COPY — pending behaviour is exercised
    here by make_pending(), not by the repository's current state."""
    shutil.copytree(REPO / "sql", tmp_path / "sql")
    for ds_dir in sorted((tmp_path / "sql/current").iterdir()):
        if ds_dir.is_dir() and ds_dir.name != DS:
            shutil.rmtree(ds_dir)
    man = manifest(tmp_path)
    for o in [o for o in man["objects"] if o["object_name"] not in BASELINE_BODY_SHA256]:
        (tmp_path / o["canonical_path"]).unlink()
        man["objects"].remove(o)
        man["rebuild_order"].remove(o["object_name"])
    # The fixture is a DEPLOYED baseline: anything the repository currently has pending is normalised to
    # captured_live here, so pending behaviour is exercised only where make_pending() asks for it.
    for o in man["objects"]:
        o["sync_state"] = "captured_live"
        o["canonical_schema_verification"] = "bigquery_verified"
        o["capture_main_sha"] = o["capture_main_sha"] or SHA
        o["captured_at"] = o["captured_at"] or "2026-09-21T13:00:00Z"
        o["live_body_sha256_at_capture"] = o["canonical_body_sha256"]
        o["live_schema_sha256_at_capture"] = o["canonical_schema_sha256"]
        o["live_description_sha256_at_capture"] = o["canonical_description_sha256"]
        o["live_schema_at_capture"] = o["canonical_schema"]
    save_manifest(tmp_path, man)
    return tmp_path


def make_pending(root, name, body, cols, description="pending fixture"):
    """Git-first change of one object: new canonical file + manifest, live capture fields untouched."""
    text = (f"-- fixture\nCREATE OR REPLACE VIEW `{P}.{DS}.{name}`\nOPTIONS (description = \"{description}\")\n"
            f"AS\n{body};\n")
    (root / f"sql/current/ozon_mart/{name}.sql").write_text(text, encoding="utf-8")
    man = manifest(root)
    o = next(x for x in man["objects"] if x["object_name"] == name)
    o.update(sync_state="pending_deploy", canonical_schema_verification="unverified",
             canonical_body_sha256=sha(body.strip()), canonical_description_sha256=sha(description),
             canonical_schema=cols, canonical_column_count=len(cols),
             canonical_schema_sha256=sha(json.dumps(cols, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
             dependencies=[], external_dependencies=[])
    save_manifest(root, man)
    return o


PENDING_COLS = [{"column_name": "a", "data_type": "INT64", "is_nullable": "YES", "ordinal_position": 1}]


# ------------------------------------------------------------------------------------------ positive

def test_all_baseline_objects_match_on_real_repository(root):
    report, _, transport = verify(root)
    assert report["overall"] == "MATCH", json.dumps(report, indent=1)[:3000]
    assert report["summary"] == {"MATCH": len(BASELINE_BODY_SHA256)}
    assert report["unexpected_live"] == [] and report["dataset_unproven"] == []
    assert all(o["etag_stable"] for o in report["objects"])
    assert report["proposed_capture"] == {}
    assert report["canonical_git_sha"] == SHA and report["git_provenance_verified"] is True
    assert all(method == "GET" for method, _ in transport.calls)
    assert len(transport.calls) == 2 + len(BASELINE_BODY_SHA256) * 2 == report["requests"]


def test_live_hash_reuses_r2b_canonical_hash_v1_for_all_baseline_objects(root):
    man = manifest(root)
    assert {o["object_name"] for o in man["objects"]} == set(BASELINE_BODY_SHA256)
    for o in man["objects"]:
        body, _ = stored_parts(root, o["object_name"])
        assert vl.live_body_sha256(P, DS, o["object_name"], body) == BASELINE_BODY_SHA256[o["object_name"]]
        # BigQuery trims the body and drops one ';' on storage; the hash must be insensitive to exactly that
        assert vl.live_body_sha256(P, DS, o["object_name"], "\n " + body + ";\n") == BASELINE_BODY_SHA256[o["object_name"]]


def test_hash_code_is_imported_not_duplicated():
    src = (REPO / "tools/verify_current_sql_live.py").read_text(encoding="utf-8")
    assert "contract.view_body" in src and "contract.sha256_text" in src and "contract.schema_sha256" in src
    assert "hashlib" not in src


def test_output_is_deterministic_apart_from_timestamps(root):
    a, _, _ = verify(root)
    b, _, _ = verify(root)
    for r in (a, b):
        r.pop("captured_at_start"), r.pop("captured_at_end")
    assert a == b


# ------------------------------------------------------------------------------------------ drift

def mutate(root, name, fn, responses=None):
    responses = responses or live_responses(root)
    fn(responses[f"{BASE}/{name}"])
    return responses


def test_body_drift(root):
    r = mutate(root, LEAF, lambda t: t["view"].update(query=t["view"]["query"] + "\n-- changed"))
    report, _, _ = verify(root, r)
    o = by_name(report)[LEAF]
    assert (o["status"], o["drift_kinds"], o["body_status"]) == ("BODY_DRIFT", ["BODY_DRIFT"], "DRIFT")
    assert report["overall"] == "DRIFT" and vl.EXIT[report["overall"]] == 1


def test_schema_drift_with_column_level_diff(root):
    r = mutate(root, LEAF, lambda t: t["schema"]["fields"][0].update(type="STRING"))
    report, _, _ = verify(root, r)
    o = by_name(report)[LEAF]
    assert o["status"] == "SCHEMA_DRIFT" and o["schema_status"] == "DRIFT"
    assert o["schema_diff"][0]["ordinal_position"] == 1 and o["schema_diff"][0]["live"]["data_type"] == "STRING"


def test_required_column_is_schema_drift(root):
    r = mutate(root, LEAF, lambda t: t["schema"]["fields"][0].update(mode="REQUIRED"))
    assert by_name(verify(root, r)[0])[LEAF]["status"] == "SCHEMA_DRIFT"


def test_description_drift(root):
    r = mutate(root, LEAF, lambda t: t.update(description=t["description"] + " (edited in console)"))
    o = by_name(verify(root, r)[0])[LEAF]
    assert (o["status"], o["description_status"]) == ("DESCRIPTION_DRIFT", "DRIFT")


def test_missing_description_is_drift(root):
    r = mutate(root, LEAF, lambda t: t.pop("description"))
    assert by_name(verify(root, r)[0])[LEAF]["status"] == "DESCRIPTION_DRIFT"


def test_missing_live(root):
    r = live_responses(root)
    r[f"{BASE}/{LEAF}"] = (404, b'{"error": {"message": "Not found: Table ..."}}')
    report, _, _ = verify(root, r)
    assert by_name(report)[LEAF]["status"] == "MISSING_LIVE" and report["overall"] == "DRIFT"


@pytest.mark.parametrize("live_type", ["TABLE", "MATERIALIZED_VIEW", "EXTERNAL", "SNAPSHOT"])
def test_wrong_object_type(root, live_type):
    def change(t):
        t["type"] = live_type
        t.pop("view")
    report, _, _ = verify(root, mutate(root, LEAF, change))
    assert by_name(report)[LEAF]["status"] == "OBJECT_TYPE_DRIFT" and report["overall"] == "DRIFT"


def test_legacy_sql_view_is_type_drift(root):
    report, _, _ = verify(root, mutate(root, LEAF, lambda t: t["view"].update(useLegacySql=True)))
    assert by_name(report)[LEAF]["status"] == "OBJECT_TYPE_DRIFT"


@pytest.mark.parametrize("live_type", ["TABLE", "VIEW", "MATERIALIZED_VIEW"])
def test_unexpected_live_object(root, live_type):
    report, _, _ = verify(root, live_responses(root, extra_tables=[("V_OZON_ROGUE", live_type)]))
    assert report["unexpected_live"] == [{"dataset": DS, "object": "V_OZON_ROGUE", "live_type": live_type,
                                          "status": "UNEXPECTED_LIVE"}]
    assert report["overall"] == "DRIFT" and report["summary"]["UNEXPECTED_LIVE"] == 1
    assert report["summary"]["MATCH"] == len(BASELINE_BODY_SHA256)


def test_multiple_drift_kinds_and_precedence(root):
    def change(t):
        t["view"]["query"] += " "  + "-- x"
        t["schema"]["fields"][0]["name"] = "renamed"
        t["description"] = "other"
    o = by_name(verify(root, mutate(root, LEAF, change))[0])[LEAF]
    assert o["drift_kinds"] == ["BODY_DRIFT", "SCHEMA_DRIFT", "DESCRIPTION_DRIFT"]
    assert o["status"] == "BODY_DRIFT"


def test_drift_outranks_unproven_for_overall(root):
    r = mutate(root, LEAF, lambda t: t["view"].update(query="SELECT 1"))
    r[f"{BASE}/V_OZON_MART_FRESHNESS"] = (500, b"")
    report, _, _ = verify(root, r)
    assert report["overall"] == "DRIFT"


# ------------------------------------------------------------------------------------------ unproven

@pytest.mark.parametrize("response,code", [
    ((403, b'{"error": {"message": "Access Denied: secret detail user@example.com"}}'), "object:HTTP_403"),
    ((500, b"internal"), "object:HTTP_500"),
    (TimeoutError("timed out"), "object:TIMEOUT"),
    (ConnectionResetError("reset"), "object:NETWORK"),
    ((200, b"not json"), "object:MALFORMED_RESPONSE"),
    ((200, b"[1, 2]"), "object:MALFORMED_RESPONSE"),
])
def test_fetch_failures_are_unproven(root, response, code):
    r = live_responses(root)
    r[f"{BASE}/{LEAF}"] = response
    report, text, _ = verify(root, r)
    o = by_name(report)[LEAF]
    assert o["status"] == "METADATA_UNPROVEN" and o["unproven"] == [code]
    assert report["overall"] == "UNPROVEN" and vl.EXIT["UNPROVEN"] == 2
    assert "Access Denied" not in text and "user@example.com" not in text


@pytest.mark.parametrize("change", [
    lambda t: t.pop("tableReference"),
    lambda t: t["tableReference"].update(tableId="OTHER"),
    lambda t: t.pop("etag"),
    lambda t: t.update(lastModifiedTime="yesterday"),
    lambda t: t["view"].pop("useLegacySql"),
    lambda t: t["view"].update(query=None),
])
def test_malformed_object_metadata_is_unproven(root, change):
    o = by_name(verify(root, mutate(root, LEAF, change))[0])[LEAF]
    assert o["status"] == "METADATA_UNPROVEN"


@pytest.mark.parametrize("field", ["etag", "lastModifiedTime"])
def test_object_modified_during_capture(root, field):
    r = live_responses(root)
    first = r[f"{BASE}/{LEAF}"]
    second = copy.deepcopy(first)
    second[field] = "etag-changed" if field == "etag" else "1788525349999"
    r[f"{BASE}/{LEAF}"] = [first, second]
    report, _, _ = verify(root, r)
    o = by_name(report)[LEAF]
    assert o["status"] == "METADATA_UNPROVEN" and o["etag_stable"] is False
    assert "object:MODIFIED_DURING_CAPTURE" in o["unproven"] and report["overall"] == "UNPROVEN"


def test_object_modified_during_capture_hides_no_drift_as_match(root):
    r = mutate(root, LEAF, lambda t: t["view"].update(query="SELECT 2"))
    first = r[f"{BASE}/{LEAF}"]
    r[f"{BASE}/{LEAF}"] = [first, dict(first, etag="moved")]
    o = by_name(verify(root, r)[0])[LEAF]
    assert o["status"] == "METADATA_UNPROVEN" and o["drift_kinds"] == ["BODY_DRIFT"]


@pytest.mark.parametrize("field,code", [
    ({"name": "s", "type": "RECORD", "fields": [{"name": "x", "type": "INTEGER"}]}, "schema:UNSUPPORTED_SCHEMA_STRUCT"),
    ({"name": "s", "type": "STRUCT"}, "schema:UNSUPPORTED_SCHEMA_STRUCT"),
    ({"name": "a", "type": "INTEGER", "mode": "REPEATED"}, "schema:UNSUPPORTED_SCHEMA_REPEATED"),
    ({"name": "n", "type": "NUMERIC", "precision": "10", "scale": "2"}, "schema:UNSUPPORTED_SCHEMA_FORM"),
    ({"name": "s", "type": "STRING", "maxLength": "10"}, "schema:UNSUPPORTED_SCHEMA_FORM"),
    ({"name": "g", "type": "GEOGRAPHY"}, "schema:UNSUPPORTED_SCHEMA_TYPE"),
    ({"name": "j", "type": "JSON"}, "schema:UNSUPPORTED_SCHEMA_TYPE"),
    ({"name": "b", "type": "BIGNUMERIC"}, "schema:UNSUPPORTED_SCHEMA_TYPE"),
    ({"name": "x", "type": "INTEGER", "mode": "WEIRD"}, "schema:UNSUPPORTED_SCHEMA_TYPE"),
    ({"type": "INTEGER"}, "schema:MALFORMED_SCHEMA"),
])
def test_unsupported_schema_forms_are_unproven_not_flattened(root, field, code):
    r = mutate(root, LEAF, lambda t: t["schema"]["fields"].append(field))
    o = by_name(verify(root, r)[0])[LEAF]
    assert o["status"] == "METADATA_UNPROVEN" and code in o["unproven"] and o["schema_status"] == "UNPROVEN"


def test_list_failure_is_dataset_unproven(root):
    r = live_responses(root)
    r[BASE] = (403, b"")
    report, _, _ = verify(root, r)
    assert report["dataset_unproven"] == [{"dataset": DS, "code": "LIST_HTTP_403"}]
    assert report["summary"] == {"MATCH": len(BASELINE_BODY_SHA256)} and report["overall"] == "UNPROVEN"


def _listing(root, extra=()):
    return live_responses(root, extra_tables=extra)[BASE]


def _two_snapshots(root, t1_extra, t2_extra):
    """tables.list answers T1 on the first call and T2 on the final re-read; objects are stable."""
    r = live_responses(root)
    r[BASE] = [_listing(root, t1_extra), _listing(root, t2_extra)]
    return r


def test_list_stable_canonical_is_authoritative(root):
    report, _, _ = verify(root, _two_snapshots(root, [], []))
    assert report["unexpected_live"] == [] and report["dataset_unproven"] == []
    assert report["overall"] == "MATCH" and vl.EXIT["MATCH"] == 0


def test_list_stable_unexpected_object_is_drift(root):
    x = [("V_OZON_X", "TABLE")]
    report, _, _ = verify(root, _two_snapshots(root, x, x))
    assert report["unexpected_live"] == [{"dataset": DS, "object": "V_OZON_X", "live_type": "TABLE",
                                          "status": "UNEXPECTED_LIVE"}]
    assert report["dataset_unproven"] == []
    assert report["overall"] == "DRIFT" and vl.EXIT["DRIFT"] == 1


@pytest.mark.parametrize("t1,t2", [
    ([("V_OZON_X", "TABLE")], []),                            # transient disappearance
    ([], [("V_OZON_X", "TABLE")]),                            # transient appearance
    ([("V_OZON_X", "TABLE")], [("V_OZON_X", "VIEW")]),        # same name, type changed between snapshots
], ids=["disappearance", "appearance", "type-change"])
def test_list_unstable_is_unproven_never_drift(root, t1, t2):
    report, _, _ = verify(root, _two_snapshots(root, t1, t2))
    assert report["unexpected_live"] == []
    assert report["dataset_unproven"] == [{"dataset": DS, "code": "LIST_CHANGED_DURING_CAPTURE"}]
    assert report["summary"] == {"MATCH": len(BASELINE_BODY_SHA256)}
    assert report["overall"] == "UNPROVEN" and vl.EXIT["UNPROVEN"] == 2


def test_list_transient_cases_are_symmetric(root):
    x = [("V_OZON_X", "TABLE")]
    a, _, _ = verify(root, _two_snapshots(root, x, []))
    b, _, _ = verify(root, _two_snapshots(root, [], x))
    strip = lambda r: {k: v for k, v in r.items() if k not in ("captured_at_start", "captured_at_end")}  # noqa: E731
    assert strip(a) == strip(b)


def test_list_pagination(root):
    r = live_responses(root)
    tables = r[BASE]["tables"]
    pages = [{"tables": tables[:5], "nextPageToken": "tok-2"}, {"tables": tables[5:]}]
    calls = []

    class Paged(FakeTransport):
        def get(self, url):
            if url.split("?")[0] == BASE:
                calls.append(url)
                self.calls.append(("GET", url))
                return 200, json.dumps(pages[1] if "pageToken=tok-2" in url else pages[0]).encode()
            return super().get(url)
    transport = Paged(r)
    report, _ = vl.run(args_for(root), transport_factory=lambda tok: transport,
                       environ={"BQ_VERIFY_ACCESS_TOKEN": TOKEN}, runner=fake_git())
    assert report["overall"] == "MATCH" and any("pageToken=tok-2" in c for c in calls)


# ------------------------------------------------------------------------------------------ pending

def test_pending_not_deployed(root):
    responses = live_responses(root)            # production still has the old (captured) definition
    make_pending(root, LEAF, "SELECT 1 AS a", PENDING_COLS)
    report, _, _ = verify(root, responses)
    o = by_name(report)[LEAF]
    assert o["status"] == "PENDING_NOT_DEPLOYED"
    assert {o["body_status"], o["schema_status"], o["description_status"]} == {"LIVE_CAPTURE_MATCH"}
    assert report["overall"] == "PENDING" and vl.EXIT["PENDING"] == 0
    assert report["proposed_capture"] == {}


def test_pending_deployed_match_and_proposed_capture(root):
    make_pending(root, LEAF, "SELECT 1 AS a", PENDING_COLS)
    responses = live_responses(root)            # production now has the new canonical definition
    report, _, _ = verify(root, responses)
    o = by_name(report)[LEAF]
    assert o["status"] == "PENDING_DEPLOYED_MATCH" and report["overall"] == "PENDING"
    cap = report["proposed_capture"][f"{DS}.{LEAF}"]
    assert cap == {"sync_state": "captured_live", "canonical_schema_verification": "bigquery_verified",
                   "capture_main_sha": SHA, "captured_at": report["captured_at_start"],
                   "live_body_sha256_at_capture": sha("SELECT 1 AS a"),
                   "live_schema_sha256_at_capture": o["canonical_schema_sha256"],
                   "live_description_sha256_at_capture": sha("pending fixture"),
                   "live_schema_at_capture": PENDING_COLS}
    assert list(report["proposed_capture"]) == [f"{DS}.{LEAF}"]


def test_manifest_is_never_modified(root):
    make_pending(root, LEAF, "SELECT 1 AS a", PENDING_COLS)
    before = (root / "sql/current/ozon_mart/MANIFEST.json").read_bytes()
    verify(root, live_responses(root))
    assert (root / "sql/current/ozon_mart/MANIFEST.json").read_bytes() == before


def test_pending_matching_neither_side_is_drift(root):
    responses = live_responses(root)
    make_pending(root, LEAF, "SELECT 1 AS a", PENDING_COLS)
    mutate(root, LEAF, lambda t: t["view"].update(query="SELECT 3 AS a"), responses)
    report, _, _ = verify(root, responses)
    o = by_name(report)[LEAF]
    assert o["status"] == "BODY_DRIFT" and report["overall"] == "DRIFT" and report["proposed_capture"] == {}


def test_pending_half_deployed_is_drift(root):
    make_pending(root, LEAF, "SELECT 1 AS a", PENDING_COLS)
    responses = live_responses(root)
    mutate(root, LEAF, lambda t: t.update(description="old description"), responses)
    o = by_name(verify(root, responses)[0])[LEAF]
    assert o["status"] == "DESCRIPTION_DRIFT" and o["body_status"] == "MATCH"


def test_pending_with_unproven_component_is_unproven(root):
    make_pending(root, LEAF, "SELECT 1 AS a", PENDING_COLS)
    responses = mutate(root, LEAF, lambda t: t["schema"]["fields"].append({"name": "r", "type": "RECORD"}),
                       live_responses(root))
    report, _, _ = verify(root, responses)
    assert by_name(report)[LEAF]["status"] == "METADATA_UNPROVEN" and report["proposed_capture"] == {}


# ------------------------------------------------------------------------------------------ git provenance

@pytest.mark.parametrize("runner,notes", [
    (fake_git(dirty=True), ["working tree is dirty"]),
    (fake_git(reachable=1), ["HEAD is not reachable from origin/main"]),
    (fake_git(reachable=128), ["origin/main unavailable (fetch it first)"]),
    (fake_git(head_ok=False), ["git HEAD unavailable"]),
])
def test_unverified_provenance_blocks_proposed_capture(root, runner, notes):
    make_pending(root, LEAF, "SELECT 1 AS a", PENDING_COLS)
    report, _, _ = verify(root, live_responses(root), runner=runner)
    assert by_name(report)[LEAF]["status"] == "PENDING_DEPLOYED_MATCH"      # comparison still runs (diagnostic)
    assert report["git_provenance_verified"] is False and report["git_provenance_notes"] == notes
    assert report["proposed_capture"] == {}


def test_clean_reachable_commit_is_verified_and_labelled(root):
    report, _, _ = verify(root)
    assert report["git_provenance_verified"] is True and report["canonical_git_sha"] == SHA
    assert "NOT proof of the deployed commit" in report["canonical_git_sha_meaning"]
    assert "git_commit" not in report


def test_git_runner_uses_fixed_argv_without_shell(root):
    runner = fake_git()
    verify(root, runner=runner)
    assert [a[3] for a, _ in runner.calls] == ["rev-parse", "status", "merge-base"]
    for argv, kw in runner.calls:
        assert argv[:3] == ["git", "-C", str(root.resolve())] and kw["shell"] is False


def test_git_provenance_against_real_repository():
    sha, verified, notes = vl.git_provenance(REPO, subprocess.run)
    assert sha is None or re.fullmatch(r"[0-9a-f]{40}", sha)
    assert verified is (not notes)


# ------------------------------------------------------------------------------------------ network boundary

@pytest.fixture
def guard():
    return vl.RequestGuard(HOST, P, {DS: [LEAF]})


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "get"])
def test_only_get_is_allowed(guard, method):
    with pytest.raises(vl.ToolError, match="GET only"):
        guard.check(method, f"{BASE}/{LEAF}")


@pytest.mark.parametrize("url", [
    f"http://{HOST}/bigquery/v2/projects/{P}/datasets/{DS}/tables/{LEAF}",
    f"https://evil.example.com/bigquery/v2/projects/{P}/datasets/{DS}/tables/{LEAF}",
    f"https://{HOST}:8443/bigquery/v2/projects/{P}/datasets/{DS}/tables/{LEAF}",
    f"https://user:pw@{HOST}/bigquery/v2/projects/{P}/datasets/{DS}/tables/{LEAF}",
    f"https://{HOST}/bigquery/v2/projects/{P}/datasets/{DS}/tables/V_OZON_NOT_MANAGED",
    f"https://{HOST}/bigquery/v2/projects/{P}/datasets/wb_mart/tables",
    f"https://{HOST}/bigquery/v2/projects/other-project/datasets/{DS}/tables/{LEAF}",
    f"https://{HOST}/bigquery/v2/projects/{P}/datasets/{DS}/tables/{LEAF}/data",
    f"https://{HOST}/bigquery/v2/projects/{P}/queries",
    f"https://{HOST}/bigquery/v2/projects/{P}/jobs",
    f"https://{HOST}/bigquery/v2/projects/{P}/datasets/{DS}/tables/../../../queries",
    f"https://{HOST}/bigquery/v2/projects/{P}/datasets/{DS}/tables/{LEAF}?fields=view",
    f"https://{HOST}/bigquery/v2/projects/{P}/datasets/{DS}/tables?maxResults=1&selectedFields=x",
    f"https://{HOST}/bigquery/v2/projects/{P}/datasets/{DS}/tables/{LEAF}#frag",
])
def test_unexpected_targets_fail_closed(guard, url):
    with pytest.raises(vl.ToolError):
        guard.check("GET", url)


def test_allowed_targets(guard):
    guard.check("GET", f"{BASE}/{LEAF}")
    guard.check("GET", f"{BASE}?maxResults=1000&pageToken=abc")


def test_unknown_api_host_refused():
    with pytest.raises(vl.ToolError):
        vl.RequestGuard("evil.example.com", P, {DS: [LEAF]})


def test_policy_violation_is_error_and_nothing_is_fetched(root, monkeypatch):
    real = vl.list_tables
    monkeypatch.setattr(vl, "list_tables", lambda client, url: real(client, url.replace("/datasets/", "/jobs/../datasets/")))
    report, _, transport = verify(root)
    assert report["overall"] == "ERROR" and report["error"]["code"] == "NETWORK_POLICY" and transport.calls == []


def test_project_must_match_manifest_and_manifest_cannot_redirect(root):
    report, _, transport = verify(root, project="other-project-123")
    assert report["overall"] == "ERROR" and report["error"]["code"] == "CONFIG" and transport.calls == []


def test_invalid_repository_contract_is_error_before_network(root):
    man = manifest(root)
    man["project"] = "evil-project-99"          # would retarget every URL; R2B contract rejects it
    save_manifest(root, man)
    report, _, transport = verify(root, project="evil-project-99")
    assert report["overall"] == "ERROR" and report["error"]["code"] == "CONTRACT" and transport.calls == []


def test_unmanaged_dataset_option_is_error(root):
    report, _, transport = verify(root, dataset=["wb_mart"])
    assert report["overall"] == "ERROR" and transport.calls == []


def test_real_transport_refuses_redirects():
    handler = vl._NoRedirect()
    with pytest.raises(vl.FetchError):
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example.com/")


def test_real_transport_sends_get_only(monkeypatch):
    seen = []

    class Opener:
        def open(self, req, timeout):
            seen.append((req.get_method(), req.full_url, req.get_header("Authorization")))
            raise TimeoutError("timed out")
    t = vl.UrllibTransport(TOKEN, 1)
    t._opener = Opener()
    with pytest.raises(vl.FetchError) as e:
        t.get(f"{BASE}/{LEAF}")
    assert e.value.code == "TIMEOUT" and seen[0][0] == "GET"


# ------------------------------------------------------------------------------------------ credentials

def test_token_from_env_and_never_in_output(root):
    report, text, _ = verify(root)
    assert TOKEN not in text and TOKEN not in json.dumps(report)


def test_missing_token_is_error_without_network(root):
    report, _, transport = verify(root, environ={})
    assert report["overall"] == "ERROR" and report["error"]["code"] == "AUTH" and transport.calls == []


def test_http_401_is_error(root):
    r = live_responses(root)
    r[BASE] = (401, b'{"error": "invalid token ' + TOKEN.encode() + b'"}')
    report, text, _ = verify(root, r)
    assert report["overall"] == "ERROR" and report["error"]["code"] == "AUTH" and TOKEN not in text


def test_no_implicit_subprocess_for_credentials(root, monkeypatch):
    calls = []

    def recorder(argv, **kw):
        calls.append(argv)
        return fake_git()(argv, **kw)
    monkeypatch.setattr(subprocess, "run", recorder)
    transport = FakeTransport(live_responses(root))
    report, _ = vl.run(args_for(root), transport_factory=lambda tok: transport,
                       environ={"BQ_VERIFY_ACCESS_TOKEN": TOKEN}, runner=recorder)
    assert report["overall"] == "MATCH"
    assert all(argv[0] == "git" for argv in calls) and not any("gcloud" in " ".join(a) for a in calls)


def test_default_runner_is_not_used_for_token_when_env_given(root, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess used")
    monkeypatch.setattr(subprocess, "run", boom)
    transport = FakeTransport(live_responses(root))
    report, _ = vl.run(args_for(root), transport_factory=lambda tok: transport,
                       environ={"BQ_VERIFY_ACCESS_TOKEN": TOKEN}, runner=fake_git())
    assert report["overall"] == "MATCH"


def test_explicit_token_command_safe_argv(root):
    seen = []

    def runner(argv, **kw):
        if argv[0] == "git":
            return fake_git()(argv, **kw)
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, TOKEN + "\n", "")
    cmd = 'gcloud auth print-access-token --account "a b"; rm -rf /'
    report, text, _ = verify(root, runner=runner, environ={}, token_command=cmd)
    assert report["overall"] == "MATCH" and TOKEN not in text
    argv, kw = seen[0]
    assert argv == ["gcloud", "auth", "print-access-token", "--account", "a b;", "rm", "-rf", "/"]
    assert kw["shell"] is False and kw["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize("result,code", [
    (subprocess.CompletedProcess([], 1, "", "ERROR: secret-ish stderr " + TOKEN), "AUTH"),
    (subprocess.CompletedProcess([], 0, "", ""), "AUTH"),
    (subprocess.CompletedProcess([], 0, "not a token\nwith spaces", ""), "AUTH"),
    (FileNotFoundError("gcloud"), "AUTH"),
])
def test_token_command_failures_are_redacted(root, result, code):
    def runner(argv, **kw):
        if argv[0] == "git":
            return fake_git()(argv, **kw)
        if isinstance(result, Exception):
            raise result
        return result
    report, text, transport = verify(root, runner=runner, environ={}, token_command="gcloud auth print-access-token")
    assert report["overall"] == "ERROR" and report["error"]["code"] == code and transport.calls == []
    assert "secret-ish" not in text and TOKEN not in text


def test_token_leak_in_output_is_suppressed(root, monkeypatch):
    monkeypatch.setattr(vl, "TOOL", TOKEN)       # simulate a bug that would echo the credential
    transport = FakeTransport(live_responses(root))
    report, text = vl.run(args_for(root), transport_factory=lambda tok: transport,
                          environ={"BQ_VERIFY_ACCESS_TOKEN": TOKEN}, runner=fake_git())
    assert TOKEN not in text and report["error"]["code"] == "REDACTION" and report["overall"] == "ERROR"


# ------------------------------------------------------------------------------------------ output / exit

def test_report_contains_no_sql_or_description_text(root):
    report, text, _ = verify(root, mutate(root, LEAF, lambda t: t["view"].update(query=t["view"]["query"] + " ")))
    for o in manifest(REPO)["objects"]:
        body, desc = stored_parts(REPO, o["object_name"])
        assert body[:60] not in text and desc[:40] not in text
    required = {"schema_version", "tool", "hash_contract", "canonical_git_sha", "git_provenance_verified", "project",
                "captured_at_start", "captured_at_end", "overall", "summary", "objects", "unexpected_live",
                "proposed_capture", "error", "mode"}
    assert required <= set(report) and report["mode"] == "metadata_only"
    assert {"status", "drift_kinds", "live_type", "live_last_modified", "etag_stable", "schema_diff"} <= set(report["objects"][0])


@pytest.mark.parametrize("scenario,overall,code", [
    ("match", "MATCH", 0), ("pending", "PENDING", 0), ("drift", "DRIFT", 1), ("unproven", "UNPROVEN", 2), ("error", "ERROR", 3),
])
def test_cli_exit_codes(root, tmp_path, scenario, overall, code, capsys):
    responses = live_responses(root)
    env = {"BQ_VERIFY_ACCESS_TOKEN": TOKEN}
    if scenario == "pending":
        make_pending(root, LEAF, "SELECT 1 AS a", PENDING_COLS)
    elif scenario == "drift":
        mutate(root, LEAF, lambda t: t.update(description="x"), responses)
    elif scenario == "unproven":
        responses[f"{BASE}/{LEAF}"] = (503, b"")
    elif scenario == "error":
        env = {}
    out = tmp_path / "report.json"
    transport = FakeTransport(responses)
    rc = vl.main(["--project", P, "--root", str(root), "--output", str(out)],
                 transport_factory=lambda tok: transport, environ=env, runner=fake_git())
    assert rc == code and json.loads(out.read_text())["overall"] == overall
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err and f"overall: {overall}" in captured.err


def test_tests_are_offline(monkeypatch, root):
    def no_network(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    report, _, _ = verify(root)
    assert report["overall"] == "MATCH"
