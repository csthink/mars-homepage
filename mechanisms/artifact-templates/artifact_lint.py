#!/usr/bin/env python3
"""artifact_lint.py · 五类工件（proposal / spec / milestones / task / ruling）的可选一致性核对。

判断辅助工具，不是门：不接 pre-commit、不进 gates all、人工发起。规则正本 = 本文件（常量与 --help 输出）
与本单元 proposal/spec/milestones/task/ruling 模板的引导块；这组 kind 取同一闭集。
design.template.md 是人工核对模板，不在本工具的 kind / --template 覆盖面。

调用形态：
  python3 mechanisms/artifact-templates/artifact_lint.py <kind> <path>...            核对实例
  python3 mechanisms/artifact-templates/artifact_lint.py <kind> --template <path>    核对模板自身
  kind ∈ proposal | spec | milestones | task | ruling；--json 把结论以 JSON 打到 stdout。

本工具退出码：0 = PASS（全部检查项通过）、1 = VIOLATION（至少一项查出违规）、
2 = NOT_CHECKED（文件不可读、非 UTF-8、结构无法可靠解析、用法错误或未捕获异常）。多文件时取最重者。

实例核对的四类判定（每类一个检查项，另加物理字节前置项）：
  physical   UTF-8 无 BOM、无 CR、无 NUL、末尾恰一个 LF；实例内不得残留 `<!-- template:guide` 引导块与模板占位。
  header     页首逐行：H1 文法、`> Depends on:` 值行、空引用行、`> 权威状态: subject ...` 行、随后恰一空行进入首个 H2；
             ruling 为页首六行 `> Ruling / Date / Type / Object / Basis / Decision`。
  headings   H2 闭集与顺序（含编号全有全无、扩展 H2 规则、反思三个 H3、task 九个 H2 与 Extension 形态、
             milestones 各 H2 内允许的 H3、task 各节最低条目）。
  ids        spec 六族定义文法与定义域；milestones 的 M-ID、task-id、类型前缀、Spec 引用、依赖；task 的 record id
             同值链、Identity 四字段、Source References；ruling 文件名与页首编号。
  lifecycle  页首以外不得出现 `> Status:` 一类治理状态 metadata 行（禁入键见 LIFECYCLE_KEYS）。
模板核对（--template）：physical、strip（剥离全部引导块后逐行等于本文件定义的骨架）、lifecycle（全文不含
状态 token Status / Revision / FROZEN / DRAFT）。
标题与定义行的识别一律排除 fenced code 与 HTML 注释；围栏或注释未闭合即 NOT_CHECKED（fail closed）。
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import sys

PASS, VIOLATION, NOT_CHECKED = "PASS", "VIOLATION", "NOT_CHECKED"
EXIT = {PASS: 0, VIOLATION: 1, NOT_CHECKED: 2}
KINDS = ("proposal", "spec", "milestones", "task", "ruling")
INSTANCE_CHECKS = ("physical", "header", "headings", "ids", "lifecycle")
TEMPLATE_CHECKS = ("physical", "strip", "lifecycle")

GUIDE_OPEN = "<!-- template:guide"
GUIDE_CLOSE = "-->"
STATUS_TOKENS = ("Status", "Revision", "FROZEN", "DRAFT")
LIFECYCLE_KEYS = ("Status", "Revision", "Round", "Review", "Owner approval", "Branch", "Workflow state",
                  "Published", "Closed", "Freeze", "Sources", "Amendment", "Drafting authorization")
SUBJECT_TAIL = "（治理记录目录按所在仓的布局规则解析；唯一状态正本）"
PLACEHOLDERS = ("<产品名>", "<milestones-subject>", "<task-record-id>", "<短标题>", "<task-id>", "RU-<NN>",
                "YYYY-MM-DD", "<Milestone 名称>", "<非空规划目标>")

PROPOSAL_H1 = "# <产品名> 产品提案（proposal）"
PROPOSAL_CORE = ("产品是什么", "目标用户与问题", "MVP 边界", "MVP 成功条件", "本提案不决定的事", "反思")
PROPOSAL_R = ("必须现在定", "同类潜在 bug 一并封住", "可接受残留（分级）")
SPEC_H1 = "# <产品名> MVP Spec"
SPEC_CORE = ("产品目标与用户问题", "In Scope", "Out of Scope", "Functional Requirements",
             "Non-functional Requirements", "核心旅程与产品约束", "MVP 整体成功条件", "本 spec 不决定的事", "反思")
SPEC_R = ("必须现在定", "同类潜在 bug 一并防止", "可接受残留（分级）")
SPEC_FAMILY_DOMAIN = {"S": "In Scope", "O": "Out of Scope", "FR": "Functional Requirements",
                      "NFR": "Non-functional Requirements", "C": "核心旅程与产品约束", "SC": "MVP 整体成功条件"}
SPEC_ID_ONLY_SECTIONS = ("In Scope", "Out of Scope", "Functional Requirements",
                         "Non-functional Requirements", "MVP 整体成功条件")
MILESTONES_H1 = "# <产品名> MVP Milestones"
MILESTONES_CORE = ("规划基线", "Milestone 条目", "跨 Milestone 依赖与调度", "反思")
MILESTONES_R = SPEC_R
MILESTONE_FIELDS = ("阶段目标", "UI 变更", "UI lineage", "任务清单", "验收条件")
TASK_TYPES = {"低保真": "design", "高保真": "design", "前端": "feature", "后端": "feature", "治理机制": "gov"}
MILESTONE_BLOCK = (
    "### M-NN <Milestone 名称>", "",
    "- 阶段目标：<非空规划目标>", "- UI 变更：<有 | 无>", "- UI lineage：<不适用 | 非空稳定 lineage 引用>",
    "- 任务清单：",
    "  - <task-id> <非空一句话交付边界> · 类型：<低保真 | 高保真 | 前端 | 后端 | 治理机制>"
    " · Spec 引用：spec.md@rN <ID>、<ID> · 依赖：<无 | task-id 列表>",
    "- 验收条件：", "  - <非空且可判断的 Milestone completion 条件>")
TASK_H1 = "# <task-record-id> · <短标题>"
TASK_H2 = ("Identity", "通俗说明（给人读）", "Goal", "Goal Conditions", "Scope In", "Scope Out",
           "Constraints", "Acceptance Criteria", "Source References")
TASK_LIST_SECTIONS = ("Goal Conditions", "Scope In", "Scope Out", "Acceptance Criteria")
TASK_PLAIN_SENTENCE = "本节只帮助人建立心智模型，不是任务契约的权威取值；冲突时以其余正式章节为准。"
IDENTITY_LINES = ("- Task record ID: `<task-record-id>`", "- Task kind: `feature | hotfix | design | governance`",
                  "- Definition subject: `<task-record-id>-task`", "- Product line: `<stable-product-line-id>`")
TASK_KIND_BY_PREFIX = {"feature": "feature", "design": "design", "gov": "governance", "hotfix": "hotfix"}
PRIMARY_LINE = "- Primary source: `milestones.md@rN <task-id>`"
RULING_KEYS = ("Ruling", "Date", "Type", "Object", "Basis", "Decision")
RULING_TYPES = ("finalization", "review-skip", "finding-disposition", "close", "done")
RULING_HEADER = ("> Ruling: RU-<NN>", "> Date: YYYY-MM-DD",
                 "> Type: <finalization | review-skip | finding-disposition | close | done>",
                 "> Object: <object identity>", "> Basis: <durable references>", "> Decision: <Owner decision>")

RE_SPEC_DEF = re.compile(r"^- \*\*((S|O|FR|NFR|C|SC)-([0-9]+))\*\*[ \t]+(\S.*)$")
RE_SPEC_ID = re.compile(r"^(S|O|FR|NFR|C|SC)-[0-9]+$")
RE_SPEC_REF_GROUP = re.compile(r"^spec\.md@r[1-9][0-9]*[ \t]+(\S.*)$")
RE_TASK_ID = re.compile(r"^(feature|design|gov)-t(0|[1-9][0-9]*)$")
RE_RECORD_ID = re.compile(r"^(?:(feature|design|gov)-t(?:0|[1-9][0-9]*)|(hotfix)-h([0-9a-f]{64}))$")
RE_MILESTONE_H3 = re.compile(r"^### (M-([0-9]+)) (\S.*?)(?: · Supersedes：(\S.*?))?"
                             r"(?: · WITHDRAWN in milestones\.md@r[1-9][0-9]*)?$")
RE_TASK_LINE = re.compile(r"^  - (\S+) (.+?) · 类型：(\S+) · Spec 引用：(.+?) · 依赖：(.+?)"
                          r"(?: · Supersedes：(\S.*?))?(?: · WITHDRAWN in milestones\.md@r[1-9][0-9]*)?$")
RE_RULING_ID = re.compile(r"^RU-(0[1-9]|[1-9][0-9]+)$")
RE_RULING_FILE = re.compile(r"^RU-(0[1-9]|[1-9][0-9]+)-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
RE_MILESTONES_SUBJECT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RE_HEADER_KV = re.compile(r"^> ([A-Za-z][A-Za-z ]*?):[ \t]*(.*?)[ \t]*$")
RE_H2 = re.compile(r"^## (?:([0-9]+)\. )?(\S.*)$")
RE_H3 = re.compile(r"^### (\S.*)$")
RE_SUFFIX = re.compile(r"^（[^（）]+）$")


class Unparseable(Exception):
    """结构无法可靠解析（围栏 / 注释未闭合等）：相关检查项落 NOT_CHECKED。"""


class Finding(object):
    def __init__(self, line, detail):
        self.line, self.detail = line, detail

    def json(self):
        return {"line": self.line, "detail": self.detail}


# ---------------------------------------------------------------- 文本骨架（模板剥离等式的期望值）

def status_line(subject):
    return "> 权威状态: subject `%s`%s" % (subject, SUBJECT_TAIL)


def _with_blank(lines):
    out = []
    for ln in lines:
        out.extend(["", ln])
    return out


def skeleton(kind):
    if kind == "proposal":
        head = [PROPOSAL_H1, "", "> Depends on:", "> 无", ">", status_line("proposal")]
        body = ["## " + h for h in PROPOSAL_CORE] + ["### " + h for h in PROPOSAL_R]
        return head + _with_blank(body)
    if kind == "spec":
        head = [SPEC_H1, "", "> Depends on:", "> `proposal.md@rN`", ">", status_line("spec")]
        body = ["## " + h for h in SPEC_CORE] + ["### " + h for h in SPEC_R]
        return head + _with_blank(body)
    if kind == "milestones":
        head = [MILESTONES_H1, "", "> Depends on:", "> `spec.md@rN`", ">", status_line("<milestones-subject>")]
        out = head + _with_blank(["## " + MILESTONES_CORE[0], "## " + MILESTONES_CORE[1]])
        out += [""] + list(MILESTONE_BLOCK)
        out += _with_blank(["## " + MILESTONES_CORE[2], "## " + MILESTONES_CORE[3]] + ["### " + h for h in MILESTONES_R])
        return out
    if kind == "task":
        head = [TASK_H1, "", "> Depends on:", "> `milestones.md@rN <task-id>`", ">", status_line("<task-record-id>-task")]
        out = head + ["", "## Identity", ""] + list(IDENTITY_LINES)
        out += ["", "## 通俗说明（给人读）", "", TASK_PLAIN_SENTENCE]
        out += _with_blank(["## " + h for h in TASK_H2[2:8]])
        out += ["", "## Source References", "", PRIMARY_LINE]
        return out
    if kind == "ruling":
        return list(RULING_HEADER)
    raise ValueError(kind)


# ---------------------------------------------------------------- 读入与结构行

def physical_findings(data):
    f = []
    if data.startswith(b"\xef\xbb\xbf"):
        f.append(Finding(1, "含 UTF-8 BOM"))
    if b"\r" in data:
        f.append(Finding(None, "含 CR"))
    if b"\x00" in data:
        f.append(Finding(None, "含 NUL"))
    if not data.endswith(b"\n") or data.endswith(b"\n\n"):
        f.append(Finding(None, "文件须以恰一个 LF 结束"))
    return f


def structural_mask(lines):
    """每行是否属正文（非 fenced code、非 HTML 注释）；未闭合即 Unparseable。"""
    mask, in_fence, fence_mark, in_comment = [], False, None, False
    for i, ln in enumerate(lines):
        if in_comment:
            mask.append(False)
            if "-->" in ln:
                in_comment = False
            continue
        if in_fence:
            mask.append(False)
            if ln.startswith(fence_mark):
                in_fence = False
            continue
        s = ln.lstrip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence, fence_mark = True, s[:3]
            mask.append(False)
            continue
        if ln.lstrip().startswith("<!--"):
            mask.append(False)
            if "-->" not in ln:
                in_comment = True
            continue
        mask.append(True)
    if in_fence:
        raise Unparseable("fenced code 未闭合，无法可靠识别标题")
    if in_comment:
        raise Unparseable("HTML 注释未闭合，无法可靠识别标题")
    return mask


def strip_guides(lines):
    """模板剥离：删除全部引导块并合并空行；形态违规返回 findings。"""
    out, findings, i, n = [], [], 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln == GUIDE_OPEN:
            j, closed = i + 1, False
            while j < n:
                if lines[j] == GUIDE_OPEN:
                    findings.append(Finding(j + 1, "引导块嵌套"))
                if lines[j] == GUIDE_CLOSE:
                    closed = True
                    break
                j += 1
            if not closed:
                findings.append(Finding(i + 1, "引导块未闭合"))
                break
            i = j + 1
            continue
        if ln.startswith("<!--"):
            findings.append(Finding(i + 1, "非 template:guide 形态的 HTML 注释"))
        out.append(ln)
        i += 1
    merged = []
    for ln in out:
        if ln == "" and merged and merged[-1] == "":
            continue
        merged.append(ln)
    while merged and merged[-1] == "":
        merged.pop()
    return merged, findings


def headings(lines, mask):
    """[(行号 1 起, level, text)]，只取正文行。"""
    out = []
    for i, ln in enumerate(lines):
        if not mask[i]:
            continue
        if ln.startswith("### "):
            out.append((i + 1, 3, ln[4:]))
        elif ln.startswith("## "):
            out.append((i + 1, 2, ln[3:]))
        elif ln.startswith("# "):
            out.append((i + 1, 1, ln[2:]))
    return out


def match_core(text, title, allow_suffix):
    if text == title:
        return True
    return allow_suffix and text.startswith(title) and bool(RE_SUFFIX.match(text[len(title):]))


def split_h2(text):
    m = RE_H2.match("## " + text)
    return (m.group(1), m.group(2)) if m else (None, text)


# ---------------------------------------------------------------- 页首

def _name_ok(name):
    return bool(name) and name == name.strip() and "<" not in name and ">" not in name


def check_header(kind, lines):
    f = []
    if kind == "ruling":
        return check_ruling_header(lines)
    if len(lines) < 8:
        return [Finding(1, "页首不足八行（H1、空行、Depends on 两行、空引用行、权威状态行、空行、首个 H2）")]
    h1 = lines[0]
    subject = None
    if kind == "proposal":
        m = re.match(r"^# (.+) 产品提案（proposal）$", h1)
        if not m or not _name_ok(m.group(1)):
            f.append(Finding(1, "H1 须为「# <产品名> 产品提案（proposal）」，产品名非空且不含 < >"))
        if lines[3] != "> 无":
            f.append(Finding(4, "proposal 的 Depends on 值行固定为「> 无」"))
        subject = "proposal"
    elif kind in ("spec", "milestones"):
        word = "Spec" if kind == "spec" else "Milestones"
        m = re.match(r"^# (.+) MVP %s(（[^（）]+）)?$" % word, h1)
        if not m or not _name_ok(m.group(1)):
            f.append(Finding(1, "H1 须为「# <产品名> MVP %s[（括注）]」，产品名非空且不含 < >" % word))
        up = "proposal" if kind == "spec" else "spec"
        if not re.match(r"^> `%s\.md@r[1-9][0-9]*`$" % up, lines[3]):
            f.append(Finding(4, "Depends on 值行须为「> `%s.md@rN`」（N 为正整数，不写路径 / latest）" % up))
        if kind == "spec":
            subject = "spec"
        else:
            m2 = re.match(r"^> 权威状态: subject `([^`]*)`", lines[5])
            if m2 and RE_MILESTONES_SUBJECT.match(m2.group(1)):
                subject = m2.group(1)
            else:
                f.append(Finding(6, "milestones 的 subject 须匹配 [a-z0-9]+(?:-[a-z0-9]+)*"))
    elif kind == "task":
        m = re.match(r"^# (\S+) · (\S.*)$", h1)
        rid = m.group(1) if m else None
        if not m or not RE_RECORD_ID.match(rid):
            f.append(Finding(1, "H1 须为「# <task-record-id> · <短标题>」，ID 文法 feature-t<N> | design-t<N> | gov-t<N> | hotfix-h<64hex>"))
            rid = None
        if rid and rid.startswith("hotfix-"):
            if lines[3] != "> `无`":
                f.append(Finding(4, "hotfix 的 Depends on 值行固定为「> `无`」"))
        else:
            m3 = re.match(r"^> `milestones\.md@r[1-9][0-9]* (\S+)`$", lines[3])
            if not m3:
                f.append(Finding(4, "里程碑任务的 Depends on 值行须为「> `milestones.md@rN <task-id>`」"))
            elif rid and m3.group(1) != rid:
                f.append(Finding(4, "Depends on 内 task-id 须等于 H1 的 task-record-id"))
        subject = (rid + "-task") if rid else None
    if lines[1] != "":
        f.append(Finding(2, "H1 后须恰一空行"))
    if lines[2] != "> Depends on:":
        f.append(Finding(3, "第三行须为「> Depends on:」"))
    if lines[4] != ">":
        f.append(Finding(5, "Depends on 值行后须为空引用行「>」"))
    if subject is not None and lines[5] != status_line(subject):
        f.append(Finding(6, "权威状态行须逐字为「%s」" % status_line(subject)))
    if lines[6] != "":
        f.append(Finding(7, "权威状态行后须恰一空行"))
    if not lines[7].startswith("## "):
        f.append(Finding(8, "页首之后须直接进入首个 H2，不得插入其他 metadata"))
    return f


def check_ruling_header(lines):
    f = []
    if len(lines) < 6:
        return [Finding(1, "裁定件页首须为六行 > Ruling / Date / Type / Object / Basis / Decision")]
    values = {}
    for i, key in enumerate(RULING_KEYS):
        m = RE_HEADER_KV.match(lines[i])
        if not m or m.group(1) != key:
            f.append(Finding(i + 1, "第 %d 行须为「> %s: <值>」" % (i + 1, key)))
            continue
        values[key] = m.group(2)
        if not m.group(2):
            f.append(Finding(i + 1, "%s 值不得为空" % key))
    if "Ruling" in values and not RE_RULING_ID.match(values["Ruling"]):
        f.append(Finding(1, "Ruling 值须为 RU-<NN>（01 起，99 后写 100）"))
    if "Date" in values:
        try:
            if not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", values["Date"]):
                raise ValueError
            datetime.date.fromisoformat(values["Date"])
        except ValueError:
            f.append(Finding(2, "Date 须为有效的 YYYY-MM-DD"))
    if "Type" in values and values["Type"] not in RULING_TYPES:
        f.append(Finding(3, "Type 须取闭集之一：%s" % " | ".join(RULING_TYPES)))
    if len(lines) > 6 and lines[6] != "":
        f.append(Finding(7, "页首六行后须为空行"))
    return f


# ---------------------------------------------------------------- 标题闭集

def _core_scan(kind, hs, core, r_titles, allow_h2_suffix, allow_h3_suffix, allow_ext, r_exact):
    f = []
    h2s = [(ln, split_h2(t)) for (ln, lv, t) in hs if lv == 2]
    nums = [n for (_ln, (n, _t)) in h2s]
    if any(n is not None for n in nums) and any(n is None for n in nums):
        f.append(Finding(None, "H2 编号须全有或全无"))
    elif nums and nums[0] is not None:
        for k, (ln, (n, _t)) in enumerate(h2s):
            if int(n) != k + 1:
                f.append(Finding(ln, "H2 编号须自 1 起逐节加一（此处应为 %d）" % (k + 1)))
    hits = {}
    for ln, (_n, text) in h2s:
        matched = None
        for title in core:
            if match_core(text, title, allow_h2_suffix):
                matched = title
                break
        if matched is None:
            if not allow_ext:
                f.append(Finding(ln, "本类工件不开放扩展 H2：「%s」" % text))
            continue
        hits.setdefault(matched, []).append(ln)
    for title in core:
        if title not in hits:
            f.append(Finding(None, "核心 H2「%s」缺失" % title))
        elif len(hits[title]) > 1:
            f.append(Finding(hits[title][1], "核心 H2「%s」出现多次" % title))
    order = [t for t in core if t in hits]
    seq = sorted(order, key=lambda t: hits[t][0])
    if seq != order:
        f.append(Finding(None, "核心 H2 相对顺序须与闭集一致：%s" % " → ".join(core)))
    # 反思 H3
    last = core[-1]
    if last in hits:
        start = hits[last][0]
        nxt = min([ln for (ln, lv, _t) in hs if lv == 2 and ln > start] + [10 ** 9])
        h3s = [(ln, t) for (ln, lv, t) in hs if lv == 3 and start < ln < nxt]
        matched = []
        for ln, t in h3s:
            hit = None
            for r in r_titles:
                if match_core(t, r, allow_h3_suffix):
                    hit = r
                    break
            if hit is None and r_exact:
                f.append(Finding(ln, "「%s」内只允许三个反思 H3" % last))
            if hit is not None:
                matched.append((ln, hit))
        seen = [r for (_ln, r) in matched]
        for r in r_titles:
            if seen.count(r) == 0:
                f.append(Finding(None, "反思 H3「%s」缺失" % r))
            elif seen.count(r) > 1:
                f.append(Finding(None, "反思 H3「%s」出现多次" % r))
        if [r for r in seen if seen.count(r) == 1] != [r for r in r_titles if r in seen and seen.count(r) == 1]:
            f.append(Finding(None, "反思三个 H3 顺序须为 %s" % " → ".join(r_titles)))
    return f, hits


def check_headings(kind, lines, mask):
    hs = headings(lines, mask)
    f = []
    h1s = [ln for (ln, lv, _t) in hs if lv == 1]
    if kind != "ruling" and len(h1s) != 1:
        f.append(Finding(h1s[1] if len(h1s) > 1 else None, "全文须恰一行 H1"))
    if kind == "proposal":
        g, _ = _core_scan(kind, hs, PROPOSAL_CORE, PROPOSAL_R, True, True, True, False)
        return f + g
    if kind == "spec":
        g, _ = _core_scan(kind, hs, SPEC_CORE, SPEC_R, True, False, True, False)
        return f + g
    if kind == "milestones":
        g, hits = _core_scan(kind, hs, MILESTONES_CORE, MILESTONES_R, False, False, False, True)
        f += g
        for title in (MILESTONES_CORE[0], MILESTONES_CORE[2]):
            if title in hits:
                start = hits[title][0]
                nxt = min([ln for (ln, lv, _t) in hs if lv == 2 and ln > start] + [10 ** 9])
                for ln, lv, _t in hs:
                    if lv == 3 and start < ln < nxt:
                        f.append(Finding(ln, "「%s」不允许 H3" % title))
        return f
    if kind == "task":
        h2s = [(ln, t) for (ln, lv, t) in hs if lv == 2]
        core = [(ln, t) for (ln, t) in h2s if not t.startswith("Extension: ")]
        ext = [(ln, t) for (ln, t) in h2s if t.startswith("Extension: ")]
        if [t for (_ln, t) in core] != list(TASK_H2):
            f.append(Finding(None, "九个核心 H2 须恰为且依序为：%s" % " / ".join(TASK_H2)))
        elif ext and ext[0][0] < core[-1][0]:
            f.append(Finding(ext[0][0], "Extension H2 须在九个核心 H2 之后"))
        for ln, t in ext:
            body = _section_body(lines, mask, hs, ln)
            first = next((b for b in body if b.strip()), "")
            m = re.match(r"^Maps to: (.+)$", first)
            if not m or m.group(1) not in TASK_H2:
                f.append(Finding(ln, "Extension 首行须为「Maps to: <核心 H2 名>」"))
        if [t for (_ln, t) in core] == list(TASK_H2):
            sec = dict((t, ln) for (ln, t) in core)
            for title in TASK_LIST_SECTIONS:
                body = _section_body(lines, mask, hs, sec[title])
                if not any(b.startswith("- ") for b in body):
                    f.append(Finding(sec[title], "「%s」至少一条无序列表条目" % title))
            for title in ("Goal", "Constraints"):
                body = _section_body(lines, mask, hs, sec[title])
                if not any(b.strip() for b in body):
                    f.append(Finding(sec[title], "「%s」不得为空" % title))
            body = _section_body(lines, mask, hs, sec[TASK_H2[1]])
            first = next((b for b in body if b.strip()), "")
            if first != TASK_PLAIN_SENTENCE:
                f.append(Finding(sec[TASK_H2[1]], "通俗说明首段须逐字为「%s」" % TASK_PLAIN_SENTENCE))
        return f
    return f


def _section_body(lines, mask, hs, start_ln):
    """H2 起始行（1 起）之后到下一 H2 之前的正文行（排除围栏 / 注释）。"""
    nxt = min([ln for (ln, lv, _t) in hs if lv == 2 and ln > start_ln] + [len(lines) + 1])
    return [lines[i] for i in range(start_ln, nxt - 1) if mask[i]]


# ---------------------------------------------------------------- ID 文法

def check_ids(kind, path, lines, mask):
    hs = headings(lines, mask)
    if kind == "spec":
        return _spec_ids(lines, mask, hs)
    if kind == "milestones":
        return _milestones_ids(lines, mask, hs)
    if kind == "task":
        return _task_ids(path, lines, mask, hs)
    if kind == "ruling":
        return _ruling_ids(path, lines)
    return []


def _h2_of_line(hs, i):
    cur = None
    for ln, lv, t in hs:
        if ln > i + 1:
            break
        if lv == 2:
            cur = split_h2(t)[1]
    return cur


def _spec_ids(lines, mask, hs):
    f, seen, numeric = [], {}, {}
    for i, ln in enumerate(lines):
        if not mask[i]:
            continue
        sec = _h2_of_line(hs, i)
        core = next((t for t in SPEC_CORE if sec and match_core(sec, t, True)), None)
        m = RE_SPEC_DEF.match(ln)
        if m:
            full, fam, num = m.group(1), m.group(2), m.group(3)
            if int(num) <= 0:
                f.append(Finding(i + 1, "%s 的数值须大于零" % full))
            if core != SPEC_FAMILY_DOMAIN[fam]:
                f.append(Finding(i + 1, "%s 族只能在「%s」内定义" % (fam, SPEC_FAMILY_DOMAIN[fam])))
            if full in seen:
                f.append(Finding(i + 1, "%s 定义重复（首见第 %d 行）" % (full, seen[full])))
            seen.setdefault(full, i + 1)
            key = (fam, int(num))
            if key in numeric and numeric[key] != full:
                f.append(Finding(i + 1, "%s 与 %s 同族同值冲突" % (full, numeric[key])))
            numeric.setdefault(key, full)
        elif ln.startswith("- ") and core in SPEC_ID_ONLY_SECTIONS:
            f.append(Finding(i + 1, "「%s」内每条顶层条目须带族 ID：- **<族>-<n>** …" % core))
    return f


def _milestones_ids(lines, mask, hs):
    f = []
    k2 = next((ln for (ln, lv, t) in hs if lv == 2 and match_core(split_h2(t)[1], MILESTONES_CORE[1], False)), None)
    if k2 is None:
        return f
    end = min([ln for (ln, lv, _t) in hs if lv == 2 and ln > k2] + [len(lines) + 1])
    m_ids, task_ids, deps_by_task, fam_nums = {}, {}, {}, {}
    block, blocks = None, []
    for i in range(k2, end - 1):
        if not mask[i]:
            continue
        ln = lines[i]
        if ln.startswith("### "):
            m = RE_MILESTONE_H3.match(ln)
            if not m:
                f.append(Finding(i + 1, "Milestone H3 须为「### M-NN <名称>[ · Supersedes：…][ · WITHDRAWN in milestones.md@rN]」"))
                block = {"fields": [], "tasks": [], "accept": [], "ln": i + 1, "bad": True}
                blocks.append(block)
                continue
            mid, num = m.group(1), int(m.group(2))
            if num <= 0:
                f.append(Finding(i + 1, "%s 数值须大于零" % mid))
            if num in m_ids:
                f.append(Finding(i + 1, "%s 与 %s 数值冲突" % (mid, m_ids[num])))
            m_ids.setdefault(num, mid)
            block = {"fields": [], "tasks": [], "accept": [], "ln": i + 1, "bad": False, "cur": None}
            blocks.append(block)
            continue
        if block is None or block["bad"]:
            continue
        if ln.startswith("- "):
            m = re.match(r"^- ([^：]+)：(.*)$", ln)
            name = m.group(1) if m else None
            block["fields"].append((i + 1, name, m.group(2) if m else None))
            block["cur"] = name
            continue
        if ln.startswith("  - "):
            if block["cur"] == "任务清单":
                block["tasks"].append((i + 1, ln))
            elif block["cur"] == "验收条件":
                block["accept"].append((i + 1, ln))
            continue
    for b in blocks:
        if b["bad"]:
            continue
        names = [n for (_ln, n, _v) in b["fields"]]
        if names != list(MILESTONE_FIELDS):
            f.append(Finding(b["ln"], "Milestone 固定字段须恰为且依序为：%s" % " / ".join(MILESTONE_FIELDS)))
        for ln, n, v in b["fields"]:
            if n == "阶段目标" and not v.strip():
                f.append(Finding(ln, "阶段目标不得为空"))
            if n == "UI 变更" and v not in ("有", "无"):
                f.append(Finding(ln, "UI 变更须为 有 | 无"))
            if n == "UI lineage" and not v.strip():
                f.append(Finding(ln, "UI lineage 须为 不适用 或非空引用"))
            if n == "任务清单" and v not in ("", "无"):
                f.append(Finding(ln, "任务清单字段行后只跟任务行（或写 无）"))
            if n == "验收条件" and v != "":
                f.append(Finding(ln, "验收条件字段行后只跟条件行"))
        if not b["accept"]:
            f.append(Finding(b["ln"], "验收条件至少一条"))
        for ln, raw in b["tasks"]:
            m = RE_TASK_LINE.match(raw)
            if not m:
                f.append(Finding(ln, "任务行须为「  - <task-id> <边界> · 类型：<五值> · Spec 引用：<组> · 依赖：<无 | 列表>」"))
                continue
            tid, boundary, typ, refs, deps = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
            mt = RE_TASK_ID.match(tid)
            if not mt:
                f.append(Finding(ln, "task-id「%s」须为 feature-t<N> | design-t<N> | gov-t<N>（N 自 0 起不补零）" % tid))
                continue
            if tid in task_ids:
                f.append(Finding(ln, "task-id %s 重复定义（首见第 %d 行）" % (tid, task_ids[tid])))
            task_ids.setdefault(tid, ln)
            fam_nums.setdefault(mt.group(1), set()).add(int(mt.group(2)))
            if any(sep in boundary for sep in (" · 类型：", " · Spec 引用：", " · 依赖：")):
                f.append(Finding(ln, "交付边界不得含固定分隔串"))
            if typ not in TASK_TYPES:
                f.append(Finding(ln, "类型须取 %s" % " | ".join(TASK_TYPES)))
            elif TASK_TYPES[typ] != mt.group(1):
                f.append(Finding(ln, "类型「%s」须配前缀 %s-" % (typ, TASK_TYPES[typ])))
            for grp in refs.split("；"):
                mg = RE_SPEC_REF_GROUP.match(grp.strip())
                if not mg or not all(RE_SPEC_ID.match(t) for t in mg.group(1).split("、")):
                    f.append(Finding(ln, "Spec 引用组须为「spec.md@rN <ID>[、<ID>…]」，多组以 ； 分隔"))
                    break
            deps_by_task[tid] = (ln, deps)
    for tid, (ln, deps) in deps_by_task.items():
        if deps == "无":
            continue
        items = deps.split("、")
        if len(set(items)) != len(items):
            f.append(Finding(ln, "依赖列表重复"))
        for d in items:
            if d == tid:
                f.append(Finding(ln, "依赖不得含自身"))
            elif d not in task_ids:
                f.append(Finding(ln, "依赖目标 %s 未在本件定义" % d))
    for fam, nums in fam_nums.items():
        expect = set(range(0, max(nums) + 1))
        if nums != expect:
            f.append(Finding(None, "%s 族编号须自 t0 起连续无空洞，缺 %s" % (fam, sorted(expect - nums))))
    return f


def _task_ids(path, lines, mask, hs):
    f = []
    m = re.match(r"^# (\S+) · ", lines[0] if lines else "")
    rid = m.group(1) if m and RE_RECORD_ID.match(m.group(1)) else None
    if rid is None:
        return f
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem != rid:
        f.append(Finding(1, "文件名 stem「%s」须等于 task-record-id「%s」" % (stem, rid)))
    parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
    grand = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(path))))
    if grand == "tasks" and parent != rid:
        f.append(Finding(1, "目录名「%s」须等于 task-record-id「%s」" % (parent, rid)))
    prefix = RE_RECORD_ID.match(rid)
    family = prefix.group(1) or prefix.group(2)
    sec = dict((split_h2(t)[1], ln) for (ln, lv, t) in hs if lv == 2)
    if "Identity" in sec:
        body = [b for b in _section_body(lines, mask, hs, sec["Identity"]) if b.strip()]
        expect_keys = [l.split(":")[0] for l in IDENTITY_LINES]
        if [b.split(":")[0] for b in body] != expect_keys:
            f.append(Finding(sec["Identity"], "Identity 须恰为四行、依序：%s" % " / ".join(k[2:] for k in expect_keys)))
        else:
            vals = [re.match(r"^- [^:]+: `([^`]*)`$", b) for b in body]
            if not all(vals):
                f.append(Finding(sec["Identity"], "Identity 字段值须以反引号包裹"))
            else:
                v = [x.group(1) for x in vals]
                if v[0] != rid:
                    f.append(Finding(sec["Identity"], "Task record ID 须等于 H1 的「%s」" % rid))
                if v[1] not in TASK_KIND_BY_PREFIX.values():
                    f.append(Finding(sec["Identity"], "Task kind 须取一个实际值：feature | hotfix | design | governance"))
                elif v[1] != TASK_KIND_BY_PREFIX[family]:
                    f.append(Finding(sec["Identity"], "Task kind「%s」与 ID 前缀 %s- 不一致" % (v[1], family)))
                if v[2] != rid + "-task":
                    f.append(Finding(sec["Identity"], "Definition subject 须为「%s-task」" % rid))
                if not v[3]:
                    f.append(Finding(sec["Identity"], "Product line 不得为空"))
    if "Source References" in sec:
        body = [b for b in _section_body(lines, mask, hs, sec["Source References"]) if b.strip()]
        if family == "hotfix":
            f += _hotfix_sources(sec["Source References"], body, prefix.group(3))
        else:
            dep = lines[3][2:] if len(lines) > 3 else ""
            if not body or body[0] != "- Primary source: %s" % dep:
                f.append(Finding(sec["Source References"], "首行须为「- Primary source: %s」（逐字等于页首依赖）" % dep))
            for b in body[1:]:
                mm = re.match(r"^- (Spec|Decision|Other): `([^`]+)`$", b)
                if not mm:
                    f.append(Finding(sec["Source References"], "来源行须为「- Spec | Decision | Other: `…`」"))
                    continue
                kind_, val = mm.group(1), mm.group(2)
                if kind_ == "Spec":
                    ms = re.match(r"^spec\.md@r[1-9][0-9]*[ \t]+(\S.*)$", val)
                    ids = re.split(r"[、,\s]+", ms.group(1).strip()) if ms else []
                    if not ms or not ids or not all(RE_SPEC_ID.match(x) for x in ids):
                        f.append(Finding(sec["Source References"], "Spec 行须为「spec.md@rN <稳定 ID 列表>」"))
                elif not re.search(r"@r[1-9][0-9]*", val):
                    f.append(Finding(sec["Source References"], "%s 行须带 @rN 冻结修订号" % kind_))
    return f


def _hotfix_sources(ln, body, digest):
    f = []
    pat = (r"^- Primary source: hotfix-registration `([A-Za-z0-9_-]+)`$",
           r"^- Registration byte length: `(0|[1-9][0-9]*)`$",
           r"^- Registration SHA-256: `([0-9a-f]{64})`$")
    if len(body) < 3:
        return [Finding(ln, "hotfix 的 Source References 须恰含三行：Primary source / Registration byte length / Registration SHA-256")]
    ms = [re.match(p, b) for p, b in zip(pat, body[:3])]
    if not all(ms):
        return [Finding(ln, "hotfix 三行形态：hotfix-registration `<base64url>` / byte length `<n>` / SHA-256 `<64hex>`")]
    raw = ms[0].group(1)
    try:
        data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError):
        return [Finding(ln, "base64url 不可解码")]
    if int(ms[1].group(1)) != len(data):
        f.append(Finding(ln, "Registration byte length 与解码字节数不符"))
    sha = hashlib.sha256(data).hexdigest()
    if sha != ms[2].group(1):
        f.append(Finding(ln, "Registration SHA-256 与解码字节的 SHA-256 不符"))
    if sha != digest:
        f.append(Finding(ln, "record ID 内 digest 与登记字节 SHA-256 不符"))
    return f


def _ruling_ids(path, lines):
    f = []
    name = os.path.basename(path)
    m = RE_RULING_FILE.match(name)
    if not m:
        f.append(Finding(None, "文件名须为 RU-<NN>-<slug>.md（slug 文法 [a-z0-9]+(?:-[a-z0-9]+)*）"))
        return f
    mh = RE_HEADER_KV.match(lines[0]) if lines else None
    if mh and mh.group(1) == "Ruling" and RE_RULING_ID.match(mh.group(2)):
        if mh.group(2) != "RU-" + m.group(1):
            f.append(Finding(1, "页首 Ruling 编号须等于文件名编号 RU-%s" % m.group(1)))
    return f


# ---------------------------------------------------------------- 生命周期禁入

def check_lifecycle(kind, lines, mask):
    f = []
    if kind == "ruling":
        return f
    for i, ln in enumerate(lines):
        if i < 7 or not mask[i]:
            continue
        m = RE_HEADER_KV.match(ln)
        if m and m.group(1) in LIFECYCLE_KEYS:
            f.append(Finding(i + 1, "禁入的治理状态 metadata 行「> %s:」" % m.group(1)))
    return f


# ---------------------------------------------------------------- 单文件核对

def lint_file(kind, path, template=False, *, data=None):
    checks = []
    names = TEMPLATE_CHECKS if template else INSTANCE_CHECKS

    def done(name, findings):
        checks.append({"check": name, "state": VIOLATION if findings else PASS,
                       "findings": [x.json() for x in findings]})

    def not_checked(names_, reason):
        for n in names_:
            checks.append({"check": n, "state": NOT_CHECKED, "findings": [], "reason": reason})

    if data is None:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            not_checked(names, "文件不可读：%s" % exc)
            return _file_record(kind, path, template, checks)
    pf = physical_findings(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        not_checked(names, "不是合法 UTF-8")
        return _file_record(kind, path, template, checks)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if template:
        done("physical", pf)
        stripped, form = strip_guides(lines)
        diffs = list(form)
        gold = skeleton(kind)
        for k in range(max(len(stripped), len(gold))):
            exp = gold[k] if k < len(gold) else None
            act = stripped[k] if k < len(stripped) else None
            if exp != act:
                diffs.append(Finding(None, "剥离后第 %d 行：期望 %r，实际 %r" % (k + 1, exp, act)))
        done("strip", diffs)
        done("lifecycle", [Finding(None, "含状态 token %r" % t) for t in STATUS_TOKENS if t in text])
        return _file_record(kind, path, template, checks)
    residue = []
    for i, ln in enumerate(lines):
        if ln.startswith(GUIDE_OPEN):
            residue.append(Finding(i + 1, "实例内残留模板引导块"))
        for ph in PLACEHOLDERS:
            if ph in ln:
                residue.append(Finding(i + 1, "实例内残留模板占位「%s」" % ph))
    done("physical", pf + residue)
    done("header", check_header(kind, lines))
    try:
        mask = structural_mask(lines)
    except Unparseable as exc:
        not_checked(("headings", "ids", "lifecycle"), str(exc))
        return _file_record(kind, path, template, checks)
    done("headings", check_headings(kind, lines, mask))
    done("ids", check_ids(kind, path, lines, mask))
    done("lifecycle", check_lifecycle(kind, lines, mask))
    return _file_record(kind, path, template, checks)


def aggregate(states):
    if VIOLATION in states:
        return VIOLATION
    if NOT_CHECKED in states:
        return NOT_CHECKED
    return PASS


def _file_record(kind, path, template, checks):
    state = aggregate([c["state"] for c in checks])
    return {"path": path, "kind": kind, "mode": "template" if template else "instance",
            "state": state, "checks": checks}


# ---------------------------------------------------------------- 入口

def main(argv=None):
    parser = argparse.ArgumentParser(prog="artifact_lint.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--template", action="store_true", help="核对模板自身（剥离等式）而非实例")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结论")
    args = parser.parse_args(argv)
    files = [lint_file(args.kind, p, args.template) for p in args.paths]
    state = aggregate([f["state"] for f in files])
    rec = {"tool": "artifact-lint", "state": state, "exit_code": EXIT[state], "files": files}
    if args.json:
        sys.stdout.write(json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    else:
        for f in files:
            sys.stdout.write("[artifact-lint] %s %s (%s %s)\n" % (f["state"], f["path"], f["kind"], f["mode"]))
            for c in f["checks"]:
                if c["state"] == NOT_CHECKED:
                    sys.stdout.write("  未查成 %s：%s\n" % (c["check"], c.get("reason")))
                for x in c["findings"]:
                    where = ("第 %d 行" % x["line"]) if x["line"] else "全文"
                    sys.stdout.write("  违规 %s：%s：%s\n" % (c["check"], where, x["detail"]))
    return EXIT[state]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # 未捕获异常映射为 2
        sys.stderr.write("[artifact-lint] NOT_CHECKED：未捕获异常 %s：%s\n" % (type(exc).__name__, exc))
        sys.exit(2)
