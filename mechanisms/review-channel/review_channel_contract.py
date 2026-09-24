"""review_channel_contract — 契约常量（评审通道设计 §5.1；单一语义源）。

判词机读 schema、finding 与题号文法、叙述约定、分类闭集、失败码闭集、vendors 闭集、
Registry 与 Receipt 的闭集取值。判词模块与指令注入模块经本模块同源（设计 §5.2）。
本模块只含常量与纯函数，不导入任何契约模块。
"""
import re

# ---------------------------------------------------------------- schema 标识
REQUEST_SCHEMA = "review-channel-request/v4"   # Amendment 4 换号：review_brief.questions 与 standard_questions 退出（Amendment 2：inputs.candidates 数组）
VERDICT_SCHEMA = "review-channel-verdict/v4"   # Amendment 4 换号：question_assessments[] 成员减少（Amendment 2：candidates 必填字段）
RECEIPT_SCHEMA = "review-channel-receipt/v4"
RECEIPT_READ_SCHEMAS = ("review-channel-receipt/v2", "review-channel-receipt/v3", RECEIPT_SCHEMA)
DECISIONS_SCHEMA = "review-channel-decisions/v1"   # 决定文件（§6.12，Amendment 4）
PROFILE_SCHEMA = "review-channel-effective-profile/v4"
PROFILE_READ_SCHEMAS = ("review-channel-effective-profile/v3", PROFILE_SCHEMA)
REGISTRY_SCHEMA = "review-channel-registry/v5"
REGISTRY_READ_SCHEMAS = ("review-channel-registry/v3", "review-channel-registry/v4", REGISTRY_SCHEMA)
CHANNEL_ID = "csthink-harness-plane review-channel"
DESIGN_FILE = "HarnessPlane_Review_Channel_Design_v1.md"

# ---------------------------------------------------------------- 模型提供方标识闭集（设计 §6.11，受锁）
VENDORS = ("Anthropic", "OpenAI", "DeepSeek", "Zhipu")
HUMAN = "human"  # 保留字：不属 vendors 表、不进派生集合

# ---------------------------------------------------------------- 评审请求闭集（§6.1）
STAGES = ("task", "impl")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
MODES = ("preflight", "probe", "review")
SUBJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ROUND_RE = re.compile(r"^r[1-9][0-9]*$")
# task-artifact-schema §8.3 顶层 ID 闭集
TASK_RECORD_RE = re.compile(r"^(?:(?:feature|design|gov)-t[1-9][0-9]*|hotfix-h[0-9a-f]{64})$")
QUESTION_ID_RE = re.compile(r"^Q-[A-Z]+$")   # 五题闭集题号文法（§6.4，Amendment 4）：与旧 Q<n> / STD-<n> 不相交
RESIDUAL_ID_RE = re.compile(r"^RES-[1-9][0-9]*$")

# 五题闭集（§6.4，Amendment 4；Owner 2026-09-06 裁定 F1 / F2）：required question 集合是通道常量、按 stage 取套，
# caller 不再提交题目。task 轮逐字承接 spec.md@r5 FR-21 的五维度；impl 轮承接原 STD-1 / STD-2 / STD-3 与交付方法 §7.6 边界条。
TASK_QUESTIONS = (
    ("Q-FIDELITY", "Fidelity：Definition 是否忠实于上游任务名单项与 spec 条目"),
    ("Q-GOAL", "Goal Soundness：目标是否成立、可达"),
    ("Q-BOUNDARY", "Boundary：范围边界是否清楚，未越出名单项"),
    ("Q-ACCEPT", "Acceptability：Acceptance Criteria 是否可判定"),
    ("Q-EXEC", "Executability：按 Definition 能否施工"),
)
IMPL_QUESTIONS = (
    ("Q-CHANGE", "改动区正确性：差异块内的改动是否正确、完整地实现触发事实（Changelog / Definition）"),
    ("Q-UPSTREAM", "上游一致：改动与上游冻结件、既有 Owner 裁定不冲突，被审对象自设的规则成立"),
    ("Q-PREMISE", "前提与起点完备：被审对象所依赖的前置对象、起点步骤是否齐备"),
    ("Q-REFERENCE", "引用可解析：被审对象内每处引用在被审输入内可定位或被证据面限制声明覆盖；域一引用的修订号与任务书并列标注的现行修订号是否相符"),
    ("Q-BOUNDARY", "边界与残留：改动面未越出触发事实；差异块之外的发现只以 unchanged_region 报出"),
)
QUESTIONS_BY_STAGE = {"task": TASK_QUESTIONS, "impl": IMPL_QUESTIONS}

# 决定文件（§6.12，Amendment 4）
DECISIONS_FILE = "decisions.json"
DECISION_ACTIONS = ("approve", "fix", "skip")
DECISION_FIELDS = ("finding_id", "action", "instructions", "work_item", "owner_verbatim")
DECISIONS_FIELDS = ("decisions_schema", "subject", "stage", "round", "task_record", "verdict_sha256", "decided_at", "decisions")
WORK_ITEM_RE = re.compile(r"^[a-z0-9-]+:(OD|KB)-[0-9]+$")   # 台账项 id 文法
DISPOSITIONS_HEADING = "Dispositions (decisions.json sha256 %s)"   # 处置段首行（§6.12 其三）；%s = 决定文件字节 SHA-256 前 12 位

# ---------------------------------------------------------------- 被审输入（§6.2）
ROLES = ("review-task", "candidate", "reference")
FORBIDDEN_BUNDLE_NAMES = ("AGENTS.md", "CLAUDE.md", "CODEX.md", "GEMINI.md", ".codex", ".claude",
                          ".cursorrules", ".rules")
RENAME_PREFIX = "source--"
PREVIOUS_PREFIX = "previous--"
RESERVED_BUNDLE_PREFIXES = (PREVIOUS_PREFIX, RENAME_PREFIX)   # §6.2 第 4 条保留前缀（封闭集合，Amendment 1）
MANIFEST_NAME = "bundle_manifest.json"
# 解析域（§6.2 第 2 条，Amendment 1）：两个顶层目录名只在此消费，须逐字命中 rules_catalog.json 的 top_level_members
SPEC_CHAIN_DIR = "sdd"            # 域一 = 规格链目录（布局权威 §6.1）
MECHANISMS_DIR = "mechanisms"     # 域二 = 机制区各单元目录（布局权威 §5）
DOMAIN_DIRS = (SPEC_CHAIN_DIR, MECHANISMS_DIR)
DESIGN_MASTER_GLOB = "HarnessPlane_*_Design_v*.md"   # 机制设计正本名文法（布局权威 §5.1；与 layout 门 design-master-header 同一模式）
GOVERNANCE_DIR = "records/governance"   # 现行修订号标注的读取根（freeze-record 设计 §4.3；只标注、不校验）
# evidence_limits[].kind 闭集（§6.1，Amendment 1；R29-B2 整改）；自减重第 3 步起收为两值（repo-od-10:KB-08，Owner 裁定 F3 甲）
EVIDENCE_LIMIT_KINDS = ("exemption", "planned-location")
EVIDENCE_LIMIT_KIND_DEFAULT = "exemption"

# ---------------------------------------------------------------- 判词（§6.5）
FENCE_INFO = "review-channel-verdict"
VERDICTS = ("PASS", "FAIL")
SEVERITIES = ("blocking", "non_blocking", "human")
SEVERITY_LETTER = {"blocking": "B", "non_blocking": "N", "human": "H"}
ASSESSMENTS = ("SATISFIED", "NOT_SATISFIED", "INDETERMINATE")
DISPOSITIONS = ("RESOLVED", "UNRESOLVED", "INDETERMINATE")
ORIGINS = ("changed_region", "unchanged_region", "introduced_by_remediation", "carried_over")
FINDING_ID_RE = re.compile(r"^(R[1-9][0-9]*)-([BNH])([1-9][0-9]*)$")
VERDICT_FIELDS = (
    "verdict_schema", "verdict", "human_decision_required", "subject", "stage", "round", "candidate", "candidates",
    "question_assessments", "findings", "previous_findings_disposition",
    "accepted_residuals_acknowledged", "scope_files_read", "tools_used", "authorization_disclaimer",
)
FINDING_FIELDS = ("id", "severity", "title", "location", "origin", "summary")
LOCATION_FIELDS = ("file", "anchor")
CANDIDATE_FIELDS = ("bundle_name", "sha256")
ASSESSMENT_FIELDS = ("question_id", "assessment", "evidence_refs", "finding_ids")   # Amendment 4：四成员（legacy:OD-15 的断言两成员退役）
# 规则 14（§6.5.2，Amendment 4；Owner 裁定 B / F4 甲）：下列 origin 的 finding 不得为 blocking
NON_BLOCKING_ORIGINS = ("unchanged_region",)
DISPOSITION_FIELDS = ("id", "disposition", "evidence_refs")
# §6.4「全部字段」机械判据（Amendment 1）：带嵌套必填成员的顶层字段及其成员树，递归至 §6.5.1 声明的最深层；
# 指令注入的字段行成员名序列由此渲染，字段行校验器按同一常量核对（同源，§5.2）
VERDICT_MEMBER_TREE = {
    "candidate": {k: None for k in CANDIDATE_FIELDS},
    "candidates": {k: None for k in CANDIDATE_FIELDS},
    "question_assessments": {k: None for k in ASSESSMENT_FIELDS},
    "findings": {k: ({m: None for m in LOCATION_FIELDS} if k == "location" else None) for k in FINDING_FIELDS},
    "previous_findings_disposition": {k: None for k in DISPOSITION_FIELDS},
}

# 叙述正文的机械可恢复约定（§6.4）
NARRATIVE_VERDICT_RE = re.compile(r"^\*\*Verdict: (PASS|FAIL)\*\*\s*$")
NARRATIVE_HUMAN_RE = re.compile(r"^\*\*Human decision required: (yes|no)\*\*\s*$")
# 标题形态恒为三级（`### <id> · <severity> · <title>`，§6.4；R1-B6 整改：不再接受其他标题级别）
NARRATIVE_FINDING_HEADING_RE = re.compile(
    r"^### (R[1-9][0-9]*-[BNH][1-9][0-9]*) · (blocking|non_blocking|human) · (.+?)\s*$")
NARRATIVE_QUESTIONS_HEADING = "## Question assessments"
NARRATIVE_PREVIOUS_HEADING = "## Previous findings"
NARRATIVE_QUESTION_LINE_RE = re.compile(
    r"^- ([A-Z][A-Za-z0-9-]*): (SATISFIED|NOT_SATISFIED|INDETERMINATE)\s*$")
NARRATIVE_PREVIOUS_LINE_RE = re.compile(
    r"^- (R[1-9][0-9]*-[BNH][1-9][0-9]*): (RESOLVED|UNRESOLVED|INDETERMINATE)\s*$")
RAW_HTML_RE = re.compile(r"^ {0,3}<[A-Za-z!/?]")


def narrative_skeleton(values):
    """§6.4 叙述骨架的同源渲染函数（Amendment 2）。values 为占位形态或机读块实际值：
    {verdict, human (bool|None), findings: [(id, severity, title)] | None, questions: [(id, assessment)],
     previous: [(id, disposition)] | None, round_tag}。
    以占位值渲染即注入骨架（findings 取 None → 模板行）；以机读块实际值渲染即校验类补发的骨架。
    返回行列表：首两行、finding 小节标题行（各随一空行）、`## Question assessments` 节、r2+ `## Previous findings` 节。"""
    v = values
    lines = ["**Verdict: %s**" % (v["verdict"] if v.get("verdict") is not None else "<PASS|FAIL>")]
    human = v.get("human")
    lines.append("**Human decision required: %s**" % ("<yes|no>" if human is None else ("yes" if human else "no")))
    lines.append("")
    if v.get("findings") is None:
        lines.append("### %s-<B|N|H><n> · <blocking|non_blocking|human> · <title>" % v["round_tag"].upper())
        lines.append("<本条 finding 的叙述>")
        lines.append("")
    else:
        for fid, sev, title in v["findings"]:
            lines.append("### %s · %s · %s" % (fid, sev, title))
            lines.append("<本条 finding 的叙述>")
            lines.append("")
    lines.append(NARRATIVE_QUESTIONS_HEADING)
    for qid, assessment in v["questions"]:
        a = assessment if assessment is not None else "<SATISFIED|NOT_SATISFIED|INDETERMINATE>"
        lines.append("- %s: %s" % (qid, a))
    if v.get("previous") is not None:
        lines.append("")
        lines.append(NARRATIVE_PREVIOUS_HEADING)
        for pid, disp in v["previous"]:
            lines.append("- %s: %s" % (pid, disp if disp is not None else "<RESOLVED|UNRESOLVED|INDETERMINATE>"))
    return lines


def skeleton_values_from_block(block, required_question_ids, previous_finding_ids):
    """由机读块实际值取骨架输入（校验类补发）。缺失或非法的判断字段取占位（骨架只承载结构行，判断字段另经逐字节核对）。"""
    fs = block.get("findings") if isinstance(block.get("findings"), list) else []
    findings = [(f.get("id"), f.get("severity"), f.get("title")) for f in fs if isinstance(f, dict)]
    qa = {q.get("question_id"): q for q in (block.get("question_assessments") or []) if isinstance(q, dict)}
    questions = []
    for qid in required_question_ids:
        q = qa.get(qid, {})
        a = q.get("assessment") if q.get("assessment") in ASSESSMENTS else None
        questions.append((qid, a))
    previous = None
    if previous_finding_ids is not None:
        pd = {d.get("id"): d for d in (block.get("previous_findings_disposition") or []) if isinstance(d, dict)}
        previous = [(pid, pd.get(pid, {}).get("disposition") if pd.get(pid, {}).get("disposition") in DISPOSITIONS else None)
                    for pid in previous_finding_ids]
    return {"verdict": block.get("verdict") if block.get("verdict") in VERDICTS else None,
            "human": block.get("human_decision_required") if isinstance(block.get("human_decision_required"), bool) else None,
            "findings": findings, "questions": questions, "previous": previous, "round_tag": str(block.get("round") or "r?")}
FENCE_LINE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")

# 信封包容（§6.5.3）
EXTRACTION_TIERS = ("strict", "repaired", "reemitted", "invalid")
REPAIRS = ("missing-closing-fence", "opener-indent", "fence-length", "info-string-case",
           "trailing-content", "block-relocated")
INVARIANCE_STATUSES = ("passed", "mismatch", "not_reached")
REEMIT_RECOVERABLE_FIELDS = ("verdict", "human_decision_required", "findings", "question_assessments",
                             "previous_findings_disposition")
REEMIT_UNVERIFIABLE_FIELDS = ("location", "origin", "summary", "evidence_refs", "finding_ids",
                              "accepted_residuals_acknowledged", "scope_files_read", "tools_used",
                              "candidate", "candidates")
# 补发触发类闭集（§6.5.4，Amendment 2）：信封类 = 三级不可解析；校验类 = 可解析而校验失败且失败项全属可确定性整改项
REEMIT_TRIGGERS = ("envelope", "validation")
# §6.5.2 末段：已知值缺陷所涉字段（正确值由通道确定）与判断字段（= 全部字段减去已知值字段）
KNOWN_VALUE_FIELDS = ("verdict_schema", "authorization_disclaimer", "subject", "stage", "round", "candidate", "candidates",
                      "accepted_residuals_acknowledged")
JUDGMENT_FIELDS = tuple(f for f in VERDICT_FIELDS if f not in KNOWN_VALUE_FIELDS)
# 校验失败项分类（§6.5.2 末段闭集）：known = 已知值缺陷 · narrative = 叙述缺陷 · other = 其余
PROBLEM_CLASSES = ("known", "narrative", "other")
PROBE_PAYLOAD = "PROBE-OK"

# ---------------------------------------------------------------- Receipt 与分类（§6.6 / §6.7）
CLASSIFICATIONS = (
    "preflight_failed", "provider_unavailable", "authentication_failed", "runtime_rejected_config",
    "timeout_or_transport_failure", "reviewer_output_invalid", "probe_completed",
    "completed_with_valid_verdict", "interrupted", "cancelled_by_host", "execution_port_failure",
)
INFRA_CLASSIFICATIONS = ("provider_unavailable", "authentication_failed", "runtime_rejected_config",
                         "timeout_or_transport_failure", "cancelled_by_host", "execution_port_failure")
ATTEMPT_OUTCOMES = ("governed_verdict", "receipt_only", "preflight_failed")
CALL_PATH_PROOFS = ("PROVEN", "NOT_PROVEN", "INDETERMINATE")
PROFILE_BINDINGS = ("SUFFICIENT", "MISMATCH", "INSUFFICIENT")
VERDICT_VALIDATIONS = ("VALID", "INVALID", "NOT_REACHED")
RECEIPT_PHASES = ("routed", "sealed", "called", "completed")
ATTEMPT_PHASES = ("routed", "sealed", "calling", "called", "validated")
DELIVERIES = ("tool", "inline")
SELECTION_SOURCES = ("explicit-request", "inherited-snapshot", "system-default")
EFFORT_SOURCES = ("explicit-request", "inherited-snapshot", "registry-default")
OVERRIDE_SOURCES = ("explicit-request", "inherited-snapshot", "none")
CAPABILITY_SUGGESTIONS = ("REVIEW_ENABLED", "PROBED", "no-higher-than-UNVERIFIED",
                          "requested-profile-not-PROBED-or-ENABLED")
LOGICAL_MODEL_MATCHES = ("exact", "registered_equivalent", "unreported", "mismatch")
ROUTE_PROVENANCES = ("direct", "exact", "registered_equivalent", "conflicting", "unverifiable")
ROUTE_VISIBILITIES = ("reported", "unreported")
RESOLVED_UPSTREAM_VISIBILITIES = ("direct", "pending-response")
PROVIDER_EFFECTIVE_BEHAVIOR = "unverified"
# effective Profile calls[] 单项键闭集（§6.6 调用组；由 review_channel_runtime.build_call_record 生成、读端全字段校验，R3-B7 整改）
CALL_RECORD_KEYS = (
    "call_index", "response_model", "logical_model_match", "upstream_route_visibility", "route_provenance",
    "effective_effort", "effort_source", "provider_effective_behavior", "runtime_version", "process", "usage",
    "jsonl_event_count", "request_count", "request_ids", "retry", "http_status", "failure",
    "call_path_proof", "profile_binding",
)
PROCESS_KEYS = ("exit_code", "timed_out", "stderr_summary")
RETRY_KEYS = ("transport_retry_state", "provider_retry_state")
MODEL_IDENTITY_ASSURANCE = "operational-claim"
AUTH_MODE_BUILTIN = "login-session"

# 退出码（本通道设计 §5.3：按子命令与 attempt 分类映射，不等于判词 PASS / FAIL）
EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_INTERNAL = 2
CLASSIFICATION_EXIT = {
    "completed_with_valid_verdict": 0,
    "probe_completed": 0,
}

# ---------------------------------------------------------------- Registry（§6.11）
PROVIDER_KINDS = ("official-direct", "aggregator", "builtin-native")
TRANSPORTS = ("responses", "chat", "messages", "builtin")
STRUCTURED_OUTPUTS = ("json_schema", "json_object", "none")
CAPABILITY_STATUSES = ("REGISTERED", "CONFIGURED", "UNVERIFIED", "PROBED", "REVIEW_ENABLED")
FORMAL_REQUIRED_STATUSES = ("CONFIGURED", "UNVERIFIED", "PROBED")
DEFAULTS_KEYS = ("max_rounds", "timeout_seconds", "effort")
DIAGNOSTICS_DIR = "records/diagnostics/review-channel"
RECEIPT_REF_FIELDS = (
    "receipt_schema", "attempt_id", "subject", "stage", "round", "mode", "receipt_phase",
    "classification", "attempt_outcome", "call_path_proof", "profile_binding", "verdict_validation",
    "verdict_published", "verdict_path", "verdict_sha256", "effective_profile", "input_manifest_sha256",
)

# ---------------------------------------------------------------- 失败码闭集（preflight / 分类另记）
FAILURE_CODES = (
    "execution-port-unregistered", "execution-port-purpose-mismatch", "execution-port-profile-mismatch",
    "execution-port-binding-mismatch", "execution-port-identity-unverified", "execution-port-capability-unverified",
    "execution-port-result-unknown", "execution-port-observation-incomplete", "execution-port-integrity-mismatch",
    "definition-structure-invalid", "request-invalid", "cancelled_by_host", "execution_port_failure",
    'archive-unconfigured', 'archive-unavailable', 'archive-identity-mismatch',
    'archive-integrity-failed', 'archive-conflict', 'archive-incomplete',
    # 路由与启动
    "request-unrouteable", "internal-error", "interrupted",
    # Request 校验
    "request-schema", "request-field-not-allowed", "vendor-not-registered", "profile-unknown", "effort-unsupported",
    "invocation-authorization-required",
    # Registry
    "registry-invalid", "runtime-adapter-missing", "capability-not-configured",
    "formal-authorization-required", "receipt-ref-invalid",
    # 秘密
    "secret-missing", "secret-duplicate", "secret-dynamic-rejected", "secret-structural-collision", "auth-source-missing",
    "auth-source-malformed", "auth-source-missing-at-staging", "auth-source-malformed-at-staging",
    "zshrc-unreadable",
    # 被审输入
    "root-closure-unavailable", "reference-unresolved", "reference-ambiguous", "reference-missing",
    "candidate-unreadable", "reference-unreadable", "seal-mismatch", "history-unavailable",
    "input-capacity-exceeded", "baseline-unrecoverable", "bundle-name-collision", "duplicate-reference-source",
    # 资格 / 选择 / 继承
    "eligibility", "inherit-unanchored", "inherit-unmaterializable", "round-gap-unexplained",
    "round-not-increasing", "round_budget_exhausted", "round-in-progress",
    # 决定文件（§6.12，Amendment 4）
    "decisions-missing", "decisions-invalid", "decisions-mismatch",
    # 运行时（codex 适配器）
    "runtime-key-protected", "runtime-key-unrecognized", "runtime-recognition-indeterminate",
    "runtime-binary-missing", "runtime-adapter-unmaterialized",
    # 调用结局（基础设施四类同名失败码，Receipt 另记 failure_call_index）
    "provider_unavailable", "authentication_failed", "runtime_rejected_config", "timeout_or_transport_failure",
    "empty-output", "profile-binding", "wrapper-invalid", "reemit-disabled", "verdict-invalid",
    "probe-payload-mismatch", "secret-in-input",
    # 输出与发布
    "reemit-unparseable", "reemit-invariance-mismatch", "secret-leak", "tracked-round-dir-occupied",
    "publish-rename-failed", "receipt-backlink-failed", "liveness-undeterminable",
    "attempt-alloc-failed",
)

# 早期 Receipt 消毒：按失败码键控的静态消息（§6.1）
STATIC_FAILURE_MESSAGES = {code: "preflight rejected: %s" % code for code in FAILURE_CODES}
STATIC_FAILURE_MESSAGES["interrupted"] = "attempt process terminated before writing its Receipt"
STATIC_FAILURE_MESSAGES["internal-error"] = "channel internal error"


def finding_letter(severity):
    return SEVERITY_LETTER[severity]


def required_question_ids(stage):
    """该 stage 的 required question id 序列（五题闭集常量，§6.4）；stage 表外即 KeyError（请求校验已封闭 stage 闭集）。"""
    return [q[0] for q in QUESTIONS_BY_STAGE[stage]]


def capability_transition(call_path_proof, profile_binding, verdict_validation):
    """状态转换固定表（§6.11 四行互斥穷举）。返回 (attempt_outcome, capability_suggestion)。"""
    if call_path_proof in ("NOT_PROVEN", "INDETERMINATE"):
        return "receipt_only", "no-higher-than-UNVERIFIED"
    if call_path_proof == "PROVEN" and profile_binding in ("MISMATCH", "INSUFFICIENT"):
        return "receipt_only", "requested-profile-not-PROBED-or-ENABLED"
    if call_path_proof == "PROVEN" and profile_binding == "SUFFICIENT":
        if verdict_validation == "VALID":
            return "governed_verdict", "REVIEW_ENABLED"
        if verdict_validation in ("INVALID", "NOT_REACHED"):
            return "receipt_only", "PROBED"
    raise ValueError("combination outside the frozen table: %s/%s/%s"
                     % (call_path_proof, profile_binding, verdict_validation))


def aggregate_call_path(values):
    """attempt 级保守聚合（§6.6）：全 PROVEN 才 PROVEN；任一 NOT_PROVEN 即 NOT_PROVEN；否则 INDETERMINATE。"""
    values = list(values)
    if values and all(v == "PROVEN" for v in values):
        return "PROVEN"
    if any(v == "NOT_PROVEN" for v in values):
        return "NOT_PROVEN"
    return "INDETERMINATE"


def aggregate_profile_binding(values):
    values = list(values)
    if any(v == "MISMATCH" for v in values):
        return "MISMATCH"
    if any(v == "INSUFFICIENT" for v in values):
        return "INSUFFICIENT"
    return "SUFFICIENT"


def reduce_single_valued(calls, key):
    """单值消费端归约（§6.6）：全部相等取该值，否则 None。"""
    vals = [c.get(key) for c in calls]
    if not vals:
        return None
    first = vals[0]
    return first if all(v == first for v in vals) else None


LEGACY_RECEIPT_KEYS = (
    "receipt_schema", "attempt_id", "subject", "stage", "round", "task_record", "mode", "receipt_phase",
    "classification", "failure_code", "failure_call_index", "failure_message", "attempt_outcome", "call_path_proof", "profile_binding",
    "verdict_validation", "verdict_published", "capability_suggestion", "input_manifest_sha256",
    "input_manifest_path", "verdict_path", "verdict_sha256", "raw_final_message_sha256", "raw_block_sha256",
    "extraction", "verdict_problems", "human_decision_required", "delivery", "decisions_sha256",
    "effective_profile", "round_budget", "formal_review_authorized_by_owner",
    "invocation_authorization_sha256", "request_sha256", "runtime", "upstream_route_visibility", "route_provenance",
    "diagnostics", "options", "synthesized_by", "utc",
)


RECEIPT_KEYS = LEGACY_RECEIPT_KEYS + ("evidence_storage", "execution")

# Old physical versions keep their original closed sets and maintenance scope.
PRODUCT_FAILURE_CODES = tuple(code for code in FAILURE_CODES if code.startswith('execution-port-')) + ('definition-structure-invalid','request-invalid','cancelled_by_host','execution_port_failure')
LEGACY_CLASSIFICATIONS = tuple(v for v in CLASSIFICATIONS if v not in ('cancelled_by_host','execution_port_failure'))
LEGACY_FAILURE_CODES = tuple(v for v in FAILURE_CODES if v not in PRODUCT_FAILURE_CODES)
