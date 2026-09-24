---
name: cst-review
description: Owner 已对某候选指令「送审」，或本轮判词为 FAIL 而续投授权仍在时使用：把受治理评审按评审通道的调用形态机械发起 - 起草评审请求件、preflight 直到通过、review 取判词、核对四件证据落位，FAIL 后以 `respond` 写决定文件再投下一轮，含变更集投递、封顶两轮的到额三出口与常见失败码的处置。不用于起草候选、判 done、re-Freeze、push 或建 MR。
---

# 受治理评审送审（cst-review）

> 中立能力正本条目（布局权威 §5.2 落点，`cst-*` 命名的第二个实例）。能力语义、判定与状态推进恒在 `mechanisms/review-channel/review_channel.py` 与评审通道设计；本条目只承载调用形态、次序、判据出处与本仓已实撞的失败模式，不转录设计规则、不自写状态。
> 本条目是雏形（Owner 2026-09-05 裁定，台账 `repo:OD-09` 同日 addendum）。正式形态 = Owner 2026-09-03 裁定 D2 的命令族 `cst-review`（承载任务 gov-t27），本条目是其实测输入、不预支其设计。评审通道设计的四条 amendment 输入（叙述格式的补发覆盖面、指令注入骨架、多候选评审、投递束瘦身）已登记为 `repo-od-09:KB-02`，本条目同样不预支。

## 何时调用

Owner 已就某候选给出「送审」指令；或上一轮判词为 FAIL 且本次续投仍在 Owner 已给的授权面内（一修一投）。前置条件：候选字节已是本轮要投的终态并留在工作区；本轮 Changelog 的沿革叙述已与候选当前字节一致；台账内有可引的授权行（写进 `invocation_authorization`）；本轴的轮次额度（缺省 2，评审通道设计 §6.10）未到额，或已有 Owner 的加轮授权；r2+ 时本轴上一轮的决定文件已由 `respond` 写入（§6.12）。

不调用：Owner 未给送审指令；候选还在改；额度已到而无加轮授权（先呈报，见步骤 3）。

## 调用形态与开关

三个子命令共用一条形态（评审通道设计 §5.3 的净化启动形态，解释器取 `gates.interpreter`、不经 `PATH` 查找）：

```text
"$(git config --get gates.interpreter)" -B -E -s -S -X pycache_prefix="$(mktemp -d)" \
  mechanisms/review-channel/review_channel.py <preflight | review> --request <请求件绝对路径>
"$(git config --get gates.interpreter)" -B -E -s -S -X pycache_prefix="$(mktemp -d)" \
  mechanisms/review-channel/review_channel.py respond --decisions <决定输入绝对路径>
```

`--request` 与 `--decisions` 只收绝对路径，相对路径是受控 usage 失败。子命令闭集另含 `probe` 与 `selftest`，本条目不覆盖；`register` 自评审通道 Amendment 4（FROZEN r5）起退役。

| 开关 | 适用子命令 | 缺省 | 作用 |
|---|---|---|---|
| `--no-reemit` | `review` | 信封类（三级不可解析）与校验类（机读块完好、失败只落在通道已知值字段或叙述排版）各自动补发至多一次 | 关闭两类补发并记入 Receipt（失败码 `reemit-disabled`）。按缺省即可，不为省时关闭 |
| `--repo-root <路径>` | `preflight` / `review` / `respond` | 从可执行物位置向上解析 | 指定仓根。在任务 worktree 内按缺省即可 |

退出码与 attempt 分类、失败码的唯一映射恒以评审通道设计 §5.3 的映射表与 §6.7 分类闭集为准，本条目不转录之。三条读法：`0` = 本次 attempt 走完且判词校验 VALID（判词 PASS 与 FAIL 同为 `0`，业务结论在判词与 Receipt、不在退出码）；`1` = preflight 拒绝，或 attempt 以失败分类结束（失败码在 stderr 报告与 Receipt）；`2` = 内部异常或 Receipt 写入自身失败（运行时身份资格检查自 Amendment 4 起退役），处置 = 停下呈报（`AGENTS.md` §6 遇非预期仓库状态同款纪律）。

## CL-55 归档模式操作

本 clone 的 CL-55 切换决定为 records/governance/review-channel/HarnessPlane_Review_Channel_Owner_Decisions_D11.md，本任务分支已启用通道 §8.6。先核实际 checkout 含正式切换决定与实现，再核本机配置；已声明启用而缺配置时拒绝，不能落回 legacy-git 或创建空归档。缺少本批决定或实现的旧 checkout 不得发起新评审。下方 v4 示例与四件入 Git 步骤仅适用于旧模式和历史解释，archive-v1 使用本节。

归档模式的送审入口不变，新请求用 review-channel-request/v5；上一轮引用、response 选择与字段闭集从 §8.6.1 取，不改造下方 v4 示例冒用。request 可在 ignored 层编辑，由通道保存 exact copy。preflight 在调用前验证主根、备份及轮次连续性；失败按通道原因处置，不能建空目录重起 r1。

review 返回的成功须含通过双副本验证的 ArchiveRef，调用者核实际 Receipt、判词、清单与快照；不再轮询 working tree 下 receipt 是否出现，不对四件执行 git add。FAIL 后仍按 Owner 原话 respond，保留 response 引用并按新请求形态续投；不改原 round。中断只用显式 recover 核保存状态，不通过重新调用 provider 猜测恢复。需要只读原件校验时用通道 archive verify，命令及错误以 §8.6 为准。

Git 只保存正式决定所需最终结果、候选身份和引用；Changelog/进度不逐轮抄 finding 与完整处置。具体需 Owner 决定的事项仍在既有台账记录并指原件。subject 花名册由正式治理目录及历史声明派生，不因新仓外 round 出生生成虚构目录。Freeze/判 done/push/MR 仍不属于本 skill 的授权面。

## 步骤（legacy-git，切换前）

1. **取授权与轴**。Owner 送审指令原话记进本任务进度文件（一行 `note`）；`invocation_authorization` 写承载该次评审的台账行 id（先例：`repo:OD-09`）。轴的划分 = 一份请求一份判词，候选可多件（评审通道设计 Amendment 2 多候选，§6.2 第 1 条）：单件修正 `subject` = 机制单元名（任务定义评审 = `<task-record-id>-task`）、`inputs.candidates` 恰一件；变更集（交付方法 §7.6）`subject` 取变更集轴 `changelog-<change-identity 小写>`（先例轴 `changelog-cl-31`）、`inputs.candidates` 列出全部字节发生变化的受锁成员（首项即首候选）、Changelog 与其余成员作参考件同束，一轮一次投递、一份判词；各成员的冻结记录行 `check` 以同一轮终轮判词为锚（freeze-record 设计 §5.1）。同一轮次序列内候选集合不变（增减成员即另起轴）。变更集与单件的请求件文法相同（`review-channel-request/v4`），额度按轴计、缺省 2。证据落点与 attempt 落点由通道按 §8.1 派生，本条目不拼路径。

2. **起草请求件**（`review-channel-request/v4`），落 `review-attempts/<subject>/r<N>/request.json`，本地暂存层、永不入 Git。字段骨架：

   ```json
   {
     "request_schema": "review-channel-request/v4",
     "subject": "<机制单元名，文法 ^[a-z0-9][a-z0-9-]*$>",
     "stage": "impl",
     "round": "r<N>",
     "previous_round": {
       "verdict_path": "reviews/<subject>/r<N-1>/HarnessPlane_<Subject>_Review_R<N-1>.md",
       "receipt_path": "reviews/<subject>/r<N-1>/receipt-r<N-1>.json"
     },
     "round_extensions": [
       {"authorized_by": "<台账行与 Owner 指令原话>", "at": "<ISO 8601 时刻>", "added_rounds": 1, "note": "<已投递轮数、允许轮数与本次加轮理由>"}
     ],
     "caller": "claude-code",
     "artifact_author": {
       "human_only": false,
       "authors": [{"tool": "claude-code", "model": "<精确模型标识>", "vendor": "Anthropic"}]
     },
     "profile": {"provider": "codex-builtin", "model": "gpt-5.6-sol"},
     "effort": "high",
     "inputs": {
       "candidates": ["<候选的仓内相对路径；变更集时逐件列出，首项为首候选>"],
       "references": ["<候选正文与本轮 Changelog 引用的每个仓内文件>"]
     },
     "review_brief": {
       "background": "<触发事实、变更集成员划分、本轮在整条评审线上的位置>",
       "check_surfaces": "<在评审面的节号与逐 hunk 对照来源；并列写明不在评审面的项>",
       "evidence_limits": [{"reference": "<仓外对象或规定落点>", "reason": "<理由>", "kind": "exemption | planned-location"}],
       "accepted_residuals": [{"id": "RES-1", "text": "<r2+ 先逐字粘入 respond 输出的派生残留项，再续编本轮显式残留>"}],
       "remediation_statement": "<r2+ 必填：以 respond 输出的处置段逐字起始，其后追加整改叙述>"
     },
     "invocation_authorization": "<台账行 id>"
   }
   ```

   - **r1 与 r2+ 的差别恰三处**：`previous_round`（r1 禁填、r2+ 必填，两个路径都取上一轮的 tracked 落点）、`review_brief.remediation_statement`（r1 禁填、r2+ 必填非空且以处置段起始）、上一轮决定文件（r1 无、r2+ 须在场，缺即 `decisions-missing`）。填反即 preflight 拒绝。
   - 问题集不由 caller 起草：required question 集合是通道常量五题闭集，按 `stage` 取套（§6.4）；请求件带 `review_brief.questions` 或 `standard_questions` 即请求文法失败。caller 的关注点只写 `check_surfaces`。
   - `round_extensions` 只在额度到额并已获加轮授权时写（§6.10），无加轮时整个字段省略。
   - `profile` 显式写（`{provider, model}` 成对，取值须在 §6.11 Registry 闭集内），不依赖缺省选择与继承，使每轮的评审方在请求件内自证。
   - `artifact_author.authors[].vendor` 须逐字命中 §6.11 `vendors` 闭集；跨模型提供方资格按 §6.8 判定，通道不推断、只记录。
   - `inputs.references` 须覆盖候选正文与本轮 Changelog 引用的**每个**仓内文件；仓外对象（如出生档案库正本）写进 `evidence_limits[]` 并给 `reason`（`kind` 缺省即豁免）；引用了尚未建立的顶层落点时该项 `kind` 写 `planned-location`。域一引用（规划基线件裸名带修订号）无论修订号是否在案均放行，任务书并列标注引用修订号与现行修订号，不需要也不接受 `archive-revision` 声明；受锁且未变化的上游件也须投原文（锁值引用件形态已退役），只审改动区由任务书第 3 节差异块承载。
   - 字段语义、取值域与全部校验规则的正本 = 评审通道设计 §6.1，引用解析与投递名派生 = §6.2，本条目不转录之。**请求件写对了没有，以 `preflight` 的输出为准**，不必先逐条比对规则。

3. **preflight 直到退出码 0**。该子命令零提供方接触、不消耗轮次额度、不产 tracked 证据，故可反复跑。本仓已实撞的失败码与修法（判据出处 = §6.2、§6.10 与 §6.12）：
   - `reference-missing`：候选或 Changelog 引用了某个仓内文件而它不在 `inputs.references`。修法 = 补进 references。起草时先把候选与 Changelog 内的仓内路径引用整理成一份清单再填，可省下多轮 preflight。
   - `reference-unresolved` / `reference-ambiguous`：裸文件名在其解析域内零命中或多于一个命中。修法 = 改用能由候选自身文本唯一解析的形态（带域前缀的仓内相对路径，或带修订号后缀的规划基线件裸名）；通道明确不以 `references` 消解歧义。
   - `bundle-name-collision`：整束投递名派生取尽路径段后仍冲突。实撞：`HANDOFF/README.md` 与仓根 `README.md` 同束即撞，因为投递名按来源路径字典序逐段扩展派生，而仓根单段路径无更多段可扩展。**处置须可执行**：两件都是仓内现存件，豁免对仓内现存正本不成立（见禁则），故不得以「另一件写进 `evidence_limits` 不投」了事。可执行形态恰两条，取其一：其一，候选正文改以文字指称该件（不以反引号写其路径），该串因此不进入引用提取集，本轮只投另一件；其二，本轮只投其中一件，并确认候选正文没有以反引号路径引用未投的那件。该缺口（仓根单段路径在投递名派生下无扩展余地）登记于台账 `gov-t11:KB-02`，其通道侧处置属后续设计输入，本条目不预支。
   - `round_budget_exhausted`：已投递轮数达到允许轮数（缺省 2）。先停下呈报；Owner 恒在三条出口内择一（评审通道设计 §6.10）：「加 N 轮」（按步骤 6 写 `round_extensions`）、「带保留 re-Freeze」/「带保留判 done」（不经通道）、「退回」（不经通道）。
   - `decisions-missing` / `decisions-invalid` / `decisions-mismatch`：r2+ 请求件所依的上一轮决定文件缺席；或该文件不可解析、不合 §6.12 五条规则；或请求件的处置段 / 派生残留项 / 上一轮判词身份与决定文件不一致。修法 = 先跑 `respond`（步骤 6），再把其 stdout 报告对象 `next_round` 成员内的处置段与派生残留项逐字粘入请求件；不手写处置结论。每次 `respond` 都改变决定文件字节（`decided_at`），故请求件恒以最近一次 `respond` 的输出起草。
   - `baseline-unrecoverable`：候选在现行冻结事实内有身份（`sha256`），而 attempts 封存副本与 Git 历史均无该字节。**这是仓库状态问题、不是请求件问题**：停下呈报，不改请求件绕过。
   - `round-in-progress`：同一轮已有通道进程持锁。等它结束，不并发同一轮。

4. **review**。单轴：直接跑 `review`；发布后通道不执行任何 Git 动作（`register` 与自动 `git add` 自 Amendment 4 起退役）。变更集自 Amendment 2 起以多候选单轴投递（步骤 1）；仍需多轴（不组变更集的多 subject）时各轴 `review` 同时起，评审阶段各写各的 attempt 目录互不干扰。
   补发（§6.5.4）由通道自动执行、至多一次：信封类（机读块不可解析）与校验类（机读块完好、失败只落在通道已知值字段或叙述排版）；Receipt `extraction.reemit_trigger` 记触发类、`reemit_reasons` 记原始失败项。判断字段的缺陷不补发，仍按 `verdict-invalid` 归因后重投；含题号集合不等于该 stage 五题、以及规则 14（`origin = unchanged_region` 而 `severity = blocking`，`verdict_problems[]` 记以 `rule-14:<id>` 起始的一项）两类，均不占额度、原样重投同一轮。
   一轮耗时长（codex-builtin / gpt-5.6-sol / high 实测约 10 至 20 分钟）：后台发起，轮询 `reviews/<subject>/r<N>/receipt-r<N>.json`，该文件出现即本轮已发布。

5. **落位核对**（只核对、不重做）。
   - 四件最低成员齐备：任务书、判词、`bundle_manifest.json`、`receipt-r<N>.json`（§8.1）。tracked 轮目录在任何时刻只有两态：不存在，或四件齐备。
   - 暂存：caller 按精确路径 `git add` 四件最低成员（通道不再自动暂存；证据登记本为只读历史档案、不再登记）；提交仍属 caller（D-08 工作分支常设授权）。
   - 该 subject 首次出现 `reviews/<subject>/` 时，仓根 `README.md` 的 subject 花名册会多一列取值：跑 `manifest --write` 重新生成 `README.md` 与 `mechanisms/MECHANISMS.md`（两份是整文件生成物、不手改，调用形态见仓根 `README.md` 的「门与命令」节）。
   - Receipt 的 `verdict_problems[]`、`extraction`、`round_budget`、`decisions_sha256`（r2+）是本轮事实的机读面。判词叙述与它不一致时，原样呈报两者，不自行调和（`AGENTS.md` §2 权威冲突即停）。

6. **判词处置**。
   - **`verdict-invalid`**（判词校验 INVALID、分类 `reviewer_output_invalid`：机读块完好而人读叙述排版不合契约、题号集合不等或规则 14 违规）：该轮不发布、**不消耗轮次额度**。先读 Receipt 的 `verdict_problems[]` 归因（§6.7 规则 7：无效后重试前先归因），确属评审方输出漂移、题号集合不等或规则 14 违规的即**原样重投同一轮**、请求件一字不改；同一轮原样重投至多两次，第三次仍无效即停下呈报。无效 attempt 的内容不算证据（§6.7 规则 8）。
   - **FAIL 且额度未到**：先呈报 finding 清单，Owner 逐条给「处置 <finding-id>: approve / fix / skip」与原话；执行者把三值与原话（`owner_verbatim` 逐字、不改写）写成决定输入（`{subject, stage, round[, task_record], decisions: [{finding_id, action, instructions, work_item, owner_verbatim}]}`，不填 `verdict_sha256` 与 `decided_at`），跑 `respond`（`fix` 须附 `instructions`；blocking 的 `skip` 须附台账项 id 作 `work_item`）；决定文件落 `review-attempts/<subject>/r<N>/decisions.json`（任务轴落 `tasks/<task-record-id>/attempts/<stage>-r<N>/`），永不入 Git。随后按决定整改（核的对象是代码与候选字节本身，不按判词叙述直接改；整改落候选、实现与 Changelog），做一次投递前自审校（caller 侧、只读、不消耗额度），再回步骤 2 起草下一轮请求件：`remediation_statement` 以 `respond` 输出 `next_round.remediation_statement` 逐字起始、其后追加整改叙述（每一句先对照代码核实，两段之间不得互相拉扯，实撞：CL-39 变更集 `freeze-record` 轴第七轮）；`accepted_residuals` 先逐字粘入 `next_round.accepted_residuals`。每轮结果在本任务进度文件写一行 `note`，并在 Changelog 第 7 项逐轮登记（轮次目录、判词结论、finding 与处置）；Changelog 引轮次目录、不转录其中的取值。
   - **额度到额（缺省 2）**：停下呈报，Owner 三出口择一（步骤 3 `round_budget_exhausted` 条）；取「加 N 轮」时请求件写 `round_extensions`，`authorized_by` 引台账行与 Owner 指令原话，`note` 写清已投递轮数、允许轮数与本次加轮数（§6.10）；取带保留出口时本条目到此为止，落地形态见 `HANDOFF/README.md` 指令表。
   - **PASS**：`git add` 四件后作检查点提交，然后按 `HANDOFF/README.md` 指令与阶段对照表呈报下一条 Owner 指令。终态是 re-Freeze，其落地形态见 `HANDOFF/README.md` 冻结件指令表，本条目到此为止。

## 禁则

- 不手改通道产物：判词、任务书、清单与 Receipt 是不可变证据，只由通道发布；`review` 未发布的轮不得手工补齐四件，也不得从 attempt 目录搬运。
- 不改请求件以求通过 preflight：被拒的是事实（引用缺件、投递名冲突、额度到额、决定文件缺席），修事实、不修判据；`evidence_limits` 的豁免只对仓外对象或上游明确允许不入的对象成立，仓内现存正本不得豁免。
- 不在候选与 Changelog 不一致时投递：Changelog 的沿革叙述须与候选当前字节相符。「本轮未触碰候选字节」一类陈述在候选已变时是假陈述，评审方会据此判 blocking。
- 不手写 `remediation_statement` 的处置段与派生残留项：两者只由 `respond` 输出、逐字粘入，改写即 `decisions-mismatch`；Owner 原话只进 `owner_verbatim`、任何环节不改写。
- 不并发跑同一轮，也不在两个 worktree 对同一轮各起 attempt：通道的文件锁与目标核对只在本工作树内有效（§8.2 威胁模型其二），跨 worktree 的同轮冲突要到 MR 合并时才暴露。
- 不因判词 FAIL 而扩大改动面：整改只覆盖 finding 指名的项与其同根项，与本次触发事实无关的改动另立（交付方法 §7.6 边界条）。差异块之外的发现只以 `unchanged_region` 报出、不阻断（规则 14），验证开始后的新需求经 `skip` 转后续工作项。
- 退出码 `2`、非预期的仓库状态、判词与 Receipt 互相矛盾：停下呈报，不绕过、不换个方法重试。
- 触及设计哲学分叉或须 Human 裁决的 finding：停下呈报问题描述与建议理由，不在续投授权内自行裁定。
