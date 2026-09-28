"""只读追溯投影：从任一片段还原它为何被保留、谁负责、后来怎样被使用。

投影不做校验（校验见 contracts/stream），只折叠事件；公开视图剔除
``visibility=internal`` 的证据，并以角色代替具体责任人标识。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

PUBLIC = "public"
INTERNAL = "internal"


def _body(event: Mapping[str, Any]) -> Mapping[str, Any]:
    body = event.get("payload")
    return body if isinstance(body, Mapping) else {}


def _public_party(party: Any, internal: bool) -> Any:
    if not isinstance(party, Mapping):
        return party
    if internal:
        return dict(party)
    return {"role": party.get("role")}


def _visible(item: Mapping[str, Any], internal: bool) -> bool:
    return internal or item.get("visibility") == PUBLIC


@dataclass(frozen=True)
class _State:
    revisions: dict[str, list[Mapping[str, Any]]]
    spans: dict[str, str]
    findings: dict[str, Mapping[str, Any]]
    exceptions: dict[str, list[Mapping[str, Any]]]
    restrictions: list[Mapping[str, Any]]
    suggestions: list[Mapping[str, Any]]
    releases: list[Mapping[str, Any]]
    errata: list[Mapping[str, Any]]
    recomputes: list[Mapping[str, Any]]
    sources: dict[str, Mapping[str, Any]]


def _fold(events: Sequence[Mapping[str, Any]]) -> _State:
    revisions: dict[str, list[Mapping[str, Any]]] = {}
    spans: dict[str, str] = {}
    findings: dict[str, Mapping[str, Any]] = {}
    exceptions: dict[str, list[Mapping[str, Any]]] = {}
    restrictions: list[Mapping[str, Any]] = []
    suggestions: list[Mapping[str, Any]] = []
    releases: list[Mapping[str, Any]] = []
    errata: list[Mapping[str, Any]] = []
    recomputes: list[Mapping[str, Any]] = []
    sources: dict[str, Mapping[str, Any]] = {}
    for event in events:
        etype = event.get("event_type")
        body = _body(event)
        if etype == "REVISION_REGISTERED":
            revisions.setdefault(str(body.get("work_id")), []).append(event)
            for span_id in body.get("incorporated_span_ids", []):
                spans.setdefault(str(span_id), str(body.get("work_id")))
        elif etype == "SOURCE_REGISTERED":
            sources[str(body.get("source_id"))] = event
        elif etype == "FINDING_RAISED":
            findings[str(body.get("finding_id"))] = event
        elif etype in (
            "EXCEPTION_PROPOSED",
            "AUTHOR_STATEMENT_RECORDED",
            "ON_SET_RECORD_ATTACHED",
            "REVIEW_POSITIONED",
            "EXCEPTION_DECIDED",
            "REVIEW_OUTCOME_RECORDED",
        ):
            exceptions.setdefault(str(body.get("exception_id")), []).append(event)
        elif etype == "CITATION_RESTRICTION_IMPOSED":
            restrictions.append(event)
        elif etype == "REVISION_SUGGESTED":
            suggestions.append(event)
        elif etype in ("RELEASE_FROZEN", "RELEASE_PUBLISHED"):
            releases.append(event)
        elif etype == "ERRATUM_FILED":
            errata.append(event)
        elif etype in ("RECOMPUTE_REQUESTED", "RECOMPUTE_COMPLETED"):
            recomputes.append(event)
    return _State(revisions, spans, findings, exceptions, restrictions,
                  suggestions, releases, errata, recomputes, sources)


def _span_exceptions(state: _State, span_id: str) -> list[str]:
    result: list[str] = []
    for exid, events in state.exceptions.items():
        for event in events:
            if event.get("event_type") != "EXCEPTION_PROPOSED":
                continue
            body = _body(event)
            scope = body.get("scope", {})
            if body.get("target_span", {}).get("span_id") == span_id or span_id in scope.get("span_ids", []):
                result.append(exid)
                break
    return result


def _exception_view(events: list[Mapping[str, Any]], internal: bool) -> dict[str, Any]:
    view: dict[str, Any] = {"statements": [], "on_set_records": [], "positions": []}
    for event in events:
        etype = event.get("event_type")
        body = _body(event)
        if etype == "EXCEPTION_PROPOSED":
            view["exception_id"] = body.get("exception_id")
            view["rationale"] = body.get("rationale")
            view["scope"] = body.get("scope")
            view["risk_domains"] = body.get("risk_domains")
            view["proposed_by"] = _public_party(body.get("proposed_by"), internal)
        elif etype == "AUTHOR_STATEMENT_RECORDED" and _visible(body, internal):
            view["statements"].append({"statement": body.get("statement"), "at": event.get("occurred_at")})
        elif etype == "ON_SET_RECORD_ATTACHED" and _visible(body, internal):
            view["on_set_records"].append({
                "source_id": body.get("source_id"),
                "note": body.get("note"),
                "recorded_by": _public_party(body.get("recorded_by"), internal),
                "at": event.get("occurred_at"),
            })
        elif etype == "REVIEW_POSITIONED" and _visible(body, internal):
            view["positions"].append({
                "reviewer": _public_party(body.get("reviewer"), internal),
                "position": body.get("position"),
                "comment": body.get("comment"),
                "at": event.get("occurred_at"),
            })
        elif etype == "EXCEPTION_DECIDED":
            view["decision"] = {
                "decision": body.get("decision"),
                "decided_by": _public_party(body.get("decided_by"), internal),
                "signed_off_by": [_public_party(p, internal) for p in body.get("signed_off_by", [])],
                "dissent_preserved": [
                    {"party_id": (p.get("party_id") if internal else None),
                     "position": p.get("position")}
                    for p in body.get("dissent_preserved", [])
                    if isinstance(p, Mapping)
                ],
                "review": body.get("review"),
                "at": event.get("occurred_at"),
            }
        elif etype == "REVIEW_OUTCOME_RECORDED":
            view.setdefault("reviews", []).append({
                "trigger": body.get("trigger"),
                "outcome": body.get("outcome"),
                "reviewed_by": _public_party(body.get("reviewed_by"), internal),
                "at": event.get("occurred_at"),
            })
    return view


def trace_span(events: Any, span_id: str, *, internal: bool = True) -> dict[str, Any]:
    """按片段还原完整案卷。internal=False 时只给公开说明。"""
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise ValueError("事件流必须是 JSON 数组")
    state = _fold(events)
    work_id = state.spans.get(span_id)

    revisions = []
    if work_id is not None:
        for event in state.revisions.get(work_id, []):
            body = _body(event)
            if span_id in body.get("incorporated_span_ids", []):
                revisions.append({"work_id": work_id, "revision_no": body.get("revision_no"),
                                  "medium": body.get("medium"), "at": event.get("occurred_at")})

    findings = []
    exception_ids = set(_span_exceptions(state, span_id))
    for exid in exception_ids:
        for event in state.exceptions.get(exid, []):
            if event.get("event_type") != "EXCEPTION_PROPOSED":
                continue
            for ref in _body(event).get("finding_refs", []):
                raised = state.findings.get(str(ref))
                if raised is not None:
                    b = _body(raised)
                    findings.append({
                        "finding_id": ref,
                        "rule_id": b.get("rule_id"),
                        "rule_version": b.get("rule_version"),
                        "severity": b.get("severity"),
                    })

    exceptions = [_exception_view(state.exceptions[exid], internal) for exid in sorted(exception_ids)]

    restrictions = [
        {"restriction_id": _body(e).get("restriction_id"),
         "exception_id": _body(e).get("exception_id"),
         "terms": _body(e).get("terms"),
         "imposed_by": _public_party(_body(e).get("imposed_by"), internal)}
        for e in state.restrictions if _body(e).get("span_id") == span_id
    ]
    suggestions = [
        {"suggestion_id": _body(e).get("suggestion_id"),
         "proposed_change": _body(e).get("proposed_change"),
         "status": _body(e).get("status"),
         "suggested_by": _public_party(_body(e).get("suggested_by"), internal)}
        for e in state.suggestions if _body(e).get("span_id") == span_id
    ]

    # 发行采用：只统计冻结集合含本片段例外、且冻结版本真实纳入本片段的发行。
    adoptions: list[dict[str, Any]] = []
    freeze_by_release = {
        str(_body(e).get("release_id")): e
        for e in state.releases if e.get("event_type") == "RELEASE_FROZEN"
    }
    published_at = {
        str(_body(e).get("release_id")): _body(e).get("published_at")
        for e in state.releases if e.get("event_type") == "RELEASE_PUBLISHED"
    }
    for release_id, freeze in freeze_by_release.items():
        fbody = _body(freeze)
        ref = fbody.get("revision_ref", {})
        revision = next(
            (e for e in state.revisions.get(str(ref.get("work_id")), [])
             if _body(e).get("revision_no") == ref.get("revision_no")),
            None,
        )
        incorporated = revision is not None and span_id in _body(revision).get("incorporated_span_ids", [])
        entries = [en for en in fbody.get("exception_set", []) if isinstance(en, Mapping)]
        hit = [str(en.get("exception_id")) for en in entries if str(en.get("exception_id")) in exception_ids]
        if incorporated and hit:
            adoptions.append({
                "release_id": release_id,
                "revision_ref": ref,
                "exceptions_frozen": hit,
                "frozen_at": fbody.get("frozen_at"),
                "published_at": published_at.get(release_id),
                "basis_at_time": [
                    {"exception_id": en.get("exception_id"), "retained_basis": en.get("retained_basis")}
                    for en in entries if str(en.get("exception_id")) in exception_ids
                ],
            })

    errata = [
        {"erratum_id": _body(e).get("erratum_id"),
         "release_id": _body(e).get("release_id"),
         "exception_id": _body(e).get("exception_id"),
         "correction": _body(e).get("correction"),
         "at": e.get("occurred_at")}
        for e in state.errata if _body(e).get("span_id") == span_id
    ]

    recompute_hits: list[dict[str, Any]] = []
    for event in state.recomputes:
        body = _body(event)
        if event.get("event_type") == "RECOMPUTE_COMPLETED":
            for outcome in body.get("outcomes", []):
                if isinstance(outcome, Mapping) and outcome.get("span_id") == span_id:
                    recompute_hits.append({"job_id": body.get("job_id"), "action": outcome.get("action"),
                                           "detail": outcome.get("detail")})
        elif span_id in body.get("affected_span_ids", []):
            recompute_hits.append({"job_id": body.get("job_id"), "trigger": body.get("trigger")})

    return {
        "view": INTERNAL if internal else PUBLIC,
        "span_id": span_id,
        "work_id": work_id,
        "revisions_using_span": revisions,
        "findings": findings,
        "exceptions": exceptions,
        "citation_restrictions": restrictions,
        "revision_suggestions": suggestions,
        "release_adoptions": adoptions,
        "errata": errata,
        "recompute_effects": recompute_hits,
    }
