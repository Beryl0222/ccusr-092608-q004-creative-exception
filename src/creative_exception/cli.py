"""从命令行校验领域事件，或直接回放完整案卷场景。

* 校验单个事件文件：``python -m creative_exception.cli <schema.json> <event.json>``
* 校验事件数组：文件内容为 ``[事件, ...]`` 时逐个校验
* 回放场景：追加 ``--replay`` 后通过契约校验并按存储顺序回放，
  同时检查每个聚合的乐观并发版本号
"""

import json
import sys
from pathlib import Path

from .contracts import validate_event
from .domain import EventStore


def _load_schema_and_events(path_schema: str, path_events: str) -> tuple[dict, list[dict]]:
    schema = json.loads(Path(path_schema).read_text(encoding="utf-8"))
    raw = json.loads(Path(path_events).read_text(encoding="utf-8"))
    events = raw if isinstance(raw, list) else [raw]
    return schema, events


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    replay = False
    if "--replay" in argv:
        replay = True
        argv.remove("--replay")
    if len(argv) != 2:
        print(
            "用法: python -m creative_exception.cli <schema.json> <events.json> [--replay]",
            file=sys.stderr,
        )
        return 2
    schema, events = _load_schema_and_events(argv[0], argv[1])
    all_issues = []
    for index, event in enumerate(events):
        issues = validate_event(event, schema)
        for issue in issues:
            all_issues.append((index, issue))
    if all_issues:
        for index, issue in all_issues:
            print(f"[{index}] {issue.field}	{issue.code}	{issue.message}")
        return 1
    if replay:
        store = EventStore(schema)
        for event in events:
            store.append(event)
        print(f"replayed {len(events)} events")
        return 0
    print("valid" if len(events) == 1 else f"valid {len(events)} events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
