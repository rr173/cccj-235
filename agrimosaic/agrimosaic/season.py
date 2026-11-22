"""季末离线作业：逐地块复测资格台账。

输入一份季末清单（地块半开矩形、按日严格递增的预定巡测轮次及其镶嵌包路径、
裁决门槛），输出机器可读 JSON 台账，告诉调度员：

* 已确认达到“连续 K 轮低值且首尾跨越至少 D 日”的面积（下界）；
* 把所有未知观测乐观视为低值时可能达标的面积上界（含已确认部分）；
* 已确认面积首次达标时的末轮日期；
* 以下界/上界对整块地的占比与面积门槛比较得出的复测资格。

几何上全程使用整数半开矩形的面积与交集运算：

* 不按实际覆盖面积枚举基本单元（极粗格不会被展开）；
* 多轮叠加只做矩形之间的真实交集裁剪，不把互不相交的局部边界拉通成
  横纵边线的全笛卡尔积；
* 面积全部是整数基本单元平方数，门槛比较用整数交叉相乘，门槛处不会换档。

作业只读输入包，不修改任何镶嵌包，也不产出地图界面或逐位置输出。
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .geometry import Rect, area, intersection, split_with_rects
from .model import InputError, winner_key
from .rtree import build_rtree, query as rtree_query
from .store import Package, load_package

# 单轮单点状态
ROUND_LOW = "low"                  # 有赢家、质量达标且反射不高于上限
ROUND_NOT_LOW = "not_low"          # 有赢家、质量达标但反射高于上限
ROUND_UNKNOWN = "unknown"          # 无赢家 / 缺包 / 赢家质量低于门槛

# 地块复测资格
QUAL_RETEST = "retest"             # 下界达到门槛：列入复测
QUAL_EXCLUDED = "excluded"         # 上界仍不到门槛：不列入
QUAL_PENDING = "pending"           # 之间：缺测可能改变资格，待定

LEDGER_SCHEMA_VERSION = 1
_BASIS_POINTS = 10000
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Plot:
    plot_id: str
    bounds: Rect


@dataclass(frozen=True)
class Round:
    date: _dt.date
    package: Optional[str]  # None 表示该轮未取得镶嵌包


@dataclass(frozen=True)
class Criteria:
    reflectance_cap: float
    min_quality: int
    min_consecutive_rounds: int   # K：正整数
    min_span_days: int            # D：非负整数
    area_threshold_bp: int        # 1..10000 的整数基点


@dataclass
class SeasonManifest:
    plots: List[Plot]
    rounds: List[Round]
    criteria: Criteria
    base_dir: str


# ---------------- 清单解析与校验 ----------------

def _require_int(v: Any, name: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise InputError(f"{name} 必须是整数，收到 {v!r}")
    return v


def _parse_rect(obj: Any, what: str) -> Rect:
    if not isinstance(obj, dict):
        raise InputError(f"{what} 必须是对象，收到 {type(obj).__name__}")
    try:
        x0, y0, x1, y1 = (_require_int(obj[k], f"{what}.{k}")
                          for k in ("x0", "y0", "x1", "y1"))
    except KeyError as exc:
        raise InputError(f"{what} 缺少字段 {exc.args[0]}") from None
    if any(c < 0 for c in (x0, y0, x1, y1)):
        raise InputError(f"{what} 坐标必须是非负整数，收到 {(x0, y0, x1, y1)}")
    if x1 <= x0 or y1 <= y0:
        raise InputError(f"{what} 必须是正面积半开矩形，收到 {(x0, y0, x1, y1)}")
    return (x0, y0, x1, y1)


def parse_manifest(path: str) -> SeasonManifest:
    """读取并严格校验季末清单；任何非法情形都抛 InputError。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except OSError as exc:
        raise InputError(f"季末清单无法打开：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"季末清单不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(obj, dict):
        raise InputError("季末清单必须是 JSON 对象")

    raw_plots = obj.get("plots")
    if not isinstance(raw_plots, list) or not raw_plots:
        raise InputError("清单 plots 必须是非空数组")
    plots: List[Plot] = []
    seen_ids: set[str] = set()
    for item in raw_plots:
        if not isinstance(item, dict):
            raise InputError("plots 条目必须是对象")
        pid = item.get("plot_id")
        if not isinstance(pid, str) or not pid:
            raise InputError("plot_id 必须是非空字符串")
        if pid in seen_ids:
            raise InputError(f"地块编号重复：{pid}")
        seen_ids.add(pid)
        bounds = _parse_rect(item.get("bounds"), f"地块 {pid} 的 bounds")
        plots.append(Plot(pid, bounds))

    raw_rounds = obj.get("rounds")
    if not isinstance(raw_rounds, list) or not raw_rounds:
        raise InputError("清单 rounds 必须是非空数组")
    rounds: List[Round] = []
    prev_date: Optional[_dt.date] = None
    for item in raw_rounds:
        if not isinstance(item, dict):
            raise InputError("rounds 条目必须是对象")
        ds = item.get("date")
        if not isinstance(ds, str) or not _DATE_RE.match(ds):
            raise InputError(f"轮次日期必须是 YYYY-MM-DD 字符串，收到 {ds!r}")
        try:
            day = _dt.date.fromisoformat(ds)
        except ValueError as exc:
            raise InputError(f"轮次日期不是合法日历日期：{ds!r}（{exc}）") from exc
        if prev_date is not None and day <= prev_date:
            raise InputError(f"轮次日期必须按日严格递增：{prev_date} 之后又出现 {day}")
        prev_date = day
        pkg = item.get("package")
        if pkg is not None and (not isinstance(pkg, str) or not pkg):
            raise InputError(f"轮次 {ds} 的 package 必须是 null 或非空字符串")
        rounds.append(Round(day, pkg))

    raw_c = obj.get("criteria")
    if not isinstance(raw_c, dict):
        raise InputError("清单 criteria 必须是对象")
    cap = raw_c.get("reflectance_cap")
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) \
            or not math.isfinite(float(cap)):
        raise InputError(f"reflectance_cap 必须是有限数值，收到 {cap!r}")
    min_quality = _require_int(raw_c.get("min_quality"), "criteria.min_quality")
    k = _require_int(raw_c.get("min_consecutive_rounds"),
                     "criteria.min_consecutive_rounds")
    if k < 1:
        raise InputError(f"min_consecutive_rounds(K) 必须是正整数，收到 {k}")
    d_days = _require_int(raw_c.get("min_span_days"), "criteria.min_span_days")
    if d_days < 0:
        raise InputError(f"min_span_days(D) 必须是非负整数，收到 {d_days}")
    threshold = _require_int(raw_c.get("area_threshold_bp"),
                             "criteria.area_threshold_bp")
    if not 1 <= threshold <= _BASIS_POINTS:
        raise InputError(
            f"area_threshold_bp 必须是 1..10000 的整数基点，收到 {threshold}")

    return SeasonManifest(
        plots=plots, rounds=rounds,
        criteria=Criteria(float(cap), min_quality, k, d_days, threshold),
        base_dir=os.path.dirname(os.path.abspath(path)),
    )


def load_manifest_packages(manifest: SeasonManifest) -> List[Optional[Package]]:
    """按清单顺序加载全部非空包；null 轮次原样保留为 None。

    任何非空包无法读取都抛 InputError —— 作业失败，不能当成一次未观测继续出表。
    """
    packages: List[Optional[Package]] = []
    for rd in manifest.rounds:
        if rd.package is None:
            packages.append(None)
            continue
        path = rd.package
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(manifest.base_dir, path))
        try:
            packages.append(load_package(path))
        except (OSError, InputError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise InputError(
                f"轮次 {rd.date.isoformat()} 引用的非空镶嵌包无法读取："
                f"{path}（{exc}）") from exc
    return packages


# ---------------- 单轮裁决（复用既有赢家规则，再叠加质量门槛） ----------------

def classify_plot_round(pkg: Optional[Package], rect: Rect,
                        reflectance_cap: float, min_quality: int
                        ) -> List[Tuple[Rect, str]]:
    """把地块矩形在一轮内切成状态恒定的小矩形（并集恰为地块）。

    赢家裁决与 extract 完全一致：R 树候选 -> 候选边线切片 ->
    更细分辨率 → 较新时刻 → 小批号；没有有效赢家即为未知。
    本作业只在赢家确定之后再应用质量门槛，且绝不改选排名靠后的源格。
    """
    if pkg is None:
        # 轮次缺包：整块地该轮未知
        return [(rect, ROUND_UNKNOWN)]

    candidate_ids = rtree_query(pkg.nodes, pkg.root_id, rect)
    candidates = [pkg.records[i] for i in candidate_ids]
    pieces = split_with_rects(rect, [r.rect for r in candidates])

    partition: List[Tuple[Rect, str]] = []
    checked_area = 0
    for piece in pieces:
        covering = [r for r in candidates
                    if intersection(piece, r.rect) is not None]
        valid = [r for r in covering if r.valid]
        if not valid:
            label = ROUND_UNKNOWN  # 无赢家：云遮/缺通道/缺页/无声明，均不做改选
        else:
            winner = min(valid, key=winner_key)
            if winner.quality < min_quality:
                label = ROUND_UNKNOWN  # 赢家质量低于门槛，同样不改选靠后源格
            elif winner.reflectance <= reflectance_cap:
                label = ROUND_LOW
            else:
                label = ROUND_NOT_LOW
        partition.append((piece, label))
        checked_area += area(piece)
    assert checked_area == area(rect), "单轮切片面积必须与地块面积对平"
    return partition


# ---------------- 多轮增量叠加（真实矩形交集，不拉通边线） ----------------

def overlay_round(layer: List[Tuple[Rect, Tuple[str, ...]]],
                  partition: List[Tuple[Rect, str]]
                  ) -> List[Tuple[Rect, Tuple[str, ...]]]:
    """把一轮分区叠加到已有原子层上。

    每个原子只与真正和它正面积相交的分区矩形做交集裁剪，新片段都是真实的
    矩形交集，分区中互不相交的局部边线不会被拉通到无关区域，因此不会生成
    横纵边线的全笛卡尔积；片段数只随真正相交的局部边界增长，与物理面积无关。
    """
    boxes = [r for r, _ in partition]
    nodes, root_id = build_rtree(boxes)
    next_layer: List[Tuple[Rect, Tuple[str, ...]]] = []
    for atom_rect, vec in layer:
        hits = rtree_query(nodes, root_id, atom_rect)
        sub_total = 0
        matched = 0
        for idx in hits:
            inter = intersection(atom_rect, boxes[idx])
            if inter is None:
                continue
            next_layer.append((inter, vec + (partition[idx][1],)))
            sub_total += area(inter)
            matched += 1
        # 分区完整覆盖地块，每个原子必须被交集片段恰好铺满
        assert matched > 0 and sub_total == area(atom_rect), \
            "多轮叠加交集面积必须与原子面积对平"
    return next_layer


# ---------------- 逐点（标签向量）连续段判定 ----------------

def evaluate_vector(labels: Tuple[str, ...], dates: List[_dt.date],
                    k: int, min_span_days: int
                    ) -> Tuple[Optional[int], bool]:
    """返回 (首次确认的末轮下标, 未知乐观视为低值时是否可能达标)。

    未知轮次会打断真实低值连续段，不允许跨未知拼接；但允许从下一轮重新开始。
    已确认向量只记首次达标的末轮；上界判定把未知一律当低值后跑同一规则。
    """
    first_confirmed: Optional[int] = None
    run_len = 0
    run_start = 0
    for i, lab in enumerate(labels):
        if lab == ROUND_LOW:
            if run_len == 0:
                run_start = i
            run_len += 1
            if first_confirmed is None and run_len >= k \
                    and (dates[i] - dates[run_start]).days >= min_span_days:
                first_confirmed = i
        else:  # not_low 或 unknown 都打断连续段
            run_len = 0

    possible = False
    run_len = 0
    run_start = 0
    for i, lab in enumerate(labels):
        if lab == ROUND_NOT_LOW:  # 乐观情形下只有实测高值能打断
            run_len = 0
            continue
        if run_len == 0:
            run_start = i
        run_len += 1
        if run_len >= k and (dates[i] - dates[run_start]).days >= min_span_days:
            possible = True
            break
    return first_confirmed, possible


def _meets(part_area: int, total_area: int, threshold_bp: int) -> bool:
    """整数交叉相乘的精确门槛比较：part/total >= threshold/10000，绝不四舍五入。"""
    return part_area * _BASIS_POINTS >= threshold_bp * total_area


# ---------------- 台账构建 ----------------

def build_ledger(manifest: SeasonManifest,
                 packages: List[Optional[Package]]) -> Dict[str, Any]:
    criteria = manifest.criteria
    dates = [rd.date for rd in manifest.rounds]
    plot_entries: List[Dict[str, Any]] = []

    for plot in manifest.plots:
        # 初始层：整块地一个原子、零个轮次标签
        layer: List[Tuple[Rect, Tuple[str, ...]]] = [(plot.bounds, ())]
        for pkg in packages:
            partition = classify_plot_round(
                pkg, plot.bounds, criteria.reflectance_cap, criteria.min_quality)
            layer = overlay_round(layer, partition)

        # 标签向量 -> 精确整数面积
        vector_areas: Dict[Tuple[str, ...], int] = defaultdict(int)
        for atom_rect, vec in layer:
            vector_areas[vec] += area(atom_rect)
        total_area = area(plot.bounds)
        assert sum(vector_areas.values()) == total_area, \
            "台账面积必须与整块地面积对平"

        confirmed_area = 0
        possible_area = 0
        confirmed_by_date: Dict[str, int] = defaultdict(int)
        for vec, vec_area in vector_areas.items():
            first_idx, possible = evaluate_vector(
                vec, dates, criteria.min_consecutive_rounds,
                criteria.min_span_days)
            if first_idx is not None:
                confirmed_area += vec_area
                confirmed_by_date[dates[first_idx].isoformat()] += vec_area
            if possible:
                possible_area += vec_area

        assert confirmed_area <= possible_area <= total_area
        confirmed_reaches = _meets(confirmed_area, total_area,
                                   criteria.area_threshold_bp)
        possible_reaches = _meets(possible_area, total_area,
                                  criteria.area_threshold_bp)
        if confirmed_reaches:
            qualification = QUAL_RETEST
        elif not possible_reaches:
            qualification = QUAL_EXCLUDED
        else:
            qualification = QUAL_PENDING

        confirmed_bp = confirmed_area * _BASIS_POINTS // total_area
        possible_bp = possible_area * _BASIS_POINTS // total_area
        if qualification == QUAL_RETEST:
            note = (f"已确认面积占比 {confirmed_bp}bp 达到门槛 "
                    f"{criteria.area_threshold_bp}bp，列入复测")
        elif qualification == QUAL_EXCLUDED:
            note = (f"可能达标上界占比 {possible_bp}bp 仍低于门槛 "
                    f"{criteria.area_threshold_bp}bp，不列入复测")
        else:
            note = (f"已确认 {confirmed_bp}bp 未达门槛、上界 {possible_bp}bp "
                    f"可达，缺失/不可靠观测仍可能改变复测资格，待定")

        plot_entries.append({
            "plot_id": plot.plot_id,
            "bounds": {"x0": plot.bounds[0], "y0": plot.bounds[1],
                       "x1": plot.bounds[2], "y1": plot.bounds[3]},
            "total_area": total_area,
            "confirmed_area": confirmed_area,
            "possible_area_upper_bound": possible_area,
            "confirmed_bp": confirmed_bp,
            "possible_bp_upper_bound": possible_bp,
            "first_confirmed_area_by_date": dict(sorted(confirmed_by_date.items())),
            "qualification": qualification,
            "qualification_note": note,
        })

    return {
        "job": "agrimosaic.season.retest_qualification_ledger",
        "schema_version": LEDGER_SCHEMA_VERSION,
        "criteria": {
            "reflectance_cap": criteria.reflectance_cap,
            "min_quality": criteria.min_quality,
            "min_consecutive_rounds": criteria.min_consecutive_rounds,
            "min_span_days": criteria.min_span_days,
            "area_threshold_bp": criteria.area_threshold_bp,
        },
        "rounds": [
            {"date": rd.date.isoformat(),
             "observed": pkg is not None}
            for rd, pkg in zip(manifest.rounds, packages)
        ],
        "plot_count": len(plot_entries),
        "plots": plot_entries,
    }


def run_season_job(manifest_path: str) -> Dict[str, Any]:
    """清单解析 -> 包加载（失败即拒收）-> 台账构建。"""
    manifest = parse_manifest(manifest_path)
    packages = load_manifest_packages(manifest)
    return build_ledger(manifest, packages)
