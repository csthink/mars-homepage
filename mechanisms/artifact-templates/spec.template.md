# <产品名> MVP Spec

> Depends on:
> `proposal.md@rN`
>
> 权威状态: subject `spec`（治理记录目录按所在仓的布局规则解析；唯一状态正本）

<!-- template:guide
实例化：`<产品名>` 换成非空产品名（不含换行、`<`、`>`）；H1 可在 `Spec` 后带一个全角括注作产品线限定。
`rN` 换成本 spec 据以构造的 proposal 冻结修订号；`Depends on` 恰一行、不写路径、不写 latest。
页首之后恰一空行直接进入首个 H2；不得插入状态、修订号、日期、Sources 或其他 metadata 行。
H2 编号要么全有（自 1 起逐节加一）要么全无；扩展 H2 可插在核心 H2 之间，去编号后不得与核心标题同名。
条目 ID 文法：`- **<族>-<正整数>** <正文>`，族 ∈ S / O / FR / NFR / C / SC，各族只在其定义节定义；同族数值唯一；冻结后 ID 不改写、退役留 tombstone。下游引用形态：`spec.md@rN <ID>`，多项以 `、` 分隔。
实例化时删除全部引导块；核对命令 = `python3 mechanisms/artifact-templates/artifact_lint.py spec <路径>`。
-->

## 产品目标与用户问题

<!-- template:guide
最低承载：产品目标与要解决的用户问题，与 proposal 一致且可追溯；不列功能清单。禁入：本件的治理状态与治理事件；会在字节不变时过时的当前态描述。
-->

## In Scope

<!-- template:guide
最低承载：每条 `- **S-<n>** …`，写 MVP 内的能力或结果边界；S 族只在本节定义。
-->

## Out of Scope

<!-- template:guide
最低承载：每条 `- **O-<n>** …`，点名明确不做的事与不管的情境；O 族只在本节定义。
-->

## Functional Requirements

<!-- template:guide
最低承载：每条 `- **FR-<n>** …`，给可观察结果或可判条件；可按 H3 分组，FR 族只在本节（含其 H3）定义。禁入：实现设计、任务编排、milestones 内容。
-->

## Non-functional Requirements

<!-- template:guide
最低承载：每条 `- **NFR-<n>** …`；NFR 族只在本节（含其 H3）定义。
-->

## 核心旅程与产品约束

<!-- template:guide
最低承载：核心旅程叙述可无 ID；每项跨旅程的产品约束写 `- **C-<n>** …`，C 族只在本节（含其 H3）定义。
-->

## MVP 整体成功条件

<!-- template:guide
最低承载：每条 `- **SC-<n>** …`，第三方可观察判断；SC 族只在本节（含其 H3）定义。
-->

## 本 spec 不决定的事

<!-- template:guide
最低承载：每条写事项 + 承载方（下游设计、配置或后续任务）；无留白时写 `无`。
-->

## 反思

### 必须现在定

<!-- template:guide
最低承载：不现在决定就无法安全向下推进的内容，每条写「决定了什么 + 不定会怎样」。
-->

### 同类潜在 bug 一并防止

<!-- template:guide
最低承载：每条写「失效类 + 防住它的规则或边界」，规则来自本 spec 已定内容。
-->

### 可接受残留（分级）

<!-- template:guide
最低承载：每条写「等级（高 / 中 / 低）+ 残留内容 + 接受理由」；无残留时写 `无`。
禁入：把尚未做的工作写成残留；本件的评审沿革或 amendment 说明。
-->
