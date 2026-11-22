"""纯标准库实现的 STR（Sort-Tile-Recursive）打包静态 R 树。

入仓时所有源格已知，构建一次只读索引即可；窗口查询走 R 树节点剪枝，
不需要把任何粗格展开成基本单元。节点与数据记录都以整数 id 引用。
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

from .geometry import Rect, intersects

Node = dict  # {"bbox": Rect, "children": [(bbox, id), ...]}；叶子节点 id 即记录 id
PAGE = 12  # 每个节点的最大子项数


def _bbox_of(boxes: Sequence[Tuple[Rect, int]]) -> Rect:
    return (
        min(b[0][0] for b in boxes),
        min(b[0][1] for b in boxes),
        max(b[0][2] for b in boxes),
        max(b[0][3] for b in boxes),
    )


def build_rtree(boxes: Sequence[Rect]) -> Tuple[List[Node], int]:
    """根据一组矩形构建只读 R 树，返回 (nodes, root_id)，root_id 恒为 0。

    叶子节点 children 的 id 为 boxes 的下标；内部节点 id 为 nodes 下标。
    """
    if not boxes:
        return [{"bbox": (0, 0, 0, 0), "children": [], "leaf": True}], 0

    nodes: List[Node] = []
    level: List[Tuple[Rect, int]] = [(box, i) for i, box in enumerate(boxes)]
    is_leaf = True

    while True:
        # STR：先按 y 排序切水平条带，条带内按 x 排序后分页
        items = sorted(level, key=lambda b: (b[0][1], b[0][0]))
        per_slice = max(PAGE, int(math.ceil(math.sqrt(math.ceil(len(items) / PAGE)))))
        ordered: List[Tuple[Rect, int]] = []
        for i in range(0, len(items), per_slice * PAGE):
            sl = items[i : i + per_slice * PAGE]
            ordered.extend(sorted(sl, key=lambda b: (b[0][0], b[0][1])))

        new_level: List[Tuple[Rect, int]] = []
        for i in range(0, len(ordered), PAGE):
            chunk = ordered[i : i + PAGE]
            bbox = _bbox_of(chunk)
            new_level.append((bbox, len(nodes)))
            nodes.append({"bbox": bbox, "children": list(chunk), "leaf": is_leaf})

        if len(new_level) == 1:
            root_id = new_level[0][1]
            break
        level = new_level
        is_leaf = False

    # 把根挪到下标 0，使 root_id == 0，并同步修正父节点引用
    if root_id != 0:
        nodes[0], nodes[root_id] = nodes[root_id], nodes[0]
        swap = {root_id: 0, 0: root_id}
        for node in nodes:
            if not node["leaf"]:
                node["children"] = [(b, swap.get(cid, cid)) for b, cid in node["children"]]
        root_id = 0
    return nodes, root_id


def query(nodes: Sequence[Node], root_id: int, window: Rect) -> List[int]:
    """返回 bbox 与 window 正面积相交的全部叶子记录 id。"""
    hits: List[int] = []
    stack = [root_id]
    while stack:
        node = nodes[stack.pop()]
        if not intersects(node["bbox"], window):
            continue
        if node["leaf"]:
            for bbox, rec_id in node["children"]:
                if intersects(bbox, window):
                    hits.append(rec_id)
        else:
            for bbox, child_id in node["children"]:
                if intersects(bbox, window):
                    stack.append(child_id)
    return hits
