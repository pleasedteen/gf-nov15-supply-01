"""导入资料的结构与规则校验（不依赖数据库的部分）。

校验只负责判断“这份资料本身合不合规”，与库内已有资料的比对
（修订号新旧、证据号冲突等）放在 importers 模块中完成。
"""

from .errors import ValidationError
from .jsonutil import require, require_nonempty_str
from .timeutil import parse_utc


# ---------------------------------------------------------------- 工艺路线

def validate_route_document(data):
    """路线导入文件的完整校验（基础字段 + 前置组映射 + 无环）。

    导入格式：``{"OP20": [["OP10"], ["OP11", "OP12"]], ...}``
    含义：OP20 有两个前置组，组间都要满足，每组任选一道工序作为依据。
    没有前置组的工序不出现在映射中，即可独立完成。
    """
    base = validate_route_structure(data)
    groups = normalize_prereq_groups(
        data, base["route_id"], base["operations"]
    )
    _ensure_acyclic(base["route_id"], base["operations"], groups)
    base["prereq_groups"] = groups
    return base


def validate_route_structure(data):
    """路线基础字段校验（前置组在后续步骤处理）。"""
    label = "工艺路线"
    route_id = require_nonempty_str(data, "route_id", label)
    revision = require(data, "revision", int, label)
    if isinstance(revision, bool):
        raise ValidationError("工艺路线修订号必须是整数")
    if revision < 0:
        raise ValidationError("工艺路线修订号必须是非负整数")

    operations = require(data, "operations", list, label)
    if not operations:
        raise ValidationError("工艺路线的工序列表不能为空")
    ops = []
    for op in operations:
        if not isinstance(op, str) or not op.strip():
            raise ValidationError("工序名称必须是非空字符串")
        name = op.strip()
        if name in ops:
            raise ValidationError(f"工艺路线 {route_id} 工序重复：{name}")
        ops.append(name)

    delivery = require(data, "delivery_operations", list, label)
    if not delivery:
        raise ValidationError("交付工序清单不能为空")
    delivery_ops = []
    for op in delivery:
        if not isinstance(op, str) or not op.strip():
            raise ValidationError("交付工序必须是非空字符串")
        name = op.strip()
        if name not in ops:
            raise ValidationError(f"交付工序 {name} 不属于路线 {route_id}")
        if name not in delivery_ops:
            delivery_ops.append(name)

    return {
        "route_id": route_id,
        "revision": revision,
        "operations": ops,
        "delivery_operations": delivery_ops,
    }


def normalize_prereq_groups(data, route_id, operations):
    """把导入格式的前置组规整为 [{"__owner__":..., "candidates":[...]}]。

    没有前置组的工序不出现在映射中，即可独立完成。
    """
    raw = data.get("prereq_groups", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValidationError("前置组 prereq_groups 必须是以工序名为键的对象")
    op_set = set(operations)
    normalized = []
    for owner, owner_groups in raw.items():
        if not isinstance(owner, str) or not owner.strip():
            raise ValidationError("前置组的键（属主工序）必须是非空字符串")
        owner = owner.strip()
        if owner not in op_set:
            raise ValidationError(
                f"前置组属主工序 {owner} 不属于路线 {route_id}"
            )
        if not isinstance(owner_groups, list) or not owner_groups:
            raise ValidationError(f"工序 {owner} 的前置组必须是非空列表")
        for idx, group in enumerate(owner_groups):
            if not isinstance(group, list) or not group:
                raise ValidationError(f"工序 {owner} 的前置组 #{idx} 必须是非空列表")
            candidates = []
            for op in group:
                if not isinstance(op, str) or not op.strip():
                    raise ValidationError(
                        f"工序 {owner} 前置组 #{idx} 中的工序必须是非空字符串"
                    )
                name = op.strip()
                if name not in op_set:
                    raise ValidationError(
                        f"工序 {owner} 前置组引用的 {name} 不属于路线 {route_id}"
                    )
                if name in candidates:
                    raise ValidationError(
                        f"工序 {owner} 前置组 #{idx} 内工序重复：{name}"
                    )
                candidates.append(name)
            normalized.append({"__owner__": owner, "candidates": candidates})
    return normalized


def _ensure_acyclic(route_id, operations, groups):
    """对“前置候选 -> 属主工序”构成的图做 DFS 环检测。

    一组中每个候选都可能被选作依据，因此任一候选参与构成的环都不允许。
    """
    graph = {op: set() for op in operations}
    for group in groups:
        owner = group["__owner__"]
        for candidate in group["candidates"]:
            graph[owner].add(candidate)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {op: WHITE for op in operations}

    def visit(node, stack):
        color[node] = GRAY
        for nxt in sorted(graph[node]):
            if color[nxt] == GRAY:
                raise ValidationError(
                    f"工艺路线 {route_id} 存在前置依赖环：{' -> '.join(stack + [nxt])}"
                )
            if color[nxt] == WHITE:
                visit(nxt, stack + [nxt])
        color[node] = BLACK

    for op in operations:
        if color[op] == WHITE:
            visit(op, [op])


# ---------------------------------------------------------------- 设备有效期

def validate_equipment_revision(data):
    """校验一台设备的一份完整有效期修订。"""
    label = "设备资料"
    equipment_id = require_nonempty_str(data, "equipment_id", label)
    revision = require(data, "revision", int, label)
    if isinstance(revision, bool):
        raise ValidationError("设备资料修订号必须是整数")
    if revision < 1:
        raise ValidationError("设备资料修订号必须是正整数")

    periods_raw = require(data, "periods", list, label)
    periods = []
    for idx, period in enumerate(periods_raw):
        if not isinstance(period, dict):
            raise ValidationError(f"设备 {equipment_id} 有效期段 #{idx} 必须是对象")
        start = parse_utc(period.get("start"))
        end = parse_utc(period.get("end"))
        if start >= end:
            raise ValidationError(
                f"设备 {equipment_id} 有效期段 #{idx} 起点必须早于终点（含起点、不含终点）"
            )
        periods.append({"start": start, "end": end})

    # 同台设备有效期不能重叠：按起点排序后相邻段端点检查。
    ordered = sorted(periods, key=lambda p: p["start"])
    for prev, cur in zip(ordered, ordered[1:]):
        if cur["start"] < prev["end"]:
            raise ValidationError(
                f"设备 {equipment_id} 有效期重叠：{prev['start'].isoformat()} 与 "
                f"{cur['start'].isoformat()}"
            )

    return {
        "equipment_id": equipment_id,
        "revision": revision,
        "periods": [
            {"start": p["start"].isoformat(), "end": p["end"].isoformat()}
            for p in ordered
        ],
    }


def period_covers(periods, dt):
    """判断时刻 dt（aware datetime）是否落在任一段内（含起点、不含终点）。"""
    for period in periods:
        start = parse_utc(period["start"]) if isinstance(period["start"], str) else period["start"]
        end = parse_utc(period["end"]) if isinstance(period["end"], str) else period["end"]
        if start <= dt < end:
            return True
    return False


# ---------------------------------------------------------------- 工单

def validate_work_order(data):
    """校验工单登记资料的基础结构。"""
    label = "工单"
    wo_id = require_nonempty_str(data, "wo_id", label)
    route_id = require_nonempty_str(data, "route_id", label)
    revision = require(data, "route_revision", int, label)
    if isinstance(revision, bool) or revision < 0:
        raise ValidationError("工单的路线修订号必须是非负整数")
    parts = require(data, "parts", list, label)
    if not parts:
        raise ValidationError(f"工单 {wo_id} 至少登记一个件号")
    normalized_parts = []
    for part in parts:
        if not isinstance(part, str) or not part.strip():
            raise ValidationError("件号必须是非空字符串")
        name = part.strip()
        if name in normalized_parts:
            raise ValidationError(f"工单 {wo_id} 件号重复：{name}")
        normalized_parts.append(name)
    return {
        "wo_id": wo_id,
        "route_id": route_id,
        "route_revision": revision,
        "parts": normalized_parts,
    }
