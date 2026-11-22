#!/usr/bin/env python3
"""生成缺测归因证据：同一窗口内并列出现四类原因，验证优先级与面积对平。

场景（仅一个批次 B，size=1，覆盖 (0,0,4,4)，目标格 size=4 即整个窗口）：
  (0,0,1,1) 云遮记录            -> unavailable_cloud
  (1,0,2,1) 缺通道记录          -> unavailable_missing_channel
  (2,0,3,1) 与 (3,0,4,1) 无记录 -> declared_no_record（声明覆盖内缺页）
窗口外另取一格 (8,8,12,12)      -> no_declared_coverage
运行：python3 -I demo/build_gap_evidence.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)

rows = [
    {"cell": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}, "state": "cloud"},
    {"cell": {"x0": 1, "y0": 0, "x1": 2, "y1": 1}, "state": "missing_channel"},
    {"cell": {"x0": 0, "y0": 1, "x1": 1, "y1": 2},
     "reflectance": 0.55, "quality": 55},  # 一个有效格，制造部分覆盖
]
with open(os.path.join(DATA, "gap_demo.jsonl"), "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")

cfg = {
    "value_domain": {"reflectance_min": 0.0, "reflectance_max": 1.0,
                     "quality_min": 0, "quality_max": 100},
    "batches": [{
        "batch_id": "B", "acquired_at": "2026-06-01T00:00:00+00:00",
        "cell_size": 1, "coverage": {"x0": 0, "y0": 0, "x1": 4, "y1": 4},
        "source_file": "../data/gap_demo.jsonl",
    }],
}
cfg_path = os.path.join(HERE, "configs", "config_gap.json")
with open(cfg_path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)

main_py = os.path.join(REPO, "agrimosaic", "__main__.py")
pkg = os.path.join(HERE, "output", "pkg_gap")
subprocess.run([sys.executable, "-I", main_py, "build",
                "--config", cfg_path, "--out", pkg], check=True)

out1 = os.path.join(HERE, "output", "window_gap_classified.json")
subprocess.run([sys.executable, "-I", main_py, "extract", "--package", pkg,
                "--x0", "0", "--y0", "0", "--x1", "4", "--y1", "4",
                "--target-size", "4", "--output", out1], check=True)

out2 = os.path.join(HERE, "output", "window_gap_nodecl.json")
subprocess.run([sys.executable, "-I", main_py, "extract", "--package", pkg,
                "--x0", "8", "--y0", "8", "--x1", "12", "--y1", "12",
                "--target-size", "4", "--output", out2], check=True)

cell = json.load(open(out1, encoding="utf-8"))["cells"][0]
print("部分覆盖目标格状态：", cell["status"])
print("赢家面积：", cell["covered_area"], " 缺口面积：", cell["gap_area"],
      " 对平：", cell["covered_area"] + cell["gap_area"] == cell["area"])
for g in cell["gaps"]:
    print(" 缺口", g["reason"], "面积", g["area"], "区域", g["regions"])
nodecl = json.load(open(out2, encoding="utf-8"))["cells"][0]
print("无声明窗口：", nodecl["gaps"][0]["reason"])
