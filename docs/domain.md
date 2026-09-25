# 领域约定

保存创作版本、质量发现、艺术例外与发行采用之间的审议事件。

聚合对象包括`work_revision`、`quality_finding`、`exception_case`、`release_snapshot`。事件类型包括`REVISION_REGISTERED`、`FINDING_RAISED`、`EXCEPTION_PROPOSED`、`REVIEW_POSITIONED`、`RELEASE_FROZEN`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `FINDING_RAISED`：载荷还需包含 `rule_version`, `target_span`。
- `EXCEPTION_PROPOSED`：载荷还需包含 `scope`, `rationale`。
- `RELEASE_FROZEN`：载荷还需包含 `revision_ref`, `exception_set`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
