#!/usr/bin/env python3
"""批次质量审计的可复跑证据生成脚本。

构造一个可独立手算的小包，覆盖“从未在窗口裁决中胜出”的两类隐藏异常：

  * COARSE (size 4, 最旧) 的 (0,0,4,4) 数值异常，但该范围被更细的 FINE 全面压过；
  * MID_OLD (size 2) 的 (4,0,6,2) 数值异常，但同分辨率下被更新的 MID_NEW 压过；
  * EMPTY 批次声明覆盖但零记录，审计给出零统计。

运行：python3 -I demo/build_audit_evidence.py
输出：demo/output/audit_demo/ 下的包、审计配置与报告；不修改任何既有镶嵌包。
退出状态：0 = 全部批次通过；1 = 有批次未通过（报告仍完整写出）。
"""
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORK = os.path.join(HERE, "output", "audit_demo")
MAIN = os.path.join(REPO, "agrimosaic", "__main__.py")
DOMAIN = {"reflectance_min": 0.0, "reflectance_max": 1.0,
          "quality_min": 0, "quality_max": 100}


def cell(x, y, size, refl=0.1, qual=10, state=None):
    row = {"cell": {"x0": x, "y0": y, "x1": x + size, "y1": y + size}}
    if state:
        row["state"] = state
    else:
        row["reflectance"] = refl
        row["quality"] = qual
    return row


def write_jsonl(name, rows):
    path = os.path.join(WORK, name)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def build_pkg(name, batches, coverage):
    cfg_batches = []
    for bid, size, when, rows in batches:
        jsonl = write_jsonl(f"{name}_{bid}.jsonl", rows)
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


def digest(pkg):
    h = hashlib.sha256()
    for name in sorted(os.listdir(pkg)):
        with open(os.path.join(pkg, name), "rb") as fh:
            h.update(name.encode())
            h.update(fh.read())
    return h.hexdigest()


os.makedirs(WORK, exist_ok=True)

fine = [cell(x, y, 1, 0.1, 10) for y in range(4) for x in range(4)]
coarse = [cell(0, 0, 4, 0.9, 80), cell(4, 0, 4, 0.2, 20)]
mid_old = [cell(4, 0, 2, 0.85, 85), cell(6, 0, 2, 0.2, 20),
           cell(4, 2, 2, 0.2, 20), cell(6, 2, 2, 0.2, 20)]
mid_new = [cell(x, y, 2, 0.2, 20) for y in (0, 2) for x in (4, 6)]

pkg = build_pkg("pkg_audit", [
    ("FINE", 1, "2026-03-01", fine),
    ("COARSE", 4, "2026-01-01", coarse),
    ("MID_OLD", 2, "2026-01-05", mid_old),
    ("MID_NEW", 2, "2026-02-01", mid_new),
    ("EMPTY", 2, "2026-01-01", []),
], (0, 0, 12, 12))

config = {
    "packages": [os.path.relpath(pkg, WORK)],
    "batches": [
        {"batch_id": "COARSE", "rules": [
            {"channel": "reflectance", "min": 0.0, "max": 0.5,
             "anomaly_threshold": 1}]},
        {"batch_id": "MID_OLD", "rules": [
            {"channel": "quality", "min": 0, "max": 50,
             "anomaly_threshold": 1}]},
        {"batch_id": "FINE", "rules": [
            {"channel": "reflectance", "min": 0.0, "max": 0.5,
             "anomaly_threshold": 1}]},
        {"batch_id": "MID_NEW", "rules": [
            {"channel": "reflectance", "min": 0.0, "max": 0.5,
             "anomaly_threshold": 1}]},
        {"batch_id": "EMPTY", "rules": [
            {"channel": "reflectance", "min": 0.0, "max": 0.5,
             "anomaly_threshold": 1}]},
    ],
}
cfg_path = os.path.join(WORK, "audit_config.json")
with open(cfg_path, "w", encoding="utf-8") as fh:
    json.dump(config, fh, ensure_ascii=False, indent=2)

before = digest(pkg)
r = subprocess.run([sys.executable, "-I", MAIN, "audit", "--config", cfg_path],
                   capture_output=True, text=True)
after = digest(pkg)
if r.returncode == 2:
    sys.stderr.write(r.stderr)
    raise SystemExit(2)
report = json.loads(r.stdout)
report_path = os.path.join(HERE, "output", "audit_report.json")
with open(report_path, "w", encoding="utf-8") as fh:
    json.dump(report, fh, ensure_ascii=False, indent=2)

print("审计退出码：", r.returncode, "（1 = 有批次未通过，报告仍完整）")
print("只读校验（包内容哈希不变）：", before == after)
for entry in report["audit"]:
    for rule in entry["rules"]:
        print(f"  {entry['batch_id']:8s} {rule['channel']:11s} "
              f"有效 {rule['effective_record_count']:2d} "
              f"异常 {rule['anomaly_record_count']:2d} "
              f"面积 {rule['anomaly_area']:2d} "
              f"范围 {rule['anomaly_bounds']} -> {rule['status']}")
print("报告已写出：", report_path)
