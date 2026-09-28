# 创作例外保留案卷

把作品版本、原始素材、自动检查发现、作者说明、编辑判断、表演现场记录、
引用限制、修改建议与发行采用串联成只追加的审议事件流；并能从任一段文字
或镜头还原它**为何被保留、由谁承担决定责任、后来怎样被使用**。

## 解决的问题

- 自动规则会把有意保留的停顿、错位、临场反应当成瑕疵清除，且事后无法分辨
  “有意保留 / 现场偶然 / 尚未处理”。本案卷把三者的证据与决定分别留痕。
- 作者可以陈述意图，但**不能独自放行**涉版权或事实风险的内容；技术人员
  **不能用新规则批量覆盖**已签发的艺术判断。
- 多人同时审读时异议**并存**，不允许最后写入者覆盖。
- 一次发行**冻结**实际采用的例外集合；已出版/上映版本保留当时理由，
  后续只追加勘误。
- 修订只影响真实引用该片段的版本；来源撤回、复审到期、批量依赖重算被
  中断后可从检查点继续处理。
- 按权限提供**公开说明**或**内部证据**两种视图。

## 目录

- `contracts/domain.schema.json`：事件、聚合归属、载荷字段、枚举与风险签发矩阵。
- `data/sample.json`：覆盖完整链路的 30 条事件样例（可直接校验）。
- `src/creative_exception/`
  - `contracts.py`：单事件结构/枚举校验，不修改输入。
  - `stream.py`：跨事件业务不变量校验（双签、异议并存、冻结一致、续跑等）。
  - `projection.py`：按片段的只读追溯投影（内部 / 公开视图）。
  - `cli.py`：`validate` 与 `trace` 命令。
- `tests/`：事件契约、流不变量与投影测试。
- `docs/domain.md`：领域对象、事件语义与不变量清单。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

## 校验事件流

输入为数组时执行完整的事件 + 流级不变量校验；输入为单个事件对象时只做契约校验。

```bash
PYTHONPATH=src python3 -m creative_exception.cli validate contracts/domain.schema.json data/sample.json
```

有效输出 `valid`；发现问题时逐行给出 `事件标识  字段  代码  中文说明`，并返回非零状态。

## 从片段还原案卷

```bash
# 内部视图：含作者内部陈述、现场记录、并存异议与具体责任人
PYTHONPATH=src python3 -m creative_exception.cli trace contracts/domain.schema.json data/sample.json --span shot-12

# 公开视图：隐藏 internal 证据，责任人只保留角色
PYTHONPATH=src python3 -m creative_exception.cli trace contracts/domain.schema.json data/sample.json --span shot-12 --public
```

事件流存在校验问题时 `trace` 默认拒绝投影（`--force` 可强制输出）；
片段未登记时以退出码 3 明确告知，避免把“查不到”误当成“没有保留记录”。
