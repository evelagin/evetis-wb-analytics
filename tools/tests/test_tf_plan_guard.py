"""tools/tf_plan_guard.py — точная сверка состава Terraform-плана перед apply."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tf_plan_guard as g  # noqa: E402


def plan(*changes):
    return {"resource_changes": [{"address": a, "change": {"actions": acts}} for a, acts in changes]}


MIG1C = (
    "google_bigquery_table.raw_wb_stocks_t5_cr=create,"
    "google_bigquery_table.wb_stocks_snapshots_cr=update,"
    "google_bigquery_table_iam_member.shadow_write_raw_t5_cr=create,"
    "google_cloud_run_v2_job.wb_stocks_shadow=update"
)
MIG1C_PLAN = plan(
    ("google_bigquery_table.raw_wb_stocks_t5_cr", ["create"]),
    ("google_bigquery_table.wb_stocks_snapshots_cr", ["update"]),
    ("google_bigquery_table_iam_member.shadow_write_raw_t5_cr", ["create"]),
    ("google_cloud_run_v2_job.wb_stocks_shadow", ["update"]),
    ("google_service_account.loaders_shadow", ["no-op"]),
    ('google_project_service.enabled["run.googleapis.com"]', ["no-op"]),
)


def run(tmp_path, p, spec):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps(p), encoding="utf-8")
    return g.main(["x", str(f), spec])


def test_exact_match_passes_and_ignores_noop(tmp_path):
    assert run(tmp_path, MIG1C_PLAN, MIG1C) == 0


def test_replace_of_manifest_is_refused(tmp_path):
    p = json.loads(json.dumps(MIG1C_PLAN))
    p["resource_changes"][1]["change"]["actions"] = ["delete", "create"]
    assert run(tmp_path, p, MIG1C) == 1
    assert g.classify(["create", "delete"]) == "replace"


def test_unexpected_extra_resource_is_refused(tmp_path):
    p = json.loads(json.dumps(MIG1C_PLAN))
    p["resource_changes"].append({"address": "google_cloud_run_v2_job.wb_stocks_prod", "change": {"actions": ["update"]}})
    assert run(tmp_path, p, MIG1C) == 1


def test_missing_expected_change_is_refused(tmp_path):
    p = plan(("google_cloud_run_v2_job.wb_stocks_shadow", ["update"]))
    assert run(tmp_path, p, MIG1C) == 1


def test_bad_spec_is_input_error(tmp_path):
    assert run(tmp_path, MIG1C_PLAN, "google_x.y") == 2
    assert run(tmp_path, MIG1C_PLAN, "google_x.y=destroy") == 2
    assert run(tmp_path, MIG1C_PLAN, "a=create,a=update") == 2
    assert run(tmp_path, MIG1C_PLAN, " , ") == 2


def test_indexed_address_with_equals_inside_key(tmp_path):
    p = plan(('google_project_service.enabled["a=b"]', ["update"]))
    assert run(tmp_path, p, 'google_project_service.enabled["a=b"]=update') == 0
