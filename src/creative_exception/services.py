"""创作例外保留案卷服务。

在事件契约之上实现案卷的业务不变量：

* 职责分离——作者陈述意图，版权/事实风险由对应审查官放行，编辑签发，
  技术人员不能用规则批量覆盖已签发判断（尝试一律留痕拒绝）；
* 并存审议——多人审读的支持、异议、弃权全部追加保留，不被后写者覆盖；
* 范围与复审——每条例外在提案与签发时都携带适用范围和复审条件；
* 冻结与勘误——发行冻结的例外集合不可变，出版后只能追加勘误，
  当时理由永久保留；
* 精确影响——后续修订只作用于真实引用该片段的版本；
* 可中断重算——来源撤回、复审到期、批量规则重算均可中断后续跑；
* 双向溯源——从任意文字段/镜头还原保留理由、决定责任与后续使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .contracts import _timezone_is_explicit
from .domain import DomainError, EventStore, StoredEvent

SPAN_SEP = "#"

ROLE_AUTHOR = "author"
ROLE_EDITOR = "editor"
ROLE_COPYRIGHT_OFFICER = "copyright_officer"
ROLE_FACTUAL_OFFICER = "factual_officer"
ROLE_ENGINEER = "engineer"
ROLE_ADMIN = "admin"

_INTERNAL_ROLES = {
    ROLE_EDITOR,
    ROLE_COPYRIGHT_OFFICER,
    ROLE_FACTUAL_OFFICER,
    ROLE_ADMIN,
}
_OFFICER_FOR_DOMAIN = {
    "copyright": ROLE_COPYRIGHT_OFFICER,
    "factual": ROLE_FACTUAL_OFFICER,
}


class NotFound(DomainError):
    code = "not_found"


class InvalidState(DomainError):
    code = "invalid_state"


class AuthorizationError(DomainError):
    code = "unauthorized"


class ValidationFailure(DomainError):
    code = "validation_failure"


@dataclass(frozen=True)
class Principal:
    id: str
    role: str


def span_ref(revision_ref: str, span_id: str) -> str:
    return f"{revision_ref}{SPAN_SEP}{span_id}"


def split_span(ref: str) -> tuple[str, str]:
    if SPAN_SEP not in ref:
        raise ValidationFailure(f"片段标识缺少修订前缀: {ref}")
    revision, span_id = ref.rsplit(SPAN_SEP, 1)
    return revision, span_id


def _require_time(value: str, field: str) -> None:
    if not isinstance(value, str) or not _timezone_is_explicit(value):
        raise ValidationFailure(f"{field} 必须是携带时区的时间戳")


def _review_conditions(review: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(review, Mapping) or not isinstance(review.get("review_required"), bool):
        raise ValidationFailure("复审条件必须包含布尔字段 review_required")
    result = {"review_required": review["review_required"], "review_trigger": review.get("review_trigger")}
    if review["review_required"]:
        due = review.get("review_due")
        _require_time(due, "review_due")
        result["review_due"] = due
    return result


def _scope(scope: Mapping[str, Any]) -> dict[str, Any]:
    revisions = scope.get("revisions")
    if not isinstance(revisions, list) or not revisions or not all(isinstance(r, str) and r for r in revisions):
        raise ValidationFailure("适用范围必须非空，且 revisions 为非空字符串列表")
    spans = scope.get("spans", [])
    if not isinstance(spans, list) or not all(isinstance(s, str) and s for s in spans):
        raise ValidationFailure("范围 spans 必须是字符串列表")
    return {"revisions": list(revisions), "spans": list(spans)}


@dataclass
class _State:
    revisions: dict[str, dict[str, Any]]
    sources: dict[str, dict[str, Any]]
    findings: dict[str, dict[str, Any]]
    cases: dict[str, dict[str, Any]]
    suggestions: dict[str, dict[str, Any]]
    releases: dict[str, dict[str, Any]]
    jobs: dict[str, dict[str, Any]]
    audits: list[dict[str, Any]]
    span_mentions: dict[str, list[dict[str, Any]]]


class CaseFileService:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    # ------------------------------------------------------------------ 投影

    def _load(self) -> _State:
        state = _State({}, {}, {}, {}, {}, {}, {}, [], {})

        def mention(ref: str, kind: str, related: str, seq: int) -> None:
            state.span_mentions.setdefault(ref, []).append(
                {"seq": seq, "kind": kind, "related": related}
            )

        for e in self.store.events():
            p = e.payload
            kind = e.event_type
            if kind == "REVISION_REGISTERED":
                state.revisions[e.aggregate_id] = {
                    "work_id": p["work_id"],
                    "revision_no": p["revision_no"],
                    "medium": p["medium"],
                    "spans": set(p["spans"]),
                    "basis_ref": p.get("basis_ref"),
                }
                for span_id in p["spans"]:
                    mention(span_ref(e.aggregate_id, span_id), "revision", e.aggregate_id, e.seq)
            elif kind == "SOURCE_RECORDED":
                state.sources[e.aggregate_id] = {
                    "kind": p["kind"],
                    "custodian": p["custodian"],
                    "span_refs": list(p["span_refs"]),
                    "withdrawn": False,
                    "withdraw_reason": None,
                }
                for ref in p["span_refs"]:
                    mention(ref, "source", e.aggregate_id, e.seq)
            elif kind == "SOURCE_WITHDRAWN":
                source = state.sources[p["source_id"]]
                source["withdrawn"] = True
                source["withdraw_reason"] = p["reason"]
            elif kind == "FINDING_RAISED":
                state.findings[e.aggregate_id] = {
                    "rule_version": p["rule_version"],
                    "target_span": p["target_span"],
                    "detail": p.get("detail"),
                    "raised_by": p.get("raised_by"),
                    "resolution": None,
                    "case_ref": None,
                }
                mention(p["target_span"], "finding", e.aggregate_id, e.seq)
            elif kind == "FINDING_RESOLVED":
                finding = state.findings[p["finding_ref"]]
                finding["resolution"] = p["resolution"]
                finding["case_ref"] = p.get("case_ref")
            elif kind == "EXCEPTION_PROPOSED":
                state.cases[e.aggregate_id] = {
                    "ref": e.aggregate_id,
                    "finding_ref": p["finding_ref"],
                    "target_span": p["target_span"],
                    "scope": p["scope"],
                    "rationale": p["rationale"],
                    "proposed_by": p["proposed_by"],
                    "risk_flags": list(p["risk_flags"]),
                    "review": p["review"],
                    "statements": [],
                    "positions": [],
                    "clearances": {},
                    "signoff": None,
                    "suggestions": [],
                }
                mention(p["target_span"], "exception_proposed", e.aggregate_id, e.seq)
                for ref in p["scope"].get("spans", []):
                    mention(ref, "exception_scope", e.aggregate_id, e.seq)
            elif kind == "INTENT_STATED":
                case = state.cases[p["case_ref"]]
                case["statements"].append(
                    {"author_id": p["author_id"], "statement": p["statement"], "at": e.occurred_at}
                )
            elif kind == "REVIEW_POSITIONED":
                case = state.cases[p["case_ref"]]
                case["positions"].append(
                    {
                        "reviewer_id": p["reviewer_id"],
                        "stance": p["stance"],
                        "note": p.get("note"),
                        "at": e.occurred_at,
                    }
                )
            elif kind == "RISK_CLEARANCE_DECIDED":
                case = state.cases[p["case_ref"]]
                case["clearances"][p["risk_domain"]] = {
                    "decided_by": p["decided_by"],
                    "decision": p["decision"],
                    "conditions": p.get("conditions", []),
                    "note": p.get("note"),
                    "at": e.occurred_at,
                }
            elif kind == "EXCEPTION_SIGNED":
                case = state.cases[p["case_ref"]]
                case["signoff"] = {
                    "signed_by": p["signed_by"],
                    "decision": p["decision"],
                    "scope": p["scope"],
                    "review": p["review"],
                    "note": p.get("note"),
                    "at": e.occurred_at,
                }
                for ref in p["scope"].get("spans", []):
                    mention(ref, "exception_signed_scope", p["case_ref"], e.seq)
            elif kind == "SUGGESTION_RECORDED":
                state.suggestions[e.aggregate_id] = {
                    "exception_ref": p["exception_ref"],
                    "target_span": p["target_span"],
                    "suggested_by": p["suggested_by"],
                    "summary": p["summary"],
                    "decision": None,
                    "applied_to": None,
                }
                state.cases[p["exception_ref"]]["suggestions"].append(e.aggregate_id)
                mention(p["target_span"], "suggestion", e.aggregate_id, e.seq)
            elif kind == "SUGGESTION_DECIDED":
                suggestion = state.suggestions[p["suggestion_ref"]]
                suggestion["decision"] = p["decision"]
                suggestion["applied_to"] = p.get("applied_to")
            elif kind == "RELEASE_FROZEN":
                state.releases[e.aggregate_id] = {
                    "revision_ref": p["revision_ref"],
                    "frozen": list(p["exception_set"]),
                    "published_set": None,
                    "published_at": None,
                    "errata": [],
                }
            elif kind == "RELEASE_PUBLISHED":
                release = state.releases[e.aggregate_id]
                release["published_set"] = list(p["adopted_exception_set"])
                release["published_at"] = e.occurred_at
            elif kind == "ERRATUM_LINKED":
                release = state.releases[e.aggregate_id]
                release["errata"].append(
                    {
                        "erratum_id": p["erratum_id"],
                        "affects_exception_refs": list(p["affects_exception_refs"]),
                        "at": e.occurred_at,
                    }
                )
            elif kind == "BATCH_OVERRIDE_REJECTED":
                state.audits.append(
                    {
                        "audit_id": e.aggregate_id,
                        "attempted_by": p["attempted_by"],
                        "rule_version": p["rule_version"],
                        "target_exception_refs": list(p["target_exception_refs"]),
                        "reason": p["reason"],
                        "at": e.occurred_at,
                    }
                )
            elif kind in (
                "RECALCULATION_STARTED",
                "RECALCULATION_INTERRUPTED",
                "RECALCULATION_RESUMED",
                "RECALCULATION_COMPLETED",
            ):
                self._apply_job_event(state, e)
        return state

    @staticmethod
    def _apply_job_event(state: _State, e: StoredEvent) -> None:
        p = e.payload
        job = state.jobs.get(p["job_id"])
        if e.event_type == "RECALCULATION_STARTED":
            state.jobs[p["job_id"]] = {
                "job_id": p["job_id"],
                "trigger": p["trigger"],
                "refs": list(p["affected_refs"]),
                "status": "running",
                "processed": 0,
                "outcomes": [],
                "interruptions": 0,
                "rule_version": p.get("rule_version"),
                "started_at": e.occurred_at,
            }
            return
        assert job is not None
        if e.event_type == "RECALCULATION_INTERRUPTED":
            job["status"] = "interrupted"
            job["interruptions"] += 1
            job["outcomes"] = p.get("outcomes_so_far", job["outcomes"])
            job["processed"] = len(job["outcomes"])
        elif e.event_type == "RECALCULATION_RESUMED":
            job["status"] = "running"
        elif e.event_type == "RECALCULATION_COMPLETED":
            job["status"] = "completed"
            job["outcomes"] = p["outcomes"]
            job["processed"] = len(p["outcomes"])

    # ------------------------------------------------------------------ 辅助

    def _append(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        payload: Mapping[str, Any],
        event_id: str | None,
    ) -> StoredEvent:
        if event_id and (existing := self.store.get(event_id)):
            return existing
        _require_time(occurred_at, "occurred_at")
        return self.store.append(
            {
                "event_id": event_id
                or f"{aggregate_id}:v{self.store.next_version(aggregate_id)}:{event_type}:{uuid4().hex[:8]}",
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "occurred_at": occurred_at,
                "version": self.store.next_version(aggregate_id),
                "payload": dict(payload),
            }
        )

    def _require_role(self, actor: Principal, roles: Sequence[str], action: str) -> None:
        if actor.role not in roles:
            raise AuthorizationError(f"角色 {actor.role} 无权{action}")

    def _span_exists(self, state: _State, ref: str) -> bool:
        try:
            revision, span_id = split_span(ref)
        except ValidationFailure:
            return False
        revision_state = state.revisions.get(revision)
        return bool(revision_state and span_id in revision_state["spans"])

    def _case_spans(self, case: Mapping[str, Any]) -> set[str]:
        spans = {case["target_span"]}
        spans.update(case["scope"].get("spans", []))
        return spans

    def _case_is_approved(self, case: Mapping[str, Any]) -> bool:
        return bool(case["signoff"] and case["signoff"]["decision"] == "approved")

    def _case_frozen_in(self, state: _State, case_ref: str) -> list[str]:
        return [rid for rid, r in state.releases.items() if case_ref in r["frozen"]]

    def _case_published_in(self, state: _State, case_ref: str) -> list[str]:
        return [
            rid
            for rid, r in state.releases.items()
            if r["published_set"] is not None and case_ref in r["published_set"]
        ]

    def _published_revisions(self, state: _State) -> set[str]:
        return {
            r["revision_ref"]
            for r in state.releases.values()
            if r["published_set"] is not None
        }

    def _effective_scope(self, case: Mapping[str, Any]) -> Mapping[str, Any]:
        if case["signoff"] and case["signoff"]["decision"] == "approved":
            return case["signoff"]["scope"]
        return case["scope"]

    def _case_covers_revision(
        self, state: _State, case: Mapping[str, Any], revision_ref: str
    ) -> bool:
        """例外是否适用于某版本：显式范围，或该版本真实引用同一 片段。"""
        scope = self._effective_scope(case)
        if revision_ref in scope["revisions"]:
            return True
        revision = state.revisions.get(revision_ref)
        if revision is None:
            return False
        _, target_span_id = split_span(case["target_span"])
        return target_span_id in revision["spans"]

    def _risk_gate(self, case: Mapping[str, Any]) -> list[str]:
        """返回尚未放行的风险域。"""
        missing: list[str] = []
        for domain in case["risk_flags"]:
            clearance = case["clearances"].get(domain)
            if not clearance or clearance["decision"] not in ("cleared", "conditional"):
                missing.append(domain)
        return missing

    # ---------------------------------------------------------- 版本与素材

    def register_revision(
        self,
        revision_ref: str,
        work_id: str,
        revision_no: int,
        medium: str,
        spans: Sequence[str],
        actor: Principal,
        occurred_at: str,
        *,
        basis_ref: str | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        self._require_role(actor, (ROLE_EDITOR, ROLE_ADMIN), "登记作品版本")
        state = self._load()
        if revision_ref in state.revisions:
            raise InvalidState(f"版本 {revision_ref} 已登记")
        if isinstance(revision_no, bool) or not isinstance(revision_no, int) or revision_no < 1:
            raise ValidationFailure("revision_no 必须是从 1 开始的正整数")
        if medium not in ("text", "footage"):
            raise ValidationFailure("medium 必须是 text 或 footage")
        if not spans or not all(isinstance(s, str) and s for s in spans) or len(set(spans)) != len(spans):
            raise ValidationFailure("spans 必须是非空且不重复的字符串列表")
        if basis_ref and basis_ref not in state.revisions:
            raise NotFound(f"所基于的版本 {basis_ref} 不存在")
        payload: dict[str, Any] = {
            "work_id": work_id,
            "revision_no": revision_no,
            "medium": medium,
            "spans": list(spans),
        }
        if basis_ref:
            payload["basis_ref"] = basis_ref
        return self._append(
            "REVISION_REGISTERED", "work_revision", revision_ref, occurred_at, payload, event_id
        )

    def record_source(
        self,
        source_id: str,
        kind: str,
        custodian: str,
        span_refs: Sequence[str],
        actor: Principal,
        occurred_at: str,
        *,
        event_id: str | None = None,
    ) -> StoredEvent:
        self._require_role(actor, (ROLE_EDITOR, ROLE_ADMIN), "登记原始素材")
        state = self._load()
        if source_id in state.sources:
            raise InvalidState(f"素材 {source_id} 已登记")
        if kind not in ("manuscript", "raw_footage", "performance_log", "recording", "other"):
            raise ValidationFailure("素材类型未登记")
        if not custodian:
            raise ValidationFailure("必须记录素材保管人")
        if not span_refs:
            raise ValidationFailure("素材至少关联一个片段")
        for ref in span_refs:
            if not self._span_exists(state, ref):
                raise NotFound(f"素材引用了不存在的片段: {ref}")
        return self._append(
            "SOURCE_RECORDED",
            "source_material",
            source_id,
            occurred_at,
            {"source_id": source_id, "kind": kind, "custodian": custodian, "span_refs": list(span_refs)},
            event_id,
        )

    def withdraw_source(
        self,
        source_id: str,
        reason: str,
        actor: Principal,
        occurred_at: str,
        *,
        job_id: str | None = None,
        process_limit: int | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        """撤回素材来源。

        撤回本身不可撤销；若有例外通过引用链依赖该素材覆盖的片段，
        自动开启一次 ``source_withdrawn`` 重算（可用 ``process_limit``
        模拟中断，随后用 :meth:`resume_recalculation` 续跑）。
        """
        self._require_role(actor, (ROLE_EDITOR, ROLE_ADMIN), "撤回素材来源")
        state = self._load()
        source = state.sources.get(source_id)
        if not source:
            raise NotFound(f"素材 {source_id} 不存在")
        if source["withdrawn"]:
            raise InvalidState(f"素材 {source_id} 已撤回")
        if not reason:
            raise ValidationFailure("撤回必须说明原因")
        event = self._append(
            "SOURCE_WITHDRAWN",
            "source_material",
            source_id,
            occurred_at,
            {"source_id": source_id, "reason": reason},
            event_id,
        )
        affected = self._cases_touching_spans(self._load(), set(source["span_refs"]))
        if affected and job_id:
            self._start_job(
                self._load(),
                job_id,
                "source_withdrawn",
                affected,
                actor,
                occurred_at,
                process_limit=process_limit,
            )
        return event

    # ---------------------------------------------------------- 检查发现

    def raise_finding(
        self,
        finding_id: str,
        rule_version: str,
        target_span: str,
        actor: Principal,
        occurred_at: str,
        *,
        detail: str | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        self._require_role(actor, (ROLE_ENGINEER, ROLE_EDITOR, ROLE_ADMIN), "提交自动检查发现")
        state = self._load()
        if finding_id in state.findings:
            raise InvalidState(f"发现 {finding_id} 已存在")
        if not rule_version:
            raise ValidationFailure("发现必须携带规则版本")
        if not self._span_exists(state, target_span):
            raise NotFound(f"发现指向不存在的片段: {target_span}")
        payload = {"rule_version": rule_version, "target_span": target_span, "raised_by": actor.id}
        if detail:
            payload["detail"] = detail
        return self._append(
            "FINDING_RAISED", "quality_finding", finding_id, occurred_at, payload, event_id
        )

    def resolve_finding(
        self,
        finding_ref: str,
        resolution: str,
        actor: Principal,
        occurred_at: str,
        *,
        case_ref: str | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        self._require_role(actor, (ROLE_EDITOR, ROLE_ADMIN), "了结检查发现")
        state = self._load()
        finding = state.findings.get(finding_ref)
        if not finding:
            raise NotFound(f"发现 {finding_ref} 不存在")
        if finding["resolution"] is not None:
            raise InvalidState("发现已了结；结论只能通过新事件补充，不能覆盖")
        if resolution not in ("dismissed", "fixed", "accepted_as_exception"):
            raise ValidationFailure("发现了结方式未登记")
        if resolution == "accepted_as_exception":
            case = state.cases.get(case_ref or "")
            if not case:
                raise ValidationFailure("接受为例外必须指向已提案的案卷")
            if case["finding_ref"] != finding_ref:
                raise ValidationFailure("案卷与发现不匹配")
        payload: dict[str, Any] = {"finding_ref": finding_ref, "resolution": resolution}
        if case_ref:
            payload["case_ref"] = case_ref
        return self._append(
            "FINDING_RESOLVED", "quality_finding", finding_ref, occurred_at, payload, event_id
        )

    # ---------------------------------------------------------- 例外审议

    def propose_exception(
        self,
        case_ref: str,
        finding_ref: str,
        scope: Mapping[str, Any],
        rationale: str,
        proposed_by: str,
        actor: Principal,
        occurred_at: str,
        *,
        risk_flags: Sequence[str] = (),
        review: Mapping[str, Any] | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        state = self._load()
        if case_ref in state.cases:
            raise InvalidState(f"案卷 {case_ref} 已存在")
        finding = state.findings.get(finding_ref)
        if not finding:
            raise NotFound(f"发现 {finding_ref} 不存在")
        if not rationale.strip():
            raise ValidationFailure("例外提案必须陈述理由")
        normalized_scope = _scope(scope)
        for revision in normalized_scope["revisions"]:
            if revision not in state.revisions:
                raise NotFound(f"适用范围引用了不存在的版本: {revision}")
        valid_spans = {
            span_ref(revision, span_id)
            for revision in normalized_scope["revisions"]
            for span_id in state.revisions[revision]["spans"]
        }
        for ref in normalized_scope["spans"]:
            if ref not in valid_spans:
                raise ValidationFailure(f"范围片段不属于所列版本: {ref}")
        target_span = finding["target_span"]
        target_revision, _ = split_span(target_span)
        if target_revision not in normalized_scope["revisions"]:
            raise ValidationFailure("适用范围必须覆盖发现所在版本")
        flags = list(risk_flags)
        if any(flag not in ("copyright", "factual") for flag in flags) or len(set(flags)) != len(flags):
            raise ValidationFailure("风险标记只能是 copyright / factual 且不重复")
        normalized_review = _review_conditions(review or {"review_required": False})
        return self._append(
            "EXCEPTION_PROPOSED",
            "exception_case",
            case_ref,
            occurred_at,
            {
                "finding_ref": finding_ref,
                "scope": normalized_scope,
                "rationale": rationale,
                "target_span": target_span,
                "proposed_by": proposed_by,
                "risk_flags": flags,
                "review": normalized_review,
            },
            event_id,
        )

    def state_author_intent(
        self,
        case_ref: str,
        statement: str,
        actor: Principal,
        occurred_at: str,
        *,
        event_id: str | None = None,
    ) -> StoredEvent:
        """作者陈述创作意图。作者身份是唯一门槛，但陈述不构成放行。"""
        self._require_role(actor, (ROLE_AUTHOR,), "陈述创作意图")
        state = self._load()
        case = state.cases.get(case_ref)
        if not case:
            raise NotFound(f"案卷 {case_ref} 不存在")
        if not statement.strip():
            raise ValidationFailure("意图陈述不能为空")
        return self._append(
            "INTENT_STATED",
            "exception_case",
            case_ref,
            occurred_at,
            {"case_ref": case_ref, "author_id": actor.id, "statement": statement},
            event_id,
        )

    def position_review(
        self,
        case_ref: str,
        stance: str,
        actor: Principal,
        occurred_at: str,
        *,
        note: str | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        """记录审读立场。

        所有立场（含同一审读者改念）都追加保留：支持与异议并存，
        不存在“最后写入者覆盖”。
        """
        self._require_role(actor, (ROLE_EDITOR, ROLE_ADMIN), "记录审读立场")
        state = self._load()
        if case_ref not in state.cases:
            raise NotFound(f"案卷 {case_ref} 不存在")
        if stance not in ("support", "objection", "abstain"):
            raise ValidationFailure("审读立场必须是 support / objection / abstain")
        payload: dict[str, Any] = {
            "case_ref": case_ref,
            "reviewer_id": actor.id,
            "stance": stance,
        }
        if note:
            payload["note"] = note
        return self._append(
            "REVIEW_POSITIONED", "exception_case", case_ref, occurred_at, payload, event_id
        )

    def decide_risk_clearance(
        self,
        case_ref: str,
        risk_domain: str,
        decision: str,
        actor: Principal,
        occurred_at: str,
        *,
        conditions: Sequence[str] | None = None,
        note: str | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        """版权或事实风险放行决定。

        只有对应风险域的审查官能做出决定；多次决定全部留痕，
        签发时以该风险域最近一次决定为准。
        """
        state = self._load()
        case = state.cases.get(case_ref)
        if not case:
            raise NotFound(f"案卷 {case_ref} 不存在")
        required_role = _OFFICER_FOR_DOMAIN.get(risk_domain)
        if not required_role:
            raise ValidationFailure("风险域必须是 copyright 或 factual")
        self._require_role(actor, (required_role,), f"做出 {risk_domain} 风险放行")
        if risk_domain not in case["risk_flags"]:
            raise ValidationFailure(f"案卷未标记 {risk_domain} 风险，无需放行")
        if decision not in ("cleared", "conditional", "rejected"):
            raise ValidationFailure("放行决定必须是 cleared / conditional / rejected")
        conditions = list(conditions or [])
        if decision == "conditional" and not conditions:
            raise ValidationFailure("附条件放行必须列出条件")
        payload: dict[str, Any] = {
            "case_ref": case_ref,
            "risk_domain": risk_domain,
            "decided_by": actor.id,
            "decision": decision,
        }
        if conditions:
            payload["conditions"] = conditions
        if note:
            payload["note"] = note
        return self._append(
            "RISK_CLEARANCE_DECIDED", "exception_case", case_ref, occurred_at, payload, event_id
        )

    def sign_exception(
        self,
        case_ref: str,
        decision: str,
        scope: Mapping[str, Any],
        actor: Principal,
        occurred_at: str,
        *,
        review: Mapping[str, Any] | None = None,
        note: str | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        """编辑签发艺术判断。

        批准要求：作者意图已陈述、全部风险域已由对应审查官放行、
        签发范围不超出提案范围；编辑不得同时是该案风险放行决定人。
        一经批准即视为已签发艺术判断，只能通过复审/重算/勘误渠道处理。
        """
        self._require_role(actor, (ROLE_EDITOR, ROLE_ADMIN), "签发例外")
        state = self._load()
        case = state.cases.get(case_ref)
        if not case:
            raise NotFound(f"案卷 {case_ref} 不存在")
        if decision not in ("approved", "rejected", "returned"):
            raise ValidationFailure("签发决定必须是 approved / rejected / returned")
        if case["signoff"] and case["signoff"]["decision"] == "approved":
            raise InvalidState("案卷已批准签发；已签发判断不得改写，请走复审或勘误")
        signed_scope = _scope(scope)
        proposed = case["scope"]
        if not set(signed_scope["revisions"]) <= set(proposed["revisions"]):
            raise ValidationFailure("签发范围不能超出提案的版本范围")
        if not set(signed_scope["spans"]) <= set(proposed["spans"]):
            raise ValidationFailure("签发范围不能超出提案的片段范围")
        target_revision, _ = split_span(case["target_span"])
        if target_revision not in signed_scope["revisions"]:
            raise ValidationFailure("签发范围必须继续覆盖例外片段所在版本")
        signed_review = _review_conditions(review if review is not None else case["review"])
        if decision == "approved":
            if not case["statements"]:
                raise InvalidState("批准前必须留有作者创作意图陈述")
            missing = self._risk_gate(case)
            if missing:
                raise InvalidState(f"以下风险域尚未放行: {', '.join(missing)}")
            officers = {c["decided_by"] for c in case["clearances"].values()}
            if actor.id in officers:
                raise AuthorizationError("签发编辑不能同时是该案的风险放行决定人")
        payload: dict[str, Any] = {
            "case_ref": case_ref,
            "signed_by": actor.id,
            "decision": decision,
            "scope": signed_scope,
            "review": signed_review,
        }
        if note:
            payload["note"] = note
        return self._append(
            "EXCEPTION_SIGNED", "exception_case", case_ref, occurred_at, payload, event_id
        )

    # ---------------------------------------------------------- 修改建议

    def record_suggestion(
        self,
        suggestion_id: str,
        exception_ref: str,
        suggested_by: str,
        summary: str,
        actor: Principal,
        occurred_at: str,
        *,
        target_span: str | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        self._require_role(
            actor, (ROLE_ENGINEER, ROLE_EDITOR, ROLE_ADMIN), "登记修改建议"
        )
        state = self._load()
        case = state.cases.get(exception_ref)
        if not case:
            raise NotFound(f"案卷 {exception_ref} 不存在")
        if suggestion_id in state.suggestions:
            raise InvalidState(f"修改建议 {suggestion_id} 已存在")
        span = target_span or case["target_span"]
        if span not in self._case_spans(case):
            raise ValidationFailure("建议必须针对案卷范围内的片段")
        if not summary.strip():
            raise ValidationFailure("修改建议必须有摘要")
        return self._append(
            "SUGGESTION_RECORDED",
            "revision_suggestion",
            suggestion_id,
            occurred_at,
            {
                "exception_ref": exception_ref,
                "target_span": span,
                "suggested_by": suggested_by,
                "summary": summary,
            },
            event_id,
        )

    def decide_suggestion(
        self,
        suggestion_ref: str,
        decision: str,
        actor: Principal,
        occurred_at: str,
        *,
        applied_to: str | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        self._require_role(actor, (ROLE_EDITOR, ROLE_ADMIN), "裁决修改建议")
        state = self._load()
        suggestion = state.suggestions.get(suggestion_ref)
        if not suggestion:
            raise NotFound(f"修改建议 {suggestion_ref} 不存在")
        if suggestion["decision"] is not None:
            raise InvalidState("修改建议已有裁决；不能覆盖")
        if decision not in ("accepted", "rejected", "applied"):
            raise ValidationFailure("裁决必须是 accepted / rejected / applied")
        if decision == "applied":
            if not applied_to:
                raise ValidationFailure("采用建议必须指明应用到的版本")
            target_revision = state.revisions.get(applied_to)
            if not target_revision:
                raise NotFound(f"目标版本 {applied_to} 不存在")
            origin_revision, span_id = split_span(suggestion["target_span"])
            quotes_segment = (
                applied_to == origin_revision or span_id in target_revision["spans"]
            )
            if not quotes_segment:
                raise ValidationFailure(
                    "后续修订只能作用于真实引用该片段的版本："
                    f"版本 {applied_to} 未携带片段 {span_id}"
                )
            if applied_to in self._published_revisions(state):
                raise InvalidState(f"版本 {applied_to} 已出版，修改只能进入后续修订或勘误")
        payload: dict[str, Any] = {"suggestion_ref": suggestion_ref, "decision": decision}
        if applied_to:
            payload["applied_to"] = applied_to
        return self._append(
            "SUGGESTION_DECIDED",
            "revision_suggestion",
            suggestion_ref,
            occurred_at,
            payload,
            event_id,
        )

    # ---------------------------------------------------------- 发行冻结

    def freeze_release(
        self,
        release_id: str,
        revision_ref: str,
        exception_refs: Sequence[str],
        actor: Principal,
        occurred_at: str,
        *,
        event_id: str | None = None,
    ) -> StoredEvent:
        """冻结一次发行实际采用的例外集合；冻结后不可变。"""
        self._require_role(actor, (ROLE_EDITOR, ROLE_ADMIN), "冻结发行")
        state = self._load()
        if release_id in state.releases:
            raise InvalidState(f"发行 {release_id} 已冻结；冻结集合不可变")
        if revision_ref not in state.revisions:
            raise NotFound(f"版本 {revision_ref} 不存在")
        refs = list(exception_refs)
        if not refs or len(set(refs)) != len(refs):
            raise ValidationFailure("例外集合必须非空且不重复")
        for ref in refs:
            case = state.cases.get(ref)
            if not case:
                raise NotFound(f"例外 {ref} 不存在")
            if not self._case_is_approved(case):
                raise InvalidState(f"例外 {ref} 尚未经编辑批准签发，不能进入发行")
            if not self._case_covers_revision(state, case, revision_ref):
                raise InvalidState(f"例外 {ref} 的适用范围不覆盖版本 {revision_ref}")
        return self._append(
            "RELEASE_FROZEN",
            "release_snapshot",
            release_id,
            occurred_at,
            {"release_id": release_id, "revision_ref": revision_ref, "exception_set": refs},
            event_id,
        )

    def publish_release(
        self,
        release_id: str,
        actor: Principal,
        occurred_at: str,
        *,
        adopted_exception_set: Sequence[str] | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        """出版/上映。实际采用集合必须是冻结集合的子集，默认全部采用。"""
        self._require_role(actor, (ROLE_EDITOR, ROLE_ADMIN), "出版发行")
        state = self._load()
        release = state.releases.get(release_id)
        if not release:
            raise NotFound(f"发行 {release_id} 尚未冻结")
        if release["published_set"] is not None:
            raise InvalidState("发行已出版，出版事实不可重复写入")
        adopted = list(adopted_exception_set if adopted_exception_set is not None else release["frozen"])
        if not set(adopted) <= set(release["frozen"]):
            raise ValidationFailure("实际采用集合只能是冻结集合的子集")
        if len(set(adopted)) != len(adopted):
            raise ValidationFailure("采用集合不能重复")
        return self._append(
            "RELEASE_PUBLISHED",
            "release_snapshot",
            release_id,
            occurred_at,
            {
                "release_id": release_id,
                "revision_ref": release["revision_ref"],
                "adopted_exception_set": adopted,
            },
            event_id,
        )

    def link_erratum(
        self,
        release_id: str,
        erratum_id: str,
        affects_exception_refs: Sequence[str],
        actor: Principal,
        occurred_at: str,
        *,
        event_id: str | None = None,
    ) -> StoredEvent:
        """为已出版发行追加勘误；当时签发理由原样保留。"""
        self._require_role(actor, (ROLE_EDITOR, ROLE_ADMIN), "关联勘误")
        state = self._load()
        release = state.releases.get(release_id)
        if not release:
            raise NotFound(f"发行 {release_id} 不存在")
        if release["published_set"] is None:
            raise InvalidState("只有已出版/上映的发行才能关联勘误")
        if any(existing["erratum_id"] == erratum_id for existing in release["errata"]):
            raise InvalidState(f"勘误 {erratum_id} 已关联")
        refs = list(affects_exception_refs)
        if not refs:
            raise ValidationFailure("勘误必须关联至少一个当时采用的例外")
        if not set(refs) <= set(release["published_set"]):
            raise ValidationFailure("勘误只能关联本发行实际采用的例外")
        return self._append(
            "ERRATUM_LINKED",
            "release_snapshot",
            release_id,
            occurred_at,
            {
                "release_id": release_id,
                "erratum_id": erratum_id,
                "affects_exception_refs": refs,
            },
            event_id,
        )

    # ------------------------------------------------------ 批量覆盖拦截

    def attempt_batch_override(
        self,
        rule_version: str,
        target_exception_refs: Sequence[str],
        actor: Principal,
        occurred_at: str,
        *,
        reason: str = "",
        audit_id: str | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        """技术人员用新规则批量改判的尝试一律拒绝并留痕。

        无论目标是否已签发，都不会有任何案卷判断被修改；事件
        ``BATCH_OVERRIDE_REJECTED`` 记录尝试人、规则版本与目标，
        供审计与规则版本溯源。
        """
        self._require_role(
            actor, (ROLE_ENGINEER, ROLE_ADMIN), "提交批量规则覆盖"
        )
        state = self._load()
        refs = list(target_exception_refs)
        if not refs:
            raise ValidationFailure("批量覆盖必须指明目标")
        for ref in refs:
            if ref not in state.cases:
                raise NotFound(f"目标例外 {ref} 不存在")
        signed = [ref for ref in refs if self._case_is_approved(state.cases[ref])]
        audit_ref = audit_id or f"audit-{uuid4().hex[:12]}"
        return self._append(
            "BATCH_OVERRIDE_REJECTED",
            "audit_entry",
            audit_ref,
            occurred_at,
            {
                "attempted_by": actor.id,
                "rule_version": rule_version,
                "target_exception_refs": refs,
                "reason": reason
                or (
                    "规则批量改判不得覆盖已签发的艺术判断"
                    + (f"；其中已签发: {', '.join(signed)}" if signed else "")
                ),
            },
            event_id,
        )

    # ------------------------------------------------------------ 重算

    def _cases_touching_spans(self, state: _State, spans: set[str]) -> list[str]:
        return sorted(
            ref
            for ref, case in state.cases.items()
            if self._case_spans(case) & spans
        )

    def list_reviews_due(self, now: str) -> list[str]:
        """返回已签发且复审到期（含逾期）的案卷。"""
        _require_time(now, "now")
        state = self._load()
        due: list[str] = []
        for ref, case in state.cases.items():
            if not self._case_is_approved(case):
                continue
            review = case["signoff"]["review"]
            if review.get("review_required") and review["review_due"] <= now:
                due.append(ref)
        return sorted(due)

    def run_due_reviews(
        self,
        job_id: str,
        actor: Principal,
        now: str,
        *,
        process_limit: int | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        self._require_role(actor, (ROLE_EDITOR, ROLE_ADMIN), "发起复审到期重算")
        refs = self.list_reviews_due(now)
        state = self._load()
        return self._start_job(
            state, job_id, "review_due", refs, actor, now, process_limit=process_limit,
            event_id=event_id,
        )

    def recompute_for_rule(
        self,
        job_id: str,
        rule_version: str,
        target_exception_refs: Sequence[str],
        actor: Principal,
        occurred_at: str,
        *,
        process_limit: int | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        """新规则版本引发的批量依赖重算。

        已签发判断在结果中一律 ``retained``，并同时留一条
        ``BATCH_OVERRIDE_REJECTED`` 审计；未结案卷标 ``recheck_required``。
        """
        self._require_role(actor, (ROLE_ENGINEER, ROLE_ADMIN), "发起批量规则重算")
        state = self._load()
        refs = list(target_exception_refs)
        if not refs:
            raise ValidationFailure("重算必须指明目标")
        for ref in refs:
            if ref not in state.cases:
                raise NotFound(f"目标例外 {ref} 不存在")
        signed = [ref for ref in refs if self._case_is_approved(state.cases[ref])]
        if signed:
            self._append(
                "BATCH_OVERRIDE_REJECTED",
                "audit_entry",
                f"audit-{job_id}",
                occurred_at,
                {
                    "attempted_by": actor.id,
                    "rule_version": rule_version,
                    "target_exception_refs": signed,
                    "reason": f"规则 {rule_version} 批量重算不得改判已签发艺术判断，判断保留",
                },
                None,
            )
        started = self._start_job(
            self._load(),
            job_id,
            "batch_rule_recompute",
            sorted(set(refs)),
            actor,
            occurred_at,
            process_limit=process_limit,
            rule_version=rule_version,
            event_id=event_id,
        )
        return started

    def _start_job(
        self,
        state: _State,
        job_id: str,
        trigger: str,
        refs: Sequence[str],
        actor: Principal,
        occurred_at: str,
        *,
        process_limit: int | None = None,
        rule_version: str | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        if job_id in state.jobs:
            raise InvalidState(f"重算任务 {job_id} 已存在")
        if process_limit is not None and process_limit < 0:
            raise ValidationFailure("process_limit 不能为负")
        payload: dict[str, Any] = {
            "job_id": job_id,
            "trigger": trigger,
            "affected_refs": list(refs),
        }
        if rule_version:
            payload["rule_version"] = rule_version
        started = self._append(
            "RECALCULATION_STARTED",
            "recalculation_job",
            job_id,
            occurred_at,
            payload,
            event_id,
        )
        self._process_tail(self._load(), job_id, actor, occurred_at, process_limit)
        return started

    def resume_recalculation(
        self,
        job_id: str,
        actor: Principal,
        occurred_at: str,
        *,
        process_limit: int | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        """从中断游标继续处理；此前已产出的结果保留不重算。"""
        state = self._load()
        job = state.jobs.get(job_id)
        if not job:
            raise NotFound(f"重算任务 {job_id} 不存在")
        if job["status"] != "interrupted":
            raise InvalidState("只有中断状态的任务可以续跑")
        if process_limit is not None and process_limit <= 0:
            raise ValidationFailure("续跑至少要处理一个条目")
        resumed = self._append(
            "RECALCULATION_RESUMED",
            "recalculation_job",
            job_id,
            occurred_at,
            {"job_id": job_id, "cursor": job["processed"]},
            event_id,
        )
        self._process_tail(self._load(), job_id, actor, occurred_at, process_limit)
        return resumed

    def _process_tail(
        self,
        state: _State,
        job_id: str,
        actor: Principal,
        occurred_at: str,
        process_limit: int | None,
    ) -> None:
        job = state.jobs[job_id]
        outcomes = list(job["outcomes"])
        refs = job["refs"]
        budget = process_limit
        index = job["processed"]
        while index < len(refs):
            if budget is not None:
                if budget <= 0:
                    self._append(
                        "RECALCULATION_INTERRUPTED",
                        "recalculation_job",
                        job_id,
                        occurred_at,
                        {
                            "job_id": job_id,
                            "last_processed_ref": refs[index - 1],
                            "outcomes_so_far": outcomes,
                        },
                        None,
                    )
                    return
                budget -= 1
            outcomes.append(self._outcome_for(state, job, refs[index]))
            index += 1
        self._append(
            "RECALCULATION_COMPLETED",
            "recalculation_job",
            job_id,
            occurred_at,
            {"job_id": job_id, "outcomes": outcomes},
            None,
        )

    def _outcome_for(self, state: _State, job: Mapping[str, Any], ref: str) -> dict[str, str]:
        case = state.cases[ref]
        trigger = job["trigger"]
        if trigger == "source_withdrawn":
            if self._case_published_in(state, ref):
                outcome, reason = "erratum_required", "例外已随出版/上映采用，来源撤回须以勘误处理"
            elif self._case_is_approved(case):
                outcome, reason = "reopened_for_review", "已签发但未出版，依据来源撤回，需重新审议"
            else:
                outcome, reason = "recheck_required", "提案尚未签发，撤回后需重新核实依据"
        elif trigger == "review_due":
            review = case["signoff"]["review"] if case["signoff"] else {}
            due = review.get("review_due")
            reaffirmed = any(
                position["at"] > due and position["stance"] == "support"
                for position in case["positions"]
            ) or any(
                clearance["at"] > due and clearance["decision"] in ("cleared", "conditional")
                for clearance in case["clearances"].values()
            )
            if reaffirmed:
                outcome, reason = "reaffirmed", "到期后已有审读支持或风险再确认"
            else:
                outcome, reason = "review_required", "复审到期，尚无到期后的再确认"
        else:  # batch_rule_recompute
            if self._case_is_approved(case):
                outcome, reason = "retained", "已签发艺术判断保留，规则改判被拒绝"
            else:
                outcome, reason = "recheck_required", "未结案卷按新规则重新核查"
        return {"ref": ref, "outcome": outcome, "reason": reason}

    def get_job(self, job_id: str) -> dict[str, Any]:
        state = self._load()
        job = state.jobs.get(job_id)
        if not job:
            raise NotFound(f"重算任务 {job_id} 不存在")
        return {
            "job_id": job["job_id"],
            "trigger": job["trigger"],
            "affected_refs": list(job["refs"]),
            "status": job["status"],
            "processed": job["processed"] if job["status"] == "completed" else len(job["outcomes"]),
            "interruptions": job["interruptions"],
            "outcomes": list(job["outcomes"]),
        }

    # ------------------------------------------------------------ 溯源视图

    def case_dossier(self, case_ref: str) -> dict[str, Any]:
        """案卷内部全貌：提案、意图、并存立场、放行、签发、建议与采用。"""
        state = self._load()
        case = state.cases.get(case_ref)
        if not case:
            raise NotFound(f"案卷 {case_ref} 不存在")
        finding = state.findings.get(case["finding_ref"])
        return {
            "case_ref": case_ref,
            "status": self._case_status(state, case_ref),
            "finding": dict(finding) if finding else None,
            "target_span": case["target_span"],
            "proposed": {
                "by": case["proposed_by"],
                "rationale": case["rationale"],
                "scope": {
                    "revisions": list(case["scope"]["revisions"]),
                    "spans": list(case["scope"]["spans"]),
                },
                "risk_flags": list(case["risk_flags"]),
                "review": dict(case["review"]),
            },
            "author_intent": list(case["statements"]),
            "review_positions": list(case["positions"]),
            "risk_clearances": {
                domain: dict(decision) for domain, decision in case["clearances"].items()
            },
            "signoff": dict(case["signoff"]) if case["signoff"] else None,
            "suggestions": [
                dict(state.suggestions[sid]) for sid in case["suggestions"] if sid in state.suggestions
            ],
            "frozen_in": self._case_frozen_in(state, case_ref),
            "published_in": self._case_published_in(state, case_ref),
        }

    def _case_status(self, state: _State, case_ref: str) -> str:
        case = state.cases[case_ref]
        if self._case_published_in(state, case_ref):
            return "published"
        if self._case_frozen_in(state, case_ref):
            return "frozen"
        signoff = case["signoff"]
        if signoff and signoff["decision"] == "approved":
            return "approved"
        if signoff:
            return signoff["decision"]
        if self._risk_gate(case):
            return "awaiting_risk_clearance"
        return "proposed"

    def trace_span(self, span: str) -> dict[str, Any]:
        """从任意文字段或镜头还原：为何保留、谁负责、后来怎样被使用。"""
        state = self._load()
        if not self._span_exists(state, span):
            raise NotFound(f"片段 {span} 不存在")
        revision, span_id = split_span(span)
        sources = []
        for sid, source in state.sources.items():
            if span in source["span_refs"]:
                sources.append(
                    {
                        "source_id": sid,
                        "kind": source["kind"],
                        "custodian": source["custodian"],
                        "withdrawn": source["withdrawn"],
                        "withdraw_reason": source["withdraw_reason"],
                    }
                )
        findings = []
        cases = []
        suggestions = []
        for fid, finding in state.findings.items():
            if finding["target_span"] == span:
                findings.append(
                    {
                        "finding_id": fid,
                        "rule_version": finding["rule_version"],
                        "detail": finding["detail"],
                        "resolution": finding["resolution"],
                    }
                )
        for cid, case in state.cases.items():
            if span not in self._case_spans(case):
                continue
            cases.append(
                {
                    "case_ref": cid,
                    "status": self._case_status(state, cid),
                    "rationale": case["rationale"],
                    "proposed_by": case["proposed_by"],
                    "author_intent": list(case["statements"]),
                    "positions": list(case["positions"]),
                    "risk_clearances": {
                        domain: dict(decision) for domain, decision in case["clearances"].items()
                    },
                    "responsible": case["signoff"]["signed_by"] if case["signoff"] else None,
                    "signoff": dict(case["signoff"]) if case["signoff"] else None,
                    "scope": {
                        "revisions": list(self._effective_scope(case)["revisions"]),
                        "spans": list(self._effective_scope(case).get("spans", [])),
                    },
                    "review": (
                        dict(case["signoff"]["review"])
                        if case["signoff"]
                        else dict(case["review"])
                    ),
                    "frozen_in": self._case_frozen_in(state, cid),
                    "published_in": self._case_published_in(state, cid),
                }
            )
        for sid, suggestion in state.suggestions.items():
            if suggestion["target_span"] == span:
                suggestions.append({"suggestion_id": sid, **dict(suggestion)})
        jobs = []
        for job in state.jobs.values():
            matched = [o for o in job["outcomes"] if self._job_outcome_touches(state, o["ref"], span)]
            if matched:
                jobs.append(
                    {
                        "job_id": job["job_id"],
                        "trigger": job["trigger"],
                        "status": job["status"],
                        "outcomes": matched,
                    }
                )
        timeline = [dict(m) for m in state.span_mentions.get(span, [])]
        timeline.sort(key=lambda m: m["seq"])
        return {
            "span": span,
            "revision": revision,
            "span_id": span_id,
            "sources": sources,
            "findings": findings,
            "exceptions": cases,
            "suggestions": suggestions,
            "recalculations": jobs,
            "timeline": timeline,
        }

    def _job_outcome_touches(self, state: _State, case_ref: str, span: str) -> bool:
        case = state.cases.get(case_ref)
        return bool(case and span in self._case_spans(case))

    def release_view(self, release_id: str, actor: Principal) -> dict[str, Any]:
        """按权限提供发行视图：公开说明或内部证据。"""
        state = self._load()
        release = state.releases.get(release_id)
        if not release:
            raise NotFound(f"发行 {release_id} 不存在")
        adopted = list(release["published_set"] or release["frozen"])

        def public_case(case_ref: str) -> dict[str, Any]:
            case = state.cases[case_ref]
            return {
                "case_ref": case_ref,
                "target_span": case["target_span"],
                "public_rationale": case["rationale"],
            }

        view: dict[str, Any] = {
            "release_id": release_id,
            "revision_ref": release["revision_ref"],
            "published": release["published_set"] is not None,
            "adopted_exceptions": [public_case(ref) for ref in adopted],
            "errata": [dict(item) for item in release["errata"]],
        }
        if actor.role not in _INTERNAL_ROLES:
            view["visibility"] = "public"
            return view
        view["visibility"] = "internal"
        view["frozen_exception_set"] = list(release["frozen"])
        view["internal_evidence"] = [self.case_dossier(ref) for ref in adopted]
        return view

    # ----------------------------------------------------------- 事件入口

    def events(self) -> list[StoredEvent]:
        return self.store.events()
