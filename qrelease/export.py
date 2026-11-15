"""工单状态、不可用原因与放行记录的导出。"""

import json

from . import availability as av
from .release import list_release_orders


def build_export(conn, wo_id):
    """组装工单当前状态导出对象（纯数据，可直接 JSON 序列化）。"""
    view = av.evaluate_work_order(conn, wo_id)
    if view is None:
        return None
    history = {}
    for part_view in view["parts"]:
        for op_view in part_view["operations"]:
            attempts = av.list_attempts(conn, wo_id, part_view["part"],
                                        op_view["operation"])
            history[f"{part_view['part']}::{op_view['operation']}"] = [
                {
                    "evidence_id": a["evidence_id"],
                    "attempt": a["attempt"],
                    "executed_at": a["executed_at"],
                    "passed": a["passed"],
                    "equipment": a["equipment"],
                    "prereq_refs": a["prereq_refs"],
                    "imported_at": a["imported_at"],
                }
                for a in attempts
            ]
    return {
        "work_order": {
            "wo_id": view["wo_id"],
            "route_id": view["route_id"],
            "route_revision": view["route_revision"],
        },
        "current_state": view,
        "attempt_history": history,
        "release_orders": list_release_orders(conn, wo_id),
    }


def export_json(conn, wo_id, path):
    payload = build_export(conn, wo_id)
    if payload is None:
        raise ValueError(wo_id)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path
