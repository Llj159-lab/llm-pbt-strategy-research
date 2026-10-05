# 结构保持编辑器与受限修复器开发

## 研究目的

为保证受限修复只补充测试场景而不改变原有测试语义，本实验将自由文本编辑改为版本化 JSON DSL。Python 测试文件由确定性 AST 变换器生成，输入工件不能直接提交完整替代文件。

本实验考察两个问题：

1. 编辑器能否保留基线测试、既有断言和 import；
2. 结构化操作能否表达已知的状态转换和操作序列补全。

## DSL v1

| 操作 | 作用 | 保持性约束 |
| --- | --- | --- |
| `extend_given` | 使用 `st.one_of` 扩大既有 `@given` 参数的 strategy 支持集 | 原 strategy 作为 `one_of` 的第一分支保留；测试体不变 |
| `add_examples` | 向既有测试增加 JSON 可表示的 `@example` | 不执行任意表达式；测试体和断言不变 |
| `clone_test` | 克隆既有测试，并补充状态操作或替换克隆中的赋值表达式 | 原测试保持不变；克隆测试继承来源测试的断言 AST |

插入语句只允许赋值、带注解赋值、增量赋值和表达式调用。控制流、`assert`、`return`、`raise`、异常处理、import、函数或类定义、dunder 访问，以及 `eval`、`exec`、`open` 等调用均被拒绝。

## 静态审计不变量

每次应用 DSL 后执行原有 AST 审计和结构化附加审计：

- 基线测试函数集合全部保留；
- 既有测试体 AST 保持不变；
- 既有断言 AST 保持不变；
- import 保持不变；
- 不新增重复测试名、skip、xfail、宽泛异常处理或断言前提前返回；
- 克隆测试的断言序列与来源测试一致；
- 记录 baseline、规范和生成文件的 SHA-256。

实现文件为：

- `experiments/constrained_repair/structured_edit.py`；
- `experiments/constrained_repair/apply_structured_repair.py`；
- `experiments/tests/test_structured_repair.py`。

## 单元测试

使用以下命令执行编辑器与审计器测试：

```powershell
python -m pytest -q experiments/tests/test_structured_repair.py experiments/tests/test_constrained_repair_audit.py
```

结果为 `32 passed`。测试覆盖三类允许的编辑，以及断言插入、提前返回、控制流、异常处理、import、危险调用、dunder 访问、任意 strategy 调用、重复测试名、未定义赋值目标和与原 strategy 完全相同的 no-op 等拒绝情形。审计器还覆盖克隆测试继承来源测试中既有 `return` 的情况：继承的结构不会被误报为新增违规，真正新增的提前返回仍会被拒绝。

该结果证明编辑器和审计器在单元测试范围内按规则工作；生成测试能否发现缺陷由后续动态评估单独判断。

## 开发工件

三个开发任务的 DSL 规范依据已知 ground truth 设计，用于验证 DSL 的表达能力和审计边界。

### BIDC-003

规范文件为 `experiments/configs/structured-repair-development-bidc-v1.json`：

1. 克隆 `test_bidict_inverse_consistency`，在原不变量之前对已有 key 执行一次更新；
2. 克隆 `test_orderedbidict_copy_order_fidelity`，将克隆中的重建操作替换为原生 `ob.copy()`；
3. 两份克隆均继承来源测试的既有断言，不新增 oracle。

生成收据记录如下：

- baseline SHA-256：`e37e561651d361ab850e35a332d2cd9a10e66a891c2ba936570b3c4e983992b6`；
- specification SHA-256：`5517781558281562f72d520f579f1eb1ecd7d3f1f5759ae324ad982abd2427b4`；
- repaired SHA-256：`6ba8f8945f7319b183bcccc5ebe48321936616abd68e4b61a6ef506903b54563`；
- 测试函数数：`11 -> 13`；
- 既有测试体变更：`0`；
- 既有断言变更：`0`；
- AST/diff 审计：`accepted`。

### CACH-005

规范文件为 `experiments/configs/structured-repair-development-cach-v1.json`。该规范克隆 `test_tlrucache_expire_method`，在不改变来源断言的前提下加入一个较晚过期的缓存项，使 `expire()` 同时面对“较早项已过期、较晚项未过期”的堆顺序场景。

- baseline SHA-256：`8032ce0f8a4cacac19b803298e8bd5cc600ef8ecad87431892a7d79d2705cc88`；
- specification SHA-256：`6286544e9439eba9f426a16af069bb08e6ce830e0ba689cac8d96c5d2a7bbc9c`；
- repaired SHA-256：`bce8d990639c5e2c5b12bf3e37d6997065df3e5f710ac28b71ae8336c7ed4934`；
- 测试函数数：`8 -> 9`；
- 既有测试体和既有断言变更：`0`；
- AST/diff 审计：`accepted`。

### CTRS-001

规范文件为 `experiments/configs/structured-repair-development-ctrs-v1.json`。该规范克隆 alias 测试，将 converter 配置替换为 `detailed_validation=False`，并注册使用 alias 的结构化 hook，以表达 `bug_1` 所需的组合配置。

- baseline SHA-256：`bcde7fbca7c85b189995080aa71301286af26397507968f12a9d05a3301bb35e`；
- specification SHA-256：`c05e2e7a8226d2f8a9761db36fe56624a6a7c6c80d794bfe0b4caa257213d50b`；
- repaired SHA-256：`6a374c8e6d27c50960b643992a2d682c717b7c616750afac806af5019bfec15c`；
- 测试函数数：`12 -> 13`；
- 既有测试体和既有断言变更：`0`；
- AST/diff 审计：`accepted`。

## 配对动态评估

冻结 baseline 与结构化修复版使用相同的评估器、执行参数和任务环境。参数为 `collection-timeout=120`、`setup-timeout=300`、`eval-deadline=1800`。六次评估均成功收集测试，超时数为 `0`，`eval_phase_aborted=false`。

严格结果按以下规则判定：至少一个测试函数在 buggy 版本失败，并在只修复目标缺陷的版本通过。混合宽松结果中的 `bugs_found` 不替代严格 F-to-P。

| 任务 | baseline 严格 F-to-P | 结构化修复严格 F-to-P | 新增测试的直接贡献 | 结果解释 |
| --- | ---: | ---: | --- | --- |
| `BIDC-003` | `0/4` | `2/4` | `bug_1`、`bug_4` | 两个新增克隆测试各形成一个严格 F-to-P；baseline 的 `bug_2`、`bug_3` 仅为宽松发现 |
| `CACH-005` | `2/4` | `3/4` | `bug_4` | baseline 命中 `bug_2`、`bug_3`；新增克隆测试补充命中 `bug_4` |
| `CTRS-001` | `1/4` | `2/4` | 无 | 新增克隆测试在 buggy 与 fixed 版本中均失败；表面增加的 `bug_1` 来自原有测试跨轮结果变化，不能归因于 DSL 编辑 |

这些结果是依据已知 ground truth 编写的人工 DSL 开发集结果，不是模型生成结果，也不进入未见任务统计。BIDC 和 CACH 表明结构保持 DSL 可以表达产生直接严格 F-to-P 的场景补全；CTRS 暴露了单次 Hypothesis 执行的跨轮不稳定性。

## 结论与证据边界

结构化修复器已完成实现、单元测试、三个开发任务的静态审计和配对动态评估。三份人工 DSL 工件均通过审计；BIDC 和 CACH 的结果提供了结构化操作能够产生新严格 F-to-P 的机制证据。

该 DSL 版本的规范由人工依据已知 ground truth 编写，模型能否自主产生合规且有效的 DSL、以及方法在未见任务上的严格 F-to-P 表现，尚未由本实验验证。相关结论应与模型生成实验和未见任务评估分开报告。
