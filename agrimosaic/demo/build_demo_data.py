#!/usr/bin/env python3
"""生成演示数据：三个共原点批次，覆盖小尺度窗口与极粗格/文件缺页案例。

运行：python3 -I demo/build_demo_data.py
输出目录 demo/data/ 与 demo/configs/ 仅为本工具的输入夹具，与脚本分目录存放。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CONFIGS = os.path.join(HERE, "configs")
os.makedirs(DATA, exist_ok=True)
os.makedirs(CONFIGS, exist_ok=True)


def write_jsonl(name, rows):
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------- 夹具 A：8x8 窗口、16 个目标格（size=2），逐格可人工核对 ----------
# B1：size=1 最细最旧，只覆盖 y<4
b1 = [
    {"cell": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}, "reflectance": 0.10, "quality": 10},
    {"cell": {"x0": 1, "y0": 0, "x1": 2, "y1": 1}, "reflectance": 0.11, "quality": 11},
    {"cell": {"x0": 0, "y0": 1, "x1": 1, "y1": 2}, "reflectance": 0.12, "quality": 12},
    {"cell": {"x0": 1, "y0": 1, "x1": 2, "y1": 2}, "reflectance": 0.13, "quality": 13},
    {"cell": {"x0": 2, "y0": 0, "x1": 3, "y1": 1}, "state": "missing_channel"},
    {"cell": {"x0": 3, "y0": 0, "x1": 4, "y1": 1}, "state": "cloud"},
]
write_jsonl("b1_fine.jsonl", b1)

# B2：size=2，覆盖 8x8；(2,2,4,4) 与 (4,4,..) 多处缺页
b2 = [
    {"cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}, "reflectance": 0.20, "quality": 20},
    {"cell": {"x0": 2, "y0": 0, "x1": 4, "y1": 2}, "reflectance": 0.21, "quality": 21},
    {"cell": {"x0": 4, "y0": 0, "x1": 6, "y1": 2}, "reflectance": 0.22, "quality": 22},
    {"cell": {"x0": 6, "y0": 0, "x1": 8, "y1": 2}, "reflectance": 0.23, "quality": 23},
    {"cell": {"x0": 0, "y0": 2, "x1": 2, "y1": 4}, "state": "cloud"},
    {"cell": {"x0": 4, "y0": 2, "x1": 6, "y1": 4}, "reflectance": 0.24, "quality": 24},
    # (2,2)-(4,4) 声明覆盖内缺记录（文件缺页）
    # y>=4 全部缺页
]
write_jsonl("b2_mid.jsonl", b2)

# B3：size=4 最粗最新，覆盖 16x16；(4,4,8,8) 缺页
b3 = [
    {"cell": {"x0": 0, "y0": 0, "x1": 4, "y1": 4}, "reflectance": 0.30, "quality": 30},
    {"cell": {"x0": 4, "y0": 0, "x1": 8, "y1": 4}, "reflectance": 0.31, "quality": 31},
    {"cell": {"x0": 0, "y0": 4, "x1": 4, "y1": 8}, "reflectance": 0.32, "quality": 32},
]
write_jsonl("b3_coarse.jsonl", b3)

config_a = {
    "value_domain": {"reflectance_min": 0.0, "reflectance_max": 1.0,
                     "quality_min": 0, "quality_max": 100},
    "batches": [
        {"batch_id": "B1", "acquired_at": "2026-01-01T00:00:00+00:00",
         "cell_size": 1, "coverage": {"x0": 0, "y0": 0, "x1": 8, "y1": 4},
         "source_file": "../data/b1_fine.jsonl"},
        {"batch_id": "B2", "acquired_at": "2026-02-01T00:00:00+00:00",
         "cell_size": 2, "coverage": {"x0": 0, "y0": 0, "x1": 8, "y1": 8},
         "source_file": "../data/b2_mid.jsonl"},
        {"batch_id": "B3", "acquired_at": "2026-03-01T00:00:00+00:00",
         "cell_size": 4, "coverage": {"x0": 0, "y0": 0, "x1": 16, "y1": 16},
         "source_file": "../data/b3_coarse.jsonl"},
    ],
}
with open(os.path.join(CONFIGS, "config_a.json"), "w", encoding="utf-8") as fh:
    json.dump(config_a, fh, ensure_ascii=False, indent=2)

# ---------- 夹具 B：极粗格（2^20）+ 单个基本单元小格 + 空文件缺页 ----------
HUGE = 2 ** 20
# 粗格物理源格放在远离演示窗口的位置，覆盖声明也只到 HUGE 边界，
# 这样 (16,16) 一带不被粗/细批次声明，仅由空批次 EMPTY 声明。
write_jsonl("huge_coarse.jsonl", [
    {"cell": {"x0": 0, "y0": 0, "x1": HUGE, "y1": HUGE},
     "reflectance": 0.70, "quality": 70},
])
write_jsonl("one_fine.jsonl", [
    {"cell": {"x0": 1, "y0": 1, "x1": 2, "y1": 2},
     "reflectance": 0.20, "quality": 20},
])
# 空文件：批次声明覆盖但零记录，用于演示“声明内缺记录”与“无声明覆盖”的区别
open(os.path.join(DATA, "empty_batch.jsonl"), "w", encoding="utf-8").close()

config_b = {
    "value_domain": {"reflectance_min": 0.0, "reflectance_max": 1.0,
                     "quality_min": 0, "quality_max": 100},
    "batches": [
        # 粗/细批次只声明各自源格所在的近原点区域；(16,16) 一带仅 EMPTY 批次声明
        {"batch_id": "COARSE", "acquired_at": "2026-01-01T00:00:00+00:00",
         "cell_size": HUGE, "coverage": {"x0": 0, "y0": 0, "x1": HUGE, "y1": HUGE},
         "source_file": "../data/huge_coarse.jsonl"},
        {"batch_id": "FINE", "acquired_at": "2026-02-01T00:00:00+00:00",
         "cell_size": 1, "coverage": {"x0": 0, "y0": 0, "x1": 8, "y1": 8},
         "source_file": "../data/one_fine.jsonl"},
        {"batch_id": "EMPTY", "acquired_at": "2026-03-01T00:00:00+00:00",
         "cell_size": 2,
         "coverage": {"x0": HUGE, "y0": HUGE, "x1": HUGE + 8, "y1": HUGE + 8},
         "source_file": "../data/empty_batch.jsonl"},
    ],
}
with open(os.path.join(CONFIGS, "config_b.json"), "w", encoding="utf-8") as fh:
    json.dump(config_b, fh, ensure_ascii=False, indent=2)

print("demo data written:", os.listdir(DATA))
print("demo configs written:", os.listdir(CONFIGS))
