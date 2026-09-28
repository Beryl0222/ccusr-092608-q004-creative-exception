"""跨事件业务不变量校验。

事件流按时间顺序输入。校验器先建只读索引，再执行一组规则：
引用完整、版本单调、风险双签、异议并存、冻结一致、勘误关联、
重算检查点续跑。不修改输入；问题按稳定键排序。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .contracts import ContractIssue, validate_event


def _issue(event_id: str, field: str, code: str, message: str) -> StreamIssue:
    return StreamIssue(event_id, field, code, message)


@dataclass(frozen=True)
class StreamIssue:
    event_id: str
    field: str
    code: str
    message: str


@dataclass(frozen=True)
class _Index:
    events: list[Mapping[str, Any]]
    by_id: dict[str, Mapping[str, Any]]
    revisions: dict[str, list[Mapping[str, Any]]]
    sources: dict[str, Mapping[str, Any]]
    spans: dict[str, str]  # span_id -> work_id（登记该片段的版本所属作品）
    findings: dict[str, Mapping[str, Any]]
    exceptions: dict[str, list[Mapping[str, Any]]]


def _body(event: Mapping[str, Any]) -> Mapping[str, Any]:
    body = event.get("payload")
    return body if isinstance(body, Mapping) else {}


def _build_index(events: Sequence[Mapping[str, Any]]) -> _Index:
    by_id: dict[str, Mapping[str, Any]] = {}
    revisions: dict[str, list[Mapping[str, Any]]] = {}
    sources: dict[str, Mapping[str, Any]] = {}
    spans: dict[str, str] = {}
    findings: dict[str, Mapping[str, Any]] = {}
    exceptions: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        etype = event.get("event_type")
        body = _body(event)
        eid = str(event.get("event_id", ""))
        if isinstance(eid, str) and eid:
            by_id.setdefault(eid, event)
        if etype == "REVISION_REGISTERED":
            work_id = str(body.get("work_id", ""))
            revisions.setdefault(work_id, []).append(event)
            for span_id in body.get("incorporated_span_ids", []):
                if isinstance(span_id, str):
                    spans.setdefault(span_id, work_id)
        elif etype == "SOURCE_REGISTERED":
            sid = str(body.get("source_id", ""))
            if sid:
                sources.setdefault(sid, event)
        elif etype == "FINDING_RAISED":
            fid = str(body.get("finding_id", ""))
            if fid:
                findings.setdefault(fid, event)
        elif etype in (
            "EXCEPTION_PROPOSED",
            "AUTHOR_STATEMENT_RECORDED",
            "ON_SET_RECORD_ATTACHED",
            "REVIEW_POSITIONED",
            "EXCEPTION_DECIDED",
            "REVIEW_OUTCOME_RECORDED",
            "RULE_OVERRIDE_REJECTED",
        ):
            exid = str(body.get("exception_id", ""))
            if exid:
                exceptions.setdefault(exid, []).append(event)
    return _Index(list(events), by_id, revisions, sources, spans, findings, exceptions)


def _roles(parties: Any) -> set[str]:
    roles: set[str] = set()
    if isinstance(parties, list):
        for party in parties:
            if isinstance(party, Mapping) and isinstance(party.get("role"), str):
                roles.add(party["role"])
    return roles


def _is_party(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("party_id"), str)
        and bool(value["party_id"].strip())
        and isinstance(value.get("role"), str)
    )


def _validate_event_shapes(events: Sequence[Mapping[str, Any]], schema: Mapping[str, Any]) -> list[StreamIssue]:
    issues: list[StreamIssue] = []
    seen: set[str] = set()
    for event in events:
        eid = str(event.get("event_id", "")) if isinstance(event, Mapping) else ""
        for issue in validate_event(event, schema):
            issues.append(_issue(eid, issue.field, issue.code, issue.message))
        if eid:
            if eid in seen:
                issues.append(_issue(eid, "event_id", "duplicate_event", "事件标识重复"))
            seen.add(eid)
    return issues


def _validate_ordering_and_versions(index: _Index) -> list[StreamIssue]:
    issues: list[StreamIssue] = []
    last_ts: str | None = None
    version_cursor: dict[tuple[str, str], int] = {}
    revision_nos: dict[str, int] = {}
    for event in index.events:
        eid = str(event.get("event_id", ""))
        ts = event.get("occurred_at")
        if isinstance(ts, str) and last_ts is not None and ts < last_ts:
            issues.append(_issue(eid, "occurred_at", "out_of_order", "事件流必须按发生时间非递减排列"))
        if isinstance(ts, str):
            last_ts = ts
        key = (str(event.get("aggregate_type", "")), str(event.get("aggregate_id", "")))
        version = event.get("version")
        if isinstance(version, int):
            prev = version_cursor.get(key)
            if prev is not None and version <= prev:
                issues.append(_issue(eid, "version", "version_not_advanced", "同一聚合的版本必须严格递增"))
            version_cursor[key] = version
        if event.get("event_type") == "REVISION_REGISTERED":
            body = _body(event)
            work_id = str(body.get("work_id", ""))
            no = body.get("revision_no")
            if isinstance(no, int):
                prev_no = revision_nos.get(work_id)
                if prev_no is not None and no <= prev_no:
                    issues.append(_issue(eid, "payload.revision_no", "revision_no_not_advanced", "同一作品的版本号必须递增"))
                revision_nos[work_id] = no
    return issues


def _earlier_events(index: _Index, exception_id: str, upto: int) -> list[Mapping[str, Any]]:
    return [
        event
        for event in index.exceptions.get(exception_id, [])
        if index.events.index(event) < upto
    ]


def _decision_before(index: _Index, exception_id: str, upto: int) -> Mapping[str, Any] | None:
    for pos in range(upto - 1, -1, -1):
        event = index.events[pos]
        if (
            event.get("event_type") == "EXCEPTION_DECIDED"
            and _body(event).get("exception_id") == exception_id
        ):
            return event
    return None


def _validate_references(index: _Index) -> list[StreamIssue]:
    issues: list[StreamIssue] = []
    frozen: dict[str, Mapping[str, Any]] = {}
    published: set[str] = set()
    revision_keys = set()
    for event in index.events:
        body = _body(event)
        work_id = body.get("work_id")
        no = body.get("revision_no")
        if event.get("event_type") == "REVISION_REGISTERED":
            revision_keys.add((str(work_id), no))

    for pos, event in enumerate(index.events):
        etype = event.get("event_type")
        body = _body(event)
        eid = str(event.get("event_id", ""))

        def missing(field: str, message: str) -> None:
            issues.append(_issue(eid, field, "broken_reference", message))

        if etype == "SOURCE_WITHDRAWN" and str(body.get("source_id")) not in index.sources:
            missing("payload.source_id", "撤回的素材必须先登记")
        elif etype == "FINDING_RAISED":
            span = body.get("target_span")
            sid = span.get("span_id") if isinstance(span, Mapping) else None
            if not isinstance(sid, str) or sid not in index.spans:
                missing("payload.target_span.span_id", "发现指向的片段必须先在某版本中登记")
        elif etype == "EXCEPTION_PROPOSED":
            exid = str(body.get("exception_id"))
            for ref in body.get("finding_refs", []):
                if not isinstance(ref, str) or ref not in index.findings:
                    missing("payload.finding_refs", f"发现引用 {ref!r} 不存在")
            span = body.get("target_span")
            sid = span.get("span_id") if isinstance(span, Mapping) else None
            if not isinstance(sid, str) or sid not in index.spans:
                missing("payload.target_span.span_id", "例外指向的片段必须先登记")
            scope = body.get("scope", {})
            for covered in scope.get("span_ids", []) if isinstance(scope, Mapping) else []:
                if covered not in index.spans:
                    missing("payload.scope.span_ids", f"适用范围片段 {covered!r} 未登记")
            if not _is_party(body.get("proposed_by")):
                missing("payload.proposed_by", "提案人必须是带角色的参与方")
            if exid in index.exceptions and any(
                e.get("event_type") == "EXCEPTION_PROPOSED"
                and e is not event
                for e in index.exceptions[exid]
            ):
                issues.append(_issue(eid, "payload.exception_id", "duplicate_proposal", "同一例外只能提案一次"))
        elif etype in (
            "AUTHOR_STATEMENT_RECORDED",
            "ON_SET_RECORD_ATTACHED",
            "REVIEW_POSITIONED",
        ):
            exid = str(body.get("exception_id"))
            if not any(
                e.get("event_type") == "EXCEPTION_PROPOSED"
                for e in _earlier_events(index, exid, pos)
            ):
                missing("payload.exception_id", "陈述/记录/立场必须挂在已提案的例外上")
            if etype == "ON_SET_RECORD_ATTACHED" and str(body.get("source_id")) not in index.sources:
                missing("payload.source_id", "现场记录引用的素材必须先登记")
            if etype == "REVIEW_POSITIONED" and not _is_party(body.get("reviewer")):
                missing("payload.reviewer", "审读人必须是带角色的参与方")
        elif etype == "EXCEPTION_DECIDED":
            exid = str(body.get("exception_id"))
            proposal = next(
                (e for e in _earlier_events(index, exid, pos)
                 if e.get("event_type") == "EXCEPTION_PROPOSED"),
                None,
            )
            if proposal is None:
                missing("payload.exception_id", "决定必须针对已提案的例外")
            if not _is_party(body.get("decided_by")):
                missing("payload.decided_by", "决定人必须是带角色的参与方")
            for party in body.get("signed_off_by", []):
                if not _is_party(party):
                    missing("payload.signed_off_by", "签发方必须是带角色的参与方")
            if proposal is not None:
                proposed_scope = set(_body(proposal).get("scope", {}).get("span_ids", []))
                decided_scope = set(body.get("scope", {}).get("span_ids", []))
                if not decided_scope <= proposed_scope:
                    issues.append(_issue(
                        eid, "payload.scope.span_ids", "scope_expanded",
                        "决定的适用范围不得超出提案范围",
                    ))
        elif etype == "RULE_OVERRIDE_REJECTED":
            exid = str(body.get("exception_id"))
            if _decision_before(index, exid, pos) is None:
                missing("payload.exception_id", "被拒绝的规则覆盖必须针对已决定的例外")
            if not _is_party(body.get("attempted_by")):
                missing("payload.attempted_by", "尝试方必须是带角色的参与方")
        elif etype == "REVIEW_OUTCOME_RECORDED":
            exid = str(body.get("exception_id"))
            if _decision_before(index, exid, pos) is None:
                missing("payload.exception_id", "复审结果必须针对已决定的例外")
            if not _is_party(body.get("reviewed_by")):
                missing("payload.reviewed_by", "复审人必须是带角色的参与方")
        elif etype == "CITATION_RESTRICTION_IMPOSED":
            if str(body.get("exception_id")) not in index.exceptions:
                missing("payload.exception_id", "引用限制必须挂在已登记的例外上")
            if str(body.get("span_id")) not in index.spans:
                missing("payload.span_id", "引用限制指向的片段必须先登记")
            if not _is_party(body.get("imposed_by")):
                missing("payload.imposed_by", "限制施加方必须是带角色的参与方")
        elif etype == "REVISION_SUGGESTED":
            if str(body.get("span_id")) not in index.spans:
                missing("payload.span_id", "修改建议指向的片段必须先登记")
            if not _is_party(body.get("suggested_by")):
                missing("payload.suggested_by", "建议人必须是带角色的参与方")
        elif etype == "RELEASE_FROZEN":
            ref = body.get("revision_ref", {})
            if (str(ref.get("work_id")), ref.get("revision_no")) not in revision_keys:
                missing("payload.revision_ref", "冻结的版本必须先登记")
            release_id = str(body.get("release_id"))
            if release_id in frozen:
                issues.append(_issue(eid, "payload.release_id", "release_refrozen", "一次发行只能冻结一次例外集合"))
            frozen[release_id] = event
            for entry in body.get("exception_set", []):
                if not isinstance(entry, Mapping) or not isinstance(entry.get("exception_id"), str):
                    missing("payload.exception_set", "冻结集合条目必须包含 exception_id")
                    continue
                decision = _decision_before(index, str(entry.get("exception_id")), pos)
                if decision is None or _body(decision).get("decision") not in (
                    "retained", "retained_with_conditions",
                ):
                    issues.append(_issue(
                        eid, "payload.exception_set", "set_entry_not_retained",
                        f"冻结条目 {entry.get('exception_id')!r} 必须已被签发保留",
                    ))
        elif etype == "RELEASE_PUBLISHED":
            release_id = str(body.get("release_id"))
            if release_id not in frozen:
                missing("payload.release_id", "发行必须先冻结才能出版/上映")
            elif release_id in published:
                issues.append(_issue(eid, "payload.release_id", "release_republished", "一次发行只能出版/上映一次"))
            published.add(release_id)
        elif etype == "ERRATUM_FILED":
            if str(body.get("release_id")) not in published:
                missing("payload.release_id", "勘误必须关联已出版/上映的发行")
            if str(body.get("exception_id")) not in index.exceptions:
                missing("payload.exception_id", "勘误必须关联例外")
            if str(body.get("span_id")) not in index.spans:
                missing("payload.span_id", "勘误指向的片段必须先登记")
    return issues


def _validate_decisions(index: _Index, schema: Mapping[str, Any]) -> list[StreamIssue]:
    """风险双签、技术人员禁签、作者不能独自放行、复审条件、异议并存。"""
    issues: list[StreamIssue] = []
    risk_signoff: Mapping[str, str] = schema.get("risk_signoff", {})
    positions: dict[str, list[Mapping[str, Any]]] = {}

    for pos, event in enumerate(index.events):
        etype = event.get("event_type")
        body = _body(event)
        eid = str(event.get("event_id", ""))
        if etype == "REVIEW_POSITIONED":
            positions.setdefault(str(body.get("exception_id")), []).append(event)
        if etype != "EXCEPTION_DECIDED":
            continue

        exid = str(body.get("exception_id"))
        proposal = next(
            (e for e in _earlier_events(index, exid, pos)
             if e.get("event_type") == "EXCEPTION_PROPOSED"),
            None,
        )
        decision = body.get("decision")
        decider = body.get("decided_by", {})
        signers = body.get("signed_off_by", [])
        signer_roles = _roles(signers)
        decider_role = decider.get("role") if isinstance(decider, Mapping) else None

        # 技术人员不能签发艺术判断，只能触发重算。
        if decider_role == "technician":
            issues.append(_issue(eid, "payload.decided_by.role", "technician_cannot_decide",
                                 "技术人员不能签发艺术例外判断"))

        if decision in ("retained", "retained_with_conditions"):
            risk_domains = set(_body(proposal).get("risk_domains", [])) if proposal is not None else set()
            # 纯艺术例外：作者或责任编辑可放行；涉版权/事实：作者可陈述，
            # 但必须另有对应责任方签发，作者本人不能独自放行。
            if "copyright" in risk_domains and risk_signoff["copyright"] not in signer_roles:
                issues.append(_issue(eid, "payload.signed_off_by", "missing_copyright_signoff",
                                     "涉版权风险的保留必须由法务签发"))
            if "factual" in risk_domains and risk_signoff["factual"] not in signer_roles:
                issues.append(_issue(eid, "payload.signed_off_by", "missing_factual_signoff",
                                     "涉事实风险的保留必须由事实核查签发"))
            only_author = not signer_roles - {"author"}
            if risk_domains & {"copyright", "factual"} and only_author:
                issues.append(_issue(eid, "payload.signed_off_by", "author_cannot_self_release",
                                     "作者不能独自放行涉版权或事实风险的内容"))

            # 保留决定必须带适用范围与复审条件。
            review = body.get("review")
            if not isinstance(review, Mapping) or not isinstance(review.get("due_at"), str):
                issues.append(_issue(eid, "payload.review.due_at", "review_condition_required",
                                     "任何保留例外都必须明确复审条件（到期时间与触发）"))
            triggers = review.get("triggers") if isinstance(review, Mapping) else None
            if not isinstance(triggers, list) or not triggers:
                issues.append(_issue(eid, "payload.review.triggers", "review_trigger_required",
                                     "复审条件必须至少包含一个复审触发"))

        # 异议并存：决定时既有的全部立场必须原样保留，且至少覆盖每个非赞成立场。
        taken = positions.get(exid, [])
        if taken:
            preserved = body.get("dissent_preserved")
            if not isinstance(preserved, list):
                issues.append(_issue(eid, "payload.dissent_preserved", "dissent_must_be_listed",
                                     "多人审读时异议须并存，决定必须列出并存立场"))
            else:
                actual = {
                    (p.get("payload", {}).get("reviewer", {}).get("party_id"),
                     p.get("payload", {}).get("position"))
                    for p in taken
                }
                recorded = {
                    (p.get("party_id"), p.get("position"))
                    for p in preserved if isinstance(p, Mapping)
                }
                missing_positions = actual - recorded
                if missing_positions:
                    issues.append(_issue(eid, "payload.dissent_preserved", "dissent_overwritten",
                                         f"并存立场缺失（最后写入者不得覆盖）: {sorted(missing_positions)}"))
    return issues


def _latest_decision_state(index: _Index, exception_id: str) -> str | None:
    """返回例外当前生效状态：被复审撤销则 revoked，否则为决定值。"""
    decision: str | None = None
    for event in index.exceptions.get(exception_id, []):
        etype = event.get("event_type")
        if etype == "EXCEPTION_DECIDED":
            decision = str(_body(event).get("decision"))
        elif etype == "REVIEW_OUTCOME_RECORDED":
            outcome = _body(event).get("outcome")
            if outcome == "revoked":
                decision = "revoked"
            elif outcome == "amended" and decision is not None:
                decision = "retained_with_conditions"
    return decision


def _exceptions_for_span(index: _Index, span_id: str) -> list[str]:
    result: list[str] = []
    for exid, events in index.exceptions.items():
        for event in events:
            if event.get("event_type") != "EXCEPTION_PROPOSED":
                continue
            target = _body(event).get("target_span", {})
            scope = _body(event).get("scope", {})
            if target.get("span_id") == span_id or span_id in scope.get("span_ids", []):
                result.append(exid)
                break
    return result


def _validate_recompute(index: _Index) -> list[StreamIssue]:
    """重算必须可续跑，且不能批量覆盖已签发判断。"""
    issues: list[StreamIssue] = []
    requested: dict[str, Mapping[str, Any]] = {}
    checkpoints: dict[str, list[Mapping[str, Any]]] = {}
    completed: set[str] = set()

    for pos, event in enumerate(index.events):
        etype = event.get("event_type")
        body = _body(event)
        eid = str(event.get("event_id", ""))
        job_id = str(body.get("job_id"))

        if etype == "RECOMPUTE_REQUESTED":
            if job_id in requested:
                issues.append(_issue(eid, "payload.job_id", "duplicate_job", "重算任务不能重复发起"))
            requested[job_id] = event
            for span_id in body.get("affected_span_ids", []):
                if span_id not in index.spans:
                    issues.append(_issue(eid, "payload.affected_span_ids", "span_not_referenced",
                                         f"重算只能影响真实登记的片段，{span_id!r} 未被任何版本引用"))
        elif etype == "RECOMPUTE_CHECKPOINTED":
            if job_id not in requested:
                issues.append(_issue(eid, "payload.job_id", "broken_reference", "检查点必须属于已发起的重算任务"))
            checkpoints.setdefault(job_id, []).append(event)
            seq = body.get("seq")
            prev_seqs = [_body(e).get("seq") for e in checkpoints.get(job_id, [])[:-1]]
            if not isinstance(seq, int) or seq < 1 or seq in prev_seqs:
                issues.append(_issue(eid, "payload.seq", "checkpoint_seq", "检查点序号必须从 1 起严格递增"))
            affected = set(_body(requested[job_id]).get("affected_span_ids", [])) if job_id in requested else set()
            for span_id in body.get("processed_span_ids", []):
                if span_id not in affected:
                    issues.append(_issue(eid, "payload.processed_span_ids", "scope_creep",
                                         "重算处理范围不得超出任务声明的受影响片段"))
        elif etype == "RECOMPUTE_COMPLETED":
            if job_id not in requested:
                issues.append(_issue(eid, "payload.job_id", "broken_reference", "完成事件必须属于已发起的重算任务"))
                continue
            if job_id in completed:
                issues.append(_issue(eid, "payload.job_id", "duplicate_completion", "重算任务不能重复完成"))
            completed.add(job_id)
            affected = set(_body(requested[job_id]).get("affected_span_ids", []))
            processed = set(body.get("processed_span_ids", []))
            if not affected <= processed:
                issues.append(_issue(eid, "payload.processed_span_ids", "incomplete_processing",
                                     "中断续跑后完成时必须覆盖全部受影响片段"))
            cps = checkpoints.get(job_id, [])
            statuses = [_body(e).get("status") for e in cps]
            if "interrupted" in statuses and "resumed" not in statuses:
                issues.append(_issue(eid, "payload.job_id", "interruption_not_resumed",
                                     "发生中断的重算必须先记录 resumed 检查点才能完成"))
            for outcome in body.get("outcomes", []):
                if not isinstance(outcome, Mapping):
                    issues.append(_issue(eid, "payload.outcomes", "object_required", "重算结果必须是对象"))
                    continue
                span_id = outcome.get("span_id")
                action = outcome.get("action")
                if span_id not in index.spans:
                    issues.append(_issue(eid, "payload.outcomes", "broken_reference",
                                         f"重算结果片段 {span_id!r} 未登记"))
                # 已签发且仍生效的艺术保留：重算只能标记复审，不得直接改判/重开覆盖。
                for exid in _exceptions_for_span(index, str(span_id)):
                    state = _latest_decision_state(index, exid)
                    if state in ("retained", "retained_with_conditions") and action not in (
                        "no_change", "exception_flagged_for_review",
                    ):
                        issues.append(_issue(eid, "payload.outcomes", "signed_judgment_protected",
                                             f"片段 {span_id} 的已签发保留 {exid} 不能被新规则批量覆盖"))
    return issues


def _validate_published_immutability(index: _Index) -> list[StreamIssue]:
    """已出版/上映版本保留当时理由；勘误只追加关联，不改写冻结集合。"""
    issues: list[StreamIssue] = []
    frozen: dict[str, Mapping[str, Any]] = {}
    for event in index.events:
        if event.get("event_type") == "RELEASE_FROZEN":
            frozen[str(_body(event).get("release_id"))] = event

    for event in index.events:
        if event.get("event_type") != "ERRATUM_FILED":
            continue
        body = _body(event)
        eid = str(event.get("event_id", ""))
        freeze = frozen.get(str(body.get("release_id")))
        if freeze is None:
            continue
        ref = _body(freeze).get("revision_ref", {})
        work_id, rev_no = str(ref.get("work_id")), ref.get("revision_no")
        revision = next(
            (e for e in index.revisions.get(work_id, [])
             if _body(e).get("revision_no") == rev_no),
            None,
        )
        incorporated = set(_body(revision).get("incorporated_span_ids", [])) if revision is not None else set()
        if str(body.get("span_id")) not in incorporated:
            issues.append(_issue(eid, "payload.span_id", "erratum_unrelated_span",
                                 "勘误只能关联冻结版本真实采用的片段"))
        frozen_exceptions = {
            str(entry.get("exception_id"))
            for entry in _body(freeze).get("exception_set", [])
            if isinstance(entry, Mapping)
        }
        if str(body.get("exception_id")) not in frozen_exceptions:
            issues.append(_issue(eid, "payload.exception_id", "erratum_unrelated_exception",
                                 "勘误必须关联该次发行冻结集合中的例外，并保留当时理由"))
    return issues


def validate_stream(events: Any, schema: Mapping[str, Any]) -> list[StreamIssue]:
    """校验整个事件流，返回稳定排序的问题列表，不修改输入。"""
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        return [StreamIssue("$", "$", "array_required", "事件流必须是 JSON 数组")]
    issues: list[StreamIssue] = []
    issues.extend(_validate_event_shapes(events, schema))
    if not all(isinstance(event, Mapping) for event in events):
        return sorted(issues, key=lambda i: (i.event_id, i.field, i.code))
    index = _build_index(events)
    issues.extend(_validate_ordering_and_versions(index))
    issues.extend(_validate_references(index))
    issues.extend(_validate_decisions(index, schema))
    issues.extend(_validate_recompute(index))
    issues.extend(_validate_published_immutability(index))
    return sorted(issues, key=lambda i: (i.event_id, i.field, i.code))
