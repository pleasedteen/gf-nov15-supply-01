"""终端文本渲染（JSON 输出由 --json 直接序列化，不经此模块）。"""


def render_reasons(reasons, indent=2):
    lines = []
    prefix = " " * indent
    for reason in reasons:
        lines.append(f"{prefix}- [{reason['code']}] {reason['message']}")
        for sub in reason.get("causes", []) or []:
            lines.append(f"{prefix}  * [{sub['code']}] {sub['message']}")
            for deeper in sub.get("causes", []) or []:
                lines.append(
                    f"{prefix}    > [{deeper['code']}] {deeper['message']}"
                )
    return lines


def render_wo_view(view):
    lines = []
    lines.append(
        f"工单 {view['wo_id']}  路线 {view['route_id']} 修订 "
        f"{view['route_revision']}"
    )
    lines.append(
        "交付工序: " + ", ".join(view["route"]["delivery_operations"])
    )
    lines.append("=" * 72)
    for part_view in view["parts"]:
        lines.append(f"件号 {part_view['part']}")
        for op in part_view["operations"]:
            if not op["has_result"]:
                lines.append(f"  {op['operation']}: 无结果")
                lines.extend(render_reasons(op["reasons"], indent=4))
                continue
            mark = "可用" if op["usable"] else "不可用"
            lines.append(
                f"  {op['operation']}: {mark} | 当前证据 {op['evidence_id']} | "
                f"尝试 {op['attempt']} | {'通过' if op['passed'] else '不通过'} | "
                f"{op['executed_at']}"
            )
            if op["equipment"]:
                lines.append(f"    设备: {', '.join(op['equipment'])}")
            if op["prereq_refs"]:
                refs = ", ".join(
                    f"组{k}->{v}" for k, v in sorted(op["prereq_refs"].items())
                )
                lines.append(f"    前置引用: {refs}")
            if not op["usable"]:
                lines.extend(render_reasons(op["reasons"], indent=4))
        lines.append("-" * 72)
    if view["releasable"]:
        lines.append("结论: 全部交付工序当前可用，可以办理整单放行")
    else:
        lines.append("结论: 不可整单放行，阻塞项如下")
        for item in view["blocking"]:
            lines.append(
                f"  件 {item['part']} 工序 {item['operation']}:"
            )
            lines.extend(render_reasons(item["reasons"], indent=4))
    return "\n".join(lines)


def render_attempts(attempts):
    lines = [f"共 {len(attempts)} 次尝试（按尝试号倒序，第一条为当前结果）"]
    for index, a in enumerate(attempts):
        tag = "（当前）" if index == 0 else ""
        lines.append(
            f"- 尝试 {a['attempt']}{tag} 证据 {a['evidence_id']} | "
            f"{'通过' if a['passed'] else '不通过'} | {a['executed_at']} | "
            f"设备 {a['equipment'] or '无'} | 引用 {a['prereq_refs'] or '无'}"
        )
    return "\n".join(lines)


def render_release(order):
    snapshot = order["snapshot"]
    lines = [
        f"放行单号 {order['release_id']}  状态 {order['status']}",
        f"请求编号 {order['request_id']}  工单 {order['wo_id']}",
        f"创建于 {order['created_at']}",
        f"冻结路线 {snapshot['route_id']} 修订 {snapshot['route_revision']}",
        f"交付工序 {', '.join(snapshot['delivery_operations'])}",
        f"证据闭包（{len(snapshot['evidence_closure'])} 条）:",
    ]
    for evidence_id in snapshot["evidence_closure"]:
        r = snapshot["results"][evidence_id]
        lines.append(
            f"  - {evidence_id}: {r['part']} / {r['operation']} / 尝试 "
            f"{r['attempt']} / {'通过' if r['passed'] else '不通过'} / "
            f"{r['executed_at']}"
        )
        if r["prereq_refs"]:
            lines.append(
                f"      引用 {', '.join(k + '->' + v for k, v in sorted(r['prereq_refs'].items()))}"
            )
    lines.append("设备有效期修订快照:")
    for equipment_id, rev in snapshot["equipment_revisions"].items():
        if rev is None:
            lines.append(f"  - {equipment_id}: 当时无资料")
        else:
            lines.append(f"  - {equipment_id}: 修订 {rev['revision']} 段 {rev['periods']}")
    if order["revoke_reason"]:
        lines.append("撤销原因:")
        reason = order["revoke_reason"]
        lines.append(f"  {reason['message']}（{reason['triggered_at']}）")
        for trigger in reason["triggers"]:
            lines.append(f"  - [{trigger['code']}] {trigger['message']}")
    return "\n".join(lines)
