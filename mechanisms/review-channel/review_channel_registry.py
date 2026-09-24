"""review_channel_registry — Registry 装载、schema 校验、capability 证据核验、defaults（设计 §6.11）。

Registry 是活体数据文件（不入锁面）。适配器发现由入口注入的 `adapter_loader(runtime_id)` 承载
（数据驱动发现，设计 §5.2）；本模块不导入适配器接口模块。
capability 声称 PROBED / REVIEW_ENABLED 的条目按 §6.6「receipt_ref 核验」以 Git 提交 blob 三方相等核对。
"""
import os

import review_channel_base as base
import review_channel_contract as C
import review_channel_execution as X

REGISTRY_FILE = os.path.join(base.UNIT_DIR, "review_channel_registry.json")
_TOP_KEYS = ("registry_schema", "registry_revision", "defaults", "system_default", "providers")
_PROVIDER_KEYS = ("display_name", "kind", "runtime", "transport", "key_env", "base_env", "auth_source",
                  "structured_output", "models", "note")   # r18 R18-B2 整改：provider 级 registered_equivalents 不在冻结 schema
_MODEL_KEYS = ("claimed_vendor", "default_effort", "supported_efforts", "max_input_bytes",
               "registered_equivalents", "capability", "note")
_CAP_KEYS = ("status", "bound_transport", "bound_effort", "receipt_ref", "receipt_commit",
             "receipt_sha256", "evidence_runtime_version", "note")


def _bad(detail):
    raise base.PreflightError("registry-invalid", detail)


def _closed(obj, allowed, where):
    if not isinstance(obj, dict):
        _bad("%s must be an object" % where)
    for k in obj:
        if k not in allowed:
            _bad("%s.%s unknown member" % (where, k))


def load_registry_bytes(path=REGISTRY_FILE):
    try:
        return base.read_bytes(path)
    except OSError as exc:
        _bad("registry unreadable: %s" % type(exc).__name__)


def load_registry(path=REGISTRY_FILE, adapter_loader=None, repo_root=None, verify_evidence=True, profile_checker=None):
    """装载并校验；返回 {"registry": obj, "sha256": ..., "revision": int, "adapters": {runtime: module}}。"""
    data = load_registry_bytes(path)
    try:
        obj = base.strict_json_load(data)
    except (ValueError, UnicodeDecodeError) as exc:
        _bad("registry JSON: %s" % exc)
    _closed(obj, _TOP_KEYS + (("execution_ports", "model_mappings") if obj.get("registry_schema")==C.REGISTRY_SCHEMA else ()), "registry")
    if obj.get("registry_schema") not in C.REGISTRY_READ_SCHEMAS:
        _bad("registry_schema must be %s" % C.REGISTRY_SCHEMA)
    rev = obj.get("registry_revision")
    if not base.is_strict_int(rev) or rev <= 0:
        _bad("registry_revision must be a positive integer")
    defaults = obj.get("defaults")
    _closed(defaults, C.DEFAULTS_KEYS, "defaults")
    for k in C.DEFAULTS_KEYS:
        if k not in defaults:
            _bad("defaults.%s missing" % k)
    if not base.is_strict_int(defaults["max_rounds"]) or defaults["max_rounds"] <= 0:
        _bad("defaults.max_rounds must be a positive integer")
    if not base.is_strict_int(defaults["timeout_seconds"]) or defaults["timeout_seconds"] <= 0:
        _bad("defaults.timeout_seconds must be a positive integer")
    if defaults["effort"] not in C.EFFORTS:
        _bad("defaults.effort outside the closed set")
    sd = obj.get("system_default")
    if not isinstance(sd, dict) or set(sd.keys()) != {"provider", "model"}:
        _bad("system_default must be {provider, model}")
    providers = obj.get("providers")
    if not isinstance(providers, dict) or not providers:
        _bad("providers must be a non-empty object")
    adapters = {}
    for pid, prov in providers.items():
        _validate_provider(pid, prov, adapter_loader, adapters, "mixed" if obj["registry_schema"]==C.REGISTRY_SCHEMA else obj["registry_schema"]=="review-channel-registry/v4")
    if sd["provider"] not in providers or sd["model"] not in providers[sd["provider"]]["models"]:
        _bad("system_default does not name a registered provider / model pair")
    if verify_evidence:
        root = repo_root or base.DEFAULT_REPO_ROOT
        for pid, prov in providers.items():
            for slug, model in prov["models"].items():
                cap = model["capability"]
                if cap["status"] in ("PROBED", "REVIEW_ENABLED"):
                    verify_receipt_ref(root, pid, prov, slug, model, cap, profile_checker)
    if obj["registry_schema"]==C.REGISTRY_SCHEMA:
        X.verify_ports(obj, repo_root or base.DEFAULT_REPO_ROOT, verify_evidence, profile_checker)
    return {"registry": obj, "sha256": base.sha256_bytes(data), "revision": rev, "adapters": adapters,
            "bytes": len(data)}


def _validate_provider(pid, prov, adapter_loader, adapters, archive_schema=False):
    where = "providers.%s" % pid
    if not base.is_nonempty_str(pid):
        _bad("provider id must be a non-empty string")
    _closed(prov, _PROVIDER_KEYS, where)
    for k in ("display_name", "kind", "runtime", "transport", "models"):
        if k not in prov:
            _bad("%s.%s missing" % (where, k))
    if not base.is_nonempty_str(prov["display_name"]):
        _bad("%s.display_name" % where)
    kind = prov["kind"]
    if kind not in C.PROVIDER_KINDS:
        _bad("%s.kind outside the closed set" % where)
    runtime = prov["runtime"]
    if not base.is_nonempty_str(runtime) or not all(c.isalnum() or c == "_" for c in runtime):
        _bad("%s.runtime grammar" % where)
    transport = prov["transport"]
    if transport not in C.TRANSPORTS:
        _bad("%s.transport outside the closed set" % where)
    if kind in ("official-direct", "aggregator"):
        for k in ("key_env", "base_env"):
            if not base.is_nonempty_str(prov.get(k)):
                _bad("%s.%s required for %s" % (where, k, kind))
        if "auth_source" in prov:
            _bad("%s.auth_source forbidden for %s" % (where, kind))
    else:
        if not base.is_nonempty_str(prov.get("auth_source")):
            _bad("%s.auth_source required for builtin-native" % where)
        for k in ("key_env", "base_env"):
            if k in prov:
                _bad("%s.%s forbidden for builtin-native" % (where, k))
    if "structured_output" in prov and prov["structured_output"] not in C.STRUCTURED_OUTPUTS:
        _bad("%s.structured_output outside the closed set" % where)
    # 适配器数据驱动发现（§5.2）
    if adapter_loader is None:
        _bad("adapter loader not wired")
    if runtime not in adapters:
        mod = adapter_loader(runtime)
        if mod is None:
            raise base.PreflightError("runtime-adapter-missing",
                                      "%s.runtime %r: adapters/%s.py absent" % (where, runtime, runtime))
        adapters[runtime] = mod
    mod = adapters[runtime]
    if getattr(mod, "RUNTIME_ID", None) != runtime:
        _bad("%s.runtime adapter RUNTIME_ID mismatch" % where)
    if kind not in getattr(mod, "SUPPORTED_KINDS", ()):
        _bad("%s.kind %r not supported by adapter %r" % (where, kind, runtime))
    if transport not in getattr(mod, "SUPPORTED_TRANSPORTS", ()):
        _bad("%s.transport %r not supported by adapter %r" % (where, transport, runtime))
    inline = bool(getattr(mod, "INLINE_DELIVERY", False))
    # r18 R18-B2 整改（§6.11 schema：structured_output / max_input_bytes 限 http 类 runtime，registered_equivalents 限 aggregator 模型）：
    # 不适用的字段一律拒绝，不得接受后被忽略
    if "structured_output" in prov and not inline:
        _bad("%s.structured_output only applies to inline-delivery (http-class) runtimes; adapter %r never consumes it" % (where, runtime))
    models = prov["models"]
    if not isinstance(models, dict):
        _bad("%s.models must be an object" % where)
    for slug, model in models.items():
        _validate_model(where, slug, model, inline, kind, archive_schema)


def _validate_model(where, slug, model, inline, kind=None, archive_schema=False):
    w = "%s.models.%s" % (where, slug)
    if not base.is_nonempty_str(slug):
        _bad("%s slug" % w)
    _closed(model, _MODEL_KEYS, w)
    for k in ("claimed_vendor", "default_effort", "supported_efforts", "capability"):
        if k not in model:
            _bad("%s.%s missing" % (w, k))
    if model["claimed_vendor"] not in C.VENDORS:
        raise base.PreflightError("vendor-not-registered",
                                  "%s.claimed_vendor %r not in the vendors closed set" % (w, model["claimed_vendor"]))
    sup = model["supported_efforts"]
    if not isinstance(sup, list) or not sup or any(e not in C.EFFORTS for e in sup) or len(set(sup)) != len(sup):
        _bad("%s.supported_efforts" % w)
    if model["default_effort"] not in sup:
        _bad("%s.default_effort must be in supported_efforts" % w)
    if inline:
        mib = model.get("max_input_bytes")
        if not base.is_strict_int(mib) or mib <= 0:
            _bad("%s.max_input_bytes required (positive integer) for inline-delivery runtimes" % w)
    elif "max_input_bytes" in model:
        _bad("%s.max_input_bytes only applies to inline-delivery (http-class) runtimes; never consumed here" % w)   # r18 R18-B2
    if "registered_equivalents" in model:
        if kind != "aggregator":
            _bad("%s.registered_equivalents only applies to aggregator models; identity binding never consumes it for %s" % (w, kind))   # r18 R18-B2
        if not isinstance(model["registered_equivalents"], dict):
            _bad("%s.registered_equivalents must be an object" % w)
    cap = model["capability"]
    if archive_schema == "mixed": archive_schema = "evidence" in cap
    _closed(cap, tuple(k for k in _CAP_KEYS if not k.startswith("receipt_"))+("evidence",) if archive_schema else _CAP_KEYS, w + ".capability")
    status = cap.get("status")
    if status not in C.CAPABILITY_STATUSES:
        _bad("%s.capability.status outside the closed set" % w)
    if status in ("PROBED", "REVIEW_ENABLED"):
        if archive_schema:
            try:
                import review_evidence as E
                E.capability_evidence(cap.get('evidence'))
            except E.EvidenceError as exc:_bad(str(exc))
        required=('bound_transport','bound_effort') if archive_schema else ('bound_transport','bound_effort','receipt_ref','receipt_commit','receipt_sha256')
        for k in required:
            if not base.is_nonempty_str(cap.get(k)):
                _bad("%s.capability.%s required for %s" % (w, k, status))
        if cap["bound_transport"] not in C.TRANSPORTS:
            _bad("%s.capability.bound_transport" % w)
        if cap["bound_effort"] not in sup:
            _bad("%s.capability.bound_effort must be in supported_efforts" % w)
        if archive_schema:return
        if not base.hex64(cap["receipt_sha256"]):
            _bad("%s.capability.receipt_sha256 grammar" % w)
        rc = cap["receipt_commit"]
        if len(rc) != 40 or any(c not in "0123456789abcdef" for c in rc):
            _bad("%s.capability.receipt_commit must be a full lowercase SHA" % w)


def _ref_reject(detail):
    raise base.PreflightError("receipt-ref-invalid", detail)


def _durable_location(root, ref, provider_id=None):
    """返回 'tracked' | 'diagnostics' | None（不在耐久落点）。"""
    if not isinstance(ref, str) or ref.startswith("/") or ".." in ref.split("/"):
        return None
    parts = ref.split("/")
    name = parts[-1]
    if ref.startswith(C.DIAGNOSTICS_DIR + "/") and len(parts) == 5 and name.startswith("receipt-") \
            and name.endswith(".json"):
        return "diagnostics"
    if len(parts) >= 3 and name.startswith("receipt-r") and name.endswith(".json"):
        if parts[0] == "reviews" and len(parts) == 4:
            return "tracked"
        if parts[0] == "tasks" and len(parts) == 5 and parts[2] == "reviews":
            return "tracked"
    return None


def verify_receipt_ref(root, pid, prov, slug, model, cap, profile_checker=None):
    """§6.6 / §6.11：按声称状态核对 receipt_ref（内容驱动、Git blob 三方相等、HEAD 祖先）。"""
    import review_evidence as E
    try:
        evidence=cap.get('evidence')
        if evidence is not None:
            E.capability_evidence(evidence)
            if evidence['kind']=='archive':
                archive=E.configured(root);E.require(archive is not None,'archive capability before switch','archive-unconfigured')
                d,files=archive.read(evidence['ref'])
                E.require(d['kind']==('round' if cap['status']=='REVIEW_ENABLED' else 'attempt'),'capability object kind')
                E.require(evidence['receipt_path'] in files,'Receipt not present in object','archive-incomplete')
                work=files[evidence['receipt_path']];E.require(base.sha256_bytes(work)==evidence['receipt_sha256'],'capability Receipt hash')
                receipt=base.strict_json_load(work)
                E.require(receipt.get('receipt_schema') in ('review-channel-receipt/v3',C.RECEIPT_SCHEMA) and receipt.get('evidence_storage')=={'kind':'archive','repository_id':archive.repository_id,'round_key':d['round_key']},'capability Receipt origin')
                decision=evidence['decision'];fixed=E.fixed_file(root,decision['commit'],decision['path'])['content']
                result=E.result_block(fixed);actual=E.minimal_result(root,evidence['ref'])
                E.require(all(result[k]==actual[k] for k in actual if k!='residuals'),'capability fixed decision differs')
                check_receipt_evidence(receipt,cap['status'],'tracked' if d['kind']=='round' else 'diagnostics',pid,prov,slug,model,cap,profile_checker)
                return
            cap=dict(cap,**{k:v for k,v in evidence.items() if k!='kind'})
        ref=cap['receipt_ref'];loc=_durable_location(root,ref)
        E.require(loc is not None,'Receipt is not at a legacy durable location')
        work=E.fixed_file(root,cap['receipt_commit'],ref)['content']
        E.require(base.sha256_bytes(work)==cap['receipt_sha256'],'committed Receipt hash differs')
        present=os.path.join(root,ref)
        if os.path.lexists(present):E.require(E.read_file(E.safe_path(root,ref))==work,'working Receipt differs from fixed original')
        receipt=base.strict_json_load(work)
        check_receipt_evidence(receipt,cap['status'],loc,pid,prov,slug,model,cap,profile_checker)
    except (E.EvidenceError,ValueError,UnicodeError) as exc:_ref_reject(str(exc))


def check_receipt_evidence(receipt, status, loc, pid, prov, slug, model, cap, profile_checker=None):
    """内容驱动核验（纯函数，供自测直接调用）。
    `profile_checker` = effective Profile 全字段校验器（由入口注入 review_channel_receipt.profile_shape_problems；
    契约模块之间不横向导入，§5.2，R6-B7 整改）；缺席即 fail closed。"""
    if not isinstance(receipt, dict) or receipt.get("receipt_schema") not in C.RECEIPT_READ_SCHEMAS:
        _ref_reject("receipt_schema mismatch")
    if X.receipt_common_problems(receipt) or receipt.get("execution") is not None:
        _ref_reject("maintenance capability requires valid maintenance Receipt")
    for k in C.RECEIPT_REF_FIELDS:
        if k not in receipt:
            _ref_reject("receipt field %s missing" % k)
    mode = receipt.get("mode")
    if mode not in C.MODES or mode == "preflight":
        _ref_reject("receipt mode %r cannot carry evidence" % (mode,))
    three = (receipt["call_path_proof"], receipt["profile_binding"], receipt["verdict_validation"])
    # R3-B7 整改：阶段、结局、发布标志与判词组的交叉条件逐项核对，不只核模式 / 分类 / 三维
    if status == "PROBED":
        ok_probe = (mode == "probe" and receipt["classification"] == "probe_completed" and receipt["receipt_phase"] == "completed"
                    and three == ("PROVEN", "SUFFICIENT", "NOT_REACHED"))
        ok_review = (mode == "review" and receipt["classification"] == "reviewer_output_invalid"
                     and receipt["receipt_phase"] == "called" and three == ("PROVEN", "SUFFICIENT", "INVALID"))
        if not (ok_probe or ok_review) or loc != "diagnostics":
            _ref_reject("PROBED evidence path not satisfied")
        if not (receipt["attempt_outcome"] == "receipt_only" and receipt["verdict_published"] is False
                and receipt["verdict_path"] is None and receipt["verdict_sha256"] is None):
            _ref_reject("PROBED evidence must be receipt_only with no published verdict")
    elif status == "REVIEW_ENABLED":
        if not (mode == "review" and receipt["receipt_phase"] == "completed"
                and receipt["classification"] == "completed_with_valid_verdict"
                and three == ("PROVEN", "SUFFICIENT", "VALID") and receipt["verdict_published"] is True
                and loc == "tracked"):
            _ref_reject("REVIEW_ENABLED evidence path not satisfied")
        if not (receipt["attempt_outcome"] == "governed_verdict" and base.is_nonempty_str(receipt["verdict_path"])
                and base.hex64(receipt["verdict_sha256"])):
            _ref_reject("REVIEW_ENABLED evidence must be a governed verdict with a published path and hash")
    else:
        _ref_reject("status %s carries no evidence" % status)
    ep = receipt.get("effective_profile")
    if not isinstance(ep, dict) or ep.get("profile_schema") not in C.PROFILE_READ_SCHEMAS:
        _ref_reject("effective_profile schema mismatch")
    if profile_checker is None:
        _ref_reject("no effective_profile checker injected; capability evidence cannot be verified")
    shape = profile_checker(ep)
    if shape:
        _ref_reject("effective_profile shape: %s" % "; ".join(shape))
    if ep["calls"] is None:
        _ref_reject("effective_profile call group must be present for capability evidence")
    if (ep["verdict_path"], ep["verdict_sha256"]) != (receipt["verdict_path"], receipt["verdict_sha256"]):
        _ref_reject("effective_profile published group must equal the receipt published fields")
    if ep.get("route_provider") != pid or ep.get("requested_model") != slug:
        _ref_reject("effective_profile provider / model mismatch")
    if ep.get("transport") != cap["bound_transport"]:
        _ref_reject("effective_profile transport differs from bound_transport")
    if ep.get("effective_effort") is None or ep.get("effective_effort") != cap["bound_effort"]:
        _ref_reject("effective_profile effective_effort differs from bound_effort (or null)")
    if ep.get("claimed_vendor") != model["claimed_vendor"]:
        _ref_reject("effective_profile claimed_vendor mismatch")
    if ep.get("runtime") != prov["runtime"] or ep.get("route_provider_kind") != prov["kind"]:
        _ref_reject("effective_profile runtime / kind mismatch")
    if prov["kind"] == "builtin-native":
        if ep.get("auth_mode") != C.AUTH_MODE_BUILTIN or ep.get("auth_source_path") != prov["auth_source"]:
            _ref_reject("effective_profile auth binding mismatch")
        if ep.get("provider_key_env") is not None or ep.get("provider_base_env") is not None:
            _ref_reject("effective_profile key / base must be null for builtin-native")
    else:
        if ep.get("provider_key_env") != prov["key_env"] or ep.get("provider_base_env") != prov["base_env"]:
            _ref_reject("effective_profile key / base env names mismatch")
        if ep.get("auth_mode") is not None or ep.get("auth_source_path") is not None:
            _ref_reject("effective_profile auth binding must be null for env-keyed providers")


def find_profile(reg, provider_id, model_slug):
    prov = reg["registry"]["providers"].get(provider_id)
    if prov is None or model_slug not in prov["models"]:
        raise base.PreflightError("profile-unknown", "profile %s / %s not registered" % (provider_id, model_slug))
    return prov, prov["models"][model_slug]


def admission(cap_status, mode, formal):
    """准入矩阵（§6.11）。合格返回 None，否则失败码。"""
    if cap_status == "REGISTERED":
        return "capability-not-configured"
    if cap_status in C.FORMAL_REQUIRED_STATUSES and not formal:
        return "formal-authorization-required"
    return None


def effective_status(model, transport, effort):
    """已绑定 Profile 的比对三支（§6.11）：(model, transport, effort) 与绑定不符 → 按 UNVERIFIED。"""
    cap = model["capability"]
    st = cap["status"]
    if st in ("PROBED", "REVIEW_ENABLED"):
        if cap["bound_transport"] != transport or cap["bound_effort"] != effort:
            return "UNVERIFIED"
    return st
