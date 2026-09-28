# 领域约定

保存创作版本、原始素材、自动检查发现、艺术例外与发行采用之间的全部审议事实。
所有事实只追加、不覆盖：意图、异议、放行、签发、拒绝、重算结论都各自留痕，
任何判断都能事后还原为“谁、依据什么、在何时、承担什么责任”。

## 聚合

| 聚合 | 含义 |
| --- | --- |
| `work_revision` | 作品版本（文字 `text` 或镜头 `footage`），携带其真实包含的片段标识集合；后续版本可用 `basis_ref` 指向前序版本。 |
| `source_material` | 原始素材（手稿、原始素材、表演现场记录、录音等），登记保管人与其覆盖的片段；可撤回，撤回不可撤销。 |
| `quality_finding` | 自动检查发现，必须携带规则版本与目标片段。 |
| `exception_case` | 创作例外保留案卷：提案、作者意图、并存审读立场、风险放行、编辑签发、修改建议与复审条件。 |
| `revision_suggestion` | 针对例外片段的修改建议及其裁决。 |
| `release_snapshot` | 一次发行冻结的实际采用集合；出版/上映后只能追加勘误。 |
| `audit_entry` | 审计事实，例如批量规则覆盖被拒绝。 |
| `recalculation_job` | 可中断、可续跑的依赖重算任务。 |

片段标识（span）形如 `版本标识#片段标识`（如 `novel-rev1#p12-pause`、
`film-cut1#shot07-take3`），文字段与镜头使用同一套溯源机制。

## 事件

| 事件 | 聚合 | 要点 |
| --- | --- | --- |
| `REVISION_REGISTERED` | work_revision | 版本号从 1 开始递增，登记全部片段。 |
| `SOURCE_RECORDED` / `SOURCE_WITHDRAWN` | source_material | 素材登记与撤回（带原因）。 |
| `FINDING_RAISED` / `FINDING_RESOLVED` | quality_finding | 发现携带 `rule_version`、`target_span`；了结方式为 dismissed/fixed/accepted_as_exception。 |
| `EXCEPTION_PROPOSED` | exception_case | 必须携带适用范围 `scope`、理由、目标片段、风险标记与复审条件 `review`。 |
| `INTENT_STATED` | exception_case | 作者陈述创作意图；只表达意图，不构成放行。 |
| `REVIEW_POSITIONED` | exception_case | 审读立场 support/objection/abstain，全部追加保留。 |
| `RISK_CLEARANCE_DECIDED` | exception_case | 版权/事实风险放行 cleared/conditional/rejected，仅对应审查官可决定。 |
| `EXCEPTION_SIGNED` | exception_case | 编辑签发 approved/rejected/returned，范围与复审条件随签发固化。 |
| `SUGGESTION_RECORDED` / `SUGGESTION_DECIDED` | revision_suggestion | 建议裁决 accepted/rejected/applied。 |
| `RELEASE_FROZEN` / `RELEASE_PUBLISHED` / `ERRATUM_LINKED` | release_snapshot | 冻结不可变；采用集合是冻结集合子集；出版后只追加勘误。 |
| `BATCH_OVERRIDE_REJECTED` | audit_entry | 规则批量覆盖尝试一律拒绝并留痕。 |
| `RECALCULATION_STARTED/INTERRUPTED/RESUMED/COMPLETED` | recalculation_job | 重算生命周期，中断记录游标 `last_processed_ref`，续跑从游标继续。 |

所有发生时间必须携带时区；事件与聚合的配对、载荷必填字段、载荷枚举由
`contracts/domain.schema.json` 固定，基础校验不改写调用方输入。

## 业务不变量（由 `CaseFileService` 强制）

### 职责分离，无人能独自放行

- 作者（author）只能陈述意图（INTENT_STATED），不能签发、不能做版权/事实放行。
- 版权风险只由 copyright_officer、事实风险只由 factual_officer 放行；
  `conditional` 必须列出条件；案卷未标记的风险域无需放行也不得决定。
- 例外批准的前置条件：已有作者意图陈述；案卷标记的每个风险域都有对应审查官的
  cleared/conditional 决定；签发编辑不是该案任何风险放行的决定人。
- 技术人员（engineer）可以提交发现、建议、发起重算，但不能签发；
  以新规则对已签发判断做批量覆盖时，不修改任何案卷，只写一条
  `BATCH_OVERRIDE_REJECTED` 审计。
- 编辑签发后即视为已签发艺术判断：同案再次 approved 被拒绝，只能通过
  复审、重算或勘误渠道处理。

### 适用范围与复审条件

- 提案范围必须非空、覆盖发现所在版本，且范围片段必须属于所列版本。
- 签发范围只能是提案范围的子集，且必须继续覆盖例外片段所在版本。
- `review.review_required=true` 时必须给出带时区的 `review_due` 与触发说明。
- 后续修订/后续发行若真实引用同一片段（新版本的片段集合携带同一 span_id），
  原签发例外可继续适用；不引用的版本不受影响。修改建议 applied 时做同样校验，
  且不能应用到已出版版本。

### 异议并存

REVIEW_POSITIONED 只追加。同一审读者改念、支持与异议同时存在时全部保留，
按发生时间排序，不存在最后写入者覆盖。已了结的发现、已裁决的建议同样不可改写。

### 冻结、出版与勘误

- RELEASE_FROZEN 的例外集合一经写入不可变；入集例外必须已批准且适用范围覆盖该版本。
- RELEASE_PUBLISHED 的实际采用集合必须是冻结集合的子集（默认全部采用）。
- 已出版/上映版本保留当时签发理由；后续变化只能以 ERRATUM_LINKED 追加勘误，
  勘误只能指向本发行实际采用的例外，同一勘误不可重复关联。

### 可中断的依赖重算

三个触发源：`source_withdrawn`（来源撤回）、`review_due`（复审到期）、
`batch_rule_recompute`（批量规则依赖重算）。

- 来源撤回自动开启重算（调用方提供 job_id 时）；按例外片段与素材覆盖片段的
  真实引用关系确定受影响集合。
- 处理可在任意条目后中断（RECALCULATION_INTERRUPTED 记录已产出结果与游标），
  RESUMED 后只处理剩余条目；已产出结果不重算。
- 结论语义：
  - source_withdrawn：已出版采用 → `erratum_required`；已签发未出版 →
    `reopened_for_review`；未签发 → `recheck_required`。
  - review_due：到期后有新的审读支持或风险再确认 → `reaffirmed`，
    否则 `review_required`。
  - batch_rule_recompute：已签发 → `retained`（同时留批量覆盖拒绝审计），
    未签发 → `recheck_required`。

### 溯源与权限视图

- `trace_span(版本#片段)` 从任意文字段或镜头还原：来源素材（含撤回状态）、
  检查发现与规则版本、例外提案与理由、作者意图、全部并存立场、风险放行、
  签发责任人、适用范围与复审条件、冻结/出版记录、修改建议、相关重算结论，
  以及按时间排列的引用时间线。
- `release_view` 按角色区分：编辑/审查官/admin 得到内部证据（完整案卷与冻结集合）；
  其他身份得到公开说明（片段、公开理由、勘误），不暴露内部证据。

## 存储语义

`EventStore` 只追加事件：相同 `event_id` 重放返回原事件（业务幂等）；
按聚合做乐观并发（version 必须等于下一序号，否则报并发冲突）；
全局 `seq` 保证溯源顺序；读出的载荷为深拷贝，历史不可被回写。
冲突隔离、订阅投递等由上层服务负责，本仓库定义可稳定交换的事实与上述业务不变量。
