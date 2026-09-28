import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from creative_exception.contracts import validate_event


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        stream = json.loads((ROOT / "data/sample.json").read_text(encoding="utf-8"))
        cls.sample = stream[0]

    def test_sample_event_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_missing_fields_are_stable(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(x.field for x in issues), [x.field for x in issues])

    def test_time_and_version_boundaries(self) -> None:
        event = dict(self.sample, occurred_at="2026-09-25T10:00:00", version=0)
        codes = {(x.field, x.code) for x in validate_event(event, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_event_payload_is_required(self) -> None:
        event = dict(self.sample, event_type="FINDING_RAISED", payload={})
        issues = validate_event(event, self.schema)
        self.assertIn(("payload.rule_version", "required"), [(x.field, x.code) for x in issues])

    def test_unknown_event_is_rejected(self) -> None:
        issues = validate_event(dict(self.sample, event_type="UNKNOWN"), self.schema)
        self.assertIn(("event_type", "unsupported_value"), [(x.field, x.code) for x in issues])

    def test_aggregate_must_match_event(self) -> None:
        issues = validate_event(dict(self.sample, aggregate_type="exception_case"), self.schema)
        self.assertIn(("aggregate_type", "aggregate_mismatch"), [(x.field, x.code) for x in issues])

    def test_enum_path_is_scoped_by_event_type(self) -> None:
        # REVIEW_OUTCOME_RECORDED.trigger 用复审触发枚举；recompute 的枚举值在此应被拒绝。
        event = {
            "event_id": "e",
            "event_type": "REVIEW_OUTCOME_RECORDED",
            "aggregate_type": "exception_case",
            "aggregate_id": "ex-1",
            "occurred_at": "2026-09-25T10:00:00+08:00",
            "version": 1,
            "payload": {
                "exception_id": "ex-1",
                "trigger": "rule_version_change",
                "outcome": "upheld",
                "reviewed_by": {"party_id": "p-1", "role": "legal"},
            },
        }
        issues = validate_event(event, self.schema)
        self.assertIn(("payload.trigger", "unsupported_value"), [(x.field, x.code) for x in issues])


if __name__ == "__main__":
    unittest.main()
