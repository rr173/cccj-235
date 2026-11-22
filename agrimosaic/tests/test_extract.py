import unittest

from agrimosaic.extract import (
    STATUS_EMPTY, STATUS_FULLY_MIXED, STATUS_HOMOGENEOUS, STATUS_PARTIAL,
    extract_cell, extract_window, validate_window,
)
from agrimosaic.geometry import area
from agrimosaic.model import (
    GAP_DECLARED_NO_RECORD, GAP_NO_DECLARED_COVERAGE,
    GAP_UNAVAILABLE_CLOUD, GAP_UNAVAILABLE_MISSING_CHANNEL,
)
from tests.helpers import DOMAIN, make_package, rec, ts


def build_small_fixture():
    """三批共原点格网，窗口 (0,0)-(8,8)、目标格边长 2，共 16 格可逐格核对。

    B1: size1, 2026-01-01, 覆盖 [0,0,8,4)        —— 最细、最旧
    B2: size2, 2026-02-01, 覆盖 [0,0,8,8)        —— 中分辨率、较新
    B3: size4, 2026-03-01, 覆盖 [0,0,16,16)      —— 最粗、最新
    """
    records = [
        # B1 稀疏细格（仅 0<=y<2 一带）
        rec("B1", (0, 0, 1, 1), 1, refl=0.10, qual=10, when=ts(2026, 1, 1)),
        rec("B1", (1, 0, 2, 1), 1, refl=0.11, qual=11, when=ts(2026, 1, 1)),
        rec("B1", (0, 1, 1, 2), 1, refl=0.12, qual=12, when=ts(2026, 1, 1)),
        rec("B1", (1, 1, 2, 2), 1, refl=0.13, qual=13, when=ts(2026, 1, 1)),
        rec("B1", (2, 0, 3, 1), 1, state="missing_channel", when=ts(2026, 1, 1)),
        rec("B1", (3, 0, 4, 1), 1, state="cloud", when=ts(2026, 1, 1)),
        # B1 在 x=6..8,y=0..2 完全没有记录（B2/B3 有效，用于正常胜出）
        # B2 中格
        rec("B2", (0, 0, 2, 2), 2, refl=0.20, qual=20, when=ts(2026, 2, 1)),
        rec("B2", (2, 0, 4, 2), 2, refl=0.21, qual=21, when=ts(2026, 2, 1)),
        rec("B2", (4, 0, 6, 2), 2, refl=0.22, qual=22, when=ts(2026, 2, 1)),
        rec("B2", (6, 0, 8, 2), 2, refl=0.23, qual=23, when=ts(2026, 2, 1)),
        rec("B2", (0, 2, 2, 4), 2, state="cloud", when=ts(2026, 2, 1)),
        rec("B2", (4, 2, 6, 4), 2, refl=0.24, qual=24, when=ts(2026, 2, 1)),
        # B2 的 (2,2,4,4) 缺记录（声明覆盖内缺页）
        # B3 粗格
        rec("B3", (0, 0, 4, 4), 4, refl=0.30, qual=30, when=ts(2026, 3, 1)),
        rec("B3", (4, 0, 8, 4), 4, refl=0.31, qual=31, when=ts(2026, 3, 1)),
        rec("B3", (0, 4, 4, 8), 4, refl=0.32, qual=32, when=ts(2026, 3, 1)),
        # B3 的 (4,4,8,8) 缺记录
    ]
    coverages = [
        ("B1", (0, 0, 8, 4)),
        ("B2", (0, 0, 8, 8)),
        ("B3", (0, 0, 16, 16)),
    ]
    return make_package(records, coverages)


class SmallFixtureTest(unittest.TestCase):
    def setUp(self):
        self.pkg = build_small_fixture()

    def test_homogeneous_fine_beats_coarse_even_though_older(self):
        # T(0,0): B1 四个有效细格铺满 -> 更细分辨率优先，四个不同物理源格 => 完整混合
        out = extract_cell((0, 0, 2, 2), self.pkg)
        self.assertEqual(out["status"], STATUS_FULLY_MIXED)
        self.assertEqual(out["gap_area"], 0)
        self.assertEqual(len(out["winners"]), 4)
        self.assertNotIn("value", out)  # 混合不给虚构值
        self.assertEqual(sum(w["area"] for w in out["winners"]), 4)
        for w in out["winners"]:
            self.assertEqual(w["source"]["batch_id"], "B1")

    def test_homogeneous_single_source_returns_raw_pair(self):
        # T(1,0) 即格 (2,0,4,2)：B1 两个记录都不可用(缺通道+云遮)，
        # B2 整格有效 -> 唯一物理源格，给原始两值
        out = extract_cell((2, 0, 4, 2), self.pkg)
        self.assertEqual(out["status"], STATUS_HOMOGENEOUS)
        self.assertEqual(out["value"], {"reflectance": 0.21, "quality": 21})
        self.assertEqual(out["provenance"]["batch_id"], "B2")
        self.assertEqual(out["provenance"]["source_cell"]["x0"], 2)
        self.assertEqual(out["covered_area"], 4)

    def test_fine_valid_piece_inside_coarse_winner(self):
        # T(2,0) 即格 (4,0,6,2)：B2 size2 与 B3 size4 都有效，B2 更细 -> 全给 B2
        out = extract_cell((4, 0, 6, 2), self.pkg)
        self.assertEqual(out["status"], STATUS_HOMOGENEOUS)
        self.assertEqual(out["value"]["reflectance"], 0.22)
        self.assertEqual(out["winners"][0]["source"]["batch_id"], "B2")

    def test_cloud_gap_in_middle_row(self):
        # T(0,1) 即格 (0,2,2,4)：B2 云遮，B3 粗格有效。
        # B3 覆盖整格 -> 全范围有赢家 B3（云遮记录不可参与胜出，但被有效候选覆盖）
        out = extract_cell((0, 2, 2, 4), self.pkg)
        self.assertEqual(out["status"], STATUS_HOMOGENEOUS)
        self.assertEqual(out["value"]["reflectance"], 0.30)

    def test_partial_cloud_where_no_valid_backup(self):
        # 构造临时包：去掉 B3，使 (0,2,2,4) 只剩 B2 云遮 -> 全空，原因 unavailable_cloud
        records = [r for r in self.pkg.records if r.batch_id != "B3"]
        coverages = [c for c in self.pkg.coverages if c[0] != "B3"]
        pkg = make_package(records, coverages)
        out = extract_cell((0, 2, 2, 4), pkg)
        self.assertEqual(out["status"], STATUS_EMPTY)
        self.assertEqual(len(out["gaps"]), 1)
        self.assertEqual(out["gaps"][0]["reason"], GAP_UNAVAILABLE_CLOUD)
        self.assertEqual(out["gaps"][0]["area"], 4)

    def test_declared_no_record_distinct_from_no_coverage(self):
        # 格 (2,2,4,4)：B2 声明覆盖但缺记录；B3 粗格有效覆盖 -> B3 均质
        out = extract_cell((2, 2, 4, 4), self.pkg)
        self.assertEqual(out["status"], STATUS_HOMOGENEOUS)
        self.assertEqual(out["value"]["reflectance"], 0.30)
        # 去掉 B3 后：B1 未声明该 y 带、B2 声明但缺记录 -> declared_no_record 全空
        records = [r for r in self.pkg.records if r.batch_id != "B3"]
        coverages = [c for c in self.pkg.coverages if c[0] != "B3"]
        pkg = make_package(records, coverages)
        out = extract_cell((2, 2, 4, 4), pkg)
        self.assertEqual(out["status"], STATUS_EMPTY)
        self.assertEqual(out["gaps"][0]["reason"], GAP_DECLARED_NO_RECORD)

    def test_no_declared_coverage_bottom_right(self):
        # 格 (6,6,8,8)：B2 覆盖到 y=8 但缺记录；B3 缺该粗格 -> B2 声明内缺记录
        out = extract_cell((6, 6, 8, 8), self.pkg)
        self.assertEqual(out["status"], STATUS_EMPTY)
        self.assertEqual(out["gaps"][0]["reason"], GAP_DECLARED_NO_RECORD)
        # 若再去掉 B2 与 B3，则没有任何批次声明该区域（B1 覆盖只到 y=4）
        records = [r for r in self.pkg.records
                   if r.batch_id not in ("B2", "B3")]
        coverages = [c for c in self.pkg.coverages if c[0] not in ("B2", "B3")]
        pkg = make_package(records, coverages)
        out = extract_cell((6, 6, 8, 8), pkg)
        self.assertEqual(out["gaps"][0]["reason"], GAP_NO_DECLARED_COVERAGE)

    def test_partial_coverage_mix_of_winner_and_gap(self):
        # 格 (4,4,6,6)：B3 缺该粗格；B2 无此区域记录。B2 声明覆盖 => 全空缺记录。
        # 加入一个只盖住左半的有效细格，制造“部分覆盖”
        extra = rec("B1", (4, 4, 5, 5), 1, refl=0.99, qual=99, when=ts(2026, 1, 1))
        # B1 覆盖矩形只到 y=4，需扩展声明，否则该格越界（这里直接构造新包）
        records = self.pkg.records + [extra]
        coverages = [("B1", (0, 0, 8, 6)), ("B2", (0, 0, 8, 8)), ("B3", (0, 0, 16, 16))]
        pkg = make_package(records, coverages)
        out = extract_cell((4, 4, 6, 6), pkg)
        self.assertEqual(out["status"], STATUS_PARTIAL)
        self.assertEqual(out["covered_area"], 1)
        self.assertEqual(out["gap_area"], 3)
        self.assertEqual(out["winners"][0]["source"]["batch_id"], "B1")
        self.assertEqual(out["gaps"][0]["reason"], GAP_DECLARED_NO_RECORD)
        self._assert_area_balances(out)

    def test_reason_priority_unavailable_over_declared_gap(self):
        # 同一目标格：一部分只有云遮记录，另一部分声明内缺记录，原因须分区报告且云遮优先排序
        records = [
            rec("B2", (0, 0, 1, 1), 1, state="cloud", when=ts(2026, 2, 1)),
        ]
        coverages = [("B2", (0, 0, 2, 2))]
        pkg = make_package(records, coverages)
        out = extract_cell((0, 0, 2, 2), pkg)
        reasons = {g["reason"]: g["area"] for g in out["gaps"]}
        self.assertEqual(reasons[GAP_UNAVAILABLE_CLOUD], 1)
        self.assertEqual(reasons[GAP_DECLARED_NO_RECORD], 3)
        # 排序：不可用原因排在前面
        self.assertEqual(out["gaps"][0]["reason"], GAP_UNAVAILABLE_CLOUD)

    def test_missing_channel_reason(self):
        records = [rec("B2", (0, 0, 2, 2), 2, state="missing_channel", when=ts(2026, 2, 1))]
        pkg = make_package(records, [("B2", (0, 0, 2, 2))])
        out = extract_cell((0, 0, 2, 2), pkg)
        self.assertEqual(out["gaps"][0]["reason"], GAP_UNAVAILABLE_MISSING_CHANNEL)

    def test_window_all_sixteen_cells_balance(self):
        result = extract_window(self.pkg, (0, 0, 8, 8), 2)
        self.assertEqual(result["cell_count"], 16)
        self.assertEqual(result["total_covered_area"] + result["total_gap_area"], 64)
        for cell in result["cells"]:
            self._assert_area_balances(cell)
        # 状态计数齐全
        statuses = set(result["summary"])
        self.assertIn(STATUS_HOMOGENEOUS, statuses)
        self.assertIn(STATUS_FULLY_MIXED, statuses)
        self.assertIn(STATUS_EMPTY, statuses)

    def test_window_validation(self):
        with self.assertRaises(ValueError):
            validate_window((0, 0, 5, 5), 2)  # 边长不是倍数
        with self.assertRaises(ValueError):
            validate_window((0, 0, 4, 4), 3)  # 非 2 的幂
        with self.assertRaises(ValueError):
            validate_window((1, 0, 3, 2), 2)  # 原点未对齐

    def _assert_area_balances(self, cell: dict):
        win_area = area(tuple(cell["target_cell"][k] for k in ("x0", "y0", "x1", "y1")))
        self.assertEqual(cell["area"], win_area)
        w = sum(x["area"] for x in cell["winners"])
        g = sum(x["area"] for x in cell["gaps"])
        self.assertEqual(w + g, win_area)
        for group in cell["winners"] + cell["gaps"]:
            self.assertEqual(group["area"],
                             sum(area((r["x0"], r["y0"], r["x1"], r["y1"]))
                                 for r in group["regions"]))


class PrecedenceTest(unittest.TestCase):
    def test_resolution_then_time_then_batch_id(self):
        coverages = [("A", (0, 0, 8, 8)), ("B", (0, 0, 8, 8)), ("C", (0, 0, 8, 8))]
        records = [
            # 同区域三个有效候选：C 最细 size1；A/B 同 size2，B 更新；再加一个同刻小批号
            rec("A", (0, 0, 4, 4), 2, refl=0.1, qual=1, when=ts(2026, 1, 1)),
            rec("B", (0, 0, 4, 4), 2, refl=0.2, qual=2, when=ts(2026, 2, 1)),
            rec("C", (0, 0, 1, 1), 1, refl=0.3, qual=3, when=ts(2026, 3, 1)),
        ]
        pkg = make_package(records, coverages)
        out = extract_cell((0, 0, 4, 4), pkg)
        # C 细格只占 (0,0,1,1)，其余区域 A/B 同尺寸 -> B 因较新而胜
        winners = {w["source"]["batch_id"]: w["area"] for w in out["winners"]}
        self.assertEqual(winners["C"], 1)
        self.assertEqual(winners["B"], 15)
        self.assertNotIn("A", winners)

    def test_batch_id_tiebreak_lexicographic(self):
        coverages = [("aa", (0, 0, 2, 2)), ("ab", (0, 0, 2, 2))]
        when = ts(2026, 5, 1)
        records = [
            rec("ab", (0, 0, 2, 2), 2, refl=0.9, qual=9, when=when),
            rec("aa", (0, 0, 2, 2), 2, refl=0.1, qual=1, when=when),
        ]
        pkg = make_package(records, coverages)
        out = extract_cell((0, 0, 2, 2), pkg)
        self.assertEqual(out["status"], STATUS_HOMOGENEOUS)
        self.assertEqual(out["provenance"]["batch_id"], "aa")  # 字典序小者胜


class CoarseFineTest(unittest.TestCase):
    def test_huge_coarse_cell_covered_by_one_small_cell(self):
        """极粗格被单个小格盖住的窗口案例（不展开粗格）。

        粗格本身有效且铺满整个窗口；单个有效小格在其 1 个基本单元面积上
        凭更细分辨率胜出，其余区域仍归粗格 -> 完整混合，两个赢家面积 1 与 15。
        """
        huge = 2 ** 20
        records = [
            rec("COARSE", (0, 0, huge, huge), huge, refl=0.7, qual=70, when=ts(2026, 1, 1)),
            rec("FINE", (1, 1, 2, 2), 1, refl=0.2, qual=20, when=ts(2026, 1, 2)),
        ]
        coverages = [("COARSE", (0, 0, huge, huge)), ("FINE", (0, 0, huge, huge))]
        pkg = make_package(records, coverages)
        out = extract_window(pkg, (0, 0, 4, 4), 2)
        self.assertEqual(out["cell_count"], 4)
        center = next(c for c in out["cells"] if c["target_cell"]["x0"] == 0
                      and c["target_cell"]["y0"] == 0)
        # 目标格 (0,0,2,2)：小格占 1，粗格占 3，全范围有赢家、两个物理源格 -> 完整混合
        self.assertEqual(center["status"], STATUS_FULLY_MIXED)
        self.assertEqual(center["gap_area"], 0)
        winners = {w["source"]["batch_id"]: w["area"] for w in center["winners"]}
        self.assertEqual(winners["FINE"], 1)
        self.assertEqual(winners["COARSE"], 3)
        # 片段数极少：仅由小格边线切出，与 huge 无关
        self.assertLessEqual(center["fragment_count"], 9)
        # 其余三个目标格完全由粗格均质覆盖，给原始两值
        for cell in out["cells"]:
            if cell is center:
                continue
            self.assertEqual(cell["status"], STATUS_HOMOGENEOUS)
            self.assertEqual(cell["value"], {"reflectance": 0.7, "quality": 70})
        # 总面积对平
        self.assertEqual(out["total_covered_area"], 16)
        self.assertEqual(out["total_gap_area"], 0)

    def test_huge_coarse_cell_with_gap_and_one_small_cell(self):
        """同一极粗场景下制造缺口：粗格在窗口外的另一格声明、窗口内无粗记录，
        单个小格只盖住一个基本单元 -> 部分覆盖，缺口原因须为声明内缺记录。"""
        huge = 2 ** 20
        records = [
            # 粗格在窗口外（覆盖声明包含窗口，但该物理源格不与窗口相交）
            rec("COARSE", (huge, huge, 2 * huge, 2 * huge), huge,
                refl=0.7, qual=70, when=ts(2026, 1, 1)),
            rec("FINE", (1, 1, 2, 2), 1, refl=0.2, qual=20, when=ts(2026, 1, 2)),
        ]
        coverages = [("COARSE", (0, 0, 2 * huge, 2 * huge)),
                     ("FINE", (0, 0, 2 * huge, 2 * huge))]
        pkg = make_package(records, coverages)
        center = extract_cell((0, 0, 2, 2), pkg)
        self.assertEqual(center["status"], STATUS_PARTIAL)
        self.assertEqual(center["covered_area"], 1)
        self.assertEqual(center["gap_area"], 3)
        self.assertEqual(center["gaps"][0]["reason"], GAP_DECLARED_NO_RECORD)
        self.assertEqual(center["winners"][0]["source"]["batch_id"], "FINE")

    def test_window_entirely_inside_one_coarse_cell(self):
        huge = 2 ** 30
        records = [rec("C", (0, 0, huge, huge), huge, refl=0.5, qual=5, when=ts(2026, 1, 1))]
        pkg = make_package(records, [("C", (0, 0, huge, huge))])
        out = extract_window(pkg, (8, 8, 12, 12), 2)
        self.assertTrue(all(c["status"] == STATUS_HOMOGENEOUS for c in out["cells"]))
        self.assertEqual(out["cells"][0]["fragment_count"], 1)  # 无切割线 => 单片段


if __name__ == "__main__":
    unittest.main()
