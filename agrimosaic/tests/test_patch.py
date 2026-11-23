"""增量交付（diff/apply）测试：端到端对照、内容指纹、拒收、顺序无关与规模不变性。"""

import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys
import unittest

from agrimosaic.extract import extract_window
from agrimosaic.model import InputError
from agrimosaic.patch import (
    apply_patch, content_fingerprint, load_package_content, make_patch, patch_to_json,
)
from agrimosaic.store import build_package, load_package
from tests.helpers import new_tmp

UTC = _dt.timezone.utc
DOMAIN_CFG = {"reflectance_min": 0.0, "reflectance_max": 1.0,
              "quality_min": 0, "quality_max": 100}
DUMMY_FP = "sha256:" + "0" * 64


def cell(x, y, s, refl=None, qual=None, state=None):
    row = {"cell": {"x0": x, "y0": y, "x1": x + s, "y1": y + s}}
    if state is not None:
        row["state"] = state
    else:
        row["reflectance"] = refl
        row["quality"] = qual
    return row


def grid(x0, y0, x1, y1, s, refl, qual):
    return [cell(x, y, s, refl, qual)
            for y in range(y0, y1, s) for x in range(x0, x1, s)]


def build_pkg(tmp, name, batches):
    """batches: [(bid, size, when_iso, coverage, rows)]；rows 元素可为 dict 或原始字符串行。"""
    cfg_batches = []
    for bid, size, when, cov, rows in batches:
        path = os.path.join(tmp, f"{name}_{bid}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(r if isinstance(r, str) else json.dumps(r))
                fh.write("\n")
        cfg_batches.append({
            "batch_id": bid, "acquired_at": when, "cell_size": size,
            "coverage": {"x0": cov[0], "y0": cov[1], "x1": cov[2], "y1": cov[3]},
            "source_file": os.path.basename(path),
        })
    cfg_path = os.path.join(tmp, f"{name}_config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"value_domain": DOMAIN_CFG, "batches": cfg_batches}, fh)
    out = os.path.join(tmp, name + ".mosaic")
    build_package(cfg_path, out)
    return out


def old_batches():
    return [
        ("A", 2, "2026-01-01T00:00:00+00:00", (0, 0, 8, 8), grid(0, 0, 8, 8, 2, 0.11, 11)),
        ("B", 2, "2026-02-01T00:00:00+00:00", (0, 0, 8, 8), grid(0, 0, 8, 8, 2, 0.22, 22)),
        ("C", 4, "2026-01-15T00:00:00+00:00", (0, 0, 16, 16), [cell(0, 0, 4, 0.33, 33)]),
        ("D", 2, "2026-01-10T00:00:00+00:00", (16, 0, 24, 8), grid(16, 0, 24, 8, 2, 0.44, 44)),
    ]


def new_batches():
    # A 仅订正采集时间（1 月 -> 3 月，反超同分辨率的 B）；C 整批撤回；
    # D 覆盖平移、旧格全删、新范围内补格；E 为补交的新批次。
    return [
        ("A", 2, "2026-03-01T00:00:00+00:00", (0, 0, 8, 8), grid(0, 0, 8, 8, 2, 0.11, 11)),
        ("B", 2, "2026-02-01T00:00:00+00:00", (0, 0, 8, 8), grid(0, 0, 8, 8, 2, 0.22, 22)),
        ("D", 2, "2026-01-10T00:00:00+00:00", (32, 0, 40, 8), grid(32, 0, 40, 8, 2, 0.55, 55)),
        ("E", 2, "2026-04-01T00:00:00+00:00", (0, 8, 4, 12),
         [cell(0, 8, 2, 0.66, 66), cell(2, 8, 2, 0.67, 67)]),
    ]


def dir_digest(path):
    h = hashlib.sha256()
    for name in sorted(os.listdir(path)):
        with open(os.path.join(path, name), "rb") as fh:
            h.update(name.encode())
            h.update(fh.read())
    return h.hexdigest()


def write_patch_file(tmp, patch, name="patch.json"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(patch_to_json(patch) if isinstance(patch, dict) else patch)
    return path


def skeleton_patch(old_dir, **overrides):
    """针对本地旧包的合法骨架补丁（base 指纹正确，target 为占位值）。"""
    patch = {
        "patch_format": "agrimosaic.patch",
        "patch_version": 1,
        "package_version": 1,
        "base_fingerprint": content_fingerprint(load_package_content(old_dir)),
        "target_fingerprint": DUMMY_FP,
        "batch_additions": [],
        "batch_removals": [],
        "batch_updates": [],
        "record_upserts": [],
        "record_deletions": [],
    }
    patch.update(overrides)
    return patch


class PatchEndToEndTest(unittest.TestCase):
    """中心独立构建新包 -> diff -> 县站 apply -> 与中心新包逐窗口对照。"""

    def setUp(self):
        self.tmp = new_tmp()
        self.old = build_pkg(self.tmp, "old", old_batches())
        self.new = build_pkg(self.tmp, "new", new_batches())
        self.old_digest = dir_digest(self.old)
        self.patch = make_patch(self.old, self.new)
        self.patch_path = write_patch_file(self.tmp, self.patch)
        self.restored = os.path.join(self.tmp, "restored.mosaic")
        self.summary = apply_patch(self.old, self.patch_path, self.restored)

    def _extract(self, pkg_dir, window, size):
        return extract_window(load_package(pkg_dir), window, size)

    def test_restored_matches_new_package_window_by_window(self):
        for window, size in [((0, 0, 8, 8), 2), ((0, 0, 8, 8), 4),
                             ((16, 0, 24, 8), 2), ((32, 0, 40, 8), 2),
                             ((0, 8, 4, 12), 2), ((0, 0, 16, 16), 4)]:
            self.assertEqual(self._extract(self.restored, window, size),
                             self._extract(self.new, window, size),
                             f"窗口 {window} 提取结果与中心新包不一致")

    def test_time_only_correction_overtakes_same_resolution_batch(self):
        before = self._extract(self.old, (0, 0, 4, 4), 2)
        after = self._extract(self.restored, (0, 0, 4, 4), 2)
        # 订正前：同分辨率下 B（2 月）新于 A（1 月），B 胜出
        self.assertEqual(before["cells"][0]["provenance"]["batch_id"], "B")
        self.assertEqual(before["cells"][0]["value"], {"reflectance": 0.22, "quality": 22})
        # 订正后：A 订正为 3 月，反超 B，且给出 A 的原始值
        self.assertEqual(after["cells"][0]["provenance"]["batch_id"], "A")
        self.assertEqual(after["cells"][0]["value"], {"reflectance": 0.11, "quality": 11})

    def test_coverage_move_deletes_old_cells_and_adds_new_ones(self):
        old_range = self._extract(self.restored, (16, 0, 24, 8), 2)
        self.assertEqual(old_range["summary"], {"empty": 16})
        for c in old_range["cells"]:
            self.assertEqual(c["gaps"][0]["reason"], "no_declared_coverage")
        new_range = self._extract(self.restored, (32, 0, 40, 8), 2)
        for c in new_range["cells"]:
            self.assertEqual(c["provenance"]["batch_id"], "D")
            self.assertEqual(c["value"], {"reflectance": 0.55, "quality": 55})

    def test_added_batch_records_found_with_correct_values(self):
        got = self._extract(self.restored, (0, 8, 4, 12), 2)
        self.assertEqual(got["cells"][0]["value"], {"reflectance": 0.66, "quality": 66})
        self.assertEqual(got["cells"][0]["provenance"]["batch_id"], "E")
        self.assertEqual(got["cells"][1]["value"], {"reflectance": 0.67, "quality": 67})

    def test_withdrawn_batch_fully_gone(self):
        pkg = load_package(self.restored)
        self.assertEqual({bid for bid, _ in pkg.coverages}, {"A", "B", "D", "E"})
        self.assertFalse(any(r.batch_id == "C" for r in pkg.records))

    def test_record_time_follows_batch_correction(self):
        pkg = load_package(self.restored)
        times = {r.batch_id: r.acquired_at for r in pkg.records}
        self.assertEqual(times["A"], _dt.datetime(2026, 3, 1, tzinfo=UTC))
        self.assertEqual(times["B"], _dt.datetime(2026, 2, 1, tzinfo=UTC))
        self.assertEqual(times["D"], _dt.datetime(2026, 1, 10, tzinfo=UTC))
        self.assertEqual(times["E"], _dt.datetime(2026, 4, 1, tzinfo=UTC))

    def test_patch_carries_no_records_for_time_only_or_withdrawn_batches(self):
        touched = {e["batch_id"] for e in self.patch["record_upserts"]} \
            | {e["batch_id"] for e in self.patch["record_deletions"]}
        self.assertNotIn("A", touched)   # 仅订正时间：一条格记录也不重传
        self.assertNotIn("C", touched)   # 整批撤回：不逐格列清单
        self.assertNotIn("B", touched)   # 未变化批次不出现
        self.assertEqual(self.patch["batch_removals"], ["C"])
        self.assertEqual([u["batch_id"] for u in self.patch["batch_updates"]], ["A", "D"])
        self.assertEqual(self.patch["batch_updates"][0]["acquired_at"],
                         "2026-03-01T00:00:00+00:00")

    def test_restored_fingerprint_equals_target_and_new_package(self):
        expected = content_fingerprint(load_package_content(self.new))
        self.assertEqual(self.patch["target_fingerprint"], expected)
        self.assertEqual(self.summary["fingerprint"], expected)
        self.assertEqual(content_fingerprint(load_package_content(self.restored)), expected)

    def test_restored_stats_and_meta_consistent_with_records(self):
        pkg = load_package(self.restored)
        new_pkg = load_package(self.new)
        self.assertEqual(pkg.meta["record_count"], len(pkg.records))
        self.assertEqual(pkg.meta["stats"], new_pkg.meta["stats"])
        self.assertEqual(pkg.meta["record_count"], new_pkg.meta["record_count"])

    def test_apply_does_not_modify_old_package(self):
        self.assertEqual(dir_digest(self.old), self.old_digest)


class PatchRecordModificationTest(unittest.TestCase):
    """保留批次内格记录的内容变化（改值、改状态）随补丁正确还原。"""

    def test_value_change_and_state_flip(self):
        tmp = new_tmp()
        old = build_pkg(tmp, "old", [
            ("A", 2, "2026-01-01T00:00:00+00:00", (0, 0, 4, 2),
             [cell(0, 0, 2, 0.11, 11), cell(2, 0, 2, 0.12, 12)]),
            ("B", 2, "2026-02-01T00:00:00+00:00", (0, 0, 4, 2),
             [cell(0, 0, 2, 0.22, 22), cell(2, 0, 2, 0.23, 23)]),
        ])
        new = build_pkg(tmp, "new", [
            ("A", 2, "2026-01-01T00:00:00+00:00", (0, 0, 4, 2),
             [cell(0, 0, 2, 0.11, 11), cell(2, 0, 2, 0.12, 12)]),
            ("B", 2, "2026-02-01T00:00:00+00:00", (0, 0, 4, 2),
             [cell(0, 0, 2, 0.29, 29), cell(2, 0, 2, state="cloud")]),
        ])
        patch = make_patch(old, new)
        upserts = {(u["cell"]["x0"], u["cell"]["y0"]): u for u in patch["record_upserts"]}
        self.assertEqual(set(upserts), {(0, 0), (2, 0)})
        self.assertEqual(upserts[(0, 0)]["reflectance"], 0.29)
        self.assertEqual(upserts[(2, 0)]["state"], "cloud")
        self.assertNotIn("reflectance", upserts[(2, 0)])

        out = os.path.join(tmp, "out.mosaic")
        apply_patch(old, write_patch_file(tmp, patch), out)
        got = extract_window(load_package(out), (0, 0, 4, 2), 2)
        # 改值：B 仍最新，胜出新值 0.29；改云遮：B 不再可用，A 的有效格胜出
        self.assertEqual(got["cells"][0]["value"], {"reflectance": 0.29, "quality": 29})
        self.assertEqual(got["cells"][0]["provenance"]["batch_id"], "B")
        self.assertEqual(got["cells"][1]["value"], {"reflectance": 0.12, "quality": 12})
        self.assertEqual(got["cells"][1]["provenance"]["batch_id"], "A")


class PatchFingerprintTest(unittest.TestCase):
    """内容指纹：等价表示不产生变更；非内容身份不改变指纹；隐藏内容必被覆盖。"""

    def test_equivalent_timezone_and_number_forms_produce_empty_patch(self):
        tmp = new_tmp()
        old = build_pkg(tmp, "old", [
            ("T", 2, "2026-01-01T08:00:00+08:00", (0, 0, 4, 2),
             ['{"cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}, "reflectance": 5e-1, "quality": 50}',
              '{"cell": {"x0": 2, "y0": 0, "x1": 4, "y1": 2}, "reflectance": 0.25, "quality": 25}']),
        ])
        new = build_pkg(tmp, "new", [
            ("T", 2, "2026-01-01T00:00:00Z", (0, 0, 4, 2),
             ['{"cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}, "reflectance": 0.50, "quality": 50}',
              '{"cell": {"x0": 2, "y0": 0, "x1": 4, "y1": 2}, "reflectance": 2.5e-1, "quality": 25}']),
        ])
        patch = make_patch(old, new)
        for key in ("batch_additions", "batch_removals", "batch_updates",
                    "record_upserts", "record_deletions"):
            self.assertEqual(patch[key], [], key)
        self.assertEqual(patch["base_fingerprint"], patch["target_fingerprint"])

    def test_member_order_source_file_and_config_layout_not_identity(self):
        tmp = new_tmp()
        rows_a = [cell(0, 0, 2, 0.1, 10), cell(2, 0, 2, 0.2, 20)]
        rows_b = [cell(0, 0, 4, 0.3, 30)]
        old = build_pkg(tmp, "old", [
            ("A", 2, "2026-01-01T00:00:00+00:00", (0, 0, 4, 2), rows_a),
            ("B", 4, "2026-01-02T00:00:00+00:00", (0, 0, 4, 4), rows_b),
        ])
        # 同样的内容：批次顺序、JSONL 行序、source_file 名、配置位置都不同
        sub = os.path.join(tmp, "elsewhere")
        os.makedirs(sub)
        new = build_pkg(sub, "new", [
            ("B", 4, "2026-01-02T00:00:00+00:00", (0, 0, 4, 4), rows_b),
            ("A", 2, "2026-01-01T00:00:00+00:00", (0, 0, 4, 2), list(reversed(rows_a))),
        ])
        patch = make_patch(old, new)
        for key in ("batch_additions", "batch_removals", "batch_updates",
                    "record_upserts", "record_deletions"):
            self.assertEqual(patch[key], [], key)
        self.assertEqual(content_fingerprint(load_package_content(old)),
                         content_fingerprint(load_package_content(new)))

    def test_fingerprint_covers_hidden_records_that_never_win(self):
        tmp = new_tmp()
        base = [("F", 1, "2026-01-02T00:00:00+00:00", (0, 0, 2, 2),
                 grid(0, 0, 2, 2, 1, 0.5, 50))]
        old = build_pkg(tmp, "old", base + [
            ("C", 4, "2026-01-01T00:00:00+00:00", (0, 0, 4, 4), [cell(0, 0, 4, 0.9, 90)])])
        other = build_pkg(tmp, "other", base + [
            ("C", 4, "2026-01-01T00:00:00+00:00", (0, 0, 4, 4), [cell(0, 0, 4, 0.1, 10)])])
        # 粗格 C 被更细的 F 全面压过，查询结果相同，但指纹必须不同
        self.assertEqual(extract_window(load_package(old), (0, 0, 2, 2), 2),
                         extract_window(load_package(other), (0, 0, 2, 2), 2))
        self.assertNotEqual(content_fingerprint(load_package_content(old)),
                            content_fingerprint(load_package_content(other)))

    def test_fingerprint_covers_empty_batches(self):
        tmp = new_tmp()
        old = build_pkg(tmp, "old", [
            ("K", 2, "2026-01-01T00:00:00+00:00", (0, 0, 2, 2), [cell(0, 0, 2, 0.5, 50)])])
        new = build_pkg(tmp, "new", [
            ("K", 2, "2026-01-01T00:00:00+00:00", (0, 0, 2, 2), [cell(0, 0, 2, 0.5, 50)]),
            ("EMPTY", 4, "2026-01-03T00:00:00+00:00", (8, 8, 16, 16), [])])
        self.assertNotEqual(content_fingerprint(load_package_content(old)),
                            content_fingerprint(load_package_content(new)))
        patch = make_patch(old, new)
        self.assertEqual([a["batch_id"] for a in patch["batch_additions"]], ["EMPTY"])
        self.assertEqual(patch["record_upserts"], [])
        out = os.path.join(tmp, "out.mosaic")
        apply_patch(old, write_patch_file(tmp, patch), out)
        got = extract_window(load_package(out), (8, 8, 12, 12), 4)
        self.assertEqual(got["cells"][0]["gaps"][0]["reason"], "declared_no_record")

    def test_same_logical_input_generates_byte_identical_patch(self):
        tmp1, tmp2 = new_tmp(), new_tmp()
        old1 = build_pkg(tmp1, "old", old_batches())
        new1 = build_pkg(tmp1, "new", new_batches())
        # 第二份：不同目录、不同批次声明顺序、不同 source_file 名
        reordered_old = [old_batches()[i] for i in (3, 2, 1, 0)]
        reordered_new = [new_batches()[i] for i in (3, 2, 1, 0)]
        old2 = build_pkg(tmp2, "previous", reordered_old)
        new2 = build_pkg(tmp2, "corrected", reordered_new)
        self.assertEqual(patch_to_json(make_patch(old1, new1)),
                         patch_to_json(make_patch(old2, new2)))


class PatchEmptyChangeTest(unittest.TestCase):
    def test_empty_patch_is_generated_and_applies(self):
        tmp = new_tmp()
        old = build_pkg(tmp, "old", old_batches())
        same = build_pkg(tmp, "same", old_batches())
        patch = make_patch(old, same)
        for key in ("batch_additions", "batch_removals", "batch_updates",
                    "record_upserts", "record_deletions"):
            self.assertEqual(patch[key], [], key)
        self.assertEqual(patch["base_fingerprint"], patch["target_fingerprint"])
        out = os.path.join(tmp, "out.mosaic")
        summary = apply_patch(old, write_patch_file(tmp, patch), out)
        self.assertEqual(summary["fingerprint"], patch["base_fingerprint"])
        self.assertEqual(extract_window(load_package(out), (0, 0, 8, 8), 2),
                         extract_window(load_package(old), (0, 0, 8, 8), 2))


class PatchApplyRejectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        self.old = build_pkg(self.tmp, "old", old_batches())
        self.old_digest = dir_digest(self.old)
        self.out = os.path.join(self.tmp, "out.mosaic")

    def assert_rejected(self, patch, msg_part=None):
        path = write_patch_file(self.tmp, patch, "reject.json")
        with self.assertRaises(InputError) as ctx:
            apply_patch(self.old, path, self.out)
        if msg_part is not None:
            self.assertIn(msg_part, str(ctx.exception))
        # 失败不留目标目录、不改写旧包
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(dir_digest(self.old), self.old_digest)

    def test_corrupt_old_package_rejected(self):
        # 记录冗余保存的采集时刻与批次不一致：包已损坏，拒绝在其上应用补丁
        patch = skeleton_patch(self.old)
        records_path = os.path.join(self.old, "records.jsonl")
        with open(records_path, encoding="utf-8") as fh:
            lines = fh.readlines()
        lines[0] = lines[0].replace("2026-01-01T00:00:00+00:00",
                                    "2026-01-02T00:00:00+00:00")
        with open(records_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        with self.assertRaises(InputError) as ctx:
            apply_patch(self.old, write_patch_file(self.tmp, patch), self.out)
        self.assertIn("不一致", str(ctx.exception))
        self.assertFalse(os.path.exists(self.out))

    def test_query_equivalent_but_hidden_different_old_package_rejected(self):
        # 隐藏源格不同（C 的粗格值不同，但 C 被 A/B 细格全面压过，查询结果相同）
        hidden = build_pkg(self.tmp, "hidden", [
            ("A", 2, "2026-01-01T00:00:00+00:00", (0, 0, 8, 8), grid(0, 0, 8, 8, 2, 0.11, 11)),
            ("B", 2, "2026-02-01T00:00:00+00:00", (0, 0, 8, 8), grid(0, 0, 8, 8, 2, 0.22, 22)),
            ("C", 4, "2026-01-15T00:00:00+00:00", (0, 0, 16, 16), [cell(0, 0, 4, 0.99, 99)]),
            ("D", 2, "2026-01-10T00:00:00+00:00", (16, 0, 24, 8), grid(16, 0, 24, 8, 2, 0.44, 44)),
        ])
        for window in ((0, 0, 8, 8), (0, 0, 16, 16)):
            self.assertEqual(extract_window(load_package(self.old), window, 4),
                             extract_window(load_package(hidden), window, 4))
        patch = make_patch(self.old, build_pkg(self.tmp, "new", new_batches()))
        path = write_patch_file(self.tmp, patch)
        with self.assertRaises(InputError) as ctx:
            apply_patch(hidden, path, self.out)
        self.assertIn("旧版指纹", str(ctx.exception))
        self.assertFalse(os.path.exists(self.out))

    def test_update_or_remove_unknown_batch_rejected(self):
        self.assert_rejected(skeleton_patch(self.old, batch_updates=[
            {"batch_id": "ZZZ", "acquired_at": "2026-05-01T00:00:00+00:00",
             "coverage": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}}]), "不存在的批次")
        self.assert_rejected(skeleton_patch(self.old, batch_removals=["ZZZ"]), "不存在的批次")

    def test_add_existing_batch_rejected(self):
        self.assert_rejected(skeleton_patch(self.old, batch_additions=[
            {"batch_id": "A", "cell_size": 2, "acquired_at": "2026-05-01T00:00:00+00:00",
             "coverage": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}}]), "已存在")

    def test_delete_nonexistent_record_rejected(self):
        self.assert_rejected(skeleton_patch(self.old, record_deletions=[
            {"batch_id": "A", "cell": {"x0": 100, "y0": 100, "x1": 102, "y1": 102}}]),
            "不存在")

    def test_conflicting_batch_ops_rejected(self):
        self.assert_rejected(skeleton_patch(
            self.old,
            batch_additions=[{"batch_id": "N", "cell_size": 2,
                              "acquired_at": "2026-05-01T00:00:00+00:00",
                              "coverage": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}}],
            batch_removals=["N"]), "冲突")

    def test_record_op_on_removed_batch_rejected(self):
        self.assert_rejected(skeleton_patch(
            self.old,
            batch_removals=["D"],
            record_upserts=[{"batch_id": "D",
                             "cell": {"x0": 16, "y0": 0, "x1": 18, "y1": 2},
                             "state": "valid", "reflectance": 0.5, "quality": 50}]), "冲突")

    def test_upsert_and_delete_same_cell_rejected(self):
        self.assert_rejected(skeleton_patch(
            self.old,
            record_upserts=[{"batch_id": "A",
                             "cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2},
                             "state": "valid", "reflectance": 0.5, "quality": 50}],
            record_deletions=[{"batch_id": "A",
                               "cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}}]), "冲突")

    def test_duplicate_ops_rejected(self):
        upsert = {"batch_id": "A", "cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2},
                  "state": "valid", "reflectance": 0.5, "quality": 50}
        self.assert_rejected(skeleton_patch(
            self.old, record_upserts=[upsert, upsert]), "重复")
        self.assert_rejected(skeleton_patch(
            self.old, batch_removals=["C", "C"]), "重复")

    def test_redundant_ops_rejected(self):
        # 与现有内容完全相同的写入、没有任何变化的批次订正，都是冗余操作
        self.assert_rejected(skeleton_patch(self.old, record_upserts=[
            {"batch_id": "A", "cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2},
             "state": "valid", "reflectance": 0.11, "quality": 11}]), "冗余")
        self.assert_rejected(skeleton_patch(self.old, batch_updates=[
            {"batch_id": "A", "acquired_at": "2026-01-01T08:00:00+08:00",
             "coverage": {"x0": 0, "y0": 0, "x1": 8, "y1": 8}}]), "冗余")

    def test_illegal_state_value_combinations_rejected(self):
        self.assert_rejected(skeleton_patch(self.old, record_upserts=[
            {"batch_id": "A", "cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2},
             "state": "valid", "reflectance": 0.5}]), "质量值")
        self.assert_rejected(skeleton_patch(self.old, record_upserts=[
            {"batch_id": "A", "cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2},
             "state": "cloud", "reflectance": 0.5, "quality": 50}]), "不得携带")
        self.assert_rejected(skeleton_patch(self.old, record_upserts=[
            {"batch_id": "A", "cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2},
             "state": "bogus"}]), "state")

    def test_misaligned_or_wrong_size_upsert_rejected(self):
        self.assert_rejected(skeleton_patch(self.old, record_upserts=[
            {"batch_id": "A", "cell": {"x0": 1, "y0": 0, "x1": 3, "y1": 2},
             "state": "valid", "reflectance": 0.5, "quality": 50}]), "对齐")
        self.assert_rejected(skeleton_patch(self.old, record_upserts=[
            {"batch_id": "A", "cell": {"x0": 0, "y0": 0, "x1": 4, "y1": 4},
             "state": "valid", "reflectance": 0.5, "quality": 50}]), "边长")

    def test_out_of_coverage_result_rejected_not_silently_dropped(self):
        # 覆盖缩小却不删除越界格：最终包不合规，必须拒绝而不是默默丢弃
        self.assert_rejected(skeleton_patch(self.old, batch_updates=[
            {"batch_id": "D", "acquired_at": "2026-01-10T00:00:00+00:00",
             "coverage": {"x0": 16, "y0": 0, "x1": 18, "y1": 2}}]), "越出")
        # 新增批次的格落在其声明覆盖之外同样拒绝
        self.assert_rejected(skeleton_patch(
            self.old,
            batch_additions=[{"batch_id": "N", "cell_size": 2,
                              "acquired_at": "2026-05-01T00:00:00+00:00",
                              "coverage": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}}],
            record_upserts=[{"batch_id": "N",
                             "cell": {"x0": 4, "y0": 4, "x1": 6, "y1": 6},
                             "state": "valid", "reflectance": 0.5, "quality": 50}]), "越出")

    def test_target_fingerprint_mismatch_rejected(self):
        new = build_pkg(self.tmp, "new", new_batches())
        patch = make_patch(self.old, new)
        patch["target_fingerprint"] = DUMMY_FP
        self.assert_rejected(patch, "新版指纹")

    def test_existing_output_dir_not_overwritten(self):
        new = build_pkg(self.tmp, "new", new_batches())
        patch = make_patch(self.old, new)
        os.makedirs(self.out)
        sentinel = os.path.join(self.out, "sentinel.txt")
        with open(sentinel, "w") as fh:
            fh.write("keep me")
        with self.assertRaises(InputError) as ctx:
            apply_patch(self.old, write_patch_file(self.tmp, patch), self.out)
        self.assertIn("已存在", str(ctx.exception))
        # 既有目标原样保留、旧包不被改写
        self.assertEqual(os.listdir(self.out), ["sentinel.txt"])
        with open(sentinel) as fh:
            self.assertEqual(fh.read(), "keep me")
        self.assertEqual(dir_digest(self.old), self.old_digest)

    def test_bad_patch_structure_rejected(self):
        cases = [
            skeleton_patch(self.old, zzz=1),
            skeleton_patch(self.old, patch_version=2),
            skeleton_patch(self.old, patch_format="other.format"),
            skeleton_patch(self.old, package_version=99),
            skeleton_patch(self.old, base_fingerprint="not-a-fingerprint"),
            skeleton_patch(self.old, record_upserts=[
                {"batch_id": "A", "cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2},
                 "state": "valid", "reflectance": 0.5, "quality": 50, "size": 2}]),
        ]
        for patch in cases:
            self.assert_rejected(patch)
        # JSON 对象内重复键
        good = patch_to_json(skeleton_patch(self.old))
        dup = good.replace('"patch_version": 1,', '"patch_version": 1, "patch_version": 1,', 1)
        self.assert_rejected(dup, "重复键")

    def test_diff_rejects_cell_size_change_of_shared_batch(self):
        changed = [("A", 4, "2026-03-01T00:00:00+00:00", (0, 0, 8, 8),
                    grid(0, 0, 8, 8, 4, 0.11, 11))] + new_batches()[1:]
        new = build_pkg(self.tmp, "newsize", changed)
        with self.assertRaises(InputError) as ctx:
            make_patch(self.old, new)
        self.assertIn("格网边长", str(ctx.exception))

    def test_diff_rejects_value_domain_change(self):
        cfg_dir = os.path.join(self.tmp, "dom")
        os.makedirs(cfg_dir)
        path = os.path.join(cfg_dir, "x.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps(cell(0, 0, 2, 0.5, 50)) + "\n")
        cfg = {"value_domain": {"reflectance_min": 0.0, "reflectance_max": 2.0,
                                "quality_min": 0, "quality_max": 100},
               "batches": [{"batch_id": "A", "acquired_at": "2026-01-01T00:00:00+00:00",
                            "cell_size": 2,
                            "coverage": {"x0": 0, "y0": 0, "x1": 2, "y1": 2},
                            "source_file": "x.jsonl"}]}
        cfg_path = os.path.join(cfg_dir, "cfg.json")
        with open(cfg_path, "w") as fh:
            json.dump(cfg, fh)
        other = os.path.join(self.tmp, "other.mosaic")
        build_package(cfg_path, other)
        with self.assertRaises(InputError) as ctx:
            make_patch(self.old, other)
        self.assertIn("值域", str(ctx.exception))


class PatchOrderIndependenceTest(unittest.TestCase):
    def test_array_listing_order_does_not_change_result(self):
        tmp = new_tmp()
        old = build_pkg(tmp, "old", old_batches())
        new = build_pkg(tmp, "new", new_batches())
        patch = make_patch(old, new)
        shuffled = dict(patch)
        for key in ("batch_additions", "batch_removals", "batch_updates",
                    "record_upserts", "record_deletions"):
            shuffled[key] = list(reversed(shuffled[key]))
        out = os.path.join(tmp, "out.mosaic")
        summary = apply_patch(old, write_patch_file(tmp, shuffled), out)
        self.assertEqual(summary["fingerprint"], patch["target_fingerprint"])
        self.assertEqual(content_fingerprint(load_package_content(out)),
                         content_fingerprint(load_package_content(new)))


class PatchScalingTest(unittest.TestCase):
    """仅订正时间与整批撤回的补丁大小不随对应批次的格数增长。"""

    def _packages(self, tmp, record_count, new_time=None, withdraw=False):
        cov = (0, 0, 32, 32)
        rows = grid(0, 0, 32, 32, 4, 0.4, 40)[:record_count]
        old = build_pkg(tmp, "old", [
            ("T", 4, "2026-01-01T00:00:00+00:00", cov, rows),
            ("K", 4, "2026-01-02T00:00:00+00:00", (40, 0, 48, 8), [cell(40, 0, 4, 0.7, 70)]),
        ])
        new_spec = [] if withdraw else [
            ("T", 4, new_time or "2026-01-01T00:00:00+00:00", cov, rows)]
        new_spec.append(("K", 4, "2026-01-02T00:00:00+00:00", (40, 0, 48, 8),
                         [cell(40, 0, 4, 0.7, 70)]))
        new = build_pkg(tmp, "new", new_spec)
        return old, new

    def test_time_only_patch_size_independent_of_record_count(self):
        patches = []
        for count in (16, 256):
            tmp = new_tmp()
            old, new = self._packages(tmp, count, new_time="2026-06-01T00:00:00+00:00")
            patch = make_patch(old, new)
            self.assertEqual(patch["record_upserts"], [])
            self.assertEqual(patch["record_deletions"], [])
            self.assertEqual(len(patch["batch_updates"]), 1)
            patches.append(patch_to_json(patch))
        self.assertEqual(len(patches[0]), len(patches[1]))

    def test_withdrawal_patch_size_independent_of_record_count(self):
        patches = []
        for count in (16, 256):
            tmp = new_tmp()
            old, new = self._packages(tmp, count, withdraw=True)
            patch = make_patch(old, new)
            self.assertEqual(patch["batch_removals"], ["T"])
            self.assertEqual(patch["record_upserts"], [])
            self.assertEqual(patch["record_deletions"], [])
            patches.append(patch_to_json(patch))
        self.assertEqual(len(patches[0]), len(patches[1]))


class PatchCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        self.repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _run(self, *args):
        main_py = os.path.join(self.repo, "agrimosaic", "__main__.py")
        return subprocess.run([sys.executable, "-I", main_py, *args],
                              cwd=self.tmp, capture_output=True, text=True)

    def test_cli_diff_apply_extract_roundtrip(self):
        old = build_pkg(self.tmp, "old", old_batches())
        new = build_pkg(self.tmp, "new", new_batches())
        patch_path = os.path.join(self.tmp, "patch.json")
        r = self._run("diff", "--old", old, "--new", new, "--output", patch_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(patch_path))

        out = os.path.join(self.tmp, "restored.mosaic")
        r = self._run("apply", "--package", old, "--patch", patch_path, "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        summary = json.loads(r.stdout)
        self.assertEqual(summary["batch_count"], 4)

        result_path = os.path.join(self.tmp, "window.json")
        r = self._run("extract", "--package", out, "--x0", "0", "--y0", "0",
                      "--x1", "4", "--y1", "4", "--target-size", "2",
                      "--output", result_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.load(open(result_path, encoding="utf-8"))
        self.assertEqual(got["cells"][0]["provenance"]["batch_id"], "A")

    def test_cli_apply_rejection_exit_2_and_no_output(self):
        old = build_pkg(self.tmp, "old", old_batches())
        new = build_pkg(self.tmp, "new", new_batches())
        patch = make_patch(old, new)
        patch["target_fingerprint"] = DUMMY_FP
        patch_path = write_patch_file(self.tmp, patch)
        out = os.path.join(self.tmp, "never.mosaic")
        r = self._run("apply", "--package", old, "--patch", patch_path, "--out", out)
        self.assertEqual(r.returncode, 2)
        self.assertIn("拒收", r.stderr)
        self.assertFalse(os.path.exists(out))

    def test_cli_diff_incompatible_packages_exit_2(self):
        old = build_pkg(self.tmp, "old", old_batches())
        changed = [("A", 4, "2026-03-01T00:00:00+00:00", (0, 0, 8, 8),
                    grid(0, 0, 8, 8, 4, 0.11, 11))] + new_batches()[1:]
        new = build_pkg(self.tmp, "new", changed)
        r = self._run("diff", "--old", old, "--new", new)
        self.assertEqual(r.returncode, 2)
        self.assertIn("格网边长", r.stderr)


if __name__ == "__main__":
    unittest.main()
