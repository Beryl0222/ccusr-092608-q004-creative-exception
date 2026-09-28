"""事件存储与领域错误。

上层服务产生的全部事实都以追加事件保存：不更新、不删除。
存储保证：

* 事件标识业务幂等——重复提交同一 ``event_id`` 返回既有事件；
* 按聚合做乐观并发——``version`` 必须等于该聚合下一序号；
* 全局递增序号 ``seq``，供溯源按真实发生顺序重建；
* 出存储的载荷为深拷贝，调用方无法回写历史。
"""

from __future__ import annotations

import copy
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import validate_event


class DomainError(Exception):
    """全部领域错误的基类，``code`` 供程序化分支。"""

    code = "domain_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ContractViolation(DomainError):
    code = "contract_violation"

    def __init__(self, issues: Sequence[Any]) -> None:
        self.issues = list(issues)
        detail = "; ".join(f"{issue.field} {issue.code}" for issue in issues)
        super().__init__(f"事件不满足契约: {detail}")


class ConcurrentModification(DomainError):
    code = "concurrent_modification"

    def __init__(self, aggregate_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"聚合 {aggregate_id} 版本冲突：期望 {expected}，实际下一个版本为 {actual}"
        )
        self.aggregate_id = aggregate_id
        self.expected = expected
        self.actual = actual


class UnknownAggregate(DomainError):
    code = "unknown_aggregate"


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    version: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at,
            "version": self.version,
            "payload": copy.deepcopy(dict(self.payload)),
        }


class EventStore:
    """内存事件存储；可整体导出/导入 JSON 便于联调与持久化。"""

    def __init__(self, schema: Mapping[str, Any]) -> None:
        self._schema = schema
        self._events: list[StoredEvent] = []
        self._by_id: dict[str, StoredEvent] = {}
        self._aggregate_versions: dict[str, int] = defaultdict(int)

    def append(self, event: Mapping[str, Any]) -> StoredEvent:
        """校验并追加一个事件。

        相同 ``event_id`` 再次提交（含并发重试）视为幂等重放，
        返回第一次写入的事件，不产生重复事实。
        """
        existing = self._by_id.get(event.get("event_id", ""))
        if existing is not None:
            return existing
        issues = validate_event(event, self._schema)
        if issues:
            raise ContractViolation(issues)
        aggregate_id = event["aggregate_id"]
        next_version = self._aggregate_versions[aggregate_id] + 1
        if event["version"] != next_version:
            raise ConcurrentModification(aggregate_id, event["version"], next_version)
        stored = StoredEvent(
            seq=len(self._events) + 1,
            event_id=event["event_id"],
            event_type=event["event_type"],
            aggregate_type=event["aggregate_type"],
            aggregate_id=aggregate_id,
            occurred_at=event["occurred_at"],
            version=next_version,
            payload=copy.deepcopy(dict(event["payload"])),
        )
        self._events.append(stored)
        self._by_id[stored.event_id] = stored
        self._aggregate_versions[aggregate_id] = next_version
        return stored

    def events(self) -> list[StoredEvent]:
        """按追加顺序返回全部事件的深拷贝。"""
        return [self._copy(stored) for stored in self._events]

    def for_aggregate(self, aggregate_id: str) -> list[StoredEvent]:
        return [self._copy(e) for e in self._events if e.aggregate_id == aggregate_id]

    def get(self, event_id: str) -> StoredEvent | None:
        stored = self._by_id.get(event_id)
        return self._copy(stored) if stored else None

    def next_version(self, aggregate_id: str) -> int:
        return self._aggregate_versions[aggregate_id] + 1

    @staticmethod
    def _copy(stored: StoredEvent) -> StoredEvent:
        return StoredEvent(
            seq=stored.seq,
            event_id=stored.event_id,
            event_type=stored.event_type,
            aggregate_type=stored.aggregate_type,
            aggregate_id=stored.aggregate_id,
            occurred_at=stored.occurred_at,
            version=stored.version,
            payload=copy.deepcopy(dict(stored.payload)),
        )

    def export_jsonl(self) -> str:
        return "\n".join(json.dumps(e.to_dict(), ensure_ascii=False) for e in self._events)

    def save_jsonl(self, path: str | Path) -> None:
        Path(path).write_text(self.export_jsonl() + "\n", encoding="utf-8")
