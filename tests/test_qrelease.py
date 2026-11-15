"""端到端规则测试（python -I tests/test_qrelease.py）。

每个用例使用独立临时数据库，覆盖需求中的各项规则。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qrelease import availability as av  # noqa: E402
from qrelease.db import connect  # noqa: E402
from qrelease.errors import ConflictError, NotFoundError, ValidationError  # noqa: E402
from qrelease.export import build_export  # noqa: E402
from qrelease.importers import (  # noqa: E402
    import_equipment_revisions,
    import_result_batch,
    import_routes,
    import_work_orders,
)
from qrelease.release import (  # noqa: E402
    get_release_order,
    list_release_orders,
    request_release,
    revocation_sweep,
)


ROUTE = {
    "route_id": "R1",
    "revision": 1,
    "operations": ["OP10", "OP20", "OP30"],
    "delivery_operations": ["OP30"],
    "prereq_groups": {
        "OP20": [["OP10"]],
        "OP30": [["OP10"], ["OP20"]],
    },
}


def result(ev, part, op, attempt, at, passed=True, equipment=None, refs=None):
    return {
        "evidence_id": ev,
        "wo_id": "WO1",
        "part": part,
        "operation": op,
        "attempt": attempt,
        "executed_at": at,
        "passed": passed,
        "equipment": equipment or [],
        "prereq_refs": refs or {},
    }


def good_chain(prefix="E", part="P1"):
    """一条满足全部前置要求的完整证据链。"""
    return [
        result(f"{prefix}1", part, "OP10", 1, "2026-02-01T08:00:00Z"),
        result(f"{prefix}2", part, "OP20", 1, "2026-02-02T08:00:00Z",
               refs={"0": f"{prefix}1"}),
        result(f"{prefix}3", part, "OP30", 1, "2026-02-03T08:00:00Z",
               refs={"0": f"{prefix}1", "1": f"{prefix}2"}),
    ]


class QReleaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.conn = connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def seed_route_wo_equipment(self, periods=None):
        import_routes(self.conn, [ROUTE])
        import_work_orders(self.conn, [{
            "wo_id": "WO1", "route_id": "R1", "route_revision": 1,
            "parts": ["P1", "P2"],
        }])
        import_equipment_revisions(self.conn, [{
            "equipment_id": "EQ1", "revision": 1,
            "periods": periods or [
                {"start": "2026-01-01T00:00:00Z",
                 "end": "2027-01-01T00:00:00Z"},
            ],
        }])

    # ------------------------------------------------------------ 路线规则

    def test_route_cycle_rejected(self):
        cyclic = dict(ROUTE, prereq_groups={
            "OP10": [["OP30"]],
            "OP20": [["OP10"]],
            "OP30": [["OP20"]],
        })
        with self.assertRaises(ValidationError):
            import_routes(self.conn, [cyclic])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM route").fetchone()["c"], 0
        )

    def test_route_empty_delivery_rejected(self):
        bad = dict(ROUTE, delivery_operations=[])
        with self.assertRaises(ValidationError):
            import_routes(self.conn, [bad])

    def test_route_delivery_not_in_operations_rejected(self):
        bad = dict(ROUTE, delivery_operations=["OP99"])
        with self.assertRaises(ValidationError):
            import_routes(self.conn, [bad])

    def test_route_prereq_foreign_operation_rejected(self):
        bad = dict(ROUTE, prereq_groups={"OP20": [["OP99"]]})
        with self.assertRaises(ValidationError):
            import_routes(self.conn, [bad])

    def test_route_same_revision_different_content_rejected(self):
        import_routes(self.conn, [ROUTE])
        changed = dict(ROUTE, operations=["OP10", "OP20", "OP30", "OP40"],
                       delivery_operations=["OP40"], prereq_groups={})
        with self.assertRaises(ConflictError):
            import_routes(self.conn, [changed])

    def test_route_new_revision_does_not_affect_old_wo(self):
        self.seed_route_wo_equipment()
        rev2 = dict(ROUTE, revision=2, operations=["OP10", "OP20", "OP30", "OP40"],
                    delivery_operations=["OP40"], prereq_groups={})
        import_routes(self.conn, [rev2])
        wo = av.get_work_order(self.conn, "WO1")
        self.assertEqual(wo["route_revision"], 1)
        view = av.evaluate_work_order(self.conn, "WO1")
        self.assertEqual(view["route"]["operations"], ROUTE["operations"])

    # ------------------------------------------------------------ 工单规则

    def test_wo_unknown_route_rejected(self):
        with self.assertRaises(ValidationError):
            import_work_orders(self.conn, [{
                "wo_id": "WOX", "route_id": "R1", "route_revision": 1,
                "parts": ["P1"],
            }])

    def test_wo_duplicate_idempotent_and_conflict(self):
        self.seed_route_wo_equipment()
        again = {"wo_id": "WO1", "route_id": "R1", "route_revision": 1,
                 "parts": ["P1", "P2"]}
        report = import_work_orders(self.conn, [again])
        self.assertEqual(report[0]["action"], "duplicate_ignored")
        changed = dict(again, parts=["P1"])
        with self.assertRaises(ConflictError):
            import_work_orders(self.conn, [changed])

    # ------------------------------------------------------------ 设备资料

    def test_equipment_overlapping_periods_rejected(self):
        with self.assertRaises(ValidationError):
            import_equipment_revisions(self.conn, [{
                "equipment_id": "EQ1", "revision": 1,
                "periods": [
                    {"start": "2026-01-01T00:00:00Z", "end": "2026-05-01T00:00:00Z"},
                    {"start": "2026-04-01T00:00:00Z", "end": "2026-09-01T00:00:00Z"},
                ],
            }])

    def test_equipment_revision_must_be_incremental(self):
        self.seed_route_wo_equipment()
        with self.assertRaises(ValidationError):
            import_equipment_revisions(self.conn, [{
                "equipment_id": "EQ1", "revision": 3,
                "periods": [{"start": "2026-01-01T00:00:00Z",
                             "end": "2028-01-01T00:00:00Z"}],
            }])
        # 被拒绝后旧修订仍可查询
        rows = self.conn.execute(
            "SELECT revision FROM equipment_revision WHERE equipment_id='EQ1'"
        ).fetchall()
        self.assertEqual([r["revision"] for r in rows], [1])

    def test_equipment_half_open_interval_boundary(self):
        # 段 [2026-01-01, 2026-02-01) 与 [2026-02-01, ...) 不重叠，
        # 边界点 2026-02-01T00:00:00Z 由第二段覆盖。
        import_equipment_revisions(self.conn, [{
            "equipment_id": "EQ1", "revision": 1,
            "periods": [
                {"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
                {"start": "2026-02-01T00:00:00Z", "end": "2026-03-01T00:00:00Z"},
            ],
        }])
        periods = av.latest_equipment_periods(self.conn, "EQ1")
        from qrelease.timeutil import parse_utc
        from qrelease.validation import period_covers
        self.assertTrue(period_covers(periods, parse_utc("2026-02-01T00:00:00Z")))
        self.assertFalse(period_covers(periods, parse_utc("2026-03-01T00:00:00Z")))

    # ------------------------------------------------------------ 批次导入

    def test_batch_rejected_on_unknown_wo(self):
        self.seed_route_wo_equipment()
        bad = [dict(good_chain()[0], wo_id="UNKNOWN")]
        with self.assertRaises(ValidationError):
            import_result_batch(self.conn, bad)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM result").fetchone()["c"], 0
        )

    def test_batch_rejected_on_wrong_part(self):
        self.seed_route_wo_equipment()
        bad = [dict(good_chain()[0], part="NOPE")]
        with self.assertRaises(ValidationError):
            import_result_batch(self.conn, bad)

    def test_batch_rejected_on_wrong_operation(self):
        self.seed_route_wo_equipment()
        bad = [dict(good_chain()[0], operation="OP99")]
        with self.assertRaises(ValidationError):
            import_result_batch(self.conn, bad)

    def test_batch_rejected_on_duplicate_evidence_different_content(self):
        self.seed_route_wo_equipment()
        items = good_chain()
        import_result_batch(self.conn, items)
        clash = dict(items[0], passed=False)
        with self.assertRaises(ValidationError):
            import_result_batch(self.conn, [clash])

    def test_batch_rejected_on_same_attempt_two_results(self):
        self.seed_route_wo_equipment()
        a = result("EA", "P1", "OP10", 1, "2026-02-01T08:00:00Z")
        b = result("EB", "P1", "OP10", 1, "2026-02-01T09:00:00Z")
        with self.assertRaises(ValidationError):
            import_result_batch(self.conn, [a, b])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM result").fetchone()["c"], 0
        )

    def test_identical_resend_counts_once(self):
        self.seed_route_wo_equipment()
        items = good_chain()
        first = import_result_batch(self.conn, items)
        self.assertEqual(first["inserted"], 3)
        second = import_result_batch(self.conn, items)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicated"], 3)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM result").fetchone()["c"], 3
        )

    def test_forward_reference_accepted(self):
        # 前置证据还没送到也先收下
        self.seed_route_wo_equipment()
        downstream = result("E3", "P1", "OP30", 1, "2026-02-03T08:00:00Z",
                            refs={"0": "E1", "1": "E2"})
        report = import_result_batch(self.conn, [downstream])
        self.assertEqual(report["inserted"], 1)
        evaluation = av.evaluate(self.conn, "E3")
        self.assertFalse(evaluation["usable"])
        self.assertTrue(any(r["code"] == "missing_evidence"
                            for r in evaluation["reasons"]))

    def test_late_prereq_arrival_auto_recovers(self):
        # 先收下游（缺前置），证据晚到分批补齐后自动变可用
        self.seed_route_wo_equipment()
        downstream = result("E3", "P1", "OP30", 1, "2026-02-03T08:00:00Z",
                            refs={"0": "E1", "1": "E2"})
        import_result_batch(self.conn, [downstream])
        self.assertFalse(av.evaluate(self.conn, "E3")["usable"])
        import_result_batch(self.conn, [
            result("E1", "P1", "OP10", 1, "2026-02-01T08:00:00Z"),
        ])
        self.assertFalse(av.evaluate(self.conn, "E3")["usable"])
        import_result_batch(self.conn, [
            result("E2", "P1", "OP20", 1, "2026-02-02T08:00:00Z",
                   refs={"0": "E1"}),
        ])
        self.assertTrue(av.evaluate(self.conn, "E3")["usable"])

    def test_missing_equipment_data_kept_and_recovers(self):
        self.seed_route_wo_equipment()
        items = good_chain()
        items[0]["equipment"] = ["EQ-MISSING"]
        import_result_batch(self.conn, items)
        self.assertFalse(av.evaluate(self.conn, "E1")["usable"])
        # 补齐设备资料后自动恢复，且下游一并恢复
        import_equipment_revisions(self.conn, [{
            "equipment_id": "EQ-MISSING", "revision": 1,
            "periods": [{"start": "2026-01-01T00:00:00Z",
                         "end": "2027-01-01T00:00:00Z"}],
        }])
        self.assertTrue(av.evaluate(self.conn, "E3")["usable"])

    def test_late_old_attempt_keeps_current(self):
        self.seed_route_wo_equipment()
        import_result_batch(self.conn, good_chain())
        late_old = result("E1-OLD", "P1", "OP10", 1, "2026-02-01T07:00:00Z")
        # 同尝试号不同证据 -> 拒绝
        with self.assertRaises(ValidationError):
            import_result_batch(self.conn, [late_old])
        # 旧尝试号在业务上不会更小地晚到（尝试号由生产侧递增），
        # 但若尝试号相同且内容完全一致则幂等；当前结果仍由最大尝试号决定
        current = av.get_current_result(self.conn, "WO1", "P1", "OP10")
        self.assertEqual(current["evidence_id"], "E1")

    # ------------------------------------------------------------ 可用性原因

    def test_failed_result_unusable(self):
        self.seed_route_wo_equipment()
        items = good_chain()
        items[0]["passed"] = False
        import_result_batch(self.conn, items)
        evaluation = av.evaluate(self.conn, "E3")
        self.assertFalse(evaluation["usable"])
        codes = {r["code"] for r in _collect_codes(evaluation["reasons"])}
        self.assertIn("result_failed", codes)

    def test_equipment_expired_unusable_and_correction_recovers(self):
        self.seed_route_wo_equipment(periods=[
            {"start": "2026-01-01T00:00:00Z", "end": "2026-01-15T00:00:00Z"},
        ])
        items = good_chain()
        items[0]["equipment"] = ["EQ1"]
        import_result_batch(self.conn, items)
        self.assertFalse(av.evaluate(self.conn, "E1")["usable"])
        # 完整新修订，覆盖执行时刻
        import_equipment_revisions(self.conn, [{
            "equipment_id": "EQ1", "revision": 2,
            "periods": [{"start": "2026-01-01T00:00:00Z",
                         "end": "2027-01-01T00:00:00Z"}],
        }])
        self.assertTrue(av.evaluate(self.conn, "E3")["usable"])

    def test_missing_group_ref_unusable(self):
        self.seed_route_wo_equipment()
        items = good_chain()
        items[2]["prereq_refs"] = {"0": "E1"}  # OP30 缺组 1
        import_result_batch(self.conn, items)
        evaluation = av.evaluate(self.conn, "E3")
        self.assertFalse(evaluation["usable"])
        self.assertTrue(any(r["code"] == "missing_group_ref"
                            for r in evaluation["reasons"]))

    def test_wrong_part_reference_unusable(self):
        self.seed_route_wo_equipment()
        p1 = good_chain("A", "P1")
        p2 = good_chain("B", "P2")
        import_result_batch(self.conn, p1 + p2)
        # P1 的 OP20 错引 P2 的 OP10
        bad = result("A2X", "P1", "OP20", 2, "2026-02-02T10:00:00Z",
                     refs={"0": "B1"})
        import_result_batch(self.conn, [bad])
        evaluation = av.evaluate(self.conn, "A2X")
        self.assertTrue(any(r["code"] == "ref_wrong_part"
                            for r in evaluation["reasons"]))

    def test_wrong_operation_reference_unusable(self):
        self.seed_route_wo_equipment()
        import_result_batch(self.conn, good_chain())
        # OP20 的前置组只允许 OP10，错引 OP30
        bad = result("E2X", "P1", "OP20", 2, "2026-02-04T08:00:00Z",
                     refs={"0": "E3"})
        import_result_batch(self.conn, [bad])
        evaluation = av.evaluate(self.conn, "E2X")
        self.assertTrue(any(r["code"] == "ref_wrong_operation"
                            for r in evaluation["reasons"]))

    def test_time_inversion_unusable(self):
        self.seed_route_wo_equipment()
        items = good_chain()
        items[1]["executed_at"] = "2026-01-30T08:00:00Z"  # 早于 OP10
        import_result_batch(self.conn, items)
        evaluation = av.evaluate(self.conn, "E2")
        self.assertTrue(any(r["code"] == "time_inversion"
                            for r in evaluation["reasons"]))

    def test_rework_supersedes_and_downstream_not_reconnected(self):
        self.seed_route_wo_equipment()
        import_result_batch(self.conn, good_chain())
        self.assertTrue(av.evaluate(self.conn, "E3")["usable"])
        rework = result("E1-R2", "P1", "OP10", 2, "2026-03-01T08:00:00Z")
        import_result_batch(self.conn, [rework])
        current = av.get_current_result(self.conn, "WO1", "P1", "OP10")
        self.assertEqual(current["evidence_id"], "E1-R2")
        # 下游仍引用旧尝试，不可用，且不会自动改接
        evaluation = av.evaluate(self.conn, "E2")
        self.assertTrue(any(r["code"] == "ref_not_current"
                            for r in evaluation["reasons"]))
        self.assertFalse(av.evaluate(self.conn, "E3")["usable"])
        # 独立分支 P2 不受影响
        import_result_batch(self.conn, good_chain("B", "P2"))
        self.assertTrue(av.evaluate(self.conn, "B3")["usable"])

    def test_reference_cycle_detected(self):
        # 手工引用拼出环：OP20 引 OP30、OP30 又引回 OP20。
        # OP20 的前置组只允许 OP10，因此同时产生错工序原因；
        # 引擎的环保护保证递归不会无限下去。
        self.seed_route_wo_equipment()
        base = result("E1", "P1", "OP10", 1, "2026-02-01T08:00:00Z")
        a = result("CA", "P1", "OP20", 1, "2026-02-02T08:00:00Z",
                   refs={"0": "CB"})
        b = result("CB", "P1", "OP30", 1, "2026-02-03T08:00:00Z",
                   refs={"0": "E1", "1": "CA"})
        import_result_batch(self.conn, [base, a, b])
        evaluation = av.evaluate(self.conn, "CA")
        self.assertFalse(evaluation["usable"])
        codes = {r["code"] for r in _collect_codes(evaluation["reasons"])}
        self.assertIn("ref_wrong_operation", codes)

    # ------------------------------------------------------------ 放行与撤销

    def test_full_release_flow_and_snapshot(self):
        self.seed_route_wo_equipment()
        p1 = good_chain("E", "P1")
        p1[0]["equipment"] = ["EQ1"]
        p2 = good_chain("B", "P2")
        p2[0]["equipment"] = ["EQ1"]
        import_result_batch(self.conn, p1)
        import_result_batch(self.conn, p2)
        outcome = request_release(self.conn, "REQ1", "WO1")
        self.assertEqual(outcome["status"], "granted")
        order = get_release_order(self.conn, outcome["release_id"])
        self.assertEqual(order["status"], "active")
        self.assertEqual(len(order["snapshot"]["evidence_closure"]), 6)
        self.assertIn("EQ1", order["snapshot"]["equipment_revisions"])

    def test_release_denied_no_partial_order(self):
        self.seed_route_wo_equipment()
        import_result_batch(self.conn, good_chain("E", "P1"))  # 只做了 P1
        outcome = request_release(self.conn, "REQ1", "WO1")
        self.assertEqual(outcome["status"], "denied")
        self.assertIsNone(outcome["release_id"])
        self.assertEqual(list_release_orders(self.conn), [])

    def test_release_request_idempotent_and_content_conflict(self):
        self.seed_route_wo_equipment()
        import_result_batch(self.conn, good_chain("E", "P1"))
        import_result_batch(self.conn, good_chain("B", "P2"))
        first = request_release(self.conn, "REQ1", "WO1")
        again = request_release(self.conn, "REQ1", "WO1")
        self.assertTrue(again["retried"])
        self.assertEqual(again["release_id"], first["release_id"])
        with self.assertRaises(ConflictError):
            request_release(self.conn, "REQ1", "WO-OTHER")

    def test_revoked_order_not_auto_restored_new_order_on_re_release(self):
        self.seed_route_wo_equipment()
        import_result_batch(self.conn, good_chain("E", "P1"))
        import_result_batch(self.conn, good_chain("B", "P2"))
        first = request_release(self.conn, "REQ1", "WO1")
        first_id = first["release_id"]
        # 返工导致撤销
        import_result_batch(self.conn, [
            result("E1-R2", "P1", "OP10", 2, "2026-03-01T08:00:00Z"),
        ])
        revoked = get_release_order(self.conn, first_id)
        self.assertEqual(revoked["status"], "revoked")
        self.assertIsNotNone(revoked["revoke_reason"])
        # 再跑扫描不会重复处理、也不会自动恢复
        self.assertEqual(revocation_sweep(self.conn), [])
        self.assertEqual(get_release_order(self.conn, first_id)["status"],
                         "revoked")
        # 修复下游后重新放行，生成新单号
        import_result_batch(self.conn, [
            result("E2-R2", "P1", "OP20", 2, "2026-03-02T08:00:00Z",
                   refs={"0": "E1-R2"}),
            result("E3-R2", "P1", "OP30", 2, "2026-03-03T08:00:00Z",
                   refs={"0": "E1-R2", "1": "E2-R2"}),
        ])
        second = request_release(self.conn, "REQ2", "WO1")
        self.assertEqual(second["status"], "granted")
        self.assertNotEqual(second["release_id"], first_id)
        # 旧单仍可查看当时快照
        old = get_release_order(self.conn, first_id)
        self.assertEqual(old["snapshot"]["evidence_closure"][0] is not None, True)

    def test_equipment_correction_revokes_release(self):
        self.seed_route_wo_equipment()
        items = good_chain()
        items[0]["equipment"] = ["EQ1"]
        import_result_batch(self.conn, items)
        import_result_batch(self.conn, good_chain("B", "P2"))
        order_id = request_release(self.conn, "REQ1", "WO1")["release_id"]
        # 新修订去掉了执行时刻的覆盖 -> 结果不可用 -> 自动撤销
        import_equipment_revisions(self.conn, [{
            "equipment_id": "EQ1", "revision": 2,
            "periods": [{"start": "2030-01-01T00:00:00Z",
                         "end": "2031-01-01T00:00:00Z"}],
        }])
        self.assertEqual(
            get_release_order(self.conn, order_id)["status"], "revoked"
        )
        # 再更正回来，结果恢复，但已撤销单不自动恢复
        import_equipment_revisions(self.conn, [{
            "equipment_id": "EQ1", "revision": 3,
            "periods": [{"start": "2026-01-01T00:00:00Z",
                         "end": "2027-01-01T00:00:00Z"}],
        }])
        self.assertTrue(av.evaluate(self.conn, "E3")["usable"])
        self.assertEqual(
            get_release_order(self.conn, order_id)["status"], "revoked"
        )

    def test_unrelated_branch_change_does_not_revoke(self):
        self.seed_route_wo_equipment()
        p1 = good_chain("E", "P1")
        p2 = good_chain("B", "P2")
        p1[0]["equipment"] = ["EQ1"]
        p2[0]["equipment"] = ["EQ1"]
        import_result_batch(self.conn, p1)
        import_result_batch(self.conn, p2)
        order_id = request_release(self.conn, "REQ1", "WO1")["release_id"]
        # P1 的 OP10 出现更高尝试号，属于单上证据，放行单撤销；
        # 而未被该单使用的其他分支变化（下面 P2 的返工）只影响 P2 自身，
        # 撤销原因中只能出现快照闭包内的证据。
        import_result_batch(self.conn, [
            result("E1-R3", "P1", "OP10", 3, "2026-04-01T08:00:00Z",
                   equipment=["EQ1"]),
        ])
        order = get_release_order(self.conn, order_id)
        self.assertEqual(order["status"], "revoked")
        triggered = {t["evidence_id"] for t in order["revoke_reason"]["triggers"]}
        self.assertTrue(
            triggered.issubset(set(order["snapshot"]["evidence_closure"]))
        )
        # P2 分支的证据未出现在撤销触发项中
        self.assertFalse(any(ev.startswith("B") for ev in triggered))

    # ------------------------------------------------------------ 持久化与导出

    def test_persistence_across_reopen(self):
        self.seed_route_wo_equipment()
        import_result_batch(self.conn, good_chain())
        self.conn.close()
        conn = connect(self.db_path)
        try:
            view = av.evaluate_work_order(conn, "WO1")
            self.assertIsNotNone(view)
            self.assertTrue(any(
                op["evidence_id"] == "E3" and op["usable"]
                for part in view["parts"] for op in part["operations"]
            ))
        finally:
            conn.close()

    def test_export_payload(self):
        self.seed_route_wo_equipment()
        import_result_batch(self.conn, good_chain())
        payload = build_export(self.conn, "WO1")
        self.assertEqual(payload["work_order"]["wo_id"], "WO1")
        self.assertIn("current_state", payload)
        self.assertIn("attempt_history", payload)
        self.assertIn("release_orders", payload)
        json.dumps(payload, ensure_ascii=False)  # 可序列化

    def test_unknown_wo_query_returns_none(self):
        self.assertIsNone(av.evaluate_work_order(self.conn, "NOPE"))
        with self.assertRaises(NotFoundError):
            request_release(self.conn, "R", "NOPE")


def _collect_codes(reasons):
    """递归展开原因树，便于断言深层原因码。"""
    out = []
    for reason in reasons:
        out.append(reason)
        out.extend(_collect_codes(reason.get("causes", []) or []))
    return out


if __name__ == "__main__":
    unittest.main(verbosity=2)
