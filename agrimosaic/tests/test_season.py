"""季末复测资格台账作业的测试。

覆盖：
* 小地块上低值位置在两轮之间移位、缺轮、质量门槛变化时的面积与首次确认日期；
* 未知轮次打断拼接、允许重新开始、已确认面积只记一次；
* 门槛精确比较（整数交叉相乘，门槛处不换档）；
* 极大地块上只有零星细格修正时总数精确、片段数与物理面积无关；
* 地块边界切过源格、地块互不影响、清单非法 / 包不可读即失败；
* CLI 端到端（含退出码与输入包不被修改）。
"""

import json
import os
import subprocess
import sys
import unittest

from agrimosaic.model import InputError
from agrimosaic.season import (
    QUAL_EXCLUDED, QUAL_PENDING, QUAL_RETEST,
    ROUND_LOW, ROUND_NOT_LOW, ROUND_UNKNOWN,
    build_ledger, classify_plot_round, evaluate_vector, overlay_round,
    load_manifest_packages, parse_manifest,
)
from agrimosaic.store import build_package
from tests.helpers import DOMAIN, new_tmp

D1, D2, D3, D4, D5 = (
    "2026-05-01", "2026-05-08", "2026-05-15", "2026-05-20", "2026-05-22")


def cell(x, y, size, refl=0.9, qual=50, state=None):
    row = {"cell": {"x0": x, "y0": y, "x1": x + size, "y1": y + size}}
    if state:
        row["state"] = state
    else:
        row["reflectance"] = refl
        row["quality"] = qual
    return row


def write_pkg(tmp, name, batches, coverage, when=None, cell_size=None):
    """在 tmp 下构建一个真实镶嵌包，返回包目录绝对路径。

    batches 为 [(batch_id, cell_size, rows), ...]；不同分辨率必须分属不同批次，
    与既有 build 数据契约一致。单批次时可直接传 rows 并给 coverage/when/cell_size。
    """
    if batches and isinstance(batches[0], dict):
        batches = [(name, cell_size, batches)]
        coverage_list = [coverage]
        when_list = [when]
    else:
        coverage_list = None
        when_list = None
    cfg_batches = []
    for i, (bid, size, rows) in enumerate(batches):
        jsonl = os.path.join(tmp, f"{name}_{bid}.jsonl")
        with open(jsonl, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        cov = coverage if coverage_list is not None else coverage
        cfg_batches.append({
            "batch_id": bid,
            "acquired_at": (when or f"2026-01-{i + 1:02d}") + "T00:00:00+00:00",
            "cell_size": size,
            "coverage": {"x0": cov[0], "y0": cov[1], "x1": cov[2], "y1": cov[3]},
            "source_file": os.path.basename(jsonl),
        })
    cfg = {
        "value_domain": {"reflectance_min": DOMAIN.reflectance_min,
                         "reflectance_max": DOMAIN.reflectance_max,
                         "quality_min": DOMAIN.quality_min,
                         "quality_max": DOMAIN.quality_max},
        "batches": cfg_batches,
    }
    cfg_path = os.path.join(tmp, name + "_config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False)
    out = os.path.join(tmp, name + ".mosaic")
    build_package(cfg_path, out)
    return out


def grid4_rows(low_cells, size=2, refl_low=0.1, refl_high=0.9, qual=50):
    """4x4 覆盖的 size 格网：low_cells 给出左下角为低值的格坐标。"""
    rows = []
    for y in range(0, 4, size):
        for x in range(0, 4, size):
            refl = refl_low if (x, y) in low_cells else refl_high
            rows.append(cell(x, y, size, refl=refl, qual=qual))
    return rows


def write_manifest(tmp, plots, rounds, cap=0.3, min_quality=10,
                   k=2, d=0, threshold=2500):
    obj = {
        "plots": [{"plot_id": pid,
                   "bounds": {"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]}}
                  for pid, b in plots],
        "rounds": [{"date": dstr, "package": pkg} for dstr, pkg in rounds],
        "criteria": {
            "reflectance_cap": cap, "min_quality": min_quality,
            "min_consecutive_rounds": k, "min_span_days": d,
            "area_threshold_bp": threshold,
        },
    }
    path = os.path.join(tmp, "season_manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    return path


def run_ledger(tmp, plots, rounds, **kw):
    path = write_manifest(tmp, plots, rounds, **kw)
    manifest = parse_manifest(path)
    packages = load_manifest_packages(manifest)
    return build_ledger(manifest, packages), manifest, packages


def by_id(ledger):
    return {p["plot_id"]: p for p in ledger["plots"]}


class ShiftAndMissingRoundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        # 三轮 size2 格网全覆盖 4x4；低值格在轮间移位
        self.p1 = write_pkg(self.tmp, "r1", grid4_rows({(0, 0)}),
                            (0, 0, 4, 4), "2026-01-01", 2)
        self.p2 = write_pkg(self.tmp, "r2", grid4_rows({(2, 2)}),
                            (0, 0, 4, 4), "2026-02-01", 2)
        self.p3 = write_pkg(self.tmp, "r3", grid4_rows({(2, 2)}),
                            (0, 0, 4, 4), "2026-03-01", 2)
        self.plot = ("P", (0, 0, 4, 4))

    def test_low_region_shifts_between_rounds(self):
        # A=(0,0,2,2) 仅首轮低；B=(2,2,4,4) 后两轮低 -> 只有 B 在第三轮首次确认
        ledger, _, _ = run_ledger(
            self.tmp, [self.plot],
            [(D1, self.p1), (D2, self.p2), (D3, self.p3)],
            k=2, d=0, threshold=2500)
        entry = by_id(ledger)["P"]
        self.assertEqual(entry["total_area"], 16)
        self.assertEqual(entry["confirmed_area"], 4)
        self.assertEqual(entry["possible_area_upper_bound"], 4)
        self.assertEqual(entry["first_confirmed_area_by_date"], {D3: 4})
        # 4/16 恰为 2500bp，门槛处精确比较 -> 列入复测
        self.assertEqual(entry["qualification"], QUAL_RETEST)

    def test_same_region_two_rounds_confirms_on_second_round(self):
        p2b = write_pkg(self.tmp, "r2b", grid4_rows({(0, 0)}),
                        (0, 0, 4, 4), "2026-02-02", 2)
        p3b = write_pkg(self.tmp, "r3b", grid4_rows(set()),
                        (0, 0, 4, 4), "2026-03-02", 2)
        ledger, _, _ = run_ledger(
            self.tmp, [self.plot],
            [(D1, self.p1), (D2, p2b), (D3, p3b)],
            k=2, d=0, threshold=3000)  # 25% 确认但门槛 30% -> 不列入
        entry = by_id(ledger)["P"]
        self.assertEqual(entry["confirmed_area"], 4)
        self.assertEqual(entry["first_confirmed_area_by_date"], {D2: 4})
        self.assertEqual(entry["qualification"], QUAL_EXCLUDED)

    def test_missing_round_breaks_stitch_but_feeds_upper_bound(self):
        # A 首轮与第三轮低，中间缺包：不能跨未知拼接 -> 无确认；
        # 未知乐观视为低值后可连成三段 -> 上界含该面积，资格待定
        p3a = write_pkg(self.tmp, "r3a", grid4_rows({(0, 0)}),
                        (0, 0, 4, 4), "2026-03-03", 2)
        ledger, _, _ = run_ledger(
            self.tmp, [self.plot],
            [(D1, self.p1), (D2, None), (D3, p3a)],
            k=2, d=0, threshold=2500)
        entry = by_id(ledger)["P"]
        self.assertEqual(entry["confirmed_area"], 0)
        self.assertEqual(entry["possible_area_upper_bound"], 4)
        self.assertEqual(entry["first_confirmed_area_by_date"], {})
        self.assertEqual(entry["qualification"], QUAL_PENDING)
        self.assertEqual(ledger["rounds"][1]["observed"], False)

    def test_unknown_then_restart_and_no_double_booking(self):
        # A：低、未知、低、低 -> 未知打断，第三/四轮重新成段，只在第四轮记一次
        p3a = write_pkg(self.tmp, "r3c", grid4_rows({(0, 0)}),
                        (0, 0, 4, 4), "2026-03-04", 2)
        p4 = write_pkg(self.tmp, "r4", grid4_rows({(0, 0)}),
                       (0, 0, 4, 4), "2026-04-04", 2)
        ledger, _, _ = run_ledger(
            self.tmp, [self.plot],
            [(D1, self.p1), (D2, None), (D3, p3a), (D4, p4)],
            k=2, d=0, threshold=1)
        entry = by_id(ledger)["P"]
        self.assertEqual(entry["confirmed_area"], 4)
        self.assertEqual(entry["first_confirmed_area_by_date"], {D4: 4})

    def test_span_days_gate_moves_first_confirmation(self):
        # A 连续三轮低：D=10 时 7 天的两段都不够，直到第三轮跨 14 天才确认
        p2a = write_pkg(self.tmp, "r2d", grid4_rows({(0, 0)}),
                        (0, 0, 4, 4), "2026-02-05", 2)
        p3a = write_pkg(self.tmp, "r3d", grid4_rows({(0, 0)}),
                        (0, 0, 4, 4), "2026-03-06", 2)
        rounds = [(D1, self.p1), (D2, p2a), (D3, p3a)]
        ledger, _, _ = run_ledger(self.tmp, [self.plot], rounds, k=2, d=10)
        entry = by_id(ledger)["P"]
        self.assertEqual(entry["confirmed_area"], 4)
        self.assertEqual(entry["first_confirmed_area_by_date"], {D3: 4})
        # 同样数据 D=0 时第二轮即首次确认
        ledger0, _, _ = run_ledger(self.tmp, [self.plot], rounds, k=2, d=0)
        self.assertEqual(by_id(ledger0)["P"]["first_confirmed_area_by_date"],
                         {D2: 4})

    def test_repeated_qualification_booked_once_at_first_date(self):
        # 四轮全低、K=2：第二轮首次确认；第三、四轮再次达标不得重复记账
        p_all = write_pkg(self.tmp, "all", grid4_rows(
            {(0, 0), (2, 0), (0, 2), (2, 2)}),
            (0, 0, 4, 4), "2026-01-01", 2)
        ledger, _, _ = run_ledger(
            self.tmp, [self.plot],
            [(D1, p_all), (D2, p_all), (D3, p_all), (D4, p_all)],
            k=2, d=0, threshold=1)
        entry = by_id(ledger)["P"]
        self.assertEqual(entry["confirmed_area"], 16)
        self.assertEqual(entry["possible_area_upper_bound"], 16)
        self.assertEqual(entry["first_confirmed_area_by_date"], {D2: 16})
        # K 大于轮数：任何面积都无法确认，上界也为 0
        ledger_k, _, _ = run_ledger(
            self.tmp, [self.plot], [(D1, p_all), (D2, p_all)],
            k=3, d=0, threshold=1)
        ek = by_id(ledger_k)["P"]
        self.assertEqual(ek["confirmed_area"], 0)
        self.assertEqual(ek["possible_area_upper_bound"], 0)
        self.assertEqual(ek["qualification"], QUAL_EXCLUDED)

    def test_all_rounds_missing_is_pending_or_excluded_by_span(self):
        rounds = [(D1, None), (D2, None), (D3, None)]
        # D=0：全部未知，乐观可连段 -> 上界为整块地
        ledger, _, _ = run_ledger(self.tmp, [self.plot], rounds,
                                  k=2, d=0, threshold=5000)
        entry = by_id(ledger)["P"]
        self.assertEqual(entry["confirmed_area"], 0)
        self.assertEqual(entry["possible_area_upper_bound"], 16)
        self.assertEqual(entry["qualification"], QUAL_PENDING)
        # D=30 超出整个巡测季跨度（14 天），乐观也无法达标 -> 不列入
        ledger_d, _, _ = run_ledger(self.tmp, [self.plot], rounds,
                                    k=2, d=30, threshold=1)
        self.assertEqual(by_id(ledger_d)["P"]["possible_area_upper_bound"], 0)
        self.assertEqual(by_id(ledger_d)["P"]["qualification"], QUAL_EXCLUDED)


class QualityGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        # 细格 F(0,0,1,1) 高反射高质量；粗格 C(0,0,2,2) 低反射低质量
        # 不同分辨率分属不同批次：F 更细，在 1 单元上必然胜出，不能被改选
        batches = [
            ("F", 1, [cell(0, 0, 1, refl=0.9, qual=90)]),
            ("C", 2, [cell(0, 0, 2, refl=0.1, qual=20)]),
        ]
        self.pkg = write_pkg(self.tmp, "q", batches, (0, 0, 2, 2))
        self.plot = ("P", (0, 0, 2, 2))

    def _partition_map(self, min_quality, cap):
        manifest = parse_manifest(write_manifest(
            self.tmp, [self.plot], [(D1, self.pkg)],
            cap=cap, min_quality=min_quality, k=1, d=0))
        packages = load_manifest_packages(manifest)
        parts = classify_plot_round(packages[0], (0, 0, 2, 2), cap, min_quality)
        totals = {ROUND_LOW: 0, ROUND_NOT_LOW: 0, ROUND_UNKNOWN: 0}
        for r, lab in parts:
            totals[lab] += (r[2] - r[0]) * (r[3] - r[1])
        return totals

    def test_winner_quality_below_gate_is_unknown_without_fallback(self):
        # 门槛 50：粗赢家质量 20 -> 3 单元未知；细赢家高反射 -> 1 单元非低值
        # 绝不能改选排名靠后的源格（细格位置不回头取粗格、粗格位置也无人可改选）
        areas = self._partition_map(50, 0.3)
        self.assertEqual(areas, {ROUND_LOW: 0, ROUND_NOT_LOW: 1,
                                 ROUND_UNKNOWN: 3})
        ledger, _, _ = run_ledger(
            self.tmp, [self.plot], [(D1, self.pkg), (D2, self.pkg)],
            cap=0.3, min_quality=50, k=2, d=0, threshold=5000)
        entry = by_id(ledger)["P"]
        self.assertEqual(entry["confirmed_area"], 0)
        self.assertEqual(entry["possible_area_upper_bound"], 3)
        self.assertEqual(entry["qualification"], QUAL_PENDING)

    def test_lowering_quality_gate_confirms_coarse_area(self):
        ledger, _, _ = run_ledger(
            self.tmp, [self.plot], [(D1, self.pkg), (D2, self.pkg)],
            cap=0.3, min_quality=10, k=2, d=0, threshold=7500)
        entry = by_id(ledger)["P"]
        # 粗格 3 单元两轮低 -> 确认；细格 1 单元高反射，始终不是低值
        self.assertEqual(entry["confirmed_area"], 3)
        self.assertEqual(entry["possible_area_upper_bound"], 3)
        # 3/4 = 7500bp，恰好等于门槛 -> 复测；门槛加 1bp 即不列入
        self.assertEqual(entry["confirmed_bp"], 7500)
        self.assertEqual(entry["qualification"], QUAL_RETEST)
        ledger_hi, _, _ = run_ledger(
            self.tmp, [self.plot], [(D1, self.pkg), (D2, self.pkg)],
            cap=0.3, min_quality=10, k=2, d=0, threshold=7501)
        self.assertEqual(by_id(ledger_hi)["P"]["qualification"], QUAL_EXCLUDED)

    def test_raising_cap_turns_winner_into_low(self):
        ledger, _, _ = run_ledger(
            self.tmp, [self.plot], [(D1, self.pkg), (D2, self.pkg)],
            cap=0.95, min_quality=10, k=2, d=0, threshold=10000)
        entry = by_id(ledger)["P"]
        self.assertEqual(entry["confirmed_area"], 4)  # 细格 0.9 <= 0.95 也成低值
        self.assertEqual(entry["qualification"], QUAL_RETEST)


class ThresholdExactnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        # 6 单元地块 (0,0,2,3)，size1 全覆盖：一个低格、五个高格，两轮相同，
        # 确认面积与上界恒为 1 单元（不存在未知观测）
        rows = [cell(0, 0, 1, refl=0.1)] + [
            cell(x, y, 1, refl=0.9)
            for y in range(3) for x in range(2) if (x, y) != (0, 0)]
        self.pkg = write_pkg(self.tmp, "t", rows, (0, 0, 2, 3),
                             "2026-01-01", 1)
        self.plot = ("P", (0, 0, 2, 3))  # 面积 6，确认 1 单元 = 1666.67bp

    def test_no_rounding_shift_at_threshold(self):
        rounds = [(D1, self.pkg), (D2, self.pkg)]
        # 该地块没有未知观测，上界=下界=1 单元，占比 1666.67bp（floor 显示 1666）
        # 门槛 1666bp：精确 10000 >= 9996 -> 达标复测
        ok, _, _ = run_ledger(self.tmp, [self.plot], rounds,
                              k=2, d=0, threshold=1666)
        self.assertEqual(by_id(ok)["P"]["qualification"], QUAL_RETEST)
        # 门槛 1667bp：精确 10000 < 10002，且上界同样不达标 -> 不列入，
        # 不会因为 floor 占比显示成 1666 或任何四舍五入而换档
        no, _, _ = run_ledger(self.tmp, [self.plot], rounds,
                              k=2, d=0, threshold=1667)
        entry = by_id(no)["P"]
        self.assertEqual(entry["qualification"], QUAL_EXCLUDED)
        self.assertEqual(entry["confirmed_bp"], 1666)
        self.assertEqual(entry["possible_bp_upper_bound"], 1666)

    def test_exact_equality_qualifies(self):
        # 4/16 = 2500bp 整：等于门槛即复测
        pkg = write_pkg(self.tmp, "e", grid4_rows({(0, 0)}),
                        (0, 0, 4, 4), "2026-01-01", 2)
        ledger, _, _ = run_ledger(
            self.tmp, [("P", (0, 0, 4, 4))],
            [(D1, pkg), (D2, pkg)], k=2, d=0, threshold=2500)
        self.assertEqual(by_id(ledger)["P"]["qualification"], QUAL_RETEST)
        ledger2, _, _ = run_ledger(
            self.tmp, [("P", (0, 0, 4, 4))],
            [(D1, pkg), (D2, pkg)], k=2, d=0, threshold=2501)
        self.assertEqual(by_id(ledger2)["P"]["qualification"], QUAL_EXCLUDED)


class HugePlotSparseCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        self.huge = 2 ** 20

    def _pkgs(self, fine_low):
        h = self.huge
        refl_fine = 0.1 if fine_low else 0.9
        r1 = write_pkg(self.tmp, "h1", [
            ("COARSE", h, [cell(0, 0, h, refl=0.1, qual=50)]),
            ("FINE", 1, [cell(1, 1, 1, refl=refl_fine, qual=90)]),
        ], (0, 0, h, h))
        r2 = write_pkg(self.tmp, "h2", [
            ("COARSE", h, [cell(0, 0, h, refl=0.1, qual=50)]),
            ("FINE", 1, [cell(3, 3, 1, refl=refl_fine, qual=90)]),
        ], (0, 0, h, h))
        return r1, r2

    def test_exact_totals_with_few_fine_cells_on_huge_plot(self):
        r1, r2 = self._pkgs(fine_low=False)
        h = self.huge
        ledger, manifest, packages = run_ledger(
            self.tmp, [("BIG", (0, 0, h, h))],
            [(D1, r1), (D2, r2)], k=2, d=0, threshold=1)
        entry = by_id(ledger)["BIG"]
        # 两个互不相交的细格各只在一轮高反射，其余 h^2-2 单元两轮都是粗格低值
        self.assertEqual(entry["total_area"], h * h)
        self.assertEqual(entry["confirmed_area"], h * h - 2)
        self.assertEqual(entry["possible_area_upper_bound"], h * h - 2)
        self.assertEqual(entry["first_confirmed_area_by_date"], {D2: h * h - 2})
        self.assertEqual(entry["qualification"], QUAL_RETEST)

        # 直接检查叠加层：原子数是小常数，与 h 无关，且面积严格对平。
        # 两条细格边线真实交错产生至多 5x5=25 个组合区；若把全部横纵边线
        # 拉通成全笛卡尔积，单轮就是 169 个格子——这里明确不允许那种膨胀。
        layer = [((0, 0, h, h), ())]
        for pkg in packages:
            partition = classify_plot_round(pkg, (0, 0, h, h), 0.3, 10)
            layer = overlay_round(layer, partition)
        self.assertLessEqual(len(layer), 25)
        self.assertLess(len(layer), 13 ** 2)
        self.assertEqual(sum((r[2] - r[0]) * (r[3] - r[1]) for r, _ in layer),
                         h * h)

    def test_layer_size_independent_of_coarse_cell_area(self):
        # 同样数量的细格修正，把粗格边长从 2^10 提到 2^20，叠加原子数不变
        counts = []
        for hh in (2 ** 10, 2 ** 15, 2 ** 20):
            p1 = write_pkg(self.tmp, f"s1_{hh}", [
                ("COARSE", hh, [cell(0, 0, hh, refl=0.1)]),
                ("FINE", 1, [cell(1, 1, 1, refl=0.9)]),
            ], (0, 0, hh, hh))
            p2 = write_pkg(self.tmp, f"s2_{hh}", [
                ("COARSE", hh, [cell(0, 0, hh, refl=0.1)]),
                ("FINE", 1, [cell(3, 3, 1, refl=0.9)]),
            ], (0, 0, hh, hh))
            ledger, manifest, packages = run_ledger(
                self.tmp, [("B", (0, 0, hh, hh))],
                [(D1, p1), (D2, p2)], k=2, d=0, threshold=1)
            self.assertEqual(by_id(ledger)["B"]["confirmed_area"], hh * hh - 2)
            layer = [((0, 0, hh, hh), ())]
            for pkg in packages:
                partition = classify_plot_round(pkg, (0, 0, hh, hh), 0.3, 10)
                layer = overlay_round(layer, partition)
            counts.append(len(layer))
        self.assertEqual(len(set(counts)), 1)  # 片段数与物理面积脱钩

    def test_fine_low_islands_are_the_only_confirmed_area(self):
        # 反过来：粗格全部高反射，同一细格位置两轮都低 -> 只有 1 单元确认
        h = self.huge
        r1 = write_pkg(self.tmp, "i1", [
            ("COARSE", h, [cell(0, 0, h, refl=0.9, qual=50)]),
            ("FINE", 1, [cell(1, 1, 1, refl=0.1, qual=90)]),
        ], (0, 0, h, h))
        r2 = write_pkg(self.tmp, "i2", [
            ("COARSE", h, [cell(0, 0, h, refl=0.9, qual=50)]),
            ("FINE", 1, [cell(1, 1, 1, refl=0.1, qual=90)]),
        ], (0, 0, h, h))
        ledger, _, _ = run_ledger(
            self.tmp, [("BIG", (0, 0, h, h))],
            [(D1, r1), (D2, r2)], k=2, d=0, threshold=1)
        entry = by_id(ledger)["BIG"]
        self.assertEqual(entry["confirmed_area"], 1)
        self.assertEqual(entry["total_area"], h * h)
        self.assertEqual(entry["first_confirmed_area_by_date"], {D2: 1})


class GeometryAndIndependenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        # size2 格网覆盖 8x8，全部低值
        rows = [cell(x, y, 2, refl=0.1) for y in range(0, 8, 2)
                for x in range(0, 8, 2)]
        self.pkg = write_pkg(self.tmp, "g", rows, (0, 0, 8, 8),
                             "2026-01-01", 2)

    def test_plot_bounds_cut_across_source_cells(self):
        # 地块 (1,1,5,5) 边界切过 size2 源格，面积仍精确为 16
        ledger, _, _ = run_ledger(
            self.tmp, [("CUT", (1, 1, 5, 5))],
            [(D1, self.pkg), (D2, self.pkg)], k=2, d=0, threshold=10000)
        entry = by_id(ledger)["CUT"]
        self.assertEqual(entry["total_area"], 16)
        self.assertEqual(entry["confirmed_area"], 16)
        self.assertEqual(entry["qualification"], QUAL_RETEST)

    def test_overlapping_plots_are_scored_independently(self):
        ledger, _, _ = run_ledger(
            self.tmp,
            [("A", (0, 0, 2, 2)), ("B", (0, 0, 4, 4))],  # 允许重叠，各算各的
            [(D1, self.pkg), (D2, self.pkg)], k=2, d=0, threshold=1)
        a, b = by_id(ledger)["A"], by_id(ledger)["B"]
        self.assertEqual(a["total_area"], 4)
        self.assertEqual(b["total_area"], 16)
        self.assertEqual(a["confirmed_area"], 4)
        self.assertEqual(b["confirmed_area"], 16)


class VectorEvaluationTest(unittest.TestCase):
    def test_unknown_resets_and_possible_treats_unknown_as_low(self):
        import datetime as _dt
        dates = [_dt.date(2026, 5, d) for d in (1, 8, 15, 22)]
        # 低 未知 低 低：真实确认在第 4 轮（下标 3）
        first, possible = evaluate_vector(
            (ROUND_LOW, ROUND_UNKNOWN, ROUND_LOW, ROUND_LOW), dates, 2, 0)
        self.assertEqual(first, 3)
        self.assertTrue(possible)
        # 低 未知 低：真实不确认；乐观把未知当低 -> 可确认
        first, possible = evaluate_vector(
            (ROUND_LOW, ROUND_UNKNOWN, ROUND_LOW), dates, 2, 0)
        self.assertIsNone(first)
        self.assertTrue(possible)
        # 高值即便在乐观情形下也打断
        first, possible = evaluate_vector(
            (ROUND_LOW, ROUND_NOT_LOW, ROUND_LOW), dates, 2, 0)
        self.assertIsNone(first)
        self.assertFalse(possible)
        # K=1 且 D=0：单个低值即确认，首次记首轮
        first, _ = evaluate_vector((ROUND_UNKNOWN, ROUND_LOW), dates[:2], 1, 0)
        self.assertEqual(first, 1)


class ManifestValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        self.good_plots = [("P", (0, 0, 2, 2))]
        self.good_rounds = [(D1, None)]

    def _expect_reject(self, **kw):
        path = write_manifest(self.tmp, **kw)
        with self.assertRaises(InputError):
            parse_manifest(path)

    def test_duplicate_plot_id_rejected(self):
        self._expect_reject(
            plots=[("P", (0, 0, 2, 2)), ("P", (2, 2, 4, 4))],
            rounds=self.good_rounds)

    def test_bad_rect_rejected(self):
        self._expect_reject(plots=[("P", (2, 0, 2, 4))], rounds=self.good_rounds)
        self._expect_reject(plots=[("P", (-1, 0, 2, 4))], rounds=self.good_rounds)

    def test_dates_must_be_strictly_increasing(self):
        self._expect_reject(plots=self.good_plots,
                            rounds=[(D2, None), (D1, None)])
        self._expect_reject(plots=self.good_plots,
                            rounds=[(D1, None), (D1, None)])

    def test_bad_date_and_package_rejected(self):
        path = os.path.join(self.tmp, "m.json")
        base = json.load(open(write_manifest(
            self.tmp, self.good_plots, self.good_rounds), encoding="utf-8"))
        for mutated in (
            lambda o: o["rounds"][0].__setitem__("date", "2026-5-1"),
            lambda o: o["rounds"][0].__setitem__("date", "not-a-date"),
            lambda o: o["rounds"][0].__setitem__("package", ""),
            lambda o: o["rounds"][0].__setitem__("package", 123),
            lambda o: o["criteria"].__setitem__("min_consecutive_rounds", 0),
            lambda o: o["criteria"].__setitem__("min_span_days", -1),
            lambda o: o["criteria"].__setitem__("area_threshold_bp", 0),
            lambda o: o["criteria"].__setitem__("area_threshold_bp", 10001),
            lambda o: o["criteria"].__setitem__("reflectance_cap", "x"),
            lambda o: o["criteria"].__setitem__("reflectance_cap", float("inf")),
            lambda o: o["criteria"].__setitem__("min_quality", True),
            lambda o: o.__setitem__("plots", []),
            lambda o: o.__setitem__("rounds", []),
        ):
            obj = json.loads(json.dumps(base))
            mutated(obj)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(obj, fh)
            with self.assertRaises(InputError):
                parse_manifest(path)

    def test_missing_manifest_file_rejected(self):
        with self.assertRaises(InputError):
            parse_manifest(os.path.join(self.tmp, "absent.json"))

    def test_unreadable_non_null_package_fails_the_job(self):
        # 不存在的包路径：必须失败，不能当成一次未观测继续出表
        path = write_manifest(
            self.tmp, self.good_plots,
            [(D1, os.path.join(self.tmp, "ghost.mosaic"))])
        with self.assertRaises(InputError):
            load_manifest_packages(parse_manifest(path))
        # 存在目录但缺少 meta.json 同样失败
        broken = os.path.join(self.tmp, "broken.mosaic")
        os.makedirs(broken)
        path2 = write_manifest(self.tmp, self.good_plots, [(D1, broken)])
        with self.assertRaises(InputError):
            load_manifest_packages(parse_manifest(path2))
        # 对照：null 轮次合法
        ok = write_manifest(self.tmp, self.good_plots, [(D1, None)])
        self.assertEqual(load_manifest_packages(parse_manifest(ok)), [None])


class SeasonCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmp()
        self.repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.main_py = os.path.join(self.repo, "agrimosaic", "__main__.py")
        self.pkg = write_pkg(self.tmp, "c", grid4_rows({(0, 0)}),
                             (0, 0, 4, 4), "2026-01-01", 2)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-I", self.main_py, *args],
            cwd=self.tmp, capture_output=True, text=True)

    def test_cli_season_roundtrip_and_input_packages_untouched(self):
        manifest = write_manifest(
            self.tmp, [("P", (0, 0, 4, 4))],
            [(D1, self.pkg), (D2, self.pkg)], k=2, d=0, threshold=2500)
        before = open(os.path.join(self.pkg, "records.jsonl"), "rb").read()
        out = os.path.join(self.tmp, "ledger.json")
        r = self._run("season", "--manifest", manifest, "--output", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        after = open(os.path.join(self.pkg, "records.jsonl"), "rb").read()
        self.assertEqual(before, after)  # 作业不修改输入包
        ledger = json.load(open(out, encoding="utf-8"))
        entry = by_id(ledger)["P"]
        self.assertEqual(entry["confirmed_area"], 4)
        self.assertEqual(entry["first_confirmed_area_by_date"], {D2: 4})
        self.assertEqual(entry["qualification"], QUAL_RETEST)
        self.assertEqual(ledger["job"],
                         "agrimosaic.season.retest_qualification_ledger")

    def test_cli_bad_manifest_and_bad_package_exit_2(self):
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        r = self._run("season", "--manifest", bad)
        self.assertEqual(r.returncode, 2)
        ghost = write_manifest(
            self.tmp, [("P", (0, 0, 4, 4))],
            [(D1, os.path.join(self.tmp, "ghost.mosaic"))])
        r = self._run("season", "--manifest", ghost)
        self.assertEqual(r.returncode, 2)
        self.assertIn("无法读取", r.stderr)

    def test_existing_build_and_extract_commands_unchanged(self):
        r = self._run("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("season", r.stdout)
        self.assertIn("build", r.stdout)
        self.assertIn("extract", r.stdout)


if __name__ == "__main__":
    unittest.main()
