#!/usr/bin/env python3
"""季末复测资格台账的可复跑证据生成脚本。

构造三块地、三轮预定巡测（中间一轮缺包），覆盖：
  1. 低值位置在两轮之间移位（SHIFT）；
  2. 缺轮打断拼接、只进入可能达标上界（SHIFT，同一块地）；
  3. 极大地块上仅有零星细格修正，面积总数精确且与物理面积无关（HUGE）；
  4. 门槛精确比较：同一确认面积在相邻 1bp 门槛下分别复测 / 不列入（EXACT）。

运行：python3 -I demo/build_season_evidence.py
输出：demo/output/season_manifest.json 与 demo/output/season_ledger.json
脚本只在 demo 目录内写夹具与证据，不修改任何已有镶嵌包。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORK = os.path.join(HERE, "output", "season_demo")
os.makedirs(WORK, exist_ok=True)
MAIN = os.path.join(REPO, "agrimosaic", "__main__.py")
DOMAIN = {"reflectance_min": 0.0, "reflectance_max": 1.0,
          "quality_min": 0, "quality_max": 100}
DATES = ["2026-05-01", "2026-05-10", "2026-05-20"]


def cell(x, y, size, refl, qual=50):
    return {"cell": {"x0": x, "y0": y, "x1": x + size, "y1": y + size},
            "reflectance": refl, "quality": qual}


def build_pkg(name, batches, coverage):
    """batches: [(batch_id, cell_size, when, rows)]。"""
    cfg_batches = []
    for bid, size, when, rows in batches:
        jsonl = os.path.join(WORK, f"{name}_{bid}.jsonl")
        with open(jsonl, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        cfg_batches.append({
            "batch_id": bid, "acquired_at": when + "T00:00:00+00:00",
            "cell_size": size,
            "coverage": {"x0": coverage[0], "y0": coverage[1],
                         "x1": coverage[2], "y1": coverage[3]},
            "source_file": os.path.basename(jsonl),
        })
    cfg = {"value_domain": DOMAIN, "batches": cfg_batches}
    cfg_path = os.path.join(WORK, name + "_config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False)
    out = os.path.join(WORK, name + ".mosaic")
    if os.path.exists(out):
        for f in os.listdir(out):
            os.remove(os.path.join(out, f))
        os.rmdir(out)
    subprocess.run([sys.executable, "-I", MAIN, "build",
                    "--config", cfg_path, "--out", out], check=True)
    return out


def grid4(low_cells, size=2):
    return [cell(x, y, size, 0.1 if (x, y) in low_cells else 0.9)
            for y in range(0, 4, size) for x in range(0, 4, size)]


# ---- 小地块三轮：低值格 (0,0) 移位到 (2,2)，中间轮缺包 ----
pkg_r1 = build_pkg("round1", [("B", 2, "2026-01-01", grid4({(0, 0)}))],
                   (0, 0, 4, 4))
pkg_r3 = build_pkg("round3", [("B", 2, "2026-02-01", grid4({(2, 2)}))],
                   (0, 0, 4, 4))
# 无缺轮、低值位置第二轮起稳定在 (2,2)：用于演示移位后在末轮首次确认
pkg_r2_settled = build_pkg(
    "round2_settled", [("B", 2, "2026-01-15", grid4({(2, 2)}))], (0, 0, 4, 4))

# ---- 极大地块：2^20 粗格全覆盖低值，每轮一个互不相交的高反射细格修正 ----
HUGE = 2 ** 20
pkg_h1 = build_pkg("huge1", [
    ("COARSE", HUGE, "2026-01-01", [cell(0, 0, HUGE, 0.1)]),
    ("FINE", 1, "2026-01-02", [cell(1, 1, 1, 0.9, 90)]),
], (0, 0, HUGE, HUGE))
pkg_h2 = build_pkg("huge2", [
    ("COARSE", HUGE, "2026-02-01", [cell(0, 0, HUGE, 0.1)]),
    ("FINE", 1, "2026-02-02", [cell(3, 3, 1, 0.9, 90)]),
], (0, 0, HUGE, HUGE))

manifest = {
    "criteria": {
        "reflectance_cap": 0.3, "min_quality": 10,
        "min_consecutive_rounds": 2, "min_span_days": 0,
        "area_threshold_bp": 2500,
    },
}
# 清单契约要求每轮一个包；HUGE 与小地块轮次结构不同，按地块拆成清单演示。
small_manifest = {
    "plots": [
        {"plot_id": "SHIFT", "bounds": {"x0": 0, "y0": 0, "x1": 4, "y1": 4}},
        {"plot_id": "EXACT", "bounds": {"x0": 0, "y0": 0, "x1": 4, "y1": 4}},
    ],
    "rounds": [
        {"date": DATES[0], "package": pkg_r1},
        {"date": DATES[1], "package": None},
        {"date": DATES[2], "package": pkg_r3},
    ],
    "criteria": manifest["criteria"],
}
# EXACT 需要同一位置两轮低值才能确认：单独给一份无缺轮的清单
exact_manifest = {
    "plots": [
        {"plot_id": "EXACT", "bounds": {"x0": 0, "y0": 0, "x1": 4, "y1": 4}},
    ],
    "rounds": [
        {"date": DATES[0], "package": pkg_r1},
        {"date": DATES[2], "package": pkg_r1},
    ],
    "criteria": {**manifest["criteria"], "area_threshold_bp": 2500},
}
huge_manifest = {
    "plots": [
        {"plot_id": "HUGE", "bounds": {"x0": 0, "y0": 0, "x1": HUGE, "y1": HUGE}},
    ],
    "rounds": [
        {"date": DATES[0], "package": pkg_h1},
        {"date": DATES[2], "package": pkg_h2},
    ],
    "criteria": {**manifest["criteria"], "area_threshold_bp": 1},
}

results = {}
for tag, man in (("shift", small_manifest),
                 ("shift_settled", {
                     "plots": [{"plot_id": "SHIFT",
                                "bounds": {"x0": 0, "y0": 0, "x1": 4, "y1": 4}}],
                     "rounds": [
                         {"date": DATES[0], "package": pkg_r1},
                         {"date": DATES[1], "package": pkg_r2_settled},
                         {"date": DATES[2], "package": pkg_r3},
                     ],
                     "criteria": manifest["criteria"],
                 }),
                 ("exact", exact_manifest),
                 ("huge", huge_manifest)):
    mp = os.path.join(WORK, f"manifest_{tag}.json")
    with open(mp, "w", encoding="utf-8") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=2)
    r = subprocess.run([sys.executable, "-I", MAIN, "season",
                        "--manifest", mp], capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit(r.returncode)
    results[tag] = json.loads(r.stdout)

# EXACT 再跑一次门槛 +1bp 的对照，证明门槛处不换档
exact_hi = json.loads(json.dumps(exact_manifest))
exact_hi["criteria"]["area_threshold_bp"] = 2501
mp = os.path.join(WORK, "manifest_exact_hi.json")
with open(mp, "w", encoding="utf-8") as fh:
    json.dump(exact_hi, fh, ensure_ascii=False, indent=2)
r = subprocess.run([sys.executable, "-I", MAIN, "season", "--manifest", mp],
                   capture_output=True, text=True)
results["exact_threshold_plus_1bp"] = json.loads(r.stdout)

out_path = os.path.join(HERE, "output", "season_ledger.json")
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(results, fh, ensure_ascii=False, indent=2)

for tag, ledger in results.items():
    print(f"== {tag} ==")
    for p in ledger["plots"]:
        print(f"  {p['plot_id']}: 总面积 {p['total_area']}  "
              f"已确认 {p['confirmed_area']}  上界 {p['possible_area_upper_bound']}  "
              f"首次确认 {p['first_confirmed_area_by_date']}  -> {p['qualification']}")
print("台账已写出：", out_path)
