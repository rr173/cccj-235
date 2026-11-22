import unittest

from agrimosaic.geometry import (
    area, intersection, intersects, merge_adjacent, share_edge,
    split_with_rects, union_rects, is_power_of_two, aligned_to_grid,
)


class GeometryTest(unittest.TestCase):
    def test_intersection_half_open(self):
        # 仅共边：半开语义下不算相交
        self.assertIsNone(intersection((0, 0, 2, 2), (2, 0, 4, 2)))
        self.assertFalse(intersects((0, 0, 2, 2), (2, 0, 4, 2)))
        self.assertEqual(intersection((0, 0, 3, 3), (2, 2, 5, 5)), (2, 2, 3, 3))

    def test_split_partitions_target_exactly(self):
        target = (0, 0, 8, 8)
        cutters = [(0, 0, 4, 4), (4, 4, 8, 8), (2, 2, 6, 6), (1, 3, 7, 5)]
        pieces = split_with_rects(target, cutters)
        self.assertEqual(sum(area(p) for p in pieces), area(target))
        # 片段两两不重叠
        for i, a in enumerate(pieces):
            for b in pieces[i + 1:]:
                self.assertIsNone(intersection(a, b))
        # 没有候选时只返回目标自身
        self.assertEqual(split_with_rects(target, []), [target])

    def test_split_independent_of_physical_area(self):
        # 极粗格 + 一个小格：粗格边线全在目标外，实际只有小格 4 条内边线 -> 9 片，
        # 片段数只由边线数量决定，与 2^20 的物理面积无关
        huge = 2 ** 20
        pieces = split_with_rects((0, 0, 4, 4), [(0, 0, huge, huge), (1, 1, 2, 2)])
        self.assertEqual(len(pieces), 9)
        self.assertEqual(sum(area(p) for p in pieces), 16)

    def test_merge_keeps_area(self):
        rects = [(0, 0, 1, 1), (1, 0, 2, 1), (0, 1, 1, 1), (3, 3, 4, 4)]
        merged = merge_adjacent(rects)
        self.assertEqual(sum(area(r) for r in merged), sum(area(r) for r in rects))
        self.assertIn((0, 0, 2, 1), merged)
        # 仅角相接不合并
        self.assertTrue(share_edge((0, 0, 1, 1), (1, 0, 2, 1)))
        self.assertFalse(share_edge((0, 0, 1, 1), (1, 1, 2, 2)))
        self.assertIsNone(union_rects([(0, 0, 1, 1), (1, 1, 2, 2)]))

    def test_grid_predicates(self):
        self.assertTrue(is_power_of_two(1), )
        self.assertTrue(is_power_of_two(1024))
        self.assertFalse(is_power_of_two(0))
        self.assertFalse(is_power_of_two(6))
        self.assertTrue(aligned_to_grid((0, 0, 4, 8), 4))
        self.assertFalse(aligned_to_grid((0, 0, 4, 7), 4))


if __name__ == "__main__":
    unittest.main()
