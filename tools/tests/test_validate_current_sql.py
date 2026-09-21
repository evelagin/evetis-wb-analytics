"""Tests for tools/validate_current_sql.py (R2B offline SQL CI).

Positive cases run against the real sql/current/ layer and adversarial fixtures; negative cases
copy the repository's sql/ tree into a temp dir, mutate exactly one thing and assert the failing
check id. Expected hashes are computed here independently of the validator's own helpers.
"""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import validate_current_sql as v  # noqa: E402

P = "project-fa311fc0-4d87-4781-986"
DS = "ozon_mart"
MANIFEST = Path("sql/current/ozon_mart/MANIFEST.json")
HISTORICAL = Path("sql/current/historical_definitions.json")
LEAF = "V_OZON_TARIFF_CHANGE_LOG"  # level 0, no dependants
# Objects outside the pinned R2A hash set: added by SCALE 1 (2026-09-20) or changed by Gate 5K
# (2026-09-21, the CIS buyout model). V_OZON_COMMISSION_RECOVERY is still captured_live — Gate 5K only
# retired it from use and changed its comment header, which is not part of canonical_hash_v1.
NON_R2A = {"ozon_mart": {"V_OZON_COMMISSION_RECOVERY", "V_OZON_CIS_BUYOUT", "FCT_OZON_SKU_PNL_DAILY",
                         "FCT_OZON_SKU_PNL_MONTHLY", "FCT_OZON_PNL_MONTHLY"},
           "evetis_mart": {"FACT_SKU_DAILY"}}
# Gate 5M deployed every object Gate 5K/5L rewrote and read them back, so nothing is pending.
GATE5K_PENDING = {"ozon_mart": set(), "evetis_mart": set()}

# canonical_hash_v1 body hashes proven equal to production in R2A (PR #140). Pinned literally.
R2A_BODY_SHA256 = {
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


# ------------------------------------------------------------------------------------------ helpers

def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def schema_hash(cols):
    return sha(json.dumps(cols, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def columns(*names, dtype="INT64"):
    return [dict(ordinal_position=i, column_name=n, data_type=dtype, is_nullable="YES")
            for i, n in enumerate(names, start=1)]


@pytest.fixture
def repo(tmp_path):
    shutil.copytree(REPO / "sql", tmp_path / "sql")
    return tmp_path


def load(repo, rel=MANIFEST):
    return json.loads((repo / rel).read_text(encoding="utf-8"))


def save(repo, data, rel=MANIFEST):
    (repo / rel).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def obj(man, name):
    return next(o for o in man["objects"] if o["object_name"] == name)


def sql_path(repo, name):
    return repo / "sql/current/ozon_mart" / f"{name}.sql"


def findings(root):
    found, _ = v.validate(Path(root))
    return found


def checks(root):
    return {f.check for f in findings(root)}


def assert_clean(root):
    found = findings(root)
    assert found == [], "\n".join(map(str, found))


def assert_fails(root, check, text=None):
    found = findings(root)
    hits = [f for f in found if f.check == check and (text is None or text in f.reason)]
    assert hits, f"expected {check} {text!r}; got:\n" + "\n".join(map(str, found))
    return hits


def view_text(name, body, description="fixture", header="-- fixture\n"):
    return (f"{header}CREATE OR REPLACE VIEW `{P}.{DS}.{name}`\n"
            f'OPTIONS (description = "{description}")\nAS\n{body};\n')


def v1_body(body):
    """canonical_hash_v1 as specified in sql/current/README.md, applied to the text after `AS`."""
    text = ("\n" + body + ";\n").strip()
    return text[:-1] if text.endswith(";") else text


def install(repo, name, body, cols, external=(), internal=(), description="fixture", header="-- fixture\n",
            text=None, state="pending_deploy"):
    """Replace one canonical object with a fixture; manifest hashes are computed independently."""
    sql_path(repo, name).write_text(text or view_text(name, body, description, header), encoding="utf-8")
    man = load(repo)
    o = obj(man, name)
    o.update(sync_state=state,
             canonical_schema_verification="unverified" if state == "pending_deploy" else "bigquery_verified",
             canonical_body_sha256=sha(v1_body(body)),
             canonical_description_sha256=sha(description),
             canonical_schema=cols, canonical_schema_sha256=schema_hash(cols),
             canonical_column_count=len(cols),
             dependencies=sorted(internal), external_dependencies=sorted(external))
    save(repo, man)
    return man


def replace_in_file(path, old, new, count=1):
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, count), encoding="utf-8")


# ------------------------------------------------------------------------------------------ positive

def test_real_repository_passes():
    assert_clean(REPO)
    _, summary = v.validate(REPO)
    assert summary["objects"] == len(R2A_BODY_SHA256) + sum(map(len, NON_R2A.values()))
    assert summary["historical_sites"] == 14


def test_canonical_hash_v1_reproduces_r2a_hashes_byte_for_byte():
    for name, expected in R2A_BODY_SHA256.items():
        text = (REPO / f"sql/current/ozon_mart/{name}.sql").read_text(encoding="utf-8")
        assert v.sha256_text(v.view_body(text, v.tokenize(text))) == expected, name
    man = load(REPO)
    assert {o["object_name"]: o["canonical_body_sha256"] for o in man["objects"]
            if o["object_name"] not in NON_R2A[DS]} == R2A_BODY_SHA256


def test_real_manifest_is_v2_captured_live():
    man = load(REPO)
    assert man["manifest_version"] == 2
    assert man["allowed_external_datasets"] == ["evetis_ref", "ozon_raw"]
    assert {o["object_name"] for o in man["objects"]} == set(R2A_BODY_SHA256) | NON_R2A[DS]
    for o in man["objects"]:
        assert o["sync_state"] == "captured_live"
        assert o["canonical_schema_verification"] == "bigquery_verified"
        assert o["canonical_body_sha256"] == o["live_body_sha256_at_capture"]
        assert o["canonical_schema"] == o["live_schema_at_capture"]


def test_real_references_match_manifest():
    man = load(REPO)
    for o in man["objects"]:
        text = (REPO / o["canonical_path"]).read_text(encoding="utf-8")
        facts = v.analyze_sql(text, o["object_name"], out := [])
        assert out == []
        internal = sorted(n for _, d, n in facts.references if d == DS)
        external = sorted(f"{d}.{n}" for _, d, n in facts.references if d != DS)
        assert (internal, external) == (o["dependencies"], o["external_dependencies"])


POSITIVE_FIXTURES = {
    "forbidden words in comments": dict(
        header="-- DROP TABLE x; DELETE FROM y; TRUNCATE TABLE z; EXECUTE IMMEDIATE 'q'; CALL p();\n",
        body="/* MERGE INTO t USING s ON TRUE; INSERT INTO t VALUES (1); UPDATE t SET a = 1 */\n"
             "SELECT 1 AS a -- ALTER TABLE t ADD COLUMN b INT64; ASSERT FALSE\n",
        cols=columns("a")),
    "forbidden words and semicolon in string literals": dict(
        body="SELECT 'DROP TABLE t; DELETE FROM u; CALL p()' AS s, \"x;y AS z\" AS t, r'EXECUTE IMMEDIATE' AS u",
        cols=columns("s", "t", "u", dtype="STRING")),
    "AS in comment, string, description and CTE": dict(
        header="/* AS AS ; */ -- AS\n", description="x AS y; z",
        body="WITH q AS (SELECT 1 AS n) SELECT n AS as_col FROM q",
        cols=columns("as_col")),
    "comments before CREATE": dict(
        header="-- header AS ; DROP\n/* multi-line\n   CREATE OR REPLACE TABLE x AS SELECT 1; */\n-- another\n",
        body="SELECT 1 AS a", cols=columns("a")),
    "backtick-qualified names": dict(
        body=f"SELECT c.offer_id AS offer_id FROM `{P}.ozon_raw.RAW_OZON_CATALOG` c "
             f"JOIN `{P}`.`evetis_ref`.`REF_SKU_CHANNEL_MAP` m ON m.marketplace_sku = c.offer_id",
        external=["evetis_ref.REF_SKU_CHANNEL_MAP", "ozon_raw.RAW_OZON_CATALOG"],
        cols=columns("offer_id", dtype="STRING")),
    "nested STRUCT/ARRAY": dict(
        body="SELECT STRUCT(1 AS a, ARRAY<STRUCT<x INT64, y STRING>>[(1, 'a')] AS arr) AS s, [1, 2] AS l",
        cols=[dict(ordinal_position=1, column_name="s", data_type="STRUCT<a INT64, arr ARRAY<STRUCT<x INT64, y STRING>>>",
                   is_nullable="YES"),
              dict(ordinal_position=2, column_name="l", data_type="ARRAY<INT64>", is_nullable="NO")]),
    "SELECT * EXCEPT": dict(
        body="SELECT * EXCEPT(_rn) FROM (SELECT 1 AS k, 1 AS _rn)", cols=columns("k")),
    "ANY_VALUE(... HAVING MAX ...)": dict(
        body=f"SELECT ANY_VALUE(status HAVING MAX finished_at) AS s FROM `{P}.ozon_raw.OZON_INGESTION_RUNS`",
        external=["ozon_raw.OZON_INGESTION_RUNS"], cols=columns("s", dtype="STRING")),
    "QUALIFY": dict(
        body=f"SELECT offer_id FROM `{P}.ozon_raw.RAW_OZON_PRICES` "
             "QUALIFY ROW_NUMBER() OVER (PARTITION BY offer_id ORDER BY snapshot_date DESC) = 1",
        external=["ozon_raw.RAW_OZON_PRICES"], cols=columns("offer_id", dtype="STRING")),
    "keyword-named column in backticks": dict(
        body="SELECT 1 AS `delete`, 2 AS `update`", cols=columns("delete", "update")),
}


@pytest.mark.parametrize("case", sorted(POSITIVE_FIXTURES))
def test_positive_fixture(repo, case):
    spec = dict(POSITIVE_FIXTURES[case])
    install(repo, LEAF, spec.pop("body"), spec.pop("cols"), **spec)
    assert_clean(repo)


# ------------------------------------------------------------------------------------------ manifest (C1-C5)

def test_duplicate_json_key(repo):
    replace_in_file(repo / MANIFEST, '"manifest_version": 2,', '"manifest_version": 2,\n  "manifest_version": 2,')
    assert_fails(repo, "C2", "duplicate JSON key")


def test_duplicate_json_key_inside_object(repo):
    replace_in_file(repo / MANIFEST, '"sync_state": "captured_live",',
                    '"sync_state": "captured_live",\n      "sync_state": "captured_live",')
    assert_fails(repo, "C2", "duplicate JSON key")


@pytest.mark.parametrize("version", [1, 3, "2", True])
def test_unsupported_manifest_version(repo, version):
    man = load(repo)
    man["manifest_version"] = version
    save(repo, man)
    assert_fails(repo, "C2", "unsupported manifest_version")


def test_v1_manifest_reports_unsupported_version(repo):
    man = load(repo)
    del man["allowed_external_datasets"]
    man["manifest_version"] = 1
    save(repo, man)
    assert_fails(repo, "C2", "unsupported manifest_version 1")


def test_missing_top_level_field(repo):
    man = load(repo)
    del man["allowed_external_datasets"]
    save(repo, man)
    assert_fails(repo, "C2", "missing top-level field")


def test_unknown_top_level_field(repo):
    man = load(repo)
    man["extra"] = 1
    save(repo, man)
    assert_fails(repo, "C2", "unknown top-level field")


def test_unknown_object_field(repo):
    man = load(repo)
    obj(man, LEAF)["note"] = "x"
    save(repo, man)
    assert_fails(repo, "C2", "unknown field")


def test_missing_object_field(repo):
    man = load(repo)
    del obj(man, LEAF)["canonical_schema_verification"]
    save(repo, man)
    assert_fails(repo, "C2", "missing field")


@pytest.mark.parametrize("field,value", [
    ("canonical_body_sha256", "ABC"),
    ("canonical_body_sha256", "E" * 64),
    ("live_body_sha256_at_capture", "0" * 63),
    ("canonical_schema_sha256", None),
    ("capture_main_sha", "eecde14"),
    ("captured_at", "2026-09-18 14:14:32"),
])
def test_malformed_hash_or_capture_field(repo, field, value):
    man = load(repo)
    obj(man, LEAF)[field] = value
    save(repo, man)
    assert_fails(repo, "C2")


def test_hash_contract_must_be_v1(repo):
    man = load(repo)
    man["hash_contract"] = "canonical_hash_v2"
    save(repo, man)
    assert_fails(repo, "C2", "hash_contract")


def test_stray_file_in_dataset_dir(repo):
    (repo / "sql/current/ozon_mart/notes.txt").write_text("x")
    assert_fails(repo, "C1", "unexpected file")


def test_stray_file_at_top_level(repo):
    (repo / "sql/current/extra.sql").write_text("SELECT 1")
    assert_fails(repo, "C1", "unexpected file")


def test_nested_directory(repo):
    (repo / "sql/current/ozon_mart/sub").mkdir()
    assert_fails(repo, "C1", "nested directory")


def test_missing_manifest(repo):
    (repo / MANIFEST).unlink()
    assert_fails(repo, "C1", "missing MANIFEST.json")


def test_symlinked_sql_file(repo):
    target = sql_path(repo, LEAF)
    real = repo / "elsewhere.sql"
    target.rename(real)
    os.symlink(real, target)
    assert_fails(repo, "C1", "symlink")


def test_missing_manifest_entry(repo):
    man = load(repo)
    man["objects"] = [o for o in man["objects"] if o["object_name"] != LEAF]
    man["rebuild_order"].remove(LEAF)
    save(repo, man)
    assert_fails(repo, "C3", "no manifest entry")


def test_extra_manifest_entry(repo):
    man = load(repo)
    ghost = dict(obj(man, LEAF), object_name="V_OZON_GHOST", canonical_path="sql/current/ozon_mart/V_OZON_GHOST.sql")
    man["objects"].append(ghost)
    save(repo, man)
    assert_fails(repo, "C3", "no SQL file")


@pytest.mark.parametrize("path", [
    "sql/current/ozon_mart/../ozon_mart/V_OZON_TARIFF_CHANGE_LOG.sql",
    "../../etc/V_OZON_TARIFF_CHANGE_LOG.sql",
    "/sql/current/ozon_mart/V_OZON_TARIFF_CHANGE_LOG.sql",
    "sql/ozon/stage3_4d2_ozon_mart_forward.sql",
])
def test_path_traversal_or_wrong_canonical_path(repo, path):
    man = load(repo)
    obj(man, LEAF)["canonical_path"] = path
    save(repo, man)
    assert_fails(repo, "C4")


def test_wrong_object_name_in_create(repo):
    replace_in_file(sql_path(repo, LEAF), f"{DS}.{LEAF}`", f"{DS}.V_OZON_SOMETHING_ELSE`")
    assert_fails(repo, "C5", "CREATE target")


def test_wrong_project_in_create_target(repo):
    replace_in_file(sql_path(repo, LEAF), f"`{P}.{DS}.{LEAF}`", f"`other-project-1.{DS}.{LEAF}`")
    assert_fails(repo, "C5", "CREATE target")


def test_duplicate_canonical_object_in_manifest(repo):
    man = load(repo)
    man["objects"].append(dict(obj(man, LEAF)))
    save(repo, man)
    assert_fails(repo, "C15", "declared 2 times")


def test_duplicate_canonical_object_across_files(repo):
    shutil.copy(sql_path(repo, LEAF), sql_path(repo, "V_OZON_TARIFF_CHANGE_LOG_COPY"))
    man = load(repo)
    man["objects"].append(dict(obj(man, LEAF), object_name="V_OZON_TARIFF_CHANGE_LOG_COPY",
                               canonical_path="sql/current/ozon_mart/V_OZON_TARIFF_CHANGE_LOG_COPY.sql"))
    save(repo, man)
    assert_fails(repo, "C15", "created by 2 canonical files")
    assert_fails(repo, "C5")


# ------------------------------------------------------------------------------------------ SQL safety (C6-C8)

def test_second_statement(repo):
    p = sql_path(repo, LEAF)
    p.write_text(p.read_text(encoding="utf-8") + "SELECT 1;\n", encoding="utf-8")
    assert_fails(repo, "C6")


def test_drop_appended(repo):
    p = sql_path(repo, LEAF)
    p.write_text(p.read_text(encoding="utf-8") + f"DROP TABLE `{P}.ozon_raw.RAW_OZON_PRICES`;\n", encoding="utf-8")
    assert_fails(repo, "C6")
    assert_fails(repo, "C8", "Drop")


def test_create_or_replace_table(repo):
    replace_in_file(sql_path(repo, LEAF), "CREATE OR REPLACE VIEW", "CREATE OR REPLACE TABLE")
    assert_fails(repo, "C7", "expected VIEW")


def test_create_materialized_view(repo):
    replace_in_file(sql_path(repo, LEAF), "CREATE OR REPLACE VIEW", "CREATE OR REPLACE MATERIALIZED VIEW")
    assert {"C7"} <= checks(repo)


def test_create_without_replace(repo):
    replace_in_file(sql_path(repo, LEAF), "CREATE OR REPLACE VIEW", "CREATE VIEW")
    assert_fails(repo, "C7", "CREATE OR REPLACE")


@pytest.mark.parametrize("stmt,node", [
    (f"DELETE FROM `{P}.ozon_raw.RAW_OZON_PRICES` WHERE TRUE", "Delete"),
    (f"INSERT INTO `{P}.ozon_raw.RAW_OZON_PRICES` (offer_id) VALUES ('x')", "Insert"),
    (f"UPDATE `{P}.ozon_raw.RAW_OZON_PRICES` SET offer_id = 'x' WHERE TRUE", "Update"),
    (f"MERGE `{P}.ozon_raw.RAW_OZON_PRICES` t USING (SELECT 1 AS k) s ON FALSE "
     "WHEN NOT MATCHED THEN INSERT (offer_id) VALUES ('x')", "Merge"),
    (f"TRUNCATE TABLE `{P}.ozon_raw.RAW_OZON_PRICES`", "TruncateTable"),
    (f"ALTER TABLE `{P}.ozon_raw.RAW_OZON_PRICES` ADD COLUMN x INT64", "Alter"),
    (f"DROP VIEW `{P}.{DS}.{LEAF}`", "Drop"),
])
def test_dml_ddl_statement_instead_of_view(repo, stmt, node):
    sql_path(repo, LEAF).write_text(f"-- header\n{stmt};\n", encoding="utf-8")
    assert_fails(repo, "C8", node)
    assert "C7" in checks(repo)


@pytest.mark.parametrize("stmt", [
    "EXECUTE IMMEDIATE 'DROP TABLE x'",
    f"CALL `{P}.ozon_raw.sp_cleanup`()",
    "BEGIN SELECT 1; END",
])
def test_command_fallback_statements(repo, stmt):
    sql_path(repo, LEAF).write_text(f"{stmt};\n", encoding="utf-8")
    assert_fails(repo, "C8", "Command")


@pytest.mark.parametrize("stmt", ["EXECUTE IMMEDIATE 'SELECT 1'", f"CALL `{P}.ozon_raw.sp`()"])
def test_command_after_valid_view(repo, stmt):
    p = sql_path(repo, LEAF)
    p.write_text(p.read_text(encoding="utf-8") + f"{stmt};\n", encoding="utf-8")
    assert_fails(repo, "C8", "forbidden statement keyword")
    assert_fails(repo, "C6")


@pytest.mark.parametrize("stmt", ["ASSERT FALSE", "ASSERT (SELECT COUNT(*) FROM t) > 0 AS 'x'"])
def test_assert(repo, stmt):
    # sqlglot has no ASSERT statement: it raises or mis-parses it, never as a CREATE VIEW.
    sql_path(repo, LEAF).write_text(f"{stmt};\n", encoding="utf-8")
    assert {"C6", "C7"} & checks(repo)
    assert_fails(repo, "C8", "exactly one CREATE")


def test_assert_after_view(repo):
    p = sql_path(repo, LEAF)
    p.write_text(p.read_text(encoding="utf-8") + "ASSERT FALSE AS 'x';\n", encoding="utf-8")
    assert_fails(repo, "C6")


def test_malformed_sql_command_fallback(repo):
    install(repo, LEAF, "SELEC 1 AS a", columns("a"))
    assert_fails(repo, "C8", "parser fallback")


def test_unparseable_sql(repo):
    install(repo, LEAF, "SELECT (1 AS a", columns("a"))
    assert "C6" in checks(repo)


def test_unsupported_view_option(repo):
    replace_in_file(sql_path(repo, LEAF), "OPTIONS (description = ",
                    "OPTIONS (expiration_timestamp = TIMESTAMP '2030-01-01', description = ")
    assert_fails(repo, "C16", "unsupported view option")


# ------------------------------------------------------------------------------------------ hashes (C9, C10, C16)

def test_wrong_body_hash(repo):
    man = load(repo)
    obj(man, LEAF)["canonical_body_sha256"] = "0" * 64
    save(repo, man)
    assert_fails(repo, "C9", "recomputed")


def test_body_edit_detected(repo):
    replace_in_file(sql_path(repo, LEAF), "SELECT", "SELECT  ")
    assert_fails(repo, "C9")


def test_wrong_schema_hash(repo):
    man = load(repo)
    obj(man, LEAF)["canonical_schema_sha256"] = "0" * 64
    save(repo, man)
    assert_fails(repo, "C10", "canonical_schema_sha256")


def test_wrong_column_count(repo):
    man = load(repo)
    obj(man, LEAF)["canonical_column_count"] += 1
    save(repo, man)
    assert_fails(repo, "C10", "canonical_column_count")


@pytest.mark.parametrize("mutate,text", [
    (lambda c: c[0].update(ordinal_position=5), "ordinal_position"),
    (lambda c: c[1].update(column_name=c[0]["column_name"].upper()), "duplicate column_name"),
    (lambda c: c[0].update(data_type="INTEGER"), "standard BigQuery type"),
    (lambda c: c[0].update(is_nullable="yes"), "is_nullable"),
    (lambda c: c[0].update(extra=1), "exactly the keys"),
])
def test_invalid_canonical_schema(repo, mutate, text):
    man = load(repo)
    o = obj(man, LEAF)
    mutate(o["canonical_schema"])
    o["canonical_schema_sha256"] = schema_hash(o["canonical_schema"])
    save(repo, man)
    assert_fails(repo, "C10", text)


def test_schema_names_differ_from_sql_projection(repo):
    install(repo, LEAF, "SELECT 1 AS a, 2 AS b", columns("a", "c"))
    assert_fails(repo, "C10", "outer projection")


def test_wrong_description_hash(repo):
    replace_in_file(sql_path(repo, LEAF), 'OPTIONS (description = "', 'OPTIONS (description = "X')
    assert_fails(repo, "C16", "canonical_description_sha256")


# ------------------------------------------------------------------------------------------ dependencies (C11-C14)

def test_undeclared_external_dependency(repo):
    install(repo, LEAF, f"SELECT offer_id FROM `{P}.ozon_raw.RAW_OZON_PRICES`", columns("offer_id", dtype="STRING"))
    assert_fails(repo, "C11", "external_dependencies")


def test_undeclared_internal_dependency(repo):
    install(repo, LEAF, f"SELECT 1 AS a FROM `{P}.{DS}.V_OZON_MART_FRESHNESS`", columns("a"))
    assert_fails(repo, "C11", "dependencies")


def test_reference_to_unmanaged_internal_object(repo):
    install(repo, LEAF, f"SELECT 1 AS a FROM `{P}.{DS}.V_OZON_NOT_MANAGED`", columns("a"),
            internal=["V_OZON_NOT_MANAGED"])
    assert_fails(repo, "C12", "not a managed canonical object")


def test_wrong_project_reference(repo):
    install(repo, LEAF, "SELECT 1 AS a FROM `other-project-1.ozon_raw.RAW_OZON_PRICES`", columns("a"),
            external=["ozon_raw.RAW_OZON_PRICES"])
    assert_fails(repo, "C12", "uses project")


def test_unqualified_reference(repo):
    install(repo, LEAF, "SELECT 1 AS a FROM ozon_raw.RAW_OZON_PRICES", columns("a"))
    assert_fails(repo, "C12", "fully qualified")


@pytest.mark.parametrize("body", [
    f"SELECT `{P}.wb_mart.fn`(1) AS a",
    f"SELECT {P.replace('-', '_')}.wb_mart.fn(1) AS a",
    "SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT 1')",
    f"SELECT * FROM `{P}.ozon_raw.RAW_OZON_*`",
    f"SELECT * FROM `{P}.region-eu`.INFORMATION_SCHEMA.VIEWS",
])
def test_hidden_or_unusual_references_rejected(repo, body):
    install(repo, LEAF, body, columns("a"))
    assert {"C12", "C13"} & checks(repo)


@pytest.mark.parametrize("ref", ["wb_mart.MART_SKU_DAILY", "wb_raw.RAW_WB_FINANCE", "wb_ops.X", "evetis_ops.OPS_CONFIG"])
def test_cross_marketplace_reference_fails_even_if_declared(repo, ref):
    install(repo, LEAF, f"SELECT 1 AS a FROM `{P}.{ref}`", columns("a"), external=[ref])
    assert_fails(repo, "C13")


def test_manifest_cannot_grant_itself_wb_dataset(repo):
    install(repo, LEAF, f"SELECT 1 AS a FROM `{P}.wb_mart.MART_SKU_DAILY`", columns("a"),
            external=["wb_mart.MART_SKU_DAILY"])
    man = load(repo)
    man["allowed_external_datasets"] = ["evetis_ref", "ozon_raw", "wb_mart"]
    save(repo, man)
    hits = assert_fails(repo, "C13")
    assert any("cannot extend the policy" in h.reason for h in hits)
    assert any("cross-marketplace reference" in h.reason for h in hits)


def test_manifest_grant_alone_fails(repo):
    man = load(repo)
    man["allowed_external_datasets"] = ["evetis_ref", "ozon_raw", "wb_mart"]
    save(repo, man)
    assert_fails(repo, "C13", "cannot extend the policy")


def test_manifest_subset_narrower_than_usage(repo):
    man = load(repo)
    man["allowed_external_datasets"] = ["ozon_raw"]
    save(repo, man)
    assert_fails(repo, "C12", "not in allowed_external_datasets")


def test_dataset_without_policy_fails_closed(repo):
    shutil.copytree(repo / "sql/current/ozon_mart", repo / "sql/current/wb_mart")
    assert_fails(repo, "C13", "no marketplace-isolation policy")


def test_dependency_cycle(repo):
    # V_OZON_SKU_UNIT_ECONOMICS_CURRENT already depends on FCT_OZON_SKU_PNL_MONTHLY; close the loop.
    install(repo, "FCT_OZON_SKU_PNL_MONTHLY", f"SELECT 1 AS a FROM `{P}.{DS}.V_OZON_SKU_UNIT_ECONOMICS_CURRENT`",
            columns("a"), internal=["V_OZON_SKU_UNIT_ECONOMICS_CURRENT"])
    assert_fails(repo, "C14", "cycle")


def test_self_reference_is_a_cycle(repo):
    install(repo, LEAF, f"SELECT 1 AS a FROM `{P}.{DS}.{LEAF}`", columns("a"), internal=[LEAF])
    assert_fails(repo, "C14", "cycle")


def test_wrong_level(repo):
    man = load(repo)
    obj(man, "V_OZON_LIFETIME_PNL")["dependency_level"] = 3  # computed level is 2
    save(repo, man)
    assert_fails(repo, "C14", "dependency_level")


def test_wrong_rebuild_order(repo):
    man = load(repo)
    order = man["rebuild_order"]
    order[0], order[-1] = order[-1], order[0]
    save(repo, man)
    assert_fails(repo, "C14", "rebuild_order")


def test_new_dependency_requires_level_update(repo):
    install(repo, LEAF, f"SELECT 1 AS a FROM `{P}.{DS}.V_OZON_AGENT_DECISION_INPUT`", columns("a"),
            internal=["V_OZON_AGENT_DECISION_INPUT"])
    assert_fails(repo, "C14", "computed 4")


# ------------------------------------------------------------------------------------------ sync_state (C18)

def test_valid_sync_states():
    """Every object is captured_live except the ones Gate 5K rewrote but did not deploy."""
    man = load(REPO)
    pending = {o["object_name"] for o in man["objects"] if o["sync_state"] == "pending_deploy"}
    assert pending == GATE5K_PENDING[DS]
    assert all(o["sync_state"] in ("captured_live", "pending_deploy") for o in man["objects"])
    assert_clean(REPO)


def test_captured_live_body_mismatch(repo):
    install(repo, LEAF, "SELECT 1 AS a", columns("a"), state="captured_live")
    assert_fails(repo, "C18", "live_body_sha256_at_capture")


def test_captured_live_schema_mismatch(repo):
    man = load(repo)
    o = obj(man, LEAF)
    o["canonical_schema"][-1]["is_nullable"] = "NO"
    o["canonical_schema_sha256"] = schema_hash(o["canonical_schema"])
    save(repo, man)
    assert_fails(repo, "C18", "live_schema_sha256_at_capture")


def test_valid_pending_deploy_keeps_live_capture(repo):
    before = obj(load(repo), LEAF)
    install(repo, LEAF, f"SELECT offer_id, 1 AS new_col FROM `{P}.ozon_raw.RAW_OZON_PRICES`",
            columns("offer_id", "new_col"), external=["ozon_raw.RAW_OZON_PRICES"])
    after = obj(load(repo), LEAF)
    for key in v.LIVE_CAPTURE_KEYS:
        assert after[key] == before[key], key
    assert after["canonical_body_sha256"] != after["live_body_sha256_at_capture"]
    assert after["canonical_schema_verification"] == "unverified"
    assert_clean(repo)


def test_pending_deploy_with_explicit_unverified_schema_contract(repo):
    install(repo, LEAF, "SELECT CAST(1 AS NUMERIC) AS amount_rub", columns("amount_rub", dtype="NUMERIC"))
    o = obj(load(repo), LEAF)
    assert o["canonical_schema"] and o["canonical_schema_verification"] == "unverified"
    assert_clean(repo)


def test_pending_deploy_null_canonical_schema(repo):
    install(repo, LEAF, "SELECT 1 AS a", columns("a"))
    man = load(repo)
    obj(man, LEAF)["canonical_schema"] = None
    save(repo, man)
    assert_fails(repo, "C10", "canonical_schema is required")


def test_pending_deploy_missing_canonical_schema(repo):
    install(repo, LEAF, "SELECT 1 AS a", columns("a"))
    man = load(repo)
    del obj(man, LEAF)["canonical_schema"]
    save(repo, man)
    assert_fails(repo, "C2", "missing field")


def test_pending_deploy_cannot_claim_bigquery_verified(repo):
    install(repo, LEAF, "SELECT 1 AS a", columns("a"))
    man = load(repo)
    obj(man, LEAF)["canonical_schema_verification"] = "bigquery_verified"
    save(repo, man)
    assert_fails(repo, "C18", "not allowed for sync_state 'pending_deploy'")


@pytest.mark.parametrize("value", ["unverified", "unknown", None])
def test_captured_live_requires_bigquery_verified(repo, value):
    man = load(repo)
    obj(man, LEAF)["canonical_schema_verification"] = value
    save(repo, man)
    assert_fails(repo, "C18", "canonical_schema_verification")


def test_unknown_sync_state(repo):
    man = load(repo)
    obj(man, LEAF)["sync_state"] = "deployed"
    save(repo, man)
    assert_fails(repo, "C18", "sync_state")


def test_pending_deploy_with_live_capture_rewritten_to_match(repo):
    install(repo, LEAF, "SELECT 1 AS a", columns("a"))
    man = load(repo)
    o = obj(man, LEAF)
    o["live_body_sha256_at_capture"] = o["canonical_body_sha256"]
    o["live_schema_sha256_at_capture"] = o["canonical_schema_sha256"]
    o["live_schema_at_capture"] = o["canonical_schema"]
    o["live_description_sha256_at_capture"] = o["canonical_description_sha256"]
    save(repo, man)
    assert_fails(repo, "C18", "nothing is pending")


def test_pending_deploy_unchanged_is_rejected(repo):
    man = load(repo)
    obj(man, LEAF).update(sync_state="pending_deploy", canonical_schema_verification="unverified")
    save(repo, man)
    assert_fails(repo, "C18", "nothing is pending")


def test_new_never_captured_object_pending_deploy(repo):
    name = "V_OZON_NEW_OBJECT"
    body = f"SELECT offer_id FROM `{P}.ozon_raw.RAW_OZON_PRICES`"
    sql_path(repo, name).write_text(view_text(name, body), encoding="utf-8")
    man = load(repo)
    cols = columns("offer_id", dtype="STRING")
    new = dict(obj(man, LEAF), object_name=name, canonical_path=f"sql/current/ozon_mart/{name}.sql",
               sync_state="pending_deploy", canonical_schema_verification="unverified",
               external_dependencies=["ozon_raw.RAW_OZON_PRICES"], dependencies=[], dependency_level=0,
               canonical_body_sha256=sha(body), canonical_description_sha256=sha("fixture"),
               canonical_schema=cols, canonical_schema_sha256=schema_hash(cols), canonical_column_count=1,
               provenance=dict(source="git-first", historical_migration=None, preflight_parity=None,
                               superseded_historical_definitions=[]))
    for key in v.LIVE_CAPTURE_KEYS:
        new[key] = None
    man["objects"].append(new)
    levels = {o["object_name"]: o["dependency_level"] for o in man["objects"]}
    man["rebuild_order"] = sorted(levels, key=lambda n: (levels[n], n))
    save(repo, man)
    assert_clean(repo)
    new["sync_state"] = "captured_live"
    new["canonical_schema_verification"] = "bigquery_verified"
    save(repo, man)
    assert_fails(repo, "C18", "never-captured")


def test_partial_null_live_capture(repo):
    install(repo, LEAF, "SELECT 1 AS a", columns("a"))
    man = load(repo)
    obj(man, LEAF)["live_body_sha256_at_capture"] = None
    save(repo, man)
    assert_fails(repo, "C18", "all present or all null")


# ------------------------------------------------------------------------------------------ C17 historical guard

COMPETING = f"CREATE OR REPLACE VIEW `{P}.ozon_mart.V_OZON_LIFETIME_PNL` AS SELECT 1 AS a;\n"


def test_c17_new_competing_historical_creator(repo):
    (repo / "sql/ozon/stage9_new.sql").write_text(COMPETING, encoding="utf-8")
    hits = assert_fails(repo, "C17", "new competing definition")
    assert hits[0].subject == "sql/ozon/stage9_new.sql"


@pytest.mark.parametrize("text", [
    "create view ozon_mart.V_OZON_LIFETIME_PNL as select 1;",
    "CREATE VIEW V_OZON_LIFETIME_PNL AS SELECT 1;",
    f"CREATE OR REPLACE TABLE {P}.ozon_mart.V_OZON_LIFETIME_PNL (a INT64);",
    "CREATE TABLE IF NOT EXISTS `p`.`ozon_mart`.`V_OZON_LIFETIME_PNL` (a INT64);",
    "CREATE MATERIALIZED VIEW `p.ozon_mart.V_OZON_LIFETIME_PNL` AS SELECT 1;",
])
def test_c17_competing_creator_spellings(repo, text):
    (repo / "sql/misc_new.sql").write_text(text, encoding="utf-8")
    assert_fails(repo, "C17", "new competing definition")


@pytest.mark.parametrize("text", [
    f"-- {COMPETING}",
    f"/* {COMPETING} */ SELECT 1;",
    f"SELECT '{COMPETING.strip().rstrip(';')}' AS s;",
    f"CREATE OR REPLACE VIEW `{P}.ozon_mart_sandbox.V_OZON_LIFETIME_PNL` AS SELECT 1;",
    f"CREATE OR REPLACE VIEW `{P}.ozon_mart.V_OZON_LIFETIME_PNL_COPY` AS SELECT 1;",
])
def test_c17_non_definitions_are_ignored(repo, text):
    (repo / "sql/misc_new.sql").write_text(text, encoding="utf-8")
    assert_clean(repo)


def test_c17_wrong_occurrence_count_in_file(repo):
    p = repo / "sql/ozon/stage3_4c_ozon_mart.sql"
    p.write_text(p.read_text(encoding="utf-8") + "\n" + COMPETING, encoding="utf-8")
    assert_fails(repo, "C17", "!= allowlisted")


def test_c17_wrong_declared_count(repo):
    data = load(repo, HISTORICAL)
    data["entries"][0]["definitions"]["ozon_mart.FCT_OZON_PNL_MONTHLY"] = 2
    save(repo, data, HISTORICAL)
    assert_fails(repo, "C17", "!= allowlisted")


def test_c17_stale_entry(repo):
    p = repo / "sql/ozon/stage3_4c_ozon_mart.sql"
    p.write_text("-- emptied\n", encoding="utf-8")
    assert_fails(repo, "C17", "stale entry")


@pytest.mark.parametrize("path,text", [
    ("../outside.sql", "safe relative .sql path"),
    ("sql/ozon/../ozon/stage3_4c_ozon_mart.sql", "safe relative .sql path"),
    ("sql/current/ozon_mart/V_OZON_LIFETIME_PNL.sql", "outside sql/current/"),
    ("sql/ozon/does_not_exist.sql", "does not exist"),
])
def test_c17_allowlist_bad_path(repo, path, text):
    data = load(repo, HISTORICAL)
    data["entries"].append({"path": path, "definitions": {"ozon_mart.V_OZON_LIFETIME_PNL": 1}})
    save(repo, data, HISTORICAL)
    assert_fails(repo, "C17", text)


def test_c17_allowlist_duplicate_entry(repo):
    data = load(repo, HISTORICAL)
    data["entries"].append(dict(data["entries"][0]))
    save(repo, data, HISTORICAL)
    assert_fails(repo, "C17", "duplicate allowlist entry")


def test_c17_allowlist_duplicate_json_key(repo):
    replace_in_file(repo / HISTORICAL, '"ozon_mart.FCT_OZON_PNL_MONTHLY": 1,',
                    '"ozon_mart.FCT_OZON_PNL_MONTHLY": 1,\n        "ozon_mart.FCT_OZON_PNL_MONTHLY": 1,')
    assert_fails(repo, "C17", "duplicate JSON key")


def test_c17_allowlist_non_canonical_object(repo):
    data = load(repo, HISTORICAL)
    data["entries"][0]["definitions"]["wb_mart.MART_SKU_DAILY"] = 1
    save(repo, data, HISTORICAL)
    assert_fails(repo, "C17", "is not a canonical object")


def test_c17_allowlist_unknown_field(repo):
    data = load(repo, HISTORICAL)
    data["entries"][0]["note"] = "x"
    save(repo, data, HISTORICAL)
    assert_fails(repo, "C17", "exactly the keys")


def test_c17_missing_allowlist(repo):
    (repo / HISTORICAL).unlink()
    assert_fails(repo, "C17", "missing")


def test_c17_does_not_require_historical_parity_or_parse(repo):
    p = repo / "sql/ozon/stage3_4d2_ozon_mart_forward.sql"
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace("SELECT", "SELEC", 3) + "\nTHIS IS NOT SQL (;\n", encoding="utf-8")
    assert_clean(repo)


# ------------------------------------------------------------------------------------------ error collection, CLI, offline

def test_multiple_violations_are_collected(repo):
    man = load(repo)
    obj(man, LEAF)["canonical_body_sha256"] = "0" * 64
    obj(man, "V_OZON_LIFETIME_PNL")["dependency_level"] = 7
    save(repo, man)
    (repo / "sql/ozon/stage9_new.sql").write_text(COMPETING, encoding="utf-8")
    assert {"C9", "C14", "C17"} <= checks(repo)
    for f in findings(repo):
        assert f.check and f.subject and f.reason


def run_cli(root):
    return subprocess.run([sys.executable, str(REPO / "tools/validate_current_sql.py"), "--root", str(root)],
                          capture_output=True, text=True, timeout=120)


def test_cli_exit_codes(repo):
    ok = run_cli(REPO)
    assert ok.returncode == 0 and ok.stdout.startswith("OK:"), ok.stdout + ok.stderr
    assert "NOT proven" in ok.stdout
    man = load(repo)
    obj(man, LEAF)["canonical_body_sha256"] = "0" * 64
    save(repo, man)
    bad = run_cli(repo)
    assert bad.returncode == 1
    assert f"FAIL [C9] {DS}.{LEAF}:" in bad.stdout


def test_cli_fails_closed_on_other_sqlglot_version(monkeypatch):
    monkeypatch.setattr(v.sqlglot, "__version__", "0.0.0")
    assert v.main(["--root", str(REPO)]) == 2


def test_cli_fails_closed_on_internal_error(monkeypatch):
    def boom(_root):
        raise RuntimeError("x")
    monkeypatch.setattr(v, "validate", boom)
    assert v.main(["--root", str(REPO)]) == 2


class _NoEnviron(dict):
    def __getitem__(self, key):
        raise AssertionError(f"validator read environment variable {key!r}")

    def get(self, key, default=None):
        raise AssertionError(f"validator read environment variable {key!r}")


def test_validator_uses_no_network_and_no_environment(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("validator attempted network access")
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(os, "environ", _NoEnviron())
    assert_clean(REPO)


def test_validator_source_has_no_network_credential_or_subprocess_use():
    src = (REPO / "tools/validate_current_sql.py").read_text(encoding="utf-8")
    for needle in ("os.environ", "getenv", "import socket", "urllib", "http.client", "import requests",
                   "subprocess", "google.", "gcloud"):
        assert needle not in src, needle
