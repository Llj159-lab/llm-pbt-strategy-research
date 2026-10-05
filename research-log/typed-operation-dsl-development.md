# 类型化操作 DSL：开发阶段协议与结果

## 研究目的

上一版结构化修复允许模型在 JSON 字段中直接写 Python 语句。模型可能输出函数定义、lambda、控制流、空字符串或重复定义源测试已有变量；即使 AST 审计拒绝这些输出，也无法检查操作对象和变量是否在插入位置已经存在。本阶段将模型输出改为类型化操作节点，由确定性程序负责生成 Python AST。

开发任务为 `BIDC-003`、`CACH-005` 和 `CTRS-001`。模型不可见 ground truth、补丁或评估器输出。本阶段验证类型化 DSL 的结构约束、模型工件接受情况和配对动态结果。

## 类型化 DSL

新协议使用 repair specification `version=2`。每个编辑只能是一个 `clone_test`，不得包含旧版 Python 字符串字段。可用字段为：

- `steps`：在克隆测试 docstring 之后插入的操作列表；
- `insert_before_steps`：以冻结源测试中的一个完整顶层语句为位置锚点的操作列表；
- `replace_assignments`：按已有简单变量名替换赋值右侧的类型化值。

操作节点限定为：

1. `method_call`：对已经存在的标识符接收者调用非 dunder 方法；
2. `function_call`：调用已经存在的模块、局部函数或安全内置函数；
3. `set_item`：对已经存在的映射或序列对象执行下标赋值；
4. `delete_item`：对已经存在的映射或序列对象执行下标删除。

操作参数直接使用 JSON 值表示字面量；使用 `{ref: name}` 表示已有变量引用。所有引用、接收者、函数名、目标名和方法名均经过标识符校验。`assign_to` 只能创建源测试中不存在的新变量，不能覆盖或重复定义源测试已有局部变量。

## 确定性 AST 审计

变换器在每个插入位置建立可用符号集合，包括模块导入、模块级定义、测试函数参数以及插入位置之前已经定义的局部变量。审计拒绝：

- 插入点之前未定义的变量引用；
- 与源测试已有局部变量重名的 `assign_to`；
- 重复创建同一个新变量；
- 未知函数、非法方法名和 dunder 方法；
- 非法位置锚点、重复锚点和修改断言的克隆；
- version 2 中混入旧版 `insert_at_start`、字符串 `insert_before` 或 Python 表达式字段。

变换器只根据类型化节点构造 AST，不解析模型提供的 Python 语句。既有测试函数、断言和 import 继续遵守结构保持审计要求。

## 实现验证

已通过 Python 语法编译和 `git diff --check`。DSL、诊断、AST 变换、评估收集和既有受限修复审计的回归测试共 `67` 项，全部通过，其中包含旧版 DSL 兼容性、类型化节点 schema、AST 生成、未定义引用和重复源变量拒绝测试。

## 模型生成与静态审计

固定模型为 `openai/qwen3-coder-plus`，温度为 `0.0`，每个阶段最多尝试两次。模型仅接收脱敏 baseline、公开测试和公开文档；上下文不包含 ground truth、补丁或评估器输出。三个任务的诊断均在第一次尝试通过。BIDC 和 CTRS 的类型化 DSL 在第一次尝试通过；CACH 第一次响应缺少 `replace_assignments` 所需字段，第二次响应通过。最终诊断接受率和类型化工件接受率均为 `3/3`。

三个接受工件均新增一个克隆测试，且未修改既有测试体、断言或 import：

| 任务 | 新增测试 | 类型化编辑 | 静态结果 |
| --- | --- | --- | --- |
| `BIDC-003` | `test_orderedbidict_consecutive_moves_to_beginning` | 在原操作前再次对同一 key 执行 `move_to_end(last=False)`，并保存一次未被后续断言使用的观察值 | accepted |
| `CACH-005` | `test_tlrucache_multiple_key_updates_leave_no_unmarked_removed_entries` | 在原更新前执行一次 `set_item` | accepted（第 2 次 DSL 尝试） |
| `CTRS-001` | `test_converter_copy_preserves_function_hooks` | 构造普通 structure/unstructure hook，注册后执行源测试中的 copy 与 roundtrip | accepted |

静态接受只证明工件满足语法和结构约束，不证明测试语义有效。CACH 工件的 key 是字面量字符串 `"key"`，不是 Hypothesis 变量 `key` 的引用；CTRS 工件使用普通类型 hook，而目标诊断陈述要求覆盖 function/predicate hook 的复制；BIDC 工件重复移动同一 key，新增观察值也未进入断言。这些事实由保存的 repair specification 和生成测试直接确认。

## 固定条件配对评估

所有动态评估使用 Hypothesis seed `20260815`。同一任务的 baseline 与方法组使用相同的显式容器镜像。函数收集均成功，评估超时均为 `0`，`eval_phase_aborted=false`。

| 任务 | baseline 严格 F-to-P | v4 方法严格 F-to-P | 新增测试直接严格命中 | 新增测试行为 |
| --- | ---: | ---: | ---: | --- |
| `BIDC-003` | `0/4` | `0/4` | `0` | 四个 buggy/fixed 对照中均通过；原有 `bug_2, bug_3` 仅为宽松发现 |
| `CACH-005` | `2/4`：`bug_2, bug_3` | `2/4`：`bug_2, bug_3` | `0` | 四个 buggy/fixed 对照中均失败，不能构成严格 F-to-P |
| `CTRS-001` | `1/4`：`bug_2` | `1/4`：`bug_2` | `0` | 四个 buggy/fixed 对照中均通过 |
| **合计** | **`3/12`** | **`3/12`** | **`0`** |  |

BIDC 的 baseline 复用了同一 seed、同一镜像下已完成的配对结果；CACH 和 CTRS 重新运行了 baseline 与方法组。原始输出位于 `experiments/eval_outputs/pbt/model-typed-operation-dsl-development-v4/`。

## 结论与证据边界

类型化操作 DSL 解决了上一版任意 Python 字符串导致的静态合规问题：模型工件接受率由 v3 的 `1/3` 提高到 v4 的 `3/3`。但是，接受工件没有对任一目标缺陷产生可直接归因的严格 F-to-P；总体方法分数与 baseline 相同，CACH 新增测试在 fixed 版本上也失败。

本阶段结果表明，结构化语法约束能够提高工件的可审计性，但还不足以保证模型生成的操作与诊断出的状态转换或操作序列在语义上对应。该结论属于三个开发任务上的方法验证，不延伸为未见任务性能结论。
