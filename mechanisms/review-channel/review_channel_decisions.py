"""review_channel_decisions — 决定文件（评审通道设计 §6.12，Amendment 4）。

Owner 对一轮判词逐条 finding 的处置决定的机读承载：`decisions.json`（`review-channel-decisions/v1`），落
`attempt_round_dir/decisions.json`（轮级、不属任一 attempt；本地暂存层，永不入 Git）。本模块承载：
- 决定输入与决定文件的校验（`respond` 与 r<N+1> preflight 共用同一组规则，任一不成立即 `decisions-invalid`）；
- 原子写入（临时文件 + 同目录 rename，已存在即整体覆盖）；
- r<N+1> 请求件派生段（处置段、派生残留项）的渲染与核对（不满足即 `decisions-mismatch`）。
本模块不导入其他契约模块。
"""
import os

import review_channel_base as base
import review_channel_contract as C


# ---------------------------------------------------------------- 落点

def decisions_path(repo_root, attempt_round_dir):
    return os.path.join(repo_root, attempt_round_dir, C.DECISIONS_FILE)


# ---------------------------------------------------------------- 校验（§6.12 五条规则）

def decision_problems(decisions, findings):
    """规则其一至其四：decisions[] 对判词 findings[]（[{id, severity, title}]）的逐条核对。返回 problems（空 = 通过）。
    对任意 JSON 形状全封闭：形状不符即问题项，不抛出。"""
    p = []
    if not isinstance(decisions, list):
        return ["decisions must be an array"]
    want = [f["id"] for f in findings]
    sev = {f["id"]: f["severity"] for f in findings}
    got = []
    for i, d in enumerate(decisions):
        w = "decisions[%d]" % i
        if not isinstance(d, dict):
            p.append("%s must be an object" % w)
            continue
        for k in d:
            if k not in C.DECISION_FIELDS:
                p.append("%s.%s unknown member" % (w, k))
        for k in ("finding_id", "action", "owner_verbatim"):
            if k not in d:
                p.append("%s.%s missing" % (w, k))
        fid = d.get("finding_id")
        if not base.is_nonempty_str(fid):
            p.append("%s.finding_id must be a non-empty string" % w)
            continue
        got.append(fid)
        if fid not in sev:
            p.append("rule 1: %s.finding_id %r is not a finding of the verdict" % (w, fid))
        action = d.get("action")
        if action not in C.DECISION_ACTIONS:
            p.append("rule 2: %s.action must be one of %s" % (w, " | ".join(C.DECISION_ACTIONS)))
        if "instructions" in d and not isinstance(d["instructions"], str):
            p.append("%s.instructions must be a string" % w)
        if action == "fix" and not base.is_nonempty_str(d.get("instructions")):
            p.append("rule 2: %s action fix requires non-empty instructions" % w)
        if "work_item" in d:
            if action != "skip":
                p.append("%s.work_item is only meaningful for action skip" % w)
            elif not (isinstance(d["work_item"], str) and C.WORK_ITEM_RE.match(d["work_item"])):
                p.append("rule 3: %s.work_item must match the ledger item id grammar" % w)
        if action == "skip" and sev.get(fid) == "blocking" and "work_item" not in d:
            p.append("rule 3: %s skips blocking finding %s without a work_item" % (w, fid))
        if not base.is_nonempty_str(d.get("owner_verbatim")):
            p.append("rule 4: %s.owner_verbatim must be a non-empty string" % w)
    if len(set(got)) != len(got):
        p.append("rule 1: duplicate finding_id in decisions")
    if sorted(set(got)) != sorted(want):
        p.append("rule 1: decisions finding_id set %s must equal the verdict finding set %s" % (sorted(set(got)), sorted(want)))
    return p


def input_problems(obj):
    """`respond` 输入（决定文件候选）的形状：subject / stage / round / task_record 与 decisions[] 在场；
    verdict_sha256 与 decided_at 由通道填写、在场即拒；decisions_schema 可选、在场须等于常量。"""
    if not isinstance(obj, dict):
        return ["decisions input must be an object"]
    p = []
    allowed = {"decisions_schema", "subject", "stage", "round", "task_record", "decisions"}
    for k in obj:
        if k in ("verdict_sha256", "decided_at"):
            p.append("%s is written by the channel; it must not be present in the input" % k)
        elif k not in allowed:
            p.append("unknown member %r" % k)
    if "decisions_schema" in obj and obj["decisions_schema"] != C.DECISIONS_SCHEMA:
        p.append("decisions_schema must be %s" % C.DECISIONS_SCHEMA)
    if not (isinstance(obj.get("subject"), str) and C.SUBJECT_RE.match(obj["subject"])):
        p.append("subject missing or malformed")
    if obj.get("stage") not in C.STAGES:
        p.append("stage missing or outside the closed set")
    if not (isinstance(obj.get("round"), str) and C.ROUND_RE.match(obj["round"])):
        p.append("round missing or malformed")
    if "task_record" in obj and not (isinstance(obj["task_record"], str) and C.TASK_RECORD_RE.match(obj["task_record"])):
        p.append("task_record present but malformed")
    if not isinstance(obj.get("decisions"), list):
        p.append("decisions must be an array")
    return p


def document_problems(doc, findings, verdict_sha256, routing):
    """决定文件（已写入形态）的全部校验：封闭对象、身份与 routing 相等、规则其一至其四、规则其五（verdict_sha256 绑定）。"""
    if not isinstance(doc, dict):
        return ["decisions document must be an object"]
    p = []
    for k in doc:
        if k not in C.DECISIONS_FIELDS:
            p.append("unknown member %r" % k)
    for k in C.DECISIONS_FIELDS:
        if k not in doc:
            p.append("member %r missing" % k)
    if p:
        return p
    if doc["decisions_schema"] != C.DECISIONS_SCHEMA:
        p.append("decisions_schema must be %s" % C.DECISIONS_SCHEMA)
    for k in ("subject", "stage", "round", "task_record"):
        if doc[k] != routing[k]:
            p.append("%s must equal the disposed round's %s" % (k, k))
    if not base.is_nonempty_str(doc["decided_at"]):
        p.append("decided_at must be a non-empty string")
    if not base.hex64(doc["verdict_sha256"]):
        p.append("verdict_sha256 grammar")
    elif verdict_sha256 is not None and doc["verdict_sha256"] != verdict_sha256:
        p.append("rule 5: verdict_sha256 does not equal the published verdict bytes of the disposed round")
    p.extend(decision_problems(doc["decisions"], findings))
    return p


# ---------------------------------------------------------------- 建立与写入

def build_document(routing, verdict_sha256, decisions):
    return {"decisions_schema": C.DECISIONS_SCHEMA, "subject": routing["subject"], "stage": routing["stage"],
            "round": routing["round"], "task_record": routing["task_record"], "verdict_sha256": verdict_sha256,
            "decided_at": base.utc_now_iso(), "decisions": [dict(d) for d in decisions]}


def write_document(path, doc):
    """原子写入（临时文件 + 同目录 rename）；已存在即整体覆盖。返回写出的字节。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = base.pretty_json(doc)
    base.write_atomic_replace(path, data)
    return data


def load_document(path):
    """读取决定文件；返回 (doc, bytes)；不存在返回 (None, None)，不可严格解析抛 ValueError。"""
    if not os.path.isfile(path):
        return None, None
    data = base.read_bytes(path)
    return base.strict_json_load(data), data


# ---------------------------------------------------------------- 派生（§6.12 其三、其四）

def _ordered(decisions, findings):
    by_id = {d["finding_id"]: d for d in decisions}
    return [(f, by_id[f["id"]]) for f in findings if f["id"] in by_id]


def dispositions_segment(decisions, findings, decisions_sha256):
    """处置段：首行 `Dispositions (decisions.json sha256 <前 12 位>)`，随后每条 finding 一行 `- <finding_id>: <action>`，
    fix 项后接 ` · <instructions>`，skip 项在 work_item 在场时后接 ` · <work_item>`；行序 = 判词 findings[] 次序。"""
    lines = [C.DISPOSITIONS_HEADING % decisions_sha256[:12]]
    for f, d in _ordered(decisions, findings):
        line = "- %s: %s" % (f["id"], d["action"])
        if d["action"] == "fix":
            line += " · " + d["instructions"]
        elif d["action"] == "skip" and d.get("work_item"):
            line += " · " + d["work_item"]
        lines.append(line)
    return "\n".join(lines)


def derived_residuals(decisions, findings):
    """派生残留项：每个 skip 项各一条 {id: RES-<n>, text: <title> · <work_item>}（无 work_item 时 text = title），
    n 自 1 起按判词 findings[] 次序连续编号。"""
    out = []
    for f, d in _ordered(decisions, findings):
        if d["action"] != "skip":
            continue
        text = f["title"] + (" · " + d["work_item"] if d.get("work_item") else "")
        out.append({"id": "RES-%d" % (len(out) + 1), "text": text})
    return out


def request_mismatches(brief, decisions, findings, decisions_sha256):
    """r<N+1> 请求件与决定文件的核对：remediation_statement 须以处置段逐字起始；accepted_residuals 须逐字含全部派生
    残留项（id 与 text），caller 追加的残留项自其后续编。返回 problems（空 = 一致）。"""
    p = []
    segment = dispositions_segment(decisions, findings, decisions_sha256)
    rem = brief.get("remediation_statement") or ""
    if not rem.startswith(segment):
        p.append("remediation_statement must start with the channel-rendered dispositions segment")
    derived = derived_residuals(decisions, findings)
    got = [{"id": r["id"], "text": r["text"]} for r in brief.get("accepted_residuals", [])]
    if got[:len(derived)] != derived:
        p.append("accepted_residuals must begin with the derived residual items %s" % derived)
    return p


def summary_rows(decisions, findings):
    """任务书第 3 节决定摘要：[(finding_id, severity, title, action, instructions, work_item)]，按判词 findings[] 次序。"""
    return [(f["id"], f["severity"], f["title"], d["action"], d.get("instructions") or "", d.get("work_item") or "")
            for f, d in _ordered(decisions, findings)]
