"""批次质量审计：在引用镶嵌包前，逐个来源批次核对原始数值异常。

审计只读包，不改变任何包内容；统计对象是记录本身而不是空间赢家：

* 只统计 ``state == "valid"`` 且所选通道（反射值或质量值）有值的源格记录；
* 同一物理记录仅计一次，不按空间赢家、覆盖关系或采集时间筛选；
* 被更细格或更新批次压过（从未在窗口裁决中胜出）的异常记录同样会被发现；
* 每批报告有效记录数、区间外记录数、异常格对应基本单元总面积
  （源格边长 s 贡献 s²），以及包住全部异常格的最小半开矩形；
* 包内已声明但零记录、或全部记录不可用的批次同样给出零统计。

规则引用不合法（点名任何包都未声明的批次）、任何包/记录结构损坏都属于
输入错误（InputError），不会生成部分报告，也不会被误标成质量未通过。
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .geometry import Rect, aligned_to_grid, contains, is_power_of_two
from .model import RECORD_STATES, VALID, InputError, SourceRecord, ValueDomain
from .store import PACKAGE_VERSION, Package

AUDIT_SCHEMA_VERSION = 1
REPORT_JOB = "agrimosaic.audit.batch_quality"

AUDIT_REFLECTANCE = "reflectance"
AUDIT_QUALITY = "quality"
AUDIT_CHANNELS = (AUDIT_REFLECTANCE, AUDIT_QUALITY)

AUDIT_PASS = "pass"
AUDIT_FAIL = "fail"


# ---------------- 配置解析与校验 ----------------

@dataclass(frozen=True)
class AuditRule:
    channel: str
    min_value: Any          # 含端点下界（float 或 int）
    max_value: Any          # 含端点上界
    anomaly_threshold: int  # 异常记录数门槛：达到（>=）即未通过


@dataclass(frozen=True)
class AuditBatchSpec:
    batch_id: str
    rules: Sequence[AuditRule]


@dataclass(frozen=True)
class AuditConfig:
    base_dir: str
    packages: Sequence[str]
    batches: Sequence[AuditBatchSpec]


def _reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> dict:
    """JSON 对象内重复键一律拒收，避免“后写覆盖先写”的静默歧义。"""
    seen: set[str] = set()
    out: dict = {}
    for key, value in pairs:
        if key in seen:
            raise InputError(f"配置 JSON 对象内出现重复键：{key!r}")
        seen.add(key)
        out[key] = value
    return out


def _require_int(v: Any, name: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise InputError(f"{name} 必须是整数，收到 {v!r}")
    return v


def _require_finite_number(v: Any, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise InputError(f"{name} 必须是有限数值，收到 {v!r}")
    return float(v)


def _parse_rule(obj: Any, what: str) -> AuditRule:
    if not isinstance(obj, dict):
        raise InputError(f"{what} 必须是对象")
    unknown = set(obj) - {"channel", "min", "max", "anomaly_threshold"}
    if unknown:
        raise InputError(f"{what} 含有未知字段：{sorted(unknown)}")
    channel = obj.get("channel")
    if channel not in AUDIT_CHANNELS:
        raise InputError(
            f"{what} 的 channel 必须是 {AUDIT_CHANNELS} 之一，收到 {channel!r}")
    lo, hi = obj.get("min"), obj.get("max")
    if channel == AUDIT_REFLECTANCE:
        lo = _require_finite_number(lo, f"{what}.min")
        hi = _require_finite_number(hi, f"{what}.max")
    else:
        lo = _require_int(lo, f"{what}.min")
        hi = _require_int(hi, f"{what}.max")
    if lo > hi:
        raise InputError(f"{what} 的合格区间下界不得大于上界：{lo} > {hi}")
    threshold = _require_int(obj.get("anomaly_threshold"), f"{what}.anomaly_threshold")
    if threshold < 1:
        raise InputError(
            f"{what}.anomaly_threshold 必须是正整数（达到该异常数即未通过），"
            f"收到 {threshold}")
    return AuditRule(channel, lo, hi, threshold)


def _parse_audit_batch(obj: Any, what: str) -> AuditBatchSpec:
    if not isinstance(obj, dict):
        raise InputError(f"{what} 必须是对象")
    unknown = set(obj) - {"batch_id", "rules"}
    if unknown:
        raise InputError(f"{what} 含有未知字段：{sorted(unknown)}")
    bid = obj.get("batch_id")
    if not isinstance(bid, str) or not bid:
        raise InputError(f"{what} 的 batch_id 必须是非空字符串")
    raw_rules = obj.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise InputError(f"{what} 的 rules 必须是非空数组")
    rules = [_parse_rule(r, f"{what}（批次 {bid}）的第 {i + 1} 条规则")
             for i, r in enumerate(raw_rules)]
    return AuditBatchSpec(bid, tuple(rules))


def parse_audit_config(path: str) -> AuditConfig:
    """读取并严格校验审计配置；任何非法引用都抛 InputError。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh, object_pairs_hook=_reject_duplicate_keys)
    except OSError as exc:
        raise InputError(f"审计配置无法打开：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"审计配置不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(obj, dict):
        raise InputError("审计配置必须是 JSON 对象")
    base_dir = os.path.dirname(os.path.abspath(path))

    raw_pkgs = obj.get("packages")
    if not isinstance(raw_pkgs, list) or not raw_pkgs:
        raise InputError("审计配置 packages 必须是非空数组")
    packages: List[str] = []
    for i, entry in enumerate(raw_pkgs):
        if not isinstance(entry, str) or not entry:
            raise InputError(f"packages 第 {i + 1} 项必须是包目录非空字符串")
        resolved = entry if os.path.isabs(entry) else os.path.normpath(
            os.path.join(base_dir, entry))
        resolved = os.path.abspath(resolved)
        if resolved in packages:
            raise InputError(f"packages 重复列出同一个包目录：{entry}")
        packages.append(resolved)

    raw_batches = obj.get("batches")
    if not isinstance(raw_batches, list) or not raw_batches:
        raise InputError("审计配置 batches 必须是非空数组")
    seen: set[str] = set()
    specs: List[AuditBatchSpec] = []
    for i, entry in enumerate(raw_batches):
        spec = _parse_audit_batch(entry, f"batches 第 {i + 1} 项")
        if spec.batch_id in seen:
            raise InputError(f"审计配置对批次 {spec.batch_id} 重复配置规则")
        seen.add(spec.batch_id)
        specs.append(spec)
    return AuditConfig(base_dir, tuple(packages), tuple(specs))


# ---------------- 包的严格只读加载 ----------------

def load_audit_package(pkg_dir: str) -> Package:
    """严格读取镶嵌包；结构损坏或引用不一致一律抛 InputError。

    与一般 ``load_package`` 不同，这里会核对包里每一条记录的结构、批次引用、
    坐标对齐与值域，确保审计不会在损坏包上给出部分报告。
    """
    meta_path = os.path.join(pkg_dir, "meta.json")
    records_path = os.path.join(pkg_dir, "records.jsonl")
    rtree_path = os.path.join(pkg_dir, "rtree.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh, object_pairs_hook=_reject_duplicate_keys)
    except OSError as exc:
        raise InputError(f"包 {pkg_dir} 的 meta.json 无法打开：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"包 {pkg_dir} 的 meta.json 不是合法 JSON：{exc}") from exc
    if not isinstance(meta, dict):
        raise InputError(f"包 {pkg_dir} 的 meta.json 必须是 JSON 对象")
    if meta.get("package_version") != PACKAGE_VERSION:
        raise InputError(f"包 {pkg_dir} 不支持的包版本：{meta.get('package_version')!r}")

    domain = _parse_meta_domain(meta.get("value_domain"), pkg_dir)
    batches = _parse_meta_batches(meta.get("batches"), pkg_dir)
    batch_ids = [b[0] for b in batches]
    if len(set(batch_ids)) != len(batch_ids):
        raise InputError(f"包 {pkg_dir} 的 meta.json 批次号重复")
    batch_map = {bid: (size, cov) for bid, size, cov, _acq in batches}
    coverages = [(bid, cov) for bid, _size, cov, _acq in batches]

    raw_record_count = meta.get("record_count")
    if not isinstance(raw_record_count, int) or isinstance(raw_record_count, bool) \
            or raw_record_count < 0:
        raise InputError(f"包 {pkg_dir} 的 meta.json record_count 必须是非负整数")

    records = []
    seen_cells: set[Tuple[str, Rect]] = set()
    try:
        with open(records_path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                rec = _parse_package_record(line, line_no, pkg_dir, domain, batch_map)
                key = (rec.batch_id, rec.rect)
                if key in seen_cells:
                    raise InputError(
                        f"包 {pkg_dir} 的 records.jsonl 第 {line_no} 行：批次 "
                        f"{rec.batch_id} 对物理源格 {rec.rect} 存在重复定义，包已损坏")
                seen_cells.add(key)
                records.append(rec)
    except OSError as exc:
        raise InputError(f"包 {pkg_dir} 的 records.jsonl 无法打开：{exc}") from exc
    if len(records) != raw_record_count:
        raise InputError(
            f"包 {pkg_dir} 的 record_count={raw_record_count} 与实际记录数 "
            f"{len(records)} 不一致")

    try:
        with open(rtree_path, "r", encoding="utf-8") as fh:
            idx = json.load(fh, object_pairs_hook=_reject_duplicate_keys)
    except OSError as exc:
        raise InputError(f"包 {pkg_dir} 的 rtree.json 无法打开：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"包 {pkg_dir} 的 rtree.json 不是合法 JSON：{exc}") from exc
    _validate_rtree(idx, len(records), pkg_dir)

    return Package(meta, records, idx["nodes"], idx["root_id"], coverages, domain)


def _bbox_ok(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 4
            and all(isinstance(c, int) and not isinstance(c, bool) for c in value)
            and value[0] <= value[2] and value[1] <= value[3])


def _validate_rtree(idx: Any, record_count: int, pkg_dir: str) -> None:
    """校验 R 树索引结构自洽：节点形状、子项 id 引用范围。

    审计不使用空间索引，但索引损坏仍属于包结构损坏，必须拒绝而不是忽略。
    """
    what = f"包 {pkg_dir} 的 rtree.json"
    if not isinstance(idx, dict) or not isinstance(idx.get("nodes"), list):
        raise InputError(f"{what} 结构不合法")
    root_id = idx.get("root_id")
    if not isinstance(root_id, int) or isinstance(root_id, bool):
        raise InputError(f"{what} 的 root_id 必须是整数")
    nodes = idx["nodes"]
    if not nodes or not 0 <= root_id < len(nodes):
        raise InputError(f"{what} 的 root_id={root_id} 越出节点范围")
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise InputError(f"{what} 的第 {i} 个节点必须是对象")
        if not _bbox_ok(node.get("bbox")):
            raise InputError(f"{what} 的第 {i} 个节点 bbox 不合法")
        if not isinstance(node.get("leaf"), bool):
            raise InputError(f"{what} 的第 {i} 个节点 leaf 必须是布尔值")
        children = node.get("children")
        if not isinstance(children, list):
            raise InputError(f"{what} 的第 {i} 个节点 children 必须是数组")
        for child in children:
            if not isinstance(child, list) or len(child) != 2 or not _bbox_ok(child[0]):
                raise InputError(f"{what} 的第 {i} 个节点子项不合法")
            cid = child[1]
            if not isinstance(cid, int) or isinstance(cid, bool) or cid < 0:
                raise InputError(f"{what} 的第 {i} 个节点子项 id 不合法")
            limit = record_count if node["leaf"] else len(nodes)
            if cid >= limit:
                raise InputError(
                    f"{what} 的第 {i} 个节点引用了越界 id {cid}（上限 {limit}）")


def _parse_meta_domain(obj: Any, pkg_dir: str) -> ValueDomain:
    if not isinstance(obj, dict):
        raise InputError(f"包 {pkg_dir} 的 meta.json 缺少 value_domain 对象")
    try:
        domain = ValueDomain(
            reflectance_min=float(obj["reflectance_min"]),
            reflectance_max=float(obj["reflectance_max"]),
            quality_min=int(obj["quality_min"]),
            quality_max=int(obj["quality_max"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InputError(f"包 {pkg_dir} 的 value_domain 不完整或类型错误：{exc}") from exc
    if domain.reflectance_min > domain.reflectance_max \
            or domain.quality_min > domain.quality_max:
        raise InputError(f"包 {pkg_dir} 的 value_domain 下界不得大于上界")
    return domain


def _parse_metric_rect(obj: Any, what: str, size: int,
                       square: bool = True) -> Rect:
    if not isinstance(obj, dict):
        raise InputError(f"{what} 必须是对象")
    try:
        x0, y0, x1, y1 = (_require_int(obj[k], f"{what}.{k}")
                          for k in ("x0", "y0", "x1", "y1"))
    except KeyError as exc:
        raise InputError(f"{what} 缺少字段 {exc.args[0]}") from None
    rect = (x0, y0, x1, y1)
    if any(c < 0 for c in rect):
        raise InputError(f"{what} 出现负坐标：{rect}")
    if x1 <= x0 or y1 <= y0:
        raise InputError(f"{what} 必须是正面积半开矩形：{rect}")
    if square and (x1 - x0 != size or y1 - y0 != size):
        raise InputError(f"{what} 边长必须等于批次 cell_size={size}，收到 {rect}")
    if not aligned_to_grid(rect, size):
        raise InputError(f"{what} 未按 cell_size={size} 的格网对齐：{rect}")
    return rect


def _parse_meta_batches(obj: Any, pkg_dir: str) -> list:
    if not isinstance(obj, list):
        raise InputError(f"包 {pkg_dir} 的 meta.json batches 必须是数组")
    out = []
    for i, b in enumerate(obj):
        what = f"包 {pkg_dir} 的 meta.json 批次第 {i + 1} 项"
        if not isinstance(b, dict):
            raise InputError(f"{what} 必须是对象")
        bid = b.get("batch_id")
        if not isinstance(bid, str) or not bid:
            raise InputError(f"{what} 的 batch_id 必须是非空字符串")
        size = _require_int(b.get("cell_size"), f"{what} 的 cell_size")
        if not is_power_of_two(size):
            raise InputError(f"{what} 的 cell_size={size} 不是 2 的非负整数次幂")
        cov = _parse_metric_rect(b.get("coverage"), f"{what} 的 coverage", size,
                                 square=False)
        acq = b.get("acquired_at")
        if not isinstance(acq, str):
            raise InputError(f"{what} 的 acquired_at 必须是字符串")
        out.append((bid, size, cov, acq))
    return out


def _parse_package_record(line: str, line_no: int, pkg_dir: str,
                           domain: ValueDomain, batch_map: dict):
    what = f"包 {pkg_dir} 的 records.jsonl 第 {line_no} 行"
    try:
        obj = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise InputError(f"{what} 不是合法 JSON：{exc}") from exc
    if not isinstance(obj, dict):
        raise InputError(f"{what} 必须是 JSON 对象")
    bid = obj.get("batch_id")
    if not isinstance(bid, str) or not bid:
        raise InputError(f"{what} 的 batch_id 必须是非空字符串")
    if bid not in batch_map:
        raise InputError(f"{what} 引用了 meta.json 中不存在的批次 {bid!r}")
    expected_size, coverage = batch_map[bid]
    cell = obj.get("cell")
    if not isinstance(cell, dict):
        raise InputError(f"{what} 的 cell 必须是对象")
    size = _require_int(cell.get("size"), f"{what} 的 cell.size")
    if size != expected_size:
        raise InputError(
            f"{what} 的 cell.size={size} 与批次 {bid} 的 cell_size={expected_size} 不符")
    try:
        x0, y0, x1, y1 = (_require_int(cell[k], f"{what} 的 cell.{k}")
                          for k in ("x0", "y0", "x1", "y1"))
    except KeyError as exc:
        raise InputError(f"{what} 的 cell 缺少字段 {exc.args[0]}") from None
    rect = (x0, y0, x1, y1)
    if x1 - x0 != size or y1 - y0 != size:
        raise InputError(f"{what} 的 cell 边长与 size={size} 不符")
    if not aligned_to_grid(rect, size):
        raise InputError(f"{what} 的 cell 未按 size={size} 的格网对齐")
    if any(c < 0 for c in rect):
        raise InputError(f"{what} 的 cell 出现负坐标")
    if not contains(coverage, rect):
        raise InputError(f"{what} 的 cell 越出批次 {bid} 的覆盖矩形 {coverage}")

    state = obj.get("state")
    if state is None:
        state = VALID
    if state not in RECORD_STATES:
        raise InputError(f"{what} 的 state={state!r} 不合法")

    refl = obj.get("reflectance")
    qual = obj.get("quality")
    # 基本类型错误即记录结构损坏。
    if refl is not None and (isinstance(refl, bool) or not isinstance(refl, (int, float))
                             or not math.isfinite(float(refl))):
        raise InputError(f"{what} 的 reflectance 必须是有限数值或缺失，收到 {refl!r}")
    if qual is not None and (isinstance(qual, bool) or not isinstance(qual, int)):
        raise InputError(f"{what} 的 quality 必须是整数或缺失，收到 {qual!r}")
    if state == VALID:
        # valid 记录按审计通道取“有值”的那些：缺一个通道不算结构性拒收，
        # 只是该通道不计入有效记录（构建产物里 valid 总是整对齐全）。
        refl_out = float(refl) if refl is not None else None
        qual_out = qual if qual is not None else None
    else:
        # 不可用记录不得同时携带认证整对（与构建契约一致）
        if domain.reflectance_ok(refl) and domain.quality_ok(qual):
            raise InputError(
                f"{what} 的 state={state} 与认证整对同时出现，记录结构损坏")
        refl_out, qual_out = None, None

    acq = obj.get("acquired_at")
    if not isinstance(acq, str):
        raise InputError(f"{what} 的 acquired_at 必须是字符串")
    try:
        ts = _dt.datetime.fromisoformat(acq.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"{what} 的 acquired_at 不是合法 ISO 8601：{exc}") from exc
    if ts.tzinfo is None:
        raise InputError(f"{what} 的 acquired_at 必须带时区偏移")
    return SourceRecord(bid, rect, size, state, refl_out, qual_out, ts)


# ---------------- 审计计算 ----------------

def _rect_json(r: Rect) -> dict:
    return {"x0": r[0], "y0": r[1], "x1": r[2], "y1": r[3]}


def _rule_value(record, channel: str):
    return record.reflectance if channel == AUDIT_REFLECTANCE else record.quality


def audit_batch_records(records: Sequence[SourceRecord], rule: AuditRule) -> dict:
    """对单批记录应用一条规则，返回原始数值统计（不判定）。

    每个 state=valid 且该通道有值的记录最多计一次；不按赢家/覆盖/时间筛选。
    异常面积按源格边长 s 计 s²，最小包围矩形取全部异常格坐标的极值。
    """
    effective = 0
    anomalies = 0
    anomaly_area = 0
    bounds: Optional[Rect] = None
    for rec in records:
        if not rec.valid:
            continue
        value = _rule_value(rec, rule.channel)
        if value is None:
            continue
        effective += 1
        if rule.min_value <= value <= rule.max_value:
            continue
        anomalies += 1
        s = rec.size
        anomaly_area += s * s
        if bounds is None:
            bounds = rec.rect
        else:
            bounds = (min(bounds[0], rec.rect[0]), min(bounds[1], rec.rect[1]),
                      max(bounds[2], rec.rect[2]), max(bounds[3], rec.rect[3]))
    return {
        "effective_record_count": effective,
        "anomaly_record_count": anomalies,
        "anomaly_area": anomaly_area,
        "anomaly_bounds": bounds,
    }


def run_audit(config: AuditConfig) -> dict:
    """读取全部包与批次，返回完整审计报告（不做任何输出）。

    输入错误在任何输出写出之前抛出；报告覆盖配置点名的每个批次与每个包，
    排序稳定（包按路径、批次按批号、规则按配置顺序），便于自动流程逐字节比较。

    批次记录可以跨多个包聚合：同一 (batch_id, 源格矩形) 只计一次；若两份包
    对同一物理源格给出不同状态/数值，视为引用歧义（输入错误）而不是静默择一。
    """
    loaded: Dict[str, Package] = {}
    for pkg_dir in config.packages:
        loaded[os.path.abspath(pkg_dir)] = load_audit_package(pkg_dir)

    declared_packages: Dict[str, List[str]] = {}
    for pkg_dir in sorted(loaded):
        for bid, _cov in loaded[pkg_dir].coverages:
            declared_packages.setdefault(bid, []).append(pkg_dir)

    # 规则引用不合法（点名包内不存在的批次）属于输入错误，绝不静默给零统计。
    for spec in config.batches:
        if spec.batch_id not in declared_packages:
            raise InputError(
                f"审计规则引用了任何包中都未声明的批次 {spec.batch_id!r}；"
                f"包内批次为 {sorted(declared_packages)}")
        if len(declared_packages[spec.batch_id]) > 1:
            raise InputError(
                f"批次 {spec.batch_id} 同时出现在多个包 "
                f"{declared_packages[spec.batch_id]} 中，审计引用存在歧义；"
                f"请让批次只归属一个来源包")

    by_batch: Dict[str, list] = {}
    declared_records: Dict[str, int] = {}
    package_entries = []
    for pkg_dir in sorted(loaded):
        pkg = loaded[pkg_dir]
        declared = sorted(bid for bid, _cov in pkg.coverages)
        counts: Dict[str, int] = {bid: 0 for bid in declared}
        for rec in pkg.records:
            counts[rec.batch_id] = counts.get(rec.batch_id, 0) + 1
            declared_records[rec.batch_id] = declared_records.get(rec.batch_id, 0) + 1
            by_batch.setdefault(rec.batch_id, []).append(rec)
        package_entries.append({
            "path": pkg_dir,
            "record_count": len(pkg.records),
            "batch_ids": declared,
            "batch_record_counts": {bid: counts[bid] for bid in sorted(counts)},
        })

    audit_entries = []
    any_failed = False
    for spec in sorted(config.batches, key=lambda s: s.batch_id):
        # 按 (y, x) 稳定排序；同一物理源格只在单一包内出现（上面已校验）。
        records = sorted(by_batch.get(spec.batch_id, []),
                         key=lambda r: (r.rect[1], r.rect[0], r.rect[3], r.rect[2]))
        rule_entries = []
        batch_failed = False
        for rule in spec.rules:
            stats = audit_batch_records(records, rule)
            failed = stats["anomaly_record_count"] >= rule.anomaly_threshold
            batch_failed = batch_failed or failed
            bounds = stats["anomaly_bounds"]
            rule_entries.append({
                "channel": rule.channel,
                "min": rule.min_value,
                "max": rule.max_value,
                "anomaly_threshold": rule.anomaly_threshold,
                "effective_record_count": stats["effective_record_count"],
                "anomaly_record_count": stats["anomaly_record_count"],
                "anomaly_area": stats["anomaly_area"],
                "anomaly_bounds": _rect_json(bounds) if bounds is not None else None,
                "status": AUDIT_FAIL if failed else AUDIT_PASS,
            })
        if batch_failed:
            any_failed = True
        audit_entries.append({
            "batch_id": spec.batch_id,
            "source_package": declared_packages[spec.batch_id][0],
            "declared_record_count": declared_records.get(spec.batch_id, 0),
            "status": AUDIT_FAIL if batch_failed else AUDIT_PASS,
            "rules": rule_entries,
        })

    failing = [e["batch_id"] for e in audit_entries if e["status"] == AUDIT_FAIL]
    return {
        "job": REPORT_JOB,
        "schema_version": AUDIT_SCHEMA_VERSION,
        "packages": package_entries,
        "audit": audit_entries,
        "summary": {
            "package_count": len(package_entries),
            "audited_batch_count": len(audit_entries),
            "failed_batch_count": len(failing),
            "failed_batch_ids": failing,
            "status": AUDIT_FAIL if any_failed else AUDIT_PASS,
        },
    }
