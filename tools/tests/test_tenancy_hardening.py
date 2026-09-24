"""Tenancy T2.1: укрепление реестра и пространства имён до создания первого проекта.

Закрывает находки независимого ревью PR #171:
  L1 — скрытый tenant.json в зарезервированном каталоге _schema/;
  L2 — повторяющиеся ключи JSON (разные парсеры видят разное значение);
  L5 — «$» в Python совпадает перед завершающим «\\n»: неполное совпадение строки;
  L6 — неоднозначный суффикс ID проекта (client + «001» = client_001);
  I1 — schema_version: true и 1.0 проходили const: 1.
"""
from __future__ import annotations

import ast
import copy
import itertools
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.tenancy import naming as N  # noqa: E402
from tools.tenancy import registry as R  # noqa: E402
from tools.tenancy import validation as V  # noqa: E402

TENANTS = REPO / "tenants"
EVETIS = V.load_tenant_document(TENANTS / "evetis" / "tenant.json")
CLIENT = V.load_tenant_document(TENANTS / "client_001" / "tenant.json")


def rules(doc):
    return {f.rule for f in V.validate_tenant(doc, "t.json")}


def put(doc, path, value):
    d = copy.deepcopy(doc)
    node, keys = d, path.split(".")
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value
    return d


@pytest.fixture
def registry_copy(tmp_path):
    root = tmp_path / "tenants"
    shutil.copytree(TENANTS, root)
    assert R.validate_root(root) == []
    return root


# ═══════════════════════════════════════════════ L1 — зарезервированный каталог
def test_hidden_tenant_json_in_schema_dir_fails(registry_copy):
    bad = put(CLIENT, "data_boundary.gcp_project_id", N.LEGACY_EVETIS["gcp_project_id"])
    (registry_copy / "_schema" / "tenant.json").write_text(json.dumps(bad), encoding="utf-8")
    found = R.validate_root(registry_copy)
    assert any("_schema/tenant.json" in f.source for f in found)
    with pytest.raises(R.RegistryInvalid):
        R.valid_tenants(registry_copy)          # потребитель не получает НИЧЕГО


@pytest.mark.parametrize("name", ["notes.md", "tenant.schema.json.bak", "extra.json", ".DS_Store"])
def test_unexpected_file_in_schema_dir_fails(registry_copy, name):
    (registry_copy / "_schema" / name).write_text("{}", encoding="utf-8")
    assert any(name in f.source for f in R.validate_root(registry_copy))


def test_subdirectory_in_schema_dir_fails(registry_copy):
    (registry_copy / "_schema" / "client_009").mkdir()
    (registry_copy / "_schema" / "client_009" / "tenant.json").write_text("{}", encoding="utf-8")
    assert any("client_009" in f.source for f in R.validate_root(registry_copy))


def test_schema_name_must_be_a_directory(tmp_path):
    root = tmp_path / "tenants"
    root.mkdir()
    shutil.copytree(TENANTS / "evetis", root / "evetis")
    (root / "_schema").write_text("x", encoding="utf-8")
    assert any("_schema" in f.source for f in R.validate_root(root))


# «Client_002», а не «Client_001»: на macOS файловая система нечувствительна к регистру
# и не даст создать каталог рядом с client_001; на Linux CI имена различны.
@pytest.mark.parametrize("name", [".hidden", "Client_002", "client-002", "x", "_private"])
def test_directory_names_must_be_tenant_ids(registry_copy, name):
    (registry_copy / name).mkdir()
    assert any(f.rule == "registry" and name in f.source for f in R.validate_root(registry_copy))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="нет символических ссылок")
def test_symlinks_are_rejected_everywhere(registry_copy):
    os.symlink(registry_copy / "evetis", registry_copy / "client_009")
    assert any("символические" in f.message for f in R.validate_root(registry_copy))
    os.unlink(registry_copy / "client_009")

    (registry_copy / "client_010").mkdir()
    os.symlink(registry_copy / "client_001" / "tenant.json", registry_copy / "client_010" / "tenant.json")
    assert any("client_010" in f.source for f in R.validate_root(registry_copy))
    shutil.rmtree(registry_copy / "client_010")

    real = registry_copy / "_schema" / "tenant.schema.json"
    target = registry_copy.parent / "schema_copy.json"
    shutil.move(real, target)
    os.symlink(target, real)
    assert any("_schema" in f.source for f in R.validate_root(registry_copy))


# ═══════════════════════════════════════════════════ L2 — повторяющиеся ключи
@pytest.mark.parametrize("text", [
    '{"tenant_id": "client_001", "tenant_id": "evetis"}',                     # верхний уровень
    '{"tenant_id": "client_001", "tenant_id": "client_001"}',                 # то же значение
    '{"marketplaces": {"ozon": {"enabled": true, "enabled": false}}}',        # вложенный
    '{"marketplaces": {"ozon": {"enabled": true, "enabled": true}}}',         # вложенный, то же
    '{"a": [{"k": 1, "k": 2}]}',                                              # объект в массиве
])
def test_duplicate_keys_are_rejected_at_any_depth(text):
    with pytest.raises(V.TenantDocumentError, match="повторяющийся ключ"):
        V.parse_tenant_json(text)


@pytest.mark.parametrize("text", ['{"schema_version": NaN}', '{"x": Infinity}', '{"x": -Infinity}'])
def test_non_standard_constants_are_rejected(text):
    with pytest.raises(V.TenantDocumentError, match="нестандартная константа"):
        V.parse_tenant_json(text)


def test_duplicate_key_error_leaks_neither_values_nor_secret_like_keys():
    uuid = "0b7c0f3e-5a41-4c0e-9d1a-2f6b8e4c1a77"
    with pytest.raises(V.TenantDocumentError) as e:
        V.parse_tenant_json(f'{{"{uuid}": 1, "{uuid}": 2}}')
    assert uuid not in str(e.value) and "<ключ скрыт>" in str(e.value)
    with pytest.raises(V.TenantDocumentError) as e:
        V.parse_tenant_json('{"legal_name": "SECRET-VALUE-1", "legal_name": "SECRET-VALUE-2"}')
    assert "SECRET-VALUE" not in str(e.value)


def test_duplicate_keys_in_registry_file_fail_before_schema(registry_copy):
    raw = (registry_copy / "client_001" / "tenant.json").read_text(encoding="utf-8")
    raw = raw.replace('"gcp_project_id": "mpa-t-client-001",',
                      '"gcp_project_id": "project-fa311fc0-4d87-4781-986",\n'
                      '    "gcp_project_id": "mpa-t-client-001",', 1)
    (registry_copy / "client_001" / "tenant.json").write_text(raw, encoding="utf-8")
    found = R.validate_root(registry_copy)
    assert any(f.rule == "json" and "повторяющийся ключ" in f.message for f in found)


def test_no_tenant_document_is_parsed_outside_the_canonical_loader():
    """Разбор JSON в tools/tenancy — только внутри parse_tenant_json; вне tools/tenancy
    никакой код не читает tenant.json сам (правило авторитета реестра)."""
    for f in (REPO / "tools" / "tenancy").glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for call in ast.walk(fn):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr in ("load", "loads")
                        and isinstance(call.func.value, ast.Name) and call.func.value.id == "json"):
                    assert fn.name == "parse_tenant_json", f"{f.name}:{call.lineno} {fn.name}"
    offenders = []
    for py in REPO.rglob("*.py"):
        rel = py.relative_to(REPO).as_posix()
        if rel.startswith(("tools/tenancy/", "tools/tests/test_tenancy")) or "/node_modules/" in rel:
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "tenant.json" in text and ("json.load" in text or "open(" in text):
            offenders.append(rel)
    assert offenders == [], f"tenant.json читается в обход реестра: {offenders}"


# ═══════════════════════════════════════════════ L5 — полное совпадение строк
MALFORMED_SUFFIXES = ["\n", " ", "\t", " ", "​", "\x00", "+x", " "]
MALFORMED_PREFIXES = ["\n", " ", "﻿", "​"]


def _variants(value):
    for s in MALFORMED_SUFFIXES:
        yield value + s
    for p in MALFORMED_PREFIXES:
        yield p + value
    yield value[:3] + "\n" + value[3:]                      # перевод строки внутри


FIELD_CASES = [
    ("tenant_id", "client_001"),
    ("data_boundary.gcp_project_id", "mpa-t-client-001"),
    ("data_boundary.datasets.ref", "ref"),
    ("marketplaces.ozon.secret_refs.seller_api_key", "ozon-seller-api-key"),
    ("marketplaces.ozon.backfill_start_date", "2026-01-01"),
]


@pytest.mark.parametrize("path,good", FIELD_CASES)
def test_identity_fields_are_all_or_nothing(path, good):
    assert rules(put(CLIENT, path, good)) == set()
    for bad in _variants(good):
        assert rules(put(CLIENT, path, bad)), f"{path}: принято {bad!r}"


def test_bi_principal_is_exact_and_ascii():
    base = put(CLIENT, "bi_access", {"mode": "ANALYTICS_SHARE_VIEWER",
                                     "principals": ["user:owner@example.com"]})
    assert rules(base) == set()
    for bad in _variants("user:owner@example.com"):
        doc = put(base, "bi_access.principals", [bad])
        assert rules(doc), f"принято {bad!r}"
    for bad in ("user:own​er@example.com", "user:владелец@example.com",
                "service:owner@example.com", "user:@example.com"):
        assert rules(put(base, "bi_access.principals", [bad])), bad


@pytest.mark.parametrize("fn,good", [
    (N.check_tenant_id, "client_001"),
    (N.check_gcp_project_id, "mpa-t-client-001"),
    (N.parse_project_id, "mpa-t-client-001"),
])
def test_naming_functions_are_all_or_nothing(fn, good):
    fn(good)
    for bad in _variants(good):
        with pytest.raises(N.NamingError):
            fn(bad)


@pytest.mark.parametrize("bad", [True, False, 1, 10, 0, -2, 2.0, "2", "02"])
def test_malformed_project_revisions(bad):
    with pytest.raises(N.NamingError):
        N.check_project_revision(bad)


def test_valid_project_revisions():
    assert N.check_project_revision(None) == 1
    assert [N.check_project_revision(r) for r in range(2, 10)] == list(range(2, 10))


# ═════════════════════════════════════════════ L6 — пространство имён проектов
def test_frozen_grammar_golden_vectors():
    """Эталон замороженной грамматики. Изменение — только через ADR миграции."""
    assert N.DEDICATED_PROJECT_PREFIX == "mpa-t"
    assert N.derive_project_id("client_001") == "mpa-t-client-001"
    assert N.derive_project_id("client_001", 2) == "mpa-t2-client-001"
    assert N.derive_project_id("client") == "mpa-t-client"
    assert N.derive_project_id("client", 9) == "mpa-t9-client"
    assert N.derive_project_id("acme_shop_2") == "mpa-t-acme-shop-2"
    assert N.terraform_state_prefix("client_001") == "tenants/client_001"


def test_known_l6_collision_is_gone():
    ids = {N.derive_project_id("client_001")}
    for rev in range(2, 10):
        ids.add(N.derive_project_id("client_001", rev))
    for tid in ("client", "client_0", "client_00", "client_01", "client_1", "client001"):
        for rev in [None, *range(2, 10)]:
            pid = N.derive_project_id(tid, rev)
            assert pid not in ids, f"{tid}/{rev} совпал с client_001: {pid}"


def _valid_ids(alphabet, lengths):
    for n in lengths:
        for tail in itertools.product(alphabet, repeat=n - 1):
            for head in "ab":
                tid = head + "".join(tail)
                try:
                    N.check_tenant_id(tid)
                except N.NamingError:
                    continue
                yield tid


def test_generated_namespace_is_injective_and_reversible():
    """Исчерпывающий перебор: tenant_id длиной 3–6 из {a,b,0,1,_} × все ревизии."""
    seen = {}
    count = 0
    for tid in _valid_ids("ab01_", range(3, 7)):
        for rev in [None, *range(2, 10)]:
            pid = N.derive_project_id(tid, rev)
            key = (tid, rev or 1)
            assert pid not in seen, f"коллизия: {key} и {seen[pid]} → {pid}"
            seen[pid] = key
            assert N.parse_project_id(pid) == key
            count += 1
    assert count > 20_000


def test_named_collision_candidates():
    names = ["client", "client_001", "client_01", "client_1", "client001", "client_001_foo",
             "client_0_01", "client_00_1", "client_010", "client_10"]
    pids = [N.derive_project_id(t, r) for t in names for r in [None, *range(2, 10)]]
    assert len(pids) == len(set(pids))


def test_ids_near_the_length_boundary_never_collide_or_truncate():
    base = "t" + "x" * 21                                        # 22 символа
    near = [base + a + b for a in "abc_1" for b in "ab1"]         # 24 символа
    near = [t for t in near if not t.endswith("_") and "__" not in t]
    for t in near:
        assert len(N.derive_project_id(t)) == 30
        with pytest.raises(N.NamingError, match="длиннее 30"):
            N.derive_project_id(t, 2)                             # ревизии не хватает места
    assert len({N.derive_project_id(t) for t in near}) == len(near)
    with pytest.raises(N.NamingError, match="длиннее 30"):
        N.derive_project_id(base + "abc")                        # 25 символов: отказ, не обрезка


@pytest.mark.parametrize("tid", ["my_google", "googl_e_x", "classless", "annulled", "undefined_co",
                                 "bssl", "has_ssl"])
def test_gcp_restricted_substrings_are_rejected(tid):
    try:
        N.check_tenant_id(tid)
    except N.NamingError:
        return                                   # отвергнут ещё раньше — тоже верно
    with pytest.raises(N.NamingError, match="запрещённые в GCP"):
        N.derive_project_id(tid)


def test_derivation_depends_only_on_tenant_id_and_revision():
    a = put(CLIENT, "display_name", "Другое имя")
    a = put(a, "legal_name", "Другое юрлицо")
    assert rules(a) == set()                     # проект не зависит от имени


def test_revision_is_validated_against_the_project_id():
    ok = put(put(CLIENT, "data_boundary.project_id_revision", 2),
             "data_boundary.gcp_project_id", "mpa-t2-client-001")
    assert rules(ok) == set()
    assert "naming" in rules(put(CLIENT, "data_boundary.project_id_revision", 2))
    for bad in (1, 10, True, 2.0, "2"):
        assert "schema" in rules(put(CLIENT, "data_boundary.project_id_revision", bad)), bad


def test_evetis_identity_is_immutable_legacy_state():
    assert EVETIS["data_boundary"]["gcp_project_id"] == "project-fa311fc0-4d87-4781-986"
    with pytest.raises(N.NamingError):
        N.parse_project_id(EVETIS["data_boundary"]["gcp_project_id"])
    assert "evetis_protected" in rules(put(EVETIS, "data_boundary.project_id_revision", 2))
    assert rules(EVETIS) == set()


def test_registry_list_is_the_consumer_interface(capsys):
    assert R.main(["list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [t["tenant_id"] for t in listed] == ["client_001", "evetis"]
    assert listed[0]["gcp_project_id"] == "mpa-t-client-001"
    assert all("secret_refs" not in json.dumps(t) for t in listed)


# ═══════════════════════════════════════════════════════ I1 — schema_version
@pytest.mark.parametrize("value,ok", [(1, True), (True, False), (1.0, False), ("1", False),
                                      (2, False), (0, False), (None, False)])
def test_schema_version_is_exactly_integer_one(value, ok):
    assert (rules(put(CLIENT, "schema_version", value)) == set()) is ok


def test_shared_ae_validator_is_unchanged():
    """I1 закрыт в схеме арендатора (type integer), а не в общем валидаторе AE:
    семантика схем AE не меняется."""
    from tools.autonomy.schema import validate
    assert validate(True, {"const": 1}) == []            # поведение общего валидатора прежнее
    assert validate(True, {"type": "integer", "const": 1}) != []
