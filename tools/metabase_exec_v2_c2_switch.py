#!/usr/bin/env python3
"""
EVETIS · WB Executive V2 — Phase C2 · переключение dashboard 2 на материализованный слой.

Док:   docs/EXECUTIVE_V2_PHASE_C2_MATERIALIZED_LAYER_2026-09-17.md
Слой:  sql/dash/executive_v2_daily_v1.sql (wb_mart.V_DASH_EXECUTIVE_V2_DAILY)

Карточки Phase B не меняются. Для каждой карточки dashboard 2 создаётся копия
«<имя> · слой C2» в той же коллекции: SQL тот же, ссылки на канонические V_DASH_*
заменены на V_DASH_EXECUTIVE_V2_DAILY, поле фильтра периода — day слоя. Исключение —
«Свежесть данных» (159): «Витрина собрана» = последняя успешная сборка слоя.
Заголовки плиток сохраняются через card.title на dashcard, поэтому экран не меняется.

  --snapshot            снять раскладку dashboard 2 до переключения (Phase B) в metabase/rollback/
  --cards               создать/обновить копии C2 (идемпотентно, по имени)
  --test                временный dashboard: раскладка Phase B → переключение → откат → архив
  --switch [--dash N]   переключить dashboard (по умолчанию 2) на копии C2
  --rollback [--dash N] вернуть раскладку из снимка Phase B (канонические view не трогает)

Профиль mb: $MB_PROFILE или evetis-dev. Секретов в скрипте нет.
"""
import json, os, re, subprocess, sys, tempfile

PROFILE = os.environ.get("MB_PROFILE", "evetis-dev")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = os.path.join(ROOT, "metabase/rollback/exec_v2_phase_c2_before")
SNAP = os.path.join(SNAP_DIR, "dashboard-2-layout.json")
MAP = os.path.join(SNAP_DIR, "card-map.json")
DB, DASH = 2, 2
SUFFIX = " · слой C2"
LAYER = "wb_mart.V_DASH_EXECUTIVE_V2_DAILY"
LAYER_TABLE = "V_DASH_EXECUTIVE_V2_DAILY"
CANON = r"wb_mart\.V_DASH_(KPI_DAILY|FINANCE_CORRECTED_DAILY|EXECUTIVE_ECONOMICS_DAILY|SETTLEMENT_DAILY|EXECUTIVE_BREAKDOWN_DAILY|BUYOUT_COHORT_DAILY)\b"
FRESHNESS_CARD = 159

FRESHNESS_SQL = f"""-- Phase C2 · {LAYER}: свежесть (не зависит от периода)
-- «Данные по» — source_data_through (исходные данные), «Витрина собрана» — последняя успешная сборка слоя.
SELECT FORMAT_DATE('%d.%m.%Y', ANY_VALUE(source_data_through)) AS data_as_of,
       CONCAT(FORMAT_TIMESTAMP('%d.%m %H:%M МСК', ANY_VALUE(layer_built_at), 'Europe/Moscow'),
              IFNULL(CONCAT(' · ⚠ ', ANY_VALUE(layer_build_alert)), '')) AS mart_built
FROM `{LAYER}`"""
FRESHNESS_DESC = ("«Данные по» — дата, по которую загружены исходные данные. «Витрина собрана» — время "
                  "последней успешной сборки слоя Executive (пересборка каждый час 07:10–23:10 МСК). "
                  "От выбранного периода не зависит.")


def mb(*args, body=None):
    cmd = ["mb", *args, "-p", PROFILE, "--json", "--max-bytes=0"]
    tmp = None
    if body is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(body, tmp, ensure_ascii=False)
        tmp.close()
        cmd += ["--file", tmp.name]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if tmp:
        os.unlink(tmp.name)
    if out.returncode != 0:
        raise SystemExit(f"mb {' '.join(args)} failed: {out.stdout[:800]} {out.stderr[:800]}")
    return json.loads(out.stdout) if out.stdout.strip() else None


def card_full(cid):
    return mb("card", "get", str(cid), "--full")


def dash_full(did):
    return mb("dashboard", "get", str(did), "--full")


def layer_day_field():
    tables = mb("table", "list", f"--db-id={DB}")
    tables = tables.get("data", tables) if isinstance(tables, dict) else tables
    tid = next((t["id"] for t in tables if t["name"] == LAYER_TABLE), None)
    if tid is None:
        raise SystemExit(f"{LAYER_TABLE} ещё не синхронизирован в Metabase (Admin → Databases → Sync)")
    fields = mb("table", "fields", str(tid))
    fields = fields.get("data", fields) if isinstance(fields, dict) else fields
    return next(f["id"] for f in fields if f["name"] == "day")


def to_layer_sql(sql):
    new = re.sub(CANON, LAYER, sql)
    if re.search(r"wb_mart\.V_DASH_(?!EXECUTIVE_V2_DAILY)", new):
        raise SystemExit("в SQL осталась ссылка на канонический V_DASH_*:\n" + new)
    return f"-- Phase C2 · {LAYER} (материализованный слой; логика карточки не менялась)\n" + new


def layout_card_ids(layout):
    return sorted({dc["card_id"] for dc in layout["dashcards"] if dc.get("card_id")})


# ── снимок ─────────────────────────────────────────────────────────────────────
def snapshot():
    if os.path.exists(SNAP):
        raise SystemExit(f"снимок уже есть: {SNAP} — не перезаписываю")
    d = dash_full(DASH)
    keep = ("card_id", "row", "col", "size_x", "size_y", "dashboard_tab_id",
            "visualization_settings", "parameter_mappings", "series", "inline_parameters")
    snap = {"id": d["id"], "name": d["name"], "width": d.get("width"), "parameters": d["parameters"],
            "dashcards": [{k: dc.get(k) for k in keep} for dc in
                          sorted(d["dashcards"], key=lambda x: (x["row"], x["col"]))]}
    if any(dc["card_id"] in load_map().values() for dc in snap["dashcards"] if dc["card_id"]):
        raise SystemExit("dashboard 2 уже на копиях C2 — снимок Phase B снимать нельзя")
    os.makedirs(SNAP_DIR, exist_ok=True)
    json.dump(snap, open(SNAP, "w"), ensure_ascii=False, indent=1)
    print(f"снимок: {SNAP} ({len(snap['dashcards'])} dashcards, карточек {len(layout_card_ids(snap))})")


def load_snap():
    return json.load(open(SNAP))


def load_map():
    return {int(k): v for k, v in json.load(open(MAP)).items()} if os.path.exists(MAP) else {}


# ── копии карточек ─────────────────────────────────────────────────────────────
def build_cards():
    snap = load_snap()
    field = layer_day_field()
    cmap = load_map()
    for cid in layout_card_ids(snap):
        src = card_full(cid)
        stage = json.loads(json.dumps(src["dataset_query"]["stages"][0]))
        if cid == FRESHNESS_CARD:
            stage["native"] = FRESHNESS_SQL
            description = FRESHNESS_DESC
        else:
            stage["native"] = to_layer_sql(stage["native"])
            description = src.get("description")
        tags = stage.get("template-tags") or []
        tags = tags if isinstance(tags, list) else list(tags.values())
        tag_obj = {}
        for t in tags:
            t = {k: v for k, v in t.items()}
            if t.get("type") == "dimension":
                t["dimension"] = ["field", {"lib/uuid": t["dimension"][1]["lib/uuid"]}, field]
            tag_obj[t["name"]] = t
        dq = {"database": DB, "lib/type": "mbql/query",
              "stages": [{"lib/type": "mbql.stage/native", "native": stage["native"], "template-tags": tag_obj}]}
        body = {
            "name": src["name"] + SUFFIX,
            "type": "question",
            "display": src["display"],
            "description": description,
            "collection_id": src["collection_id"],
            "visualization_settings": src.get("visualization_settings") or {},
            "dataset_query": dq,
        }
        if cid in cmap:
            mb("card", "update", str(cmap[cid]), body=body)
            print(f"updated {cid} → {cmap[cid]} {body['name']}")
        else:
            new = mb("card", "create", body=body)
            cmap[cid] = new["id"]
            print(f"created {cid} → {new['id']} {body['name']}")
        json.dump({str(k): v for k, v in sorted(cmap.items())}, open(MAP, "w"), ensure_ascii=False, indent=1)
    # контроль: копия читает только слой и фильтрует период по day слоя
    for cid, nid in sorted(cmap.items()):
        st = card_full(nid)["dataset_query"]["stages"][0]
        assert not re.search(r"wb_mart\.V_DASH_(?!EXECUTIVE_V2_DAILY)", st["native"]), (cid, nid)
        tags = st.get("template-tags") or []
        tags = tags if isinstance(tags, list) else list(tags.values())
        assert all(t["dimension"][2] == field for t in tags if t.get("type") == "dimension"), (cid, nid)
    print(f"копий: {len(cmap)}")


# ── раскладка ──────────────────────────────────────────────────────────────────
def layout_body(snap, cmap=None):
    dashcards = []
    for i, dc in enumerate(snap["dashcards"], 1):
        dc = json.loads(json.dumps(dc))
        dc["id"] = -i
        cid = dc.get("card_id")
        if cmap and cid:
            src_name = card_full(cid)["name"]
            vs = dc.get("visualization_settings") or {}
            vs.setdefault("card.title", src_name)  # экран сохраняет прежний заголовок
            dc["visualization_settings"] = vs
            dc["card_id"] = cmap[cid]
            for pm in dc.get("parameter_mappings") or []:
                pm["card_id"] = cmap[cid]
        dashcards.append(dc)
    return {"width": snap["width"], "parameters": snap["parameters"], "dashcards": dashcards}


def apply_layout(did, body):
    mb("dashboard", "update", str(did), body=body)
    d = dash_full(did)
    got = sorted((dc["row"], dc["col"], dc["size_x"], dc["size_y"], dc.get("card_id")) for dc in d["dashcards"])
    want = sorted((dc["row"], dc["col"], dc["size_x"], dc["size_y"], dc.get("card_id")) for dc in body["dashcards"])
    assert got == want, "раскладка не совпала с заданной"
    assert d["parameters"] == body["parameters"], "параметры dashboard изменились"
    return d


def switch(did):
    snap, cmap = load_snap(), load_map()
    missing = [c for c in layout_card_ids(snap) if c not in cmap]
    if missing:
        raise SystemExit(f"нет копий C2 для карточек {missing} — сначала --cards")
    apply_layout(did, layout_body(snap, cmap))
    print(f"dashboard {did}: {len(snap['dashcards'])} dashcards → карточки слоя C2")


def rollback(did):
    snap = load_snap()
    apply_layout(did, layout_body(snap))
    d = dash_full(did)
    assert layout_card_ids(d) == layout_card_ids(snap), "после отката карточки не совпали со снимком"
    print(f"dashboard {did}: возвращён на карточки Phase B ({len(snap['dashcards'])} dashcards)")


def test():
    snap = load_snap()
    d = mb("dashboard", "create", body={"name": "Executive V2 · проверка C2 (временный)",
                                        "collection_id": card_full(layout_card_ids(snap)[0])["collection_id"],
                                        "parameters": snap["parameters"]})
    tid = d["id"]
    print(f"временный dashboard {tid}")
    apply_layout(tid, layout_body(snap))
    print("  раскладка Phase B ✓")
    switch(tid)
    print("  переключение ✓")
    rollback(tid)
    print("  откат ✓")
    switch(tid)
    print(f"  повторно переключён для визуальной проверки: http://localhost:3000/dashboard/{tid}")
    print(f"  после проверки: mb dashboard archive {tid}")


if __name__ == "__main__":
    a = sys.argv[1:]
    did = int(a[a.index("--dash") + 1]) if "--dash" in a else DASH
    if "--snapshot" in a:
        snapshot()
    elif "--cards" in a:
        build_cards()
    elif "--test" in a:
        test()
    elif "--switch" in a:
        switch(did)
    elif "--rollback" in a:
        rollback(did)
    else:
        print(__doc__)
