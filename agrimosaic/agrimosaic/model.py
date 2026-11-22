"""入仓数据模型与输入校验。

批次（Batch）：批号、采集时刻、原点对齐的覆盖矩形、源格 JSONL 文件。
源格记录（SourceRecord）：全局坐标的半开方格 + 状态（有效 / 云遮 / 缺通道）。
所有拒收条件都抛 InputError，且校验与文件到达顺序无关，重复定义一律拒收。
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from .geometry import Rect, aligned_to_grid, contains, is_power_of_two

# ---- 失败原因编码（同时也是输出 JSON 中的原因码） ----
GAP_UNAVAILABLE_CLOUD = "unavailable_cloud"          # 显式云遮
GAP_UNAVAILABLE_MISSING_CHANNEL = "unavailable_missing_channel"  # 缺一个通道/越界
GAP_DECLARED_NO_RECORD = "declared_no_record"        # 覆盖内却没有任何记录
GAP_NO_DECLARED_COVERAGE = "no_declared_coverage"    # 没有任何批次声明覆盖

# 同一位置多种失败并存时的优先级：不可用记录 > 云遮细分 > 缺记录 > 无覆盖
GAP_REASON_PRIORITY = {
    GAP_UNAVAILABLE_CLOUD: 0,
    GAP_UNAVAILABLE_MISSING_CHANNEL: 1,
    GAP_DECLARED_NO_RECORD: 2,
    GAP_NO_DECLARED_COVERAGE: 3,
}

VALID = "valid"
CLOUD = "cloud"
MISSING_CHANNEL = "missing_channel"
RECORD_STATES = (VALID, CLOUD, MISSING_CHANNEL)


class InputError(ValueError):
    """输入数据违反规约（对齐、越界、重复、值域等），整批构建必须拒收。"""


@dataclass(frozen=True)
class Batch:
    batch_id: str
    acquired_at: _dt.datetime
    coverage: Rect
    source_file: str
    cell_size: int


@dataclass(frozen=True)
class SourceRecord:
    batch_id: str
    rect: Rect
    size: int
    state: str  # VALID / CLOUD / MISSING_CHANNEL
    reflectance: Optional[float] = None
    quality: Optional[int] = None
    acquired_at: _dt.datetime = field(default_factory=lambda: _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc))

    @property
    def valid(self) -> bool:
        return self.state == VALID

    def descriptor(self) -> dict:
        """输出用的物理源格出处描述。"""
        x0, y0, x1, y1 = self.rect
        return {
            "batch_id": self.batch_id,
            "acquired_at": self.acquired_at.isoformat(),
            "source_cell": {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "size": self.size},
        }


@dataclass(frozen=True)
class ValueDomain:
    """反射值与质量值的认证值域，闭区间。"""
    reflectance_min: float
    reflectance_max: float
    quality_min: int
    quality_max: int

    def reflectance_ok(self, v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and _finite(v) \
            and self.reflectance_min <= float(v) <= self.reflectance_max

    def quality_ok(self, v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) \
            and self.quality_min <= v <= self.quality_max


def _finite(v: float) -> bool:
    f = float(v)
    return f == f and f not in (float("inf"), float("-inf"))


def _require_int(v: Any, name: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise InputError(f"{name} 必须是整数，收到 {v!r}")
    return v


def _parse_rect(obj: Any, what: str) -> Rect:
    if not isinstance(obj, dict):
        raise InputError(f"{what} 必须是对象，收到 {type(obj).__name__}")
    try:
        x0, y0, x1, y1 = (_require_int(obj[k], f"{what}.{k}") for k in ("x0", "y0", "x1", "y1"))
    except KeyError as exc:
        raise InputError(f"{what} 缺少字段 {exc.args[0]}") from None
    if x1 <= x0 or y1 <= y0:
        raise InputError(f"{what} 必须是正面积半开矩形，收到 {(x0, y0, x1, y1)}")
    return (x0, y0, x1, y1)


def _parse_time(v: Any, what: str) -> _dt.datetime:
    if not isinstance(v, str):
        raise InputError(f"{what} 必须是 ISO 8601 字符串")
    try:
        t = _dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"{what} 不是合法 ISO 8601 时刻：{v!r}") from exc
    if t.tzinfo is None:
        raise InputError(f"{what} 必须带时区偏移，收到 {v!r}")
    return t


def parse_batch(obj: Any) -> Batch:
    """解析并校验批次清单中的一个批次条目。"""
    if not isinstance(obj, dict):
        raise InputError("批次条目必须是对象")
    bid = obj.get("batch_id")
    if not isinstance(bid, str) or not bid:
        raise InputError("batch_id 必须是非空字符串")
    acquired = _parse_time(obj.get("acquired_at"), f"批次 {bid} 的 acquired_at")
    coverage = _parse_rect(obj.get("coverage"), f"批次 {bid} 的 coverage")
    source_file = obj.get("source_file")
    if not isinstance(source_file, str) or not source_file:
        raise InputError(f"批次 {bid} 的 source_file 必须是非空字符串")
    size = _require_int(obj.get("cell_size"), f"批次 {bid} 的 cell_size")
    if not is_power_of_two(size):
        raise InputError(f"批次 {bid} 的 cell_size={size} 不是基本单元边长的 2 的非负整数次幂")
    if not aligned_to_grid(coverage, size):
        raise InputError(f"批次 {bid} 的 coverage {coverage} 未按 cell_size={size} 对齐")
    if any(c < 0 for c in coverage):
        raise InputError(f"批次 {bid} 的 coverage 出现负坐标")
    return Batch(bid, acquired, coverage, source_file, size)


def parse_source_line(line: str, line_no: int, batch: Batch, domain: ValueDomain) -> SourceRecord:
    """解析一行源格 JSONL 并做对齐、越界、状态与值域校验。"""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise InputError(f"{batch.source_file} 第 {line_no} 行不是合法 JSON：{exc}") from exc
    if not isinstance(obj, dict):
        raise InputError(f"{batch.source_file} 第 {line_no} 行必须是 JSON 对象")

    rect = _parse_rect(obj.get("cell"), f"{batch.source_file} 第 {line_no} 行 cell")
    if rect[2] - rect[0] != batch.cell_size or rect[3] - rect[1] != batch.cell_size:
        raise InputError(
            f"{batch.source_file} 第 {line_no} 行：源格边长与批次 cell_size={batch.cell_size} 不符"
        )
    if not aligned_to_grid(rect, batch.cell_size):
        raise InputError(f"{batch.source_file} 第 {line_no} 行：源格 {rect} 未按格网对齐")
    if any(c < 0 for c in rect):
        raise InputError(f"{batch.source_file} 第 {line_no} 行：源格出现负坐标")
    if not contains(batch.coverage, rect):
        raise InputError(
            f"{batch.source_file} 第 {line_no} 行：源格 {rect} 越出批次 {batch.batch_id} "
            f"的覆盖矩形 {batch.coverage}"
        )

    state = obj.get("state", VALID)
    if state not in RECORD_STATES:
        raise InputError(
            f"{batch.source_file} 第 {line_no} 行：state={state!r} 不合法，"
            f"允许 {RECORD_STATES} 或缺省"
        )

    refl = obj.get("reflectance")
    qual = obj.get("quality")
    refl_ok = domain.reflectance_ok(refl)
    qual_ok = domain.quality_ok(qual)
    pair_ok = refl_ok and qual_ok

    if state == CLOUD:
        # 显式云遮：不允许同时携带认证整对，状态矛盾即拒收
        if pair_ok:
            raise InputError(
                f"{batch.source_file} 第 {line_no} 行：state=cloud 与认证整对同时出现，语义矛盾"
            )
        return SourceRecord(batch.batch_id, rect, batch.cell_size, CLOUD, None, None,
                            batch.acquired_at)

    if state == MISSING_CHANNEL:
        if pair_ok:
            raise InputError(
                f"{batch.source_file} 第 {line_no} 行：state=missing_channel 却给出认证整对，"
                f"语义矛盾"
            )
        return SourceRecord(batch.batch_id, rect, batch.cell_size, MISSING_CHANNEL, None, None,
                            batch.acquired_at)

    # state 缺省或显式 valid：整对在认证值域内才有效；
    # 缺通道、非有限值或越出值域一律归入“缺一个通道”的不可用记录。
    if pair_ok:
        return SourceRecord(batch.batch_id, rect, batch.cell_size, VALID, float(refl), qual,
                            batch.acquired_at)
    return SourceRecord(batch.batch_id, rect, batch.cell_size, MISSING_CHANNEL, None, None,
                        batch.acquired_at)


def winner_key(record: SourceRecord) -> Tuple[int, int, str]:
    """赢家偏好排序键（越小越优先）：更细分辨率 → 较新采集时刻 → 字典序小批号。"""
    # 更细 = size 更小；较新 = 时间戳更大，取负时间序数实现“大时间优先”
    return (record.size, -record.acquired_at.timestamp(), record.batch_id)
