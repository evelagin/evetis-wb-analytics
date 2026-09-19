#!/usr/bin/env python3
"""Manual READ-ONLY production drift verifier for the canonical layer (sql/current/**). R2C phase 1.

Compares every object managed by sql/current/<dataset>/MANIFEST.json with the object BigQuery
currently stores, using table METADATA only (REST tables.list / tables.get): no query jobs, no
INFORMATION_SCHEMA, no dry runs, no table-data reads. It never writes to BigQuery, never edits the
manifest and never reconciles: drift is reported, classified and left for an owner decision.

Credentials are explicit: an access token from the environment variable named by --token-env
(default BQ_VERIFY_ACCESS_TOKEN) or from an operator-supplied --token-command. No command is run
implicitly for credentials. Git provenance is read with fixed `git` argv only.

canonical_git_sha in the report is the clean Git commit whose sql/current/** was COMPARED with
production during this capture. It is NOT proof of which commit was deployed.

Exit codes: 0 MATCH or PENDING; 1 DRIFT; 2 UNPROVEN; 3 ERROR.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_current_sql as contract  # noqa: E402  (R2B: canonical_hash_v1, parser, manifest contract)

SCHEMA_VERSION = 1
TOOL = "verify_current_sql_live"
DEFAULT_TOKEN_ENV = "BQ_VERIFY_ACCESS_TOKEN"
DEFAULT_API_HOST = "www.googleapis.com"   # bigquery.googleapis.com is blocked on the operator network
ALLOWED_API_HOSTS = {"www.googleapis.com", "bigquery.googleapis.com"}
API_PREFIX = "/bigquery/v2"
LIST_QUERY_KEYS = {"maxResults", "pageToken"}
MAX_LIST_PAGES = 50
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
TOKEN_CHARS = re.compile(r"[A-Za-z0-9._~+/=-]{20,4096}")

# BigQuery API schema form -> manifest (INFORMATION_SCHEMA) notation. Only forms proven equal in the
# R2C preflight (11/11 flat views) are listed; anything else is METADATA_UNPROVEN, never guessed.
API_TYPE_MAP = {
    "INTEGER": "INT64", "INT64": "INT64",
    "FLOAT": "FLOAT64", "FLOAT64": "FLOAT64",
    "BOOLEAN": "BOOL", "BOOL": "BOOL",
    "STRING": "STRING", "NUMERIC": "NUMERIC", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP",
}
API_MODE_MAP = {None: "YES", "NULLABLE": "YES", "REQUIRED": "NO"}
FIELD_KEYS_OK = {"name", "type", "mode", "description"}

# Object status precedence (most severe first). METADATA_UNPROVEN never collapses to MATCH.
PRECEDENCE = [
    "MISSING_LIVE", "OBJECT_TYPE_DRIFT", "BODY_DRIFT", "SCHEMA_DRIFT", "DESCRIPTION_DRIFT",
    "METADATA_UNPROVEN", "PENDING_DEPLOYED_MATCH", "PENDING_NOT_DEPLOYED", "MATCH",
]
DRIFT_STATUSES = {"MISSING_LIVE", "OBJECT_TYPE_DRIFT", "BODY_DRIFT", "SCHEMA_DRIFT", "DESCRIPTION_DRIFT"}
PENDING_STATUSES = {"PENDING_DEPLOYED_MATCH", "PENDING_NOT_DEPLOYED"}
EXIT = {"MATCH": 0, "PENDING": 0, "DRIFT": 1, "UNPROVEN": 2, "ERROR": 3}
COMPONENTS = ("body", "schema", "description")
KIND = {"body": "BODY_DRIFT", "schema": "SCHEMA_DRIFT", "description": "DESCRIPTION_DRIFT"}


class ToolError(Exception):
    """A failure that prevents a valid comparison (overall ERROR). Message is already sanitized."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class FetchError(Exception):
    """A per-request failure (sanitized: status code / category only, never the response body)."""

    def __init__(self, code, status=None):
        super().__init__(code)
        self.code = code
        self.status = status


# ---------------------------------------------------------------------------------------------
# Network boundary
# ---------------------------------------------------------------------------------------------

class RequestGuard:
    """Only HTTPS GET to the configured API host, and only to tables.list / tables.get paths built
    from the validated manifest. Every request passes through check() before any transport."""

    def __init__(self, host, project, datasets):
        if host not in ALLOWED_API_HOSTS:
            raise ToolError("CONFIG", f"API host {host!r} not in {sorted(ALLOWED_API_HOSTS)}")
        self.host = host
        self.list_paths = {}
        self.get_paths = set()
        for dataset, names in datasets.items():
            base = f"{API_PREFIX}/projects/{_q(project)}/datasets/{_q(dataset)}/tables"
            self.list_paths[base] = dataset
            self.get_paths.update(f"{base}/{_q(n)}" for n in names)

    def check(self, method, url):
        if method != "GET":
            raise ToolError("NETWORK_POLICY", f"HTTP method {method!r} refused (read-only verifier: GET only)")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.netloc != self.host or parts.username or parts.password or parts.fragment:
            raise ToolError("NETWORK_POLICY", "request target refused (scheme/host not allowed)")
        if parts.path in self.get_paths:
            if parts.query:
                raise ToolError("NETWORK_POLICY", "query string not allowed on tables.get")
            return
        if parts.path in self.list_paths:
            keys = {k for k, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)}
            if not keys <= LIST_QUERY_KEYS:
                raise ToolError("NETWORK_POLICY", "query parameter not allowed on tables.list")
            return
        raise ToolError("NETWORK_POLICY", "request path refused (not a managed tables.list/tables.get path)")


def _q(s):
    return urllib.parse.quote(s, safe="")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect could leave the allowlisted target
        raise FetchError("REDIRECT_REFUSED")


class UrllibTransport:
    """Real transport: GET only, no redirects, bounded response size. Returns (status, bytes)."""

    def __init__(self, token, timeout):
        self._token = token
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())

    def get(self, url):
        req = urllib.request.Request(url, method="GET", headers={
            "Authorization": "Bearer " + self._token, "Accept": "application/json"})
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                return resp.status, resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as e:
            return e.code, b""
        except (TimeoutError, OSError, urllib.error.URLError) as e:
            reason = getattr(e, "reason", e)
            raise FetchError("TIMEOUT" if isinstance(reason, TimeoutError) or "timed out" in str(reason) else "NETWORK")


class Client:
    def __init__(self, guard, transport):
        self.guard = guard
        self.transport = transport
        self.requests = 0

    def get_json(self, url):
        self.guard.check("GET", url)
        self.requests += 1
        try:
            status, body = self.transport.get(url)
        except FetchError:
            raise
        except TimeoutError:
            raise FetchError("TIMEOUT") from None
        except Exception:  # noqa: BLE001 - any transport failure is sanitized
            raise FetchError("NETWORK") from None
        if status == 401:
            raise ToolError("AUTH", "authentication failed (HTTP 401); check the supplied access token")
        if status != 200:
            raise FetchError(f"HTTP_{status}", status)
        if len(body) > MAX_RESPONSE_BYTES:
            raise FetchError("RESPONSE_TOO_LARGE")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise FetchError("MALFORMED_RESPONSE") from None
        if not isinstance(data, dict):
            raise FetchError("MALFORMED_RESPONSE")
        return data


# ---------------------------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------------------------

def obtain_token(args, environ, runner):
    if args.token_command:
        argv = shlex.split(args.token_command)
        if not argv:
            raise ToolError("AUTH", "--token-command is empty")
        try:
            res = runner(argv, shell=False, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            raise ToolError("AUTH", "token command could not be run") from None
        if res.returncode != 0:
            raise ToolError("AUTH", f"token command failed (exit {res.returncode}); output suppressed")
        token = (res.stdout or "").strip()
    else:
        token = (environ.get(args.token_env) or "").strip()
        if not token:
            raise ToolError("AUTH", f"no access token: set {args.token_env} or pass --token-command")
    if not TOKEN_CHARS.fullmatch(token):
        raise ToolError("AUTH", "access token has an unexpected format; value suppressed")
    return token


# ---------------------------------------------------------------------------------------------
# Git provenance
# ---------------------------------------------------------------------------------------------

def git_provenance(root, runner):
    """Returns (sha or None, verified, reasons). Fixed argv, no shell, no network (no fetch)."""
    def git(*args):
        try:
            res = runner(["git", "-C", str(root), *args], shell=False, capture_output=True, text=True,
                         timeout=30, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return res

    reasons = []
    head = git("rev-parse", "--verify", "HEAD")
    if head is None or head.returncode != 0:
        return None, False, ["git HEAD unavailable"]
    sha = head.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return None, False, ["git HEAD unreadable"]
    status = git("status", "--porcelain", "--untracked-files=normal")
    if status is None or status.returncode != 0:
        reasons.append("working tree state unknown")
    elif status.stdout.strip():
        reasons.append("working tree is dirty")
    anc = git("merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/main")
    if anc is None or anc.returncode not in (0, 1):
        reasons.append("origin/main unavailable (fetch it first)")
    elif anc.returncode == 1:
        reasons.append("HEAD is not reachable from origin/main")
    return sha, not reasons, reasons


# ---------------------------------------------------------------------------------------------
# Live metadata normalization
# ---------------------------------------------------------------------------------------------

@dataclass
class Live:
    exists: bool = False
    type: str | None = None
    legacy_sql: bool | None = None
    etag: str | None = None
    last_modified: int | None = None
    body_sha: str | None = None
    schema: list | None = None
    schema_sha: str | None = None
    description_sha: str | None = None
    unproven: list = field(default_factory=list)   # [(component, code)]


def live_body_sha256(project, dataset, name, query):
    """canonical_hash_v1 of a stored view body, through R2B's own view_body (no second implementation):
    the stored body is placed after a top-level AS exactly like a canonical file's body."""
    text = f"CREATE VIEW `{project}.{dataset}.{name}` AS\n{query}"
    return contract.sha256_text(contract.view_body(text, contract.tokenize(text)))


def normalize_schema(schema):
    """API schema -> manifest column list, or raise FetchError(code) when the form is not proven."""
    if not isinstance(schema, dict) or not isinstance(schema.get("fields"), list) or not schema["fields"]:
        raise FetchError("MALFORMED_SCHEMA")
    cols = []
    for i, f in enumerate(schema["fields"], start=1):
        if not isinstance(f, dict) or not isinstance(f.get("name"), str) or not isinstance(f.get("type"), str):
            raise FetchError("MALFORMED_SCHEMA")
        if f["type"] in ("RECORD", "STRUCT") or "fields" in f:
            raise FetchError("UNSUPPORTED_SCHEMA_STRUCT")
        if set(f) - FIELD_KEYS_OK:
            raise FetchError("UNSUPPORTED_SCHEMA_FORM")          # precision/scale/maxLength/...
        if f.get("mode") == "REPEATED":
            raise FetchError("UNSUPPORTED_SCHEMA_REPEATED")
        if f["type"] not in API_TYPE_MAP or f.get("mode") not in API_MODE_MAP:
            raise FetchError("UNSUPPORTED_SCHEMA_TYPE")
        cols.append({"column_name": f["name"], "data_type": API_TYPE_MAP[f["type"]],
                     "is_nullable": API_MODE_MAP[f.get("mode")], "ordinal_position": i})
    return cols


def read_object(client, url, project, dataset, name):
    live = Live()
    try:
        data = client.get_json(url)
    except FetchError as e:
        if e.status == 404:
            return live
        live.exists = None
        live.unproven.append(("object", e.code))
        return live
    live.exists = True
    ref = data.get("tableReference")
    if not isinstance(ref, dict) or (ref.get("projectId"), ref.get("datasetId"), ref.get("tableId")) != (project, dataset, name):
        live.unproven.append(("object", "MALFORMED_RESPONSE"))
        return live
    live.type = data.get("type") if isinstance(data.get("type"), str) else None
    live.etag = data.get("etag") if isinstance(data.get("etag"), str) else None
    live.last_modified = _millis(data.get("lastModifiedTime"))
    if live.type is None or live.etag is None or live.last_modified is None:
        live.unproven.append(("object", "MALFORMED_RESPONSE"))
        return live
    if live.type != "VIEW":
        return live
    view = data.get("view")
    if not isinstance(view, dict) or not isinstance(view.get("query"), str) or not isinstance(view.get("useLegacySql"), bool):
        live.unproven.append(("body", "MALFORMED_RESPONSE"))
    else:
        live.legacy_sql = view["useLegacySql"]
        try:
            live.body_sha = live_body_sha256(project, dataset, name, view["query"])
        except Exception:  # noqa: BLE001 - unhashable body is unproven, never MATCH
            live.unproven.append(("body", "BODY_NOT_HASHABLE"))
    try:
        live.schema = normalize_schema(data.get("schema"))
        live.schema_sha = contract.schema_sha256(live.schema)
    except FetchError as e:
        live.unproven.append(("schema", e.code))
    desc = data.get("description")
    if desc is None:
        live.description_sha = None          # absent description is a proven fact (drift if canonical has one)
    elif isinstance(desc, str):
        live.description_sha = contract.sha256_text(desc)
    else:
        live.unproven.append(("description", "MALFORMED_RESPONSE"))
    return live


def _millis(value):
    """lastModifiedTime is a decimal string of epoch milliseconds; kept exact for the stability check."""
    if not isinstance(value, str) or not value.isdigit():
        return None
    return int(value)


def _iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms // 1000)) if ms is not None else None


def list_tables(client, url):
    names, token = {}, None
    for _ in range(MAX_LIST_PAGES):
        q = {"maxResults": "1000"}
        if token:
            q["pageToken"] = token
        data = client.get_json(url + "?" + urllib.parse.urlencode(q))
        tables = data.get("tables", [])
        if not isinstance(tables, list):
            raise FetchError("MALFORMED_RESPONSE")
        for t in tables:
            tid = t.get("tableReference", {}).get("tableId") if isinstance(t, dict) else None
            if not isinstance(tid, str) or not contract.IDENT.fullmatch(tid):
                raise FetchError("MALFORMED_RESPONSE")
            names[tid] = t.get("type") if isinstance(t.get("type"), str) else "UNKNOWN"
        token = data.get("nextPageToken")
        if not token:
            return names
        if not isinstance(token, str) or len(token) > 2048:
            raise FetchError("MALFORMED_RESPONSE")
    raise FetchError("TOO_MANY_PAGES")


# ---------------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------------

def classify(obj, live1, live2):
    """Returns the per-object report entry (hashes and statuses only; no SQL, no description text)."""
    canon = {"body": obj["canonical_body_sha256"], "schema": obj["canonical_schema_sha256"],
             "description": obj["canonical_description_sha256"]}
    hist = {"body": obj["live_body_sha256_at_capture"], "schema": obj["live_schema_sha256_at_capture"],
            "description": obj["live_description_sha256_at_capture"]}
    pending = obj["sync_state"] == "pending_deploy"
    entry = {
        "dataset": obj["dataset"], "object": obj["object_name"], "sync_state": obj["sync_state"],
        "canonical_body_sha256": canon["body"], "canonical_schema_sha256": canon["schema"],
        "canonical_description_sha256": canon["description"],
        "live_type": live1.type, "live_legacy_sql": live1.legacy_sql, "live_last_modified": None,
        "live_body_sha256": None, "live_schema_sha256": None, "live_description_sha256": None,
        "body_status": "UNPROVEN", "schema_status": "UNPROVEN", "description_status": "UNPROVEN",
        "schema_diff": [], "etag_stable": None, "drift_kinds": [], "unproven": [], "status": None,
    }
    unproven = [f"{c}:{code}" for c, code in live1.unproven]
    if live1.exists is None or any(c == "object" for c, _ in live1.unproven):
        entry.update(status="METADATA_UNPROVEN", unproven=sorted(unproven))
        return entry
    if live1.exists is False:
        entry.update(status="MISSING_LIVE", drift_kinds=["MISSING_LIVE"],
                     body_status="MISSING", schema_status="MISSING", description_status="MISSING")
        return entry
    entry["live_last_modified"] = _iso(live1.last_modified)

    # Consistency capture: the final re-read must show the same object version.
    if live2 is None or live2.exists is not True or live2.etag != live1.etag or live2.last_modified != live1.last_modified:
        entry["etag_stable"] = False
        unproven.append("object:MODIFIED_DURING_CAPTURE")
    else:
        entry["etag_stable"] = True

    kinds = []
    if live1.type != "VIEW" or live1.legacy_sql is True:
        kinds.append("OBJECT_TYPE_DRIFT")
    if live1.type == "VIEW":
        observed = {"body": live1.body_sha, "schema": live1.schema_sha, "description": live1.description_sha}
        proven = {c: not any(u.startswith(c + ":") for u in unproven) for c in COMPONENTS}
        entry.update(live_body_sha256=live1.body_sha, live_schema_sha256=live1.schema_sha,
                     live_description_sha256=live1.description_sha)
        eq_canon = {c: proven[c] and observed[c] == canon[c] for c in COMPONENTS}
        eq_hist = {c: proven[c] and hist[c] is not None and observed[c] == hist[c] for c in COMPONENTS}
        for c in COMPONENTS:
            entry[f"{c}_status"] = ("UNPROVEN" if not proven[c] else "MATCH" if eq_canon[c]
                                    else "LIVE_CAPTURE_MATCH" if pending and eq_hist[c] else "DRIFT")
        if live1.schema is not None and not eq_canon["schema"]:
            entry["schema_diff"] = schema_diff(obj["canonical_schema"], live1.schema)
        all_proven = all(proven.values())
        if pending and all_proven and all(eq_canon.values()):
            pending_state = "PENDING_DEPLOYED_MATCH"
        elif pending and all_proven and all(eq_hist.values()):
            pending_state = "PENDING_NOT_DEPLOYED"
        else:
            pending_state = None
            # captured_live, or pending that matches neither side: drift is judged against canonical
            kinds += [KIND[c] for c in COMPONENTS if proven[c] and not eq_canon[c]]
    else:
        pending_state = None

    entry["drift_kinds"] = sorted(set(kinds), key=PRECEDENCE.index)
    entry["unproven"] = sorted(set(unproven))
    if not entry["etag_stable"]:
        status = "METADATA_UNPROVEN"       # a moving object is never MATCH, and observed drift is unreliable
    elif entry["drift_kinds"]:
        status = entry["drift_kinds"][0]
    elif entry["unproven"]:
        status = "METADATA_UNPROVEN"
    elif pending_state:
        status = pending_state
    elif pending:
        status = "METADATA_UNPROVEN"
    else:
        status = "MATCH"
    entry["status"] = status
    return entry


def schema_diff(canonical, live, limit=50):
    out = []
    for i in range(max(len(canonical), len(live))):
        c = canonical[i] if i < len(canonical) else None
        lv = live[i] if i < len(live) else None
        pick = (lambda x: None if x is None else {k: x[k] for k in ("column_name", "data_type", "is_nullable")})
        if pick(c) != pick(lv):
            out.append({"ordinal_position": i + 1, "canonical": pick(c), "live": pick(lv)})
        if len(out) >= limit:
            break
    return out


def overall_state(objects, unexpected, dataset_unproven):
    statuses = {o["status"] for o in objects}
    if statuses & DRIFT_STATUSES or unexpected:
        return "DRIFT"
    if "METADATA_UNPROVEN" in statuses or dataset_unproven:
        return "UNPROVEN"
    if statuses & PENDING_STATUSES:
        return "PENDING"
    return "MATCH"


# ---------------------------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------------------------

def load_managed(root, only_datasets):
    findings, _ = contract.validate(root)
    if findings:
        checks = sorted({f.check for f in findings})
        raise ToolError("CONTRACT", f"sql/current contract invalid ({len(findings)} finding(s): {', '.join(checks)}); "
                                    "run tools/validate_current_sql.py")
    managed = {}
    for mpath in sorted((root / "sql" / "current").glob(f"*/{contract.MANIFEST_NAME}")):
        man = json.loads(mpath.read_text(encoding="utf-8"))
        managed[man["dataset"]] = man
    if only_datasets:
        unknown = set(only_datasets) - set(managed)
        if unknown:
            raise ToolError("CONFIG", f"dataset(s) {sorted(unknown)} are not managed by sql/current")
        managed = {d: m for d, m in managed.items() if d in only_datasets}
    if not managed:
        raise ToolError("CONFIG", "no managed dataset")
    return managed


def run(args, transport_factory=None, environ=None, runner=subprocess.run, clock=time.gmtime):
    environ = os.environ if environ is None else environ
    report = {"schema_version": SCHEMA_VERSION, "tool": TOOL, "hash_contract": "canonical_hash_v1",
              "mode": "metadata_only", "project": args.project, "canonical_git_sha": None,
              "git_provenance_verified": False, "git_provenance_notes": [],
              "canonical_git_sha_meaning": "clean Git commit whose sql/current/** was compared with production "
                                           "during this capture; NOT proof of the deployed commit",
              "captured_at_start": None, "captured_at_end": None, "overall": "ERROR", "summary": {},
              "objects": [], "unexpected_live": [], "dataset_unproven": [], "proposed_capture": {},
              "requests": 0, "error": None}
    token = None
    try:
        root = Path(args.root).resolve()
        managed = load_managed(root, args.dataset)
        projects = {m["project"] for m in managed.values()}
        if projects != {args.project}:
            raise ToolError("CONFIG", "--project does not match the manifest project")
        sha, verified, notes = git_provenance(root, runner)
        report.update(canonical_git_sha=sha, git_provenance_verified=verified, git_provenance_notes=notes)
        token = obtain_token(args, environ, runner)
        guard = RequestGuard(args.api_host, args.project,
                             {d: [o["object_name"] for o in m["objects"]] for d, m in managed.items()})
        transport = (transport_factory or (lambda tok: UrllibTransport(tok, args.timeout)))(token)
        client = Client(guard, transport)
        report["captured_at_start"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", clock())
        base = f"https://{args.api_host}{API_PREFIX}/projects/{_q(args.project)}/datasets"
        objects, unexpected, ds_unproven, proposed = [], [], [], {}
        for dataset, man in managed.items():
            list_url = f"{base}/{_q(dataset)}/tables"
            names = {o["object_name"] for o in man["objects"]}
            try:
                listed1 = list_tables(client, list_url)
            except FetchError as e:
                listed1 = None
                ds_unproven.append({"dataset": dataset, "code": f"LIST_{e.code}"})
            first = {o["object_name"]: read_object(client, f"{list_url}/{_q(o['object_name'])}", args.project, dataset,
                                                   o["object_name"]) for o in man["objects"]}
            second = {n: (read_object(client, f"{list_url}/{_q(n)}", args.project, dataset, n) if lv.exists else None)
                      for n, lv in first.items()}
            try:
                listed2 = list_tables(client, list_url) if listed1 is not None else None
            except FetchError as e:
                listed2 = None
                ds_unproven.append({"dataset": dataset, "code": f"LIST_{e.code}"})
            if listed1 is not None and listed2 is not None:
                # The object set is authoritative only when both snapshots agree (names and types).
                # An unstable list is UNPROVEN: neither snapshot may produce UNEXPECTED_LIVE.
                if listed1 != listed2:
                    ds_unproven.append({"dataset": dataset, "code": "LIST_CHANGED_DURING_CAPTURE"})
                else:
                    for n in sorted(set(listed1) - names):
                        unexpected.append({"dataset": dataset, "object": n, "live_type": listed1[n], "status": "UNEXPECTED_LIVE"})
            for o in man["objects"]:
                n = o["object_name"]
                entry = classify(o, first[n], second[n])
                objects.append(entry)
                if entry["status"] == "PENDING_DEPLOYED_MATCH" and verified:
                    lv = first[n]
                    proposed[f"{dataset}.{n}"] = {
                        "sync_state": "captured_live", "canonical_schema_verification": "bigquery_verified",
                        "capture_main_sha": sha, "captured_at": report["captured_at_start"],
                        "live_body_sha256_at_capture": lv.body_sha, "live_schema_sha256_at_capture": lv.schema_sha,
                        "live_description_sha256_at_capture": lv.description_sha, "live_schema_at_capture": lv.schema}
        report["captured_at_end"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", clock())
        objects.sort(key=lambda o: (o["dataset"], o["object"]))
        summary = {}
        for o in objects:
            summary[o["status"]] = summary.get(o["status"], 0) + 1
        if unexpected:
            summary["UNEXPECTED_LIVE"] = len(unexpected)
        report.update(objects=objects, unexpected_live=unexpected, dataset_unproven=ds_unproven,
                      proposed_capture=dict(sorted(proposed.items())), summary=dict(sorted(summary.items())),
                      requests=client.requests, overall=overall_state(objects, unexpected, ds_unproven))
    except ToolError as e:
        report.update(overall="ERROR", error={"code": e.code, "message": str(e)})
    except Exception as e:  # noqa: BLE001 - fail closed; never echo arbitrary exception text
        report.update(overall="ERROR", error={"code": "INTERNAL", "message": type(e).__name__})
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if token and token in text:   # defence in depth: a token must never reach any output
        report = {"schema_version": 1, "tool": "verify_current_sql_live", "overall": "ERROR",
                  "error": {"code": "REDACTION", "message": "output suppressed: credential material detected"}}
        text = json.dumps(report, sort_keys=True, indent=2) + "\n"
    return report, text


def main(argv=None, **kwargs):
    p = argparse.ArgumentParser(description="Read-only production drift verifier for sql/current (R2C phase 1).")
    p.add_argument("--project", required=True, help="BigQuery project; must equal the manifest project")
    p.add_argument("--dataset", action="append", help="limit to a managed dataset (repeatable)")
    p.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="repository root to compare")
    p.add_argument("--token-env", default=DEFAULT_TOKEN_ENV, help=f"env var holding the access token (default {DEFAULT_TOKEN_ENV})")
    p.add_argument("--token-command", help="explicit operator command printing an access token, e.g. "
                                           "\"gcloud auth print-access-token\" (run without a shell)")
    p.add_argument("--api-host", default=DEFAULT_API_HOST, choices=sorted(ALLOWED_API_HOSTS))
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--output", help="write the JSON report here instead of stdout")
    args = p.parse_args(argv)
    report, text = run(args, **kwargs)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    for o in report.get("objects", []):
        print(f"{o['status']:24s} {o['dataset']}.{o['object']}", file=sys.stderr)
    for u in report.get("unexpected_live", []):
        print(f"{'UNEXPECTED_LIVE':24s} {u['dataset']}.{u['object']} ({u['live_type']})", file=sys.stderr)
    err = report.get("error")
    print(f"overall: {report['overall']}" + (f" — {err['code']}: {err['message']}" if err else ""), file=sys.stderr)
    return EXIT[report["overall"]]


if __name__ == "__main__":
    sys.exit(main())
