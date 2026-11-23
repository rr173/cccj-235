"""离线增量交付：内容差分补丁的生成（diff）与应用（apply）。

中心用 ``diff`` 扫描旧包与订正后独立构建的新包，产出一份可传输的补丁文件；
县站用 ``apply`` 把补丁应用到本地旧包，在新目录得到一份标准 ``.mosaic`` 包。

补丁传的是**数据内容的变更**，不是包文件的字节差异：

* 批次以批号识别：可新增、整批撤回（只传一次批号，不逐格列清单）、
  或更新采集时间与声明覆盖范围；共有批次的格网边长不允许改变。
* 格记录以 (批号, 完整半开源格坐标) 识别：内容是状态与有效状态下的
  反射/质量整对。保留批次只传新增、删除或内容变化的格记录；
  只订正采集时间的批次一条格记录也不重传。
* 没变化的批次与格记录一律不传；补丁不夹带派生统计、R 树索引或完整包。
* 处理全程以批次/格记录为单位，不按格的基本单元面积展开。

补丁绑定旧版与新版各自的**规范化内容指纹**（见 ``content_fingerprint``），
接收端先校验本地包确实是指定的旧版，应用后再校验还原内容与新版指纹一致；
任何失败都不会改写旧包、覆盖既有目标或留下可被误认成成功的目标包。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .audit import _reject_duplicate_keys, load_audit_package
from .geometry import Rect, aligned_to_grid, contains, is_power_of_two
from .model import RECORD_STATES, VALID, InputError, SourceRecord, ValueDomain
from .store import PACKAGE_VERSION, _stats, write_package_dir

PATCH_FORMAT = "agrimosaic.patch"
PATCH_VERSION = 1

RecordKey = Tuple[str, Rect]  # (批号, 完整半开源格坐标)


# ---------------- 规范化内容模型与内容指纹 ----------------

@dataclass(frozen=True)
class BatchHeader:
    """批次的内容身份：批号、格网边长、采集时刻、声明覆盖矩形。"""
    batch_id: str
    cell_size: int
    acquired_at: _dt.datetime
    coverage: Rect


@dataclass
class PackageContent:
    """一个包的全部内容身份（不含路径、统计、索引等派生物）。"""
    package_version: int
    domain: ValueDomain
    batches: Dict[str, BatchHeader]        # 批号 -> 批次头
    records: Dict[RecordKey, SourceRecord]  # (批号, 源格) -> 记录


def _parse_iso_instant(value: Any, what: str) -> _dt.datetime:
    if not isinstance(value, str):
        raise InputError(f"{what} 必须是 ISO 8601 字符串")
    try:
        t = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"{what} 不是合法 ISO 8601 时刻：{value!r}") from exc
    if t.tzinfo is None:
        raise InputError(f"{what} 必须带时区偏移，收到 {value!r}")
    return t


def _utc_iso(t: _dt.datetime) -> str:
    """时刻的规范化表示：统一换算到 UTC 的 ISO 8601 字符串。

    等价的不同时区表示（如 +08:00 与 Z）归一到同一字符串。
    """
    return t.astimezone(_dt.timezone.utc).isoformat()


def load_package_content(pkg_dir: str) -> PackageContent:
    """严格读取一个包并还原其内容身份；结构损坏一律抛 InputError。

    在审计级只读校验（结构、对齐、值域、状态组合、索引自洽）之上，
    额外核对每条格记录冗余保存的采集时刻与所在批次一致——
    冗余时间派生自批次，不一致即视为包已损坏。
    """
    pkg = load_audit_package(pkg_dir)
    meta = pkg.meta
    batches: Dict[str, BatchHeader] = {}
    for entry in meta["batches"]:
        bid = entry["batch_id"]
        acq = _parse_iso_instant(entry["acquired_at"], f"包 {pkg_dir} 批次 {bid} 的 acquired_at")
        cov = entry["coverage"]
        batches[bid] = BatchHeader(
            batch_id=bid,
            cell_size=entry["cell_size"],
            acquired_at=acq,
            coverage=(cov["x0"], cov["y0"], cov["x1"], cov["y1"]),
        )
    records: Dict[RecordKey, SourceRecord] = {}
    for rec in pkg.records:
        header = batches[rec.batch_id]
        if rec.acquired_at != header.acquired_at:
            raise InputError(
                f"包 {pkg_dir} 已损坏：批次 {rec.batch_id} 的源格 {rec.rect} "
                f"冗余保存的采集时刻 {rec.acquired_at.isoformat()} 与批次时刻 "
                f"{header.acquired_at.isoformat()} 不一致")
        records[(rec.batch_id, rec.rect)] = rec
    return PackageContent(
        package_version=meta["package_version"],
        domain=pkg.domain,
        batches=batches,
        records=records,
    )


def _canonical_float(v: float) -> float:
    """有限数值的规范化：解析为 IEEE 双精度后即为唯一身份。

    JSON 文本里的 0.5 / 0.50 / 5e-1 解析后是同一个双精度数，
    序列化时 Python 输出最短往返表示，等价写法不会产生不同指纹。
    """
    f = float(v)
    if not math.isfinite(f):
        raise InputError(f"内容指纹不接受非有限数值：{v!r}")
    return f


def canonical_content(content: PackageContent) -> list:
    """把内容身份整理成规范化结构（排序确定、表示唯一）。"""
    dom = content.domain
    batches = [
        [b.batch_id, b.cell_size, _utc_iso(b.acquired_at), list(b.coverage)]
        for b in sorted(content.batches.values(), key=lambda b: b.batch_id)
    ]
    records = []
    for (bid, rect), rec in sorted(content.records.items(),
                                   key=lambda kv: (kv[0][0],) + kv[0][1]):
        entry: list = [bid, rect[0], rect[1], rect[2], rect[3], rec.state]
        if rec.state == VALID:
            entry.append(_canonical_float(rec.reflectance))
            entry.append(rec.quality)
        records.append(entry)
    return [
        content.package_version,
        [_canonical_float(dom.reflectance_min), _canonical_float(dom.reflectance_max),
         dom.quality_min, dom.quality_max],
        batches,
        records,
    ]


def content_fingerprint(content: PackageContent) -> str:
    """计算包的规范化内容指纹（SHA-256，带 ``sha256:`` 前缀）。

    覆盖：包版本、认证值域、全部批次身份（批号、格网边长、采集时刻、
    声明覆盖）与全部格记录身份（批号、完整半开源格坐标、状态、有效整对）
    ——包括从未胜出的格与零记录的空批次。

    不覆盖：输入目录与 source_file 路径、成员排列、JSON 排版、统计字段、
    索引布局；等价的时区表示与有限反射数值写法归一后也不会改变指纹。
    """
    canon = canonical_content(content)
    text = json.dumps(canon, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------- 补丁生成（diff） ----------------

def _coverage_json(rect: Rect) -> dict:
    return {"x0": rect[0], "y0": rect[1], "x1": rect[2], "y1": rect[3]}


def _record_entry(rec: SourceRecord) -> dict:
    """格记录内容变化的补丁条目：状态 + 有效状态下的反射/质量整对。"""
    x0, y0, x1, y1 = rec.rect
    entry: Dict[str, Any] = {
        "batch_id": rec.batch_id,
        "cell": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
        "state": rec.state,
    }
    if rec.state == VALID:
        entry["reflectance"] = _canonical_float(rec.reflectance)
        entry["quality"] = rec.quality
    return entry


def _record_content_equal(a: SourceRecord, b: SourceRecord) -> bool:
    """格记录内容是否一致（状态与有效整对；冗余时间派生自批次，不参与）。"""
    if a.state != b.state:
        return False
    if a.state == VALID:
        return _canonical_float(a.reflectance) == _canonical_float(b.reflectance) \
            and a.quality == b.quality
    return True


def make_patch(old_dir: str, new_dir: str) -> dict:
    """扫描旧包与新包，生成内容差分补丁（不修改任何输入）。

    两包必须同为受支持的包版本、认证值域相同，且共有批次的格网边长不变，
    否则拒收（该情形超出补丁模型的表达能力）。
    """
    old = load_package_content(old_dir)
    new = load_package_content(new_dir)
    if old.package_version != new.package_version:
        raise InputError(
            f"两包包版本不一致：旧 {old.package_version}，新 {new.package_version}")
    if old.domain != new.domain:
        raise InputError("两版认证值域不同，无法用内容补丁交付")

    batch_additions: List[dict] = []
    batch_removals: List[str] = []
    batch_updates: List[dict] = []
    record_upserts: List[dict] = []
    record_deletions: List[dict] = []

    for bid in sorted(new.batches):
        header = new.batches[bid]
        if bid not in old.batches:
            batch_additions.append({
                "batch_id": bid,
                "cell_size": header.cell_size,
                "acquired_at": _utc_iso(header.acquired_at),
                "coverage": _coverage_json(header.coverage),
            })
            continue
        prev = old.batches[bid]
        if prev.cell_size != header.cell_size:
            raise InputError(
                f"共有批次 {bid} 的格网边长改变（{prev.cell_size} -> "
                f"{header.cell_size}），超出补丁模型的表达能力")
        if prev.acquired_at != header.acquired_at or prev.coverage != header.coverage:
            batch_updates.append({
                "batch_id": bid,
                "acquired_at": _utc_iso(header.acquired_at),
                "coverage": _coverage_json(header.coverage),
            })

    for bid in sorted(old.batches):
        if bid not in new.batches:
            # 整批撤回：只传一次批号，不逐格列清单
            batch_removals.append(bid)

    # 格记录：保留批次只传新增、删除或内容变化的；新增批次的全部记录都是新增。
    # 记录内容只含状态与有效整对，批次仅订正时间时这里不会产生任何条目。
    for bid in sorted(new.batches):
        if bid in old.batches:
            continue
        added = [kv for kv in new.records.items() if kv[0][0] == bid]
        for _key, rec in sorted(added, key=lambda kv: kv[0][1]):
            record_upserts.append(_record_entry(rec))

    for bid in sorted(old.batches):
        if bid not in new.batches:
            continue
        old_keys = {k for k in old.records if k[0] == bid}
        new_keys = {k for k in new.records if k[0] == bid}
        for key in sorted(old_keys - new_keys, key=lambda k: k[1]):
            record_deletions.append({
                "batch_id": bid,
                "cell": _coverage_json(key[1]),
            })
        for key in sorted(new_keys - old_keys, key=lambda k: k[1]):
            record_upserts.append(_record_entry(new.records[key]))
        for key in sorted(old_keys & new_keys, key=lambda k: k[1]):
            if not _record_content_equal(old.records[key], new.records[key]):
                record_upserts.append(_record_entry(new.records[key]))

    return {
        "patch_format": PATCH_FORMAT,
        "patch_version": PATCH_VERSION,
        "package_version": old.package_version,
        "base_fingerprint": content_fingerprint(old),
        "target_fingerprint": content_fingerprint(new),
        "batch_additions": batch_additions,
        "batch_removals": batch_removals,
        "batch_updates": batch_updates,
        "record_upserts": record_upserts,
        "record_deletions": record_deletions,
    }


def patch_to_json(patch: dict) -> str:
    """补丁的稳定序列化：同样的逻辑输入生成逐字节相同的补丁。"""
    return json.dumps(patch, ensure_ascii=False, indent=2) + "\n"


# ---------------- 补丁解析与校验 ----------------

_PATCH_KEYS = {"patch_format", "patch_version", "package_version",
               "base_fingerprint", "target_fingerprint",
               "batch_additions", "batch_removals", "batch_updates",
               "record_upserts", "record_deletions"}


def _require_int(v: Any, name: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise InputError(f"{name} 必须是整数，收到 {v!r}")
    return v


def _require_rect(obj: Any, what: str) -> Rect:
    if not isinstance(obj, dict):
        raise InputError(f"{what} 必须是对象，收到 {type(obj).__name__}")
    unknown = set(obj) - {"x0", "y0", "x1", "y1"}
    if unknown:
        raise InputError(f"{what} 含有未知字段：{sorted(unknown)}")
    try:
        rect = tuple(_require_int(obj[k], f"{what}.{k}") for k in ("x0", "y0", "x1", "y1"))
    except KeyError as exc:
        raise InputError(f"{what} 缺少字段 {exc.args[0]}") from None
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        raise InputError(f"{what} 必须是正面积半开矩形，收到 {rect}")
    if any(c < 0 for c in rect):
        raise InputError(f"{what} 出现负坐标：{rect}")
    return rect  # type: ignore[return-value]


def _require_batch_id(v: Any, what: str) -> str:
    if not isinstance(v, str) or not v:
        raise InputError(f"{what} 必须是非空字符串批号")
    return v


@dataclass(frozen=True)
class ParsedPatch:
    package_version: int
    base_fingerprint: str
    target_fingerprint: str
    batch_additions: Tuple[dict, ...]
    batch_removals: Tuple[str, ...]
    batch_updates: Tuple[dict, ...]
    record_upserts: Tuple[dict, ...]
    record_deletions: Tuple[dict, ...]


def _require_fingerprint(v: Any, what: str) -> str:
    if not isinstance(v, str) or not v.startswith("sha256:") or len(v) != 71:
        raise InputError(f"{what} 必须是 sha256:<64 位十六进制> 形式的指纹")
    return v


def parse_patch(path: str) -> ParsedPatch:
    """读取并严格校验补丁文件结构；数组列出顺序不影响后续应用结果。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh, object_pairs_hook=_reject_duplicate_keys)
    except OSError as exc:
        raise InputError(f"补丁文件无法打开：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"补丁文件不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(obj, dict):
        raise InputError("补丁必须是 JSON 对象")
    unknown = set(obj) - _PATCH_KEYS
    if unknown:
        raise InputError(f"补丁含有未知字段：{sorted(unknown)}")
    if obj.get("patch_format") != PATCH_FORMAT:
        raise InputError(f"补丁格式标识不符：{obj.get('patch_format')!r}")
    if obj.get("patch_version") != PATCH_VERSION:
        raise InputError(f"不支持的补丁格式版本：{obj.get('patch_version')!r}")
    package_version = obj.get("package_version")
    if package_version != PACKAGE_VERSION:
        raise InputError(f"补丁针对的包版本不受支持：{package_version!r}")

    def _entries(name: str) -> List[dict]:
        raw = obj.get(name)
        if not isinstance(raw, list):
            raise InputError(f"补丁的 {name} 必须是数组")
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise InputError(f"补丁的 {name} 第 {i + 1} 项必须是对象")
        return raw

    additions = []
    for i, entry in enumerate(_entries("batch_additions")):
        what = f"batch_additions 第 {i + 1} 项"
        if set(entry) != {"batch_id", "cell_size", "acquired_at", "coverage"}:
            raise InputError(f"{what} 字段必须是 batch_id/cell_size/acquired_at/coverage")
        bid = _require_batch_id(entry["batch_id"], what)
        size = _require_int(entry["cell_size"], f"{what} 的 cell_size")
        if not is_power_of_two(size):
            raise InputError(f"{what} 的 cell_size={size} 不是 2 的非负整数次幂")
        acq = _parse_iso_instant(entry["acquired_at"], f"{what} 的 acquired_at")
        cov = _require_rect(entry["coverage"], f"{what} 的 coverage")
        if not aligned_to_grid(cov, size):
            raise InputError(f"{what} 的 coverage {cov} 未按 cell_size={size} 对齐")
        additions.append({"batch_id": bid, "cell_size": size,
                          "acquired_at": acq, "coverage": cov})

    removals_raw = obj.get("batch_removals")
    if not isinstance(removals_raw, list):
        raise InputError("补丁的 batch_removals 必须是数组")
    removals = [_require_batch_id(v, f"batch_removals 第 {i + 1} 项")
                for i, v in enumerate(removals_raw)]

    updates = []
    for i, entry in enumerate(_entries("batch_updates")):
        what = f"batch_updates 第 {i + 1} 项"
        if set(entry) != {"batch_id", "acquired_at", "coverage"}:
            raise InputError(f"{what} 字段必须是 batch_id/acquired_at/coverage")
        bid = _require_batch_id(entry["batch_id"], what)
        acq = _parse_iso_instant(entry["acquired_at"], f"{what} 的 acquired_at")
        cov = _require_rect(entry["coverage"], f"{what} 的 coverage")
        updates.append({"batch_id": bid, "acquired_at": acq, "coverage": cov})

    upserts = []
    for i, entry in enumerate(_entries("record_upserts")):
        what = f"record_upserts 第 {i + 1} 项"
        unknown_keys = set(entry) - {"batch_id", "cell", "state", "reflectance", "quality"}
        if unknown_keys:
            raise InputError(f"{what} 含有未知字段：{sorted(unknown_keys)}")
        bid = _require_batch_id(entry.get("batch_id"), what)
        cell = _require_rect(entry.get("cell"), f"{what} 的 cell")
        state = entry.get("state")
        if state not in RECORD_STATES:
            raise InputError(f"{what} 的 state={state!r} 不合法，允许 {RECORD_STATES}")
        refl, qual = entry.get("reflectance"), entry.get("quality")
        if state == VALID:
            if isinstance(refl, bool) or not isinstance(refl, (int, float)) \
                    or not math.isfinite(float(refl)):
                raise InputError(f"{what} 的 valid 记录必须携带有限反射值")
            if isinstance(qual, bool) or not isinstance(qual, int):
                raise InputError(f"{what} 的 valid 记录必须携带整数质量值")
            upserts.append({"batch_id": bid, "cell": cell, "state": state,
                            "reflectance": float(refl), "quality": qual})
        else:
            if refl is not None or qual is not None:
                raise InputError(
                    f"{what} 的 state={state} 记录不得携带反射/质量值")
            upserts.append({"batch_id": bid, "cell": cell, "state": state,
                            "reflectance": None, "quality": None})

    deletions = []
    for i, entry in enumerate(_entries("record_deletions")):
        what = f"record_deletions 第 {i + 1} 项"
        if set(entry) != {"batch_id", "cell"}:
            raise InputError(f"{what} 字段必须是 batch_id/cell")
        bid = _require_batch_id(entry["batch_id"], what)
        cell = _require_rect(entry["cell"], f"{what} 的 cell")
        deletions.append({"batch_id": bid, "cell": cell})

    return ParsedPatch(
        package_version=package_version,
        base_fingerprint=_require_fingerprint(obj.get("base_fingerprint"), "base_fingerprint"),
        target_fingerprint=_require_fingerprint(obj.get("target_fingerprint"),
                                                "target_fingerprint"),
        batch_additions=tuple(additions),
        batch_removals=tuple(removals),
        batch_updates=tuple(updates),
        record_upserts=tuple(upserts),
        record_deletions=tuple(deletions),
    )


# ---------------- 补丁应用（apply） ----------------

def _check_no_conflicts(patch: ParsedPatch, old: PackageContent) -> None:
    """拒绝重复或冲突的操作、引用不存在的批次/删除对象（与数组顺序无关）。"""
    additions = [e["batch_id"] for e in patch.batch_additions]
    removals = list(patch.batch_removals)
    updates = [e["batch_id"] for e in patch.batch_updates]
    for name, ids in (("batch_additions", additions), ("batch_removals", removals),
                      ("batch_updates", updates)):
        if len(set(ids)) != len(ids):
            raise InputError(f"补丁的 {name} 对同一批次出现重复操作")
    overlap = set(additions) & set(removals) | set(additions) & set(updates) \
        | set(removals) & set(updates)
    if overlap:
        raise InputError(f"补丁对批次 {sorted(overlap)} 存在冲突操作")

    for bid in additions:
        if bid in old.batches:
            raise InputError(f"补丁要新增的批次 {bid} 在本地旧包中已存在")
    for bid in removals + updates:
        if bid not in old.batches:
            raise InputError(f"补丁引用了本地旧包中不存在的批次 {bid}")
    for entry in patch.batch_updates:
        prev = old.batches[entry["batch_id"]]
        if prev.acquired_at == entry["acquired_at"] and prev.coverage == entry["coverage"]:
            raise InputError(
                f"补丁对批次 {entry['batch_id']} 的订正没有任何变化，属于冗余操作")

    removed = set(removals)
    added = set(additions)
    upsert_keys: set = set()
    for entry in patch.record_upserts:
        bid, cell = entry["batch_id"], entry["cell"]
        if bid in removed:
            raise InputError(f"格记录操作指向已整批撤回的批次 {bid}，操作冲突")
        if bid not in old.batches and bid not in added:
            raise InputError(f"格记录操作引用了不存在的批次 {bid}")
        key = (bid, cell)
        if key in upsert_keys:
            raise InputError(f"补丁对格 {key} 存在重复的写入操作")
        upsert_keys.add(key)

    deletion_keys: set = set()
    for entry in patch.record_deletions:
        bid, cell = entry["batch_id"], entry["cell"]
        if bid in removed or bid in added:
            raise InputError(f"格记录删除指向被撤回或新增的批次 {bid}，操作冲突")
        if bid not in old.batches:
            raise InputError(f"格记录删除引用了不存在的批次 {bid}")
        key = (bid, cell)
        if key in upsert_keys:
            raise InputError(f"补丁对格 {key} 同时写入又删除，操作冲突")
        if key in deletion_keys:
            raise InputError(f"补丁对格 {key} 存在重复的删除操作")
        deletion_keys.add(key)
        if key not in old.records:
            raise InputError(f"补丁要删除的格 {key} 在本地旧包中不存在")


def apply_patch(pkg_dir: str, patch_path: str, out_dir: str) -> dict:
    """把补丁应用到本地旧包，在新目录产出标准 .mosaic 包，返回摘要。

    依次校验：本地包确为补丁指定的旧版（base 指纹）→ 操作无重复/冲突、
    引用存在 → 还原内容合法（格落在批次最终覆盖内、状态值组合合法）→
    还原内容与新版指纹一致。任何一步失败都不改写旧包、不覆盖既有目标、
    不留下可被误认成成功的目标目录。
    """
    patch = parse_patch(patch_path)
    old = load_package_content(pkg_dir)
    if content_fingerprint(old) != patch.base_fingerprint:
        raise InputError(
            "本地包的内容指纹与补丁指定的旧版指纹不一致，拒绝应用"
            "（本地包可能不是补丁针对的旧版，或隐藏记录不同）")

    _check_no_conflicts(patch, old)

    # 1) 批次头变更：撤回、新增、订正时间/覆盖
    batches: Dict[str, BatchHeader] = dict(old.batches)
    for bid in patch.batch_removals:
        del batches[bid]
    for entry in patch.batch_additions:
        batches[entry["batch_id"]] = BatchHeader(
            batch_id=entry["batch_id"], cell_size=entry["cell_size"],
            acquired_at=entry["acquired_at"], coverage=entry["coverage"])
    for entry in patch.batch_updates:
        prev = batches[entry["batch_id"]]
        batches[entry["batch_id"]] = BatchHeader(
            batch_id=prev.batch_id, cell_size=prev.cell_size,
            acquired_at=entry["acquired_at"], coverage=entry["coverage"])

    # 2) 格记录变更：整批撤回连带删除、逐格删除、写入（新增或内容变化）
    removed_batches = set(patch.batch_removals)
    records: Dict[RecordKey, SourceRecord] = {
        key: rec for key, rec in old.records.items()
        if key[0] not in removed_batches
    }
    for entry in patch.record_deletions:
        del records[(entry["batch_id"], entry["cell"])]
    for entry in patch.record_upserts:
        bid, cell = entry["batch_id"], entry["cell"]
        header = batches[bid]
        key = (bid, cell)
        new_rec = SourceRecord(
            batch_id=bid, rect=cell, size=header.cell_size, state=entry["state"],
            reflectance=entry["reflectance"], quality=entry["quality"],
            acquired_at=header.acquired_at)
        existing = records.get(key)
        if existing is not None and _record_content_equal(existing, new_rec):
            raise InputError(f"补丁对格 {key} 的写入与现有内容完全相同，属于冗余操作")
        records[key] = new_rec

    # 3) 最终包合规校验：批次声明覆盖按格网对齐；每格落在批次最终声明覆盖内、
    #    边长与格网合法、有效整对在认证值域内；越界的格一律报错，绝不默默丢弃。
    domain = old.domain
    for header in batches.values():
        if not aligned_to_grid(header.coverage, header.cell_size):
            raise InputError(
                f"还原内容非法：批次 {header.batch_id} 的声明覆盖 "
                f"{header.coverage} 未按 cell_size={header.cell_size} 对齐")
    for (bid, rect), rec in records.items():
        header = batches[bid]
        if rect[2] - rect[0] != header.cell_size or rect[3] - rect[1] != header.cell_size:
            raise InputError(
                f"还原内容非法：批次 {bid} 的格 {rect} 边长与 cell_size="
                f"{header.cell_size} 不符")
        if not aligned_to_grid(rect, header.cell_size):
            raise InputError(f"还原内容非法：批次 {bid} 的格 {rect} 未按格网对齐")
        if not contains(header.coverage, rect):
            raise InputError(
                f"还原内容非法：批次 {bid} 的格 {rect} 越出该批次最终声明覆盖 "
                f"{header.coverage}（补丁必须同时删除或移动越界格，不能默默丢弃）")
        if rec.state == VALID and not (
                domain.reflectance_ok(rec.reflectance) and domain.quality_ok(rec.quality)):
            raise InputError(
                f"还原内容非法：批次 {bid} 的格 {rect} 的有效整对越出认证值域")

    # 4) 批次时间订正后，该批全部格记录冗余保存的采集时刻随之更新，
    #    哪怕该格没有出现在补丁里。
    final_records: List[SourceRecord] = []
    for (bid, _rect), rec in records.items():
        header = batches[bid]
        if rec.acquired_at != header.acquired_at:
            rec = SourceRecord(bid, rec.rect, rec.size, rec.state,
                               rec.reflectance, rec.quality, header.acquired_at)
        final_records.append(rec)

    # 5) 还原内容必须与新版指纹一致，否则拒绝（不写出任何内容）。
    final_content = PackageContent(old.package_version, domain, batches,
                                   {(r.batch_id, r.rect): r for r in final_records})
    fingerprint = content_fingerprint(final_content)
    if fingerprint != patch.target_fingerprint:
        raise InputError(
            "还原内容的内容指纹与补丁指定的新版指纹不一致，拒绝产出目标包")

    # 6) 派生统计与索引随输出记录重建，原子落盘；目标已存在则拒收。
    #    source_file 不是内容身份，补丁不传输：保留批次沿用旧包原值，
    #    新增批次在接收端没有源文件，记为 null（现有命令均不读取该字段）。
    old_source_files = _old_source_files(pkg_dir)
    meta = {
        "package_version": old.package_version,
        "tool_version": __version__,
        "value_domain": {
            "reflectance_min": domain.reflectance_min,
            "reflectance_max": domain.reflectance_max,
            "quality_min": domain.quality_min,
            "quality_max": domain.quality_max,
        },
        "batches": [
            {
                "batch_id": b.batch_id,
                "acquired_at": b.acquired_at.isoformat(),
                "cell_size": b.cell_size,
                "coverage": _coverage_json(b.coverage),
                "source_file": old_source_files.get(b.batch_id),
            }
            for b in sorted(batches.values(), key=lambda h: h.batch_id)
        ],
        "record_count": len(final_records),
        "stats": _stats(final_records),
    }
    write_package_dir(out_dir, meta, final_records)
    return {
        "out_dir": out_dir,
        "record_count": len(final_records),
        "batch_count": len(batches),
        "fingerprint": fingerprint,
        "stats": meta["stats"],
    }


def _old_source_files(pkg_dir: str) -> Dict[str, Optional[str]]:
    """旧包 meta 里各批次的 source_file 原值（非字符串一律视为 null）。"""
    with open(os.path.join(pkg_dir, "meta.json"), "r", encoding="utf-8") as fh:
        meta = json.load(fh)
    batches = meta.get("batches")
    out: Dict[str, Optional[str]] = {}
    if isinstance(batches, list):
        for entry in batches:
            if isinstance(entry, dict) and isinstance(entry.get("batch_id"), str):
                value = entry.get("source_file")
                out[entry["batch_id"]] = value if isinstance(value, str) else None
    return out
