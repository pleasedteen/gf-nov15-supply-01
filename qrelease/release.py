"""整单放行、放行快照与自动撤销。

放行成功时冻结当时采用的路线、交付结果、逐层引用的证据以及设备有效期
修订；之后只要单上使用过的证据被替代或变得不可用，就自动撤销该单。
撤销不删除任何资料，旧单仍可完整查看。
"""

import json
from datetime import datetime, timezone

from .errors import ConflictError, NotFoundError, ValidationError
from .jsonutil import dumps
from . import availability as av


def _now():
    return datetime.now(timezone.utc).isoformat()


def _next_release_id(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM release_order"
    ).fetchone()
    return f"RO-{row['c'] + 1:06d}"


def _fingerprint(wo_id):
    """放行请求的内容指纹：当前仅以工单为内容。"""
    return dumps({"wo_id": wo_id})


def _collect_closure(conn, view):
    """从每件的交付工序当前证据出发，BFS 收集逐层引用的全部证据。"""
    evidence_ids = []
    seen = set()
    queue = []
    for part_view in view["parts"]:
        for op_view in part_view["operations"]:
            if op_view["operation"] in view["route"]["delivery_operations"]:
                queue.append(op_view["evidence_id"])

    while queue:
        evidence_id = queue.pop(0)
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        result = av.get_result(conn, evidence_id)
        if result is None:
            continue
        evidence_ids.append(evidence_id)
        for ref_id in result["prereq_refs"].values():
            if ref_id not in seen:
                queue.append(ref_id)
    return evidence_ids


def _build_snapshot(conn, view, closure_ids):
    """冻结放行时刻的完整资料。"""
    equipment_used = {}
    results = {}
    for evidence_id in closure_ids:
        result = av.get_result(conn, evidence_id)
        results[evidence_id] = result
        for equipment_id in result["equipment"]:
            if equipment_id in equipment_used:
                continue
            row = conn.execute(
                "SELECT revision, periods_json FROM equipment_revision "
                "WHERE equipment_id=? ORDER BY revision DESC LIMIT 1",
                (equipment_id,),
            ).fetchone()
            if row is not None:
                equipment_used[equipment_id] = {
                    "revision": row["revision"],
                    "periods": json.loads(row["periods_json"]),
                }
            else:
                equipment_used[equipment_id] = None

    return {
        "frozen_at": _now(),
        "wo_id": view["wo_id"],
        "route_id": view["route_id"],
        "route_revision": view["route_revision"],
        "route": view["route"],
        "delivery_operations": view["route"]["delivery_operations"],
        "parts": view["parts"],
        "evidence_closure": closure_ids,
        "results": results,
        "equipment_revisions": equipment_used,
    }


def request_release(conn, request_id, wo_id):
    """办理一次整单放行（请求编号用于同号重试）。"""
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValidationError("放行请求编号必须是非空字符串")
    request_id = request_id.strip()
    if not isinstance(wo_id, str) or not wo_id.strip():
        raise ValidationError("工单号必须是非空字符串")
    wo_id = wo_id.strip()

    fingerprint = _fingerprint(wo_id)
    existing = conn.execute(
        "SELECT * FROM release_request WHERE request_id=?", (request_id,)
    ).fetchone()
    if existing is not None:
        if existing["fingerprint"] != fingerprint:
            raise ConflictError(
                f"放行请求编号 {request_id} 已用于其他内容（工单 "
                f"{json.loads(existing['fingerprint']).get('wo_id')}），拒绝"
            )
        # 同号同内容重试：沿用原结果，不重新判定、不生成新单号
        return {
            "request_id": request_id,
            "retried": True,
            "status": existing["status"],
            "release_id": existing["release_id"],
            "reason": json.loads(existing["reason_json"])
            if existing["reason_json"] else None,
        }

    if av.get_work_order(conn, wo_id) is None:
        raise NotFoundError(f"工单 {wo_id} 未登记")

    view = av.evaluate_work_order(conn, wo_id)
    with conn:
        if not view["releasable"]:
            # 失败不生成部分放行单，但保留请求记录
            reason = {
                "code": "not_releasable",
                "message": "存在不可用的交付工序，整单放行失败（不生成部分放行单）",
                "blocking": view["blocking"],
            }
            conn.execute(
                "INSERT INTO release_request (request_id, wo_id, fingerprint, "
                "status, release_id, reason_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (request_id, wo_id, fingerprint, "denied", None,
                 json.dumps(reason, ensure_ascii=False), _now()),
            )
            return {"request_id": request_id, "retried": False,
                    "status": "denied", "release_id": None, "reason": reason}

        closure_ids = _collect_closure(conn, view)
        snapshot = _build_snapshot(conn, view, closure_ids)
        release_id = _next_release_id(conn)
        conn.execute(
            "INSERT INTO release_order (release_id, request_id, wo_id, status, "
            "snapshot_json, revoke_reason_json, created_at, revoked_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (release_id, request_id, wo_id, "active",
             json.dumps(snapshot, ensure_ascii=False), None, _now(), None),
        )
        conn.execute(
            "INSERT INTO release_request (request_id, wo_id, fingerprint, "
            "status, release_id, reason_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (request_id, wo_id, fingerprint, "granted", release_id, None, _now()),
        )
        return {
            "request_id": request_id,
            "retried": False,
            "status": "granted",
            "release_id": release_id,
            "reason": None,
        }


def revocation_sweep(conn):
    """按当前资料重查所有有效放行单，撤销证据已替代/已不可用的单。

    在每次导入后自动执行，也可由 CLI 手动触发。
    没有用于该单的其他分支变化不会影响它；已撤销单不会自动恢复。
    """
    revoked = []
    active_orders = conn.execute(
        "SELECT * FROM release_order WHERE status='active'"
    ).fetchall()
    for order_row in active_orders:
        order = dict(order_row)
        snapshot = json.loads(order["snapshot_json"])
        triggers = []
        memo = {}
        for evidence_id in snapshot["evidence_closure"]:
            current = av.get_result(conn, evidence_id)
            if current is None:
                # 证据只增不删，正常不会发生；防御性处理
                triggers.append({
                    "evidence_id": evidence_id,
                    "code": "evidence_missing",
                    "message": f"证据 {evidence_id} 在当前资料中不存在",
                })
                continue
            # 证据被替代：同工单/件/工序出现更大尝试号
            latest = av.get_current_result(
                conn, current["wo_id"], current["part"], current["operation"]
            )
            if latest is not None and latest["evidence_id"] != evidence_id:
                triggers.append({
                    "evidence_id": evidence_id,
                    "code": "evidence_superseded",
                    "message": (
                        f"证据 {evidence_id}（{current['operation']} 尝试号 "
                        f"{current['attempt']}）已被 {latest['evidence_id']} 替代"
                    ),
                    "superseded_by": latest["evidence_id"],
                })
            evaluation = av.evaluate(conn, evidence_id, memo)
            if not evaluation["usable"]:
                triggers.append({
                    "evidence_id": evidence_id,
                    "code": "evidence_unusable",
                    "message": f"证据 {evidence_id} 当前不可用",
                    "reasons": evaluation["reasons"],
                })

        if triggers:
            reason = {
                "code": "auto_revoked",
                "message": "放行单使用的证据被替代或变得不可用，自动撤销",
                "triggered_at": _now(),
                "triggers": triggers,
            }
            with conn:
                conn.execute(
                    "UPDATE release_order SET status='revoked', "
                    "revoke_reason_json=?, revoked_at=? WHERE release_id=?",
                    (json.dumps(reason, ensure_ascii=False), reason["triggered_at"],
                     order["release_id"]),
                )
                conn.execute(
                    "UPDATE release_request SET status='revoked' "
                    "WHERE release_id=?",
                    (order["release_id"],),
                )
            revoked.append({"release_id": order["release_id"],
                            "wo_id": order["wo_id"], "reason": reason})
    return revoked


def get_release_order(conn, release_id):
    row = conn.execute(
        "SELECT * FROM release_order WHERE release_id=?", (release_id,)
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["snapshot"] = json.loads(data.pop("snapshot_json"))
    data["revoke_reason"] = (
        json.loads(data.pop("revoke_reason_json"))
        if data["revoke_reason_json"] else None
    )
    return data


def list_release_orders(conn, wo_id=None):
    sql = "SELECT * FROM release_order"
    params = ()
    if wo_id:
        sql += " WHERE wo_id=?"
        params = (wo_id,)
    sql += " ORDER BY created_at, release_id"
    rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        data["revoke_reason"] = (
            json.loads(data["revoke_reason_json"])
            if data["revoke_reason_json"] else None
        )
        del data["revoke_reason_json"]
        del data["snapshot_json"]
        result.append(data)
    return result
