"""批次质量审计的测试。

覆盖：
* 独立手算的小包：有效记录数、区间外记录数、异常面积与最小包围矩形；
* 被更细格压过、被较新批次压过（从未胜出）的异常记录仍被发现；
* 异常数恰达门槛即未通过、差一即通过；
* 空批次（零记录 / 全不可用）零统计；
* 配置引用非法、包/记录损坏属于输入错误，不产生部分报告、不误标质量未通过；
* 极大地块稀疏源格：面积按 s² 累计，处理量随记录数增长而非覆盖面积；
* 稳定排序与序列化、CLI 退出码、stdout 不输出半截 JSON、失败保持既有输出文件。
"""

import json
import os
import subprocess
import sys
import unittest

from agrimosaic.audit import (
    AUDIT_FAIL, AUDIT_PASS, AuditRule, audit_batch_records,
    parse_audit_config, run_audit,
)
from agrimosaic.extract import extract_cell
from agrimosaic.model import InputError
from agrimosaic.store import build_package, load_package
from tests.helpers import new_tmp, write_config, write_fixture

UTC = "2026-01-01T00:00:00+00:00"


def cell(x, y, size, refl=0.1, qual=10, state=None, when=None):
    row = {"cell": {"x0": x, "y0": y, "x1": x + size, "y1": y + size},
           "acquired_at": when or UTC}
    if state:
        row["state"] = state
    else:
        row["reflectance"] = refl
        row["quality"] = qual
    return row


def batch_entry(bid, size, coverage, source, when):
    return {"batch_id": bid, "acquired_at": when, "cell_size": size,
            "coverage": {"x0": coverage[0], "y0": coverage[1],
                         "x1": coverage[2], "y1": coverage[3]},
            "source_file": source}


def build_pkg(tmp, name, batches):
    """batches: [(batch_id, cell_size, when, coverage, rows)] -> 包目录。"""
    entries = []
    for bid, size, when, coverage, rows in batches:
        path = write_fixture(tmp, f"{name}_{bid}.jsonl", rows)
        entries.append(batch_entry(bid, size, coverage, os.path.basename(path), when))
    cfg = write_config(tmp, entries)
    out = os.path.join(tmp, name + ".mosaic")
    build_package(cfg, out)
    return out


def write_audit_config(tmp, packages, batches, name="audit.json"):
    obj = {"packages": packages, "batches": batches}
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    return path


def rule(channel="reflectance", lo=0.0, hi=0.5, threshold=1):
    return {"channel": channel, "min": lo, "max": hi,
            "anomaly_threshold": threshold}


def spec(bid, *rules):
    return {"batch_id": bid, "rules": list(rules)}


def audit_names(tmp, packages, batches):
    cfg = write_audit_config(tmp, packages, batches)
    return run_audit(parse_audit_config(cfg))


def hand_package(tmp):
    """独立手算的小包（边长单位：基本单元）。

    批次（采集时刻越晚越新）：
      FINE    size1 2026-03-01 覆盖 (0,0,4,4)：16 格全部正常 (0.1,10)
      COARSE  size4 2026-01-01 覆盖 (0,0,8,8)：(0,0,4,4) 异常 (0.9,80)，
                                                (4,0,8,4) 正常 (0.2,20)
      MID_OLD size2 2026-01-05 覆盖 (4,0,8,8)：(4,0,6,2) 异常 (0.85,85)，其余正常
      MID_NEW size2 2026-02-01 覆盖 (4,0,8,8)：四格全部正常 (0.2,20)
      EMPTY   size2 2026-01-01 覆盖 (8,8,12,12)：零记录

    空间裁决中 COARSE 的 (0,0,4,4) 被更细 FINE 全面压过；
    MID_OLD 的 (4,0,6,2) 被同分辨率但更新的 MID_NEW 压过。
    两条异常记录都从未胜出，但审计必须仍然发现。
    """
    fine = [cell(x, y, 1, 0.1, 10) for y in range(4) for x in range(4)]
    coarse = [cell(0, 0, 4, 0.9, 80), cell(4, 0, 4, 0.2, 20)]
    mid_old = [
        cell(4, 0, 2, 0.85, 85), cell(6, 0, 2, 0.2, 20),
        cell(4, 2, 2, 0.2, 20), cell(6, 2, 2, 0.2, 20),
    ]
    mid_new = [cell(x, y, 2, 0.2, 20) for y in (0, 2) for x in (4, 6)]
    return build_pkg(tmp, "hand", [
        ("FINE", 1, "2026-03-01T00:00:00+00:00", (0, 0, 4, 4), fine),
        ("COARSE", 4, "2026-01-01T00:00:00+00:00", (0, 0, 8, 8), coarse),
        ("MID_OLD", 2, "2026-01-05T00:00:00+00:00", (4, 0, 8, 8), mid_old),
        ("MID_NEW", 2, "2026-02-01T00:00:00+00:00", (4, 0, 8, 8), mid_new),
        ("EMPTY", 2, "2026-01-01T00:00:00+00:00", (8, 8, 12, 12), []),
    ])


class HandComputedAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        self.pkg_dir = hand_package(self.tmp)
        self.pkg = load_package(self.pkg_dir)

    def _entry(self, report, bid):
        return next(e for e in report["audit"] if e["batch_id"] == bid)

    def test_anomaly_hidden_behind_finer_cell_is_found(self):
        # 提取裁决中 COARSE 的 (0,0,4,4) 从不胜出（FINE 更细且铺满）
        w = extract_cell((0, 0, 2, 2), self.pkg)
        self.assertEqual({x["source"]["batch_id"] for x in w["winners"]}, {"FINE"})
        report = audit_names(self.tmp, [self.pkg_dir], [spec("COARSE", rule())])
        entry = self._entry(report, "COARSE")
        r = entry["rules"][0]
        self.assertEqual(r["effective_record_count"], 2)
        self.assertEqual(r["anomaly_record_count"], 1)
        self.assertEqual(r["anomaly_area"], 16)  # size 4 -> 4²
        self.assertEqual(r["anomaly_bounds"], {"x0": 0, "y0": 0, "x1": 4, "y1": 4})
        self.assertEqual(entry["status"], AUDIT_FAIL)
        self.assertEqual(report["summary"]["status"], AUDIT_FAIL)

    def test_anomaly_hidden_behind_newer_batch_is_found(self):
        # 同分辨率下 MID_NEW 更新，MID_OLD 从不胜出
        w = extract_cell((4, 0, 6, 2), self.pkg)
        self.assertEqual(w["provenance"]["batch_id"], "MID_NEW")
        report = audit_names(self.tmp, [self.pkg_dir], [spec("MID_OLD", rule())])
        r = self._entry(report, "MID_OLD")["rules"][0]
        self.assertEqual(r["effective_record_count"], 4)
        self.assertEqual(r["anomaly_record_count"], 1)
        self.assertEqual(r["anomaly_area"], 4)  # size 2 -> 2²
        self.assertEqual(r["anomaly_bounds"], {"x0": 4, "y0": 0, "x1": 6, "y1": 2})

    def test_quality_channel_and_clean_batch(self):
        report = audit_names(self.tmp, [self.pkg_dir], [
            spec("COARSE", rule("quality", 0, 50, 1)),
            spec("FINE", rule("quality", 0, 50, 1)),
            spec("MID_NEW", rule("quality", 0, 50, 1)),
        ])
        coarse = self._entry(report, "COARSE")["rules"][0]
        self.assertEqual((coarse["effective_record_count"],
                          coarse["anomaly_record_count"],
                          coarse["anomaly_area"]), (2, 1, 16))
        fine = self._entry(report, "FINE")["rules"][0]
        self.assertEqual((fine["effective_record_count"],
                          fine["anomaly_record_count"],
                          fine["anomaly_area"],
                          fine["anomaly_bounds"]), (16, 0, 0, None))
        self.assertEqual(fine["status"], AUDIT_PASS)
        mid_new = self._entry(report, "MID_NEW")["rules"][0]
        self.assertEqual((mid_new["effective_record_count"],
                          mid_new["anomaly_record_count"]), (4, 0))

    def test_zero_record_batch_reports_zero_statistics(self):
        report = audit_names(self.tmp, [self.pkg_dir], [
            spec("EMPTY", rule()), spec("FINE", rule())])
        entry = self._entry(report, "EMPTY")
        self.assertEqual(entry["declared_record_count"], 0)
        self.assertEqual(entry["rules"][0]["effective_record_count"], 0)
        self.assertEqual(entry["rules"][0]["anomaly_record_count"], 0)
        self.assertEqual(entry["rules"][0]["anomaly_area"], 0)
        self.assertIsNone(entry["rules"][0]["anomaly_bounds"])
        self.assertEqual(entry["status"], AUDIT_PASS)

    def test_all_unavailable_batch_reports_zero_effective(self):
        tmp = new_tmp()
        pkg = build_pkg(tmp, "cloudy", [(
            "C", 2, UTC, (0, 0, 4, 4),
            [cell(0, 0, 2, state="cloud"), cell(2, 0, 2, state="missing_channel")],
        )])
        report = audit_names(tmp, [pkg], [spec("C", rule())])
        r = report["audit"][0]["rules"][0]
        self.assertEqual(r["effective_record_count"], 0)
        self.assertEqual(r["anomaly_record_count"], 0)
        self.assertIsNone(r["anomaly_bounds"])

    def test_empty_package_with_only_zero_record_batch(self):
        # 整包零记录：R 树只有空叶子，审计仍给出零统计而不是报结构损坏
        tmp = new_tmp()
        pkg = build_pkg(tmp, "nothing", [("N", 1, UTC, (0, 0, 4, 4), [])])
        report = audit_names(tmp, [pkg], [spec("N", rule())])
        entry = report["audit"][0]
        self.assertEqual(entry["declared_record_count"], 0)
        self.assertEqual(entry["rules"][0]["effective_record_count"], 0)
        self.assertEqual(entry["rules"][0]["anomaly_bounds"], None)
        self.assertEqual(entry["status"], AUDIT_PASS)

    def test_threshold_boundary_reached_is_fail(self):
        # 1 个异常：门槛 1 达到即未通过；门槛 2 差一即通过
        at = audit_names(self.tmp, [self.pkg_dir], [spec("COARSE", rule(threshold=1))])
        self.assertEqual(self._entry(at, "COARSE")["status"], AUDIT_FAIL)
        below = audit_names(self.tmp, [self.pkg_dir], [spec("COARSE", rule(threshold=2))])
        self.assertEqual(self._entry(below, "COARSE")["status"], AUDIT_PASS)
        self.assertEqual(below["summary"]["failed_batch_count"], 0)
        self.assertEqual(below["summary"]["status"], AUDIT_PASS)

    def test_interval_endpoints_are_inclusive(self):
        tmp = new_tmp()
        pkg = build_pkg(tmp, "edges", [(
            "E", 1, UTC, (0, 0, 3, 1),
            [cell(0, 0, 1, 0.2, 20), cell(1, 0, 1, 0.5, 50),
             cell(2, 0, 1, 0.5000001, 51)],
        )])
        report = audit_names(tmp, [pkg], [spec("E", rule("reflectance", 0.2, 0.5, 5))])
        r = report["audit"][0]["rules"][0]
        self.assertEqual(r["effective_record_count"], 3)
        self.assertEqual(r["anomaly_record_count"], 1)  # 端点 0.2/0.5 合格
        self.assertEqual(r["anomaly_area"], 1)

    def test_minimum_bounding_rectangle_and_area(self):
        # 同批 size2 异常格 (0,2)-(2,4) 与 (6,8)-(8,10)：面积 4+4，包围 (0,2)-(8,10)
        tmp = new_tmp()
        pkg = build_pkg(tmp, "bounds", [(
            "B", 2, UTC, (0, 0, 10, 10),
            [cell(0, 2, 2, 0.9, 90), cell(6, 8, 2, 0.9, 90),
             cell(2, 2, 2, 0.1, 10)],
        )])
        report = audit_names(tmp, [pkg], [spec("B", rule(threshold=1))])
        r = report["audit"][0]["rules"][0]
        self.assertEqual(r["anomaly_record_count"], 2)
        self.assertEqual(r["anomaly_area"], 8)
        self.assertEqual(r["anomaly_bounds"],
                         {"x0": 0, "y0": 2, "x1": 8, "y1": 10})
        self.assertEqual(r["status"], AUDIT_FAIL)

    def test_only_valid_records_with_channel_count_once(self):
        # valid 记录同时计入两通道；cloud/missing 不计；每条记录只计一次
        report = audit_names(self.tmp, [self.pkg_dir], [
            spec("COARSE", rule("reflectance", 0.0, 1.0, 1),
                 rule("quality", 0, 100, 1)),
            spec("MID_OLD", rule("reflectance", 0.0, 1.0, 1))])
        coarse = self._entry(report, "COARSE")["rules"]
        self.assertEqual([r["effective_record_count"] for r in coarse], [2, 2])
        mid = self._entry(report, "MID_OLD")["rules"][0]
        self.assertEqual(mid["effective_record_count"], 4)


class AuditConfigValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        self.pkg_dir = hand_package(self.tmp)

    def _parse(self, obj=None, text=None):
        path = os.path.join(self.tmp, "bad_audit.json")
        with open(path, "w", encoding="utf-8") as fh:
            if text is not None:
                fh.write(text)
            else:
                json.dump(obj, fh)
        return parse_audit_config(path)

    def test_unknown_batch_reference_rejected(self):
        cfg = self._parse({"packages": [self.pkg_dir],
                           "batches": [spec("NOPE", rule())]})
        with self.assertRaises(InputError) as ctx:
            run_audit(cfg)
        self.assertIn("未声明", str(ctx.exception))

    def test_invalid_channel_rejected(self):
        with self.assertRaises(InputError):
            self._parse({"packages": [self.pkg_dir],
                         "batches": [spec("FINE", rule("elevation"))]})

    def test_inverted_interval_rejected(self):
        with self.assertRaises(InputError):
            self._parse({"packages": [self.pkg_dir],
                         "batches": [spec("FINE", rule(lo=0.9, hi=0.1))]})

    def test_threshold_must_be_positive_int(self):
        for bad in (0, -1, 1.5, True):
            with self.assertRaises(InputError):
                self._parse({"packages": [self.pkg_dir],
                             "batches": [spec("FINE", rule(threshold=bad))]})

    def test_unknown_fields_and_duplicate_keys_rejected(self):
        with self.assertRaises(InputError):
            self._parse({"packages": [self.pkg_dir],
                         "batches": [{"batch_id": "FINE",
                                      "rules": [dict(rule(), extra=1)]}]})
        with self.assertRaises(InputError):
            self._parse(text='{"packages": ["a"], "packages": ["b"], "batches": []}')

    def test_duplicate_batch_spec_rejected(self):
        with self.assertRaises(InputError):
            self._parse({"packages": [self.pkg_dir],
                         "batches": [spec("FINE", rule()), spec("FINE", rule())]})

    def test_missing_package_dir_is_input_error(self):
        cfg = self._parse({"packages": [os.path.join(self.tmp, "absent")],
                           "batches": [spec("FINE", rule())]})
        with self.assertRaises(InputError):
            run_audit(cfg)


class CorruptPackageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        self.pkg_dir = hand_package(self.tmp)
        self.cfg = write_audit_config(
            self.tmp, [self.pkg_dir], [spec("COARSE", rule())])

    def test_record_count_mismatch_rejected(self):
        meta_path = os.path.join(self.pkg_dir, "meta.json")
        meta = json.load(open(meta_path, encoding="utf-8"))
        meta["record_count"] += 1
        json.dump(meta, open(meta_path, "w", encoding="utf-8"))
        with self.assertRaises(InputError):
            run_audit(parse_audit_config(self.cfg))

    def test_unknown_batch_in_records_rejected(self):
        rec_path = os.path.join(self.pkg_dir, "records.jsonl")
        lines = open(rec_path, encoding="utf-8").read().splitlines()
        obj = json.loads(lines[0])
        obj["batch_id"] = "GHOST"
        lines[0] = json.dumps(obj)
        open(rec_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        with self.assertRaises(InputError):
            run_audit(parse_audit_config(self.cfg))

    def test_out_of_coverage_record_rejected(self):
        rec_path = os.path.join(self.pkg_dir, "records.jsonl")
        lines = open(rec_path, encoding="utf-8").read().splitlines()
        obj = json.loads(lines[0])
        if obj["batch_id"] == "COARSE":
            obj["cell"]["x0"] = 8  # 越出 COARSE 覆盖 (0,0,8,8)
            obj["cell"]["x1"] = 12
        else:
            obj["cell"]["x0"] = 100
            obj["cell"]["x1"] = 101
        lines[0] = json.dumps(obj)
        open(rec_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        with self.assertRaises(InputError):
            run_audit(parse_audit_config(self.cfg))

    def test_broken_rtree_rejected(self):
        path = os.path.join(self.pkg_dir, "rtree.json")
        open(path, "w", encoding="utf-8").write("{not json")
        with self.assertRaises(InputError):
            run_audit(parse_audit_config(self.cfg))

    def test_rtree_out_of_bounds_leaf_id_rejected(self):
        path = os.path.join(self.pkg_dir, "rtree.json")
        idx = json.load(open(path, encoding="utf-8"))
        for node in idx["nodes"]:
            if node["leaf"] and node["children"]:
                node["children"][0][1] = 10 ** 6
                break
        json.dump(idx, open(path, "w", encoding="utf-8"))
        with self.assertRaises(InputError):
            run_audit(parse_audit_config(self.cfg))

    def test_batch_declared_in_two_packages_rejected(self):
        # 两个包声明同一批号：引用歧义，按输入错误拒收而不是把记录相加
        other = build_pkg(self.tmp, "other", [(
            "COARSE", 4, UTC, (0, 0, 8, 8), [cell(0, 0, 4, 0.9, 90)])])
        cfg = write_audit_config(self.tmp, [self.pkg_dir, other],
                                 [spec("COARSE", rule())])
        with self.assertRaises(InputError) as ctx:
            run_audit(parse_audit_config(cfg))
        self.assertIn("多个包", str(ctx.exception))

    def test_audit_is_read_only(self):
        before = {name: open(os.path.join(self.pkg_dir, name), "rb").read()
                  for name in os.listdir(self.pkg_dir)}
        run_audit(parse_audit_config(self.cfg))
        after = {name: open(os.path.join(self.pkg_dir, name), "rb").read()
                 for name in os.listdir(self.pkg_dir)}
        self.assertEqual(before, after)


class SparseHugeCellTest(unittest.TestCase):
    """多条互不相交的大尺寸稀疏源格：面积按 s² 累计，处理量随记录数增长。"""

    SIZE = 2 ** 20
    STEP = 2 ** 21

    def setUp(self):
        self.tmp = new_tmp()

    def _build(self, name, count, size=SIZE):
        rows = []
        for i in range(count):
            x = i * self.STEP
            rows.append(cell(x, 0, size, 0.9, 90))
        span = max(count, 1) * self.STEP
        pkg = build_pkg(self.tmp, name, [(
            "HUGE", size, UTC, (0, 0, span, size), rows)])
        return pkg

    def test_area_scales_with_record_count_not_covered_units(self):
        pkg4 = self._build("huge4", 4)
        pkg8 = self._build("huge8", 8)
        report4 = audit_names(self.tmp, [pkg4], [spec("HUGE", rule(threshold=100))])
        report8 = audit_names(self.tmp, [pkg8], [spec("HUGE", rule(threshold=100))])
        r4 = report4["audit"][0]["rules"][0]
        r8 = report8["audit"][0]["rules"][0]
        unit = self.SIZE * self.SIZE
        self.assertEqual((r4["effective_record_count"],
                          r4["anomaly_record_count"],
                          r4["anomaly_area"]), (4, 4, 4 * unit))
        self.assertEqual((r8["effective_record_count"],
                          r8["anomaly_record_count"],
                          r8["anomaly_area"]), (8, 8, 8 * unit))
        # 每条异常记录只贡献自身 s²，而不是逐基本单元展开
        self.assertEqual(r8["anomaly_area"] // r8["anomaly_record_count"], unit)
        self.assertEqual(r4["anomaly_bounds"],
                         {"x0": 0, "y0": 0, "x1": 3 * self.STEP + self.SIZE,
                          "y1": self.SIZE})

    def test_processing_is_linear_in_records(self):
        # 记录条数相同、覆盖面积相差 2^40 倍时，审计仍只遍历记录本身。
        small = [cell(i, 0, 1, 0.9, 90) for i in range(32)]
        huge = [cell(i * self.STEP, 0, self.SIZE, 0.9, 90) for i in range(32)]
        rule_obj = AuditRule("reflectance", 0.0, 0.1, 1)

        class Counting(list):
            def __init__(self, items):
                super().__init__(items)
                self.visited = 0

            def __iter__(self):
                for item in super().__iter__():
                    self.visited += 1
                    yield item

        from agrimosaic.model import SourceRecord
        def records(rows, size):
            out = []
            for i, _ in enumerate(rows):
                x = i * (self.STEP if size == self.SIZE else 1)
                out.append(SourceRecord("H", (x, 0, x + size, size), size,
                                        "valid", 0.9, 90))
            return out

        small_seq = Counting(records(small, 1))
        huge_seq = Counting(records(huge, self.SIZE))
        small_stats = audit_batch_records(small_seq, rule_obj)
        huge_stats = audit_batch_records(huge_seq, rule_obj)
        self.assertEqual(small_seq.visited, 32)
        self.assertEqual(huge_seq.visited, 32)  # 遍历次数与格覆盖面积无关
        self.assertEqual(huge_stats["anomaly_area"], 32 * self.SIZE ** 2)
        self.assertGreater(huge_stats["anomaly_area"],
                           small_stats["anomaly_area"] * 10 ** 6)


class AuditCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        self.pkg_dir = hand_package(self.tmp)
        self.repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _run(self, *args):
        main_py = os.path.join(self.repo, "agrimosaic", "__main__.py")
        return subprocess.run([sys.executable, "-I", main_py, *args],
                              cwd=self.tmp, capture_output=True, text=True)

    def _cfg(self, batches, packages=None, name="audit.json"):
        return write_audit_config(self.tmp, packages or [self.pkg_dir],
                                  batches, name)

    def test_pass_exit_zero(self):
        cfg = self._cfg([spec("FINE", rule(threshold=1)),
                         spec("MID_NEW", rule(threshold=1))])
        r = self._run("audit", "--config", cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(report["summary"]["status"], AUDIT_PASS)

    def test_fail_exit_one_with_full_report(self):
        cfg = self._cfg([spec("COARSE", rule(threshold=1)),
                         spec("FINE", rule(threshold=1))])
        r = self._run("audit", "--config", cfg)
        self.assertEqual(r.returncode, 1, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(report["summary"]["failed_batch_ids"], ["COARSE"])
        # 完整报告仍包含全部批次
        self.assertEqual([e["batch_id"] for e in report["audit"]],
                         ["COARSE", "FINE"])

    def test_input_error_exit_two_stdout_empty_and_file_preserved(self):
        cfg = self._cfg([spec("NOPE", rule())])
        target = os.path.join(self.tmp, "report.json")
        sentinel = "预置内容，必须保持原样\n"
        open(target, "w", encoding="utf-8").write(sentinel)
        r = self._run("audit", "--config", cfg, "--output", target)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")  # stdout 模式与文件模式都不产生半截 JSON
        self.assertEqual(open(target, encoding="utf-8").read(), sentinel)

    def test_corrupt_package_exit_two_not_quality_fail(self):
        path = os.path.join(self.pkg_dir, "records.jsonl")
        open(path, "w", encoding="utf-8").write("not-json\n")
        cfg = self._cfg([spec("COARSE", rule())])
        r = self._run("audit", "--config", cfg)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertIn("拒收", r.stderr)

    def test_output_file_written_on_fail_and_deterministic(self):
        cfg = self._cfg([spec("COARSE", rule(threshold=1)),
                         spec("FINE", rule(threshold=1)),
                         spec("EMPTY", rule(threshold=1))])
        out1 = os.path.join(self.tmp, "r1.json")
        out2 = os.path.join(self.tmp, "r2.json")
        r1 = self._run("audit", "--config", cfg, "--output", out1)
        r2 = self._run("audit", "--config", cfg, "--output", out2)
        self.assertEqual(r1.returncode, 1)
        self.assertEqual(r2.returncode, 1)
        self.assertEqual(open(out1, "rb").read(), open(out2, "rb").read())
        report = json.load(open(out1, encoding="utf-8"))
        self.assertEqual(report["summary"]["failed_batch_ids"], ["COARSE"])

    def test_batch_order_stable_regardless_of_config_order(self):
        a = audit_names(self.tmp, [self.pkg_dir],
                        [spec("FINE", rule()), spec("COARSE", rule())])
        b = audit_names(self.tmp, [self.pkg_dir],
                        [spec("COARSE", rule()), spec("FINE", rule())], )
        # audit_names 每次写同一个 audit.json，故重新序列化比较
        self.assertEqual(json.dumps(a, ensure_ascii=False),
                         json.dumps(b, ensure_ascii=False))

    def test_existing_build_extract_season_cli_unaffected(self):
        # 审计只新增子命令；既有命令帮助仍可用
        for cmd in ("build", "extract", "season", "audit"):
            r = self._run(cmd, "--help")
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
