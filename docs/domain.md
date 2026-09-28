# 领域约定

保存作品版本、原始素材、质量发现、艺术例外、引用限制、修改建议、
发行采用、复审、撤回、勘误与批量重算之间**可稳定交换的基础事实**。
事件只追加、不改写；基础校验不修改调用方输入。业务幂等、并发冲突
隔离与持久化由上层服务负责。

## 聚合与事件

| 聚合 | 事件 |
| --- | --- |
| `work_revision` | `REVISION_REGISTERED` |
| `source_material` | `SOURCE_REGISTERED`、`SOURCE_WITHDRAWN` |
| `quality_finding` | `FINDING_RAISED` |
| `exception_case` | `EXCEPTION_PROPOSED`、`AUTHOR_STATEMENT_RECORDED`、`ON_SET_RECORD_ATTACHED`、`REVIEW_POSITIONED`、`EXCEPTION_DECIDED`、`REVIEW_OUTCOME_RECORDED`、`RULE_OVERRIDE_REJECTED` |
| `citation_restriction` | `CITATION_RESTRICTION_IMPOSED` |
| `revision_suggestion` | `REVISION_SUGGESTED` |
| `release_snapshot` | `RELEASE_FROZEN`、`RELEASE_PUBLISHED` |
| `erratum` | `ERRATUM_FILED` |
| `recompute_job` | `RECOMPUTE_REQUESTED`、`RECOMPUTE_CHECKPOINTED`、`RECOMPUTE_COMPLETED` |

事件类型与聚合归属的对应关系固定在 schema 的 `aggregate_by_event`，
归属不一致即 `aggregate_mismatch`。所有发生时间必须携带时区，事件流
按 `occurred_at` 非递减排列；同一聚合的 `version` 严格递增；作品版本号
`revision_no` 从 1 起递增。

## 片段（span）是追溯的锚点

- `REVISION_REGISTERED.incorporated_span_ids` 登记该版本**真实采用**的文字或镜头片段。
- 发现、例外、引用限制、修改建议、重算结果都指向 `span_id`，且片段必须先在某版本登记。
- 后续任何修订只影响真实引用该片段的版本；投影采用关系时要求“冻结版本纳入该片段”
  与“冻结集合包含该片段的例外”同时成立，防止把未采用的版本误记为采用。

## 例外审议与签发边界

- 作者通过 `AUTHOR_STATEMENT_RECORDED` 陈述意图；现场偶然由 `ON_SET_RECORD_ATTACHED`
  引用原始素材佐证。两者均可标 `visibility=public/internal`。
- 多人审读用多条 `REVIEW_POSITIONED`（`concur/dissent/abstain`）并存。
- `EXCEPTION_DECIDED` 必须在 `dissent_preserved` 中原样列出决定时已存在的**全部**
  立场；缺列即 `dissent_overwritten`（最后写入者不得覆盖）。
- 决定的适用范围不得超出提案范围（`scope_expanded`）。
- 任何 `retained` / `retained_with_conditions` 都必须带 `review.due_at` 与至少一个
  `review.triggers`（适用范围与复审条件必须明确）。
- 风险签发（schema `risk_signoff`）：
  - `artistic_only`：作者或责任编辑可放行；
  - `copyright`：必须有 `legal` 签发；
  - `factual`：必须有 `fact_checker` 签发；
  - 涉版权/事实时只有作者签发 → `author_cannot_self_release`；作者可以陈述，但不能独自放行。
- `decided_by.role=technician` → `technician_cannot_decide`。技术侧只能发起重算，
  不能签发艺术判断。
- 技术人员尝试用新规则批量改判已签发例外时登记 `RULE_OVERRIDE_REJECTED`；该事件必须
  指向一个**已决定**的例外。

## 发行冻结、不可变与勘误

- 一次发行只能 `RELEASE_FROZEN` 一次（`release_refrozen`），冻结集合中的每条例外
  必须已被签发为保留（`set_entry_not_retained`）。
- `RELEASE_PUBLISHED` 必须先冻结，且一次发行只出版/上映一次。
- 已出版/上映版本保留当时理由：冻结条目的 `retained_basis` 不被后续事件改写。
- `ERRATUM_FILED` 只能追加：必须关联已出版发行、该发行冻结集合中的例外，以及冻结版本
  真实采用的片段（`erratum_unrelated_*`）。

## 撤回、复审与批量重算续跑

- `SOURCE_WITHDRAWN` 触发既有例外复审，结果用 `REVIEW_OUTCOME_RECORDED`
  （`upheld/amended/revoked`）。已上映版本不因撤回被改写，只影响后续发行。
- 重算任务三段式：`RECOMPUTE_REQUESTED` → 若干 `RECOMPUTE_CHECKPOINTED`
  （`seq` 从 1 严格递增，状态 `running/interrupted/resumed`）→ `RECOMPUTE_COMPLETED`。
- 检查点与完成的处理片段不得超出任务声明的 `affected_span_ids`（`scope_creep`）。
- 完成时必须覆盖全部受影响片段（`incomplete_processing`）；出现过 `interrupted`
  必须先有 `resumed` 才能完成（`interruption_not_resumed`），从而在来源撤回、复审到期、
  批量依赖重算被打断后可从检查点继续。
- 对**已签发且仍生效**的艺术保留，重算结果只允许 `no_change` 或
  `exception_flagged_for_review`；`finding_reopened` 等直接改判触发
  `signed_judgment_protected`。

## 追溯与可见性

`trace_span(events, span_id, internal=...)` 从任一片段折叠出：采用它的版本、
针对它的发现、例外（提案理由／作者说明／现场记录／并存立场／决定与签发人／复审）、
引用限制、修改建议、真实采用它的发行及当时理由、勘误、重算影响。

- `internal=True`：给出含内部证据与具体责任人标识的内部案卷。
- `internal=False`（公开）：剔除 `visibility=internal` 的陈述、现场记录与立场，
  责任人只保留角色，公开说明不暴露具体身份。

## 事件载荷

各事件必填字段见 schema 的 `payload_required_by_event`；枚举见 `payload_enums`，
枚举适用路径按事件类型登记在 `payload_enum_paths_by_event`（例如 `trigger` 在复审
事件取复审触发枚举、在重算事件取重算触发枚举，互不相混）。
