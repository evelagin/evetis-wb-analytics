"""Единая схема включения контуров потоков миграции M2–M5 (F4).

Правило: у каждого потока ровно два флага Terraform — mN_shadow_enabled и mN_prod_enabled,
bool, default false; каждый подключён в infra.yml к GitHub Variable MN_{SHADOW,PROD}_ENABLED
через сравнение с 'true' (не задана → false). Других флагов включения контуров
(finance_prod_enabled, stats_shadow_on и т.п.) нет — иначе снова три способа включения на четыре потока.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TF = REPO / "infra/terraform"
INFRA = REPO / ".github/workflows/infra.yml"
STREAMS = (2, 3, 4, 5)
FLAGS = [f"m{n}_{k}_enabled" for n in STREAMS for k in ("shadow", "prod")]
VAR_RE = re.compile(r'variable\s+"([a-z0-9_]+)"\s*\{(.*?)\n\}', re.S)


def _tf_variables():
    out = {}
    for p in sorted(TF.glob("*.tf")):
        for name, body in VAR_RE.findall(p.read_text(encoding="utf-8")):
            out[name] = (p.name, body)
    return out


def test_every_flag_is_wired_from_github_variable_with_false_default():
    text = INFRA.read_text(encoding="utf-8")
    for f in FLAGS:
        gh = f.upper()
        line = f"  TF_VAR_{f}: ${{{{ vars.{gh} == 'true' }}}}"
        assert text.count(line) == 1, f"infra.yml: нет ровно одной строки {line.strip()}"


def test_declared_flags_are_bool_default_false():
    for name, (file, body) in _tf_variables().items():
        if name in FLAGS:
            assert re.search(r"type\s*=\s*bool", body), f"{file}: {name} — нужен type = bool"
            assert re.search(r"default\s*=\s*false", body), f"{file}: {name} — нужен default = false"


def test_no_nonconforming_contour_flags():
    # Флаги включения контуров потоков — только по схеме mN_{shadow,prod}_enabled.
    bad = []
    for name, (file, _) in _tf_variables().items():
        if re.search(r"(shadow|prod)_(enabled|on)$", name) and name not in FLAGS:
            bad.append(f"{file}: {name}")
    assert bad == [], "флаг контура не по схеме mN_{shadow,prod}_enabled: " + ", ".join(bad)


def test_no_flag_wired_elsewhere_with_another_expression():
    text = INFRA.read_text(encoding="utf-8")
    for f in FLAGS:
        assert len(re.findall(rf"TF_VAR_{f}\b", text)) == 1, f
    for wf in (REPO / ".github/workflows").glob("*.yml"):
        if wf.name != "infra.yml":
            assert not re.search(r"TF_VAR_m[2-5]_(shadow|prod)_enabled", wf.read_text(encoding="utf-8")), wf.name
