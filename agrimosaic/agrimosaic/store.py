"""离线入仓：读取批次清单与 JSONL 源格，校验后构建标准格网镶嵌表。

产物为单个目录（.mosaic 包）：
  meta.json   —— 版本、值域、批次与覆盖矩形、统计信息
  records.jsonl —— 全部源格记录（按 y,x 排序，行号即 R 树叶子 id）
  rtree.json  —— STR 打包 R 树节点与根 id
任何拒收条件都会抛 InputError，不会写出半成品包。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, List, Sequence, Tuple

from . import __version__
from .geometry import Rect
from .model import Batch, InputError, SourceRecord, ValueDomain, parse_batch, parse_source_line
from .rtree import build_rtree

PACKAGE_VERSION = 1


@dataclass
class BuildConfig:
    domain: ValueDomain
    batches: Sequence[Batch]


def load_config(path: str) -> BuildConfig:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise InputError("构建配置必须是 JSON 对象")
    dom = cfg.get("value_domain")
    if not isinstance(dom, dict):
        raise InputError("配置缺少 value_domain 对象")
    try:
        domain = ValueDomain(
            reflectance_min=float(dom["reflectance_min"]),
            reflectance_max=float(dom["reflectance_max"]),
            quality_min=int(dom["quality_min"]),
            quality_max=int(dom["quality_max"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InputError(f"value_domain 不完整或类型错误：{exc}") from exc
    if domain.reflectance_min > domain.reflectance_max or domain.quality_min > domain.quality_max:
        raise InputError("value_domain 下界不得大于上界")

    raw_batches = cfg.get("batches")
    if not isinstance(raw_batches, list) or not raw_batches:
        raise InputError("配置 batches 必须是非空数组")
    batches = [parse_batch(b) for b in raw_batches]

    ids = [b.batch_id for b in batches]
    if len(set(ids)) != len(ids):
        raise InputError("批次号重复，不允许重复定义")
    return BuildConfig(domain, batches)


def _resolve(base_dir: str, source_file: str) -> str:
    return source_file if os.path.isabs(source_file) else os.path.normpath(
        os.path.join(base_dir, source_file)
    )


def read_records(config: BuildConfig, config_dir: str) -> List[SourceRecord]:
    """读取全部 JSONL，执行越界/对齐校验与物理源格去重。

    去重键为 (batch_id, rect)：同一批次对同一全局格的重复定义一律拒收，
    与文件内出现顺序无关；不同批次对同一格的记录是合法的多候选。
    """
    records: List[SourceRecord] = []
    seen: set[Tuple[str, Rect]] = set()
    for batch in config.batches:
        path = _resolve(config_dir, batch.source_file)
        try:
            fh = open(path, "r", encoding="utf-8")
        except OSError as exc:
            raise InputError(f"批次 {batch.batch_id} 的源格文件无法打开：{path}（{exc}）") from exc
        with fh:
            for line_no, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                rec = parse_source_line(line, line_no, batch, config.domain)
                key = (rec.batch_id, rec.rect)
                if key in seen:
                    raise InputError(
                        f"批次 {rec.batch_id} 对物理源格 {rec.rect} 存在重复定义，"
                        f"见 {path} 第 {line_no} 行；重复定义必须拒收"
                    )
                seen.add(key)
                records.append(rec)
    records.sort(key=lambda r: (r.rect[1], r.rect[0], r.batch_id))
    return records


def build_package(config_path: str, out_dir: str) -> dict:
    """构建镶嵌表目录，返回构建摘要。"""
    config = load_config(config_path)
    config_dir = os.path.dirname(os.path.abspath(config_path))
    records = read_records(config, config_dir)

    boxes = [r.rect for r in records]
    nodes, root_id = build_rtree(boxes)

    meta = {
        "package_version": PACKAGE_VERSION,
        "tool_version": __version__,
        "value_domain": {
            "reflectance_min": config.domain.reflectance_min,
            "reflectance_max": config.domain.reflectance_max,
            "quality_min": config.domain.quality_min,
            "quality_max": config.domain.quality_max,
        },
        "batches": [
            {
                "batch_id": b.batch_id,
                "acquired_at": b.acquired_at.isoformat(),
                "cell_size": b.cell_size,
                "coverage": {"x0": b.coverage[0], "y0": b.coverage[1],
                             "x1": b.coverage[2], "y1": b.coverage[3]},
                "source_file": b.source_file,
            }
            for b in config.batches
        ],
        "record_count": len(records),
        "stats": _stats(records),
    }

    # 原子落盘：先写临时目录再重命名，拒收时不会留下半成品
    parent = os.path.dirname(os.path.abspath(out_dir)) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".agrimosaic-", dir=parent)
    try:
        with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        with open(os.path.join(tmp, "records.jsonl"), "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(_record_to_json(r), ensure_ascii=False) + "\n")
        with open(os.path.join(tmp, "rtree.json"), "w", encoding="utf-8") as fh:
            json.dump({"root_id": root_id, "nodes": nodes}, fh, ensure_ascii=False)
        if os.path.exists(out_dir):
            raise InputError(f"输出目录已存在：{out_dir}")
        os.rename(tmp, out_dir)
    finally:
        if os.path.isdir(tmp):
            for f in os.listdir(tmp):
                os.remove(os.path.join(tmp, f))
            os.rmdir(tmp)

    return {"out_dir": out_dir, "record_count": len(records),
            "batch_count": len(config.batches), "stats": meta["stats"]}


def _stats(records: Sequence[SourceRecord]) -> dict:
    stats = {"valid": 0, "cloud": 0, "missing_channel": 0}
    sizes: dict[int, int] = {}
    for r in records:
        stats[r.state] = stats.get(r.state, 0) + 1
        sizes[r.size] = sizes.get(r.size, 0) + 1
    stats["by_cell_size"] = dict(sorted(sizes.items()))
    return stats


def _record_to_json(r: SourceRecord) -> dict:
    x0, y0, x1, y1 = r.rect
    out: dict[str, Any] = {
        "batch_id": r.batch_id,
        "acquired_at": r.acquired_at.isoformat(),
        "cell": {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "size": r.size},
        "state": r.state,
    }
    if r.state == "valid":
        out["reflectance"] = r.reflectance
        out["quality"] = r.quality
    return out


# ---------------- 读取已入仓的包 ----------------

@dataclass
class Package:
    meta: dict
    records: List[SourceRecord]
    nodes: list
    root_id: int
    coverages: List[Tuple[str, Rect]]
    domain: ValueDomain


def load_package(pkg_dir: str) -> Package:
    with open(os.path.join(pkg_dir, "meta.json"), "r", encoding="utf-8") as fh:
        meta = json.load(fh)
    if meta.get("package_version") != PACKAGE_VERSION:
        raise InputError(f"不支持的包版本：{meta.get('package_version')}")
    records: List[SourceRecord] = []
    with open(os.path.join(pkg_dir, "records.jsonl"), "r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            obj = json.loads(line)
            c = obj["cell"]
            rect = (c["x0"], c["y0"], c["x1"], c["y1"])
            import datetime as _dt
            ts = _dt.datetime.fromisoformat(obj["acquired_at"])
            records.append(SourceRecord(
                batch_id=obj["batch_id"], rect=rect, size=c["size"], state=obj["state"],
                reflectance=obj.get("reflectance"), quality=obj.get("quality"),
                acquired_at=ts,
            ))
    with open(os.path.join(pkg_dir, "rtree.json"), "r", encoding="utf-8") as fh:
        idx = json.load(fh)
    dv = meta["value_domain"]
    domain = ValueDomain(dv["reflectance_min"], dv["reflectance_max"],
                         dv["quality_min"], dv["quality_max"])
    coverages = [(b["batch_id"], (b["coverage"]["x0"], b["coverage"]["y0"],
                                  b["coverage"]["x1"], b["coverage"]["y1"]))
                 for b in meta["batches"]]
    return Package(meta, records, idx["nodes"], idx["root_id"], coverages, domain)
