# LLM 生成属性测试研究记录

## 文档范围

本目录保存基线评测、留出任务观察、人工诊断、分类体系、受限修复开发、结构化 DSL 开发和三方对照试验的正式记录。记录不改写 benchmark 数据、第三方依赖、源代码、ground truth 或原始评测产物。

## 文件索引

| 文件 | 内容 |
| --- | --- |
| `baseline-evaluations.md` | 五项基线评测与结果解析 |
| `heldout-failure-mode-study.md` | 五项留出任务的失败模式观察 |
| `diagnostic-interventions.md` | 人工场景诊断与稳定性补充 |
| `state-transition-taxonomy.md` | 状态转换与操作序列覆盖遗漏的操作性定义 |
| `constrained-repair-development.md` | 受限修复器开发验证 |
| `blinded-three-arm-pilot.md` | 四项冻结任务的三方对照试验 |
| `data-provenance-and-limitations.md` | 数据来源、证据边界和研究局限 |
| `structured-repair-engine-development.md` | 结构保持编辑 DSL 与静态审计 |
| `model-structured-repair-development.md` | 模型生成 JSON DSL 的开发验证 |
| `diagnosis-guided-structured-repair-results.md` | 诊断驱动结构化修复结果 |
| `semantic-typed-operation-dsl-protocol.md` | 语义约束类型化操作 DSL 协议 |
| `semantic-typed-operation-dsl-results.md` | 语义约束类型化操作 DSL 结果 |
| `typed-operation-dsl-development.md` | 类型化操作 DSL 开发记录 |
| `frozen-protocols.md` | 留出任务、人工诊断与三方对照协议 |

## 结果判定规则

- 主要指标为逐 bug 严格 F-to-P：至少一个生成测试函数在 buggy 版本失败，并在只修复目标 bug 的版本通过。
- 宽松检测单独报告，绝不替代严格 F-to-P。
- 人工诊断与模型生成结果分开报告；人工诊断不能改写原始模型成绩。
- 公开研究材料只保留脱敏摘要和可复核的必要信息。
