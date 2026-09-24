"""review_channel_taskbook — 任务书渲染（设计 §6.3；确定性文档，事实段由通道生成）。

渲染输入 = 评审请求、预清单（封存副本）、上一轮机读块、Profile 选择结果、轮次额度、引用解析结果。
同一输入两次渲染逐字节相同。判词格式契约不出现在任务书（只由指令注入承载）。
本模块不导入其他契约模块。
"""
import difflib

import review_channel_contract as C


def _h(level, text):
    return "#" * level + " " + text


def _line(label, value):
    return "- %s: %s" % (label, value)


def diff_summary(baseline_text, current_text):
    """统一差异统计（增删行数）与逐块摘要（通道算）。"""
    a = baseline_text.split("\n")
    b = current_text.split("\n")
    added = removed = 0
    hunks = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
        hunks.append("%s: baseline lines %d-%d -> candidate lines %d-%d" % (tag, i1 + 1, i2, j1 + 1, j2))
    return {"added": added, "removed": removed, "hunks": hunks}


def render(ctx):
    """ctx 字段：
      request        规范化评审请求（request 模块输出）
      routing        {axis, tracked_round_dir, attempt_round_dir}
      pre_manifest   预清单（不含任务书）
      candidate      预清单中首候选项（Amendment 2：candidates 缺席时的兼容入口）
      candidates     预清单中全部 candidate 项，按清单候选次序（Amendment 2 多候选；缺席时取 [candidate]）
      eligibility    {author_vendors: [...], reviewer_vendor: str}
      profile        {provider, model, transport, runtime, effort, effort_source, selection_source,
                      changed_from_previous: bool|None, previous: {provider, model}|None}
      verdict_path   判词发布路径（仓内相对）
      channel        {id, design_sha256}
      previous       None | {findings: [...], verdict: str, diff: {added, removed, hunks} | diffs: [(bundle_name, diff)], profile_same: bool,
                      decisions: [(finding_id, severity, title, action, instructions, work_item)]}
      changed_region r1 改动区（Amendment 4，§6.2 第 6 条）：[{bundle_name, baseline: None | {revision, sha256}, diff: None | {added, removed, hunks}, note}]；
                      baseline 为 None = 首冻（note 为 None）或记录不含机械正本（note 说明），整件为改动区；r2+ 缺席（改动区取 previous.diffs）
      references     引用解析结果列表
      budget         {round_index, allowed_rounds, max_rounds, source, extensions_added, submitted_rounds}
      authorization  invocation_authorization 原文
      task_file      任务书投递名
    """
    r = ctx["request"]
    out = []
    subject = r["subject"]
    out.append("# %s · %s · %s · 评审任务书" % (subject, r["stage"], r["round"]))
    out.append("")
    out.append(_h(3, "历史来源"))
    sources = {(i['path'],i['source_commit'],i['blob_oid'],i['sha256']) for i in ctx.get('history_sources', []) if 'evidence' not in i}
    if sources:
        out.extend(['', '| 原路径 | source_commit | blob_oid | sha256 |', '|---|---|---|---|'])
        for row in sorted(sources):
            out.append('| '+' | '.join('`'+v.replace('|','&#124;')+'`' for v in row)+' |')
    else:
        out.append('无。')
    out.append('')
    archived=[i for i in ctx.get('history_sources',[]) if 'evidence' in i]
    if archived:
        import json
        out.extend(['| 原路径 | EvidenceRef | sha256 |','|---|---|---|'])
        for i in archived:
            out.append('| `'+i['path'].replace('|','&#124;')+'` | `'+json.dumps(i['evidence'],ensure_ascii=False,sort_keys=True).replace('|','&#124;')+'` | `'+i['sha256']+'` |')
        out.append('')
    out.append(_h(2, "1. 状态头"))
    out.append(_line("subject", "`%s`" % subject))
    out.append(_line("stage", "`%s`" % r["stage"]))
    out.append(_line("round", "`%s`" % r["round"]))
    out.append(_line("task_record", "`%s`" % r["task_record"] if r["task_record"] else "（缺席：subject 轴）"))
    out.append(_line("授权引用", "`%s`" % ctx["authorization"]))
    el = ctx["eligibility"]
    out.append(_line("跨模型提供方判定", "作者提供方集合 = %s；评审方提供方 = `%s`；判定 = 合格（评审方 ∉ 作者集合）"
                     % ("{%s}" % ", ".join("`%s`" % v for v in el["author_vendors"]) if el["author_vendors"] else "∅",
                        el["reviewer_vendor"])))
    p = ctx["profile"]
    prof = "provider `%s` · model `%s` · transport `%s` · runtime `%s` · effort `%s`（%s）· 来源 = %s" % (
        p["provider"], p["model"], p["transport"], p["runtime"], p["effort"], p["effort_source"], p["selection_source"])
    if p.get("previous") is not None:
        prof += "；与上一轮%s（上一轮 = `%s` / `%s`）" % (
            "不同" if p["changed_from_previous"] else "相同", p["previous"]["provider"], p["previous"]["model"])
    out.append(_line("Profile", prof))
    out.append(_line("判词发布路径", "`%s`" % ctx["verdict_path"]))
    out.append(_line("通道标识", "%s · 设计正本 SHA-256 `%s` · request %s · verdict %s · receipt %s"
                     % (ctx["channel"]["id"], ctx["channel"]["design_sha256"], ctx.get("request_schema", C.REQUEST_SCHEMA), C.VERDICT_SCHEMA,
                        C.RECEIPT_SCHEMA)))
    out.append("")
    out.append(_h(2, "2. 对象与目标"))
    candidates = ctx.get("candidates") or [ctx["candidate"]]
    for i, c in enumerate(candidates):
        label = "被审对象" if len(candidates) == 1 else ("被审对象 %d%s" % (i + 1, "（首候选）" if i == 0 else ""))
        out.append(_line(label, "`%s`（%d bytes，SHA-256 `%s`）" % (c["bundle_name"], c["bytes"], c["sha256"])))
    out.append("")
    out.append(r["review_brief"]["background"].rstrip())
    out.append("")
    out.append(_h(2, "3. 改动区、整改声明与决定摘要"))
    prev = ctx["previous"]
    if prev is not None:
        out.append(_h(3, "整改声明"))
        out.append("")
        out.append((r["review_brief"]["remediation_statement"] or "").rstrip())
        out.append("")
    out.append(_h(3, "改动区（通道算）"))
    out.append("评审面默认只到下列差异块；差异块之外的发现 origin 取 unchanged_region、不得为 blocking（规则 14）。")

    def diff_lines(d):
        out.append(_line("统一差异统计", "+%d / -%d 行" % (d["added"], d["removed"])))
        for hk in d["hunks"]:
            out.append("- %s" % hk)
        if not d["hunks"]:
            out.append("- 无差异块")
        out.append("")

    if prev is None:
        # r1（Amendment 4，§6.2 第 6 条）：基线 = 现行冻结事实字节；首冻（任何冻结事实内无该路径）即整件为改动区
        region = ctx.get("changed_region") or []
        for item in region:
            if len(region) > 1:
                out.append(_h(4, "候选 `%s`" % item["bundle_name"]))
            b = item["baseline"]
            if b is None:
                out.append(_line("基线", (item.get("note") + "：整件为改动区") if item.get("note") else "无现行冻结事实（首冻）：整件为改动区"))
                out.append("")
            else:
                out.append(_line("基线", "现行冻结事实 %s · SHA-256 `%s`"
                                 % (("r%d" % b["revision"]) if b.get("revision") is not None else "（修订号不可得）", b["sha256"])))
                diff_lines(item["diff"])
    else:
        out.append(_line("基线", "上一轮候选（`previous--candidate-baseline--` 族）"))
        diffs = prev.get("diffs") or [(candidates[0]["bundle_name"], prev["diff"])]
        for name, d in diffs:
            if len(diffs) > 1:
                out.append(_h(4, "候选 `%s`" % name))
            diff_lines(d)
        out.append(_h(3, "上一轮决定摘要（决定文件 §6.12）"))
        for fid, sev, title, action, instructions, work_item in prev.get("decisions") or []:
            tail = ""
            if action == "fix":
                tail = " · " + instructions
            elif action == "skip" and work_item:
                tail = " · " + work_item
            out.append("- `%s` · %s · %s → %s%s" % (fid, sev, title, action, tail))
        if not prev.get("decisions"):
            out.append("- 上一轮无 finding，决定文件无条目")
        out.append("")
        out.append(_h(3, "上一轮 finding（须逐条处置）"))
        out.append(_line("上一轮判词", prev["verdict"]))
        for f in prev["findings"]:
            out.append("- `%s` · %s · %s" % (f["id"], f["severity"], f["title"]))
        if not prev["findings"]:
            out.append("- 上一轮无 finding")
        out.append("")
        out.append(_line("本轮 Profile 与上一轮", "%s" % ("相同" if prev["profile_same"] else "不同（显式改选）")))
    out.append("")
    out.append(_h(2, "4. 核对面"))
    out.append("")
    out.append(r["review_brief"]["check_surfaces"].rstrip())
    out.append("")
    out.append(_h(2, "5. 证据面限制声明"))
    lims = r["review_brief"]["evidence_limits"]
    for lim in lims:
        out.append("- `%s`（kind = %s）：%s" % (lim["reference"], lim.get("kind", C.EVIDENCE_LIMIT_KIND_DEFAULT), lim["reason"]))
    if not lims:
        out.append("- caller 未声明证据面限制")
    out.append("")
    out.append(_h(3, "引用解析结果（通道追加）"))
    refs = ctx["references"]
    domain_name = {"one": "域一 · 规格链目录", "two": "域二 · 机制区单元目录", "three": "域三 · 全仓"}

    def revision_annotation(ref):
        """域一引用的并列标注：引用修订号与该件现行修订号（现行修订号按 freeze-record 设计 §4.3 读取，只标注、不判失败码）。"""
        cur = ref.get("current_revision")
        return "；引用修订号 r%s，该件现行修订号 %s%s" % (
            ref.get("revision"), ("r%d" % cur) if cur is not None else "不可得",
            "" if cur == ref.get("revision") else "（引用修订号非现行，是否过时由评审方判断）")

    for ref in refs:
        st = ref["status"]
        dom = ("；解析域 = %s" % domain_name[ref["domain"]]) if ref.get("domain") else ""
        if st == "delivered":
            line = "- `%s` → 已入被审输入（`%s`%s）" % (ref["token"], ref["path"], dom)
            if ref.get("domain") == "one":
                line += revision_annotation(ref)
            out.append(line)
        elif st == "exempt":
            out.append("- `%s` → 经证据面限制声明豁免（kind = exemption）：%s" % (ref["token"], ref["reason"]))
        elif st == "planned-location":
            out.append("- `%s` → 声明为规定落点（kind = planned-location；校验：首段命中仓根闭集、该顶层成员本身不存在，两项俱成立）：%s"
                       % (ref["token"], ref["reason"]))
        elif st == "planned":
            out.append("- `%s` → 计划交付文件（被审对象所在单元目录之下、仓内尚不存在，不判缺件）" % ref["token"])
        elif st == "directory":
            out.append("- `%s` → 目录引用（不作投递对象）" % ref["token"])
        elif st == "self":
            out.append("- `%s` → 被审对象自身" % ref["token"])
        elif st == "co-candidate":
            out.append("- `%s` → 候选之一（`%s`，已入被审输入）" % (ref["token"], ref["path"]))
    if not refs:
        out.append("- 被审对象内未提取到候选引用")
    out.append("")
    out.append(_h(2, "6. 已接受残留"))
    res = r["review_brief"]["accepted_residuals"]
    for it in res:
        out.append("- `%s`：%s" % (it["id"], it["text"]))
    if not res:
        out.append("- 无")
    out.append("")
    out.append("评审方对上列残留只确认不重报（机读块 `accepted_residuals_acknowledged` 为其精确等集）。")
    out.append("")
    out.append(_h(2, "7. 投递材料表"))
    out.append("")
    out.append("| 角色 | 投递名 | 字节数 | SHA-256 前 12 位 |")
    out.append("|---|---|---|---|")
    out.append("| review-task | `%s` | — | — |" % ctx["task_file"])
    for it in ctx["pre_manifest"]:
        out.append("| %s | `%s` | %d | `%s` |" % (it["role"], it["bundle_name"], it["bytes"], it["sha256"][:12]))
    out.append("")
    out.append(_h(2, "8. 问题集"))
    out.append("%s 轮五题闭集（通道常量，每轮相同；caller 的关注点只在第 4 节核对面）：" % r["stage"])
    for qid, text in C.QUESTIONS_BY_STAGE[r["stage"]]:   # 五题闭集（§6.4，Amendment 4）
        out.append("- **%s**：%s" % (qid, text))
    out.append("")
    out.append(_h(2, "9. 轮次额度状态"))
    b = ctx["budget"]
    out.append(_line("本轮序号", str(b["round_index"])))
    out.append(_line("已投递轮数", str(b["submitted_rounds"])))
    out.append(_line("生效额度", "%d（来源 = %s）" % (b["max_rounds"], b["source"])))
    out.append(_line("已用加轮", str(b["extensions_added"])))
    out.append(_line("允许轮数", str(b["allowed_rounds"])))
    out.append("")
    out.append(_h(2, "10. 授权与停止边界"))
    out.append("- 本次评审的发起授权 = `%s`。" % ctx["authorization"])
    out.append("- 评审方判词不构成 Freeze、批准、commit 或 push 的权威；判词只是 Human 裁定的输入（`authorization_disclaimer`）。")
    out.append("- 评审方在被审输入内只读工作，不写文件、不访问网络；被审输入即全部输入。")
    out.append("- 出现 severity = human 的 finding 时 caller 恒停报 Human，无论 PASS / FAIL。")
    out.append("")
    return ("\n".join(out)).encode("utf-8")
