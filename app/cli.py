"""命令行入口：反复导入声明、比较续约方案、取得授权并确认合同。

示例：
    python -m app.cli consumption
    python -m app.cli disputes
    python -m app.cli rights
    python -m app.cli batches
    python -m app.cli reconcile
    python -m app.cli import-statements path/to/more_statements.jsonl
    python -m app.cli summary
    python -m app.cli request Q0
    python -m app.cli issue Q0 guarantee+usage --on 2026-11-01
    python -m app.cli expire Q1 --on 2026-11-16
    python -m app.cli guarantees
    python -m app.cli amendments
    python -m app.cli reset
"""
from __future__ import annotations

import argparse
import json
import sys

from .service import Application
from .store import OUTPUTS_DIR, write_json


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _export(name: str, payload) -> None:
    path = OUTPUTS_DIR / f"{name}.json"
    write_json(path, payload)
    print(f"已导出 {path}")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--export", action="store_true", help="同时写入 outputs/")
    parser = argparse.ArgumentParser(description="文化素材许可用量解释与续约承保", parents=[common])
    parser.add_argument("--as-of", help="评估时点（ISO 日期），默认为全部资料可见")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in (
        "consumption", "disputes", "rights", "batches", "reconcile",
        "summary", "guarantees", "amendments", "mutual-exclusion",
    ):
        sub.add_parser(name, parents=[common])

    p_import = sub.add_parser("import-statements", parents=[common], help="追加导入声明 JSONL")
    p_import.add_argument("path")

    p_req = sub.add_parser("request", parents=[common], help="单个续约请求的方案比较与授权评估")
    p_req.add_argument("request_id")

    p_issue = sub.add_parser("issue", parents=[common], help="签发报价（占用担保额度并等待成立）")
    p_issue.add_argument("request_id")
    p_issue.add_argument("plan_id")
    p_issue.add_argument("--on", required=True)

    p_expire = sub.add_parser("expire", parents=[common], help="登记报价过期（释放预占额度）")
    p_expire.add_argument("request_id")
    p_expire.add_argument("--on", required=True)

    p_withdraw = sub.add_parser("withdraw", parents=[common], help="撤回未成立报价")
    p_withdraw.add_argument("request_id")
    p_withdraw.add_argument("--on", required=True)

    p_narrow = sub.add_parser("narrow-release", parents=[common], help="登记客户缩小发行范围")
    p_narrow.add_argument("narrowing_id")
    p_narrow.add_argument("release")
    p_narrow.add_argument("--on", required=True)
    p_narrow.add_argument("--drop-territory", action="append", default=[])
    p_narrow.add_argument("--drop-work", action="append", default=[])

    sub.add_parser("reset", parents=[common], help="清空本地应用状态（原始资料不受影响）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "reset":
        Application().state.reset()
        print("本地状态已清空")
        return 0

    app = Application(as_of=args.as_of)
    request_commands = {"request", "issue", "expire", "withdraw"}
    if args.command in request_commands and args.request_id not in app.store.requests:
        raise SystemExit(f"未知续约请求: {args.request_id}")
    if args.command == "issue":
        valid_plans = {q.plan_id for q in app.analyzer.plan_quotes(app.store.requests[args.request_id])}
        if args.plan_id not in valid_plans:
            raise SystemExit(f"非法方案 {args.plan_id}，允许: {sorted(valid_plans)}")
    dispatch = {
        "consumption": lambda: app.consumption_lines(),
        "disputes": lambda: app.usage_disputes(),
        "rights": lambda: app.rights_coverage(),
        "batches": lambda: app.batches(),
        "reconcile": lambda: app.reconciliation(),
        "summary": lambda: app.renewal_summary(),
        "guarantees": lambda: app.guarantee_usage(),
        "amendments": lambda: app.amendment_impacts(),
        "mutual-exclusion": lambda: app.mutual_exclusion(),
        "request": lambda: app.evaluate_request(args.request_id),
        "issue": lambda: app.issue_quote(args.request_id, args.plan_id, args.on),
        "expire": lambda: app.expire_quote(args.request_id, args.on),
        "withdraw": lambda: app.withdraw(args.request_id, args.on),
        "narrow-release": lambda: app.register_narrowing(
            args.narrowing_id, args.release, args.on,
            dropped_territories=args.drop_territory,
            dropped_works=args.drop_work,
        ),
    }
    if args.command == "import-statements":
        added = app.import_statements(args.path)
        payload = {"imported": added, "path": args.path}
    else:
        payload = dispatch[args.command]()
    _print(payload)
    if args.export:
        _export(args.command.replace("-", "_"), payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
