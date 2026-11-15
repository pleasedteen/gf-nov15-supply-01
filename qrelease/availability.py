"""结果可用性引擎。

可用性不是存储状态，而是每次查询/放行时依据当前资料实时计算，这样：
- 晚到的前置证据、设备资料补齐后自动恢复；
- 返工产生更高尝试号后，引用旧尝试的下游自动判为不可用；
- 设备有效期修订更正后自动重新影响已有结果及其下游。

判定条件（全部满足才可用）：
1. 结果为通过；
2. 实际使用的每台设备在执行时刻都有合格有效期；
3. 路线要求的每个前置组都明确引用了一个证据号；
4. 所引证据属于同件产品、对应要求的前置工序、是当前（最大尝试号）结果、
   其本身可用，且执行时刻不晚于本结果。
"""

import json

from .timeutil import parse_utc, to_dt
from .validation import period_covers


# ---------------------------------------------------------------- 数据读取

def get_work_order(conn, wo_id):
    row = conn.execute("SELECT * FROM work_order WHERE wo_id=?", (wo_id,)).fetchone()
    if row is None:
        return None
    return dict(row)


def get_route(conn, route_id, revision):
    row = conn.execute(
        "SELECT * FROM route WHERE route_id=? AND revision=?",
        (route_id, revision),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["operations"] = json.loads(data["operations_json"])
    data["delivery_operations"] = json.loads(data["delivery_json"])
    data["prereq_groups"] = json.loads(data["prereq_groups_json"])
    return data


def get_result(conn, evidence_id):
    row = conn.execute(
        "SELECT * FROM result WHERE evidence_id=?", (evidence_id,)
    ).fetchone()
    if row is None:
        return None
    return _hydrate(dict(row))


def get_current_result(conn, wo_id, part, operation):
    """同一工单/件/工序以尝试号最大的结果为当前结果。"""
    row = conn.execute(
        "SELECT * FROM result WHERE wo_id=? AND part=? AND operation=? "
        "ORDER BY attempt DESC LIMIT 1",
        (wo_id, part, operation),
    ).fetchone()
    return _hydrate(dict(row)) if row is not None else None


def list_attempts(conn, wo_id, part, operation):
    rows = conn.execute(
        "SELECT * FROM result WHERE wo_id=? AND part=? AND operation=? "
        "ORDER BY attempt DESC, evidence_id",
        (wo_id, part, operation),
    ).fetchall()
    return [_hydrate(dict(r)) for r in rows]


def list_parts_results(conn, wo_id, part):
    rows = conn.execute(
        "SELECT * FROM result WHERE wo_id=? AND part=? ORDER BY operation, attempt DESC",
        (wo_id, part),
    ).fetchall()
    return [_hydrate(dict(r)) for r in rows]


def _hydrate(row):
    row["equipment"] = json.loads(row.pop("equipment_json"))
    row["prereq_refs"] = json.loads(row.pop("refs_json"))
    row["passed"] = bool(row["passed"])
    return row


def latest_equipment_periods(conn, equipment_id):
    """取一台设备当前最新修订的有效期段；从未登记返回 None。"""
    row = conn.execute(
        "SELECT periods_json FROM equipment_revision WHERE equipment_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (equipment_id,),
    ).fetchone()
    return json.loads(row["periods_json"]) if row is not None else None


def equipment_periods_at_revision(conn, equipment_id, revision):
    row = conn.execute(
        "SELECT periods_json FROM equipment_revision WHERE equipment_id=? AND revision=?",
        (equipment_id, revision),
    ).fetchone()
    return json.loads(row["periods_json"]) if row is not None else None


# ---------------------------------------------------------------- 可用性判定

def evaluate(conn, evidence_id, memo=None, in_progress=None):
    """递归评估一条证据的可用性。

    返回 dict：
      {evidence_id, usable: bool, reasons: [ {code,message,detail? ,causes?} ... ]}
    原因按判定顺序收集；引用类原因的 causes 中携带被引证据的评估结果，
    形成“沿引用关系导致不可用”的完整证据链。
    """
    if memo is None:
        memo = {}
    if in_progress is None:
        in_progress = set()
    if evidence_id in memo:
        return memo[evidence_id]
    if evidence_id in in_progress:
        # 引用环（路线登记时已禁止路线环，但手工引用仍可能拼出环）
        memo[evidence_id] = {
            "evidence_id": evidence_id,
            "usable": False,
            "reasons": [{
                "code": "reference_cycle",
                "message": f"证据 {evidence_id} 的前置引用形成环",
            }],
        }
        return memo[evidence_id]
    in_progress.add(evidence_id)

    result = get_result(conn, evidence_id)
    if result is None:
        memo[evidence_id] = {
            "evidence_id": evidence_id,
            "usable": False,
            "reasons": [{
                "code": "missing_evidence",
                "message": f"引用的证据 {evidence_id} 尚未送到",
            }],
        }
        in_progress.discard(evidence_id)
        return memo[evidence_id]

    reasons = []

    if not result["passed"]:
        reasons.append({
            "code": "result_failed",
            "message": f"证据 {evidence_id} 的结果为不通过（尝试号 {result['attempt']}）",
            "detail": {"attempt": result["attempt"]},
        })

    # ---- 设备合格有效期 ----
    executed = parse_utc(result["executed_at"])
    for equipment_id in result["equipment"]:
        periods = latest_equipment_periods(conn, equipment_id)
        if periods is None:
            reasons.append({
                "code": "equipment_not_registered",
                "message": f"设备 {equipment_id} 尚无合格有效期资料",
                "detail": {"equipment_id": equipment_id},
            })
        elif not period_covers(periods, executed):
            reasons.append({
                "code": "equipment_expired",
                "message": (
                    f"设备 {equipment_id} 在执行时刻 {result['executed_at']} "
                    "没有合格有效期（按最新修订）"
                ),
                "detail": {
                    "equipment_id": equipment_id,
                    "executed_at": result["executed_at"],
                },
            })

    # ---- 前置组与引用 ----
    wo = get_work_order(conn, result["wo_id"])
    route = get_route(conn, wo["route_id"], wo["route_revision"]) if wo else None
    if route is None:
        reasons.append({
            "code": "route_missing",
            "message": "工单固定的路线修订缺失，无法核对前置要求",
        })
    else:
        groups = route["prereq_groups"]
        owners = [g["__owner__"] for g in groups]
        if result["operation"] not in owners:
            if result["prereq_refs"]:
                reasons.append({
                    "code": "unexpected_refs",
                    "message": (
                        f"工序 {result['operation']} 没有前置组，"
                        "结果却携带前置引用"
                    ),
                })
        else:
            # 同一工序的组按导入顺序编号 0..n-1
            op_groups = [g for g in groups if g["__owner__"] == result["operation"]]
            refs = result["prereq_refs"]
            for index, group in enumerate(op_groups):
                key = str(index)
                if key not in refs:
                    reasons.append({
                        "code": "missing_group_ref",
                        "message": (
                            f"工序 {result['operation']} 的前置组 #{index} "
                            "未明确引用证据号"
                        ),
                        "detail": {"group_index": index,
                                   "candidates": group["candidates"]},
                    })
                    continue
                ref_id = refs[key]
                ref_reasons = _check_reference(
                    conn, result, group, ref_id, memo, in_progress
                )
                reasons.extend(ref_reasons)
            # 引用了不存在的组序号
            valid_indices = {str(i) for i in range(len(op_groups))}
            for key in refs:
                if key not in valid_indices:
                    reasons.append({
                        "code": "extra_group_ref",
                        "message": f"引用了不存在的前置组 #{key}",
                        "detail": {"group_index": key},
                    })

    usable = not reasons
    outcome = {"evidence_id": evidence_id, "usable": usable, "reasons": reasons}
    memo[evidence_id] = outcome
    in_progress.discard(evidence_id)
    return outcome


def _check_reference(conn, owner_result, group, ref_id, memo, in_progress):
    """核对一条前置引用的全部规则，返回原因列表（空列表表示通过）。"""
    reasons = []
    referenced = get_result(conn, ref_id)
    index_note = f"前置组（属主 {owner_result['operation']}，候选 {group['candidates']}）"

    if referenced is None:
        return [{
            "code": "missing_evidence",
            "message": f"{index_note} 引用的证据 {ref_id} 尚未送到",
            "detail": {"evidence_id": ref_id},
        }]

    structural = []
    if referenced["wo_id"] != owner_result["wo_id"]:
        structural.append({
            "code": "ref_wrong_wo",
            "message": f"证据 {ref_id} 属于其他工单 {referenced['wo_id']}",
            "detail": {"evidence_id": ref_id, "expected_wo": owner_result["wo_id"],
                       "actual_wo": referenced["wo_id"]},
        })
    if referenced["part"] != owner_result["part"]:
        structural.append({
            "code": "ref_wrong_part",
            "message": (
                f"证据 {ref_id} 属于件 {referenced['part']}，"
                f"与本件 {owner_result['part']} 不一致"
            ),
            "detail": {"evidence_id": ref_id, "expected_part": owner_result["part"],
                       "actual_part": referenced["part"]},
        })
    if referenced["operation"] not in group["candidates"]:
        structural.append({
            "code": "ref_wrong_operation",
            "message": (
                f"证据 {ref_id} 的工序 {referenced['operation']} 不在该前置组"
                f"候选 {group['candidates']} 内"
            ),
            "detail": {"evidence_id": ref_id,
                       "actual_operation": referenced["operation"],
                       "candidates": group["candidates"]},
        })

    if structural:
        # 结构都不对应，递归评估没有意义，直接保留并说明
        reasons.extend(structural)
        return reasons

    # 必须是该工序的当前结果（返工产生更高尝试号后旧尝试即被替代）
    current = get_current_result(
        conn, referenced["wo_id"], referenced["part"], referenced["operation"]
    )
    if current is None or current["evidence_id"] != ref_id:
        current_id = current["evidence_id"] if current else None
        reasons.append({
            "code": "ref_not_current",
            "message": (
                f"证据 {ref_id}（尝试号 {referenced['attempt']}）已被更新的尝试"
                f"（当前证据 {current_id}）替代，不能自动改接新证据"
            ),
            "detail": {"evidence_id": ref_id,
                       "superseded_by": current_id},
        })

    # 时间倒置：后工序执行时刻不得早于引用证据
    if to_dt(owner_result["executed_at"]) < to_dt(referenced["executed_at"]):
        reasons.append({
            "code": "time_inversion",
            "message": (
                f"本结果执行时刻 {owner_result['executed_at']} 早于所引证据 "
                f"{ref_id} 的执行时刻 {referenced['executed_at']}"
            ),
            "detail": {"evidence_id": ref_id,
                       "owner_at": owner_result["executed_at"],
                       "ref_at": referenced["executed_at"]},
        })

    # 被引证据本身必须可用（携带完整原因链）
    ref_eval = evaluate(conn, ref_id, memo, in_progress)
    if not ref_eval["usable"]:
        reasons.append({
            "code": "ref_unusable",
            "message": f"所引证据 {ref_id} 自身不可用",
            "detail": {"evidence_id": ref_id},
            "causes": ref_eval["reasons"],
        })
    return reasons


# ---------------------------------------------------------------- 工单级视图

def evaluate_work_order(conn, wo_id):
    """汇总工单内每件产品每道工序当前结果的可用性与原因。"""
    wo = get_work_order(conn, wo_id)
    if wo is None:
        return None
    route = get_route(conn, wo["route_id"], wo["route_revision"])
    memo = {}
    parts_view = []
    for part in json.loads(wo["parts_json"]):
        ops_view = []
        for operation in route["operations"]:
            current = get_current_result(conn, wo_id, part, operation)
            if current is None:
                ops_view.append({
                    "operation": operation,
                    "has_result": False,
                    "usable": False,
                    "reasons": [{
                        "code": "no_result",
                        "message": f"件 {part} 工序 {operation} 还没有任何结果",
                    }],
                })
                continue
            evaluation = evaluate(conn, current["evidence_id"], memo)
            ops_view.append({
                "operation": operation,
                "has_result": True,
                "evidence_id": current["evidence_id"],
                "attempt": current["attempt"],
                "executed_at": current["executed_at"],
                "passed": current["passed"],
                "equipment": current["equipment"],
                "prereq_refs": current["prereq_refs"],
                "usable": evaluation["usable"],
                "reasons": evaluation["reasons"],
            })
        parts_view.append({"part": part, "operations": ops_view})

    delivery = route["delivery_operations"]
    releasable = True
    blocking = []
    for part_view in parts_view:
        for op_view in part_view["operations"]:
            if op_view["operation"] in delivery and not op_view["usable"]:
                releasable = False
                blocking.append({
                    "part": part_view["part"],
                    "operation": op_view["operation"],
                    "reasons": op_view["reasons"],
                })
    return {
        "wo_id": wo_id,
        "route_id": wo["route_id"],
        "route_revision": wo["route_revision"],
        "route": {
            "operations": route["operations"],
            "delivery_operations": delivery,
            "prereq_groups": route["prereq_groups"],
        },
        "parts": parts_view,
        "releasable": releasable,
        "blocking": blocking,
    }
