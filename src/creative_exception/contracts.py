"""领域事件交换契约校验。

只做单事件、无状态的结构与枚举校验；跨事件的业务不变量（双签、
异议并存、冻结集合一致、检查点续跑等）见 ``stream.py``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ContractIssue:
    field: str
    code: str
    message: str


def _timezone_is_explicit(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _dig(mapping: Any, path: Sequence[str]) -> Any:
    cur = mapping
    for part in path:
        if not isinstance(cur, Mapping) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


_MISSING = object()


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _values_at(body: Mapping[str, Any], path: Sequence[str]) -> list[Any]:
    """沿路径取值；路径中段遇到数组则对每个元素继续，返回全部叶子值。"""
    current: list[Any] = [body]
    for part in path:
        nxt: list[Any] = []
        for node in current:
            if isinstance(node, Mapping) and part in node:
                value = node[part]
                if isinstance(value, list):
                    nxt.extend(value)
                else:
                    nxt.append(value)
        current = nxt
    return current


def _validate_payload_enums(
    body: Mapping[str, Any],
    enum_paths: Mapping[str, str],
    enums: Mapping[str, Any],
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    for path_str, enum_key in enum_paths.items():
        allowed = enums.get(enum_key, [])
        for value in _values_at(body, path_str.split(".")):
            if isinstance(value, list):
                candidates = value
            else:
                candidates = [value]
            for item in candidates:
                if isinstance(item, str) and item not in allowed:
                    issues.append(ContractIssue(
                        f"payload.{path_str}",
                        "unsupported_value",
                        f"字段值 {item!r} 未在契约枚举 {enum_key} 中登记",
                    ))
    return issues


def _validate_nested_objects(
    body: Mapping[str, Any],
    nested: Mapping[str, Any],
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    for field, required in nested.items():
        if field not in body:
            continue
        value = body[field]
        if not isinstance(value, Mapping):
            issues.append(ContractIssue(
                f"payload.{field}", "object_required", "字段必须是 JSON 对象",
            ))
            continue
        for sub in required:
            if sub not in value:
                issues.append(ContractIssue(
                    f"payload.{field}.{sub}", "required", "嵌套对象缺少必填字段",
                ))
    return issues


def _validate_nonempty_lists(
    body: Mapping[str, Any],
    paths: Sequence[str],
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    for path_str in paths:
        path = path_str.split(".")
        value = _dig(body, path)
        if value is _MISSING:
            continue
        if not isinstance(value, list) or not value:
            issues.append(ContractIssue(
                f"payload.{path_str}", "non_empty_list", "字段必须是非空数组",
            ))
    return issues


def _validate_payload_datetimes(body: Mapping[str, Any]) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    for field in ("frozen_at", "published_at"):
        value = body.get(field)
        if isinstance(value, str) and not _timezone_is_explicit(value):
            issues.append(ContractIssue(
                f"payload.{field}", "timezone_required", "发生时间必须包含时区",
            ))
    review = body.get("review")
    if isinstance(review, Mapping):
        due = review.get("due_at")
        if isinstance(due, str) and not _timezone_is_explicit(due):
            issues.append(ContractIssue(
                "payload.review.due_at", "timezone_required", "复审到期时间必须包含时区",
            ))
    return issues


def validate_event(payload: Any, schema: Mapping[str, Any]) -> list[ContractIssue]:
    """返回稳定排序的问题列表，不修改输入。"""
    if not isinstance(payload, Mapping):
        return [ContractIssue("$", "object_required", "事件必须是 JSON 对象")]
    issues: list[ContractIssue] = []
    for field in schema.get("required", []):
        if field not in payload:
            issues.append(ContractIssue(str(field), "required", "缺少必填字段"))
    for field in ("event_id", "event_type", "aggregate_type", "aggregate_id"):
        if field in payload and not _is_nonempty_str(payload[field]):
            issues.append(ContractIssue(field, "non_empty_string", "字段必须是非空字符串"))
    version = payload.get("version")
    if "version" in payload and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
        issues.append(ContractIssue("version", "positive_integer", "版本必须是正整数"))
    occurred_at = payload.get("occurred_at")
    if "occurred_at" in payload and (not isinstance(occurred_at, str) or not _timezone_is_explicit(occurred_at)):
        issues.append(ContractIssue("occurred_at", "timezone_required", "发生时间必须包含时区"))
    properties = schema.get("properties", {})
    for field in ("event_type", "aggregate_type"):
        allowed = properties.get(field, {}).get("enum", [])
        value = payload.get(field)
        if isinstance(value, str) and allowed and value not in allowed:
            issues.append(ContractIssue(field, "unsupported_value", "字段值未在契约中登记"))
    event_type = payload.get("event_type")
    aggregate_type = payload.get("aggregate_type")
    body = payload.get("payload")
    if "payload" in payload and not isinstance(body, Mapping):
        issues.append(ContractIssue("payload", "object_required", "事件载荷必须是 JSON 对象"))
    elif isinstance(event_type, str) and isinstance(body, Mapping):
        for field in schema.get("payload_required_by_event", {}).get(event_type, []):
            if field not in body:
                issues.append(ContractIssue(f"payload.{field}", "required", "事件载荷缺少必填字段"))
        # 聚合类型必须与事件类型在契约中登记的归属一致。
        expected_aggregate = schema.get("aggregate_by_event", {}).get(event_type)
        if isinstance(aggregate_type, str) and expected_aggregate and aggregate_type != expected_aggregate:
            issues.append(ContractIssue(
                "aggregate_type",
                "aggregate_mismatch",
                f"事件 {event_type} 必须归属聚合 {expected_aggregate}",
            ))
        issues.extend(_validate_payload_enums(
            body,
            schema.get("payload_enum_paths_by_event", {}).get(event_type, {}),
            schema.get("payload_enums", {}),
        ))
        issues.extend(_validate_nested_objects(body, schema.get("payload_nested_objects", {})))
        issues.extend(_validate_nonempty_lists(body, schema.get("payload_nonempty_lists", [])))
        issues.extend(_validate_payload_datetimes(body))
    return sorted(issues, key=lambda issue: (issue.field, issue.code))
