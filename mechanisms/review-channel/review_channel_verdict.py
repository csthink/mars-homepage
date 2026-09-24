"""review_channel_verdict — 判词提取（信封包容三级）与机读块校验（设计 §6.5）。

- 候选块五段分区：opener 行 · 前缀 · JSON 值区段（raw_decode，逐字节保留）· 后缀（收尾散文）· 闭栏行。
- 一级 strict / 二级 repaired（六种 repair 逐条记录）/ 三级（候选块数 ≠ 1 或不可解析）。
- 严格发布文法：第 1 行 opener、原样 JSON 值区段、闭栏、空行、叙述正文。
- 直接接口 wrapper：逐成员 raw_decode 取 `machine` 原始 span 与 `narrative` 解码串。
- 十四条机械校验（§6.5.2；第 14 条 = 未改动区不阻断，Amendment 4）；叙述正文机械恢复（§6.4 约定）与不变性核对（§6.5.4）。
校验器对任意 JSON 形状全封闭：自身异常折算 INVALID。本模块不导入其他契约模块。
"""
import json
import re

import review_channel_base as base
import review_channel_contract as C

_OPENER_RE = re.compile(r"^( {0,3})(`{3,5})[ \t]*(review-channel-verdict)[ \t]*$", re.IGNORECASE)
_LINE_END_RE = re.compile(r"\r\n|\r|\n")


def normalize_lines(text):
    """叙述正文的物理行规范化（R13-B1 整改）：CRLF / 孤立 CR / LF 一律折算为 LF——只用于叙述正文的剥离、校验与发布。
    机读块的 JSON 值区段恒取原消息字节（R14-B1：CR / CRLF 是 JSON 字符串外的合法空白，不得改写），故提取阶段不做整消息折算。"""
    return _LINE_END_RE.sub("\n", text)


def split_lines(text):
    """按 CRLF / CR / LF 三种终止符切分原文，返回 (lines, offsets)：lines 不含终止符，offsets 为各行在原文中的起点。
    提取只用它定位行，而 JSON 值区段仍按原文绝对偏移切取（字节逐一保留）。"""
    lines, offsets, pos = [], [], 0
    for m in _LINE_END_RE.finditer(text):
        lines.append(text[pos:m.start()])
        offsets.append(pos)
        pos = m.end()
    lines.append(text[pos:])
    offsets.append(pos)
    return lines, offsets


_CLOSER_RE = re.compile(r"^(`{3})[ \t]*$")   # 闭栏须行首、恰三反引号（§6.5.3 严格形态；R10-B2 整改：二级只认六种 repair）
_DECODER = json.JSONDecoder()


# ---------------------------------------------------------------- 五段分区

def partition(text):
    """返回 (candidates, narrative)。candidate = {opener_line, indent, fence_len, info, prefix, json_bytes,
    json_start, json_end, suffix, closer_present, parseable, reason, leading_text}。"""
    lines, offsets = split_lines(text)
    candidates = []
    narrative_parts = []
    i = 0
    n = len(lines)
    seen_nonblank_outside = False
    while i < n:
        line = lines[i]
        m = _OPENER_RE.match(line)
        if not m:
            narrative_parts.append(line)
            if line.strip():
                seen_nonblank_outside = True
            i += 1
            continue
        cand = {"opener_line": i, "indent": len(m.group(1)), "fence_len": len(m.group(2)), "info": m.group(3),
                "prefix": "", "json_bytes": None, "json_start": None, "json_end": None, "suffix": "",
                "closer_present": False, "parseable": False, "reason": None,
                # 一级要求块位于消息开头：opener 之前存在任何物理行（含空行）即已错位（R5-N1）
                "leading_text": seen_nonblank_outside or i > 0}
        body_start = offsets[i + 1] if i + 1 < n else len(text)
        brace = text.find("{", body_start)
        # 不可解析候选块的边界：opener 之后第一个闭栏行（无闭栏则至文件末尾）；其后各行继续进入叙述正文
        next_closer = None
        for k in range(i + 1, n):
            if _CLOSER_RE.match(lines[k]):
                next_closer = k
                break
        if brace < 0 or (next_closer is not None and brace > offsets[next_closer]):
            cand["reason"] = "no JSON object start"
            cand["prefix"] = text[body_start:offsets[next_closer] if next_closer is not None else len(text)]
            candidates.append(cand)
            if next_closer is None:
                break
            i = next_closer + 1
            continue
        cand["prefix"] = text[body_start:brace]
        if cand["prefix"].strip():
            cand["reason"] = "prefix contains non-whitespace bytes"
        try:
            _value, end = _DECODER.raw_decode(text, brace)
        except ValueError as exc:
            cand["reason"] = cand["reason"] or "raw_decode failed: %s" % exc
            candidates.append(cand)
            if next_closer is None:
                break
            i = next_closer + 1
            continue
        cand["json_start"] = brace
        cand["json_end"] = end
        cand["json_bytes"] = text[brace:end]
        # 值终点之后第一个同字符、行首、无尾随文本的围栏行为闭栏
        j = 0
        while j < n and offsets[j] + len(lines[j]) < end:
            j += 1
        # j = 值终点所在行；闭栏须在其后（若值终点恰在行尾且该行剩余为空，从下一行起找）
        after_end_in_line = lines[j][end - offsets[j]:] if j < n else ""
        suffix_parts = [after_end_in_line]
        k = j + 1
        closer_idx = None
        while k < n:
            cm = _CLOSER_RE.match(lines[k])
            if cm:
                closer_idx = k
                break
            suffix_parts.append(lines[k])
            k += 1
        cand["suffix"] = "\n".join(suffix_parts)
        cand["closer_present"] = closer_idx is not None
        cand["parseable"] = cand["reason"] is None
        candidates.append(cand)
        if cand["suffix"].strip():
            narrative_parts.append(cand["suffix"].strip("\n"))
        if closer_idx is None:
            break
        i = closer_idx + 1
    narrative = "\n".join(narrative_parts)
    return candidates, narrative


def classify(text):
    """返回 extraction dict：{tier, repairs, json_bytes, narrative, raw_block, reason, candidates}。
    tier ∈ strict | repaired | invalid（三级由调用方决定是否补发）。"""
    candidates, narrative = partition(text)
    # 候选块之外开头的纯空行属信封间隔（严格文法「闭栏后一个空行」），不是叙述正文
    while narrative.startswith("\n") or narrative.startswith("\r\n"):
        narrative = narrative.lstrip("\r\n")
    result = {"tier": "invalid", "repairs": [], "json_bytes": None, "narrative": narrative, "raw_block": None,
              "reason": None, "candidates": len(candidates)}
    if len(candidates) != 1:
        result["reason"] = "candidate blocks: %d" % len(candidates)
        return result
    c = candidates[0]
    if not c["parseable"]:
        result["reason"] = c["reason"]
        return result
    repairs = []
    if not c["closer_present"]:
        repairs.append("missing-closing-fence")
    if c["indent"] > 0:
        repairs.append("opener-indent")
    if c["fence_len"] != 3:
        repairs.append("fence-length")
    if c["info"] != C.FENCE_INFO:
        repairs.append("info-string-case")
    if c["suffix"].strip():
        repairs.append("trailing-content")
    if c["leading_text"]:
        repairs.append("block-relocated")
    result["repairs"] = repairs
    result["json_bytes"] = c["json_bytes"]
    _lines, offsets = split_lines(text)
    pos_open = offsets[c["opener_line"]]
    # 原候选块字节 = opener 行首至闭栏行末（闭栏缺失时至 JSON 值区段终点）
    if c["closer_present"]:
        result["raw_block"] = text[pos_open:_closer_end(text, c["json_end"])]
    else:
        result["raw_block"] = text[pos_open:c["json_end"]]
    result["tier"] = "strict" if not repairs else "repaired"
    return result


def _closer_end(text, json_end):
    """值终点之后第一个闭栏行的行末（含其终止符之前）；三种行终止符同样处理。"""
    lines, offsets = split_lines(text)
    j = 0
    while j < len(lines) and offsets[j] + len(lines[j]) < json_end:
        j += 1
    for k in range(j + 1, len(lines)):
        if _CLOSER_RE.match(lines[k]):
            return offsets[k] + len(lines[k])
    return len(text)


def render_published(json_bytes, narrative):
    """严格发布文法（§6.5.3）。json_bytes = 原样 JSON 值区段（str）。叙述正文只剥去纯空行的首尾。"""
    body = normalize_lines(narrative)
    while body.startswith("\n"):
        body = body.lstrip("\n")
    body = body.rstrip("\n")
    doc = "```%s\n%s\n```\n\n%s" % (C.FENCE_INFO, json_bytes, body)
    if not doc.endswith("\n"):
        doc += "\n"
    return doc.encode("utf-8")


def parse_published(data):
    """读端：严格发布文法解析。返回 (machine_obj, json_bytes, narrative) 或抛 ValueError。"""
    text = data.decode("utf-8")
    opener = "```%s\n" % C.FENCE_INFO
    if not text.startswith(opener):
        raise ValueError("line 1 is not the strict opener")
    start = len(opener)
    if start >= len(text) or text[start] != "{":
        raise ValueError("JSON value segment must start immediately after the opener")
    obj, end = _DECODER.raw_decode(text, start)
    rest = text[end:]
    if not rest.startswith("\n```\n"):
        raise ValueError("closing fence must follow the JSON value segment")
    narrative = rest[len("\n```\n"):]
    if narrative and not narrative.startswith("\n"):
        raise ValueError("a blank line must separate the block from the narrative")
    if "```%s" % C.FENCE_INFO in narrative:
        raise ValueError("exactly one verdict block is allowed")
    return obj, text[start:end], narrative.lstrip("\n")


def extract_wrapper(text):
    """直接接口 wrapper {machine, narrative} 的原始成员 span 提取（§6.5.3）。
    返回 (machine_json_bytes, narrative_str) 或 (None, reason)。"""
    i = 0
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    if i >= n or text[i] != "{":
        return None, "wrapper top level is not an object"
    i += 1
    machine = None
    narrative = None
    seen = set()
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i < n and text[i] == "}":
            break
        if i >= n or text[i] != '"':
            return None, "wrapper member key expected"
        try:
            key, i = _DECODER.raw_decode(text, i)
        except ValueError as exc:
            return None, "wrapper key raw_decode failed: %s" % exc
        while i < n and text[i].isspace():
            i += 1
        if i >= n or text[i] != ":":
            return None, "wrapper ':' expected"
        i += 1
        while i < n and text[i].isspace():
            i += 1
        try:
            value, end = _DECODER.raw_decode(text, i)
        except ValueError as exc:
            return None, "wrapper value raw_decode failed: %s" % exc
        if key in seen:
            return None, "wrapper member %r duplicated" % key
        seen.add(key)
        if key == "machine":
            machine = text[i:end]
        elif key == "narrative":
            narrative = value
        i = end
        while i < n and text[i].isspace():
            i += 1
        if i < n and text[i] == ",":
            i += 1
            continue
        if i < n and text[i] == "}":
            break
        return None, "wrapper ',' or '}' expected"
    if machine is None:
        return None, "wrapper lacks machine"
    if not isinstance(narrative, str):
        return None, "wrapper narrative must be a string"
    if seen != {"machine", "narrative"}:
        return None, "wrapper must contain exactly the members machine and narrative"
    i += 1
    if text[i:].strip():
        return None, "wrapper has trailing bytes after the top-level object"
    if not machine.startswith("{"):
        return None, "machine must be an object"
    return machine, narrative


# ---------------------------------------------------------------- 叙述正文

def _fence_opener(line):
    """代码围栏起始行（CommonMark）：反引号围栏的 info string 不得含反引号——否则不是围栏（R12-B2）。"""
    m = C.FENCE_LINE_RE.match(line)
    if m and m.group(2)[0] == "`" and "`" in m.group(3):
        return None
    return m


def strip_fences(body):
    """剥离**闭合的**代码围栏（含围栏行）；未闭合的围栏不是围栏——其后各行仍是正文（R11-B2：豁免只给闭合围栏内的 HTML）。"""
    lines = normalize_lines(body).split("\n")
    closed = set()
    i = 0
    while i < len(lines):
        m = _fence_opener(lines[i])
        if not m:
            i += 1
            continue
        open_char, open_len = m.group(2)[0], len(m.group(2))
        j = i + 1
        while j < len(lines):
            mm = C.FENCE_LINE_RE.match(lines[j])
            if mm and mm.group(2)[0] == open_char and len(mm.group(2)) >= open_len and not mm.group(3).strip():
                closed.update(range(i, j + 1))
                break
            j += 1
        i = j + 1 if j < len(lines) else i + 1
    return [ln for k, ln in enumerate(lines) if k not in closed]


def raw_html_lines(narrative):
    return [ln for ln in strip_fences(narrative) if C.RAW_HTML_RE.match(ln)]


def recover_narrative(narrative):
    """按 §6.4 约定机械恢复。返回 (recovered, problems)。"""
    # R9-B3 整改：第 1 / 2 行按发布正文的物理行核对（围栏剥离之前），围栏块不得先于 Verdict 行
    narrative = normalize_lines(narrative)
    lines = narrative.split("\n")
    problems = []
    rec = {"verdict": None, "human_decision_required": None, "findings": {}, "questions": {}, "previous": {},
           "has_previous_section": False, "has_question_section": False}
    # R2-B6 / R3-B6 整改：Verdict 行与 Human decision 行是物理第 1、2 行，不容前导空行、也不容两行之间的空白行
    if lines and not lines[0].strip():
        problems.append("narrative must start with the Verdict line (no leading blank lines)")
    if len(lines) >= 1 and lines[0].strip():
        m = C.NARRATIVE_VERDICT_RE.match(lines[0].strip())
        if m:
            rec["verdict"] = m.group(1)
        else:
            problems.append("narrative line 1 must be **Verdict: PASS|FAIL**")
    elif not lines or not any(ln.strip() for ln in lines):
        problems.append("narrative is empty")
    if len(lines) >= 2:
        m = C.NARRATIVE_HUMAN_RE.match(lines[1].strip())
        if m:
            rec["human_decision_required"] = m.group(1) == "yes"
        else:
            problems.append("narrative line 2 must be **Human decision required: yes|no** (physical line 2)")
    else:
        problems.append("narrative lacks the Human decision line")
    lines = strip_fences(narrative)   # 节与行的恢复只看围栏之外
    section = None
    for ln in lines:
        s = ln.rstrip()
        m = C.NARRATIVE_FINDING_HEADING_RE.match(s)
        if m:
            fid = m.group(1)
            if fid in rec["findings"]:
                problems.append("finding heading %s duplicated" % fid)
            rec["findings"][fid] = (m.group(2), m.group(3))
            section = None
            continue
        if s.strip() == C.NARRATIVE_QUESTIONS_HEADING:
            section = "questions"
            if rec["has_question_section"]:
                problems.append("narrative section '%s' appears more than once" % C.NARRATIVE_QUESTIONS_HEADING)  # R4-B3
            rec["has_question_section"] = True
            continue
        if s.strip() == C.NARRATIVE_PREVIOUS_HEADING:
            section = "previous"
            if rec["has_previous_section"]:
                problems.append("narrative section '%s' appears more than once" % C.NARRATIVE_PREVIOUS_HEADING)  # R4-B3
            rec["has_previous_section"] = True
            continue
        if s.startswith("#"):
            section = None
            continue
        if section == "questions":
            m = C.NARRATIVE_QUESTION_LINE_RE.match(s.strip())
            if m:
                if m.group(1) in rec["questions"]:
                    problems.append("question line %s duplicated" % m.group(1))
                rec["questions"][m.group(1)] = m.group(2)
        elif section == "previous":
            m = C.NARRATIVE_PREVIOUS_LINE_RE.match(s.strip())
            if m:
                if m.group(1) in rec["previous"]:
                    problems.append("previous finding line %s duplicated" % m.group(1))
                rec["previous"][m.group(1)] = m.group(2)
    if not rec["has_question_section"]:
        problems.append("narrative lacks the '## Question assessments' section")
    return rec, problems


def compare_recovered(rec, block, previous_required):
    """比对叙述恢复值与机读块；返回不等字段名列表（可恢复字段闭集 §6.5.4）。"""
    mismatched = []
    if rec["verdict"] != block.get("verdict"):
        mismatched.append("verdict")
    if rec["human_decision_required"] != block.get("human_decision_required"):
        mismatched.append("human_decision_required")
    findings = block.get("findings") if isinstance(block.get("findings"), list) else []
    bf = {}
    ok = True
    for f in findings:
        if not isinstance(f, dict) or not isinstance(f.get("id"), str):
            ok = False
            continue
        bf[f["id"]] = (f.get("severity"), f.get("title"))
    if not ok or bf != rec["findings"]:
        mismatched.append("findings")
    qa = block.get("question_assessments") if isinstance(block.get("question_assessments"), list) else []
    bq = {}
    ok = True
    for q in qa:
        if not isinstance(q, dict) or not isinstance(q.get("question_id"), str):
            ok = False
            continue
        bq[q["question_id"]] = q.get("assessment")
    if not ok or bq != rec["questions"]:
        mismatched.append("question_assessments")
    if previous_required:
        pd = block.get("previous_findings_disposition") if isinstance(block.get("previous_findings_disposition"), list) else []
        bp = {}
        ok = True
        for p in pd:
            if not isinstance(p, dict) or not isinstance(p.get("id"), str):
                ok = False
                continue
            bp[p["id"]] = p.get("disposition")
        if not ok or bp != rec["previous"] or not rec["has_previous_section"]:
            mismatched.append("previous_findings_disposition")
    return mismatched


# ---------------------------------------------------------------- 十四条校验

def validate_machine(block, ctx):
    """ctx: subject, stage, round, candidate {bundle_name, sha256}, candidates [{bundle_name, sha256}]（缺席取 [candidate]），
    required_question_ids, previous_finding_ids (None | list), residual_ids, bundle_names, task_file,
    delivery, narrative, secret_hit (bool)。返回 problems 列表（空 = VALID）。全封闭：任何异常折算为单条 problem。"""
    return [m for _c, m in validate_machine_classified(block, ctx)]


def validate_machine_classified(block, ctx):
    """同 validate_machine，但返回 [(class, message)]，class ∈ C.PROBLEM_CLASSES（§6.5.2 末段：known = 已知值缺陷、
    narrative = 叙述缺陷、other = 其余）；§6.5.4 校验类补发的触发判据据此取值（Amendment 2）。"""
    try:
        return _validate(block, ctx)
    except Exception as exc:  # noqa: BLE001 - 全封闭兜底（规则 12）
        return [("other", "validator internal error folded to INVALID: %s" % type(exc).__name__)]


def problem_classes(classified):
    return sorted({c for c, _m in classified})


def deterministically_remediable(classified):
    """§6.5.4 校验类触发判据：失败项非空且全部属已知值缺陷或叙述缺陷。"""
    return bool(classified) and all(c in ("known", "narrative") for c, _m in classified)


def judgment_mismatches(original, reemitted):
    """校验类补发的不变性核对（§6.5.4）：判断字段逐字段经 canonical JSON 编码后逐字节相等；返回不等字段名列表。"""
    out = []
    o = original if isinstance(original, dict) else {}
    r = reemitted if isinstance(reemitted, dict) else {}
    for f in C.JUDGMENT_FIELDS:
        if base.canonical_json(o.get(f)) != base.canonical_json(r.get(f)):
            out.append(f)
    return out


def known_value_literals(ctx):
    """校验类补发指令的已知值字段正确取值清单（§6.5.4）：[(字段名, JSON 字面)]。"""
    cands = ctx.get("candidates") or [ctx["candidate"]]
    return [("verdict_schema", json.dumps(C.VERDICT_SCHEMA)),
            ("subject", json.dumps(ctx["subject"])), ("stage", json.dumps(ctx["stage"])), ("round", json.dumps(ctx["round"])),
            ("candidate", json.dumps({"bundle_name": cands[0]["bundle_name"], "sha256": cands[0]["sha256"]})),
            ("candidates", json.dumps([{"bundle_name": c["bundle_name"], "sha256": c["sha256"]} for c in cands])),
            ("accepted_residuals_acknowledged", json.dumps(list(ctx["residual_ids"]))),
            ("authorization_disclaimer", "true")]


def _validate(block, ctx):
    p = []
    known_fields = set(C.KNOWN_VALUE_FIELDS)

    def add(cls, msg):
        p.append((cls, msg))

    if not isinstance(block, dict):
        return [("other", "machine block must be a JSON object")]
    for k in block:
        if k not in C.VERDICT_FIELDS:
            add("other", "unknown field %r" % k)
    for k in C.VERDICT_FIELDS:
        if k not in block:
            add("known" if k in known_fields else "other", "field %r missing" % k)
    if block.get("verdict_schema") != C.VERDICT_SCHEMA:
        add("known", "verdict_schema must be %s" % C.VERDICT_SCHEMA)
    verdict = block.get("verdict")
    if verdict not in C.VERDICTS:
        add("other", "verdict must be PASS or FAIL")
    for k in ("subject", "stage", "round"):
        if block.get(k) != ctx[k]:
            add("known", "%s must equal the request value" % k)
    round_tag = ctx["round"].upper()
    allowed_files = list(ctx["bundle_names"])

    # findings
    findings = block.get("findings")
    if not isinstance(findings, list):
        add("other", "findings must be an array")
        findings = []
    ids = []
    sev_of = {}
    counters = {"B": [], "N": [], "H": []}
    for i, f in enumerate(findings):
        w = "findings[%d]" % i
        if not isinstance(f, dict):
            add("other", "%s must be an object" % w)
            continue
        for k in C.FINDING_FIELDS:
            if k not in f:
                add("other", "%s.%s missing" % (w, k))
        for k in f:
            if k not in C.FINDING_FIELDS:
                add("other", "%s.%s unknown field" % (w, k))
        fid = f.get("id")
        sev = f.get("severity")
        if not isinstance(fid, str) or not C.FINDING_ID_RE.match(fid):
            add("other", "%s.id grammar" % w)
            continue
        m = C.FINDING_ID_RE.match(fid)
        if m.group(1) != round_tag:
            add("other", "%s.id round prefix must be %s" % (w, round_tag))
        if sev not in C.SEVERITIES:
            add("other", "%s.severity outside the closed set" % w)
        elif C.SEVERITY_LETTER[sev] != m.group(2):
            add("other", "%s.id letter does not match severity" % w)
        if fid in ids:
            add("other", "finding id %s duplicated" % fid)
        ids.append(fid)
        sev_of[fid] = sev
        counters[m.group(2)].append(int(m.group(3)))
        if not isinstance(f.get("title"), str) or not f.get("title"):
            add("other", "%s.title must be a non-empty string" % w)
        loc = f.get("location")
        if not isinstance(loc, dict) or set(loc.keys()) != {"file", "anchor"} or not isinstance(loc["file"], str) \
                or not isinstance(loc["anchor"], str) or not loc["anchor"]:
            add("other", "%s.location must be {file: string, anchor: non-empty string}" % w)
        elif loc["file"] not in allowed_files:
            add("other", "%s.location.file not a delivered bundle name" % w)
        if f.get("origin") not in C.ORIGINS:
            add("other", "%s.origin outside the closed set" % w)
        elif f["origin"] in C.NON_BLOCKING_ORIGINS and sev == "blocking":
            # 规则 14（Amendment 4；Owner 裁定 B / F4 甲）：未改动区的 finding 不得阻断；判断字段缺陷、不补发、原样重投不占额度
            add("other", "rule-14:%s: origin %s cannot be blocking (severity must be non_blocking or human)" % (fid, f["origin"]))
        if not isinstance(f.get("summary"), str):
            add("other", "%s.summary must be a string" % w)
    for letter, nums in counters.items():
        if nums and sorted(nums) != list(range(1, len(nums) + 1)):
            add("other", "%s findings must be numbered consecutively from 1" % letter)
    blocking = [i for i in ids if sev_of.get(i) == "blocking"]
    humans = [i for i in ids if sev_of.get(i) == "human"]
    if verdict == "PASS" and blocking:
        add("other", "PASS with blocking findings")
    if verdict == "FAIL" and not blocking:
        add("other", "FAIL requires at least one blocking finding")
    hdr = block.get("human_decision_required")
    if not isinstance(hdr, bool):
        add("other", "human_decision_required must be a boolean")
    elif hdr != bool(humans):
        add("other", "human_decision_required must equal the existence of human findings")

    # question assessments
    qa = block.get("question_assessments")
    if not isinstance(qa, list):
        add("other", "question_assessments must be an array")
        qa = []
    answered = []
    referenced = set()
    for i, q in enumerate(qa):
        w = "question_assessments[%d]" % i
        if not isinstance(q, dict):
            add("other", "%s must be an object" % w)
            continue
        for k in C.ASSESSMENT_FIELDS:
            if k not in q:
                add("other", "%s.%s missing" % (w, k))
        for k in q:
            if k not in C.ASSESSMENT_FIELDS:
                add("other", "%s.%s unknown field" % (w, k))
        qid = q.get("question_id")
        if not isinstance(qid, str):
            add("other", "%s.question_id must be a string" % w)
            continue
        answered.append(qid)
        a = q.get("assessment")
        if a not in C.ASSESSMENTS:
            add("other", "%s.assessment outside the closed set" % w)
        if not isinstance(q.get("evidence_refs"), list) or not all(isinstance(x, str) for x in q.get("evidence_refs") or []):
            add("other", "%s.evidence_refs must be an array of strings" % w)
        fids = q.get("finding_ids")
        if not isinstance(fids, list):
            add("other", "%s.finding_ids must be an array" % w)
            fids = []
        for fid in fids:
            if not isinstance(fid, str) or fid not in ids:
                add("other", "%s references undeclared finding %r" % (w, fid))
            else:
                referenced.add(fid)
        if a in ("NOT_SATISFIED", "INDETERMINATE") and not fids:
            add("other", "%s: non-SATISFIED assessment must reference a finding" % w)
        if verdict == "PASS":
            if a == "NOT_SATISFIED":
                add("other", "PASS requires no NOT_SATISFIED question (%s)" % qid)
            elif a == "INDETERMINATE" and any(sev_of.get(fid) != "human" for fid in fids if isinstance(fid, str)):
                add("other", "PASS with INDETERMINATE %s may only reference human findings" % qid)
    required = list(ctx["required_question_ids"])
    if sorted(answered) != sorted(required):
        # 规则 2（Amendment 4）：题号多重集 = 该 stage 五题闭集；缺题 / 多题 / 重复属判断字段缺陷（other）、不补发
        add("other", "question id multiset must equal the stage's five-question closed set exactly (required %s, answered %s)"
                 % (sorted(required), sorted(answered)))
    orphans = sorted(set(ids) - referenced)
    if orphans:
        add("other", "findings not referenced by any question: %s" % orphans)

    # previous findings disposition
    pd = block.get("previous_findings_disposition")
    if not isinstance(pd, list):
        add("other", "previous_findings_disposition must be an array")
        pd = []
    prev_required = ctx["previous_finding_ids"]
    if prev_required is None:
        if pd:
            add("other", "previous_findings_disposition must be empty in r1")
    else:
        got = []
        for i, d in enumerate(pd):
            w = "previous_findings_disposition[%d]" % i
            if not isinstance(d, dict) or set(d.keys()) != set(C.DISPOSITION_FIELDS):
                add("other", "%s must be {id, disposition, evidence_refs}" % w)
                continue
            if not isinstance(d["id"], str):
                add("other", "%s.id must be a string" % w)
                continue
            got.append(d["id"])
            if d["disposition"] not in C.DISPOSITIONS:
                add("other", "%s.disposition outside the closed set" % w)
            if not isinstance(d["evidence_refs"], list) or not all(isinstance(x, str) for x in d["evidence_refs"]):
                add("other", "%s.evidence_refs must be an array of strings" % w)
        if sorted(got) != sorted(prev_required):
            add("other", "previous_findings_disposition ids must equal the previous round finding set exactly")

    # residuals
    ack = block.get("accepted_residuals_acknowledged")
    if not isinstance(ack, list) or not all(isinstance(x, str) for x in ack):
        add("known", "accepted_residuals_acknowledged must be a string array")
    elif sorted(ack) != sorted(ctx["residual_ids"]) or len(set(ack)) != len(ack):
        add("known", "accepted_residuals_acknowledged must equal the request residual id set exactly")

    # candidate binding（规则 8，Amendment 2：candidate = 首候选；candidates = 清单全部候选的精确等集，candidates[0] = candidate）
    cands_ctx = ctx.get("candidates") or [ctx["candidate"]]
    first = cands_ctx[0]
    cand = block.get("candidate")
    if not isinstance(cand, dict) or set(cand.keys()) != {"bundle_name", "sha256"}:
        add("known", "candidate must be {bundle_name, sha256}")
    else:
        if cand["sha256"] != first["sha256"]:
            add("known", "candidate.sha256 must equal the manifest first-candidate SHA-256")
        if cand["bundle_name"] != first["bundle_name"]:
            add("known", "candidate.bundle_name must equal the manifest first-candidate bundle name")
    cl = block.get("candidates")
    if not isinstance(cl, list) or not all(isinstance(c, dict) and set(c.keys()) == {"bundle_name", "sha256"} for c in cl):
        add("known", "candidates must be an array of {bundle_name, sha256}")
    else:
        got = [(c["bundle_name"], c["sha256"]) for c in cl if isinstance(c["bundle_name"], str) and isinstance(c["sha256"], str)]
        want = [(c["bundle_name"], c["sha256"]) for c in cands_ctx]
        # R32-B2 整改：按清单候选次序逐项相等（同成员、同次序），不只核首项
        if got != want or len(got) != len(cl):
            add("known", "candidates must equal the manifest candidate list exactly (same members, same order)")
        # candidates[0] = candidate 由上两项（candidate = 清单首候选、candidates 与清单同序相等）派生，不另核

    # scope / tools / disclaimer
    scope = block.get("scope_files_read")
    if not isinstance(scope, list) or not scope or not all(isinstance(s, str) for s in scope):
        add("other", "scope_files_read must be a non-empty string array")
    else:
        outside = [s for s in scope if s not in allowed_files]
        if outside:
            add("other", "scope_files_read names files outside the delivered set: %s" % outside)
        if ctx["task_file"] not in scope:
            add("other", "scope_files_read must include the task file")
    tools = block.get("tools_used")
    if not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools):
        add("other", "tools_used must be a string array")
    elif ctx["delivery"] == "inline" and tools:
        add("other", "tools_used must be empty under inline delivery")
    elif ctx["delivery"] == "tool" and not tools:
        add("other", "tools_used must be non-empty under the tool delivery path")
    if block.get("authorization_disclaimer") is not True:
        add("known", "authorization_disclaimer must be true")

    # secrets
    if ctx.get("secret_hit"):
        add("other", "secret material detected in reviewer output")

    # narrative (rule 5 headings, rule 13 recovery, raw HTML)
    narrative = normalize_lines(ctx["narrative"])
    html = raw_html_lines(narrative)
    if html:
        add("narrative", "raw HTML line in narrative: %r" % html[0][:40])
    if any(_OPENER_RE.match(ln) for ln in narrative.split("\n")):
        add("narrative", "narrative must not contain a review-channel-verdict fence block (exactly one block per document)")   # R11-B3
    rec, rec_problems = recover_narrative(narrative)
    for x in rec_problems:
        add("narrative", "narrative: %s" % x)
    for fid in ids:
        if fid not in rec["findings"]:
            add("narrative", "finding %s has no heading-level section in the narrative" % fid)
    for field in compare_recovered(rec, block, prev_required is not None):
        add("narrative", "narrative/machine mismatch: %s" % field)
    return p
