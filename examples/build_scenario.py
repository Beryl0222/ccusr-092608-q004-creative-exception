"""生成完整联调场景 data/scenario.json。

通过案卷服务走完一条真实时间线，导出全部事件；该文件可直接用
``python -m creative_exception.cli contracts/domain.schema.json data/scenario.json --replay``
回放校验。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from creative_exception.domain import EventStore
from creative_exception.services import CaseFileService, Principal, span_ref

SCHEMA = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))

editor = Principal("editor-lin", "editor")
reviewer_b = Principal("editor-zhao", "editor")
author = Principal("author-mo", "author")
engineer = Principal("engineer-qi", "engineer")
copyright_officer = Principal("copyright-yu", "copyright_officer")


def build() -> list[dict]:
    store = EventStore(SCHEMA)
    svc = CaseFileService(store)
    n = 0

    def eid() -> str:
        nonlocal n
        n += 1
        return f"evt-{n:03d}"

    pause = span_ref("novel-rev1", "p12-pause")
    quote = span_ref("novel-rev1", "p30-quote")
    shot = span_ref("film-cut1", "shot07-take3")

    # 1. 作品版本：小说初版（文字）与初剪（镜头）
    svc.register_revision(
        "novel-rev1", "novel-silence", 1, "text",
        ["p12-pause", "p30-quote"], editor, "2026-09-01T09:00:00+08:00",
        event_id=eid(),
    )
    svc.register_revision(
        "film-cut1", "film-silence", 1, "footage",
        ["shot07-take3"], editor, "2026-09-02T09:00:00+08:00",
        event_id=eid(),
    )

    # 2. 原始素材与表演现场记录
    svc.record_source(
        "src-manuscript", "manuscript", "archive-room",
        [pause, quote], editor, "2026-09-01T10:00:00+08:00", event_id=eid(),
    )
    svc.record_source(
        "src-raw-footage", "raw_footage", "studio-vault",
        [shot], editor, "2026-09-02T10:00:00+08:00", event_id=eid(),
    )
    svc.record_source(
        "src-performance-log", "performance_log", "script-supervisor",
        [shot], editor, "2026-09-02T10:30:00+08:00", event_id=eid(),
    )

    # 3. 自动检查发现（规则 3.0）
    svc.raise_finding(
        "f-pause", "rule-3.0", pause, engineer, "2026-09-03T08:00:00+08:00",
        detail="检测到超长停顿，疑似排版瑕疵", event_id=eid(),
    )
    svc.raise_finding(
        "f-quote", "rule-3.0", quote, engineer, "2026-09-03T08:05:00+08:00",
        detail="检测到未登记引用", event_id=eid(),
    )
    svc.raise_finding(
        "f-shot", "rule-3.0", shot, engineer, "2026-09-03T08:10:00+08:00",
        detail="镜头轻微抖动，疑似稳定器故障", event_id=eid(),
    )

    # 4. 例外提案：适用范围、理由、引用风险、复审条件
    svc.propose_exception(
        "case-pause", "f-pause",
        {"revisions": ["novel-rev1"], "spans": [pause]},
        "三秒无声停顿是母女和解一场的情绪落点，属刻意保留的节奏",
        author.id, editor, "2026-09-04T09:00:00+08:00",
        review={"review_required": True, "review_trigger": "reprint",
                "review_due": "2027-01-01T00:00:00+08:00"},
        event_id=eid(),
    )
    svc.propose_exception(
        "case-quote", "f-quote",
        {"revisions": ["novel-rev1"], "spans": [quote]},
        "段落引用地方戏唱词四行，虚构人物口述，服务于情节真实感",
        author.id, editor, "2026-09-04T09:10:00+08:00",
        risk_flags=["copyright"],
        review={"review_required": False},
        event_id=eid(),
    )
    svc.propose_exception(
        "case-shot", "f-shot",
        {"revisions": ["film-cut1"], "spans": [shot]},
        "手持微抖与演员临场哽咽同框，是表演现场不可复拍的真实反应",
        author.id, editor, "2026-09-04T09:20:00+08:00",
        review={"review_required": False},
        event_id=eid(),
    )

    # 5. 作者陈述意图（只陈述，不放行风险）
    svc.state_author_intent(
        "case-pause", "停顿处我刻意删去全部心理描写，让沉默本身说话。",
        author, "2026-09-04T10:00:00+08:00", event_id=eid(),
    )
    svc.state_author_intent(
        "case-quote", "唱词只取四句意象，不构成整段搬用，且与人物口音绑定。",
        author, "2026-09-04T10:05:00+08:00", event_id=eid(),
    )
    svc.state_author_intent(
        "case-shot", "take3 的哽咽是演员临场反应，我要求保留而非补拍。",
        author, "2026-09-04T10:10:00+08:00", event_id=eid(),
    )

    # 6. 多人审读：支持与异议并存，后写者不覆盖先写者
    svc.position_review(
        "case-pause", "support", editor, "2026-09-05T09:00:00+08:00",
        note="初校与二校都确认停顿是有意安排", event_id=eid(),
    )
    svc.position_review(
        "case-pause", "objection", reviewer_b, "2026-09-05T09:30:00+08:00",
        note="担心纸书读者误认缺字，建议加编者注", event_id=eid(),
    )
    svc.position_review(
        "case-shot", "support", editor, "2026-09-05T10:00:00+08:00",
        note="导演与场记现场记录均支持保留", event_id=eid(),
    )

    # 7. 版权风险由版权审查官附条件放行
    svc.decide_risk_clearance(
        "case-quote", "copyright", "conditional", copyright_officer,
        "2026-09-06T09:00:00+08:00",
        conditions=["再版前补取曲牌出处确认", "公开说明只写“地方戏曲牌”不录全文"],
        note="四行属于短引用，附署名条件放行", event_id=eid(),
    )

    # 8. 编辑签发艺术判断（范围与复审条件随签发固化）
    svc.sign_exception(
        "case-pause", "approved",
        {"revisions": ["novel-rev1"], "spans": [pause]},
        editor, "2026-09-07T09:00:00+08:00",
        review={"review_required": True, "review_trigger": "reprint",
                "review_due": "2027-01-01T00:00:00+08:00"},
        note="异议（加编者注）随卷保留，正文维持停顿", event_id=eid(),
    )
    svc.sign_exception(
        "case-quote", "approved",
        {"revisions": ["novel-rev1"], "spans": [quote]},
        editor, "2026-09-07T09:10:00+08:00",
        review={"review_required": False},
        note="按版权审查官条件放行，公开说明不录唱词全文", event_id=eid(),
    )
    svc.sign_exception(
        "case-shot", "approved",
        {"revisions": ["film-cut1"], "spans": [shot]},
        editor, "2026-09-07T09:20:00+08:00",
        review={"review_required": False}, event_id=eid(),
    )
    svc.resolve_finding(
        "f-pause", "accepted_as_exception", editor, "2026-09-07T10:00:00+08:00",
        case_ref="case-pause", event_id=eid(),
    )
    svc.resolve_finding(
        "f-quote", "accepted_as_exception", editor, "2026-09-07T10:05:00+08:00",
        case_ref="case-quote", event_id=eid(),
    )
    svc.resolve_finding(
        "f-shot", "accepted_as_exception", editor, "2026-09-07T10:10:00+08:00",
        case_ref="case-shot", event_id=eid(),
    )

    # 9. 修改建议：停顿建议被驳回；引用措辞建议留待真实引用该片段的修订
    svc.record_suggestion(
        "sug-pause-trim", "case-pause", engineer.id,
        "建议把停顿压缩到 1 秒以统一节奏", engineer,
        "2026-09-08T09:00:00+08:00", event_id=eid(),
    )
    svc.decide_suggestion(
        "sug-pause-trim", "rejected", editor, "2026-09-08T11:00:00+08:00",
        event_id=eid(),
    )

    # 10. 冻结一次发行实际采用的例外集合，并出版/上映
    svc.freeze_release(
        "rel-book-1", "novel-rev1", ["case-pause", "case-quote"],
        editor, "2026-10-01T08:00:00+08:00", event_id=eid(),
    )
    svc.publish_release(
        "rel-book-1", editor, "2026-10-05T08:00:00+08:00", event_id=eid(),
    )
    svc.freeze_release(
        "rel-film-1", "film-cut1", ["case-shot"],
        editor, "2026-10-02T08:00:00+08:00", event_id=eid(),
    )
    svc.publish_release(
        "rel-film-1", editor, "2026-10-08T08:00:00+08:00", event_id=eid(),
    )

    # 11. 后续修订只影响真实引用该片段的版本：rev2 仍带 p30-quote
    svc.register_revision(
        "novel-rev2", "novel-silence", 2, "text", ["p30-quote"],
        editor, "2026-10-20T09:00:00+08:00",
        basis_ref="novel-rev1", event_id=eid(),
    )
    svc.record_suggestion(
        "sug-quote-rewrite", "case-quote", editor.id,
        "再版将四行唱词替换为同义概述，消除版权依赖",
        editor, "2026-10-21T09:00:00+08:00", event_id=eid(),
    )
    svc.decide_suggestion(
        "sug-quote-rewrite", "applied", editor, "2026-10-22T09:00:00+08:00",
        applied_to="novel-rev2", event_id=eid(),
    )

    # 12. 来源撤回触发重算：处理一批时中断，随后断点续跑
    svc.withdraw_source(
        "src-manuscript", "手稿授权被权利方撤回，需复核全部引用依据",
        editor, "2026-11-01T09:00:00+08:00",
        job_id="job-src-1", process_limit=1, event_id=eid(),
    )
    svc.resume_recalculation(
        "job-src-1", editor, "2026-11-01T09:30:00+08:00", event_id=eid(),
    )

    # 13. 已出版版本保留当时理由，以勘误关联被撤回来源影响的例外
    svc.link_erratum(
        "rel-book-1", "erratum-2026-11-01",
        ["case-quote", "case-pause"], editor, "2026-11-02T09:00:00+08:00",
        event_id=eid(),
    )

    # 14. 技术人员试图用新规则批量覆盖已签发判断：拒绝并留痕，随后做依赖重算
    svc.attempt_batch_override(
        "rule-4.0", ["case-pause", "case-quote", "case-shot"],
        engineer, "2026-11-10T09:00:00+08:00",
        reason="统一节奏/引用规则 4.0 全量上线，要求自动改写历史判断",
        audit_id="audit-rule4-rollout", event_id=eid(),
    )
    svc.recompute_for_rule(
        "job-rule4", "rule-4.0",
        ["case-pause", "case-quote", "case-shot"],
        engineer, "2026-11-10T09:05:00+08:00", event_id=eid(),
    )

    # 15. 复审到期重算（停提案卷约定 2027-01-01 复审）
    svc.run_due_reviews(
        "job-review-due", editor, "2027-02-01T09:00:00+08:00", event_id=eid(),
    )

    return [event.to_dict() for event in store.events()]


def main() -> int:
    events = build()
    out = ROOT / "data/scenario.json"
    out.write_text(json.dumps(events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(events)} events to {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
