import copy
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from creative_exception.projection import trace_span
from creative_exception.stream import validate_stream


def _codes(issues):
    return {(i.event_id, i.field, i.code) for i in issues}


class StreamBuilder:
    """按契约聚合归属生成时间与版本都合法的事件。"""

    def __init__(self, schema):
        self.schema = schema
        self.events = []
        self._versions = {}
        self._clock = datetime(2026, 9, 1, 9, 0, tzinfo=timezone(timedelta(hours=8)))

    def add(self, event_id, event_type, aggregate_id, payload, ts=None):
        aggregate_type = self.schema["aggregate_by_event"][event_type]
        key = (aggregate_type, aggregate_id)
        self._versions[key] = self._versions.get(key, 0) + 1
        if ts is None:
            self._clock += timedelta(minutes=5)
            ts = self._clock.isoformat()
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": ts,
            "version": self._versions[key],
            "payload": payload,
        }
        self.events.append(event)
        return event

    def review_block(self, exid="ex-1", span="s-1", risk_domains=None,
                     signoff=None, decision="retained", dissent=None,
                     review=None, decided_by=None):
        risk_domains = risk_domains or ["artistic_only"]
        signoff = signoff if signoff is not None else [{"party_id": "p-editor", "role": "responsible_editor"}]
        decided_by = decided_by or {"party_id": "p-editor", "role": "responsible_editor"}
        review = review if review is not None else {
            "due_at": "2027-03-01T00:00:00+08:00",
            "triggers": ["fixed_schedule"],
        }
        dissent = dissent if dissent is not None else [
            {"party_id": "p-editor", "position": "concur"},
        ]
        self.add("prop-1", "EXCEPTION_PROPOSED", exid, {
            "exception_id": exid,
            "finding_refs": ["f-1"],
            "target_span": {"span_id": span, "kind": "footage", "locator": "0-10s"},
            "scope": {"span_ids": [span]},
            "rationale": "节奏需要",
            "proposed_by": {"party_id": "p-director", "role": "author"},
            "risk_domains": risk_domains,
        })
        self.add("pos-1", "REVIEW_POSITIONED", exid, {
            "exception_id": exid,
            "reviewer": {"party_id": "p-editor", "role": "responsible_editor"},
            "position": "concur",
            "visibility": "public",
        })
        self.add("dec-1", "EXCEPTION_DECIDED", exid, {
            "exception_id": exid,
            "decision": decision,
            "decided_by": decided_by,
            "signed_off_by": signoff,
            "scope": {"span_ids": [span]},
            "dissent_preserved": dissent,
            "review": review,
        })

    def baseline(self, span="s-1", risk_domains=None, signoff=None, decision="retained",
                 dissent=None, review=None, decided_by=None, freeze=True, publish=True):
        self.add("rev-1", "REVISION_REGISTERED", "work-1", {
            "work_id": "work-1", "revision_no": 1, "medium": "film",
            "incorporated_span_ids": [span],
        })
        self.add("find-1", "FINDING_RAISED", "f-1", {
            "finding_id": "f-1", "rule_id": "r-1", "rule_version": "1.0",
            "target_span": {"span_id": span, "kind": "footage", "locator": "0-10s"},
            "severity": "continuity",
        })
        self.review_block("ex-1", span, risk_domains, signoff, decision, dissent, review, decided_by)
        if freeze:
            self.add("frz-1", "RELEASE_FROZEN", "rel-1", {
                "release_id": "rel-1",
                "revision_ref": {"work_id": "work-1", "revision_no": 1},
                "exception_set": [{"exception_id": "ex-1", "retained_basis": "编辑签发保留"}],
                "frozen_at": "2026-09-30T12:00:00+08:00",
            })
        if freeze and publish:
            self.add("pub-1", "RELEASE_PUBLISHED", "rel-1", {
                "release_id": "rel-1", "published_at": "2026-10-01T12:00:00+08:00",
            })
        return self


class StreamInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data/sample.json").read_text(encoding="utf-8"))

    def assertValid(self, events):
        issues = validate_stream(events, self.schema)
        self.assertEqual([], issues, msg="\n".join(f"{i.event_id} {i.code} {i.message}" for i in issues))

    def test_sample_stream_is_valid(self):
        self.assertValid(self.sample)

    def test_minimal_valid_stream(self):
        self.assertValid(StreamBuilder(self.schema).baseline().events)

    def test_copyright_requires_legal_signoff(self):
        b = StreamBuilder(self.schema).baseline(
            risk_domains=["copyright"],
            signoff=[{"party_id": "p-director", "role": "author"}],
            freeze=False, publish=False,
        )
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("dec-1", "payload.signed_off_by", "missing_copyright_signoff"), codes)
        self.assertIn(("dec-1", "payload.signed_off_by", "author_cannot_self_release"), codes)

    def test_factual_requires_fact_checker(self):
        b = StreamBuilder(self.schema).baseline(
            risk_domains=["factual"],
            signoff=[{"party_id": "p-editor", "role": "responsible_editor"}],
            freeze=False, publish=False,
        )
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("dec-1", "payload.signed_off_by", "missing_factual_signoff"), codes)

    def test_legal_signoff_satisfies_copyright(self):
        b = StreamBuilder(self.schema).baseline(
            risk_domains=["copyright"],
            signoff=[
                {"party_id": "p-legal", "role": "legal"},
                {"party_id": "p-director", "role": "author"},
            ],
        )
        self.assertValid(b.events)

    def test_technician_cannot_decide(self):
        b = StreamBuilder(self.schema).baseline(
            decided_by={"party_id": "p-bot", "role": "technician"},
            freeze=False, publish=False,
        )
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("dec-1", "payload.decided_by.role", "technician_cannot_decide"), codes)

    def test_retained_requires_review_condition(self):
        b = StreamBuilder(self.schema).baseline(review={"triggers": []}, freeze=False, publish=False)
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("dec-1", "payload.review.due_at", "review_condition_required"), codes)
        self.assertIn(("dec-1", "payload.review.triggers", "review_trigger_required"), codes)

    def test_dissent_cannot_be_overwritten(self):
        # 两人同时审读：赞成与异议都在决定前登记，决定却只保留赞成（最后写入者覆盖）。
        b = StreamBuilder(self.schema)
        b.add("rev-1", "REVISION_REGISTERED", "work-1", {
            "work_id": "work-1", "revision_no": 1, "medium": "text",
            "incorporated_span_ids": ["s-1"],
        })
        b.add("find-1", "FINDING_RAISED", "f-1", {
            "finding_id": "f-1", "rule_id": "r-1", "rule_version": "1.0",
            "target_span": {"span_id": "s-1", "kind": "manuscript", "locator": "p.3"},
            "severity": "style",
        })
        b.add("prop-1", "EXCEPTION_PROPOSED", "ex-1", {
            "exception_id": "ex-1",
            "finding_refs": ["f-1"],
            "target_span": {"span_id": "s-1", "kind": "manuscript", "locator": "p.3"},
            "scope": {"span_ids": ["s-1"]},
            "rationale": "错位是刻意的复调结构",
            "proposed_by": {"party_id": "p-author", "role": "author"},
            "risk_domains": ["artistic_only"],
        })
        b.add("pos-1", "REVIEW_POSITIONED", "ex-1", {
            "exception_id": "ex-1",
            "reviewer": {"party_id": "p-editor", "role": "responsible_editor"},
            "position": "concur",
            "visibility": "public",
        })
        b.add("pos-2", "REVIEW_POSITIONED", "ex-1", {
            "exception_id": "ex-1",
            "reviewer": {"party_id": "p-fc", "role": "fact_checker"},
            "position": "dissent",
            "visibility": "internal",
        })
        b.add("dec-1", "EXCEPTION_DECIDED", "ex-1", {
            "exception_id": "ex-1",
            "decision": "retained",
            "decided_by": {"party_id": "p-editor", "role": "responsible_editor"},
            "signed_off_by": [{"party_id": "p-editor", "role": "responsible_editor"}],
            "scope": {"span_ids": ["s-1"]},
            "dissent_preserved": [{"party_id": "p-editor", "position": "concur"}],
            "review": {"due_at": "2027-03-01T00:00:00+08:00", "triggers": ["fixed_schedule"]},
        })
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("dec-1", "payload.dissent_preserved", "dissent_overwritten"), codes)

        # 两个立场并存即合法。
        decide = next(e for e in b.events if e["event_id"] == "dec-1")
        decide["payload"]["dissent_preserved"] = [
            {"party_id": "p-editor", "position": "concur"},
            {"party_id": "p-fc", "position": "dissent"},
        ]
        self.assertValid(b.events)

    def test_decision_scope_cannot_expand_proposal(self):
        b = StreamBuilder(self.schema).baseline(freeze=False, publish=False)
        # 登记第二个片段，决定越权把它纳入范围。
        b.events[0]["payload"]["incorporated_span_ids"].append("s-2")
        decide = next(e for e in b.events if e["event_id"] == "dec-1")
        decide["payload"]["scope"]["span_ids"] = ["s-1", "s-2"]
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("dec-1", "payload.scope.span_ids", "scope_expanded"), codes)

    def test_freeze_only_allows_retained(self):
        b = StreamBuilder(self.schema).baseline(decision="rejected", freeze=False, publish=False)
        b.add("frz-1", "RELEASE_FROZEN", "rel-1", {
            "release_id": "rel-1",
            "revision_ref": {"work_id": "work-1", "revision_no": 1},
            "exception_set": [{"exception_id": "ex-1", "retained_basis": "x"}],
            "frozen_at": "2026-09-30T12:00:00+08:00",
        })
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("frz-1", "payload.exception_set", "set_entry_not_retained"), codes)

    def test_publish_requires_freeze_and_is_once_only(self):
        b = StreamBuilder(self.schema).baseline(freeze=False, publish=False)
        b.add("pub-x", "RELEASE_PUBLISHED", "rel-9", {
            "release_id": "rel-9", "published_at": "2026-10-01T12:00:00+08:00",
        })
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("pub-x", "payload.release_id", "broken_reference"), codes)

    def test_signed_judgment_protected_from_batch_override(self):
        b = StreamBuilder(self.schema).baseline()
        b.add("rc-req", "RECOMPUTE_REQUESTED", "job-1", {
            "job_id": "job-1", "trigger": "rule_version_change",
            "affected_span_ids": ["s-1"],
        })
        b.add("rc-cp", "RECOMPUTE_CHECKPOINTED", "job-1", {
            "job_id": "job-1", "seq": 1, "status": "running",
            "processed_span_ids": ["s-1"],
        })
        b.add("rc-done", "RECOMPUTE_COMPLETED", "job-1", {
            "job_id": "job-1", "processed_span_ids": ["s-1"],
            "outcomes": [{"span_id": "s-1", "action": "finding_reopened"}],
        })
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("rc-done", "payload.outcomes", "signed_judgment_protected"), codes)

    def test_flag_for_review_is_allowed_on_resumed_job(self):
        b = StreamBuilder(self.schema).baseline()
        b.add("rc-req", "RECOMPUTE_REQUESTED", "job-1", {
            "job_id": "job-1", "trigger": "source_withdrawal",
            "affected_span_ids": ["s-1"],
        })
        b.add("cp-1", "RECOMPUTE_CHECKPOINTED", "job-1", {
            "job_id": "job-1", "seq": 1, "status": "interrupted",
            "processed_span_ids": ["s-1"],
        })
        b.add("cp-2", "RECOMPUTE_CHECKPOINTED", "job-1", {
            "job_id": "job-1", "seq": 2, "status": "resumed",
            "processed_span_ids": ["s-1"],
        })
        b.add("rc-done", "RECOMPUTE_COMPLETED", "job-1", {
            "job_id": "job-1", "processed_span_ids": ["s-1"],
            "outcomes": [{"span_id": "s-1", "action": "exception_flagged_for_review"}],
        })
        self.assertValid(b.events)

    def test_interrupted_job_must_resume_before_completion(self):
        b = StreamBuilder(self.schema).baseline()
        b.add("rc-req", "RECOMPUTE_REQUESTED", "job-1", {
            "job_id": "job-1", "trigger": "manual", "affected_span_ids": ["s-1"],
        })
        b.add("cp-1", "RECOMPUTE_CHECKPOINTED", "job-1", {
            "job_id": "job-1", "seq": 1, "status": "interrupted",
            "processed_span_ids": ["s-1"],
        })
        b.add("rc-done", "RECOMPUTE_COMPLETED", "job-1", {
            "job_id": "job-1", "processed_span_ids": ["s-1"],
            "outcomes": [{"span_id": "s-1", "action": "no_change"}],
        })
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("rc-done", "payload.job_id", "interruption_not_resumed"), codes)

    def test_completion_must_cover_all_affected_spans(self):
        b = StreamBuilder(self.schema).baseline()
        b.add("rev-2", "REVISION_REGISTERED", "work-1", {
            "work_id": "work-1", "revision_no": 2, "medium": "film",
            "incorporated_span_ids": ["s-2"],
        })
        b.add("rc-req", "RECOMPUTE_REQUESTED", "job-1", {
            "job_id": "job-1", "trigger": "rule_version_change",
            "affected_span_ids": ["s-1", "s-2"],
        })
        b.add("rc-done", "RECOMPUTE_COMPLETED", "job-1", {
            "job_id": "job-1", "processed_span_ids": ["s-1"],
            "outcomes": [{"span_id": "s-1", "action": "no_change"}],
        })
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("rc-done", "payload.processed_span_ids", "incomplete_processing"), codes)

    def test_checkpoint_cannot_creep_beyond_declared_scope(self):
        b = StreamBuilder(self.schema).baseline()
        b.add("rc-req", "RECOMPUTE_REQUESTED", "job-1", {
            "job_id": "job-1", "trigger": "manual", "affected_span_ids": ["s-1"],
        })
        b.add("cp-1", "RECOMPUTE_CHECKPOINTED", "job-1", {
            "job_id": "job-1", "seq": 1, "status": "running",
            "processed_span_ids": ["s-1", "s-9"],
        })
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("cp-1", "payload.processed_span_ids", "scope_creep"), codes)

    def test_erratum_must_reference_published_frozen_exception(self):
        b = StreamBuilder(self.schema).baseline()
        b.add("err-1", "ERRATUM_FILED", "err-1", {
            "erratum_id": "err-1", "release_id": "rel-1",
            "exception_id": "ex-other", "span_id": "s-1",
            "correction": "补署名",
        })
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("err-1", "payload.exception_id", "erratum_unrelated_exception"), codes)
        self.assertIn(("err-1", "payload.exception_id", "broken_reference"), codes)

    def test_versions_must_advance_and_events_be_ordered(self):
        events = copy.deepcopy(StreamBuilder(self.schema).baseline().events)
        events[-1]["version"] = events[-2]["version"]  # 同聚合版本回退
        codes = _codes(validate_stream(events, self.schema))
        self.assertTrue(any(code == "version_not_advanced" for _, _, code in codes))

        ordered = copy.deepcopy(StreamBuilder(self.schema).baseline().events)
        ordered[0]["occurred_at"], ordered[1]["occurred_at"] = ordered[1]["occurred_at"], ordered[0]["occurred_at"]
        codes = _codes(validate_stream(ordered, self.schema))
        self.assertIn(("find-1", "occurred_at", "out_of_order"), codes)

    def test_broken_finding_reference(self):
        b = StreamBuilder(self.schema).baseline(freeze=False, publish=False)
        next(e for e in b.events if e["event_id"] == "prop-1")["payload"]["finding_refs"] = ["nope"]
        codes = _codes(validate_stream(b.events, self.schema))
        self.assertIn(("prop-1", "payload.finding_refs", "broken_reference"), codes)


class ProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data/sample.json").read_text(encoding="utf-8"))

    def test_internal_view_shields_internal_evidence_in_public(self):
        internal = trace_span(self.sample, "shot-12", internal=True)
        public = trace_span(self.sample, "shot-12", internal=False)
        iex = internal["exceptions"][0]
        pex = public["exceptions"][0]
        self.assertTrue(iex["statements"], "内部视图应包含作者陈述")
        self.assertEqual([], pex["statements"], "公开视图必须隐藏内部作者陈述")
        # 内部异议对公众不可见，但决定责任以角色保留。
        public_positions = {(p["reviewer"].get("role"), p["position"]) for p in pex["positions"]}
        self.assertNotIn(("fact_checker", "dissent"), public_positions)
        self.assertEqual({"role": "responsible_editor"}, pex["decision"]["decided_by"])
        self.assertIn("party_id", iex["decision"]["decided_by"])

    def test_trace_explains_responsibility_and_adoption(self):
        view = trace_span(self.sample, "shot-34", internal=True)
        self.assertEqual("work-001", view["work_id"])
        roles = {p["role"] for p in view["exceptions"][0]["decision"]["signed_off_by"]}
        self.assertEqual({"legal", "author"}, roles)
        adoption = view["release_adoptions"][0]
        self.assertEqual("release-001", adoption["release_id"])
        self.assertTrue(adoption["basis_at_time"][0]["retained_basis"])
        self.assertEqual([["verbatim_limit", "credit_required"]],
                         [r["terms"] for r in view["citation_restrictions"]])
        self.assertTrue(any(e["release_id"] == "release-001" for e in view["errata"]))

    def test_release_without_span_does_not_claim_adoption(self):
        b = StreamBuilder(self.schema).baseline()
        # 新版本不再引用 s-1，却把旧例外放进冻结集合：trace 不应把它算作 s-1 的采用。
        b.add("rev-2", "REVISION_REGISTERED", "work-1", {
            "work_id": "work-1", "revision_no": 2, "medium": "film",
            "incorporated_span_ids": ["s-2"],
        })
        b.add("frz-2", "RELEASE_FROZEN", "rel-2", {
            "release_id": "rel-2",
            "revision_ref": {"work_id": "work-1", "revision_no": 2},
            "exception_set": [{"exception_id": "ex-1", "retained_basis": "沿用"}],
            "frozen_at": "2026-11-01T12:00:00+08:00",
        })
        view = trace_span(b.events, "s-1", internal=True)
        self.assertEqual(["rel-1"], [a["release_id"] for a in view["release_adoptions"]])
        self.assertEqual([1], [r["revision_no"] for r in view["revisions_using_span"]])


if __name__ == "__main__":
    unittest.main()
