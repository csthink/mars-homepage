# <产品名> MVP Milestones

> Depends on:
> `spec.md@rN`
>
> 权威状态: subject `<milestones-subject>`（治理记录目录按所在仓的布局规则解析；唯一状态正本）

<!-- template:guide
实例化：`<产品名>` 换成非空产品名；H1 可在 `Milestones` 后带一个全角括注作产品线限定。
`rN` 换成本计划据以构造或最近显式对齐的 spec 冻结修订号；页首不列 spec 条目 ID。
`<milestones-subject>` 换成该实例治理记录登记的 subject（文法 `[a-z0-9]+(?:-[a-z0-9]+)*`），首次冻结后不变。
正文恰四个 H2、顺序固定、不开放扩展 H2；编号要么全有（1 至 4）要么全无；标题不带括注。
本件是里程碑任务的唯一名单：任务行不带状态字段（进行中 / 已完成 / blocked 一律禁入），进度住进度文件。
实例化时删除全部引导块；核对命令 = `python3 mechanisms/artifact-templates/artifact_lint.py milestones <路径>`。
-->

## 规划基线

<!-- template:guide
最低承载：本计划据以构造的 spec 修订与稳定的计划规则（阶段划分依据、调度原则）；本节不开 H3。
禁入：Milestone 总览表、任务汇总或依赖汇总（它们只住 Milestone 条目的任务行）。
-->

## Milestone 条目

### M-NN <Milestone 名称>

- 阶段目标：<非空规划目标>
- UI 变更：<有 | 无>
- UI lineage：<不适用 | 非空稳定 lineage 引用>
- 任务清单：
  - <task-id> <非空一句话交付边界> · 类型：<低保真 | 高保真 | 前端 | 后端 | 治理机制> · Spec 引用：spec.md@rN <ID>、<ID> · 依赖：<无 | task-id 列表>
- 验收条件：
  - <非空且可判断的 Milestone completion 条件>

<!-- template:guide
每个 Milestone = 一个 H3 + 五个固定字段（顺序、名称、列位置固定，不增字段）。
M-ID：`M-<数字>`，1 至 99 写两位（`M-01`），数值全文唯一，退役 ID 不复用；编号只表达身份，不表达顺序。
task-id：`feature-t<N>` / `design-t<N>` / `gov-t<N>`，N 自 0 起不补零，每族连续无空洞；全文唯一，移动 Milestone 不改 ID。
类型与前缀映射：低保真 / 高保真 → design-；前端 / 后端 → feature-；治理机制 → gov-。
Spec 引用：一个或多个 `spec.md@rN <ID>、<ID>` 组，多组以 `；` 分隔；不写裸 ID、章节号、latest。
依赖：`无` 或以 `、` 分隔的 task-id 列表，不含自身、不重复，目标须在本件有定义。
后继与退役尾段（可选，顺序固定）：` · Supersedes：<ID 列表>`，再 ` · WITHDRAWN in milestones.md@rN`；H3 与任务行同形。
-->

## 跨 Milestone 依赖与调度

<!-- template:guide
最低承载：跨 Milestone 的依赖解释与 Human 调度原则；本节不开 H3、不复制任务行。
-->

## 反思

### 必须现在定

<!-- template:guide
最低承载：不现在决定就无法安全推进的计划内容。
-->

### 同类潜在 bug 一并防止

<!-- template:guide
最低承载：每条写「失效类 + 防住它的规则」。
-->

### 可接受残留（分级）

<!-- template:guide
最低承载：每条写「等级 + 残留内容 + 接受理由」；无残留时写 `无`。本节只允许这三个 H3。
-->
