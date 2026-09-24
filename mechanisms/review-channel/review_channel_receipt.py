"""review_channel_receipt — Receipt 与 effective Profile 组装（设计 §6.6）。

- effective Profile 三组：选定组（封存时）、调用组（calls[] 累积 + 三个归约字段）、发布组（落位后）；
  未到确定时点的组整组 null。
- attempt 级三维 = calls[] 保守聚合 × 校验结果。
- Receipt 按阶段条件 schema（routed / sealed / called / completed）与补产 interrupted 行的字段闭集。
本模块不导入其他契约模块。
"""
import copy
import review_channel_base as base
import review_channel_contract as C
import review_channel_execution as X

SELECTED_KEYS = (
    "caller", "artifact_author", "route_provider", "route_provider_kind", "runtime", "requested_model",
    "resolved_upstream", "claimed_vendor", "routing_mode", "transport", "requested_effort",
    "requested_effort_source", "runtime_overrides", "overrides_source", "selection_source", "registry_revision",
    "registry_sha256", "contract_version", "contract_design_sha256", "request_sha256", "input_manifest_sha256",
    "round", "attempt_id", "model_identity_assurance", "inheritance_context", "reviewer_tool_surface",
    "provider_key_env", "provider_base_env", "auth_mode", "auth_source_path",
)
CALL_GROUP_KEYS = ("calls", "effective_effort", "effort_source", "response_model", "logical_model_match")
PUBLISHED_KEYS = ("verdict_path", "verdict_sha256")
LEGACY_PROFILE_KEYS = ("profile_schema",) + SELECTED_KEYS + CALL_GROUP_KEYS + PUBLISHED_KEYS
PROFILE_KEYS = LEGACY_PROFILE_KEYS + ("execution_applicability",)

LEGACY_RECEIPT_KEYS=C.LEGACY_RECEIPT_KEYS
RECEIPT_KEYS=C.RECEIPT_KEYS


def selected_group(sel):
    """sel 字段由入口在封存时汇集（见 review_channel.py）。"""
    prov = sel["provider"]
    kind = prov["kind"]
    return {
        "execution_applicability": sel.get("execution_applicability"),
        "caller": sel["caller"],
        "artifact_author": {"human_only": sel["artifact_author"]["human_only"],
                            "authors": sel["artifact_author"]["authors"],
                            "model_vendors": list(sel["model_vendors"])},
        "route_provider": sel["provider_id"],
        "route_provider_kind": kind,
        "runtime": prov["runtime"],
        "requested_model": sel["model_slug"],
        # 聚合商：上游提供方与模型由应答侧标识在调用后确定（调用组 route_provenance / upstream_route_visibility），
        # 选定时只能记路由提供方自身；official-direct / builtin-native 的上游即路由提供方（R1-B10 整改）
        "resolved_upstream": ({"provider": None, "model": None, "route_visibility": "pending-response"} if kind == "aggregator"
                              else {"provider": sel["provider_id"], "model": sel["model_slug"], "route_visibility": "direct"}),
        "claimed_vendor": sel["model"]["claimed_vendor"],
        "routing_mode": "aggregator_managed" if kind == "aggregator" else "direct",
        "transport": prov["transport"],
        "requested_effort": sel["effort"],
        "requested_effort_source": sel["effort_source"],
        "runtime_overrides": dict(sel["overrides"]),
        "overrides_source": sel["overrides_source"],
        "selection_source": sel["selection_source"],
        "registry_revision": sel["registry_revision"],
        "registry_sha256": sel["registry_sha256"],
        "contract_version": sel["contract_version"],
        "contract_design_sha256": sel["contract_design_sha256"],
        "request_sha256": sel["request_sha256"],
        "input_manifest_sha256": sel["input_manifest_sha256"],
        "round": sel["round"],
        "attempt_id": sel["attempt_id"],
        "model_identity_assurance": C.MODEL_IDENTITY_ASSURANCE,
        "inheritance_context": {"question_ids": list(sel["question_ids"]), "bundle_names": list(sel["bundle_names"]),
                                "task_file": sel["task_file"]},
        "reviewer_tool_surface": {"web_search": "forced-off", "delivery": sel["delivery"]},
        "provider_key_env": prov.get("key_env") if kind != "builtin-native" else None,
        "provider_base_env": prov.get("base_env") if kind != "builtin-native" else None,
        "auth_mode": C.AUTH_MODE_BUILTIN if kind == "builtin-native" else None,
        "auth_source_path": prov.get("auth_source") if kind == "builtin-native" else None,
    }


def reduction_conflict(calls):
    """三个归约字段任一不等即冲突（§6.6 单值消费端归约）。"""
    return any(C.reduce_single_valued(calls, k) is None and any(c.get(k) is not None for c in calls)
               for k in ("effective_effort", "effort_source", "response_model", "logical_model_match"))


def call_group(calls):
    """调用组：calls[] 与三个归约字段——全部相等各取其值；任一不等时三字段整体 null（R1-B3 整改）。"""
    if not calls:
        return None
    conflict = reduction_conflict(calls)
    return {
        "calls": [dict(c) for c in calls],
        "effective_effort": None if conflict else C.reduce_single_valued(calls, "effective_effort"),
        "effort_source": None if conflict else C.reduce_single_valued(calls, "effort_source"),
        "response_model": None if conflict else C.reduce_single_valued(calls, "response_model"),
        "logical_model_match": None if conflict else C.reduce_single_valued(calls, "logical_model_match"),
    }


def route_facts(calls):
    """Receipt 级路由来源事实（§6.11 / R1-B10）：由 calls[] 归约；Profile 形状（v3）不变。"""
    if not calls:
        return None, None
    conflict = reduction_conflict(calls)
    return (C.reduce_single_valued(calls, "upstream_route_visibility"),
            None if conflict else C.reduce_single_valued(calls, "route_provenance"))


def build_profile(selected, calls, published):
    """三组并集恰为 PROFILE_KEYS；未到时点的组整组 null。"""
    prof = {"profile_schema": C.PROFILE_SCHEMA, "execution_applicability": selected.get("execution_applicability") if selected else None}
    for k in SELECTED_KEYS:
        prof[k] = selected[k] if selected is not None else None
    cg = call_group(calls) if calls else None
    for k in CALL_GROUP_KEYS:
        prof[k] = cg[k] if cg is not None else None
    for k in PUBLISHED_KEYS:
        prof[k] = published[k] if published is not None else None
    return prof


_STR_KEYS = ("caller", "route_provider", "runtime", "requested_model", "claimed_vendor", "transport", "requested_effort",
             "registry_sha256", "contract_version", "contract_design_sha256", "request_sha256", "input_manifest_sha256",
             "round", "attempt_id")


def profile_shape_problems(prof):
    """继承读端全字段校验（§6.9；R1-B7 整改：键集合 + 逐键类型与闭集 + 交叉约束）。"""
    p = []
    if not isinstance(prof, dict):
        return ["effective_profile must be an object"]
    if prof.get("profile_schema") not in C.PROFILE_READ_SCHEMAS:
        p.append("profile_schema mismatch")
    keys = PROFILE_KEYS if prof.get("profile_schema") == C.PROFILE_SCHEMA else LEGACY_PROFILE_KEYS
    for k in keys:
        if k not in prof:
            p.append("profile key %s missing" % k)
    for k in prof:
        if k not in keys:
            p.append("profile key %s unknown" % k)
    if p:
        return p
    if prof.get("execution_applicability") is not None and not X.applicability(prof["execution_applicability"]):
        p.append("execution_applicability shape")
    for k in _STR_KEYS:
        if not base.is_nonempty_str(prof[k]):
            p.append("profile %s must be a non-empty string" % k)
    if prof["route_provider_kind"] not in C.PROVIDER_KINDS:
        p.append("route_provider_kind outside the closed set")
    if prof["transport"] not in C.TRANSPORTS:
        p.append("transport outside the closed set")
    if prof["claimed_vendor"] not in C.VENDORS:
        p.append("claimed_vendor outside the closed set")
    if prof["requested_effort"] not in C.EFFORTS or prof["requested_effort_source"] not in C.EFFORT_SOURCES:
        p.append("requested effort / source outside the closed set")
    if prof["selection_source"] not in C.SELECTION_SOURCES or prof["overrides_source"] not in C.OVERRIDE_SOURCES:
        p.append("selection / overrides source outside the closed set")
    if prof["routing_mode"] not in ("direct", "aggregator_managed"):
        p.append("routing_mode outside the closed set")
    if not base.is_strict_int(prof["registry_revision"]) or prof["registry_revision"] <= 0:
        p.append("registry_revision must be a positive integer")
    ov = prof["runtime_overrides"]
    if not (isinstance(ov, dict) and all(base.is_nonempty_str(k) and isinstance(v, (str, int, float, bool)) for k, v in ov.items())):
        p.append("runtime_overrides must be an object of non-empty string keys to scalar values")
    if prof["model_identity_assurance"] != C.MODEL_IDENTITY_ASSURANCE:
        p.append("model_identity_assurance must be operational-claim")
    aa = prof["artifact_author"]
    if not (isinstance(aa, dict) and set(aa) == {"human_only", "authors", "model_vendors"} and isinstance(aa["human_only"], bool)
            and isinstance(aa["authors"], list) and aa["authors"] and isinstance(aa["model_vendors"], list)
            and all(isinstance(a, dict) and set(a) == {"tool", "model", "vendor"} and all(base.is_nonempty_str(a[k]) for k in a) for a in aa["authors"])
            and all(v in C.VENDORS for v in aa["model_vendors"])):
        p.append("artifact_author shape")
    else:
        # R4-B4 整改：派生关系——Human 条目恰为三元 human、模型条目 vendor 在闭集、human_only ⇔ 全部为 Human、model_vendors = 派生集合
        human = {"tool": C.HUMAN, "model": C.HUMAN, "vendor": C.HUMAN}
        derived = []
        for a in aa["authors"]:
            if a["tool"] == C.HUMAN or a["vendor"] == C.HUMAN:
                if a != human:
                    p.append("artifact_author Human entry must be exactly {human, human, human}")
                continue
            if a["vendor"] not in C.VENDORS:
                p.append("artifact_author vendor outside the closed set")
            derived.append(a["vendor"])
        if aa["human_only"] != (not derived) or aa["model_vendors"] != sorted(set(derived)):
            p.append("artifact_author human_only / model_vendors must be derived from authors")
    ic = prof["inheritance_context"]
    if not (isinstance(ic, dict) and set(ic) == {"question_ids", "bundle_names", "task_file"} and isinstance(ic["question_ids"], list)
            and all(base.is_nonempty_str(q) for q in ic["question_ids"]) and isinstance(ic["bundle_names"], list)
            and all(base.is_nonempty_str(b) for b in ic["bundle_names"]) and base.is_nonempty_str(ic["task_file"])
            and ic["task_file"] in ic["bundle_names"]):
        p.append("inheritance_context shape")
    ru = prof["resolved_upstream"]
    if not (isinstance(ru, dict) and set(ru) == {"provider", "model", "route_visibility"}
            and all(v is None or base.is_nonempty_str(v) for v in ru.values())
            and ru["route_visibility"] in C.RESOLVED_UPSTREAM_VISIBILITIES):
        p.append("resolved_upstream shape")
    elif ru["route_visibility"] == "direct":
        if prof["route_provider_kind"] == "aggregator" or ru["provider"] != prof["route_provider"] or ru["model"] != prof["requested_model"]:
            p.append("resolved_upstream direct cross-constraint")
    elif prof["route_provider_kind"] != "aggregator" or ru["provider"] is not None or ru["model"] is not None:
        p.append("resolved_upstream pending-response cross-constraint")
    ts = prof["reviewer_tool_surface"]
    if not (isinstance(ts, dict) and set(ts) == {"web_search", "delivery"} and ts["web_search"] == "forced-off" and ts["delivery"] in C.DELIVERIES):
        p.append("reviewer_tool_surface shape")
    if not (base.hex64(prof["registry_sha256"]) and base.hex64(prof["request_sha256"]) and base.hex64(prof["input_manifest_sha256"])
            and (prof["contract_design_sha256"] is None or base.hex64(prof["contract_design_sha256"]))):
        p.append("hash field grammar")
    if not C.ROUND_RE.match(prof["round"]):
        p.append("round grammar")
    # 交叉约束：路由面按 kind 二选一
    if prof["route_provider_kind"] == "builtin-native":
        if prof["auth_mode"] != C.AUTH_MODE_BUILTIN or not base.is_nonempty_str(prof["auth_source_path"]) \
                or prof["provider_key_env"] is not None or prof["provider_base_env"] is not None:
            p.append("builtin-native auth binding cross-constraint")
    else:
        if not base.is_nonempty_str(prof["provider_key_env"]) or not base.is_nonempty_str(prof["provider_base_env"]) \
                or prof["auth_mode"] is not None or prof["auth_source_path"] is not None:
            p.append("env-keyed provider binding cross-constraint")
    if (prof["routing_mode"] == "aggregator_managed") != (prof["route_provider_kind"] == "aggregator"):
        p.append("routing_mode must match route_provider_kind")
    # 调用组：整组 null 或整组在场且 calls[] 非空；每项按 CALL_RECORD_KEYS 全字段校验（R3-B7 整改）
    cg = [prof[k] for k in CALL_GROUP_KEYS]
    if prof["calls"] is not None:
        if not (isinstance(prof["calls"], list) and prof["calls"] and all(isinstance(c, dict) for c in prof["calls"])):
            p.append("calls must be a non-empty array of objects when present")
        elif [c.get("call_index") for c in prof["calls"]] != list(range(1, len(prof["calls"]) + 1)):
            p.append("calls[].call_index must ascend from 1")
        else:
            for c in prof["calls"]:
                probs = call_record_problems(c)
                if not probs:
                    probs = call_record_kind_problems(c, prof["route_provider_kind"], prof["requested_model"])
                p.extend("calls[%s] %s" % (c["call_index"], msg) for msg in probs)
            if not p:
                # R4-B4 整改：三个归约字段与 effort_source 须恰等于按 calls[] 重算的调用组（冲突时整组 null）
                cg_expected = call_group(prof["calls"])
                for k in CALL_GROUP_KEYS[1:]:
                    if prof[k] != cg_expected[k]:
                        p.append("%s must equal the reduction of calls[] (expected %r)" % (k, cg_expected[k]))
        if prof["effective_effort"] is not None and (prof["effective_effort"] not in C.EFFORTS or prof["effort_source"] not in C.EFFORT_SOURCES):
            p.append("effective effort / source outside the closed set")
        if prof["logical_model_match"] is not None and prof["logical_model_match"] not in C.LOGICAL_MODEL_MATCHES:
            p.append("logical_model_match outside the closed set")
    elif any(v is not None for v in cg):
        p.append("call group must be entirely null before the first call")
    pg = [prof[k] for k in PUBLISHED_KEYS]
    if any(v is None for v in pg) and any(v is not None for v in pg):
        p.append("published group must be entirely null or entirely present")
    if prof["verdict_sha256"] is not None and not base.hex64(prof["verdict_sha256"]):
        p.append("verdict_sha256 grammar")
    if prof["verdict_path"] is not None and not base.is_nonempty_str(prof["verdict_path"]):
        p.append("verdict_path must be a non-empty string when present")   # R15-B1
    return p


def _opt_int(v):
    return v is None or base.is_strict_int(v)


def _int_or_unknown(v):
    return v == "unknown" or (base.is_strict_int(v) and v >= 0)


def call_record_problems(c):
    """calls[] 单项全字段校验（键闭集 + 逐键类型与值域，R3-B7 整改）。"""
    p = []
    if not isinstance(c, dict):
        return ["must be an object"]
    if set(c) != set(C.CALL_RECORD_KEYS):
        return ["key set must equal CALL_RECORD_KEYS (missing %s, unknown %s)"
                % (sorted(set(C.CALL_RECORD_KEYS) - set(c)), sorted(set(c) - set(C.CALL_RECORD_KEYS)))]
    if not (base.is_strict_int(c["call_index"]) and c["call_index"] >= 1):
        p.append("call_index must be a positive integer")
    if not (c["response_model"] is None or base.is_nonempty_str(c["response_model"])):
        p.append("response_model must be null or a non-empty string")
    if c["logical_model_match"] not in C.LOGICAL_MODEL_MATCHES:
        p.append("logical_model_match outside the closed set")
    if c["upstream_route_visibility"] not in C.ROUTE_VISIBILITIES:
        p.append("upstream_route_visibility outside the closed set")
    if c["route_provenance"] not in C.ROUTE_PROVENANCES:
        p.append("route_provenance outside the closed set")
    if c["effective_effort"] not in C.EFFORTS or c["effort_source"] not in C.EFFORT_SOURCES:
        p.append("effective_effort / effort_source outside the closed set")
    if c["provider_effective_behavior"] != C.PROVIDER_EFFECTIVE_BEHAVIOR:
        p.append("provider_effective_behavior must be %s" % C.PROVIDER_EFFECTIVE_BEHAVIOR)
    if not (c["runtime_version"] is None or base.is_nonempty_str(c["runtime_version"])):
        p.append("runtime_version must be null or a non-empty string")
    pr = c["process"]
    if not (isinstance(pr, dict) and set(pr) == set(C.PROCESS_KEYS) and _opt_int(pr["exit_code"])
            and isinstance(pr["timed_out"], bool) and isinstance(pr["stderr_summary"], str)):
        p.append("process shape")
    if not (c["usage"] == "unknown" or isinstance(c["usage"], dict)):
        p.append("usage must be an object or \"unknown\"")
    if not (_int_or_unknown(c["jsonl_event_count"]) and _int_or_unknown(c["request_count"])):
        p.append("jsonl_event_count / request_count must be a non-negative integer or \"unknown\"")
    if not (c["request_ids"] == "unknown" or (isinstance(c["request_ids"], list) and c["request_ids"]
                                               and all(base.is_nonempty_str(x) for x in c["request_ids"]))):
        p.append("request_ids must be a non-empty array of strings or \"unknown\"")
    rt = c["retry"]
    if not (isinstance(rt, dict) and set(rt) == set(C.RETRY_KEYS) and all(base.is_nonempty_str(v) for v in rt.values())):
        p.append("retry shape")
    if not _opt_int(c["http_status"]):
        p.append("http_status must be null or an integer")
    if not (c["failure"] is None or c["failure"] in C.INFRA_CLASSIFICATIONS):
        p.append("failure must be null or an infrastructure classification")
    if c["call_path_proof"] not in C.CALL_PATH_PROOFS or c["profile_binding"] not in C.PROFILE_BINDINGS:
        p.append("call_path_proof / profile_binding outside the closed set")
    if p:
        return p
    # R5-B3 整改：记录内部交叉约束（与 runtime.assess_call_path / assess_profile_binding 的派生规则一致）
    if c["failure"] is not None and c["call_path_proof"] != "NOT_PROVEN":
        p.append("a failed call must be NOT_PROVEN")
    if (c["process"]["timed_out"] or c["process"]["exit_code"] not in (None, 0)) and c["call_path_proof"] != "NOT_PROVEN":
        p.append("a timed-out or non-zero-exit call must be NOT_PROVEN")
    if c["http_status"] is not None and not (200 <= c["http_status"] < 300) and c["call_path_proof"] != "NOT_PROVEN":
        p.append("a non-2xx call must be NOT_PROVEN")
    if c["response_model"] is None:
        if c["logical_model_match"] != "unreported" or c["upstream_route_visibility"] != "unreported":
            p.append("no response_model ⇒ logical_model_match and route visibility must be unreported")
    elif c["logical_model_match"] == "unreported" or c["upstream_route_visibility"] != "reported":
        p.append("response_model present ⇒ logical_model_match decided and route visibility reported")
    if c["logical_model_match"] == "mismatch" and c["profile_binding"] != "MISMATCH":
        p.append("logical mismatch ⇒ profile_binding MISMATCH")
    if c["logical_model_match"] in ("exact", "registered_equivalent") and c["profile_binding"] != "SUFFICIENT":
        p.append("exact / registered_equivalent ⇒ profile_binding SUFFICIENT")
    return p


def call_record_kind_problems(c, kind, requested_model=None):
    """调用记录与路由提供方 kind / 请求模型的派生关系（route_provenance、未报告标识下的 profile_binding，
    response_model 与 logical_model_match 互证，R5-B3 / R6-B2 整改）。"""
    p = []
    lmm = c["logical_model_match"]
    if requested_model is not None and c["response_model"] is not None:
        if lmm == "exact" and c["response_model"] != requested_model:
            p.append("exact match requires response_model == requested_model")
        if lmm in ("registered_equivalent", "mismatch") and c["response_model"] == requested_model:
            p.append("response_model equal to requested_model is an exact match, not %s" % lmm)
    if kind != "aggregator":
        expected = "direct"
    elif lmm in ("exact", "registered_equivalent"):
        expected = lmm
    elif lmm == "mismatch":
        expected = "conflicting"
    else:
        expected = "unverifiable"
    if c["route_provenance"] != expected:
        p.append("route_provenance must be %s for kind %s and match %s" % (expected, kind, lmm))
    if lmm == "unreported" and c["profile_binding"] != ("INSUFFICIENT" if kind == "aggregator" else "SUFFICIENT"):
        p.append("unreported identity ⇒ profile_binding INSUFFICIENT for aggregators, SUFFICIENT otherwise")
    return p


def aggregate_dims(calls):
    """attempt 级 call_path_proof / profile_binding（保守聚合）；归约冲突使 profile_binding = MISMATCH。"""
    if not calls:
        return "NOT_PROVEN", "INSUFFICIENT"
    cpp = C.aggregate_call_path(c["call_path_proof"] for c in calls)
    pb = C.aggregate_profile_binding(c["profile_binding"] for c in calls)
    if reduction_conflict(calls):
        pb = "MISMATCH"
    return cpp, pb


def runtime_group(adapter, calls, previous_runtime, inline):
    """运行时组（§6.6 确定性表示）。previous_runtime = 上一轮 Receipt 的 runtime 组或 None。"""
    if not calls:
        return None
    first = calls[0]
    version = first.get("runtime_version")
    group = {"adapter": adapter,
             "tool_version": None if inline else version,
             "api_endpoint_id": version if inline else None,
             "changed_within_attempt": (len(calls) >= 2 and calls[1].get("runtime_version") != version),
             "changed_across_rounds": None}
    if previous_runtime is None:
        group["changed_across_rounds"] = None
    elif previous_runtime.get("adapter") != adapter:
        group["changed_across_rounds"] = True
    else:
        prev_v = previous_runtime.get("tool_version") if not inline else previous_runtime.get("api_endpoint_id")
        group["changed_across_rounds"] = prev_v != version
    return group


def empty_receipt():
    return {k: None for k in RECEIPT_KEYS}


def build_receipt(st):
    """由入口的 attempt 状态 st 组装 Receipt（正常写出路径）。st 字段见 review_channel.py `AttemptState`。"""
    r = empty_receipt()
    r["receipt_schema"] = C.RECEIPT_SCHEMA
    r['evidence_storage']=st.get('evidence_storage')
    r['execution']=copy.deepcopy(st.get('execution'))
    if r['execution'] is not None:
        x=r['execution'];x['failure_code']=x.get('failure_code') or st.get('failure_code')
        x['capability_suggestion']='CONFIGURED'
        clean=not st.get('diagnostics',{}).get('secret_leak_redacted') and not st.get('diagnostics',{}).get('secret_leak_in_evidence')
        if clean and x['failure_code'] is None and st['call_path_proof']=='PROVEN' and st['profile_binding']=='SUFFICIENT':
            if st['classification']=='probe_completed':x['capability_suggestion']='CALL_ONLY'
            elif st['classification']=='completed_with_valid_verdict' and st['verdict_validation']=='VALID' and st['verdict_published']:x['capability_suggestion']='REVIEW_ENABLED'
    r["attempt_id"] = st["attempt_id"]
    r["subject"] = st["routing"]["subject"]
    r["stage"] = st["routing"]["stage"]
    r["round"] = st["routing"]["round"]
    r["task_record"] = st["routing"]["task_record"]
    r["mode"] = st["mode"]
    r["receipt_phase"] = st["receipt_phase"]
    r["classification"] = st["classification"]
    r["failure_code"] = st["failure_code"]
    r["failure_call_index"] = st.get("failure_call_index")
    r["failure_message"] = C.STATIC_FAILURE_MESSAGES.get(st["failure_code"]) if st["failure_code"] else None
    r["attempt_outcome"] = st["attempt_outcome"]
    r["call_path_proof"] = st["call_path_proof"]
    r["profile_binding"] = st["profile_binding"]
    r["verdict_validation"] = st["verdict_validation"]
    r["verdict_published"] = st["verdict_published"]
    r["capability_suggestion"] = st["capability_suggestion"]
    r["formal_review_authorized_by_owner"] = st["formal"]
    r["invocation_authorization_sha256"] = st["invocation_authorization_sha256"]
    r["request_sha256"] = st["request_sha256"]
    r["diagnostics"] = st["diagnostics"]
    r["options"] = st["options"]
    r["utc"] = base.utc_now_iso()
    phase = st["receipt_phase"]
    if phase in ("sealed", "called", "completed"):
        r["input_manifest_sha256"] = st["input_manifest_sha256"]
        r["input_manifest_path"] = st["input_manifest_path"]
        r["round_budget"] = st["round_budget"]
        r["decisions_sha256"] = st["decisions_sha256"]   # r2+ 上一轮决定文件字节身份；r1 为 null（§6.12，Amendment 4 增员不换号）
        r["delivery"] = st["delivery"]
        r["effective_profile"] = build_profile(st["selected"], st["calls"] if phase != "sealed" else None,
                                               st["published"] if phase == "completed" else None)
    if phase in ("called", "completed"):
        r["runtime"] = st["runtime_group"]
        r["upstream_route_visibility"], r["route_provenance"] = route_facts(st["calls"])
        r["raw_final_message_sha256"] = st["raw_final_message_sha256"]
        r["raw_block_sha256"] = st["raw_block_sha256"]
        r["extraction"] = st["extraction"]
        r["verdict_problems"] = list(st["verdict_problems"])
        r["human_decision_required"] = st["human_decision_required"]
    if phase == "completed" and st["published"] is not None:
        r["verdict_path"] = st["published"]["verdict_path"]
        r["verdict_sha256"] = st["published"]["verdict_sha256"]
    return r


def synthesize_interrupted(att, synthesized_by):
    """补产 interrupted Receipt（§6.6 补产行）：字段只来自 attempt.json，另加恢复事实。
    `synthesized_by` = 恢复者身份对象 {pid, pid_start, attempt_id | null, role}（R1-B4 整改）。
    attempt.json 的 `effective_profile` 自 sealed 起按三组填充状态原样在场，本函数只复制、不计算（发布组恒 null）。"""
    r = empty_receipt()
    r["execution"] = copy.deepcopy(att.get("execution"))
    if r["execution"] is not None:r["execution"].update(capability_suggestion="CONFIGURED",failure_code="interrupted")
    r["evidence_storage"] = att.get("evidence_storage")
    phase = att["phase"]
    r["receipt_schema"] = C.RECEIPT_SCHEMA
    r["attempt_id"] = att["attempt_id"]
    r["subject"] = att["routing"]["subject"]
    r["stage"] = att["routing"]["stage"]
    r["round"] = att["routing"]["round"]
    r["task_record"] = att["routing"].get("task_record")
    r["mode"] = att["mode"]
    r["receipt_phase"] = phase
    r["classification"] = "interrupted"
    r["failure_code"] = "interrupted"
    r["failure_message"] = C.STATIC_FAILURE_MESSAGES["interrupted"]
    r["attempt_outcome"] = "preflight_failed"
    r["verdict_published"] = False
    r["request_sha256"] = att["request_sha256"]
    r["invocation_authorization_sha256"] = att["invocation_authorization_sha256"]
    r["formal_review_authorized_by_owner"] = att["formal_review_authorized_by_owner"]
    r["synthesized_by"] = synthesized_by
    r["utc"] = base.utc_now_iso()
    prof_rec = att.get("effective_profile") or {}
    calls = prof_rec.get("calls") or []   # R2-B4 整改：调用事实只从记录内 effective_profile.calls[] 取
    call_index = att.get("call_index")
    if phase in ("routed", "sealed"):
        dims = ("NOT_PROVEN", "INSUFFICIENT", "NOT_REACHED")
    elif phase == "calling":
        if call_index == 2 and calls:
            cpp = C.aggregate_call_path([calls[0]["call_path_proof"], "INDETERMINATE"])
            pb = C.aggregate_profile_binding([calls[0]["profile_binding"], "INSUFFICIENT"])
            dims = (cpp, pb, "NOT_REACHED")
        else:
            dims = ("INDETERMINATE", "INSUFFICIENT", "NOT_REACHED")
    elif phase == "called":
        # R3-B4 整改：called 记录已承载 attempt 级两维（调用返回后写入），补产只复制、不重算
        dims = (att["call_path_proof"], att["profile_binding"], "NOT_REACHED")
    else:  # validated
        dims = (att["call_path_proof"], att["profile_binding"], att["verdict_validation"])
    r["call_path_proof"], r["profile_binding"], r["verdict_validation"] = dims
    if phase in ("sealed", "calling", "called", "validated"):
        prof = dict(att["effective_profile"])
        if prof.get("profile_schema")=="review-channel-effective-profile/v3":
            prof.update(profile_schema=C.PROFILE_SCHEMA,execution_applicability=None)
        for k in PUBLISHED_KEYS:
            prof[k] = None
        r["effective_profile"] = prof
        r["input_manifest_sha256"] = att["input_manifest_sha256"]
        r["round_budget"] = att["round_budget"]
        r["decisions_sha256"] = att["decisions_sha256"]
        r["delivery"] = att["delivery"]
    if phase in ("called", "validated") or (phase == "calling" and call_index == 2):
        r["runtime"] = att.get("runtime_group")
        # 自查整改（r18 前）：§6.6 补产行闭集之外全部字段恒 null 且补产不计算——路由来源两字段不由 calls[] 归约，保持 null
    if phase == "validated":
        r["extraction"] = att.get("extraction")
    return r


def receipt_shape_problems(receipt):
    if not isinstance(receipt,dict):return ['Receipt must be an object']
    version=receipt.get('receipt_schema')
    if version not in C.RECEIPT_READ_SCHEMAS:return ['Receipt schema']
    # Member dispatch is owned by review_channel_execution.receipt_key_problems (v4 / v3 closed sets, v2 original read semantics).
    errors=X.receipt_key_problems(receipt)
    if errors:return errors
    errors=X.receipt_common_problems(receipt)
    if receipt['classification'] not in C.CLASSIFICATIONS:errors.append('Receipt classification')
    if receipt.get('failure_code') is not None and receipt['failure_code'] not in C.FAILURE_CODES:errors.append('Receipt failure code')
    profile=receipt.get('effective_profile')
    if profile is not None:errors.extend(profile_shape_problems(profile))
    if version==C.RECEIPT_SCHEMA:
        execution=receipt['execution'];errors.extend(X.execution_problems(execution))
        if execution is not None and not X.execution_problems(execution):
            expected=X.effective_applicability(execution)
            if not profile or profile.get('execution_applicability')!=expected:errors.append('Receipt/Profile execution identity')
    return errors
