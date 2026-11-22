"""整数坐标半开矩形的精确几何运算。

矩形统一表示为 (x0, y0, x1, y1)，区间为 [x0, x1) × [y0, y1)，
所有坐标与边长均为非负整数。目标格、源格、覆盖矩形共用该表示。
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

Rect = Tuple[int, int, int, int]


def intersects(a: Rect, b: Rect) -> bool:
    """两个半开矩形是否具有正面积交集（仅共边不算相交）。"""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def intersection(a: Rect, b: Rect) -> Rect | None:
    """返回正面积交集；仅共边或相离时返回 None。"""
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x0 < x1 and y0 < y1:
        return (x0, y0, x1, y1)
    return None


def area(r: Rect) -> int:
    """矩形面积（基本单元平方数）。"""
    return (r[2] - r[0]) * (r[3] - r[1])


def contains(outer: Rect, inner: Rect) -> bool:
    """outer 是否完整包含 inner（含边界贴合）。"""
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def is_power_of_two(n: int) -> bool:
    return isinstance(n, int) and n >= 1 and (n & (n - 1)) == 0


def aligned_to_grid(rect: Rect, size: int) -> bool:
    """矩形是否按 size 格网对齐（坐标均为 size 的整数倍，边长为正）。"""
    return (
        rect[0] % size == 0
        and rect[1] % size == 0
        and rect[2] % size == 0
        and rect[3] % size == 0
        and rect[2] > rect[0]
        and rect[3] > rect[1]
    )


def split_with_rects(target: Rect, cutters: Sequence[Rect]) -> List[Rect]:
    """用一组切割矩形的边线把 target 切成互不重叠、并集恰为 target 的小矩形。

    切割矩形只贡献其边线，不要求覆盖 target；落在 target 外的边被裁掉。
    该函数是“不栅格化叠加”的核心：输出片段数量只与切割线数量有关，
    与 target 的物理面积无关，因此极粗覆盖也不会被展开成基本单元。
    """
    xs = {target[0], target[2]}
    ys = {target[1], target[3]}
    tx0, ty0, tx1, ty1 = target
    for r in cutters:
        for x in (r[0], r[2]):
            if tx0 < x < tx1:
                xs.add(x)
        for y in (r[1], r[3]):
            if ty0 < y < ty1:
                ys.add(y)
    xss = sorted(xs)
    yss = sorted(ys)
    pieces: List[Rect] = []
    for i in range(len(xss) - 1):
        for j in range(len(yss) - 1):
            pieces.append((xss[i], yss[j], xss[i + 1], yss[j + 1]))
    return pieces


def share_edge(a: Rect, b: Rect) -> bool:
    """两个面积为正、互不重叠的矩形是否共享一条完整边（合并后仍是矩形）。"""
    # 竖向共边：左右相邻
    if a[2] == b[0] or b[2] == a[0]:
        return a[1] == b[1] and a[3] == b[3]
    # 横向共边：上下相邻
    if a[3] == b[1] or b[3] == a[1]:
        return a[0] == b[0] and a[2] == b[2]
    return False


def union_rects(rects: Iterable[Rect]) -> Rect | None:
    """矩形并集（当且仅当它们恰好拼成一个矩形时返回该矩形）。"""
    rects = list(rects)
    if not rects:
        return None
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[2] for r in rects)
    y1 = max(r[3] for r in rects)
    if sum(area(r) for r in rects) == (x1 - x0) * (y1 - y0):
        return (x0, y0, x1, y1)
    return None


def merge_adjacent(rects: Sequence[Rect]) -> List[Rect]:
    """把相同来源/原因的相邻矩形合并为尽量少的矩形。

    仅当两个矩形共享一条完整边、合并后仍为单个矩形时才合并；
    合并保持总面积不变，输出按 (y0, x0) 排序。
    """
    merged: List[Rect] = list(rects)
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                if share_edge(merged[i], merged[j]):
                    u = union_rects((merged[i], merged[j]))
                    if u is not None:
                        merged = [r for k, r in enumerate(merged) if k not in (i, j)] + [u]
                        changed = True
                        break
            if changed:
                break
    return sorted(merged, key=lambda r: (r[1], r[0]))
