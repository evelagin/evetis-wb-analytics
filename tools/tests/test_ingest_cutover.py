"""Этап C (OWNER ACK 11.11.2026): DDL таблиц владения и Terraform координатора cutover.

Проверяется статически (без облака):
  * флаг cutover_coordinator_enabled — bool, default false, подключён в infra.yml по схеме TF_VAR;
  * каждый ресурс ingest_cutover.tf создаётся ТОЛЬКО при флаге;
  * у sa-ingest-cutover нет лишних ролей: jobUser + своя роль ровно с bigquery.jobs.listAll, dataEditor —
    только три таблицы wb_ops, dataViewer — только поресурсно на журналы/RAW; ни Scheduler, ни Run admin,
    ни секретов;
  * Scheduler ingest-cutover-tick создаётся на паузе, Job — на bootstrap-образе с args ["cutover"];
  * DDL: три таблицы, seed шести потоков (APPS_SCRIPT / LEGACY_ACTIVE / epoch 1), seed не меняет
    существующие строки; колонки совпадают с тем, что читает и пишет код координатора.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TF = (REPO / "infra/terraform/ingest_cutover.tf").read_text(encoding="utf-8")
INFRA = (REPO / ".github/workflows/infra.yml").read_text(encoding="utf-8")
DDL = (REPO / "sql/ops/ingest_writer_ownership.sql").read_text(encoding="utf-8")
STORE = (REPO / "cloud/src/loaders/cutover/store.ts").read_text(encoding="utf-8")
GATE_TS = (REPO / "cloud/src/ownership/gate.ts").read_text(encoding="utf-8")
GATE_GS = (REPO / "apps-script/ingestion/WriterGate.gs").read_text(encoding="utf-8")

RES_RE = re.compile(r'^resource\s+"([a-z0-9_]+)"\s+"([a-z0-9_]+)"\s*\{(.*?)^\}', re.S | re.M)
VAR_RE = re.compile(r'^variable\s+"([a-z0-9_]+)"\s*\{(.*?)^\}', re.S | re.M)


def _resources():
    return [(t, n, body) for t, n, body in RES_RE.findall(TF)]


def test_flag_is_bool_default_false_and_wired_from_github_variable():
    vars_ = dict(VAR_RE.findall(TF))
    body = vars_["cutover_coordinator_enabled"]
    assert re.search(r"type\s*=\s*bool", body) and re.search(r"default\s*=\s*false", body)
    line = "  TF_VAR_cutover_coordinator_enabled: ${{ vars.CUTOVER_COORDINATOR_ENABLED == 'true' }}"
    assert INFRA.count(line) == 1
    assert re.search(r"default\s*=\s*\[\]", vars_["cutover_gate_reader_streams"])
    assert INFRA.count("TF_VAR_cutover_gate_reader_streams: ${{ vars.CUTOVER_GATE_READER_STREAMS || '[]' }}") == 1
    # Флаг не притворяется флагом контура потока (test_stream_enable_flags: только mN_{shadow,prod}_enabled).
    assert not re.search(r"(shadow|prod)_(enabled|on)$", "cutover_coordinator_enabled")


def test_every_resource_is_behind_the_flag():
    res = _resources()
    assert len(res) >= 10
    for t, n, body in res:
        head = body.strip().splitlines()[0]
        assert "var.cutover_coordinator_enabled" in head, f"{t}.{n}: первая строка — не условие флага ({head})"


def test_coordinator_sa_has_no_extra_roles():
    sa = 'google_service_account.ingest_cutover[0].email'
    project_roles, table_roles, other = [], [], []
    for t, n, body in _resources():
        if sa not in body:
            continue
        role = re.search(r'role\s*=\s*"?([^"\n]+)"?', body)
        role = role.group(1).strip() if role else ""
        if t == "google_project_iam_member":
            project_roles.append(role)
        elif t == "google_bigquery_table_iam_member":
            table_roles.append((n, role, body))
        elif t in ("google_cloud_run_v2_job",):
            continue
        else:
            other.append((t, n))
    # Своя роль вместо jobUser (у jobUser побочные права — Dataform, Gemini).
    assert project_roles == ["google_project_iam_custom_role.ingest_cutover_bigquery[0].id"]
    custom = re.search(r'resource "google_project_iam_custom_role" "ingest_cutover_bigquery" \{(.*?)^\}', TF, re.S | re.M).group(1)
    assert re.search(r'permissions\s*=\s*\["bigquery\.jobs\.create", "bigquery\.config\.get", "bigquery\.jobs\.listAll"\]', custom)
    assert "roles/bigquery.jobUser" not in TF
    assert other == [], other
    edit = [(n, b) for n, r, b in table_roles if r == "roles/bigquery.dataEditor"]
    view = [(n, b) for n, r, b in table_roles if r == "roles/bigquery.dataViewer"]
    assert [n for n, _ in edit] == ["ingest_cutover_edit_ops"] and 'dataset_id = "wb_ops"' in edit[0][1]
    assert sorted(n for n, _ in view) == ["ingest_cutover_read_evidence", "ingest_cutover_read_qualification"]
    assert "var.raw_dataset" in dict(view)["ingest_cutover_read_evidence"]
    assert "local.cutover_qualification_table" in dict(view)["ingest_cutover_read_qualification"]
    assert {r for _, r, _ in table_roles} == {"roles/bigquery.dataEditor", "roles/bigquery.dataViewer"}
    ops = re.search(r"cutover_ops_tables\s*=\s*\{(.*?)\}", TF, re.S).group(1)
    # Пишет координатор только владение и журнал; квалификацию — читает (ревью #426).
    assert sorted(re.findall(r'"([A-Z_]+)"', ops)) == ["INGEST_CUTOVER_LOG", "INGEST_WRITER_OWNERSHIP"]
    # Нет ни Scheduler, ни Run admin, ни секретов, ни IAM-администрирования — ни в одной роли файла.
    for bad in ("scheduler", "run.admin", "run.developer", "secretmanager", "iam.serviceAccountUser", "bigquery.admin", "dataOwner"):
        assert bad not in TF.replace("google_cloud_scheduler_job", "").replace("scheduler_prod", "").replace("scheduler_ingest", ""), bad
    # Датасет целиком координатору не выдаётся (писателю квалификации — только чтение wb_raw, см. ниже).
    for t, n, body in _resources():
        if t == "google_bigquery_dataset_iam_member":
            assert sa not in body, n


def test_evidence_tables_cover_what_the_coordinator_reads():
    read = set(re.findall(r"this\.raw\('([A-Z_]+)'\)", STORE))
    tf = set(re.findall(r'"([A-Z_0-9]+)"', re.search(r"cutover_evidence_tables\s*=\s*toset\(\[(.*?)\]\)", TF, re.S).group(1)))
    assert read <= tf, read - tf
    assert tf <= read | {"RAW_WB_ADV_COSTS_RUNS"}, tf - read


def test_loader_sa_reads_only_the_ownership_table():
    for name in ("writer_gate_read_loaders_prod", "writer_gate_read_stream_prod", "writer_gate_read_deployer"):
        body = re.search(rf'resource "google_bigquery_table_iam_member" "{name}" \{{(.*?)^\}}', TF, re.S | re.M).group(1)
        assert "local.cutover_ops_tables.ownership" in body and 'roles/bigquery.dataViewer' in body


def test_job_and_scheduler_shape():
    job = re.search(r'resource "google_cloud_run_v2_job" "ingest_cutover_prod" \{(.*?)^\}', TF, re.S | re.M).group(1)
    assert 'name                = "ingest-cutover-prod"' in job
    assert 'args  = ["cutover"]' in job and "image = var.container_image" in job
    assert "google_service_account.ingest_cutover[0].email" in job and "max_retries     = 0" in job
    sch = re.search(r'resource "google_cloud_scheduler_job" "ingest_cutover_tick" \{(.*?)^\}', TF, re.S | re.M).group(1)
    assert 'name             = "ingest-cutover-tick"' in sch and "paused           = true" in sch
    assert "ignore_changes = [paused]" in sch
    assert "google_service_account.scheduler_prod.email" in sch


def test_ddl_tables_columns_and_seed():
    for t in ("INGEST_WRITER_OWNERSHIP", "INGEST_CUTOVER_LOG", "INGEST_QUALIFICATION"):
        assert f"CREATE TABLE IF NOT EXISTS `wb_ops.{t}`" in DDL, t
    own = DDL[DDL.index("`wb_ops.INGEST_WRITER_OWNERSHIP` ("):DDL.index("OPTIONS (description = 'Этап C: единственный")]
    for col in ("stream", "owner", "state", "epoch", "revision", "legacy_watermark", "cloud_watermark",
                "auto_cutover_enabled", "fenced_at", "active_since", "checks_since", "changed_at", "changed_by", "reason"):
        assert re.search(rf"^\s+{col}\s", own, re.M), col
        assert col in STORE, f"колонка {col} не используется store.ts"
    log = DDL[DDL.index("`wb_ops.INGEST_CUTOVER_LOG` ("):DDL.index("PARTITION BY DATE(transition_at)")]
    for col in ("transition_id", "stream", "from_state", "to_state", "epoch", "run_id", "source_watermark", "reason", "evidence", "transition_at"):
        assert re.search(rf"^\s+{col}\s", log, re.M), col
    q = DDL[DDL.index("`wb_ops.INGEST_QUALIFICATION` ("):DDL.index("PARTITION BY business_date")]
    for col in ("stream", "business_date", "check_group", "verdict", "details", "image_digest", "verdict_at"):
        assert re.search(rf"^\s+{col}\s", q, re.M), col
    # Seed: шесть потоков, Apps Script, LEGACY_ACTIVE, epoch 1; существующие строки не трогаются.
    assert "UNNEST(['stocks', 'statistics', 'ads', 'finance', 'supplies', 'ref'])" in DDL
    assert "DECLARE stocks_owner STRING DEFAULT 'APPS_SCRIPT';" in DDL
    assert DDL.index("DECLARE stocks_owner") < DDL.index("CREATE TABLE IF NOT EXISTS `")
    seed = DDL[DDL.index("MERGE `wb_ops.INGEST_WRITER_OWNERSHIP`"):DDL.index("-- 5)")]
    assert "WHEN NOT MATCHED THEN" in seed and "WHEN MATCHED" not in seed.replace("WHEN NOT MATCHED", "")
    assert "VALUES (S.stream, S.owner, S.state, 1, 0, FALSE," in seed
    assert "IF(s = 'stocks', stocks_owner, 'APPS_SCRIPT')" in seed
    assert "'LEGACY_ACTIVE'" in seed


def test_one_table_name_everywhere():
    assert "OWNERSHIP_TABLE = 'INGEST_WRITER_OWNERSHIP'" in (REPO / "cloud/src/ownership/streams.ts").read_text(encoding="utf-8")
    assert "var WB_WRITER_GATE_TABLE_ = 'INGEST_WRITER_OWNERSHIP';" in GATE_GS
    assert "var WB_WRITER_GATE_DATASET_ = 'wb_ops';" in GATE_GS
    assert "DEFAULT_OWNERSHIP_DATASET = 'wb_ops'" in (REPO / "cloud/src/ownership/streams.ts").read_text(encoding="utf-8")
    # Gate Apps Script — короткий параметризованный SELECT с потолком тарификации.
    assert "WHERE stream = @s" in GATE_GS and "maximumBytesBilled" in GATE_GS
    assert "WRITER_EPOCH_CHANGED" in GATE_TS and "WRITER_EPOCH_CHANGED" in GATE_GS


def test_no_reserved_word_columns():
    # Ревью #426 (B1): `at` — зарезервированное слово BigQuery; ни в DDL, ни в SQL координатора.
    code = re.sub(r"--[^\n]*", "", DDL)
    assert not re.search(r"(?<![A-Za-z_])at\s+TIMESTAMP", code)
    assert not re.search(r"\(at\)|,\s*at\)|UNIX_MILLIS\(at\)", code + STORE)


def test_qualify_writer_sa_and_job():
    sa = "google_service_account.ingest_qualify[0].email"
    roles = [(t, n, re.search(r'role\s*=\s*"?([^"\n]+)"?', b).group(1).strip(), b) for t, n, b in _resources() if sa in b and "iam" in t]
    got = sorted((t, r) for t, _, r, _ in roles)
    assert got == sorted([
        ("google_project_iam_member", "google_project_iam_custom_role.ingest_qualify_bigquery[0].id"),
        ("google_bigquery_dataset_iam_member", "roles/bigquery.dataViewer"),
        ("google_bigquery_table_iam_member", "roles/bigquery.dataEditor"),
    ]), got
    write = [b for t, _, r, b in roles if r == "roles/bigquery.dataEditor"][0]
    assert "local.cutover_qualification_table" in write and 'dataset_id = "wb_ops"' in write
    read = [b for t, _, r, b in roles if t == "google_bigquery_dataset_iam_member"][0]
    assert "var.raw_dataset" in read
    custom = re.search(r'resource "google_project_iam_custom_role" "ingest_qualify_bigquery" \{(.*?)^\}', TF, re.S | re.M).group(1)
    assert re.search(r'permissions\s*=\s*\["bigquery\.jobs\.create", "bigquery\.config\.get"\]', custom)
    job = re.search(r'resource "google_cloud_run_v2_job" "ingest_qualify_prod" \{(.*?)^\}', TF, re.S | re.M).group(1)
    assert 'args  = ["qualify"]' in job and "image = var.container_image" in job
    sch = re.search(r'resource "google_cloud_scheduler_job" "ingest_qualify" \{(.*?)^\}', TF, re.S | re.M).group(1)
    assert "paused           = true" in sch


def test_identities_wired_without_personal_data():
    assert INFRA.count("TF_VAR_cutover_apps_script_user: ${{ vars.CUTOVER_APPS_SCRIPT_USER }}") == 1
    assert re.search(r'variable "cutover_apps_script_user" \{[^}]*default\s*=\s*""', TF, re.S)
    assert "CUTOVER_STREAM_PROD_SAS = jsonencode(local.cutover_writer_sas)" in TF
    assert not re.search(r"[A-Za-z0-9._%+-]+@(gmail|yandex|mail|evetis)\.", TF)


def test_parity_sql_bundled_into_image():
    docker = (REPO / "cloud/Dockerfile").read_text(encoding="utf-8")
    shadow = (REPO / ".github/workflows/deploy-shadow.yml").read_text(encoding="utf-8")
    assert "COPY parity-sql ./parity-sql" in docker and (REPO / "cloud/parity-sql/.gitkeep").exists()
    assert "cp ../sql/mig_*_parity.sql parity-sql/ 2>/dev/null || true" in shadow
    assert shadow.index("cp ../sql/mig_*_parity.sql") < shadow.index("docker build")
