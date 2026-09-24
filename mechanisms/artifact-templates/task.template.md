# <task-record-id> · <短标题>

> Depends on:
> `milestones.md@rN <task-id>`
>
> 权威状态: subject `<task-record-id>-task`（治理记录目录按所在仓的布局规则解析；唯一状态正本）

<!-- template:guide
落点：`tasks/<task-record-id>/<task-record-id>.md`；目录成员闭集 = 本件、按条件建立的 `design.md`、`rulings/RU-<NN>-<slug>.md`、`reviews/<stage>-r<N>/`（stage ∈ task / impl；仅 legacy-git 原件，CL-55 候选切换后按布局 §7.5 退役，不再为新轮建立）、`attempts/`（永不入 Git）。
变更 design 的骨架 = `design.template.md`，适用条件与人工核差 = 交付方法 §6.2；它不冻结、不改变 Definition、不产生新授权。台账任务不用本目录，以合法诊断规划承载设计。
task-record-id 文法：`feature-t<N>` / `design-t<N>` / `gov-t<N>`（N 自 0 起不补零）或 `hotfix-h<64 位小写十六进制>`；H1、目录名、文件名 stem、Identity 字段与 subject 前缀同值。
里程碑任务：`Depends on` 写 `milestones.md@rN <task-id>`，rN = 据以定义本任务的 milestones 冻结修订号。hotfix：写 `无`，来源落 Source References。
页首不得出现状态、修订号、轮次、分支、审批或日期字段；`权威状态` 后恰一空行直接进入 `## Identity`。
九个 H2 顺序固定、不编号；其后可加 `## Extension: <名称>`，首行写 `Maps to: <核心 H2 名>`，只细化一个核心面。
生命周期：定稿 = `finalization` 裁定件且本件零字节变化；改动 Goal / Scope / Constraints / Acceptance Criteria 的语义 = 关闭本任务、开新任务，不就地改写；终态 = `done` 或 `close` 裁定件。
实例化时删除全部引导块；核对命令 = `python3 mechanisms/artifact-templates/artifact_lint.py task <路径>`。
-->

## Identity

- Task record ID: `<task-record-id>`
- Task kind: `feature | hotfix | design | governance`
- Definition subject: `<task-record-id>-task`
- Product line: `<stable-product-line-id>`

<!-- template:guide
恰四行、顺序固定。Task kind 取一个实际值并与 ID 前缀一致（feature-t → feature，design-t → design，gov-t → governance，hotfix-h → hotfix）。Product line 是跨仓稳定的产品线标识，不是仓路径。
-->

## 通俗说明（给人读）

本节只帮助人建立心智模型，不是任务契约的权威取值；冲突时以其余正式章节为准。

<!-- template:guide
首段逐字保留。其后用自然语言说「为什么做、做成后有什么不同」；不写路径、字段名、条款号、命令、退出码、hash、修订号或逐项验收值。
-->

## Goal

<!-- template:guide
恰一个任务目标；不复述上游全文，不含实现方案。
-->

## Goal Conditions

<!-- template:guide
无序列表 ≥ 1 条：目标成立所需的可判断条件（描述目标世界，不是验收步骤）。
-->

## Scope In

<!-- template:guide
无序列表 ≥ 1 条。
-->

## Scope Out

<!-- template:guide
无序列表 ≥ 1 条；点名最容易误吞的相邻面与其权威承载，不写「其他都不做」。
-->

## Constraints

<!-- template:guide
覆盖不可改变的上游决定、授权 / 安全边界与适用验证；不写动态工作区状态。
-->

## Acceptance Criteria

<!-- template:guide
无序列表 ≥ 1 条；每条 Human 在交付时可判，写结果与证据；机械门判绿不替代语义验收。
禁入：当前轮次、finding、分支、commit、MR、施工步骤、治理事件事实。
-->

## Source References

- Primary source: `milestones.md@rN <task-id>`

<!-- template:guide
里程碑任务：Primary source 逐字等于页首依赖；可续 Spec / Decision / Other 行（值用反引号包裹）：`- Spec: spec.md@rN <ID 列表>`、`- Decision: <decision-id>@rN`、`- Other: <裸名>@rN <ID 列表>`，每件一行。
hotfix：恰三行（值用反引号包裹）`- Primary source: hotfix-registration <base64url 无 padding>`、`- Registration byte length: <十进制>`、`- Registration SHA-256: <64 位小写十六进制>`，三者与 ID 内 digest 同值。
-->
