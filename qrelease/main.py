"""命令行入口。

通用参数：
  --db PATH   SQLite 数据库文件位置（默认 ./qrelease.db，也可用
              QRELEASE_DB 环境变量），纯本地文件，离线可用。
导入类命令接受一个 JSON 文件；查询类命令默认文本输出，加 --json 输出
机器可读 JSON。
"""

import argparse
import json
import os
import sys

from . import availability as av
from .db import connect
from .errors import ConflictError, NotFoundError, QReleaseError, ValidationError
from .export import build_export, export_json
from .importers import (
    import_equipment_revisions,
    import_result_batch,
    import_routes,
    import_work_orders,
)
from .release import (
    get_release_order,
    list_release_orders,
    request_release,
    revocation_sweep,
)
from .render import render_attempts, render_release, render_wo_view


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _print(obj, as_json):
    if as_json or isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(obj)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="qrelease",
        description="离线质量放行本地工具：导入路线/工单/设备/结果证据，"
                    "查询可用性原因并办理整单放行。",
    )
    parser.add_argument(
        "--db", default=os.environ.get("QRELEASE_DB", "qrelease.db"),
        help="SQLite 数据库文件路径（默认 ./qrelease.db 或环境变量 QRELEASE_DB）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-routes", help="导入工艺路线（JSON 数组文件）")
    p.add_argument("file")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("import-work-orders", help="导入工单登记")
    p.add_argument("file")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("import-equipment", help="导入设备合格有效期完整修订")
    p.add_argument("file")

    p = sub.add_parser("import-results", help="按批导入结果/证据")
    p.add_argument("file")

    p = sub.add_parser("query", help="查询工单内每件各工序当前结果与可用性")
    p.add_argument("wo_id")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("history", help="查看某件某工序的全部尝试（含旧尝试）")
    p.add_argument("wo_id")
    p.add_argument("part")
    p.add_argument("operation")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("release", help="办理整单放行（按当时资料重查）")
    p.add_argument("request_id", help="放行请求编号，同号同内容重试沿用原结果")
    p.add_argument("wo_id")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("revoke-sweep", help="手动执行放行单自动撤销扫描")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("show-release", help="查看放行单（含已撤销单的当时快照）")
    p.add_argument("release_id")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("list-releases", help="列出行行记录，可按工单过滤")
    p.add_argument("wo_id", nargs="?")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("export", help="导出工单当前状态、不可用原因及放行记录")
    p.add_argument("wo_id")
    p.add_argument("out_path")

    p = sub.add_parser("show-route", help="查看已登记路线的某个修订")
    p.add_argument("route_id")
    p.add_argument("revision", type=int)
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("show-equipment", help="查看设备各修订的有效期")
    p.add_argument("equipment_id")
    p.add_argument("--json", action="store_true", dest="as_json")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        cmd = args.command
        if cmd == "import-routes":
            report = import_routes(conn, _load(args.file))
            revocation_sweep(conn)
            _print({"imported_routes": report}, args_is_json(args))
        elif cmd == "import-work-orders":
            report = import_work_orders(conn, _load(args.file))
            _print({"imported_work_orders": report}, args_is_json(args))
        elif cmd == "import-equipment":
            report = import_equipment_revisions(conn, _load(args.file))
            _print(report, args_is_json(args))
        elif cmd == "import-results":
            report = import_result_batch(conn, _load(args.file))
            _print(report, args_is_json(args))
        elif cmd == "query":
            view = av.evaluate_work_order(conn, args.wo_id)
            if view is None:
                raise NotFoundError(f"工单 {args.wo_id} 未登记")
            if args.as_json:
                _print(view, True)
            else:
                print(render_wo_view(view))
        elif cmd == "history":
            attempts = av.list_attempts(conn, args.wo_id, args.part, args.operation)
            if not attempts:
                raise NotFoundError(
                    f"工单 {args.wo_id} 件 {args.part} 工序 {args.operation} 无结果"
                )
            if args.as_json:
                _print(attempts, True)
            else:
                print(render_attempts(attempts))
        elif cmd == "release":
            outcome = request_release(conn, args.request_id, args.wo_id)
            _print(outcome, args.as_json)
        elif cmd == "revoke-sweep":
            revoked = revocation_sweep(conn)
            _print({"revoked": revoked}, args.as_json)
        elif cmd == "show-release":
            order = get_release_order(conn, args.release_id)
            if order is None:
                raise NotFoundError(f"放行单 {args.release_id} 不存在")
            if args.as_json:
                _print(order, True)
            else:
                print(render_release(order))
        elif cmd == "list-releases":
            orders = list_release_orders(conn, args.wo_id)
            _print(orders, args.as_json)
        elif cmd == "export":
            payload = build_export(conn, args.wo_id)
            if payload is None:
                raise NotFoundError(f"工单 {args.wo_id} 未登记")
            export_json(conn, args.wo_id, args.out_path)
            print(f"已导出到 {args.out_path}")
        elif cmd == "show-route":
            route = av.get_route(conn, args.route_id, args.revision)
            if route is None:
                raise NotFoundError(
                    f"路线 {args.route_id} 修订 {args.revision} 未登记"
                )
            _print(route, args.as_json)
        elif cmd == "show-equipment":
            rows = conn.execute(
                "SELECT revision, periods_json, created_at FROM "
                "equipment_revision WHERE equipment_id=? ORDER BY revision",
                (args.equipment_id,),
            ).fetchall()
            if not rows:
                raise NotFoundError(f"设备 {args.equipment_id} 无资料")
            payload = [
                {"equipment_id": args.equipment_id, "revision": r["revision"],
                 "periods": json.loads(r["periods_json"]),
                 "created_at": r["created_at"]}
                for r in rows
            ]
            _print(payload, args.as_json)
        return 0
    except (ValidationError, ConflictError, NotFoundError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except QReleaseError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"错误: 找不到文件 {exc.filename}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"错误: JSON 文件无法解析：{exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def args_is_json(args):
    return getattr(args, "as_json", False)


if __name__ == "__main__":
    sys.exit(main())
