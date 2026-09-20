#!/usr/bin/env python3
"""Offline contract validator for the canonical current-SQL layer (sql/current/**).

R2B. Reads repository files only: no network, no credentials, no environment
variables, no BigQuery. It proves repository-side integrity of the canonical
layer (manifest v2, canonical_hash_v1, parser-level SQL safety, dependency
graph, marketplace isolation, historical-definition guard). It does NOT prove
production parity, BigQuery round-trip storage, live output schema or live
metadata: that is R2C.

Usage:
    python tools/validate_current_sql.py [--root REPO_ROOT]

Exit codes: 0 contract holds; 1 contract violations (all listed);
            2 internal error / unsupported environment (fail closed).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

# The parser is part of the contract: its tokenizer positions define the canonical_hash_v1
# body span and its AST defines statement safety. A different version must be reviewed first.
SQLGLOT_VERSION = "30.18.0"
DIALECT = "bigquery"

CURRENT_DIR = ("sql", "current")
MANIFEST_NAME = "MANIFEST.json"
HISTORICAL_NAME = "historical_definitions.json"
TOP_LEVEL_FILES = {"README.md", HISTORICAL_NAME}

MANIFEST_VERSION = 2
HISTORICAL_VERSION = 1
HASH_CONTRACT = "canonical_hash_v1 (see sql/current/README.md)"

# Hard marketplace-isolation policy. A manifest may declare a SUBSET of these datasets in
# allowed_external_datasets; it can never grant itself more. A dataset directory without an
# entry here fails closed. WB and Ozon are separate domains; only evetis_ref is shared.
# evetis_mart (SCALE 1) is the marketplace-neutral layer ABOVE both domains: it may read the two
# marts and evetis_ref, never wb_raw / ozon_raw — it normalises authoritative marketplace facts and
# must not recompute marketplace economics from RAW. Neither marketplace mart may read it back.
EXTERNAL_DATASET_POLICY = {
    "ozon_mart": frozenset({"ozon_raw", "evetis_ref"}),
    "evetis_mart": frozenset({"wb_mart", "ozon_mart", "evetis_ref"}),
}

OBJECT_TYPES = {"VIEW"}
SYNC_STATES = {"captured_live", "pending_deploy"}
# canonical_schema_verification allowed per sync_state.
SCHEMA_VERIFICATION = {
    "captured_live": {"bigquery_verified"},
    "pending_deploy": {"unverified"},
}

TOP_KEYS = {
    "manifest_version", "dataset", "project", "hash_contract",
    "allowed_external_datasets", "rebuild_order", "objects",
}
OBJECT_KEYS = {
    "dataset", "object_name", "object_type", "canonical_path", "sync_state",
    "dependencies", "external_dependencies", "dependency_level",
    "capture_main_sha", "captured_at", "provenance",
    "live_body_sha256_at_capture", "live_schema_sha256_at_capture",
    "live_description_sha256_at_capture", "live_schema_at_capture",
    "canonical_body_sha256", "canonical_description_sha256",
    "canonical_schema_sha256", "canonical_schema_verification",
    "canonical_column_count", "canonical_schema",
}
LIVE_CAPTURE_KEYS = (
    "capture_main_sha", "captured_at", "live_body_sha256_at_capture",
    "live_schema_sha256_at_capture", "live_description_sha256_at_capture",
    "live_schema_at_capture",
)
PROVENANCE_KEYS = {"source", "historical_migration", "preflight_parity", "superseded_historical_definitions"}
COLUMN_KEYS = {"column_name", "data_type", "is_nullable", "ordinal_position"}
HISTORICAL_KEYS = {"version", "purpose", "entries"}
HISTORICAL_ENTRY_KEYS = {"path", "definitions"}

HEX64 = re.compile(r"[0-9a-f]{64}")
GIT_SHA = re.compile(r"[0-9a-f]{40}")
UTC_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,1023}")
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
SIMPLE_TYPE = re.compile(
    r"(INT64|NUMERIC|BIGNUMERIC|FLOAT64|BOOL|STRING|BYTES|DATE|DATETIME|TIME|TIMESTAMP"
    r"|GEOGRAPHY|JSON|INTERVAL)"
)
PARAM_TYPE = re.compile(r"(ARRAY|STRUCT|RANGE)<.+>")

# Any of these nodes anywhere in a canonical file means it is not a pure view definition.
FORBIDDEN_NODES = tuple(
    getattr(exp, name) for name in (
        "Command", "Drop", "Delete", "TruncateTable", "Update", "Insert", "Merge", "Alter",
        "Grant", "Revoke", "LoadData", "Copy", "Export", "Declare", "Set", "Commit", "Rollback",
        "Transaction", "Use", "Execute", "Describe", "Kill", "Pragma", "Analyze", "Cache",
        "Uncache", "Refresh",
    ) if hasattr(exp, name)
)
# Token types sqlglot only emits for statements it cannot model (CALL, EXECUTE IMMEDIATE,
# BEGIN ... END): rejected on tokens as well as on the resulting Command node.
FORBIDDEN_TOKENS = {TokenType.COMMAND, TokenType.EXECUTE}

# C17 token scan of historical SQL: CREATE [modifiers] VIEW|TABLE [IF NOT EXISTS] <name>.
CREATE_MODIFIERS = {"OR", "REPLACE", "TEMP", "TEMPORARY", "MATERIALIZED", "EXTERNAL", "SNAPSHOT"}
CREATE_KINDS = {"VIEW", "TABLE"}
NAME_STOP_TOKENS = {TokenType.L_PAREN, TokenType.SEMICOLON, TokenType.ALIAS, TokenType.COMMA}
SCAN_SKIP_DIRS = {"node_modules"}


@dataclass(frozen=True)
class Finding:
    check: str
    subject: str
    reason: str

    def __str__(self) -> str:
        return f"FAIL [{self.check}] {self.subject}: {self.reason}"


class ContractError(Exception):
    """Malformed input that prevents a check from running (reported as a finding)."""


# ---------------------------------------------------------------------------------------------
# Strict JSON
# ---------------------------------------------------------------------------------------------

def _reject_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ContractError(f"duplicate JSON key {key!r}")
        seen[key] = value
    return seen


def _reject_constant(name):
    raise ContractError(f"non-standard JSON constant {name}")


def load_strict_json(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ContractError(f"cannot read as UTF-8: {e}") from None
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except json.JSONDecodeError as e:
        raise ContractError(f"invalid JSON: {e}") from None


# ---------------------------------------------------------------------------------------------
# canonical_hash_v1 (unchanged from R2A; see sql/current/README.md)
# ---------------------------------------------------------------------------------------------

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def schema_sha256(columns) -> str:
    return sha256_text(json.dumps(columns, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def tokenize(sql: str):
    return sqlglot.tokenize(sql, read=DIALECT)


def view_body(sql: str, tokens) -> str:
    """canonical_hash_v1 body: raw text after the first top-level AS token following CREATE,
    trimmed, with exactly one terminal ';' removed. Comments and strings are not tokens, so
    'AS' inside them is never matched; parentheses (OPTIONS, column lists) are skipped."""
    start = next((k for k, t in enumerate(tokens) if t.token_type == TokenType.CREATE), None)
    if start is None:
        raise ContractError("no CREATE token")
    depth = 0
    for tok in tokens[start:]:
        if tok.token_type == TokenType.L_PAREN:
            depth += 1
        elif tok.token_type == TokenType.R_PAREN:
            depth -= 1
        elif tok.token_type == TokenType.ALIAS and depth == 0:
            body = sql[tok.end + 1:].strip()
            return body[:-1] if body.endswith(";") else body
    raise ContractError("no top-level AS after CREATE")


# ---------------------------------------------------------------------------------------------
# Canonical SQL file analysis (tokens + AST, never regex over SQL text)
# ---------------------------------------------------------------------------------------------

@dataclass
class SqlFacts:
    target: tuple | None = None          # (project, dataset, name)
    body: str | None = None
    description: str | None = None
    references: set | None = None        # {(project, dataset, name)}
    output_columns: list | None = None   # outer projection names, None if not derivable


def analyze_sql(sql: str, subject: str, findings: list) -> SqlFacts:
    facts = SqlFacts()

    def fail(check, reason):
        findings.append(Finding(check, subject, reason))

    try:
        tokens = tokenize(sql)
    except SqlglotError as e:
        fail("C6", f"tokenizer error: {_first_line(e)}")
        return facts

    creates = sum(1 for t in tokens if t.token_type == TokenType.CREATE)
    if creates != 1:
        fail("C8", f"expected exactly one CREATE keyword, found {creates}")
    for tok in tokens:
        if tok.token_type in FORBIDDEN_TOKENS:
            fail("C8", f"forbidden statement keyword {tok.text.upper()!r} at line {tok.line} "
                       "(CALL / EXECUTE IMMEDIATE / scripting)")
    semis = [k for k, t in enumerate(tokens) if t.token_type == TokenType.SEMICOLON]
    if len(semis) > 1 or (semis and semis[0] != len(tokens) - 1):
        fail("C6", "more than one statement: ';' is only allowed once, as the final token")

    try:
        body = view_body(sql, tokens)
        facts.body = body
    except ContractError as e:
        fail("C9", f"cannot locate canonical_hash_v1 body: {e}")

    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except SqlglotError as e:
        fail("C6", f"parser error: {_first_line(e)}")
        return facts
    if len(statements) != 1:
        fail("C6", f"expected exactly one statement, parsed {len(statements)}: "
                   + ", ".join(type(s).__name__ for s in statements))
    if not statements:
        return facts

    root = statements[0]
    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            detail = " (parser fallback: unsupported or malformed SQL)" if isinstance(node, exp.Command) else ""
            fail("C8", f"forbidden statement node {type(node).__name__}{detail}")
        elif isinstance(node, exp.Create) and node is not root:
            fail("C8", "nested CREATE")
    for extra in statements[1:]:
        for node in extra.walk():
            if isinstance(node, FORBIDDEN_NODES):
                fail("C8", f"forbidden statement node {type(node).__name__} in additional statement")

    if not isinstance(root, exp.Create):
        fail("C7", f"statement is {type(root).__name__}, expected CREATE OR REPLACE VIEW")
        return facts
    kind = (root.args.get("kind") or "").upper()
    if kind != "VIEW":
        fail("C7", f"CREATE kind is {kind or '?'}, expected VIEW")
    if not root.args.get("replace"):
        fail("C7", "expected CREATE OR REPLACE")
    if root.args.get("exists"):
        fail("C7", "IF NOT EXISTS is not allowed")
    if not isinstance(root.expression, exp.Query):
        fail("C7", f"view body is {type(root.expression).__name__}, expected a query")

    target = root.this.this if isinstance(root.this, exp.Schema) else root.this
    if isinstance(target, exp.Table):
        facts.target = (target.catalog, target.db, target.name)
    else:
        fail("C5", "cannot resolve CREATE target")

    props = root.args.get("properties")
    descriptions = []
    for prop in (props.expressions if props else []):
        if isinstance(prop, exp.Property) and prop.name.lower() == "description":
            if isinstance(prop.args.get("value"), exp.Literal) and prop.args["value"].is_string:
                descriptions.append(prop.args["value"].this)
            else:
                fail("C16", "description option must be a string literal")
        elif isinstance(prop, (exp.MaterializedProperty, exp.TemporaryProperty)):
            fail("C7", f"{type(prop).__name__} is not allowed (plain VIEW only)")
        else:
            fail("C16", f"unsupported view option/property {prop.sql(DIALECT)[:80]!r} "
                        "(only OPTIONS(description) is allowed)")
    if len(descriptions) == 1:
        facts.description = descriptions[0]
    else:
        fail("C16", f"expected exactly one OPTIONS(description), found {len(descriptions)}")

    query = root.expression
    if isinstance(query, exp.Expression):
        facts.references = _references(query, subject, findings)
        facts.output_columns = _output_columns(query)
    return facts


def _references(query, subject, findings) -> set:
    ctes = {cte.alias_or_name for cte in query.find_all(exp.CTE)}
    refs = set()
    for table in query.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            findings.append(Finding("C12", subject, f"table-valued function or non-table source "
                                                   f"{table.sql(DIALECT)[:80]!r} is not allowed"))
            continue
        if not table.catalog and not table.db and table.name in ctes:
            continue
        if not (table.catalog and table.db):
            findings.append(Finding("C12", subject, f"reference {table.sql(DIALECT)[:80]!r} is not "
                                                   "fully qualified as `project.dataset.object`"))
            continue
        if not IDENT.fullmatch(table.name) or not IDENT.fullmatch(table.db):
            findings.append(Finding("C12", subject, f"reference {table.sql(DIALECT)[:80]!r} is not a plain "
                                                   "object name (wildcards/INFORMATION_SCHEMA not allowed)"))
            continue
        refs.add((table.catalog, table.db, table.name))
    for func in query.find_all(exp.Anonymous):
        if "." in func.name or func.name.upper() == "EXTERNAL_QUERY":
            findings.append(Finding("C12", subject, f"qualified/external function call {func.name!r} "
                                                   "is not allowed (routine references are undeclared deps)"))
    for dot in query.find_all(exp.Dot):
        if isinstance(dot.expression, exp.Func):
            findings.append(Finding("C12", subject, f"qualified function call {dot.sql(DIALECT)[:80]!r} "
                                                   "is not allowed (routine references are undeclared deps)"))
    return refs


def _output_columns(query):
    selects = getattr(query, "selects", None)
    if not selects:
        return None
    names = []
    for sel in selects:
        if isinstance(sel, exp.Star) or (isinstance(sel, exp.Column) and isinstance(sel.this, exp.Star)):
            return None
        name = sel.alias_or_name
        if not name:
            return None
        names.append(name)
    return names


def _first_line(err) -> str:
    return str(err).strip().splitlines()[0][:200] if str(err).strip() else type(err).__name__


# ---------------------------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------------------------

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _safe_rel_path(p) -> bool:
    if not isinstance(p, str) or not p or p.startswith("/") or "\\" in p or "\x00" in p:
        return False
    parts = p.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _check_schema(cols, label, subject, fail) -> bool:
    if not isinstance(cols, list) or not cols:
        fail("C10", f"{label} must be a non-empty list of columns")
        return False
    ok = True
    names = set()
    for i, col in enumerate(cols, start=1):
        if not isinstance(col, dict) or set(col) != COLUMN_KEYS:
            fail("C10", f"{label}[{i}] must have exactly the keys {sorted(COLUMN_KEYS)}")
            ok = False
            continue
        if not _is_int(col["ordinal_position"]) or col["ordinal_position"] != i:
            fail("C10", f"{label}[{i}] ordinal_position must be {i}")
            ok = False
        name = col["column_name"]
        if not isinstance(name, str) or not IDENT.fullmatch(name):
            fail("C10", f"{label}[{i}] invalid column_name {name!r}")
            ok = False
        elif name.lower() in names:
            fail("C10", f"{label} duplicate column_name {name!r}")
            ok = False
        else:
            names.add(name.lower())
        dtype = col["data_type"]
        if not isinstance(dtype, str) or not (SIMPLE_TYPE.fullmatch(dtype) or PARAM_TYPE.fullmatch(dtype)):
            fail("C10", f"{label}[{i}] data_type {dtype!r} is not a standard BigQuery type name")
            ok = False
        if col["is_nullable"] not in ("YES", "NO"):
            fail("C10", f"{label}[{i}] is_nullable must be 'YES' or 'NO'")
            ok = False
    return ok


def _check_object_fields(obj, dataset, subject, fail, root: Path) -> bool:
    """C2 (contract fields/format) and C4 (safe canonical path). Returns False if the object is
    too malformed for the semantic checks."""
    if not isinstance(obj, dict):
        fail("C2", "object entry must be a JSON object")
        return False
    keys = set(obj)
    if keys != OBJECT_KEYS:
        if keys - OBJECT_KEYS:
            fail("C2", f"unknown field(s) {sorted(keys - OBJECT_KEYS)}")
        if OBJECT_KEYS - keys:
            fail("C2", f"missing field(s) {sorted(OBJECT_KEYS - keys)}")
        return False
    ok = True

    def bad(check, reason):
        nonlocal ok
        fail(check, reason)
        ok = False

    name = obj["object_name"]
    if not isinstance(name, str) or not IDENT.fullmatch(name):
        bad("C2", f"object_name {name!r} is not a plain identifier")
        return False
    if obj["dataset"] != dataset:
        bad("C5", f"object dataset {obj['dataset']!r} != manifest dataset {dataset!r}")
    if obj["object_type"] not in OBJECT_TYPES:
        bad("C7", f"object_type {obj['object_type']!r} not supported (allowed: {sorted(OBJECT_TYPES)})")
    expected_path = f"sql/current/{dataset}/{name}.sql"
    cpath = obj["canonical_path"]
    if cpath != expected_path or not _safe_rel_path(cpath):
        bad("C4", f"canonical_path {cpath!r} must be exactly {expected_path!r}")
    else:
        full = root / cpath
        current = (root / Path(*CURRENT_DIR)).resolve()
        if full.is_symlink() or not full.is_file():
            bad("C4", f"canonical_path {cpath!r} is not a regular file")
        elif current not in full.resolve().parents:
            bad("C4", f"canonical_path {cpath!r} escapes sql/current")
    state = obj["sync_state"]
    if state not in SYNC_STATES:
        bad("C18", f"sync_state {state!r} not in {sorted(SYNC_STATES)}")
    for key in ("dependencies", "external_dependencies"):
        val = obj[key]
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            bad("C2", f"{key} must be a list of strings")
        elif val != sorted(set(val)):
            bad("C2", f"{key} must be sorted and unique")
    for dep in obj["dependencies"] if isinstance(obj["dependencies"], list) else []:
        if isinstance(dep, str) and not IDENT.fullmatch(dep):
            bad("C2", f"dependency {dep!r} must be a bare object name in {dataset}")
    for dep in obj["external_dependencies"] if isinstance(obj["external_dependencies"], list) else []:
        parts = dep.split(".") if isinstance(dep, str) else []
        if len(parts) != 2 or not all(IDENT.fullmatch(p) for p in parts):
            bad("C2", f"external dependency {dep!r} must be 'dataset.object'")
    if not _is_int(obj["dependency_level"]) or obj["dependency_level"] < 0:
        bad("C2", "dependency_level must be a non-negative integer")

    for key in ("canonical_body_sha256", "canonical_description_sha256", "canonical_schema_sha256"):
        if not isinstance(obj[key], str) or not HEX64.fullmatch(obj[key]):
            bad("C2", f"{key} must be 64 lowercase hex characters")
    verification = obj["canonical_schema_verification"]
    if state in SCHEMA_VERIFICATION and verification not in SCHEMA_VERIFICATION[state]:
        bad("C18", f"canonical_schema_verification {verification!r} not allowed for sync_state "
                   f"{state!r} (allowed: {sorted(SCHEMA_VERIFICATION[state])})")
    if not _is_int(obj["canonical_column_count"]):
        bad("C2", "canonical_column_count must be an integer")
    if obj["canonical_schema"] is None:
        bad("C10", "canonical_schema is required (an explicit output-schema contract); "
                   "'unknown' is not a valid Git-first state")
    elif _check_schema(obj["canonical_schema"], "canonical_schema", subject, fail):
        if obj["canonical_column_count"] != len(obj["canonical_schema"]):
            bad("C10", f"canonical_column_count {obj['canonical_column_count']} != "
                       f"{len(obj['canonical_schema'])} columns in canonical_schema")
    else:
        ok = False

    live_nulls = [k for k in LIVE_CAPTURE_KEYS if obj[k] is None]
    if live_nulls and len(live_nulls) != len(LIVE_CAPTURE_KEYS):
        bad("C18", f"live capture fields must be all present or all null; null: {live_nulls}")
    elif live_nulls and state != "pending_deploy":
        bad("C18", "a never-captured object (null live capture) must be sync_state 'pending_deploy'")
    elif not live_nulls:
        for key in ("live_body_sha256_at_capture", "live_schema_sha256_at_capture",
                    "live_description_sha256_at_capture"):
            if not isinstance(obj[key], str) or not HEX64.fullmatch(obj[key]):
                bad("C2", f"{key} must be 64 lowercase hex characters")
        if not isinstance(obj["capture_main_sha"], str) or not GIT_SHA.fullmatch(obj["capture_main_sha"]):
            bad("C2", "capture_main_sha must be a full 40-hex git SHA")
        if not isinstance(obj["captured_at"], str) or not UTC_TS.fullmatch(obj["captured_at"]):
            bad("C2", "captured_at must be UTC 'YYYY-MM-DDTHH:MM:SSZ'")
        if not _check_schema(obj["live_schema_at_capture"], "live_schema_at_capture", subject, fail):
            ok = False

    prov = obj["provenance"]
    if not isinstance(prov, dict) or set(prov) != PROVENANCE_KEYS:
        bad("C2", f"provenance must have exactly the keys {sorted(PROVENANCE_KEYS)}")
    else:
        if not isinstance(prov["source"], str) or not prov["source"].strip():
            bad("C2", "provenance.source must be a non-empty string")
        if prov["historical_migration"] is not None and not _safe_rel_path(prov["historical_migration"]):
            bad("C2", "provenance.historical_migration must be a safe relative path or null")
        if prov["preflight_parity"] is not None and not isinstance(prov["preflight_parity"], str):
            bad("C2", "provenance.preflight_parity must be a string or null")
        sup = prov["superseded_historical_definitions"]
        if not isinstance(sup, list) or not all(_safe_rel_path(p) for p in sup):
            bad("C2", "provenance.superseded_historical_definitions must be a list of safe relative paths")
    return ok


def validate_dataset(root: Path, ds_dir: Path, findings: list) -> list:
    """Validate one sql/current/<dataset>/ directory. Returns [(dataset, object_name)] managed."""
    dataset = ds_dir.name
    rel_dir = f"sql/current/{dataset}"

    def fail_at(subject):
        return lambda check, reason: findings.append(Finding(check, subject, reason))

    fail = fail_at(rel_dir)

    # C1: directory layout
    sql_files = {}
    has_manifest = False
    for entry in sorted(ds_dir.iterdir()):
        if entry.is_symlink():
            fail("C1", f"symlink {entry.name!r} is not allowed")
        elif entry.is_dir():
            fail("C1", f"nested directory {entry.name!r} is not allowed")
        elif entry.name == MANIFEST_NAME:
            has_manifest = True
        elif entry.suffix == ".sql" and IDENT.fullmatch(entry.stem):
            sql_files[entry.stem] = entry
        else:
            fail("C1", f"unexpected file {entry.name!r} (only {MANIFEST_NAME} and <OBJECT>.sql)")
    if not IDENT.fullmatch(dataset):
        fail("C1", "dataset directory name is not a plain identifier")
        return []
    if dataset not in EXTERNAL_DATASET_POLICY:
        fail("C13", f"no marketplace-isolation policy for dataset {dataset!r} in the validator; "
                    "a new dataset needs a reviewed policy entry")
    if not has_manifest:
        fail("C1", f"missing {MANIFEST_NAME}")
        return []

    mpath = f"{rel_dir}/{MANIFEST_NAME}"
    mfail = fail_at(mpath)
    try:
        man = load_strict_json(ds_dir / MANIFEST_NAME)
    except ContractError as e:
        mfail("C2", str(e))
        return []
    if not isinstance(man, dict):
        mfail("C2", "manifest must be a JSON object")
        return []
    version = man.get("manifest_version")
    if not _is_int(version) or version != MANIFEST_VERSION:
        mfail("C2", f"unsupported manifest_version {version!r} (this validator supports {MANIFEST_VERSION})")
        return []
    keys = set(man)
    if keys - TOP_KEYS:
        mfail("C2", f"unknown top-level field(s) {sorted(keys - TOP_KEYS)}")
    if TOP_KEYS - keys:
        mfail("C2", f"missing top-level field(s) {sorted(TOP_KEYS - keys)}")
        return []
    if man["dataset"] != dataset:
        mfail("C2", f"dataset {man['dataset']!r} != directory name {dataset!r}")
    project = man["project"]
    if not isinstance(project, str) or not PROJECT_ID.fullmatch(project):
        mfail("C2", f"project {project!r} is not a valid project id")
    if man["hash_contract"] != HASH_CONTRACT:
        mfail("C2", f"hash_contract must be exactly {HASH_CONTRACT!r}")

    allowed = man["allowed_external_datasets"]
    policy = EXTERNAL_DATASET_POLICY.get(dataset, frozenset())
    if not isinstance(allowed, list) or not all(isinstance(d, str) for d in allowed) or allowed != sorted(set(allowed)):
        mfail("C2", "allowed_external_datasets must be a sorted, unique list of dataset names")
        allowed = []
    for ds in allowed:
        if ds == dataset or ds not in policy:
            why = "cross-marketplace dataset" if ds.startswith("wb_") and dataset.startswith("ozon_") else "dataset"
            mfail("C13", f"allowed_external_datasets grants {why} {ds!r} outside the hard validator policy "
                         f"for {dataset} ({sorted(policy)}); a manifest cannot extend the policy")
    allowed_set = set(allowed) & policy

    objects = man["objects"]
    if not isinstance(objects, list):
        mfail("C2", "objects must be a list")
        return []

    # C15: duplicate objects in the manifest
    names = [o.get("object_name") for o in objects if isinstance(o, dict)]
    for name, n in Counter(names).items():
        if n > 1:
            mfail("C15", f"object {name!r} is declared {n} times")

    good = {}
    for i, obj in enumerate(objects):
        name = obj.get("object_name") if isinstance(obj, dict) else None
        subject = f"{dataset}.{name}" if isinstance(name, str) else f"{mpath} objects[{i}]"
        if _check_object_fields(obj, dataset, subject, fail_at(subject), root) and name not in good:
            good[name] = obj

    # C3: manifest <-> files 1:1
    for name in sorted(set(sql_files) - set(names)):
        fail_at(f"{rel_dir}/{name}.sql")("C3", "SQL file has no manifest entry")
    for name in sorted(set(n for n in names if isinstance(n, str)) - set(sql_files)):
        fail_at(f"{dataset}.{name}")("C3", "manifest entry has no SQL file")

    # Per-object SQL checks
    facts = {}
    targets = Counter()
    for name, path in sorted(sql_files.items()):
        rel = f"{rel_dir}/{path.name}"
        subject = f"{dataset}.{name} ({rel})"
        try:
            sql = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            findings.append(Finding("C6", subject, f"cannot read as UTF-8: {e}"))
            continue
        f = analyze_sql(sql, subject, findings)
        facts[name] = f
        if f.target:
            targets[f.target] += 1
            if f.target != (project, dataset, name):
                findings.append(Finding("C5", subject, f"CREATE target {'.'.join(f.target)!r} != "
                                                       f"{project}.{dataset}.{name} (file name / object_name)"))
    for target, n in targets.items():
        if n > 1:
            fail("C15", f"object {'.'.join(target)} is created by {n} canonical files")

    # Per-object manifest <-> SQL semantics
    internal_refs = {}
    for name, obj in good.items():
        f = facts.get(name)
        subject = f"{dataset}.{name}"
        ofail = fail_at(subject)
        if f is None:
            continue
        if f.body is not None and sha256_text(f.body) != obj["canonical_body_sha256"]:
            ofail("C9", f"canonical_body_sha256 {obj['canonical_body_sha256']} != recomputed "
                        f"canonical_hash_v1 {sha256_text(f.body)}")
        if schema_sha256(obj["canonical_schema"]) != obj["canonical_schema_sha256"]:
            ofail("C10", f"canonical_schema_sha256 {obj['canonical_schema_sha256']} != recomputed "
                         f"{schema_sha256(obj['canonical_schema'])}")
        if f.output_columns is not None:
            declared = [c["column_name"] for c in obj["canonical_schema"]]
            if f.output_columns != declared:
                ofail("C10", "canonical_schema column names/order differ from the SQL's outer projection: "
                             f"sql={f.output_columns} manifest={declared}")
        if f.description is not None and sha256_text(f.description) != obj["canonical_description_sha256"]:
            ofail("C16", f"canonical_description_sha256 {obj['canonical_description_sha256']} != recomputed "
                         f"{sha256_text(f.description)}")
        if f.references is None:
            continue
        internal, external = set(), set()
        for proj, ds, obj_name in sorted(f.references):
            ref = f"{proj}.{ds}.{obj_name}"
            if proj != project:
                ofail("C12", f"reference {ref!r} uses project {proj!r}, manifest project is {project!r}")
            elif ds == dataset:
                internal.add(obj_name)
            elif ds not in policy:
                why = "cross-marketplace reference" if ds.startswith("wb_") and dataset.startswith("ozon_") \
                    else "reference outside the marketplace-isolation policy"
                ofail("C13", f"{why} {ref!r}: {dataset} may only read {sorted(policy)}")
            elif ds not in allowed_set:
                ofail("C12", f"reference {ref!r}: dataset {ds!r} is not in allowed_external_datasets")
                external.add(f"{ds}.{obj_name}")
            else:
                external.add(f"{ds}.{obj_name}")
        for dep in obj["external_dependencies"]:
            ds = dep.split(".")[0]
            if ds not in policy:
                ofail("C13", f"declared external dependency {dep!r} is outside the hard policy {sorted(policy)}")
        for dep in sorted(internal):
            if dep not in good and dep not in sql_files:
                ofail("C12", f"reference {dataset}.{dep} is not a managed canonical object")
        if sorted(internal) != obj["dependencies"]:
            ofail("C11", f"dependencies {obj['dependencies']} != references in SQL {sorted(internal)}")
        if sorted(external) != obj["external_dependencies"]:
            ofail("C11", f"external_dependencies {obj['external_dependencies']} != references in SQL "
                         f"{sorted(external)}")
        internal_refs[name] = internal

    # C14: graph (from SQL references; manifest deps must equal them, C11)
    graph = {n: {d for d in internal_refs.get(n, set(good[n]["dependencies"])) if d in good} for n in good}
    levels, cyclic = _levels(graph)
    if cyclic:
        fail_at(mpath)("C14", f"dependency cycle among {sorted(cyclic)}")
    else:
        for name, obj in good.items():
            if obj["dependency_level"] != levels[name]:
                fail_at(f"{dataset}.{name}")("C14", f"dependency_level {obj['dependency_level']} != "
                                                    f"computed {levels[name]}")
        expected = sorted(good, key=lambda n: (levels[n], n))
        if len(good) == len(objects) and man["rebuild_order"] != expected:
            fail_at(mpath)("C14", f"rebuild_order must be {expected} (by level, then name)")

    # C18: sync_state contract
    for name, obj in good.items():
        _check_sync_state(obj, fail_at(f"{dataset}.{name}"))

    return [(dataset, n) for n in good]


def _levels(graph):
    levels, remaining = {}, dict(graph)
    while remaining:
        ready = [n for n, deps in remaining.items() if all(d in levels for d in deps)]
        if not ready:
            return levels, set(remaining)
        for n in ready:
            levels[n] = 1 + max((levels[d] for d in graph[n]), default=-1)
            del remaining[n]
    return levels, set()


def _check_sync_state(obj, fail):
    state = obj["sync_state"]
    captured = obj["live_body_sha256_at_capture"] is not None
    if state == "captured_live":
        if obj["canonical_body_sha256"] != obj["live_body_sha256_at_capture"]:
            fail("C18", "captured_live requires canonical_body_sha256 == live_body_sha256_at_capture; "
                        "a Git-first change must set sync_state 'pending_deploy'")
        if obj["canonical_schema_sha256"] != obj["live_schema_sha256_at_capture"]:
            fail("C18", "captured_live requires canonical_schema_sha256 == live_schema_sha256_at_capture")
        if obj["canonical_description_sha256"] != obj["live_description_sha256_at_capture"]:
            fail("C18", "captured_live requires canonical_description_sha256 == "
                        "live_description_sha256_at_capture")
        if obj["canonical_schema"] != obj["live_schema_at_capture"]:
            fail("C18", "captured_live requires canonical_schema == live_schema_at_capture")
    elif state == "pending_deploy" and captured:
        if (obj["canonical_body_sha256"] == obj["live_body_sha256_at_capture"]
                and obj["canonical_schema_sha256"] == obj["live_schema_sha256_at_capture"]
                and obj["canonical_description_sha256"] == obj["live_description_sha256_at_capture"]):
            fail("C18", "pending_deploy but canonical body, schema and description all equal the live "
                        "capture: nothing is pending (use captured_live after a read-only recapture; "
                        "live capture fields must not be rewritten to match an undeployed change)")


# ---------------------------------------------------------------------------------------------
# C17: historical-definition guard
# ---------------------------------------------------------------------------------------------

def created_objects(sql: str):
    """Token scan: [(dataset or None, name)] for each CREATE [...] VIEW|TABLE <name>."""
    tokens = tokenize(sql)
    out = []
    for i, tok in enumerate(tokens):
        if tok.token_type != TokenType.CREATE:
            continue
        j = i + 1
        while j < len(tokens) and tokens[j].text.upper() in CREATE_MODIFIERS:
            j += 1
        if j >= len(tokens) or tokens[j].text.upper() not in CREATE_KINDS:
            continue
        j += 1
        if [t.text.upper() for t in tokens[j:j + 3]] == ["IF", "NOT", "EXISTS"]:
            j += 3
        if j >= len(tokens) or tokens[j].token_type in NAME_STOP_TOKENS:
            continue
        k = j
        while (k + 1 < len(tokens) and tokens[k + 1].start == tokens[k].end + 1
               and tokens[k + 1].token_type not in NAME_STOP_TOKENS):
            k += 1
        parts = sql[tokens[j].start:tokens[k].end + 1].replace("`", "").split(".")
        out.append((parts[-2] if len(parts) >= 2 else None, parts[-1]))
    return out


def _iter_sql_files(root: Path):
    current = Path(*CURRENT_DIR)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SCAN_SKIP_DIRS)
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir == current or current in rel_dir.parents:
            dirnames[:] = []
            continue
        for fn in sorted(filenames):
            if fn.endswith(".sql"):
                yield (rel_dir / fn).as_posix()


def validate_historical(root: Path, managed: list, findings: list):
    rel = f"sql/current/{HISTORICAL_NAME}"
    fail = lambda check, reason, subject=rel: findings.append(Finding(check, subject, reason))  # noqa: E731
    canonical = {(ds.casefold(), name.casefold()): f"{ds}.{name}" for ds, name in managed}
    names = {name.casefold() for _, name in managed}

    declared = {}
    path = root / Path(*CURRENT_DIR) / HISTORICAL_NAME
    if not path.is_file() or path.is_symlink():
        fail("C17", "missing historical-definition allowlist")
    else:
        try:
            data = load_strict_json(path)
        except ContractError as e:
            fail("C17", str(e))
            data = None
        if data is not None:
            if not isinstance(data, dict) or set(data) != HISTORICAL_KEYS:
                fail("C17", f"allowlist must have exactly the keys {sorted(HISTORICAL_KEYS)}")
            elif not _is_int(data["version"]) or data["version"] != HISTORICAL_VERSION:
                fail("C17", f"unsupported allowlist version {data['version']!r}")
            elif not isinstance(data["entries"], list):
                fail("C17", "entries must be a list")
            else:
                for i, entry in enumerate(data["entries"]):
                    if not isinstance(entry, dict) or set(entry) != HISTORICAL_ENTRY_KEYS:
                        fail("C17", f"entries[{i}] must have exactly the keys {sorted(HISTORICAL_ENTRY_KEYS)}")
                        continue
                    epath, defs = entry["path"], entry["definitions"]
                    if (not _safe_rel_path(epath) or not epath.endswith(".sql")
                            or epath.startswith("sql/current/")):
                        fail("C17", f"entries[{i}] path {epath!r} must be a safe relative .sql path "
                                    "outside sql/current/")
                        continue
                    if epath in declared:
                        fail("C17", f"duplicate allowlist entry {epath!r}")
                        continue
                    full = root / epath
                    if full.is_symlink() or not full.is_file():
                        fail("C17", f"allowlisted file {epath!r} does not exist")
                    if not isinstance(defs, dict) or not defs:
                        fail("C17", f"entries[{i}] definitions must be a non-empty object")
                        continue
                    clean = {}
                    for key, count in defs.items():
                        if key.casefold() not in {"{}.{}".format(*k) for k in canonical}:
                            fail("C17", f"{epath}: {key!r} is not a canonical object")
                        elif not _is_int(count) or count < 1:
                            fail("C17", f"{epath}: count for {key!r} must be a positive integer")
                        else:
                            clean[key] = count
                    declared[epath] = clean

    observed = {}
    for relpath in _iter_sql_files(root):
        try:
            sql = (root / relpath).read_text(encoding="utf-8")
            folded = sql.casefold()
            # A file that never spells a canonical object name cannot CREATE it: skip tokenizing.
            if not any(name in folded for name in names):
                continue
            creates = created_objects(sql)
        except (OSError, UnicodeDecodeError, SqlglotError) as e:
            fail("C17", f"cannot tokenize ({_first_line(e)}); the guard cannot prove it defines no "
                        "canonical object", relpath)
            continue
        hits = Counter()
        for ds, name in creates:
            if ds is None:
                if name.casefold() in names:
                    hits.update(v for (d, n), v in canonical.items() if n == name.casefold())
            elif (ds.casefold(), name.casefold()) in canonical:
                hits[canonical[(ds.casefold(), name.casefold())]] += 1
        if hits:
            observed[relpath] = dict(hits)

    for relpath, hits in sorted(observed.items()):
        if relpath not in declared:
            fail("C17", f"new competing definition of canonical object(s) {sorted(hits)} outside "
                        "sql/current/; change sql/current/ instead, or review and allowlist it in "
                        f"{HISTORICAL_NAME}", relpath)
        elif hits != declared[relpath]:
            fail("C17", f"observed canonical definitions {dict(sorted(hits.items()))} != allowlisted "
                        f"{dict(sorted(declared[relpath].items()))}", relpath)
    for relpath in sorted(set(declared) - set(observed)):
        if (root / relpath).is_file():
            fail("C17", "allowlisted file no longer defines any canonical object (stale entry)", relpath)
    return observed


# ---------------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------------

def validate(root: Path):
    """Return (findings, summary)."""
    root = Path(root).resolve()
    findings = []
    current = root / Path(*CURRENT_DIR)
    if not current.is_dir() or current.is_symlink():
        return [Finding("C1", "sql/current", "directory missing")], {}
    managed = []
    datasets = []
    for entry in sorted(current.iterdir()):
        if entry.is_symlink():
            findings.append(Finding("C1", f"sql/current/{entry.name}", "symlink is not allowed"))
        elif entry.is_dir():
            datasets.append(entry.name)
            managed += validate_dataset(root, entry, findings)
        elif entry.name not in TOP_LEVEL_FILES:
            findings.append(Finding("C1", f"sql/current/{entry.name}",
                                    f"unexpected file (allowed: {sorted(TOP_LEVEL_FILES)} and dataset dirs)"))
    for key, n in Counter(managed).items():
        if n > 1:
            findings.append(Finding("C15", ".".join(key), f"canonical object managed {n} times"))
    observed = validate_historical(root, managed, findings)
    summary = dict(datasets=datasets, objects=len(managed),
                   historical_files=len(observed), historical_sites=sum(sum(h.values()) for h in observed.values()))
    return findings, summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                        help="repository root (default: parent of tools/)")
    args = parser.parse_args(argv)
    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
    if sqlglot.__version__ != SQLGLOT_VERSION:
        print(f"ERROR: sqlglot {sqlglot.__version__} installed, validator is pinned to {SQLGLOT_VERSION}; "
              "install tools/requirements-sql-ci.txt", file=sys.stderr)
        return 2
    try:
        findings, summary = validate(Path(args.root))
    except Exception as e:  # noqa: BLE001 - fail closed on anything unexpected
        print(f"ERROR: validator internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    for f in sorted(set(findings), key=lambda f: (f.check, f.subject, f.reason)):
        print(f)
    if findings:
        print(f"\nsql/current contract: {len(set(findings))} violation(s).")
        return 1
    print(f"OK: sql/current contract holds — datasets {summary['datasets']}, {summary['objects']} objects, "
          f"checks C1–C18; historical guard: {summary['historical_sites']} allowlisted definition(s) "
          f"in {summary['historical_files']} file(s).")
    print("Scope: repository-side integrity only. Production parity is NOT proven here (R2C).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
