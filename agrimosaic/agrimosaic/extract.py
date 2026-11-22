"""按矩形窗口从标准格网镶嵌表提取目标格。

核心方法（全程整数矩形运算，不逐基本单元栅格化）：
1. R 树取出与目标格正面积相交的全部候选源格；
2. 用候选源格的边线把目标格切成互不重叠的小片段（split_with_rects），
   每个片段内的候选集合恒定；
3. 片段内有效候选按 更细分辨率 → 较新时刻 → 小批号 选赢家；无赢家时按
   不可用(云遮 > 缺通道) → 声明内缺记录 → 无声明覆盖 的优先级归因；
4. 相同赢家或相同原因的相邻片段合并，面积必须与目标格面积对平。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .geometry import (
    Rect, area, contains, intersection, is_power_of_two, merge_adjacent,
    split_with_rects,
)
from .model import (
    GAP_DECLARED_NO_RECORD, GAP_NO_DECLARED_COVERAGE, GAP_REASON_PRIORITY,
    GAP_UNAVAILABLE_CLOUD, GAP_UNAVAILABLE_MISSING_CHANNEL,
    SourceRecord, winner_key,
)
from .rtree import query as rtree_query
from .store import Package

# 目标格状态码
STATUS_HOMOGENEOUS = "homogeneous"          # 全范围同一物理源格赢家，给出原始两值
STATUS_FULLY_MIXED = "fully_mixed"          # 完整混合：全范围有赢家但不止一个物理源格
STATUS_PARTIAL = "partial_coverage"         # 部分覆盖：有赢家也有未覆盖片段
STATUS_EMPTY = "empty"                      # 全空：没有任何赢家片段


@dataclass
class WindowRequest:
    window: Rect
    target_size: int


def validate_window(window: Rect, target_size: int) -> None:
    if not isinstance(target_size, int) or not is_power_of_two(target_size):
        raise ValueError(f"target_size={target_size} 必须是 2 的非负整数次幂")
    if any(not isinstance(c, int) or c < 0 for c in window):
        raise ValueError(f"窗口坐标必须是非负整数：{window}")
    if window[2] <= window[0] or window[3] <= window[1]:
        raise ValueError(f"窗口必须是正面积矩形：{window}")
    if window[0] % target_size or window[1] % target_size:
        raise ValueError(f"窗口原点 {window[:2]} 未按 target_size={target_size} 对齐")
    if (window[2] - window[0]) % target_size or (window[3] - window[1]) % target_size:
        raise ValueError(f"窗口边长不是 target_size={target_size} 的整数倍")


def iter_target_cells(window: Rect, target_size: int):
    """按行优先（y 再 x）枚举窗口内目标格。"""
    for y in range(window[1], window[3], target_size):
        for x in range(window[0], window[2], target_size):
            yield (x, y, x + target_size, y + target_size)


def _best_gap_reason(piece: Rect, candidates: Sequence[SourceRecord],
                     coverages: Sequence[tuple[str, Rect]]) -> str:
    """无有效赢家片段的失败归因。

    优先级（多种并存取最高者）：
      不可用记录（云遮 > 缺通道） > 声明内缺记录 > 无声明覆盖。
    """
    reasons: set[str] = set()
    declared = any(intersection(piece, cov) is not None for _, cov in coverages)
    for rec in candidates:
        inter = intersection(piece, rec.rect)
        if inter is None:
            continue
        if rec.state == "cloud":
            reasons.add(GAP_UNAVAILABLE_CLOUD)
        elif rec.state == "missing_channel":
            reasons.add(GAP_UNAVAILABLE_MISSING_CHANNEL)
    if reasons:
        return min(reasons, key=lambda r: GAP_REASON_PRIORITY[r])
    if declared:
        return GAP_DECLARED_NO_RECORD
    return GAP_NO_DECLARED_COVERAGE


def extract_cell(target: Rect, pkg: Package) -> dict:
    """提取单个目标格，返回可直接 JSON 序列化的结果对象。"""
    candidate_ids = rtree_query(pkg.nodes, pkg.root_id, target)
    candidates = [pkg.records[i] for i in candidate_ids]
    pieces = split_with_rects(target, [r.rect for r in candidates])

    # 片段 -> 赢家记录 / 失败原因
    winner_pieces: dict[int, List[Rect]] = defaultdict(list)
    gap_pieces: dict[str, List[Rect]] = defaultdict(list)
    piece_winners: List[Optional[SourceRecord]] = []
    total_piece_area = 0

    for piece in pieces:
        total_piece_area += area(piece)
        covering = [r for r in candidates if intersection(piece, r.rect) is not None]
        valid = [r for r in covering if r.valid]
        if valid:
            winner = min(valid, key=winner_key)
            winner_pieces[id(winner)].append(piece)
            piece_winners.append(winner)
        else:
            reason = _best_gap_reason(piece, covering, pkg.coverages)
            gap_pieces[reason].append(piece)
            piece_winners.append(None)

    assert total_piece_area == area(target), "片段面积之和必须等于目标格面积"

    winners_out = []
    winning_records = [w for w in piece_winners if w is not None]
    unique_winners = _unique_by_identity(winning_records)
    for w in unique_winners:
        rects = merge_adjacent(winner_pieces[id(w)])
        winners_out.append({
            "source": w.descriptor(),
            "reflectance": w.reflectance,
            "quality": w.quality,
            "regions": [_rect_json(r) for r in rects],
            "area": sum(area(r) for r in rects),
        })

    gaps_out = []
    for reason, rects in gap_pieces.items():
        merged = merge_adjacent(rects)
        gaps_out.append({
            "reason": reason,
            "regions": [_rect_json(r) for r in merged],
            "area": sum(area(r) for r in merged),
        })
    gaps_out.sort(key=lambda g: GAP_REASON_PRIORITY[g["reason"]])

    covered_area = sum(g["area"] for g in winners_out)
    gap_area = sum(g["area"] for g in gaps_out)
    assert covered_area + gap_area == area(target), "赢家与缺口面积必须对平目标格"

    n_winners = len(unique_winners)
    if gap_area == 0 and n_winners == 1:
        status = STATUS_HOMOGENEOUS
    elif gap_area == 0:
        status = STATUS_FULLY_MIXED
    elif covered_area == 0:
        status = STATUS_EMPTY
    else:
        status = STATUS_PARTIAL

    out = {
        "target_cell": _rect_json(target),
        "target_size": target[2] - target[0],
        "status": status,
        "covered_area": covered_area,
        "gap_area": gap_area,
        "area": area(target),
        "winners": winners_out,
        "gaps": gaps_out,
        "fragment_count": len(pieces),
    }
    if status == STATUS_HOMOGENEOUS:
        only = winners_out[0]
        out["value"] = {"reflectance": only["reflectance"], "quality": only["quality"]}
        out["provenance"] = only["source"]
    return out


def _unique_by_identity(records: Sequence[SourceRecord]) -> List[SourceRecord]:
    seen: set[int] = set()
    out: List[SourceRecord] = []
    for r in records:
        if id(r) not in seen:
            seen.add(id(r))
            out.append(r)
    # 稳定排序：按赢家键，便于核对
    return sorted(out, key=winner_key)


def _rect_json(r: Rect) -> dict:
    return {"x0": r[0], "y0": r[1], "x1": r[2], "y1": r[3]}


def extract_window(pkg: Package, window: Rect, target_size: int) -> dict:
    """提取整个矩形窗口，返回汇总对象（cells 按行优先排列）。"""
    validate_window(window, target_size)
    cells = [extract_cell(t, pkg) for t in iter_target_cells(window, target_size)]
    summary: dict[str, int] = defaultdict(int)
    for c in cells:
        summary[c["status"]] += 1
    return {
        "window": _rect_json(window),
        "target_size": target_size,
        "cell_count": len(cells),
        "summary": dict(summary),
        "total_covered_area": sum(c["covered_area"] for c in cells),
        "total_gap_area": sum(c["gap_area"] for c in cells),
        "cells": cells,
    }
