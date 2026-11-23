#!/usr/bin/env python3
"""离线增量交付（diff/apply）的可复跑证据生成脚本。

场景：县站保存了旧包；中心补交观测（新增批次 E）、撤掉作废航扫（整批撤回 C）、
订正采集时间（A 仅改时间，在同分辨率重叠区反超 B）、移动声明覆盖
（D 删旧格、在旧范围之外补新格）。中心从旧包与订正后**独立构建**的新包生成
补丁；县站只拿本地旧包与补丁，在新目录还原出标准 .mosaic 包。

脚本同时核对：
  * 还原包与中心新包逐窗口提取结果一致、内容指纹一致；
  * 仅订正时间的批次不在补丁里重传任何格记录，整批撤回只传一次批号；
  * 仅订正时间 / 整批撤回的补丁字节数不随对应批次格数增长；
  * 查询结果相同但隐藏源格不同的旧包会被拒绝（旧版指纹不符）。

运行：python3 -I demo/build_patch_evidence.py
输出：demo/output/patch_demo/ 下的包、补丁与提取结果；不修改任何既有镶嵌包。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORK = os.path.join(HERE, "output", "patch_demo")
MAIN = os.path.join(REPO, "agrimosaic", "__main__.py")
DOMAIN = {"reflectance_min": 0.0, "reflectance_max": 1.0,
          "quality_min": 0, "quality_max": 100}


def cell(x, y, s, refl=None, qual=None, state=None):
    row = {"cell": {"x0": x, "y0": y, "x1": x + s, "y1": y + s}}
    if state:
        row["state"] = state
    else:
        row["reflectance"] = refl
        row["quality"] = qual
    return row


def grid(x0, y0, x1, y1, s, refl, qual):
    return [cell(x, y, s, refl, qual)
            for y in range(y0, y1, s) for x in range(x0, x1, s)]


def run(*args, expect=0):
    r = subprocess.run([sys.executable, "-I", MAIN, *args],
                       capture_output=True, text=True, cwd=WORK)
    if r.returncode != expect:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"命令失败（期望退出码 {expect}，实际 {r.returncode}）：{args}")
    return r


def build_pkg(name, batches):
    cfg_batches = []
    for bid, size, when, cov, rows in batches:
        fn = f"{name}_{bid}.jsonl"
        with open(os.path.join(WORK, fn), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        cfg_batches.append({"batch_id": bid, "acquired_at": when, "cell_size": size,
                            "coverage": {"x0": cov[0], "y0": cov[1],
                                         "x1": cov[2], "y1": cov[3]},
                            "source_file": fn})
    cfg_path = os.path.join(WORK, f"{name}_config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"value_domain": DOMAIN, "batches": cfg_batches}, fh)
    out = os.path.join(WORK, name + ".mosaic")
    if os.path.exists(out):
        for f in os.listdir(out):
            os.remove(os.path.join(out, f))
        os.rmdir(out)
    run("build", "--config", cfg_path, "--out", out)
    return out


def extract(pkg, window, size, out_name):
    path = os.path.join(WORK, out_name)
    run("extract", "--package", pkg,
        "--x0", str(window[0]), "--y0", str(window[1]),
        "--x1", str(window[2]), "--y1", str(window[3]),
        "--target-size", str(size), "--output", path)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


os.makedirs(WORK, exist_ok=True)

# ---- 旧包（县站保存）与订正后独立构建的新包 ----
old = build_pkg("old", [
    ("A", 2, "2026-01-01T00:00:00+00:00", (0, 0, 8, 8), grid(0, 0, 8, 8, 2, 0.11, 11)),
    ("B", 2, "2026-02-01T00:00:00+00:00", (0, 0, 8, 8), grid(0, 0, 8, 8, 2, 0.22, 22)),
    ("C", 4, "2026-01-15T00:00:00+00:00", (0, 0, 16, 16), [cell(0, 0, 4, 0.33, 33)]),
    ("D", 2, "2026-01-10T00:00:00+00:00", (16, 0, 24, 8), grid(16, 0, 24, 8, 2, 0.44, 44)),
])
new = build_pkg("new", [
    ("A", 2, "2026-03-01T00:00:00+00:00", (0, 0, 8, 8), grid(0, 0, 8, 8, 2, 0.11, 11)),
    ("B", 2, "2026-02-01T00:00:00+00:00", (0, 0, 8, 8), grid(0, 0, 8, 8, 2, 0.22, 22)),
    ("D", 2, "2026-01-10T00:00:00+00:00", (32, 0, 40, 8), grid(32, 0, 40, 8, 2, 0.55, 55)),
    ("E", 2, "2026-04-01T00:00:00+00:00", (0, 8, 4, 12),
     [cell(0, 8, 2, 0.66, 66), cell(2, 8, 2, 0.67, 67)]),
])

# ---- 中心生成补丁，县站应用补丁 ----
patch_path = os.path.join(WORK, "delivery.patch.json")
run("diff", "--old", old, "--new", new, "--output", patch_path)
with open(patch_path, encoding="utf-8") as fh:
    patch = json.load(fh)
restored = os.path.join(WORK, "restored.mosaic")
if os.path.exists(restored):
    for f in os.listdir(restored):
        os.remove(os.path.join(restored, f))
    os.rmdir(restored)
r = run("apply", "--package", old, "--patch", patch_path, "--out", restored)
summary = json.loads(r.stdout)

# ---- 对照：还原包 == 中心新包（逐窗口、逐格） ----
checks = []
same = all(extract(restored, w, s, "win_restored.json")
           == extract(new, w, s, "win_new.json")
           for w, s in [((0, 0, 8, 8), 2), ((16, 0, 24, 8), 2),
                        ((32, 0, 40, 8), 2), ((0, 8, 4, 12), 2)])
checks.append(("还原包与中心新包逐窗口提取一致", same))
checks.append(("还原包指纹 == 新版指纹",
               summary["fingerprint"] == patch["target_fingerprint"]))

win_old = extract(old, (0, 0, 4, 4), 2, "win_old_overlap.json")
win_new = extract(restored, (0, 0, 4, 4), 2, "win_restored_overlap.json")
checks.append(("订正前重叠区赢家是 B（较新）",
               win_old["cells"][0]["provenance"]["batch_id"] == "B"))
checks.append(("仅订正时间后 A 反超且给出 A 的原始值",
               win_new["cells"][0]["provenance"]["batch_id"] == "A"
               and win_new["cells"][0]["value"] == {"reflectance": 0.11, "quality": 11}))

d_old_range = extract(restored, (16, 0, 24, 8), 2, "win_d_old_range.json")
d_new_range = extract(restored, (32, 0, 40, 8), 2, "win_d_new_range.json")
checks.append(("D 旧范围格已删除（无声明覆盖）",
               d_old_range["cells"][0]["gaps"][0]["reason"] == "no_declared_coverage"))
checks.append(("D 新范围补格且值正确",
               d_new_range["cells"][0]["provenance"]["batch_id"] == "D"
               and d_new_range["cells"][0]["value"] == {"reflectance": 0.55, "quality": 55}))

touched = {e["batch_id"] for e in patch["record_upserts"]} \
    | {e["batch_id"] for e in patch["record_deletions"]}
checks.append(("仅订正时间的 A 不在补丁传任何格记录", "A" not in touched))
checks.append(("整批撤回的 C 只传一次批号",
               patch["batch_removals"] == ["C"] and "C" not in touched))

# ---- 规模不变性：补丁字节数不随对应批次格数增长 ----
sizes = {}
for count in (16, 256):
    rows = grid(0, 0, 32, 32, 4, 0.4, 40)[:count]
    o = build_pkg(f"scale{count}_old", [
        ("T", 4, "2026-01-01T00:00:00+00:00", (0, 0, 32, 32), rows)])
    n_t = build_pkg(f"scale{count}_newtime", [
        ("T", 4, "2026-06-01T00:00:00+00:00", (0, 0, 32, 32), rows)])
    n_w = build_pkg(f"scale{count}_newdrop", [
        ("K", 4, "2026-01-02T00:00:00+00:00", (40, 0, 48, 8), [cell(40, 0, 4, 0.7, 70)])])
    # 整批撤回场景里新包只留一个无关批次；时间订正场景只改 T 的时间
    o2 = build_pkg(f"scale{count}_old2", [
        ("T", 4, "2026-01-01T00:00:00+00:00", (0, 0, 32, 32), rows),
        ("K", 4, "2026-01-02T00:00:00+00:00", (40, 0, 48, 8), [cell(40, 0, 4, 0.7, 70)])])
    for tag, nn in (("time", n_t), ("drop", n_w)):
        p = os.path.join(WORK, f"scale{count}_{tag}.patch.json")
        src = o2 if tag == "drop" else o
        run("diff", "--old", src, "--new", nn, "--output", p)
        sizes[(tag, count)] = os.path.getsize(p)
checks.append(("仅订正时间的补丁字节数不随格数增长（16 格 == 256 格）",
               sizes[("time", 16)] == sizes[("time", 256)]))
checks.append(("整批撤回的补丁字节数不随格数增长（16 格 == 256 格）",
               sizes[("drop", 16)] == sizes[("drop", 256)]))

# ---- 查询结果相同但隐藏源格不同的旧包：必须被拒绝 ----
hidden = build_pkg("hidden", [
    ("A", 2, "2026-01-01T00:00:00+00:00", (0, 0, 8, 8), grid(0, 0, 8, 8, 2, 0.11, 11)),
    ("B", 2, "2026-02-01T00:00:00+00:00", (0, 0, 8, 8), grid(0, 0, 8, 8, 2, 0.22, 22)),
    ("C", 4, "2026-01-15T00:00:00+00:00", (0, 0, 16, 16), [cell(0, 0, 4, 0.99, 99)]),
    ("D", 2, "2026-01-10T00:00:00+00:00", (16, 0, 24, 8), grid(16, 0, 24, 8, 2, 0.44, 44)),
])
same_query = extract(hidden, (0, 0, 8, 8), 4, "win_hidden.json") \
    == extract(old, (0, 0, 8, 8), 4, "win_old8.json")
r = run("apply", "--package", hidden, "--patch", patch_path,
        "--out", os.path.join(WORK, "never.mosaic"), expect=2)
checks.append(("隐藏源格不同的旧包查询结果相同（对照前提）", same_query))
checks.append(("该旧包被拒绝（旧版指纹不符，退出码 2，无目标目录）",
               not os.path.exists(os.path.join(WORK, "never.mosaic"))))

print("增量交付证据（demo/output/patch_demo/）：")
for label, ok in checks:
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")
print(f"  补丁大小：主场景 {os.path.getsize(patch_path)} 字节；"
      f"时间订正 16/256 格 = {sizes[('time', 16)]}/{sizes[('time', 256)]} 字节；"
      f"整批撤回 16/256 格 = {sizes[('drop', 16)]}/{sizes[('drop', 256)]} 字节")
if not all(ok for _, ok in checks):
    raise SystemExit(1)
