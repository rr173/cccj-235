"""测试共用工具：在内存中直接构造 Package，以及临时 JSONL 夹具。"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from typing import List, Sequence, Tuple

from agrimosaic.geometry import Rect
from agrimosaic.model import SourceRecord, ValueDomain
from agrimosaic.rtree import build_rtree
from agrimosaic.store import Package

UTC = _dt.timezone.utc
DOMAIN = ValueDomain(0.0, 1.0, 0, 100)


def ts(y: int, mo: int = 1, d: int = 1) -> _dt.datetime:
    return _dt.datetime(2026, y, mo, d, tzinfo=UTC) if False else _dt.datetime(y, mo, d, tzinfo=UTC)


def rec(batch_id: str, rect: Rect, size: int, state: str = "valid",
        refl: float | None = 0.5, qual: int | None = 50,
        when: _dt.datetime | None = None) -> SourceRecord:
    return SourceRecord(
        batch_id=batch_id, rect=rect, size=size, state=state,
        reflectance=refl if state == "valid" else None,
        quality=qual if state == "valid" else None,
        acquired_at=when or ts(2026, 1, 1),
    )


def make_package(records: Sequence[SourceRecord],
                 coverages: Sequence[Tuple[str, Rect]],
                 domain: ValueDomain | None = None) -> Package:
    boxes = [r.rect for r in records]
    nodes, root = build_rtree(boxes)
    meta = {"package_version": 1, "batches": [
        {"batch_id": bid, "coverage": {"x0": c[0], "y0": c[1], "x1": c[2], "y1": c[3]}}
        for bid, c in coverages]}
    return Package(meta=meta, records=list(records), nodes=nodes, root_id=root,
                   coverages=list(coverages), domain=domain or DOMAIN)


def write_fixture(tmp: str, name: str, lines: List[dict]) -> str:
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return path


def write_config(tmp: str, batches: List[dict], domain: dict | None = None) -> str:
    cfg = {
        "value_domain": domain or {
            "reflectance_min": 0.0, "reflectance_max": 1.0,
            "quality_min": 0, "quality_max": 100,
        },
        "batches": batches,
    }
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False)
    return path


def new_tmp() -> str:
    return tempfile.mkdtemp(prefix="agri-test-")
