import json
import os
import subprocess
import sys
import unittest

from agrimosaic.model import InputError
from agrimosaic.store import build_package, load_package, load_config, read_records
from tests.helpers import new_tmp, write_config, write_fixture


def base_batch_entry(path: str, size: int = 2, coverage=(0, 0, 8, 8),
                     bid="B1", when="2026-01-01T00:00:00+00:00") -> dict:
    return {"batch_id": bid, "acquired_at": when, "cell_size": size,
            "coverage": {"x0": coverage[0], "y0": coverage[1],
                         "x1": coverage[2], "y1": coverage[3]},
            "source_file": os.path.basename(path)}


class ValidationRejectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()

    def _build(self, batches):
        cfg = write_config(self.tmp, batches)
        return build_package(cfg, os.path.join(self.tmp, "out.mosaic"))

    def test_duplicate_physical_cell_rejected(self):
        path = write_fixture(self.tmp, "b1.jsonl", [
            {"cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}, "reflectance": 0.1, "quality": 1},
            {"cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}, "reflectance": 0.2, "quality": 2},
        ])
        with self.assertRaises(InputError) as ctx:
            self._build([base_batch_entry(path)])
        self.assertIn("重复定义", str(ctx.exception))

    def test_out_of_coverage_rejected(self):
        path = write_fixture(self.tmp, "b1.jsonl", [
            {"cell": {"x0": 6, "y0": 6, "x1": 8, "y1": 8}, "reflectance": 0.1, "quality": 1},
        ])
        with self.assertRaises(InputError) as ctx:
            self._build([base_batch_entry(path, coverage=(0, 0, 8, 4))])
        self.assertIn("越出", str(ctx.exception))

    def test_misaligned_cell_rejected(self):
        path = write_fixture(self.tmp, "b1.jsonl", [
            {"cell": {"x0": 1, "y0": 0, "x1": 3, "y1": 2}, "reflectance": 0.1, "quality": 1},
        ])
        with self.assertRaises(InputError) as ctx:
            self._build([base_batch_entry(path, size=2)])
        self.assertIn("对齐", str(ctx.exception))

    def test_cell_size_mismatch_rejected(self):
        path = write_fixture(self.tmp, "b1.jsonl", [
            {"cell": {"x0": 0, "y0": 0, "x1": 4, "y1": 4}, "reflectance": 0.1, "quality": 1},
        ])
        with self.assertRaises(InputError) as ctx:
            self._build([base_batch_entry(path, size=2)])
        self.assertIn("cell_size", str(ctx.exception))

    def test_non_power_of_two_batch_rejected(self):
        path = write_fixture(self.tmp, "b1.jsonl", [])
        entry = base_batch_entry(path, size=6)
        with self.assertRaises(InputError) as ctx:
            self._build([entry])
        self.assertIn("2 的非负整数次幂", str(ctx.exception))

    def test_value_domain_out_of_range_becomes_unavailable_not_rejected(self):
        # 反射值越出认证值域且未声明状态 => 归入缺通道（不可用），构建不拒收
        path = write_fixture(self.tmp, "b1.jsonl", [
            {"cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}, "reflectance": 9.9, "quality": 1},
            {"cell": {"x0": 2, "y0": 0, "x1": 4, "y1": 2}, "reflectance": 0.5},  # 缺质量值
        ])
        out = self._build([base_batch_entry(path)])
        self.assertEqual(out["stats"]["missing_channel"], 2)
        self.assertEqual(out["stats"]["valid"], 0)

    def test_cloud_and_missing_channel_records_kept(self):
        path = write_fixture(self.tmp, "b1.jsonl", [
            {"cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}, "state": "cloud"},
            {"cell": {"x0": 2, "y0": 0, "x1": 4, "y1": 2}, "state": "missing_channel"},
            {"cell": {"x0": 4, "y0": 0, "x1": 6, "y1": 2}, "reflectance": 0.4, "quality": 4},
        ])
        out = self._build([base_batch_entry(path)])
        self.assertEqual(out["stats"]["valid"], 1)
        self.assertEqual(out["stats"]["cloud"], 1)
        self.assertEqual(out["stats"]["missing_channel"], 1)
        self.assertEqual(out["stats"]["by_cell_size"], {2: 3})

    def test_duplicate_batch_id_rejected(self):
        p1 = write_fixture(self.tmp, "b1.jsonl", [])
        p2 = write_fixture(self.tmp, "b2.jsonl", [])
        e1 = base_batch_entry(p1, bid="X")
        e2 = base_batch_entry(p2, bid="X")
        with self.assertRaises(InputError) as ctx:
            self._build([e1, e2])
        self.assertIn("批次号重复", str(ctx.exception))

    def test_missing_source_file_rejected(self):
        entry = base_batch_entry(os.path.join(self.tmp, "absent.jsonl"))
        with self.assertRaises(InputError) as ctx:
            self._build([entry])
        self.assertIn("无法打开", str(ctx.exception))

    def test_empty_source_file_is_valid_declared_page_gap(self):
        # 文件存在但为空（缺页）：构建成功，该批次零记录，提取时归为声明内缺记录
        path = write_fixture(self.tmp, "empty.jsonl", [])
        out_dir = os.path.join(self.tmp, "out.mosaic")
        summary = self._build([base_batch_entry(path)])
        self.assertEqual(summary["record_count"], 0)
        pkg = load_package(out_dir)
        from agrimosaic.extract import extract_cell
        out = extract_cell((0, 0, 2, 2), pkg)
        self.assertEqual(out["gaps"][0]["reason"], "declared_no_record")

    def test_naive_datetime_rejected(self):
        path = write_fixture(self.tmp, "b1.jsonl", [])
        entry = base_batch_entry(path, when="2026-01-01T00:00:00")
        with self.assertRaises(InputError):
            self._build([entry])

    def test_no_half_baked_output_on_rejection(self):
        path = write_fixture(self.tmp, "b1.jsonl", [
            {"cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}, "reflectance": 0.1, "quality": 1},
            {"cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}, "reflectance": 0.2, "quality": 2},
        ])
        out_dir = os.path.join(self.tmp, "never.mosaic")
        cfg = write_config(self.tmp, [base_batch_entry(path)])
        with self.assertRaises(InputError):
            build_package(cfg, out_dir)
        self.assertFalse(os.path.exists(out_dir))


class CliEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        self.repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _run(self, *args):
        # 隔离模式下 PYTHONPATH 被忽略；直接运行包内 __main__.py（其内部自举仓库根）
        main_py = os.path.join(self.repo, "agrimosaic", "__main__.py")
        return subprocess.run(
            [sys.executable, "-I", main_py, *args],
            cwd=self.tmp, capture_output=True, text=True,
        )

    def test_build_and_extract_roundtrip(self):
        b1 = write_fixture(self.tmp, "b1.jsonl", [
            {"cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2},
             "reflectance": 0.3, "quality": 30},
            {"cell": {"x0": 2, "y0": 0, "x1": 4, "y1": 2}, "state": "cloud"},
        ])
        b2 = write_fixture(self.tmp, "b2.jsonl", [
            {"cell": {"x0": 0, "y0": 0, "x1": 4, "y1": 4},
             "reflectance": 0.8, "quality": 80},
        ])
        cfg = write_config(self.tmp, [
            base_batch_entry(b1, size=2, bid="B1", when="2026-01-01T00:00:00+00:00"),
            base_batch_entry(b2, size=4, coverage=(0, 0, 8, 8), bid="B2",
                             when="2026-02-01T00:00:00+00:00"),
        ])
        out_dir = os.path.join(self.tmp, "pkg.mosaic")
        r = self._run("build", "--config", cfg, "--out", out_dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "meta.json")))

        result_path = os.path.join(self.tmp, "result.json")
        r = self._run("extract", "--package", out_dir,
                      "--x0", "0", "--y0", "0", "--x1", "4", "--y1", "4",
                      "--target-size", "2", "--output", result_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        result = json.load(open(result_path, encoding="utf-8"))
        self.assertEqual(result["cell_count"], 4)
        # T(0,0)：B1 有效细格占整格 -> B1 均质原始值
        first = result["cells"][0]
        self.assertEqual(first["status"], "homogeneous")
        self.assertEqual(first["value"], {"reflectance": 0.3, "quality": 30})
        # T(1,0)：B1 云遮，B2 有效粗格 -> B2
        second = result["cells"][1]
        self.assertEqual(second["provenance"]["batch_id"], "B2")

    def test_cli_rejects_bad_input_with_exit_2(self):
        b1 = write_fixture(self.tmp, "b1.jsonl", [
            {"cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}, "reflectance": 0.1, "quality": 1},
            {"cell": {"x0": 0, "y0": 0, "x1": 2, "y1": 2}, "reflectance": 0.2, "quality": 2},
        ])
        cfg = write_config(self.tmp, [base_batch_entry(b1)])
        r = self._run("build", "--config", cfg,
                      "--out", os.path.join(self.tmp, "x.mosaic"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("拒收", r.stderr)


if __name__ == "__main__":
    unittest.main()
