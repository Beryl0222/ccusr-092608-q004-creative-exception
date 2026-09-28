"""案卷服务不变量测试。"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from creative_exception.domain import (
    ConcurrentModification,
    ContractViolation,
    EventStore,
)
from creative_exception.services import (
    AuthorizationError,
    CaseFileService,
    InvalidState,
    NotFound,
    Principal,
    ValidationFailure,
    span_ref,
)

SCHEMA = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))

T0 = "2026-09-01T09:00:00+08:00"
T1 = "2026-09-02T09:00:00+08:00"
T2 = "2026-09-03T09:00:00+08:00"
T3 = "2026-09-04T09:00:00+08:00"
T4 = "2026-09-05T09:00:00+08:00"
T5 = "2026-09-06T09:00:00+08:00"
T6 = "2026-09-07T09:00:00+08:00"

editor = Principal("e1", "editor")
editor2 = Principal("e2", "editor")
author = Principal("a1", "author")
engineer = Principal("eng1", "engineer")
cr = Principal("cr1", "copyright_officer")
fr = Principal("fr1", "factual_officer")


def new_service() -> CaseFileService:
    return CaseFileService(EventStore(SCHEMA))


def register_revision(svc, ref="rev1", spans=("p1", "p2"), medium="text", no=1, **kw):
    return svc.register_revision(
        ref, "work-1", no, medium, list(spans), editor, T0, **kw
    )


def raise_finding(svc, fid="f1", span=None, actor=engineer, **kw):
    return svc.raise_finding(
        fid, "rule-1.0", span or span_ref("rev1", "p1"), actor, T1, **kw
    )


def propose(svc, ref="case-1", fid="f1", risk_flags=(), review=None, actor=editor):
    span = span_ref("rev1", "p1")
    return svc.propose_exception(
        ref, fid,
        {"revisions": ["rev1"], "spans": [span]},
        "刻意保留：这是创作意图",
        author.id, actor, T2,
        risk_flags=risk_flags, review=review,
    )


def approve(svc, ref="case-1", risk_flags=(), review=None, signer=editor2):
    span = span_ref("rev1", "p1")
    if risk_flags:
        for domain in risk_flags:
            officer = cr if domain == "copyright" else fr
            svc.decide_risk_clearance(ref, domain, "cleared", officer, T5)
    return svc.sign_exception(
        ref, "approved",
        {"revisions": ["rev1"], "spans": [span]},
        signer, T6, review=review,
    )


def full_case(svc, ref="case-1", fid="f1", risk_flags=(), review=None):
    register_revision(svc)
    raise_finding(svc, fid)
    propose(svc, ref, fid, risk_flags=risk_flags, review=review)
    svc.state_author_intent(ref, "这里的停顿是有意的。", author, T3)
    approve(svc, ref, risk_flags=risk_flags, review=review)
    return ref


class ContractExtensionTests(unittest.TestCase):
    def test_scenario_file_replays(self) -> None:
        events = json.loads((ROOT / "data/scenario.json").read_text(encoding="utf-8"))
        store = EventStore(SCHEMA)
        for event in events:
            store.append(event)
        self.assertEqual(len(events), len(store.events()))

    def test_aggregate_mismatch_detected(self) -> None:
        event = {
            "event_id": "x", "event_type": "EXCEPTION_SIGNED",
            "aggregate_type": "work_revision", "aggregate_id": "a",
            "occurred_at": T0, "version": 1, "payload": {},
        }
        issues = {(i.field, i.code) for i in __import__(
            "creative_exception.contracts", fromlist=["validate_event"]
        ).validate_event(event, SCHEMA)}
        self.assertIn(("aggregate_type", "aggregate_mismatch"), issues)

    def test_payload_enum_validated(self) -> None:
        event = {
            "event_id": "x", "event_type": "REVIEW_POSITIONED",
            "aggregate_type": "exception_case", "aggregate_id": "a",
            "occurred_at": T0, "version": 1,
            "payload": {"case_ref": "c", "reviewer_id": "r", "stance": "veto"},
        }
        from creative_exception.contracts import validate_event
        issues = validate_event(event, SCHEMA)
        self.assertIn(("payload.stance", "unsupported_value"), [(i.field, i.code) for i in issues])


class StoreTests(unittest.TestCase):
    def test_idempotent_event_id(self) -> None:
        store = EventStore(SCHEMA)
        svc = CaseFileService(store)
        register_revision(svc)
        raw = {
            "event_id": "dup-1",
            "event_type": "FINDING_RAISED",
            "aggregate_type": "quality_finding",
            "aggregate_id": "f1",
            "occurred_at": T1,
            "version": 1,
            "payload": {"rule_version": "rule-1.0", "target_span": span_ref("rev1", "p1")},
        }
        e1 = store.append(raw)
        e2 = store.append(dict(raw))  # 重试/重放同一事件标识
        self.assertEqual(e1.seq, e2.seq)
        self.assertEqual(2, len(store.events()))

    def test_optimistic_concurrency(self) -> None:
        store = EventStore(SCHEMA)
        event = {
            "event_id": "x1", "event_type": "REVISION_REGISTERED",
            "aggregate_type": "work_revision", "aggregate_id": "rev1",
            "occurred_at": T0, "version": 1,
            "payload": {"work_id": "w", "revision_no": 1, "medium": "text", "spans": ["p1"]},
        }
        store.append(event)
        with self.assertRaises(ConcurrentModification):
            store.append({**event, "event_id": "x2", "version": 1})

    def test_contract_violation_rejected(self) -> None:
        store = EventStore(SCHEMA)
        with self.assertRaises(ContractViolation):
            store.append(
                {
                    "event_id": "x", "event_type": "FINDING_RAISED",
                    "aggregate_type": "quality_finding", "aggregate_id": "f",
                    "occurred_at": "2026-09-01T09:00:00", "version": 1,
                    "payload": {"rule_version": "r"},
                }
            )


class SeparationOfDutyTests(unittest.TestCase):
    def test_author_cannot_sign_or_clear(self) -> None:
        svc = new_service()
        full_case(svc, "case-ok")
        with self.assertRaises(AuthorizationError):
            svc.sign_exception(
                "case-ok", "rejected",
                {"revisions": ["rev1"], "spans": [span_ref("rev1", "p1")]},
                author, T6,
            )
        svc2 = new_service()
        register_revision(svc2)
        raise_finding(svc2)
        propose(svc2, risk_flags=("copyright",))
        with self.assertRaises(AuthorizationError):
            svc2.decide_risk_clearance("case-1", "copyright", "cleared", author, T5)

    def test_engineer_cannot_sign(self) -> None:
        svc = new_service()
        register_revision(svc)
        raise_finding(svc)
        propose(svc)
        with self.assertRaises(AuthorizationError):
            svc.sign_exception(
                "case-1", "approved",
                {"revisions": ["rev1"], "spans": [span_ref("rev1", "p1")]},
                engineer, T6,
            )

    def test_copyright_officer_cannot_decide_factual(self) -> None:
        svc = new_service()
        register_revision(svc)
        raise_finding(svc)
        propose(svc, risk_flags=("factual",))
        with self.assertRaises(AuthorizationError):
            svc.decide_risk_clearance("case-1", "factual", "cleared", cr, T5)

    def test_author_statement_does_not_clear_risk(self) -> None:
        svc = new_service()
        register_revision(svc)
        raise_finding(svc)
        propose(svc, risk_flags=("copyright",))
        svc.state_author_intent("case-1", "我声明这是合理使用。", author, T3)
        with self.assertRaises(InvalidState) as ctx:
            approve(svc)
        self.assertIn("copyright", ctx.exception.message)

    def test_signer_must_differ_from_officer(self) -> None:
        svc = new_service()
        register_revision(svc)
        raise_finding(svc)
        propose(svc, risk_flags=("copyright",))
        svc.state_author_intent("case-1", "意图", author, T3)
        # 同一自然人若既做版权放行又做签发，按身份标识识别并拒绝
        dual_officer = Principal("same-person", "copyright_officer")
        dual_editor = Principal("same-person", "editor")
        svc.decide_risk_clearance("case-1", "copyright", "cleared", dual_officer, T5)
        with self.assertRaises(AuthorizationError):
            svc.sign_exception(
                "case-1", "approved",
                {"revisions": ["rev1"], "spans": [span_ref("rev1", "p1")]},
                dual_editor, T6,
            )

    def test_requires_author_intent_before_approval(self) -> None:
        svc = new_service()
        register_revision(svc)
        raise_finding(svc)
        propose(svc)
        with self.assertRaises(InvalidState):
            svc.sign_exception(
                "case-1", "approved",
                {"revisions": ["rev1"], "spans": [span_ref("rev1", "p1")]},
                editor2, T6,
            )


class ReviewCoexistenceTests(unittest.TestCase):
    def test_positions_coexist_and_are_not_overwritten(self) -> None:
        svc = new_service()
        full_case(svc)
        svc.position_review("case-1", "support", editor, T4, note="保留")
        svc.position_review("case-1", "objection", editor2, T4, note="反对")
        svc.position_review("case-1", "support", editor2, T4, note="改念支持")
        dossier = svc.case_dossier("case-1")
        stances = [(p["reviewer_id"], p["stance"]) for p in dossier["review_positions"]]
        self.assertEqual(
            stances,
            [("e1", "support"), ("e2", "objection"), ("e2", "support")],
        )


class ScopeAndReviewTests(unittest.TestCase):
    def test_scope_must_cover_finding_revision(self) -> None:
        svc = new_service()
        register_revision(svc, "rev1")
        register_revision(svc, "rev2", no=2)
        raise_finding(svc)
        with self.assertRaises(ValidationFailure):
            svc.propose_exception(
                "case-1", "f1",
                {"revisions": ["rev2"], "spans": []},
                "理由", author.id, editor, T2,
            )

    def test_scope_cannot_reference_foreign_span(self) -> None:
        svc = new_service()
        register_revision(svc)
        raise_finding(svc)
        with self.assertRaises(ValidationFailure):
            svc.propose_exception(
                "case-1", "f1",
                {"revisions": ["rev1"], "spans": [span_ref("rev1", "nope")]},
                "理由", author.id, editor, T2,
            )

    def test_signed_scope_cannot_exceed_proposal(self) -> None:
        svc = new_service()
        register_revision(svc, spans=("p1", "p2"))
        raise_finding(svc)
        propose(svc)
        svc.state_author_intent("case-1", "意图", author, T3)
        with self.assertRaises(ValidationFailure):
            svc.sign_exception(
                "case-1", "approved",
                {"revisions": ["rev1"], "spans": [span_ref("rev1", "p2")]},
                editor2, T6,
            )

    def test_conditional_clearance_requires_conditions(self) -> None:
        svc = new_service()
        register_revision(svc)
        raise_finding(svc)
        propose(svc, risk_flags=("copyright",))
        with self.assertRaises(ValidationFailure):
            svc.decide_risk_clearance("case-1", "copyright", "conditional", cr, T5)

    def test_review_due_requires_timestamp(self) -> None:
        svc = new_service()
        register_revision(svc)
        raise_finding(svc)
        with self.assertRaises(ValidationFailure):
            propose(svc, review={"review_required": True, "review_due": "soon"})


class ImmutabilityTests(unittest.TestCase):
    def test_approved_signoff_cannot_be_changed(self) -> None:
        svc = new_service()
        full_case(svc)
        with self.assertRaises(InvalidState):
            svc.sign_exception(
                "case-1", "rejected",
                {"revisions": ["rev1"], "spans": [span_ref("rev1", "p1")]},
                editor2, T6,
            )

    def test_resolved_finding_cannot_be_re_resolved(self) -> None:
        svc = new_service()
        full_case(svc)
        svc.resolve_finding("f1", "accepted_as_exception", editor, T6, case_ref="case-1")
        with self.assertRaises(InvalidState):
            svc.resolve_finding("f1", "fixed", editor, T6)

    def test_frozen_set_is_immutable_and_publish_is_subset(self) -> None:
        svc = new_service()
        full_case(svc)
        svc.freeze_release("rel-1", "rev1", ["case-1"], editor, T6)
        with self.assertRaises(InvalidState):
            svc.freeze_release("rel-1", "rev1", ["case-1"], editor, T6)
        svc.publish_release("rel-1", editor, T6, adopted_exception_set=[])
        with self.assertRaises(InvalidState):
            svc.publish_release("rel-1", editor, T6)

    def test_freeze_rejects_unapproved_and_out_of_scope(self) -> None:
        svc = new_service()
        register_revision(svc, spans=("p1", "p2"))
        raise_finding(svc, "f-a")
        propose(svc, "case-a", "f-a")
        with self.assertRaises(InvalidState):
            svc.freeze_release("rel-1", "rev1", ["case-a"], editor, T6)
        # 另一案卷已签发，但其范围不覆盖不含 p1 的 rev-other
        svc.raise_finding("f-b", "rule-1", span_ref("rev1", "p2"), engineer, T1)
        svc.propose_exception(
            "case-b", "f-b",
            {"revisions": ["rev1"], "spans": [span_ref("rev1", "p2")]},
            "理由", author.id, editor, T2,
        )
        svc.state_author_intent("case-b", "意图", author, T3)
        svc.sign_exception(
            "case-b", "approved",
            {"revisions": ["rev1"], "spans": [span_ref("rev1", "p2")]},
            editor2, T6,
        )
        register_revision(svc, "rev-other", spans=("p9",), no=2)
        with self.assertRaises(InvalidState):
            svc.freeze_release("rel-2", "rev-other", ["case-b"], editor, T6)

    def test_published_revision_change_is_erratum_only(self) -> None:
        svc = new_service()
        register_revision(svc, spans=("p1", "p2"))
        raise_finding(svc)
        propose(svc)
        svc.state_author_intent("case-1", "意图", author, T3)
        approve(svc)
        svc.freeze_release("rel-1", "rev1", ["case-1"], editor, T6)
        svc.publish_release("rel-1", editor, T6)
        svc.record_suggestion("s1", "case-1", engineer.id, "改写", engineer, T6,
                              target_span=span_ref("rev1", "p1"))
        with self.assertRaises(InvalidState):
            svc.decide_suggestion("s1", "applied", editor, T6, applied_to="rev1")
        # 已出版版本只能追加勘误；当时签发理由原样保留
        svc.link_erratum("rel-1", "e1", ["case-1"], editor, "2026-09-08T09:00:00+08:00")
        signoff_before = svc.case_dossier("case-1")["signoff"]
        svc.link_erratum("rel-1", "e2", ["case-1"], editor, "2026-09-08T10:00:00+08:00")
        self.assertEqual(signoff_before, svc.case_dossier("case-1")["signoff"])
        self.assertEqual(
            ["e1", "e2"], [e["erratum_id"] for e in svc.release_view("rel-1", editor)["errata"]]
        )
        # 勘误不能指向本发行未采用的例外，同一勘误不能重复关联
        with self.assertRaises(ValidationFailure):
            svc.link_erratum("rel-1", "e3", ["case-ghost"], editor,
                             "2026-09-08T11:00:00+08:00")
        with self.assertRaises(InvalidState):
            svc.link_erratum("rel-1", "e1", ["case-1"], editor,
                             "2026-09-08T12:00:00+08:00")

    def test_erratum_only_before_publish_rejected(self) -> None:
        svc = new_service()
        full_case(svc)
        svc.freeze_release("rel-1", "rev1", ["case-1"], editor, T6)
        with self.assertRaises(InvalidState):
            svc.link_erratum("rel-1", "e1", ["case-1"], editor, T6)


class BatchOverrideTests(unittest.TestCase):
    def test_batch_override_is_rejected_and_audited(self) -> None:
        svc = new_service()
        full_case(svc)
        before = svc.case_dossier("case-1")["signoff"]
        event = svc.attempt_batch_override(
            "rule-9.0", ["case-1"], engineer, T6, reason="全量改判",
            audit_id="audit-1",
        )
        self.assertEqual("BATCH_OVERRIDE_REJECTED", event.event_type)
        after = svc.case_dossier("case-1")["signoff"]
        self.assertEqual(before["signed_by"], after["signed_by"])

    def test_rule_recompute_retains_signed_judgments(self) -> None:
        svc = new_service()
        full_case(svc)
        svc.recompute_for_rule("job-1", "rule-9.0", ["case-1"], engineer, T6)
        job = svc.get_job("job-1")
        self.assertEqual("completed", job["status"])
        self.assertEqual("retained", job["outcomes"][0]["outcome"])
        audits = [e for e in svc.events() if e.event_type == "BATCH_OVERRIDE_REJECTED"]
        self.assertEqual(1, len(audits))


class SuggestionImpactTests(unittest.TestCase):
    def test_applied_only_to_revision_actually_quoting_segment(self) -> None:
        svc = new_service()
        register_revision(svc, "rev1", spans=("p1", "p2"))
        raise_finding(svc)
        propose(svc)
        svc.state_author_intent("case-1", "意图", author, T3)
        approve(svc)
        register_revision(svc, "rev2", spans=("p2",), no=2, basis_ref="rev1")
        svc.record_suggestion("s1", "case-1", editor.id, "改 p1", editor, T6)
        with self.assertRaises(ValidationFailure):
            svc.decide_suggestion("s1", "applied", editor, T6, applied_to="rev2")
        register_revision(svc, "rev3", spans=("p1",), no=3, basis_ref="rev1")
        svc.decide_suggestion("s1", "applied", editor, T6, applied_to="rev3")
        self.assertEqual("applied", svc.case_dossier("case-1")["suggestions"][0]["decision"])

    def test_frozen_exception_can_cover_later_revision_quoting_same_span(self) -> None:
        svc = new_service()
        full_case(svc)
        register_revision(svc, "rev2", spans=("p1",), no=2, basis_ref="rev1")
        svc.freeze_release("rel-2", "rev2", ["case-1"], editor, T6)


class RecalculationTests(unittest.TestCase):
    def _three_cases(self) -> CaseFileService:
        svc = new_service()
        register_revision(svc, spans=("p1", "p2", "p3"))
        svc.record_source(
            "src-1", "manuscript", "custodian",
            [span_ref("rev1", s) for s in ("p1", "p2", "p3")], editor, T1,
        )
        for i, span in enumerate(("p1", "p2", "p3"), start=1):
            fid = f"f{i}"
            cid = f"case-{i}"
            svc.raise_finding(fid, "rule-1", span_ref("rev1", span), engineer, T1)
            propose(svc, cid, fid)
            svc.state_author_intent(cid, "意图", author, T3)
            approve(svc, cid, signer=editor2)
        return svc

    def test_interrupted_then_resumed_completes_all(self) -> None:
        svc = self._three_cases()
        svc.withdraw_source(
            "src-1", "撤回", editor, T6, job_id="job-1", process_limit=1
        )
        job = svc.get_job("job-1")
        self.assertEqual("interrupted", job["status"])
        self.assertEqual(1, job["processed"])
        svc.resume_recalculation("job-1", editor, T6)
        job = svc.get_job("job-1")
        self.assertEqual("completed", job["status"])
        self.assertEqual(3, len(job["outcomes"]))
        self.assertEqual(1, job["interruptions"])

    def test_resume_without_interruption_rejected(self) -> None:
        svc = self._three_cases()
        svc.withdraw_source("src-1", "撤回", editor, T6, job_id="job-1")
        with self.assertRaises(InvalidState):
            svc.resume_recalculation("job-1", editor, T6)

    def test_source_withdrawn_after_publication_requires_erratum(self) -> None:
        svc = new_service()
        full_case(svc)
        svc.record_source(
            "src-1", "manuscript", "custodian", [span_ref("rev1", "p1")], editor, T1
        )
        svc.freeze_release("rel-1", "rev1", ["case-1"], editor, T6)
        svc.publish_release("rel-1", editor, T6)
        svc.withdraw_source("src-1", "撤回", editor, T6, job_id="job-1")
        outcome = svc.get_job("job-1")["outcomes"][0]
        self.assertEqual("erratum_required", outcome["outcome"])

    def test_source_withdrawn_unpublished_signed_is_reopened(self) -> None:
        svc = new_service()
        full_case(svc)
        svc.record_source(
            "src-1", "manuscript", "custodian", [span_ref("rev1", "p1")], editor, T1
        )
        svc.withdraw_source("src-1", "撤回", editor, T6, job_id="job-1")
        self.assertEqual(
            "reopened_for_review", svc.get_job("job-1")["outcomes"][0]["outcome"]
        )

    def test_review_due_job(self) -> None:
        svc = new_service()
        full_case(
            svc,
            review={"review_required": True, "review_trigger": "reprint",
                    "review_due": "2027-01-01T00:00:00+08:00"},
        )
        svc.run_due_reviews("job-due", editor, "2027-02-01T00:00:00+08:00")
        job = svc.get_job("job-due")
        self.assertEqual("review_required", job["outcomes"][0]["outcome"])
        # 到期后再确认立场，重算结论变为 reaffirmed
        svc.position_review("case-1", "support", editor, "2027-03-01T00:00:00+08:00")
        svc.run_due_reviews("job-due-2", editor, "2027-03-02T00:00:00+08:00")
        self.assertEqual("reaffirmed", svc.get_job("job-due-2")["outcomes"][0]["outcome"])

    def test_batch_trigger_interruptible(self) -> None:
        svc = self._three_cases()
        svc.recompute_for_rule(
            "job-rule", "rule-2", ["case-1", "case-2", "case-3"],
            engineer, T6, process_limit=2,
        )
        self.assertEqual("interrupted", svc.get_job("job-rule")["status"])
        svc.resume_recalculation("job-rule", engineer, T6)
        job = svc.get_job("job-rule")
        self.assertEqual("completed", job["status"])
        self.assertTrue(all(o["outcome"] == "retained" for o in job["outcomes"]))

    def test_duplicate_job_rejected(self) -> None:
        svc = new_service()
        full_case(svc)
        svc.run_due_reviews("job-1", editor, "2027-02-01T00:00:00+08:00")
        with self.assertRaises(InvalidState):
            svc.run_due_reviews("job-1", editor, "2027-02-01T00:00:00+08:00")


class TraceAndViewTests(unittest.TestCase):
    def test_trace_span_reconstructs_full_chain(self) -> None:
        svc = new_service()
        full_case(svc)
        svc.record_source(
            "src-1", "manuscript", "custodian", [span_ref("rev1", "p1")], editor, T1
        )
        svc.freeze_release("rel-1", "rev1", ["case-1"], editor, T6)
        svc.publish_release("rel-1", editor, T6)
        trace = svc.trace_span(span_ref("rev1", "p1"))
        self.assertEqual("src-1", trace["sources"][0]["source_id"])
        self.assertEqual("f1", trace["findings"][0]["finding_id"])
        case = trace["exceptions"][0]
        self.assertEqual("published", case["status"])
        self.assertEqual("e2", case["responsible"])
        self.assertIn("rel-1", case["published_in"])
        self.assertTrue(trace["timeline"])

    def test_trace_unknown_span(self) -> None:
        svc = new_service()
        register_revision(svc)
        with self.assertRaises(NotFound):
            svc.trace_span(span_ref("rev1", "ghost"))

    def test_public_vs_internal_release_view(self) -> None:
        svc = new_service()
        full_case(svc)
        svc.freeze_release("rel-1", "rev1", ["case-1"], editor, T6)
        svc.publish_release("rel-1", editor, T6)
        svc.link_erratum("rel-1", "err-1", ["case-1"], editor, "2026-09-08T09:00:00+08:00")
        public = svc.release_view("rel-1", author)
        self.assertEqual("public", public["visibility"])
        self.assertNotIn("internal_evidence", public)
        self.assertEqual("case-1", public["adopted_exceptions"][0]["case_ref"])
        internal = svc.release_view("rel-1", editor)
        self.assertEqual("internal", internal["visibility"])
        self.assertIn("signoff", internal["internal_evidence"][0])
        self.assertIn("case-1", internal["frozen_exception_set"])


if __name__ == "__main__":
    unittest.main()
