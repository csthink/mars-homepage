"""review_channel_selection — Profile 选择、继承、显式改选、跨模型提供方资格、轮次额度（设计 §6.8 至 §6.10）。

- 选择三级：显式 profile > r2+ 上一轮有效判词的 effective Profile 快照 > r1 Registry system_default；本级失败不下探。
- 继承经治理链锚定（previous_round.receipt_path 位于 tracked_round_dir 且受 Git 跟踪、Receipt 记录成功、
  Profile 全字段校验、verdict_sha256 重算相等、subject / stage 一致、序号更小）；Registry 漂移防线。
- 资格：reviewer vendor ∉ author vendors（大小写敏感精确比较）。
- 轮次额度：计数对象 = tracked_parent 下存在 receipt-r<K>.json 的轮数；序号严格递增、缺口须 review-skip ruling。
本模块不导入其他契约模块。
"""
import os
import re

import review_channel_base as base
import review_channel_contract as C
import review_channel_history as H

_ROUND_DIR_RE = re.compile(r"^(?:(task|impl)-)?r([1-9][0-9]*)$")
_RULING_RE = re.compile(r"^RU-[0-9]{2,}-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")


# ---------------------------------------------------------------- 资格（§6.8）

def check_eligibility(model_vendors, human_only, reviewer_vendor):
    if reviewer_vendor not in C.VENDORS:
        raise base.PreflightError("vendor-not-registered", "reviewer vendor %r outside the closed set" % reviewer_vendor)
    vendors = list(model_vendors)
    if human_only:
        if vendors:
            raise base.PreflightError("eligibility", "human_only with a non-empty model vendor set")
        return
    if not vendors or any(v in ("", "unknown") or v not in C.VENDORS for v in vendors):
        raise base.PreflightError("eligibility", "author model vendor set empty, unknown or outside the closed set")
    if reviewer_vendor in vendors:
        raise base.PreflightError("eligibility", "reviewer vendor %s is in the author vendor set" % reviewer_vendor)


# ---------------------------------------------------------------- 轮目录扫描

def scan_rounds(repo_root, tracked_parent, axis, stage):
    """返回 {K: {"dir": relpath, "receipt": bool}}（同轴同 stage 的轮目录）。"""
    import review_evidence as E
    try:
        if E.storage_mode(repo_root)=='archive-v1':return E.scan_rounds(repo_root,tracked_parent,axis,stage)
    except E.EvidenceError as exc:raise base.PreflightError(exc.code,exc.message) from exc
    out = {}
    absparent = os.path.join(repo_root, tracked_parent)
    for entry in sorted(os.scandir(absparent), key=lambda e: e.name) if os.path.isdir(absparent) else []:
        if not entry.is_dir(follow_symlinks=False):
            continue
        m = _ROUND_DIR_RE.match(entry.name)
        if not m:
            continue
        if axis == "task":
            if m.group(1) != stage:
                continue
        elif m.group(1) is not None:
            continue
        k = int(m.group(2))
        rel = tracked_parent + "/" + entry.name
        receipt = os.path.isfile(os.path.join(entry.path, "receipt-r%d.json" % k))
        out[k] = {"dir": rel, "receipt": receipt}
    historical = H.items(repo_root, tracked_parent+'/')
    versions = {}
    for item in historical:
        path = item['path']; versions.setdefault(path, set()).add(item['blob_oid'])
        if len(versions[path]) > 1:
            raise base.PreflightError('history-unavailable', 'history-ambiguous: '+path)
        current = os.path.join(repo_root, path)
        if os.path.isfile(current) and base.sha256_bytes(base.read_bytes(current)) != item['sha256']:
            raise base.PreflightError('history-unavailable', 'history-integrity: '+path)
        rest = path[len(tracked_parent)+1:]; parts = rest.split('/')
        if len(parts) != 2:
            continue
        m = _ROUND_DIR_RE.fullmatch(parts[0])
        if not m or (m.group(1) != stage if axis == 'task' else m.group(1) is not None):
            continue
        k = int(m.group(2)); rel = tracked_parent+'/'+parts[0]
        row = out.setdefault(k, {'dir': rel, 'receipt': False})
        row['receipt'] |= parts[1] == 'receipt-r%d.json' % k
    return out


def rulings_dir(routing):
    if routing["axis"] == "task":
        return "tasks/%s/rulings" % routing["task_record"]
    return "records/governance/%s/rulings" % routing["subject"]


def review_skip_covers(repo_root, routing, k):
    """ruling 目录内存在 Type: review-skip 且 Object 指向该轮的裁定即覆盖。"""
    d = os.path.join(repo_root, rulings_dir(routing))
    if not os.path.isdir(d):
        return False
    # R2-B11 / R3-B9 整改：整词匹配（两侧边界都排除字母、数字与短横线），r1 不得命中 r10 / impl-r10 / impl-r1x
    pattern = re.compile(r"(?<![A-Za-z0-9-])(?:%s-)?r%d(?![A-Za-z0-9-])" % (re.escape(routing["stage"]), k))
    for entry in sorted(os.scandir(d), key=lambda e: e.name):
        if not entry.is_file(follow_symlinks=False) or not _RULING_RE.match(entry.name):
            continue
        try:
            text = base.read_bytes(entry.path).decode("utf-8", "replace")
        except OSError:
            continue
        head = text[:4000].split("\n")
        typ = [ln for ln in head if ln.startswith("> Type:")]
        obj = [ln for ln in head if ln.startswith("> Object:")]
        # Type 字段值精确等于 review-skip（R3-B9 整改：不做子串判定）
        if not typ or typ[0][len("> Type:"):].strip() != "review-skip":
            continue
        if obj and pattern.search(obj[0]):
            return True
    return False


def check_round_sequence(repo_root, routing, rounds):
    """序号严格递增；缺口须逐个被 review-skip 覆盖。返回 previous_valid_round（最大已投递轮序号或 None）。"""
    n = routing["round_index"]
    existing = sorted(rounds)
    if existing and n <= max(existing):
        raise base.PreflightError("round-not-increasing",
                                  "round r%d must exceed the largest existing round r%d" % (n, max(existing)))
    submitted = sorted(k for k, v in rounds.items() if v["receipt"])
    last = submitted[-1] if submitted else 0
    for k in range(last + 1, n):
        if k in rounds and rounds[k]["receipt"]:
            continue
        if not review_skip_covers(repo_root, routing, k):
            raise base.PreflightError("round-gap-unexplained",
                                      "round r%d has no tracked Receipt and no review-skip ruling" % k)
    return last or None


def round_budget(request, defaults, rounds):
    """§6.10：允许轮数 = 生效 max_rounds + 加轮之和；已投递轮数 ≥ 允许即 exhausted。"""
    if request["max_rounds"] is not None:
        max_rounds, source = request["max_rounds"], "request"
    else:
        max_rounds, source = defaults["max_rounds"], "default"
    added = sum(e["added_rounds"] for e in request["round_extensions"])
    submitted = sum(1 for v in rounds.values() if v["receipt"])
    allowed = max_rounds + added
    return {"max_rounds": max_rounds, "source": source, "extensions": [dict(e) for e in request["round_extensions"]],
            "extensions_added": added, "allowed_rounds": allowed, "round_index": request["round_index"],
            "submitted_rounds": submitted, "exhausted": submitted >= allowed}


# ---------------------------------------------------------------- 上一轮锚定（§6.9）

def _expected_receipt_dir(routing, k):
    if routing["axis"] == "task":
        return "%s/%s-r%d" % (routing["tracked_parent"], routing["stage"], k)
    return "%s/r%d" % (routing["tracked_parent"], k)


def load_previous(repo_root, request, routing, latest_submitted=None, receipt_checker=None):
    """读取并锚定上一轮：返回 previous dict（含 receipt / verdict / manifest 字节与对象、K）。
    `latest_submitted` = 该 stage 下最近一轮已投递（有 tracked Receipt）的序号；previous_round 必须恰指向它（R1-B7 整改）。"""
    pr = request["previous_round"]
    if 'evidence' in pr:
        import review_evidence as E
        try:return E.load_previous(repo_root,request,routing,latest_submitted)
        except E.EvidenceError as exc:raise base.PreflightError(exc.code,exc.message) from exc
    rp = pr["receipt_path"].replace(os.sep, "/")
    vp = pr["verdict_path"].replace(os.sep, "/")
    for p in (rp, vp):
        if p.startswith("/") or ".." in p.split("/"):
            raise base.PreflightError("inherit-unanchored", "previous_round paths must be repository-relative")
    rdir, rname = rp.rsplit("/", 1) if "/" in rp else ("", rp)
    m = re.match(r"^receipt-r([1-9][0-9]*)\.json$", rname)
    if not m:
        raise base.PreflightError("inherit-unanchored", "previous receipt filename grammar")
    k = int(m.group(1))
    if rdir != _expected_receipt_dir(routing, k):
        raise base.PreflightError("inherit-unanchored", "previous receipt is not inside the expected tracked round dir")
    if k >= routing["round_index"]:
        raise base.PreflightError("inherit-unanchored", "previous round must precede the current round")
    if latest_submitted is not None and k != latest_submitted:
        raise base.PreflightError("inherit-unanchored",
                                  "previous_round must point to the latest submitted round r%d, not r%d" % (latest_submitted, k))
    if vp.rsplit("/", 1)[0] != rdir:
        raise base.PreflightError("inherit-unanchored", "previous verdict must sit in the same tracked round dir")
    historical = H.round_files(repo_root, rdir)
    for p in (rp, vp, rdir + "/" + C.MANIFEST_NAME):
        if historical is not None:
            if p not in historical:
                raise base.PreflightError("inherit-unanchored", "historical member missing: "+p)
            continue
        if not os.path.isfile(os.path.join(repo_root, p)):
            raise base.PreflightError("inherit-unanchored", "previous round file absent: %s" % p)
        if not base.git_tracked(repo_root, p):
            raise base.PreflightError("inherit-unanchored", "previous round file not tracked by git: %s" % p)
    get_bytes = lambda p: historical[p]["content"] if historical is not None else base.read_bytes(os.path.join(repo_root, p))
    receipt_bytes = get_bytes(rp)
    verdict_bytes = get_bytes(vp)
    manifest_bytes = get_bytes(rdir + "/" + C.MANIFEST_NAME)
    try:
        receipt = base.strict_json_load(receipt_bytes)
        manifest = base.strict_json_load(manifest_bytes)
    except (ValueError, UnicodeDecodeError):
        raise base.PreflightError("inherit-unanchored", "previous receipt or manifest unparseable")
    if not isinstance(receipt, dict) or receipt.get("receipt_schema") not in C.RECEIPT_READ_SCHEMAS:
        raise base.PreflightError("inherit-unanchored", "previous receipt schema mismatch")
    if not (receipt.get("classification") == "completed_with_valid_verdict" and receipt.get("verdict_published") is True
            and receipt.get("attempt_outcome") == "governed_verdict" and receipt.get("mode") == "review"
            and receipt.get("receipt_phase") == "completed"
            and (receipt.get("call_path_proof"), receipt.get("profile_binding"), receipt.get("verdict_validation")) == ("PROVEN", "SUFFICIENT", "VALID")):
        raise base.PreflightError("inherit-unanchored", "previous receipt does not record a governed verdict in the frozen success form")
    import review_channel_execution as X
    if (receipt_checker or X.receipt_common_problems)(receipt) or receipt.get("evidence_storage") is not None:
        raise base.PreflightError("inherit-unanchored", "previous Receipt shape invalid")
    for field in C.RECEIPT_REF_FIELDS:
        if field not in receipt:
            raise base.PreflightError("inherit-unanchored", "previous receipt lacks the frozen field %s" % field)
    if receipt.get("subject") != routing["subject"] or receipt.get("stage") != routing["stage"]:
        raise base.PreflightError("inherit-unanchored", "previous receipt subject / stage mismatch")
    if receipt.get("round") != "r%d" % k:
        raise base.PreflightError("inherit-unanchored", "previous receipt round mismatch")
    if receipt.get("verdict_sha256") != base.sha256_bytes(verdict_bytes):
        raise base.PreflightError("inherit-unanchored", "previous verdict bytes differ from the receipt verdict_sha256")
    if receipt.get("verdict_path") != vp:
        raise base.PreflightError("inherit-unanchored", "previous receipt verdict_path differs from previous_round.verdict_path")
    if not isinstance(manifest, dict) or manifest.get("manifest_sha256") != receipt.get("input_manifest_sha256"):
        raise base.PreflightError("inherit-unanchored", "previous manifest hash differs from the receipt")
    # R11-B1 整改：清单摘要按 inputs 的 canonical 内容重算，不信任自报值——工作树被改动的 manifest 不得成为继承锚
    if not isinstance(manifest.get("inputs"), list) or base.sha256_bytes(base.canonical_json(manifest["inputs"])) != manifest["manifest_sha256"]:
        raise base.PreflightError("inherit-unanchored", "previous manifest_sha256 does not equal the canonical hash of its inputs")
    return {"k": k, "receipt": receipt, "receipt_bytes": receipt_bytes, "receipt_path": rp,
            "verdict_bytes": verdict_bytes, "verdict_path": vp, "manifest": manifest,
            "manifest_bytes": manifest_bytes, "manifest_path": rdir + "/" + C.MANIFEST_NAME,
            "tracked_round_dir": rdir, "history_sources": list(historical.values()) if historical else []}


def drift_check(prof, provider_id, provider, model):
    """Registry 漂移防线：快照的非秘密绑定与当前 Registry 条目逐一比对。"""
    kind = provider["kind"]
    checks = [
        (prof.get("route_provider_kind"), kind, "kind"),
        (prof.get("runtime"), provider["runtime"], "runtime"),
        (prof.get("transport"), provider["transport"], "transport"),
        (prof.get("claimed_vendor"), model["claimed_vendor"], "claimed vendor"),
    ]
    if kind == "builtin-native":
        checks += [(prof.get("auth_mode"), C.AUTH_MODE_BUILTIN, "auth_mode"),
                   (prof.get("auth_source_path"), provider["auth_source"], "auth_source_path"),
                   (prof.get("provider_key_env"), None, "key env"), (prof.get("provider_base_env"), None, "base env")]
    else:
        checks += [(prof.get("provider_key_env"), provider["key_env"], "key env"),
                   (prof.get("provider_base_env"), provider["base_env"], "base env"),
                   (prof.get("auth_mode"), None, "auth_mode"), (prof.get("auth_source_path"), None, "auth_source_path")]
    for got, want, what in checks:
        if got != want:
            raise base.PreflightError("inherit-unmaterializable",
                                      "inherited snapshot %s differs from the current Registry entry %s" % (what, provider_id))


def select(request, reg, previous, profile_shape_problems):
    """三级选择。previous = load_previous 输出或 None。返回 selection dict。"""
    registry = reg["registry"]
    providers = registry["providers"]
    prev_prof = previous["receipt"].get("effective_profile") if previous else None
    prev_pair = None
    if prev_prof is not None:
        problems = profile_shape_problems(prev_prof)
        if problems:
            raise base.PreflightError("inherit-unanchored", "previous effective_profile: %s" % problems[0])
        prev_pair = {"provider": prev_prof["route_provider"], "model": prev_prof["requested_model"]}
    if request["profile"] is not None:
        pid, slug = request["profile"]["provider"], request["profile"]["model"]
        source = "explicit-request"
    elif previous is not None:
        pid, slug = prev_pair["provider"], prev_pair["model"]
        source = "inherited-snapshot"
    else:
        pid, slug = registry["system_default"]["provider"], registry["system_default"]["model"]
        source = "system-default"
    prov = providers.get(pid)
    if prov is None or slug not in prov["models"]:
        raise base.PreflightError("profile-unknown", "profile %s / %s not registered" % (pid, slug))
    model = prov["models"][slug]
    inherited = source == "inherited-snapshot"
    if inherited:
        if prev_prof.get("effective_effort") is None:
            raise base.PreflightError("inherit-unanchored", "previous attempt-level effective_effort is null")
        drift_check(prev_prof, pid, prov, model)
    # effort：显式 > 继承 > Registry 默认
    if request["effort"] is not None:
        effort, effort_source = request["effort"], "explicit-request"
    elif inherited:
        effort, effort_source = prev_prof["effective_effort"], "inherited-snapshot"
    else:
        effort, effort_source = model["default_effort"], "registry-default"
    if effort not in model["supported_efforts"]:
        raise base.PreflightError("effort-unsupported", "effort %s not in supported_efforts of %s / %s" % (effort, pid, slug))
    # overrides：当轮显式优先（显式 {} 亦为显式：清除继承），沉默（字段缺席）才整套继承（R5-B2 整改）
    if request["runtime_overrides"] is not None:
        overrides, ov_source = dict(request["runtime_overrides"]), "explicit-request"
    elif inherited:
        # R5-B8 整改：沉默即整套继承上一轮快照——快照为合法空对象时来源仍是 inherited-snapshot
        overrides, ov_source = dict(prev_prof["runtime_overrides"]), "inherited-snapshot"
    else:
        overrides, ov_source = {}, "none"
    changed = None if prev_pair is None else (prev_pair != {"provider": pid, "model": slug})
    return {"provider_id": pid, "provider": prov, "model_slug": slug, "model": model, "effort": effort,
            "effort_source": effort_source, "selection_source": source, "overrides": overrides,
            "overrides_source": ov_source, "changed_from_previous": changed, "previous_pair": prev_pair,
            "previous_runtime": (previous["receipt"].get("runtime") if previous else None)}
