"""资料导入：路线、工单、设备有效期修订、结果批次。

所有写入都在单个事务内完成；任何一项不合规都会抛
:class:`ValidationError`，由调用方回滚，保证“整份/整批拒绝、已有资料
保持原样”。
"""

import json
from datetime import datetime, timezone

from .errors import ConflictError, ValidationError
from .jsonutil import dumps, require, require_nonempty_str
from .release import revocation_sweep
from .timeutil import normalize, parse_utc
from .validation import (
    validate_equipment_revision,
    validate_route_document,
    validate_work_order,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


# ================================================================ 工艺路线

def import_routes(conn, documents):
    """登记一份或多份工艺路线，返回登记情况列表。

    同一 (route_id, revision) 重发且内容完全一致时幂等跳过；
    同编号同修订但内容不同则拒绝整份。
    """
    if not isinstance(documents, list):
        raise ValidationError("路线导入文件必须是路线对象的列表")
    report = []
    with conn:  # 任一失败整体回滚
        for raw in documents:
            route = validate_route_document(raw)
            row = conn.execute(
                "SELECT operations_json, delivery_json, prereq_groups_json "
                "FROM route WHERE route_id=? AND revision=?",
                (route["route_id"], route["revision"]),
            ).fetchone()
            payload = (
                dumps(route["operations"]),
                dumps(route["delivery_operations"]),
                dumps(route["prereq_groups"]),
            )
            if row is not None:
                existing = (
                    row["operations_json"],
                    row["delivery_json"],
                    row["prereq_groups_json"],
                )
                if existing != payload:
                    raise ConflictError(
                        f"路线 {route['route_id']} 修订 {route['revision']} "
                        "已登记且内容不同，不能覆盖"
                    )
                report.append(
                    {"route_id": route["route_id"], "revision": route["revision"],
                     "action": "duplicate_ignored"}
                )
                continue
            conn.execute(
                "INSERT INTO route (route_id, revision, operations_json, "
                "delivery_json, prereq_groups_json, created_at) VALUES (?,?,?,?,?,?)",
                (route["route_id"], route["revision"], payload[0], payload[1],
                 payload[2], _now()),
            )
            report.append(
                {"route_id": route["route_id"], "revision": route["revision"],
                 "action": "registered"}
            )
    return report


# ================================================================ 工单

def import_work_orders(conn, documents):
    """登记工单；工单开出时固定路线修订，之后路线改版不影响旧工单。"""
    if not isinstance(documents, list):
        raise ValidationError("工单导入文件必须是工单对象的列表")
    report = []
    with conn:
        for raw in documents:
            wo = validate_work_order(raw)
            route = conn.execute(
                "SELECT 1 FROM route WHERE route_id=? AND revision=?",
                (wo["route_id"], wo["route_revision"]),
            ).fetchone()
            if route is None:
                raise ValidationError(
                    f"工单 {wo['wo_id']} 引用的路线 {wo['route_id']} 修订 "
                    f"{wo['route_revision']} 尚未登记"
                )
            row = conn.execute(
                "SELECT route_id, route_revision, parts_json FROM work_order WHERE wo_id=?",
                (wo["wo_id"],),
            ).fetchone()
            payload = (wo["route_id"], wo["route_revision"], dumps(wo["parts"]))
            if row is not None:
                existing = (row["route_id"], row["route_revision"], row["parts_json"])
                if existing != payload:
                    raise ConflictError(
                        f"工单 {wo['wo_id']} 已登记且内容不同，不能覆盖"
                    )
                report.append({"wo_id": wo["wo_id"], "action": "duplicate_ignored"})
                continue
            conn.execute(
                "INSERT INTO work_order (wo_id, route_id, route_revision, "
                "parts_json, created_at) VALUES (?,?,?,?,?)",
                (wo["wo_id"], wo["route_id"], wo["route_revision"],
                 payload[2], _now()),
            )
            report.append({"wo_id": wo["wo_id"], "action": "registered"})
    return report


# ================================================================ 设备有效期

def import_equipment_revisions(conn, documents):
    """登记设备有效期的完整新修订。

    不合规整份拒绝，旧修订保留；同设备修订号必须严格递增。
    没有登记过的设备首份修订号必须为 1。
    """
    if not isinstance(documents, list):
        raise ValidationError("设备资料导入文件必须是对象的列表")
    report = []
    with conn:
        for raw in documents:
            rev = validate_equipment_revision(raw)
            latest = conn.execute(
                "SELECT MAX(revision) AS m FROM equipment_revision WHERE equipment_id=?",
                (rev["equipment_id"],),
            ).fetchone()["m"]
            expected = 1 if latest is None else latest + 1
            if rev["revision"] != expected:
                raise ValidationError(
                    f"设备 {rev['equipment_id']} 新修订号必须是 {expected}，"
                    f"收到 {rev['revision']}（更正须提交完整新修订）"
                )
            conn.execute(
                "INSERT INTO equipment_revision (equipment_id, revision, "
                "periods_json, created_at) VALUES (?,?,?,?)",
                (rev["equipment_id"], rev["revision"],
                 dumps(rev["periods"]), _now()),
            )
            report.append(
                {"equipment_id": rev["equipment_id"], "revision": rev["revision"],
                 "action": "registered"}
            )
    # 设备有效期更正会重新影响已有结果及其下游，导入后自动重查放行单。
    revoked = revocation_sweep(conn)
    return {"imported": report, "revoked_release_orders": revoked}


# ================================================================ 结果批次

def _canonical_result(item):
    """生成用于“同证据号同内容”比较的规范内容（不含证据号本身）。"""
    refs = item.get("prereq_refs", {})
    if refs is None:
        refs = {}
    if not isinstance(refs, dict):
        raise ValidationError("prereq_refs 必须是以组序号为键的对象")
    normalized_refs = {}
    for key, evidence_id in refs.items():
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise ValidationError(f"前置组 {key} 引用的证据号必须是非空字符串")
        normalized_refs[str(key)] = evidence_id.strip()

    equipment = item.get("equipment", [])
    if equipment is None:
        equipment = []
    if not isinstance(equipment, list):
        raise ValidationError("equipment 必须是设备编号字符串的列表（可为空）")
    equipment_norm = []
    for eq in equipment:
        if not isinstance(eq, str) or not eq.strip():
            raise ValidationError("设备编号必须是非空字符串")
        equipment_norm.append(eq.strip())

    passed = item.get("passed")
    if not isinstance(passed, bool):
        raise ValidationError("passed 必须是布尔值 true/false")

    attempt = item.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValidationError("attempt 必须是正整数")

    return {
        "wo_id": require_nonempty_str(item, "wo_id", "结果"),
        "part": require_nonempty_str(item, "part", "结果"),
        "operation": require_nonempty_str(item, "operation", "结果"),
        "attempt": attempt,
        "executed_at": normalize(require_nonempty_str(item, "executed_at", "结果")),
        "passed": passed,
        "equipment": equipment_norm,
        "prereq_refs": normalized_refs,
    }


def import_result_batch(conn, items):
    """导入一批结果（证据）。

    拒绝条件（任一命中整批拒绝、库内保持原样）：
    - 证据号相同但内容不同；
    - 同工单/件/工序的同一尝试号出现两份不同结果；
    - 工单、件号、工序未登记或互不对应；
    - 单条结果自身字段不合规。
    前置证据未到、设备资料缺失不属于拒绝条件，先收下，之后自动恢复可用性。
    """
    if not isinstance(items, list):
        raise ValidationError("结果批次必须是结果对象的列表")

    # ---- 第一阶段：结构校验 + 批内冲突预检（不写库） ----
    normalized = []
    evidence_seen = {}          # evidence_id -> canonical
    attempt_seen = {}           # (wo, part, op, attempt) -> (evidence_id, canonical)
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError(f"批次第 #{index} 条不是对象")
        evidence_id = require_nonempty_str(item, "evidence_id", "结果")
        canon = _canonical_result(item)

        if evidence_id in evidence_seen:
            if evidence_seen[evidence_id] != canon:
                raise ValidationError(
                    f"批内证据号 {evidence_id} 出现两份不同内容，整批拒绝"
                )
            # 完全重复的条目，后续去重即可
            continue
        evidence_seen[evidence_id] = canon

        key = (canon["wo_id"], canon["part"], canon["operation"], canon["attempt"])
        if key in attempt_seen:
            prev_ev, prev_canon = attempt_seen[key]
            if prev_canon != canon:
                raise ValidationError(
                    f"工单 {canon['wo_id']} 件 {canon['part']} 工序 "
                    f"{canon['operation']} 尝试号 {canon['attempt']} 出现两份"
                    f"不同结果（证据 {prev_ev} 与 {evidence_id}），整批拒绝"
                )
            continue
        attempt_seen[key] = (evidence_id, canon)
        normalized.append((evidence_id, canon))

    # ---- 第二阶段：与库内资料核对 ----
    wo_cache = {}
    route_cache = {}

    def get_wo(wo_id):
        if wo_id not in wo_cache:
            wo_cache[wo_id] = conn.execute(
                "SELECT * FROM work_order WHERE wo_id=?", (wo_id,)
            ).fetchone()
        return wo_cache[wo_id]

    def get_route(route_id, revision):
        cache_key = (route_id, revision)
        if cache_key not in route_cache:
            route_cache[cache_key] = conn.execute(
                "SELECT * FROM route WHERE route_id=? AND revision=?", cache_key
            ).fetchone()
        return route_cache[cache_key]

    for evidence_id, canon in normalized:
        wo = get_wo(canon["wo_id"])
        if wo is None:
            raise ValidationError(
                f"证据 {evidence_id} 的工单 {canon['wo_id']} 未登记，整批拒绝"
            )
        if canon["part"] not in json.loads(wo["parts_json"]):
            raise ValidationError(
                f"证据 {evidence_id} 的件号 {canon['part']} 不属于工单 "
                f"{canon['wo_id']}，整批拒绝"
            )
        route = get_route(wo["route_id"], wo["route_revision"])
        operations = json.loads(route["operations_json"])
        if canon["operation"] not in operations:
            raise ValidationError(
                f"证据 {evidence_id} 的工序 {canon['operation']} 不属于工单 "
                f"{canon['wo_id']} 固定路线 {wo['route_id']} 修订 "
                f"{wo['route_revision']}，整批拒绝"
            )

        existing = conn.execute(
            "SELECT * FROM result WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        if existing is not None:
            existing_canon = {
                "wo_id": existing["wo_id"],
                "part": existing["part"],
                "operation": existing["operation"],
                "attempt": existing["attempt"],
                "executed_at": existing["executed_at"],
                "passed": bool(existing["passed"]),
                "equipment": json.loads(existing["equipment_json"]),
                "prereq_refs": json.loads(existing["refs_json"]),
            }
            if existing_canon != canon:
                raise ValidationError(
                    f"证据号 {evidence_id} 已存在但内容不同，整批拒绝"
                )

        clash = conn.execute(
            "SELECT evidence_id FROM result WHERE wo_id=? AND part=? "
            "AND operation=? AND attempt=? AND evidence_id<>?",
            (canon["wo_id"], canon["part"], canon["operation"],
             canon["attempt"], evidence_id),
        ).fetchone()
        if clash is not None:
            raise ValidationError(
                f"工单 {canon['wo_id']} 件 {canon['part']} 工序 "
                f"{canon['operation']} 尝试号 {canon['attempt']} 已由证据 "
                f"{clash['evidence_id']} 登记且内容不同，整批拒绝"
            )

    # ---- 第三阶段：原子写入（完全重复的重发只算一次） ----
    inserted = 0
    duplicated = 0
    with conn:
        cur = conn.execute(
            "INSERT INTO import_batch (imported_at, item_count) VALUES (?,?)",
            (_now(), len(normalized)),
        )
        batch_id = cur.lastrowid
        for evidence_id, canon in normalized:
            exists = conn.execute(
                "SELECT 1 FROM result WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
            if exists:
                duplicated += 1
                continue
            conn.execute(
                "INSERT INTO result (evidence_id, wo_id, part, operation, "
                "attempt, executed_at, passed, equipment_json, refs_json, "
                "imported_at, batch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (evidence_id, canon["wo_id"], canon["part"], canon["operation"],
                 canon["attempt"], canon["executed_at"], int(canon["passed"]),
                 dumps(canon["equipment"]), dumps(canon["prereq_refs"]),
                 _now(), batch_id),
            )
            inserted += 1

    # 新证据可能替代旧尝试或改变可用性，导入后自动重查相关放行单。
    revoked = revocation_sweep(conn)
    return {"batch_id": batch_id, "inserted": inserted,
            "duplicated": duplicated, "total_in_file": len(items),
            "revoked_release_orders": revoked}
