# 正式实验清单

本文档列出公开仓库中与主研究问题直接相关的实验及其证据位置。

| 实验 | 目的 | 主要记录 |
| --- | --- | --- |
| 基础设施与数据协议验收 | 验证任务目录、可见性边界和 evaluator 闭环 | `data-protocol-acceptance.md`、`evaluation-visibility-checklist.md` |
| 早期正式基线评测 | 观察模型在开发任务上的严格 F→P 与宽松检测 | `baseline-evaluations.md` |
| 留出任务评测 | 在冻结任务上观察严格漏检及其执行状态 | `heldout-failure-mode-study.md` |
| 人工场景诊断 | 使用代码与执行证据解释部分漏检机制 | `diagnostic-interventions.md` |
| 状态转换与操作序列分类 | 冻结相关失败模式的操作性定义和判定边界 | `state-transition-taxonomy.md` |
| 受限修复开发验证 | 检查 strategy/操作序列受限修复的工程可行性 | `constrained-repair-development.md` |
| 三方对照试点 | 比较原始基线、受限修复和完整重生成 | `blinded-three-arm-pilot.md` |
| 结构化操作 DSL 验证 | 检查类型化编辑、AST 变换和审计约束 | `semantic-typed-operation-dsl-protocol.md`、`semantic-typed-operation-dsl-results.md` |

模型结果、人工诊断和修复验证分别记录。正式结论以逐 bug 严格 F→P、宽松检测、执行状态和编辑审计为依据。
