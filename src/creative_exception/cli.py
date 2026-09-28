"""命令行入口：validate 校验事件/事件流，trace 按片段还原案卷。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_event
from .projection import trace_span
from .stream import validate_stream


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _cmd_validate(args: argparse.Namespace) -> int:
    schema = _load(args.schema)
    data = _load(args.events)
    if isinstance(data, list):
        issues = validate_stream(data, schema)
        for issue in issues:
            print(f"{issue.event_id}\t{issue.field}\t{issue.code}\t{issue.message}")
    elif isinstance(data, Mapping):
        issues = validate_event(data, schema)
        for issue in issues:
            print(f"{issue.field}\t{issue.code}\t{issue.message}")
    else:
        print("输入必须是事件对象或事件数组", file=sys.stderr)
        return 2
    if not issues:
        print("valid")
        return 0
    return 1


def _cmd_trace(args: argparse.Namespace) -> int:
    schema = _load(args.schema)
    events = _load(args.events)
    if not isinstance(events, list):
        print("trace 需要事件流（JSON 数组）", file=sys.stderr)
        return 2
    issues = validate_stream(events, schema)
    if issues and not args.force:
        for issue in issues:
            print(f"{issue.event_id}\t{issue.field}\t{issue.code}\t{issue.message}", file=sys.stderr)
        print("事件流存在问题，拒绝投影；可用 --force 强制输出。", file=sys.stderr)
        return 1
    view = trace_span(events, args.span, internal=not args.public)
    referenced = any((
        view["work_id"],
        view["findings"],
        view["exceptions"],
        view["release_adoptions"],
        view["citation_restrictions"],
        view["revision_suggestions"],
        view["recompute_effects"],
    ))
    if not referenced:
        print(f"片段 {args.span} 未在事件流中登记，无法还原案卷。", file=sys.stderr)
        return 3
    print(json.dumps(view, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="创作例外保留案卷命令行")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="校验事件对象或事件流")
    p_validate.add_argument("schema")
    p_validate.add_argument("events")
    p_validate.set_defaults(func=_cmd_validate)

    p_trace = sub.add_parser("trace", help="按片段还原保留案卷")
    p_trace.add_argument("schema")
    p_trace.add_argument("events")
    p_trace.add_argument("--span", required=True, help="要还原的文字/镜头片段标识")
    p_trace.add_argument("--public", action="store_true", help="只输出公开说明，隐藏内部证据")
    p_trace.add_argument("--force", action="store_true", help="即使校验有问题也输出投影")
    p_trace.set_defaults(func=_cmd_trace)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
