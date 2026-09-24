"""review_channel_request — 评审请求校验（评审通道设计 §6.1；对任意 JSON 全封闭）。

两层：`minimal_route(raw)` 最小可路由解析（四路由字段 + task_record），失败即 UnrouteableError；
`validate_request(obj)` 完整校验，任一不符即 PreflightError（失败码在 contract.FAILURE_CODES 内）。
本模块不导入其他契约模块。
"""
import review_channel_base as base
import review_channel_contract as C

_ALLOWED_TOP = (
    "request_schema", "subject", "stage", "round", "caller", "task_record", "artifact_author",
    "profile", "effort", "runtime_overrides", "attempt_options", "inputs", "review_brief",
    "previous_round", "max_rounds", "round_extensions", "formal_review_authorized_by_owner",
    "invocation_authorization",
)   # Amendment 4：standard_questions 随五题闭集退役（在场即未知成员）


def _reject(detail):
    raise base.PreflightError("request-schema", detail)


def _str(obj, key, where, required=True, allow_empty=False):
    if key not in obj:
        if required:
            _reject("%s.%s missing" % (where, key))
        return None
    v = obj[key]
    if not isinstance(v, str) or (not allow_empty and v == ""):
        _reject("%s.%s must be a non-empty string" % (where, key))
    return v


def minimal_route(raw):
    """最小可路由解析：返回 routing dict {subject, stage, round, task_record, request}。
    JSON 不可解析 / 非对象 / 四路由字段任一缺失或不合文法 → UnrouteableError。"""
    try:
        obj = base.strict_json_load(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise base.UnrouteableError("request JSON unparseable: %s" % exc)
    if not isinstance(obj, dict):
        raise base.UnrouteableError("request top level is not an object")
    subject = obj.get("subject")
    stage = obj.get("stage")
    rnd = obj.get("round")
    if not (isinstance(subject, str) and C.SUBJECT_RE.match(subject)):
        raise base.UnrouteableError("subject missing or malformed")
    if stage not in C.STAGES:
        raise base.UnrouteableError("stage missing or outside the closed set")
    if not (isinstance(rnd, str) and C.ROUND_RE.match(rnd)):
        raise base.UnrouteableError("round missing or malformed")
    task_record = None
    if "task_record" in obj:
        task_record = obj["task_record"]
        if not (isinstance(task_record, str) and C.TASK_RECORD_RE.match(task_record)):
            raise base.UnrouteableError("task_record present but malformed")
    return {"subject": subject, "stage": stage, "round": rnd, "task_record": task_record, "request": obj}


def round_index(rnd):
    return int(rnd[1:])


def repo_relative_path(value):
    """仓内相对 POSIX 路径文法（R1-B1 整改）：非空、不以 '/' 起、无 '\\'、无空段、无 '.' / '..' 段、无控制字符。"""
    if not base.is_nonempty_str(value) or value.startswith("/") or "\\" in value:
        return False
    if any(ord(c) < 32 for c in value):
        return False
    segments = value.split("/")
    return all(seg not in ("", ".", "..") for seg in segments)


def _validate_authors(author):
    where = "artifact_author"
    if not isinstance(author, dict):
        _reject("%s must be an object" % where)
    for k in author:
        if k not in ("human_only", "authors"):
            _reject("%s.%s unknown member" % (where, k))
    human_only = author.get("human_only")
    if not isinstance(human_only, bool):
        _reject("%s.human_only must be a boolean" % where)
    authors = author.get("authors")
    if not isinstance(authors, list) or not authors:
        _reject("%s.authors must be a non-empty array" % where)
    vendors = []
    all_human = True
    for i, a in enumerate(authors):
        w = "%s.authors[%d]" % (where, i)
        if not isinstance(a, dict) or set(a.keys()) != {"tool", "model", "vendor"}:
            _reject("%s must be {tool, model, vendor}" % w)
        for k in ("tool", "model", "vendor"):
            if not base.is_nonempty_str(a[k]):
                _reject("%s.%s must be a non-empty string" % (w, k))
        if a["tool"] == C.HUMAN or a["vendor"] == C.HUMAN:
            if a != {"tool": C.HUMAN, "model": C.HUMAN, "vendor": C.HUMAN}:
                _reject("%s Human entry must be exactly {human, human, human}" % w)
            continue
        all_human = False
        if a["vendor"] not in C.VENDORS:
            raise base.PreflightError("vendor-not-registered",
                                      "%s.vendor %r not in the vendors closed set" % (w, a["vendor"]))
        vendors.append(a["vendor"])
    if human_only != all_human:
        _reject("%s.human_only must equal 'all authors are Human entries'" % where)
    return sorted(set(vendors))


def _validate_ids(items, where, pattern, kind):
    if not isinstance(items, list):
        _reject("%s must be an array" % where)
    seen = set()
    for i, it in enumerate(items):
        w = "%s[%d]" % (where, i)
        if not isinstance(it, dict) or set(it.keys()) != {"id", "text"}:
            _reject("%s must be {id, text}" % w)
        if not base.is_nonempty_str(it["id"]) or not pattern.match(it["id"]):
            _reject("%s.id must match %s grammar" % (w, kind))
        if it["id"] in seen:
            _reject("%s.id duplicate" % w)
        seen.add(it["id"])
        if not base.is_nonempty_str(it["text"]):
            _reject("%s.text must be a non-empty string" % w)
    return [it["id"] for it in items]


def _validate_brief(brief, rnd):
    where = "review_brief"
    if not isinstance(brief, dict):
        _reject("%s must be an object" % where)
    # Amendment 4：questions 退出（required question 集合 = 通道常量五题闭集，§6.4）；在场即未知成员
    keys = {"background", "check_surfaces", "evidence_limits", "accepted_residuals", "remediation_statement"}
    for k in brief:
        if k not in keys:
            _reject("%s.%s unknown member" % (where, k))
    _str(brief, "background", where)
    _str(brief, "check_surfaces", where)
    limits = brief.get("evidence_limits")
    if not isinstance(limits, list):
        _reject("%s.evidence_limits must be an array" % where)
    seen_refs = set()
    for i, lim in enumerate(limits):
        w = "%s.evidence_limits[%d]" % (where, i)
        if not isinstance(lim, dict) or not {"reference", "reason"} <= set(lim.keys()) \
                or not set(lim.keys()) <= {"reference", "reason", "kind"}:
            _reject("%s must be {reference, reason[, kind]}" % w)
        if not base.is_nonempty_str(lim["reference"]) or not base.is_nonempty_str(lim["reason"]):
            _reject("%s members must be non-empty strings" % w)
        # Amendment 1（R29-B2）：kind 可选、闭集 exemption | planned-location、缺省 exemption；表外值即请求文法失败
        if "kind" in lim and lim["kind"] not in C.EVIDENCE_LIMIT_KINDS:
            _reject("%s.kind must be one of %s" % (w, " | ".join(C.EVIDENCE_LIMIT_KINDS)))
        # Amendment 1（R30-B1）：reference 数组内唯一——同一引用出现多项，无论 kind 异同，即请求文法失败
        if lim["reference"] in seen_refs:
            _reject("%s.reference %r already declared in evidence_limits" % (w, lim["reference"]))
        seen_refs.add(lim["reference"])
    if "accepted_residuals" not in brief:
        _reject("%s.accepted_residuals missing" % where)
    rids = _validate_ids(brief["accepted_residuals"], where + ".accepted_residuals", C.RESIDUAL_ID_RE,
                         "residual id")
    rem = brief.get("remediation_statement")
    if round_index(rnd) >= 2:
        if not base.is_nonempty_str(rem):
            _reject("%s.remediation_statement is required and non-empty in r2+" % where)
    else:
        if "remediation_statement" in brief and rem not in (None, ""):
            _reject("%s.remediation_statement is forbidden in r1" % where)
    return rids


def validate_request(obj, mode):
    """完整校验。返回规范化 dict（不含秘密，不改原对象）。"""
    if not isinstance(obj, dict):
        _reject("top level must be an object")
    for k in obj:
        if k not in _ALLOWED_TOP:
            _reject("unknown top-level member %r" % k)
    if obj.get("request_schema") not in (C.REQUEST_SCHEMA, "review-channel-request/v5"):
        _reject("request_schema must be %s" % C.REQUEST_SCHEMA)
    subject = _str(obj, "subject", "request")
    if not C.SUBJECT_RE.match(subject):
        _reject("subject grammar")
    stage = obj.get("stage")
    if stage not in C.STAGES:
        _reject("stage outside the closed set")
    rnd = _str(obj, "round", "request")
    if not C.ROUND_RE.match(rnd):
        _reject("round grammar")
    caller = _str(obj, "caller", "request")
    if len(caller.encode("utf-8")) > 256:
        _reject("caller exceeds 256 UTF-8 bytes")
    task_record = None
    if "task_record" in obj:
        task_record = obj["task_record"]
        if not (isinstance(task_record, str) and C.TASK_RECORD_RE.match(task_record)):
            _reject("task_record grammar")
    if "artifact_author" not in obj:
        _reject("artifact_author missing")
    model_vendors = _validate_authors(obj["artifact_author"])

    profile = None
    if "profile" in obj:
        p = obj["profile"]
        if not isinstance(p, dict) or set(p.keys()) != {"provider", "model"}:
            _reject("profile must be {provider, model} (pair)")
        if not base.is_nonempty_str(p["provider"]) or not base.is_nonempty_str(p["model"]):
            _reject("profile members must be non-empty strings")
        profile = {"provider": p["provider"], "model": p["model"]}
    effort = None
    if "effort" in obj:
        effort = obj["effort"]
        if effort not in C.EFFORTS:
            _reject("effort outside the closed set")
    overrides = None   # None = 字段缺席（沉默 → 整套继承）；显式对象（含 {}）= 当轮显式覆盖（R5-B2 整改）
    if "runtime_overrides" in obj:
        ov = obj["runtime_overrides"]
        if not isinstance(ov, dict):
            _reject("runtime_overrides must be an object")
        for k, v in ov.items():
            if not base.is_nonempty_str(k):
                _reject("runtime_overrides keys must be non-empty strings")
            if not isinstance(v, (str, int, float, bool)):
                _reject("runtime_overrides values must be scalars")
        overrides = dict(ov)
    attempt_options = {}
    if "attempt_options" in obj:
        ao = obj["attempt_options"]
        if not isinstance(ao, dict):
            _reject("attempt_options must be an object")
        for k in ao:
            if k != "timeout_seconds":
                _reject("attempt_options.%s unknown member" % k)
        if "timeout_seconds" in ao:
            t = ao["timeout_seconds"]
            if not base.is_strict_int(t) or t <= 0:
                _reject("attempt_options.timeout_seconds must be a positive integer")
            attempt_options["timeout_seconds"] = t

    inputs = obj.get("inputs")
    if not isinstance(inputs, dict) or set(inputs.keys()) != {"candidates", "references"}:
        _reject("inputs must be {candidates, references}")
    cands = inputs["candidates"]
    # Amendment 2 多候选（§6.1）：candidates 一到多件、数组内唯一、与 references 不相交，次序即清单候选次序（首候选 = candidates[0]）
    if not isinstance(cands, list) or not cands or not all(repo_relative_path(c) for c in cands):
        _reject("inputs.candidates must be a non-empty array of repository-relative POSIX paths (no leading '/', no '..', no empty segment)")
    refs = inputs["references"]
    if not isinstance(refs, list) or not all(repo_relative_path(r) for r in refs):
        _reject("inputs.references must be an array of repository-relative POSIX paths")
    if len(set(cands)) != len(cands) or len(set(refs)) != len(refs) or set(cands) & set(refs):
        _reject("inputs paths must be distinct and no candidate may repeat as a reference")
    if task_record is not None and len(cands) != 1:
        # R33-B1 整改：任务轴 candidate role 恰一件（task-artifact-schema §10.2），多候选只适用于 subject 轴
        _reject("inputs.candidates must list exactly one candidate on the task axis (task-artifact-schema §10.2); multi-candidate requests are subject-axis only")

    if "review_brief" not in obj:
        _reject("review_brief missing")
    residual_ids = _validate_brief(obj["review_brief"], rnd)

    previous_round = None
    if round_index(rnd) >= 2:
        pr = obj.get("previous_round")
        if obj['request_schema']=='review-channel-request/v5':
            import review_evidence as E
            try:
                E.closed(pr, ('evidence','response'), 'previous_round'); E.reference(pr['evidence'])
                if pr['response'] is not None:
                    E.reference(pr['response']); E.require(pr['response']['kind']=='archive','response must be ArchiveRef')
            except E.EvidenceError as exc: _reject(str(exc))
            previous_round = dict(pr)
        elif not isinstance(pr, dict) or set(pr.keys()) != {"verdict_path", "receipt_path"}:
            _reject("previous_round {verdict_path, receipt_path} is required in r2+")
        if obj['request_schema']==C.REQUEST_SCHEMA:
            if not base.is_nonempty_str(pr["verdict_path"]) or not base.is_nonempty_str(pr["receipt_path"]):
                _reject("previous_round members must be non-empty strings")
            previous_round = {"verdict_path": pr["verdict_path"], "receipt_path": pr["receipt_path"]}
    elif "previous_round" in obj:
        # Amendment 1（R23-B2）：r1 携带 previous_round 即 request-field-not-allowed（与 remediation_statement 同款轮次约束）
        raise base.PreflightError("request-field-not-allowed", "previous_round is forbidden in r1")

    max_rounds = None
    if "max_rounds" in obj:
        max_rounds = obj["max_rounds"]
        if not base.is_strict_int(max_rounds) or max_rounds <= 0:
            _reject("max_rounds must be a positive integer")
    extensions = []
    if "round_extensions" in obj:
        ext = obj["round_extensions"]
        if not isinstance(ext, list):
            _reject("round_extensions must be an array")
        for i, e in enumerate(ext):
            w = "round_extensions[%d]" % i
            if not isinstance(e, dict) or set(e.keys()) != {"authorized_by", "at", "added_rounds", "note"}:
                _reject("%s must be {authorized_by, at, added_rounds, note}" % w)
            if not base.is_nonempty_str(e["authorized_by"]) or not base.is_nonempty_str(e["at"]):
                _reject("%s.authorized_by / at must be non-empty strings" % w)
            if not base.is_strict_int(e["added_rounds"]) or e["added_rounds"] <= 0:
                _reject("%s.added_rounds must be a positive integer" % w)
            if not isinstance(e["note"], str):
                _reject("%s.note must be a string" % w)
            extensions.append(dict(e))
    formal = False
    if "formal_review_authorized_by_owner" in obj:
        v = obj["formal_review_authorized_by_owner"]
        if v is not True and v is not False:
            _reject("formal_review_authorized_by_owner must be a JSON boolean")
        formal = v is True
    invocation = None
    if "invocation_authorization" in obj:
        invocation = obj["invocation_authorization"]
        if not base.is_nonempty_str(invocation):
            _reject("invocation_authorization must be a non-empty string")
        if len(invocation.encode("utf-8")) > 4096:
            _reject("invocation_authorization exceeds 4096 UTF-8 bytes")
    if mode in ("probe", "review") and invocation is None:
        raise base.PreflightError("invocation-authorization-required",
                                  "invocation_authorization is required for probe / review")

    return {
        "subject": subject, "stage": stage, "round": rnd, "round_index": round_index(rnd),
        "caller": caller, "task_record": task_record,
        "artifact_author": {"human_only": obj["artifact_author"]["human_only"],
                            "authors": [dict(a) for a in obj["artifact_author"]["authors"]]},
        "model_vendors": model_vendors,
        "profile": profile, "effort": effort, "runtime_overrides": overrides,
        "attempt_options": attempt_options,
        "inputs": {"candidates": list(cands), "references": list(refs)},
        "review_brief": {
            "background": obj["review_brief"]["background"],
            "check_surfaces": obj["review_brief"]["check_surfaces"],
            "evidence_limits": [{"reference": x["reference"], "reason": x["reason"],
                                 "kind": x.get("kind", C.EVIDENCE_LIMIT_KIND_DEFAULT)}
                                for x in obj["review_brief"]["evidence_limits"]],
            "accepted_residuals": [dict(x) for x in obj["review_brief"]["accepted_residuals"]],
            "remediation_statement": obj["review_brief"].get("remediation_statement"),
        },
        "residual_ids": residual_ids,
        "previous_round": previous_round, "max_rounds": max_rounds, "round_extensions": extensions,
        "formal_review_authorized_by_owner": formal, "invocation_authorization": invocation,
    }
