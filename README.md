# 创作例外保留案卷

保存作品版本、原始素材、自动检查发现、作者意图、编辑判断、表演现场记录、
引用限制、修改建议与发行采用之间的全部审议事实，让每一处被保留的“不工整”
都能回答三个问题：**为什么被保留、由谁承担决定责任、后来怎样被使用。**

## 目录

- `contracts/domain.schema.json`：事件、聚合配对、载荷必填字段与枚举约定。
- `data/sample.json`：单事件联调样例。
- `data/scenario.json`：覆盖完整时间线的 45 个事件场景（含中断/续跑、勘误、批量覆盖拒绝）。
- `src/creative_exception/contracts.py`：基础契约校验（不改写输入）。
- `src/creative_exception/domain.py`：只追加事件存储（幂等、乐观并发、全局序号）。
- `src/creative_exception/services.py`：案卷服务，强制职责分离、并存异议、
  范围/复审、冻结与勘误、精确影响、可中断重算与溯源视图。
- `examples/build_scenario.py`：通过服务生成 `data/scenario.json`，也是用法示例。
- `tests/`：契约与服务不变量测试。
- `docs/domain.md`：领域对象、事件语义与全部业务不变量。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests examples
```

## 样例校验

```bash
PYTHONPATH=src python3 -m creative_exception.cli contracts/domain.schema.json data/sample.json
PYTHONPATH=src python3 -m creative_exception.cli contracts/domain.schema.json data/scenario.json --replay
```

事件文件可以是单个事件对象或事件数组；样例有效时输出 `valid`
（数组为 `valid N events`），`--replay` 额外按存储顺序回放并校验乐观并发版本。
发现问题时逐行给出位置、字段、代码和中文说明，并返回非零状态。

重新生成完整场景：

```bash
PYTHONPATH=src python3 examples/build_scenario.py
```

## 核心规则一览

- 作者陈述意图但不能独自放行版权/事实风险；风险由对应审查官放行，编辑签发，
  且签发人不得同时是该案风险放行决定人。
- 技术人员用新规则批量覆盖已签发艺术判断的尝试一律拒绝并留痕，不改动任何案卷。
- 审读立场（支持/异议/弃权）全部并存追加，后写者不覆盖先写者。
- 每条例外带适用范围与复审条件；签发范围不得超出提案范围。
- 发行冻结的例外集合不可变；出版/上映后当时理由永久保留，新变化只追加勘误。
- 修改建议只能应用到真实引用该片段的后续版本，已出版版本不可改。
- 来源撤回、复审到期、批量依赖重算都可中断并从游标续跑；已出版采用受影响时
  结论为 `erratum_required`。
- 从任意文字段或镜头（`版本#片段`）可还原完整保留链；发行视图按权限区分
  公开说明与内部证据。
