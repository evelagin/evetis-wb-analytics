#!/usr/bin/env python3
"""Очередь КИЗ кремов Вишня …079 и Амбра …062 (папка Очередь_КИЗ).

Регламент владельца: коды выдаются строго по порядку страниц мастер-PDF, начиная с указателя,
только со статусом «В обороте» (INTRODUCED); «Нанесён»/выбывшие пропускаются. Сканы ФФ не используются.

Команды:
  status                      — указатели, остатки, сколько годных впереди
  issue --cherry N --amber M --purpose "…"   — выдать коды: проверка в ГИС МТ, PDF для ФФ, запись в индекс, сдвиг указателя
  set-doc --sets FILE.json --issue ISSUE.json — собрать документ формирования наборов из кодов наборов и выданных кодов

Индекс: ИНДЕКС_<крем>.csv (страница;код;статус;эмиссия;использовано;скан ФФ;выдано ФФ). Указатель: _ТЕКУЩАЯ_СТРАНИЦА.txt.
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
import chz  # noqa: E402

DIR = os.path.expanduser("~/Documents/02_Эветис_косметика_маркетплейсы/02_Маркировка_КИЗ_ЧЗ/Очередь_КИЗ")
POINTER = os.path.join(DIR, "_ТЕКУЩАЯ_СТРАНИЦА.txt")
QUEUES = {
    "cherry": {"name": "Вишня", "gtin": "04619689656079", "pdf": "Крем_Вишня_КИЗ_до 643 включительно.pdf", "index": "ИНДЕКС_Вишня_079.csv"},
    "amber": {"name": "Амбра", "gtin": "04619689656062", "pdf": "Крем_Ванила_КИЗ_до 400 включительно.pdf", "index": "ИНДЕКС_Амбра_062.csv"},
}
FIELDS = ["страница", "код", "статус ГИС МТ", "эмиссия", "использовано", "скан ФФ", "выдано ФФ"]


# ---------------------------------------------------------------- индекс и указатель

def load_index(key: str) -> list[dict]:
    p = os.path.join(DIR, QUEUES[key]["index"])
    if not os.path.exists(p):
        # первичный индекс от 30.09 назывался с датой — подхватываем его
        cands = sorted(f for f in os.listdir(DIR) if f.startswith(QUEUES[key]["index"][:-4]))
        if not cands:
            sys.exit(f"нет индекса {p}")
        p = os.path.join(DIR, cands[-1])
    with open(p, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    for r in rows:
        r["страница"] = int(r["страница"])
        r.setdefault("выдано ФФ", "")
    return rows


def save_index(key: str, rows: list[dict]) -> None:
    p = os.path.join(DIR, QUEUES[key]["index"])
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_pointer() -> dict:
    ptr = {}
    if os.path.exists(POINTER):
        for line in open(POINTER, encoding="utf-8"):
            m = re.search(r"(Амбра|Вишня).*следующая свободная\s*=\s*(\d+)", line)
            if m:
                ptr["amber" if m.group(1) == "Амбра" else "cherry"] = int(m.group(2))
    return ptr


def save_pointer(ptr: dict) -> None:
    with open(POINTER, "w", encoding="utf-8") as f:
        f.write(f"Обновлено {dt.datetime.now():%Y-%m-%d %H:%M}\n")
        for key in ("amber", "cherry"):
            f.write(f"{QUEUES[key]['name']} …{QUEUES[key]['gtin'][-3:]} ({QUEUES[key]['pdf'].split('_КИЗ')[0]}): следующая свободная = {ptr[key]}\n")
        f.write("Перед выдачей кодов проверять статус: только «В обороте» (INTRODUCED). Сканы ФФ не использовать.\n")


# ---------------------------------------------------------------- PDF страниц для ФФ

def extract_pages(key: str, pages: list[int], out_path: str) -> None:
    """Собирает новый PDF из указанных страниц мастер-файла (PDFKit через JXA)."""
    src = os.path.join(DIR, QUEUES[key]["pdf"])
    script = r'''
ObjC.import('PDFKit');
function run(argv){
  var src=$.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
  var out=$.PDFDocument.alloc.init; var pages=JSON.parse(argv[2]);
  for (var i=0;i<pages.length;i++){ out.insertPageAtIndex(src.pageAtIndex(pages[i]-1), i); }
  out.writeToFile(argv[1]); return String(out.pageCount);
}'''
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", script, src, out_path, json.dumps(pages)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out_path):
        sys.exit(f"не удалось собрать PDF: {r.stderr[-500:]}")


# ---------------------------------------------------------------- команды

def cmd_status(args) -> None:
    ptr = load_pointer()
    for key in ("amber", "cherry"):
        rows = load_index(key)
        p = ptr.get(key)
        ahead = [r for r in rows if r["страница"] >= p]
        ok = [r for r in ahead if r["статус ГИС МТ"] == "INTRODUCED" and not r["выдано ФФ"]]
        print(f"{QUEUES[key]['name']} …{QUEUES[key]['gtin'][-3:]}: указатель {p}, впереди {len(ahead)} стр., годных «В обороте» {len(ok)}, "
              f"выдано ФФ всего {sum(1 for r in rows if r['выдано ФФ'])}, выбыло {sum(1 for r in rows if r['статус ГИС МТ']=='RETIRED')}")


def cmd_issue(args) -> None:
    env = chz.load_env()
    c = chz.contour(env, "prod")
    ptr = load_pointer()
    stamp = dt.datetime.now().strftime("%Y-%m-%d")
    tag = re.sub(r"[^\w\-]+", "_", args.purpose)[:40]
    result = {"date": stamp, "purpose": args.purpose, "cherry": [], "amber": []}
    for key, n in (("cherry", args.cherry), ("amber", args.amber)):
        if not n:
            continue
        rows = load_index(key)
        start = ptr[key]
        cand = [r for r in rows if r["страница"] >= start and not r["выдано ФФ"] and r["статус ГИС МТ"] == "INTRODUCED"]
        take = cand[: n * 2]  # с запасом: перепроверим статусы в ГИС МТ прямо сейчас
        info = chz.cises_info(c, [r["код"] for r in take])
        chosen = []
        for r in take:
            live = info.get(r["код"], {}).get("status")
            r["статус ГИС МТ"] = live or r["статус ГИС МТ"]
            if live == "INTRODUCED" and len(chosen) < n:
                chosen.append(r)
        if len(chosen) < n:
            sys.exit(f"{QUEUES[key]['name']}: годных кодов не хватает ({len(chosen)} из {n}), расширьте запас")
        pages = [r["страница"] for r in chosen]
        skipped = [r["страница"] for r in rows if start <= r["страница"] < pages[-1] and r["страница"] not in pages]
        print(f"{QUEUES[key]['name']}: выдаю {n} шт., страницы {pages[0]}–{pages[-1]}, пропущено {len(skipped)} {skipped[:10]}")
        if args.dry_run:
            result[key] = [{"page": r["страница"], "code": r["код"]} for r in chosen]
            continue
        for r in chosen:
            r["выдано ФФ"] = f"{stamp} {args.purpose}"
        save_index(key, rows)
        ptr[key] = pages[-1] + 1
        pdf = os.path.join(DIR, f"ФФ_{QUEUES[key]['name']}_{n}шт_{tag}_{stamp}.pdf")
        extract_pages(key, pages, pdf)
        csvp = pdf[:-4] + ".csv"
        with open(csvp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["страница", "код"])
            w.writerows([r["страница"], r["код"]] for r in chosen)
        result[key] = [{"page": r["страница"], "code": r["код"]} for r in chosen]
        print(f"   PDF для ФФ: {pdf}\n   список: {csvp}")
    if not args.dry_run:
        save_pointer(ptr)
        issue_path = os.path.join(DIR, f"ВЫДАЧА_{tag}_{stamp}.json")
        json.dump(result, open(issue_path, "w"), ensure_ascii=False, indent=1)
        print(f"указатели: амбра → {ptr['amber']}, вишня → {ptr['cherry']}; запись выдачи: {issue_path}")
        chz.journal("queue_issue", purpose=args.purpose, cherry=len(result["cherry"]), amber=len(result["amber"]),
                    pointer=ptr)
    else:
        print("DRY-RUN: индекс, указатель и PDF не изменены")


def cmd_set_doc(args) -> None:
    """sets: JSON {"gtin": "0461…130", "codes": [коды наборов…]}; issue: ВЫДАЧА_*.json из issue."""
    sets = json.load(open(args.sets, encoding="utf-8"))
    issue = json.load(open(args.issue, encoding="utf-8"))
    comp = {"04619689656123": ["cherry", "amber"], "04619689656130": ["cherry"], "04619689656147": ["amber"]}
    need = comp.get(sets["gtin"])
    if not need:
        sys.exit(f"неизвестный состав для набора {sets['gtin']}")
    pools = {k: list(issue.get(k, [])) for k in need}
    units = []
    for s in sets["codes"]:
        sntins = [sets["extra"]] if sets.get("extra") else []
        for k in need:
            if not pools[k]:
                sys.exit(f"не хватает выданных кодов {QUEUES[k]['name']} для набора {s}")
            sntins.append(pools[k].pop(0)["code"])
        units.append({"unitSerialNumber": s, "sntins": sntins})
    doc = {"participantId": "682962587600", "aggregationUnits": units}
    json.dump(doc, open(args.out, "w"), ensure_ascii=False, indent=1)
    print(f"документ: {args.out} — наборов {len(units)}; далее: chz.py --env prod set --file {args.out} --dry-run --confirm")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    i = sub.add_parser("issue")
    i.add_argument("--cherry", type=int, default=0)
    i.add_argument("--amber", type=int, default=0)
    i.add_argument("--purpose", required=True, help="назначение: поставка/набор, попадёт в индекс и имя файла")
    i.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("set-doc")
    s.add_argument("--sets", required=True)
    s.add_argument("--issue", required=True)
    s.add_argument("--out", required=True)
    args = ap.parse_args()
    {"status": cmd_status, "issue": cmd_issue, "set-doc": cmd_set_doc}[args.cmd](args)


if __name__ == "__main__":
    main()
