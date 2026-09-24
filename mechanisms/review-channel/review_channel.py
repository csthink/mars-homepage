"""review_channel — 评审通道唯一入口（评审通道设计 §5.3；子命令解析、模块装配、退出码）。

调用形态（本通道设计 §5.3 的净化启动）：
  "$(git config --get gates.interpreter)" -B -E -s -S -X pycache_prefix="$(mktemp -d)" \
      mechanisms/review-channel/review_channel.py <preflight|probe|review|selftest> --request <绝对路径> [选项]
      mechanisms/review-channel/review_channel.py respond --decisions <决定输入绝对路径> [选项]

选项：--no-reemit（关闭补发，记入 Receipt）
      --repo-root <路径>（默认从可执行物位置向上解析）
`register` 子命令及其开关自减重第 3 步退役（设计 §8.4，Amendment 4）：发布之后通道不执行任何 Git 动作。
      无注入口：Registry 与适配器集恒取自 --repo-root 所指仓根的 mechanisms/review-channel/，配置正本恒取自当前 HOME 的 ~/.zshrc（自测以隔离仓 + 隔离 HOME 实撞）

通道不是门：退出码 0 / 1 / 2 的含义与净化启动由本通道设计 §5.3 定义，判词 PASS / FAIL 另由 Receipt 承载。
"""
import errno
from functools import wraps
from contextlib import contextmanager
import threading
import fcntl
import json
import os
import re
import sys
import tempfile

import review_evidence as E
import review_channel_base as base
import review_channel_contract as C
import review_channel_execution as X
import review_channel_decisions as D
import review_channel_inputs as I
import review_channel_instruction as N
import review_channel_receipt as RC
import review_channel_registry as G
import review_channel_request as R
import review_channel_runtime as RT
import review_channel_secrets as S
import review_channel_selection as SL
import review_channel_taskbook as T
import review_channel_history as H
import review_channel_verdict as V

SUBCOMMANDS = ("preflight", "probe", "review", "respond", "selftest", "archive")
CONTRACT_VERSION = ("review-channel design v1 (request %s / verdict %s / receipt %s / profile %s / registry %s)"
                    % (C.REQUEST_SCHEMA, C.VERDICT_SCHEMA, C.RECEIPT_SCHEMA, C.PROFILE_SCHEMA, C.REGISTRY_SCHEMA))
LOCK_NAME = ".in-progress"
ATTEMPT_JSON = "attempt.json"
REQUEST_JSON = "request.json"
RECEIPT_JSON = "receipt.json"
STAGING_PREFIX = "publish.tmp-"
ALLOC_PREFIX = ".alloc-"
RECEIPT_TMP_PREFIX = "receipt.json.tmp-"
UNKNOWN_TOKEN = "unknown"  # 名字内令牌不可得的标记：所有者恒不可判，永不清理

_ROUND_LOCK = threading.local()  # 每个 worker 的当前 attempt 作用域各持一个 fd


# ---------------------------------------------------------------- 报告

_OUTPUT_SCRUB = threading.local()   # 秘密解析后由入口注入扫描集：此后 stderr / stdout 全部经脱敏（R11-B2 整改）


def _scrubbed(text):
    scan = getattr(_OUTPUT_SCRUB,"scan",None)
    if scan is None:
        return text
    values = scan()
    data, _hit = base.scrub_secrets(text.encode("utf-8"), values)
    if base.contains_secret(data, values):
        # 不动点未达（preflight 保证下不可达）：整条输出以替代文本代替；替代文本同样终检（R21-B1），仍命中即输出空串
        fallback = base.REDACTED_OUTPUT
        return "" if base.contains_secret(fallback, values) else fallback.decode("ascii")
    return data.decode("utf-8", "replace")


def _stderr(msg):
    text=_scrubbed(msg.rstrip("\n"))
    sink=getattr(_OUTPUT_SCRUB,"diagnostic_sink",None)
    if sink is not None:sink(text)
    else:sys.stderr.write(text+"\n")


def _report(obj):
    text=_scrubbed(json.dumps(obj,ensure_ascii=False,indent=2))
    sink=getattr(_OUTPUT_SCRUB,"report_sink",None)
    if sink is not None:
        if text:sink(json.loads(text))
    else:sys.stdout.write(text+"\n")


def output_scope(fn):
    @wraps(fn)
    def run(mode,opts):
        previous=dict(vars(_OUTPUT_SCRUB))
        _OUTPUT_SCRUB.scan=None
        _OUTPUT_SCRUB.report_sink=getattr(opts,'report_sink',None)
        _OUTPUT_SCRUB.diagnostic_sink=getattr(opts,'diagnostic_sink',None)
        try:return fn(mode,opts)
        finally:
            vars(_OUTPUT_SCRUB).clear();vars(_OUTPUT_SCRUB).update(previous)
    return run


REGISTRY_REL = "mechanisms/review-channel/review_channel_registry.json"   # Registry 与适配器集恒取自被审仓根（R10-B1 整改）
ADAPTERS_REL = "mechanisms/review-channel/adapters"


class Options:
    def __init__(self):
        self.request = None
        self.decisions = None   # respond：决定输入绝对路径
        self.repo_root = base.DEFAULT_REPO_ROOT
        self.reemit = True
        self.execution_context = None  # trusted product injection only; no CLI/JSON switch

    # 派生路径（不可由命令行替换，R10-B1 整改）：Registry 与适配器集 = 被审仓根内受治理的单元文件；配置正本 = 当前 HOME 的 ~/.zshrc
    @property
    def registry(self):
        return os.path.join(self.repo_root, REGISTRY_REL)

    @property
    def adapters_dir(self):
        return os.path.join(self.repo_root, ADAPTERS_REL)

    @property
    def zshrc(self):
        return S.ZSHRC_PATH


def parse_args(argv):
    if not argv or argv[0] not in SUBCOMMANDS:
        raise base.ChannelError("usage", "subcommand must be one of %s" % ", ".join(SUBCOMMANDS))
    sub = argv[0]
    if sub=='archive':
        import argparse
        parser=argparse.ArgumentParser(prog='review_channel.py archive')
        parser.add_argument('operation',choices=('verify','restore','recover'))
        parser.add_argument('--ref');parser.add_argument('--attempt');parser.add_argument('--destination');parser.add_argument('--repo-root',default=base.DEFAULT_REPO_ROOT)
        try: opts=parser.parse_args(argv[1:])
        except SystemExit as exc:raise base.ChannelError('usage','archive arguments invalid') from exc
        if opts.operation in ('verify','restore') and (not opts.ref or not os.path.isabs(opts.ref)):
            raise base.ChannelError('usage','--ref requires an absolute JSON file')
        if opts.operation=='restore' and not opts.destination:raise base.ChannelError('usage','restore requires --destination')
        if opts.operation=='recover' and not opts.attempt:raise base.ChannelError('usage','recover requires --attempt')
        return sub,opts,[]
    opts = Options()
    i = 1
    rest = []
    while i < len(argv):
        a = argv[i]
        if a in ("--request", "--repo-root", "--decisions") and i + 1 >= len(argv):
            raise base.ChannelError("usage", "%s requires an operand" % a)   # R11-B3：缺操作数是受控 usage 失败，不是 traceback
        if a == "--request":
            opts.request = argv[i + 1]; i += 2
        elif a == "--decisions":
            opts.decisions = argv[i + 1]; i += 2
        elif a == "--repo-root":
            opts.repo_root = os.path.realpath(argv[i + 1]); i += 2
        elif a == "--no-reemit":
            opts.reemit = False; i += 1
        else:
            rest.append(a); i += 1
    if sub != "selftest":
        if rest:
            raise base.ChannelError("usage", "unknown argument %r" % rest[0])
        if sub == "respond":
            if opts.request is not None:
                raise base.ChannelError("usage", "respond takes --decisions, not --request")
            if not opts.decisions or not os.path.isabs(opts.decisions):
                raise base.ChannelError("usage", "--decisions <absolute path> is required")
        else:
            if opts.decisions is not None:
                raise base.ChannelError("usage", "--decisions is only accepted by respond")
            if not opts.request or not os.path.isabs(opts.request):
                raise base.ChannelError("usage", "--request <absolute path> is required")
    return sub, opts, rest


# ---------------------------------------------------------------- 落点派生（§8.1）

def derive_routing(route):
    axis = "task" if route["task_record"] else "subject"
    stage, rnd, subject = route["stage"], route["round"], route["subject"]
    if axis == "task":
        tr = route["task_record"]
        tracked = "tasks/%s/reviews/%s-%s" % (tr, stage, rnd)
        attempts = "tasks/%s/attempts/%s-%s" % (tr, stage, rnd)
    else:
        tracked = "reviews/%s/%s" % (subject, rnd)
        attempts = "review-attempts/%s/%s" % (subject, rnd)
    return {"subject": subject, "stage": stage, "round": rnd, "round_index": R.round_index(rnd),
            "task_record": route["task_record"], "axis": axis, "tracked_round_dir": tracked,
            "attempt_round_dir": attempts, "tracked_parent": tracked.rsplit("/", 1)[0]}


def previous_attempt_round_dir(routing, k):
    if routing["axis"] == "task":
        return "tasks/%s/attempts/%s-r%d" % (routing["task_record"], routing["stage"], k)
    return "review-attempts/%s/r%d" % (routing["subject"], k)


# ---------------------------------------------------------------- 崩溃遗留与补产（§8.2 第 6 步）

def _owner_from_name(name, prefix):
    """`.alloc-<attempt_id>-<pid>-<token>` 与 `receipt.json.tmp-<pid>-<token>` 的所有者解析；返回 (id, pid, token) 或 None。"""
    rest = name[len(prefix):]
    if prefix == ALLOC_PREFIX:
        parts = rest.rsplit("-", 2)
        if len(parts) != 3:
            return None
        ident, pid, token = parts
    else:
        parts = rest.split("-", 1)
        if len(parts) != 2:
            return None
        ident, (pid, token) = None, parts
    try:
        return ident, int(pid), token
    except ValueError:
        return None


def _liveness(pid, token):
    """attempt.json 所记原始令牌的存活判定。"""
    return base.owner_alive(pid, token)


def _liveness_named(pid, token_safe):
    """目录名 / 临时文件名内嵌的净化令牌（_safe_token 形态）的存活判定；令牌为 UNKNOWN_TOKEN 即不可判。"""
    if token_safe == UNKNOWN_TOKEN:
        return None
    state = base.process_state(pid)
    if state is None:
        return None
    if state in ("absent", "zombie"):
        return False
    current = base.process_start_token(pid)
    if current is None:
        return None
    return _safe_token(current) == token_safe


def preflight_cleanup(repo_root, routing, recoverer):
    """对 attempt_round_dir 内遗留物按所有者存活性处置（§8.2 第 5 / 6 步）：已发布 attempt 补硬链接、死所有者遗留删除、
    interrupted 补产，全部为 preflight 前置专属（R6-B1 整改的 backlink_only 范围随 register 退役而消失，Amendment 4）。返回报告列表。"""
    report = []
    ard = os.path.join(repo_root, routing["attempt_round_dir"])
    if not os.path.isdir(ard):
        return report
    tracked_receipt = os.path.join(repo_root, routing["tracked_round_dir"], "receipt-%s.json" % routing["round"])
    archived = H.round_files(repo_root, routing["tracked_round_dir"])
    archived_receipt = archived.get(routing["tracked_round_dir"]+"/receipt-%s.json" % routing["round"]) if archived else None
    tracked_attempt_id = None
    if archived_receipt is not None:
        tracked_attempt_id = base.strict_json_load(archived_receipt["content"]).get("attempt_id")
    elif os.path.isfile(tracked_receipt):
        try:
            tracked_attempt_id = base.strict_json_load(base.read_bytes(tracked_receipt)).get("attempt_id")
        except (ValueError, UnicodeDecodeError, AttributeError):
            tracked_attempt_id = None
    for entry in sorted(os.scandir(ard), key=lambda e: e.name):
        if not entry.is_dir(follow_symlinks=False):
            continue
        name = entry.name
        if name.startswith(ALLOC_PREFIX):
            owner = _owner_from_name(name, ALLOC_PREFIX)
            if owner is None:
                continue
            alive = _liveness_named(owner[1], owner[2])
            if alive is False:
                _cleanup_remove(report, base.remove_tree, entry.path, {"action": "alloc-removed", "name": name})
            elif alive is None:
                report.append({"action": "liveness-undeterminable", "name": name})
            continue
        att_path = os.path.join(entry.path, ATTEMPT_JSON)
        if not os.path.isfile(att_path):
            continue
        try:
            att = base.strict_json_load(base.read_bytes(att_path))
        except (ValueError, UnicodeDecodeError):
            continue
        pid, token = att.get("pid"), att.get("pid_start")
        alive = _liveness(pid, token) if isinstance(pid, int) else False
        # 暂存与临时 Receipt：所有者不存活即删（与 Receipt 无关）
        for sub in os.scandir(entry.path):
            if sub.name.startswith(STAGING_PREFIX) and sub.is_dir(follow_symlinks=False) and alive is False:
                _cleanup_remove(report, base.remove_tree, sub.path, {"action": "staging-removed", "attempt": name})
            if sub.name.startswith(RECEIPT_TMP_PREFIX) and sub.is_file(follow_symlinks=False):
                owner = _owner_from_name(sub.name, RECEIPT_TMP_PREFIX)
                if owner is not None:
                    o_alive = _liveness_named(owner[1], owner[2])
                    if o_alive is False:
                        _cleanup_remove(report, os.unlink, sub.path, {"action": "receipt-tmp-removed", "attempt": name})
        receipt_path = os.path.join(entry.path, RECEIPT_JSON)
        if os.path.exists(receipt_path):
            continue
        if alive is None:
            report.append({"action": "liveness-undeterminable", "attempt": name})
            continue
        if alive:
            continue
        if tracked_attempt_id == name and (archived_receipt is not None or os.path.isfile(tracked_receipt)):
            try:
                if archived_receipt is not None:
                    base.write_then_link(receipt_path, archived_receipt["content"], receipt_path+".history-tmp")
                else:
                    os.link(tracked_receipt, receipt_path)
                report.append({"action": "backlinked", "attempt": name})
            except FileExistsError:
                pass
            except OSError as exc:
                report.append({"action": "receipt-backlink-failed", "attempt": name, "error": type(exc).__name__})
            continue
        receipt = RC.synthesize_interrupted(att, recoverer)
        data = base.pretty_json(receipt)
        own_token = base.process_start_token(os.getpid())
        tmp_name = "%s%d-%s" % (RECEIPT_TMP_PREFIX, os.getpid(), _safe_token(own_token) if own_token else UNKNOWN_TOKEN)
        ok = base.write_then_link(receipt_path, data, tmp_name)
        report.append({"action": "interrupted-synthesized" if ok else "interrupted-already-present", "attempt": name})
    return report


def _cleanup_remove(report, fn, path, entry):
    """前置清理的删除动作收口（R5-B3 整改）：并发恢复者先删 = 目标已达（只记事实）；其他 OSError 只记报告、不逃逸。"""
    try:
        fn(path)
    except FileNotFoundError:
        report.append(dict(entry, action=entry["action"] + "-by-concurrent-recoverer"))
        return
    except OSError as exc:
        report.append(dict(entry, action=entry["action"].replace("removed", "remove-failed"), error=type(exc).__name__))
        return
    report.append(entry)


def _safe_token(text):
    return "".join(c if c.isalnum() else "_" for c in text)[:48]


def _recoverer_identity():
    """恢复者身份（§6.6 补产行：pid + pid_start + 所属 attempt_id 或 preflight 标识）。前置清理先于 attempt 分配，恒为 preflight 身份。"""
    return {"pid": os.getpid(), "pid_start": base.process_start_token(os.getpid()), "attempt_id": None, "role": "preflight"}


# ---------------------------------------------------------------- attempt 分配与阶段状态记录（§6.1）

# 治理 JSON 工件的结构字节（成员名与 JSON 字面量）：与之重叠的秘密无法受值级脱敏保护（r18 自查整改，R18-B1 线索）
STRUCTURAL_TOKENS = tuple(sorted(set(
    RC.RECEIPT_KEYS + RC.PROFILE_KEYS + (
        "attempt_id", "mode", "pid", "pid_start", "at", "phase", "routing", "request_sha256", "invocation_authorization_sha256",
        "formal_review_authorized_by_owner", "effective_profile", "input_manifest_sha256", "round_budget", "decisions_sha256",
        "delivery", "call_index", "runtime_group", "call_path_proof", "profile_binding", "verdict_validation", "extraction",
        "subject", "stage", "round", "task_record", "axis", "tracked_round_dir", "attempt_round_dir",
        "human_only", "authors", "model_vendors", "tool", "model", "vendor", "provider", "route_visibility",
        "question_ids", "bundle_names", "task_file", "web_search",
        "max_rounds", "source", "extensions", "allowed_rounds", "round_index", "exhausted", "extensions_added", "submitted_rounds",
        "authorized_by", "added_rounds", "note",
        "tier", "repairs", "reemit_raw_message_sha256", "reemit_unverifiable_fields", "invariance_check", "status", "mismatched_fields",
        "reemit_trigger", "reemit_reasons", "candidates", "path", "bytes", "sha256",
        "adapter", "tool_version", "api_endpoint_id", "changed_within_attempt", "changed_across_rounds",
        "artifacts", "secret_leak_redacted", "secret_leak_in_evidence", "request_secret_hit_at_allocation", "environment_report",
        "adapter_preflight", "cleanup", "action", "name", "attempt", "error", "reemit_enabled", "role",
        "response_model", "logical_model_match", "upstream_route_visibility", "route_provenance", "effective_effort", "effort_source",
        "provider_effective_behavior", "runtime_version", "process", "usage", "jsonl_event_count", "request_count", "request_ids",
        "retry", "http_status", "failure", "exit_code", "timed_out", "stderr_summary",
        "null", "true", "false",
    ))))
# pretty_json 输出里成员名之外的结构流只含：空白、`,:{}[]`、数字字符与 JSON 字面量 null / true / false 的字母
_QUOTE_FREE_STREAM_RE = re.compile(r"[\s,:{}\[\]0-9.+\-eEnultrfas]*")
_REDACTED_TEXT = base.REDACTED.decode("ascii")
_REDACTED_OUTPUT_TEXT = base.REDACTED_OUTPUT.decode("ascii")


def _generated_bytes_hit(text):
    """text 是否可能是脱敏算法自身生成字节的子串（R20-B1 / R21-B1 闭包）。生成字节语言 = 连续哨兵 S^k（相邻秘密各自替换后
    紧邻）、其后可接去重后缀 `#n`（scrub_json 同名成员），以及输出层替代文本 REDACTED_OUTPUT。判据：text 落在 S^k 内
    （k 取 len(text) // len(S) + 2 已足以覆盖任意跨界子串）；或 text = a + b，a 为 S^k 的某后缀（可空）、b = "#" + 纯数字（数字可空）；
    或 text 落在替代文本内。被拒绝的秘密不可能由替换再生，且每次替换至少消耗一个非生成字节（scrub_secrets 趟数界的依据）。"""
    rep = _REDACTED_TEXT * (len(text) // len(_REDACTED_TEXT) + 2)
    if text in rep or text in _REDACTED_OUTPUT_TEXT:
        return True
    for i in range(len(text) + 1):
        a, b = text[:i], text[i:]
        if (a == "" or rep.endswith(a)) and b.startswith("#") and b[1:].isdigit() or (a == "" or rep.endswith(a)) and b == "#":
            return True
    return False
# 诊断层 artifact 名（diagnostics.artifacts 的成员名，含补发前缀形态）——preflight 可预知的动态成员名之一（R19-B1）
ARTIFACT_NAMES = tuple(prefix + name for prefix in ("", "reemit-")
                       for name in ("reviewer_last_message.md", "runtime_events.jsonl", "stderr.log", "response.json", "request_shape.json"))


def _member_names(obj, out):
    """递归收集 JSON 对象内全部成员名（preflight 可预知的动态成员名来源：环境状态报告、适配器事实、sanitized overrides）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                out.add(k)
            _member_names(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _member_names(v, out)
    return out


def _structural_collisions(scan_values, extra_names=()):
    """返回值级脱敏后仍可能出现在治理 JSON 工件字节里的秘密值（bytes）。序列化后字符串值内的秘密恒被替换、且值内的
    `"` 恒转义为 `\\"`，故秘密只可能经四条途径留在字节流中（r18 R18-B1 整改，按构造逐一列举）：
    其一，含裸 `"`（只可能匹配结构流）；其二，可能是脱敏输出自身生成字节（哨兵、哨兵#n）的子串（替换结果会重新含秘密，R20-B1）；
    其三，是某成员名带引号形态 `"k"` 的子串（含裸成员名；成员名 = 固定 schema 成员名 ∪ extra_names 所给的现场动态成员名，R19-B1）；
    其四，只由结构流字符构成（空白、标点、数字字符、字面量字母）。"""
    names = set(STRUCTURAL_TOKENS) | set(extra_names)
    out = []
    for v in scan_values:
        try:
            text = v.decode("utf-8") if isinstance(v, bytes) else v
        except UnicodeDecodeError:
            continue
        if not text:
            continue
        if ('"' in text or _generated_bytes_hit(text) or any(text in '"%s"' % tok for tok in names)
                or _QUOTE_FREE_STREAM_RE.fullmatch(text)):
            out.append(v if isinstance(v, bytes) else v.encode("utf-8"))
    return out


class Attempt:
    def __init__(self, repo_root, routing, mode, raw_request, opts):
        self.repo_root = repo_root
        self.routing = routing
        self.mode = mode
        self.raw = raw_request   # request.json 恒为 Request 原始字节 exact copy（§8.1；gov-t8:OD-09 乙案，RES-4）
        self.opts = opts
        self.pid = os.getpid()
        self.pid_start = base.process_start_token(self.pid)  # None = 不可得，记录为 null，恒不可判（R2-B9）
        self.attempt_id = base.new_attempt_id()
        self.archive = getattr(opts,'archive',None)
        self.attempt_round_dir = os.path.join(repo_root, routing["attempt_round_dir"])
        if self.archive is not None:self.attempt_round_dir=str(self.archive.primary/'pending')
        self.dir = os.path.join(self.attempt_round_dir, self.attempt_id)
        self.request_sha256 = base.sha256_bytes(raw_request)
        self.record = None
        self.st = None
        self.scan_set = None  # 由入口在秘密解析后注入（callable → 扫描集）

    def allocate(self, invocation_sha, formal):
        os.makedirs(self.attempt_round_dir, exist_ok=True)
        if self.archive:E.sync_ancestors(self.attempt_round_dir,self.archive.primary)
        alloc = os.path.join(self.attempt_round_dir, "%s%s-%d-%s" % (ALLOC_PREFIX, self.attempt_id, self.pid,
                                                                       _safe_token(self.pid_start) if self.pid_start else UNKNOWN_TOKEN))
        os.makedirs(alloc, mode=0o700)
        base.write_new(os.path.join(alloc, REQUEST_JSON), self.raw)
        self.record = {
            "attempt_id": self.attempt_id, "mode": self.mode, "pid": self.pid, "pid_start": self.pid_start,
            "at": base.utc_now_iso(), "phase": "routed",
            "routing": {k: self.routing[k] for k in ("subject", "stage", "round", "task_record", "axis",
                                                     "tracked_round_dir", "attempt_round_dir")},
            "request_sha256": self.request_sha256, "invocation_authorization_sha256": invocation_sha,
            "formal_review_authorized_by_owner": formal,
        }
        base.write_new(os.path.join(alloc, ATTEMPT_JSON), base.pretty_json(self.record))
        base.fsync_dir(alloc)
        os.rename(alloc, self.dir)
        base.fsync_dir(self.attempt_round_dir)

    def advance(self, phase, **fields):
        """阶段状态记录原子重写；落盘前经秘密扫描（R2-B2 整改：记录内的调用事实不得先于终检持久化）。
        脱敏为结构感知（只改字符串值，成员名与形状不动；r18 自查整改）。"""
        self.record["phase"] = phase
        self.record["at"] = base.utc_now_iso()
        self.record.update(fields)
        scan = self.scan_set() if self.scan_set is not None else []
        clean, _hit = base.scrub_json(self.record, scan)
        data = base.pretty_json(clean)
        if base.contains_secret(data, scan):
            # 序列化后二次终检（R20-B1）：可生成字节与结构字节的碰撞已在 preflight 拒绝，此处不可达；仍 fail closed，不写含秘密的记录
            raise base.ChannelError("internal-error", "secret material survives scrubbing in the attempt record")
        base.write_atomic_replace(os.path.join(self.dir, ATTEMPT_JSON), data)


@contextmanager
def round_lock_scope():
    if getattr(_ROUND_LOCK, "fd", None) is not None:
        raise base.ChannelError("round-in-progress", "another attempt owns this worker lock")
    try:
        yield
    finally:
        fd = getattr(_ROUND_LOCK, "fd", None)
        if fd is not None:
            _ROUND_LOCK.fd = None
            os.close(fd)


def acquire_round_lock(attempt_round_dir):
    """同轮建议性文件锁：当前 attempt 内只获取一次，结束由 finally 释放；EWOULDBLOCK ⇔ 另一进程正持有。返回 True / False。"""
    if getattr(_ROUND_LOCK, "fd", None) is not None:
        raise base.ChannelError("internal-error", "round lock acquired twice in one attempt")
    path = os.path.join(attempt_round_dir, LOCK_NAME)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            try:
                holder = os.read(fd, 4096).decode("utf-8", "replace")
            except OSError:
                holder = ""
            os.close(fd)
            _stderr("round lock held by another channel process; last holder note (diagnostic only): %s" % holder.strip())
            return False
        os.close(fd)
        raise
    _ROUND_LOCK.fd = fd
    return True


def note_lock_holder(attempt):
    fd = getattr(_ROUND_LOCK, "fd", None)
    if fd is None:
        return
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, base.pretty_json({"attempt_id": attempt.attempt_id, "pid": attempt.pid,
                                       "pid_start": attempt.pid_start, "at": base.utc_now_iso()}))
    except OSError:
        pass


# ---------------------------------------------------------------- preflight 检查清单

class Preflight:
    """三模式同一套检查清单；结果汇集在 self.* 供封存、调用与 Receipt 消费。"""

    def __init__(self, repo_root, routing, mode, request_obj, opts, workdir):
        self.repo_root = repo_root
        self.routing = routing
        self.mode = mode
        self.opts = opts
        self.workdir = workdir
        self.request = R.validate_request(request_obj, mode)
        try:
            self.archive=E.configured(repo_root,writable=True)
            expected='review-channel-request/v5' if self.archive else C.REQUEST_SCHEMA
            E.require(request_obj.get('request_schema')==expected,'request schema does not match storage mode','archive-unconfigured')
        except E.EvidenceError as exc:raise base.PreflightError(exc.code,exc.message) from exc
        self.report = {}

    def run(self):
        rq = self.request
        root = self.repo_root
        # Validate the exact bytes which reference resolution and seal_inputs consume.
        self.candidates_rel=list(rq["inputs"]["candidates"])
        self.candidate_bytes={path:I.read_repo_file(root,path,"candidate") for path in self.candidates_rel}
        X.check_definition(root,rq,self.candidate_bytes)
        self.history_view = H.helper().head_commit(root)
        # Registry 与适配器
        loader = lambda rid: RT.load_adapter(rid, self.opts.adapters_dir)  # noqa: E731
        self.reg = G.load_registry(self.opts.registry, adapter_loader=loader, repo_root=root, profile_checker=RC.profile_shape_problems)
        defaults = self.reg["registry"]["defaults"]
        # 轮目录、序号、额度
        rounds = SL.scan_rounds(root, self.routing["tracked_parent"], self.routing["axis"], self.routing["stage"])
        latest_submitted = SL.check_round_sequence(root, self.routing, rounds)
        self.budget = SL.round_budget(rq, defaults, rounds)
        if self.budget["exhausted"]:
            last_blocking = self._last_round_blocking(rounds)
            raise base.PreflightError(
                "round_budget_exhausted",
                "submitted rounds %d >= allowed rounds %d (max_rounds %d from %s + extensions %d); last round blocking "
                "findings: %s; to continue, append a Human authorization record to request.round_extensions "
                "{authorized_by, at, added_rounds, note}" % (self.budget["submitted_rounds"], self.budget["allowed_rounds"],
                                                           self.budget["max_rounds"], self.budget["source"],
                                                           self.budget["extensions_added"], last_blocking))
        # 上一轮锚定
        self.previous = None
        self.previous_machine = None
        self.decisions = None
        self.decisions_sha256 = None
        if rq["round_index"] >= 2:
            self.previous = SL.load_previous(root, rq, self.routing, latest_submitted, receipt_checker=RC.receipt_shape_problems)
            try:
                self.previous_machine, _jb, _narr = V.parse_published(self.previous["verdict_bytes"])
            except (ValueError, UnicodeDecodeError) as exc:
                raise base.PreflightError("inherit-unanchored", "previous verdict does not satisfy the strict grammar: %s" % exc)
            self._load_decisions(root, rq)
        # 选择、资格、准入
        self.sel = SL.select(rq, self.reg, self.previous, RC.profile_shape_problems)
        prov, model = self.sel["provider"], self.sel["model"]
        SL.check_eligibility(rq["model_vendors"], rq["artifact_author"]["human_only"], model["claimed_vendor"])
        product = getattr(self.opts,"execution_context",None)
        self.secrets = S.SecretHandle()
        if product is not None:
            product.validate_selection(self.reg, rq, self.sel, self.previous, self.mode)
            self.capability_status = product.port.registration["capability"]["status"]
            self.secrets.add_scan_values(product.scan_set())
            self.env_report = {}  # Host owns credentials; applicable scan values come from trusted context.
        else:
            if self.previous is not None and self.previous["receipt"].get("execution") is not None:
                raise base.PreflightError("inherit-unmaterializable", "product evidence cannot become maintenance scope")
            status = G.effective_status(model, prov["transport"], self.sel["effort"])
            code = G.admission(status, self.mode, rq["formal_review_authorized_by_owner"])
            if code is not None:
                raise base.PreflightError(code,"capability status %s under mode %s" % (status,self.mode))
            self.capability_status=status
            self.env_report=S.resolve_for_provider(prov,self.secrets,self.opts.zshrc)
        _OUTPUT_SCRUB.scan = self.secrets.scan_set   # R11-B1：秘密一经解析即启用输出清洗，先于适配器 preflight 与任何后续异常
        # R18-B1 / R19-B1（Owner 甲案，gov-t8:OD-11 / RES-6）：与治理工件结构字节重叠的秘密值不可受脱敏保护——先从扫描集移除
        # （其字面本就是公开记号），再以 preflight 失败结束；不显示值、长度或前后缀。碰撞面 = 固定 schema 成员名 ∪ 此刻已知的
        # 动态成员名（环境状态报告的变量名键、sanitized overrides 键、诊断 artifact 名）；适配器事实键在其 preflight 之后再核一次
        self._refuse_structural_collisions(_member_names(self.env_report, _member_names(self.sel["overrides"], set(ARTIFACT_NAMES))))
        # 适配器 preflight
        self.adapter = product if product is not None else self.reg["adapters"][prov["runtime"]]
        self.delivery = "inline" if self.adapter.INLINE_DELIVERY else "tool"
        self.adapter_facts = self.adapter.preflight(self.adapter_ctx(call_index=1, instruction=""))
        self._refuse_structural_collisions(_member_names(self.adapter_facts, set()))   # 适配器事实键（R19-B1）
        # 引用解析
        closure = I.root_closure(root)
        # Amendment 2 多候选（§6.2 第 1 / 2 条）：候选逐件读取、逐件提取引用；候选互引记 co-candidate
        cand_pairs = []
        for cand_rel in self.candidates_rel:
            # R3-B1 整改：候选字节在引用解析之前即经仓界前置读取原语（符号链接段 / O_NOFOLLOW / realpath）
            data = self.candidate_bytes[cand_rel]   # 秘密扫描在封存写入之前统一执行（inputs.reject_secret，R6-B3）
            self.candidate_bytes[cand_rel] = data
            cand_pairs.append((cand_rel, data.decode("utf-8", "replace")))
        self.references, failure = I.resolve_references(root, cand_pairs, rq["inputs"]["references"],
                                                        rq["review_brief"]["evidence_limits"], closure)
        if failure is not None:
            raise base.PreflightError(failure[0], failure[1])
        self.report = {"routing": self.routing, "profile": {"provider": self.sel["provider_id"],
                                                             "model": self.sel["model_slug"], "effort": self.sel["effort"],
                                                             "selection_source": self.sel["selection_source"]},
                       "capability_status": self.capability_status, "budget": self.budget,
                       "decisions_sha256": self.decisions_sha256,
                       "environment": self.env_report, "adapter": self.adapter_facts,
                       "references": [{k: r[k] for k in ("candidate", "token", "form", "domain", "status", "revision",
                                                         "current_revision")}
                                      for r in self.references]}

    def _refuse_structural_collisions(self, dynamic_names):
        colliding = _structural_collisions(self.secrets.scan_set(), dynamic_names)
        if colliding:
            self.secrets.drop_values(colliding)
            raise base.PreflightError("secret-structural-collision",
                                      "%d resolved secret value(s) overlap the structural bytes of governed JSON artifacts "
                                      "(member names or JSON literals); such a literal cannot be protected by scrubbing" % len(colliding))

    def _last_round_blocking(self, rounds):
        submitted = sorted(k for k, v in rounds.items() if v["receipt"])
        if not submitted:
            return "n/a"
        k = submitted[-1]
        if rounds[k].get('evidence'):
            try:
                document,files=E.evidence(self.repo_root,rounds[k]['evidence'])
                _,raw=E.role_file(document,files,'verdict');block,_,_=V.parse_published(raw)
                return str(sum(f.get('severity')=='blocking' for f in block.get('findings',[])))
            except (E.EvidenceError,ValueError,UnicodeError):return 'unknown (original not compatible with verdict grammar)'
        d = os.path.join(self.repo_root, rounds[k]["dir"])
        archived = H.round_files(self.repo_root, rounds[k]["dir"])
        if archived:
            for path,item in archived.items():
                if "_Review_R" in path and "_Task_" not in path:
                    try:
                        obj, _jb, _n = V.parse_published(item["content"])
                        return str(sum(1 for f in obj.get("findings", []) if f.get("severity") == "blocking"))
                    except (ValueError, UnicodeDecodeError, AttributeError):
                        return "unknown (previous verdict not in the strict grammar)"
        for entry in os.scandir(d) if os.path.isdir(d) else []:
            if entry.name.endswith(".md") and "_Review_R" in entry.name and "_Task_" not in entry.name:
                try:
                    obj, _b, _n = V.parse_published(base.read_bytes(entry.path))
                    return str(sum(1 for f in obj.get("findings", []) if isinstance(f, dict) and f.get("severity") == "blocking"))
                except (ValueError, UnicodeDecodeError, AttributeError):
                    return "unknown (previous verdict not in the strict grammar)"
        return "unknown"

    def _load_decisions(self, root, rq):
        """r2+ 上一轮决定文件（§6.12，Amendment 4）：缺席 decisions-missing；形状或五条规则不成立 decisions-invalid；
        verdict_sha256 与上一轮判词字节不等、或请求件的处置段 / 派生残留项与之不符 decisions-mismatch。"""
        k = self.previous["k"]
        if self.archive is not None:
            try:doc,data=E.previous_decisions(root,rq,self.previous,self.previous_machine)
            except E.EvidenceError as exc:raise base.PreflightError('decisions-invalid',str(exc)) from exc
            findings=self.previous_findings()
            if doc is None:
                self.decisions={'decisions':[]};self.decisions_sha256=None
                return
            prior={'subject':self.routing['subject'],'stage':self.routing['stage'],'round':'r'+str(k),'task_record':self.routing['task_record']}
            problems=D.document_problems(doc,findings,None,prior)+D.request_mismatches(rq['review_brief'],doc['decisions'],findings,base.sha256_bytes(data))
            if problems:raise base.PreflightError('decisions-invalid','; '.join(problems))
            self.decisions,self.decisions_sha256=doc,base.sha256_bytes(data)
            return
        path = D.decisions_path(root, previous_attempt_round_dir(self.routing, k))
        rel = os.path.relpath(path, root)
        try:
            doc, data = D.load_document(path)
        except (ValueError, UnicodeDecodeError, OSError) as exc:
            raise base.PreflightError("decisions-invalid", "previous round decisions file %s unparseable: %s" % (rel, type(exc).__name__))
        if doc is None:
            raise base.PreflightError("decisions-missing", "previous round r%d has no decisions file at %s; run respond first" % (k, rel))
        prev_findings = self.previous_findings()
        prev_routing = {"subject": self.routing["subject"], "stage": self.routing["stage"], "round": "r%d" % k,
                        "task_record": self.routing["task_record"]}
        problems = D.document_problems(doc, prev_findings, None, prev_routing)
        if problems:
            raise base.PreflightError("decisions-invalid", "%s: %s" % (rel, "; ".join(problems)))
        if doc["verdict_sha256"] != base.sha256_bytes(self.previous["verdict_bytes"]):
            raise base.PreflightError("decisions-mismatch", "%s.verdict_sha256 does not equal the previous verdict bytes (rule 5)" % rel)
        sha = base.sha256_bytes(data)
        mismatches = D.request_mismatches(rq["review_brief"], doc["decisions"], prev_findings, sha)
        if mismatches:
            raise base.PreflightError("decisions-mismatch", "; ".join(mismatches))
        self.decisions = doc
        self.decisions_sha256 = sha

    def previous_findings(self):
        return [{"id": f["id"], "severity": f["severity"], "title": f["title"]}
                for f in self.previous_machine.get("findings", []) if isinstance(f, dict)]

    def adapter_ctx(self, call_index, instruction, instruction_user=None):
        rq = self.request
        return {"mode": self.mode, "provider_id": self.sel["provider_id"], "provider": self.sel["provider"],
                "model_slug": self.sel["model_slug"], "model": self.sel["model"], "effort": self.sel["effort"],
                "overrides": self.sel["overrides"], "secrets": self.secrets, "attempt_dir": self.workdir,
                "seal_dir": getattr(self, "seal_dir", None), "instruction": instruction,
                "instruction_user": instruction_user,
                "timeout_seconds": rq["attempt_options"].get("timeout_seconds", self.reg["registry"]["defaults"]["timeout_seconds"]),
                "call_index": call_index, "inputs": getattr(self, "final_inputs", None),
                "manifest": getattr(self,"manifest",None), "request": rq,
                "task_file": getattr(self, "task_file", None),
                # 契约层事实（非工具专属），供适配器与自测假适配器消费
                "subject": rq["subject"], "stage": rq["stage"], "round": rq["round"],
                "required_question_ids": getattr(self, "required_questions", None),
                "previous_finding_ids": getattr(self, "previous_finding_ids", None),
                "residual_ids": rq["residual_ids"]}

    def seal(self):
        """封存 → 预清单 → 任务书 → 最终清单 → 容量守卫（§6.3 次序）。"""
        rq = self.request
        root = self.repo_root
        scan = self.secrets.scan_set()
        try:
            self._seal_bundle(rq, root, scan)
        except base.PreflightError as exc:
            if exc.code == "secret-in-input":
                base.remove_tree(os.path.join(self.workdir, I.INPUTS_SUBDIR))  # 已封存的其他件一并撤下：束不投递
            raise

    def _seal_bundle(self, rq, root, scan):
        # R6-B3 整改：束内每一件（候选、参考件、previous--、任务书）在写入封存目录之前经秘密扫描，命中即 secret-in-input
        # 投递名派生整束一次性全局唯一（§6.2 第 4 条，Amendment 1）：先预占固定名——任务书名与本束实际生成的 previous-- 族
        self.task_file = I.taskbook_filename(rq["subject"], rq["round"])
        fixed = [self.task_file]
        if self.previous is not None:
            fixed.extend(I.previous_bundle_names(self.previous["manifest"]))
        self.history_sources = list(self.previous.get("history_sources", [])) if self.previous else []
        self.seal_dir, pre = I.seal_inputs(root, self.workdir, self.candidates_rel, rq["inputs"]["references"],
                                           candidate_bytes=self.candidate_bytes, scan_set=scan, fixed_names=fixed, history_sources=self.history_sources)
        baselines = None
        if self.previous is not None:
            baselines = I.add_previous_round_inputs(root, self.seal_dir, pre, self.previous,
                                                    os.path.join(root, previous_attempt_round_dir(self.routing, self.previous["k"])),
                                                    scan_set=scan, current_candidate_sources=self.candidates_rel, history_sources=self.history_sources)
        self.verdict_file = I.verdict_filename(rq["subject"], rq["round"])
        self.verdict_path = self.routing["tracked_round_dir"] + "/" + self.verdict_file
        candidates = [p for p in pre if p["role"] == "candidate"]   # 清单候选次序 = 请求 inputs.candidates 次序（seal_inputs 保序）
        candidate = candidates[0]
        prev_ctx = None
        self.changed_region = None
        if self.previous is not None:
            diffs = []
            for c in candidates:
                cand_text = base.read_bytes(os.path.join(self.seal_dir, c["bundle_name"])).decode("utf-8", "replace")
                diffs.append((c["bundle_name"], T.diff_summary(baselines[c["source"]].decode("utf-8", "replace"), cand_text)))
            prev_findings = self.previous_findings()
            prev_ctx = {"findings": prev_findings,
                        "verdict": self.previous_machine.get("verdict"),
                        "diff": diffs[0][1], "diffs": diffs,
                        "decisions": D.summary_rows(self.decisions["decisions"], prev_findings),
                        "profile_same": not self.sel["changed_from_previous"]}
        else:
            self.changed_region = self._r1_changed_region(root, candidates)
        design_path = os.path.join(base.UNIT_DIR, C.DESIGN_FILE)
        self.design_sha = base.sha256_file(design_path) if os.path.isfile(design_path) else None
        tb_ctx = {"request_schema": "review-channel-request/v5" if self.archive else C.REQUEST_SCHEMA, "request": rq, "routing": self.routing, "pre_manifest": pre, "candidate": candidate, "candidates": candidates,
                  "changed_region": self.changed_region,
                  "eligibility": {"author_vendors": rq["model_vendors"], "reviewer_vendor": self.sel["model"]["claimed_vendor"]},
                  "profile": {"provider": self.sel["provider_id"], "model": self.sel["model_slug"],
                              "transport": self.sel["provider"]["transport"], "runtime": self.sel["provider"]["runtime"],
                              "effort": self.sel["effort"], "effort_source": self.sel["effort_source"],
                              "selection_source": self.sel["selection_source"],
                              "changed_from_previous": self.sel["changed_from_previous"], "previous": self.sel["previous_pair"]},
                  "verdict_path": self.verdict_path, "channel": {"id": C.CHANNEL_ID, "design_sha256": self.design_sha},
                  "previous": prev_ctx, "references": self.references, "budget": self.budget,
                  "authorization": rq["invocation_authorization"] or "(preflight only)", "task_file": self.task_file}
        tb_ctx["history_sources"] = self.history_sources
        taskbook = T.render(tb_ctx)
        I.add_sealed_bytes(self.seal_dir, pre, self.task_file, taskbook, "review-task", "(channel-rendered)", scan_set=scan)
        self.final_inputs = pre
        self.manifest = I.manifest_document(rq["subject"], rq["stage"], rq["round"], self.task_file, pre)
        self.manifest_bytes = base.pretty_json(self.manifest)
        self.candidate = candidate
        self.candidates = candidates
        if self.delivery == "inline":
            limit = self.sel["model"]["max_input_bytes"]
            total = I.total_bytes(pre)
            if total > limit:
                raise base.PreflightError("input-capacity-exceeded", "inline delivery %d bytes exceeds max_input_bytes %d" % (total, limit))
        self.required_questions = C.required_question_ids(rq["stage"])   # 五题闭集（§6.4，Amendment 4）
        self.previous_finding_ids = None
        if self.previous is not None:
            self.previous_finding_ids = [f["id"] for f in self.previous_machine.get("findings", []) if isinstance(f, dict)]
        self.instruction = N.review_instruction({
            "task_file": self.task_file, "stage": rq["stage"], "required_question_ids": self.required_questions,
            "round_tag": rq["round"], "previous_finding_ids": self.previous_finding_ids, "residual_ids": rq["residual_ids"],
            "delivery": self.delivery})

    def _r1_changed_region(self, root, candidates):
        """r1 改动区（§6.2 第 6 条，Amendment 4；Owner 裁定 F5）：逐候选取现行冻结事实内的身份（sha256）并按 recover_baseline
        取字节算差异块；任何冻结事实内无该路径（首冻）或记录不含机械正本即整件为改动区、不判失败码；身份在案而字节不可恢复
        即 baseline-unrecoverable（与 r2+ 同码）。"""
        region = []
        baseline_identities=[]
        for c in candidates:
            fact = I.current_freeze_fact(root, c["source"])
            item = {"bundle_name": c["bundle_name"], "baseline": None, "diff": None, "note": None}
            if fact is not None and fact["sha256"] is None:
                item["note"] = ("现行冻结事实 %s 的记录不含机械正本（雏形形制），基线身份不可得"
                                % (("r%d" % fact["revision"]) if fact["revision"] is not None else "（修订号不可得）"))
            elif fact is not None:
                baseline = I.recover_baseline(root, c["source"], fact["sha256"], None, self.history_sources)
                if baseline is None:
                    raise base.PreflightError("baseline-unrecoverable",
                                              "current freeze fact bytes (%s) of %s recoverable neither from attempts nor Git history"
                                              % (fact["sha256"][:12], c["source"]))
                cand_text = base.read_bytes(os.path.join(self.seal_dir, c["bundle_name"])).decode("utf-8", "replace")
                if self.archive is not None:
                    I.reject_secret(baseline,c['source'],self.secrets.scan_set())
                    path=os.path.join(self.workdir,'baselines',fact['sha256'])
                    baseline_identities.append({'path':c['source'],'sha256':fact['sha256'],'bytes':len(baseline)})
                    if not os.path.exists(path):
                        os.makedirs(os.path.dirname(path),exist_ok=True);base.write_new(path,baseline)
                item["baseline"] = {"revision": fact["revision"], "sha256": fact["sha256"]}
                item["diff"] = T.diff_summary(baseline.decode("utf-8", "replace"), cand_text)
            region.append(item)
        if self.archive is not None:base.write_new(os.path.join(self.workdir,'baseline-identities.json'),E.canonical(baseline_identities))
        return region


# ---------------------------------------------------------------- attempt 执行

def _selected(pf, attempt):
    rq = pf.request
    return RC.selected_group({
        "caller": rq["caller"], "artifact_author": rq["artifact_author"], "model_vendors": rq["model_vendors"],
        "provider_id": pf.sel["provider_id"], "provider": pf.sel["provider"], "model_slug": pf.sel["model_slug"],
        "model": pf.sel["model"], "effort": pf.sel["effort"], "effort_source": pf.sel["effort_source"],
        "overrides": pf.sel["overrides"], "overrides_source": pf.sel["overrides_source"],
        "selection_source": pf.sel["selection_source"], "registry_revision": pf.reg["revision"],
        "registry_sha256": pf.reg["sha256"], "contract_version": CONTRACT_VERSION,
        "contract_design_sha256": pf.design_sha, "request_sha256": attempt.request_sha256,
        "input_manifest_sha256": pf.manifest["manifest_sha256"], "round": rq["round"], "attempt_id": attempt.attempt_id,
        "question_ids": pf.required_questions, "bundle_names": [i["bundle_name"] for i in pf.final_inputs],
        "task_file": pf.task_file, "delivery": pf.delivery,
        "execution_applicability": getattr(pf.opts,"execution_context",None).applicability if getattr(pf.opts,"execution_context",None) else None})


def _initial_state(attempt, mode, opts, invocation_sha, formal):
    return {
        "attempt_id": attempt.attempt_id, "routing": attempt.routing, "mode": mode, "receipt_phase": "routed",
        "classification": None, "failure_code": None, "attempt_outcome": None, "call_path_proof": "NOT_PROVEN",
        "profile_binding": "INSUFFICIENT", "verdict_validation": "NOT_REACHED", "verdict_published": False,
        "capability_suggestion": None, "formal": formal, "invocation_authorization_sha256": invocation_sha,
        "request_sha256": attempt.request_sha256,
        "diagnostics": {"artifacts": {}, "secret_leak_redacted": False, "secret_leak_in_evidence": False, "request_secret_hit_at_allocation": False,
                        "environment_report": None, "adapter_preflight": None, "cleanup": None},
        "options": {"reemit_enabled": opts.reemit},
        "input_manifest_sha256": None, "input_manifest_path": None, "round_budget": None,
        "decisions_sha256": None, "delivery": None, "selected": None, "calls": [], "published": None,
        "runtime_group": None, "raw_final_message_sha256": None, "raw_block_sha256": None, "extraction": None,
        "verdict_problems": [], "human_decision_required": None,
    }


def _scan_set(st):
    secrets = st.get("_secrets")
    return secrets.scan_set() if secrets is not None else []


def _persist_receipt(attempt, st):
    """Receipt 落盘前秘密终检（R1-B2 / R2-B2 整改：全部写出路径统一经此）：命中即记 secret_leak_in_evidence、
    维持保守分类（撤销 probe_completed / completed_with_valid_verdict 与其能力建议），并以脱敏字节写出。"""
    scan = _scan_set(st)
    _clean, hit = base.scrub_json(RC.build_receipt(st), scan)   # 结构感知：字符串值与动态成员名（R18-B1 / R19-B1）
    if hit:
        st["diagnostics"]["secret_leak_in_evidence"] = True
        # R7-B2 整改：review 调用后的 Receipt 证据字段命中 ⇒ verdict_validation = INVALID，与失败分类无关
        if st["mode"] == "review" and st["calls"] and st["verdict_validation"] != "INVALID":
            st["verdict_validation"] = "INVALID"
            st["verdict_problems"] = list(st["verdict_problems"]) + ["secret material detected in receipt evidence fields"]
            if st["extraction"] is None:
                st["extraction"] = _INVALID_EXTRACTION()
            st["capability_suggestion"] = C.capability_transition(st["call_path_proof"], st["profile_binding"], "INVALID")[1]
            if st["capability_suggestion"] == "REVIEW_ENABLED":
                st["capability_suggestion"] = "PROBED"
            # R17-B1 整改：结论在此处才确定为 INVALID → 先写 validated 态记录，再写 Receipt（崩溃补产取到确定事实）
            attempt.advance("validated", verdict_validation="INVALID", extraction=st["extraction"],
                            call_path_proof=st["call_path_proof"], profile_binding=st["profile_binding"],
                            effective_profile=RC.build_profile(st["selected"], st["calls"], None))
        if st["classification"] in ("probe_completed", "completed_with_valid_verdict"):
            st["classification"] = "reviewer_output_invalid"
            st["failure_code"] = "secret-leak"
            st["attempt_outcome"] = "receipt_only"
            st["verdict_published"] = False
            st["published"] = None
            if st["receipt_phase"] == "completed":
                st["receipt_phase"] = "called"
            if st["verdict_validation"] == "VALID":
                st["verdict_validation"] = "INVALID"
            # R3-B2 整改：降级后的 called Receipt 仍须满足阶段条件（extraction 必填、失败调用序号在场）
            if st["receipt_phase"] == "called" and st["extraction"] is None:
                st["extraction"] = _INVALID_EXTRACTION()
            if st["failure_call_index"] is None:
                st["failure_call_index"] = len(st["calls"]) or None
            st["capability_suggestion"] = C.capability_transition(st["call_path_proof"], st["profile_binding"], st["verdict_validation"])[1]
            if st["capability_suggestion"] == "REVIEW_ENABLED":
                st["capability_suggestion"] = "PROBED"
        _clean, _hit = base.scrub_json(RC.build_receipt(st), scan)
    data = base.pretty_json(_clean)
    if base.contains_secret(data, scan):
        # 固定 schema 成员名与 preflight 可预知的动态成员名已在 preflight 拒绝、其余动态成员名与字符串值已脱敏：此处不可达；
        # 仍 fail closed，不写破坏 schema 的字节
        raise base.ChannelError("internal-error", "secret material overlaps the Receipt's structural bytes after value scrubbing")
    base.write_new(os.path.join(attempt.dir, RECEIPT_JSON), data)
    if attempt.archive:
        try:attempt.evidence_ref=E.capture_attempt(attempt.archive,attempt.dir,'attempt')
        except (E.EvidenceError,OSError) as exc:
            _stderr('archive persistence failed; originals retained in pending: '+str(exc))
            raise
    return data


def _INVALID_EXTRACTION():
    return {"tier": "invalid", "repairs": [], "reemit_trigger": None, "reemit_reasons": [], "reemit_raw_message_sha256": None,
            "reemit_unverifiable_fields": [], "invariance_check": None}


def _write_failure_receipt(attempt, st, code, classification="preflight_failed", outcome="preflight_failed"):
    st["classification"] = classification
    st["failure_code"] = code
    st["attempt_outcome"] = outcome
    return _persist_receipt(attempt, st)


def _write_diagnostic(attempt, st, name, data, scan_set):
    clean, hit = base.scrub_secrets(data, scan_set)
    if hit:
        st["diagnostics"]["secret_leak_redacted"] = True
    if base.contains_secret(clean, scan_set):
        # 不动点未达（R20-B1 闭包）：诊断 artifact 不落盘，按调用后内部异常收口（reviewer_output_invalid + internal-error）
        raise base.ChannelError("internal-error", "secret material survives scrubbing in diagnostic artifact %s" % name)
    path = os.path.join(attempt.dir, name)
    try:
        base.write_new(path, clean)
    except FileExistsError:
        base.write_atomic_replace(path, clean)
    st["diagnostics"]["artifacts"][name] = base.sha256_bytes(clean)
    return clean, hit


def _call(pf, attempt, st, call_index, instruction, instruction_user=None):
    """一次适配器调用：calling → 调用 → called；返回 (result, call_record)。"""
    attempt.advance("calling", call_index=call_index)
    ctx = pf.adapter_ctx(call_index, instruction, instruction_user)
    internal = None
    try:
        result = pf.adapter.run(ctx)
    except base.ChannelError as exc:
        result = RT.empty_result()
        result["failure"] = RT.failure_from_adapter_error(exc.code)
        result["process"] = {"exit_code": None, "timed_out": False, "stderr_summary": exc.code}
    except Exception as exc:  # noqa: BLE001 - 适配器意外异常：调用已发起，按调用后内部异常处置（R2-B5）
        result = RT.empty_result()
        result["process"] = {"exit_code": None, "timed_out": False, "stderr_summary": "adapter exception: %s" % type(exc).__name__}
        internal = exc
    if result.get("execution") is not None:
        st["execution"]=result["execution"]
        attempt.advance("calling",execution=st["execution"])
    rec = RT.build_call_record(call_index, pf.mode, result, pf.sel["provider"]["kind"], pf.sel["model_slug"],
                               pf.sel["model"].get("registered_equivalents"),   # r18 R18-B2 整改：等价映射只在 aggregator 模型级（§6.11 schema）
                               pf.sel["effort"], pf.sel["effort_source"])
    if internal is not None:
        rec["call_path_proof"] = "INDETERMINATE"
    st["calls"].append(rec)
    cpp, pb = RC.aggregate_dims(st["calls"])
    st["call_path_proof"], st["profile_binding"] = cpp, pb
    st["runtime_group"] = RC.runtime_group(pf.sel["provider"]["runtime"], st["calls"], pf.sel["previous_runtime"],
                                           pf.delivery == "inline")
    # R2-B4 整改：记录只承载冻结字段——effective_profile（含 calls[]）、运行时组、attempt 级两维；不另存内部拆分字段
    attempt.advance("called", runtime_group=st["runtime_group"], call_path_proof=cpp, profile_binding=pb,
                    effective_profile=RC.build_profile(st["selected"], st["calls"], None))
    if internal is not None:
        raise base.ChannelError("internal-error", "adapter raised %s: %s" % (type(internal).__name__, internal))
    return result, rec


@output_scope
@E.lock_scope()
@round_lock_scope()
def run_attempt(mode, opts):
    """probe / review。返回退出码。"""
    repo_root = opts.repo_root
    try:
        raw = base.read_bytes(opts.request)
    except OSError as exc:
        _stderr("request-unrouteable: request file unreadable (%s)" % type(exc).__name__)
        return 1
    try:
        route = R.minimal_route(raw)
    except base.UnrouteableError as exc:
        _stderr("request-unrouteable: %s" % exc.message)
        return 1
    routing = derive_routing(route)
    recoverer = _recoverer_identity()
    try:
        opts.archive=E.configured(repo_root,writable=True)
        if opts.archive:
            opts.archive.lock(routing['tracked_round_dir'])
            E.check_pending(opts.archive,routing['tracked_round_dir'])
        cleanup = [] if opts.archive else preflight_cleanup(repo_root, routing, recoverer)
    except (base.PreflightError,E.EvidenceError) as exc:
        _report({"mode": mode, "state": "REJECTED", "failure_code": exc.code, "problems": [exc.message]})
        return 1
    req_obj = route["request"]
    inv = req_obj.get("invocation_authorization")
    inv_sha = base.sha256_bytes(inv.encode("utf-8")) if isinstance(inv, str) else None
    formal = req_obj.get("formal_review_authorized_by_owner") is True
    # R7-B1 / R8-B2（gov-t8:OD-09 乙案）：分配之前以 Registry 全体 provider 的秘密并集预扫描原始 Request——命中则 attempt
    # 在分配后立即以 secret-in-input 结束（不进入完整校验、不到达适配器与 argv）；request.json 仍为原始字节 exact copy
    # （§8.1 审计与 request_sha256 复核），调用方自带秘密的原字节留在 attempt 目录属 Owner 接受残留 RES-4
    pre_scan = opts.execution_context.scan_set() if getattr(opts,"execution_context",None) else S.union_scan_set(opts.registry, opts.zshrc)
    request_hit = base.contains_secret(raw, pre_scan)
    attempt = Attempt(repo_root, routing, mode, raw, opts)
    try:
        attempt.allocate(inv_sha, formal)
    except OSError as exc:
        _stderr("attempt-alloc-failed: %s" % type(exc).__name__)
        return 2
    st = _initial_state(attempt, mode, opts, inv_sha, formal)
    if attempt.archive:st['evidence_storage']={'kind':'archive','repository_id':attempt.archive.repository_id,'round_key':routing['tracked_round_dir']}
    st["diagnostics"]["cleanup"] = cleanup
    if request_hit:
        st["diagnostics"]["request_secret_hit_at_allocation"] = True
        try:
            _stderr("secret-in-input: the Request carries secret or endpoint material declared in the Registry (RES-4: the exact copy stays)")
            return _fail_before_call(attempt, st, "secret-in-input")
        except Exception as exc:  # noqa: BLE001
            _stderr("internal-error while writing the Receipt: %s: %s" % (type(exc).__name__, exc))
            return 2
    try:
        try:
            locked = True if attempt.archive else acquire_round_lock(attempt.attempt_round_dir)
        except Exception as exc:  # noqa: BLE001 - 取锁的非竞争异常是调用前内部异常（R3-B10）：仍产 Receipt
            _stderr("internal-error before the call (round lock): %s: %s" % (type(exc).__name__, exc))
            return _fail_before_call(attempt, st, "internal-error")
        if not locked:
            return _fail_before_call(attempt, st, "round-in-progress")
        if not attempt.archive:note_lock_holder(attempt)
        return _run_locked(pf_factory=lambda: Preflight(repo_root, routing, mode, req_obj, opts, attempt.dir),
                           attempt=attempt, st=st, mode=mode, opts=opts)
    except E.EvidenceError as exc:
        _report({'attempt_id':attempt.attempt_id,'classification':'reviewer_output_invalid' if st['calls'] else 'preflight_failed','failure_code':exc.code,'pending':attempt.dir,'problems':[exc.message],'durable_evidence':False})
        return 1
    except Exception as exc:  # noqa: BLE001 - Receipt 写入自身失败以外的兜底在 _run_locked 内；此处只剩写入失败
        _stderr("internal-error while writing the Receipt: %s: %s" % (type(exc).__name__, exc))
        return 2


def _fail_before_call(attempt, st, code):
    """调用前失败的统一收口：preflight_failed Receipt + 报告 + 退出码 1。"""
    _write_failure_receipt(attempt, st, code)
    _report({"attempt_id": attempt.attempt_id, "classification": "preflight_failed", "failure_code": code,
             "receipt": os.path.join(attempt.dir, RECEIPT_JSON)})
    return 1


def _run_locked(pf_factory, attempt, st, mode, opts):
    # ---- routed：Request 完整校验与 preflight 清单
    try:
        pf = pf_factory()
        pf.run()
        st["_secrets"] = pf.secrets
        attempt.scan_set = pf.secrets.scan_set
        st["diagnostics"]["environment_report"] = pf.env_report
        st["diagnostics"]["adapter_preflight"] = pf.adapter_facts
        pf.seal()
        # ---- sealed（状态区写入仍属调用前：其异常同样收口为 preflight_failed + internal-error，R3-B10）
        st["selected"] = _selected(pf, attempt)
        st["input_manifest_sha256"] = pf.manifest["manifest_sha256"]
        st["input_manifest_path"] = attempt.routing["tracked_round_dir"] + "/" + C.MANIFEST_NAME
        st["round_budget"] = pf.budget
        st["decisions_sha256"] = pf.decisions_sha256
        st["delivery"] = pf.delivery
        st["receipt_phase"] = "sealed"
        base.write_new(os.path.join(attempt.dir, C.MANIFEST_NAME), pf.manifest_bytes)
        attempt.advance("sealed", effective_profile=RC.build_profile(st["selected"], None, None),
                        input_manifest_sha256=st["input_manifest_sha256"], round_budget=pf.budget,
                        decisions_sha256=pf.decisions_sha256, delivery=pf.delivery,
                        verified_previous_refs=([pf.request["previous_round"]["evidence"]]+([pf.request["previous_round"]["response"]] if pf.request["previous_round"]["response"] else [])) if pf.archive and pf.previous else [],
                        evidence_storage=st.get("evidence_storage"))
    except base.PreflightError as exc:
        _stderr("preflight rejected [%s]: %s" % (exc.code, exc.message))
        return _fail_before_call(attempt, st, exc.code)
    except Exception as exc:  # noqa: BLE001 - 调用前内部异常 → preflight_failed + internal-error
        _stderr("internal-error before the call: %s: %s" % (type(exc).__name__, exc))
        return _fail_before_call(attempt, st, "internal-error")
    # ---- 调用（扫描集恒在落盘时刻重取：认证 staging 可在调用中扩展它，R1-B2 整改）
    try:
        if mode == "probe":
            return _finish_probe(pf, attempt, st)
        return _finish_review(pf, attempt, st, opts)
    except Exception as exc:  # noqa: BLE001 - 调用后内部异常 → reviewer_output_invalid + internal-error
        _stderr("internal-error after the call: %s: %s" % (type(exc).__name__, exc))
        if not st["calls"]:
            return _fail_before_call(attempt, st, "internal-error")  # 尚无调用记录：仍属调用前
        st["receipt_phase"] = "called"
        if mode == "probe":
            st["call_path_proof"] = "INDETERMINATE"
            st["verdict_validation"] = "NOT_REACHED"
        else:
            st["verdict_validation"] = "INVALID"
        if os.path.exists(os.path.join(attempt.dir, RECEIPT_JSON)):
            # Receipt 已落盘（异常发生在其后的报告阶段）：不改写，只报告
            _report({"attempt_id": attempt.attempt_id, "classification": st["classification"], "failure_code": st["failure_code"],
                     "receipt": os.path.join(attempt.dir, RECEIPT_JSON)})
            return 1
        # R3-B5 整改：调用后兜底与其余失败同经 _conclude_failure（called 阶段 extraction 补齐、失败码闭集、调用序号）
        return _conclude_failure(attempt, st, "reviewer_output_invalid", "internal-error", call_index=len(st["calls"]))


def _finish_probe(pf, attempt, st):
    result, rec = _call(pf, attempt, st, 1, N.probe_instruction())
    _persist_call_artifacts(attempt, st, result, _scan_set(st), prefix="")
    # R4-B2 整改：probe 成功（completed / probe_completed）须 PROVEN + SUFFICIENT（§6.6 成功证据形态、§6.11 状态转换表）
    bound = st["call_path_proof"] == "PROVEN" and st["profile_binding"] == "SUFFICIENT" and result["failure"] is None
    st["receipt_phase"] = "completed" if bound else "called"
    st["verdict_validation"] = "NOT_REACHED"
    leaked = st["diagnostics"]["secret_leak_redacted"]
    outcome, suggestion = C.capability_transition(st["call_path_proof"], st["profile_binding"], "NOT_REACHED")
    if result["failure"] is not None:
        classification = result["failure"]
    elif bound and not leaked:
        classification = "probe_completed"
    else:
        classification = "reviewer_output_invalid"
    st["classification"] = classification
    st["attempt_outcome"] = "receipt_only"
    st["capability_suggestion"] = suggestion
    # 失败码恒取闭集成员（R3-B5）：基础设施失败同名；载荷不匹配 / 空输出 = probe-payload-mismatch；泄漏 = secret-leak
    if classification == "probe_completed":
        st["failure_code"] = None
    elif leaked:
        st["failure_code"] = "secret-leak"
    elif result["failure"] is not None:
        st["failure_code"] = result["failure"]
    elif st["call_path_proof"] != "PROVEN":
        st["failure_code"] = "probe-payload-mismatch"
    else:
        st["failure_code"] = "profile-binding"
    st["failure_call_index"] = None if classification == "probe_completed" else 1
    # completed（probe 成功）判词组恒 null；called（probe 失败）extraction 必填 → tier = invalid（R2-B5）
    st["extraction"] = None if st["receipt_phase"] == "completed" else _INVALID_EXTRACTION()
    _persist_receipt(attempt, st)
    if attempt.archive:
        _report({'attempt_id':attempt.attempt_id,'classification':st['classification'],'source':attempt.evidence_ref,
                 'review_result':E.minimal_result(attempt.repo_root,attempt.evidence_ref),
                 'next_action':'Human reviews the archived evidence and formal result; no Registry update is automatic'})
        return 0 if st['classification']=='probe_completed' else 1
    # R3-B2 整改：对外报告与退出码取终检之后的状态（秘密终检可把 probe_completed 降级）
    classification = st["classification"]
    _report({"attempt_id": attempt.attempt_id, "classification": classification, "failure_code": st["failure_code"],
             "call_path_proof": st["call_path_proof"], "profile_binding": st["profile_binding"],
             "capability_suggestion": st["capability_suggestion"], "receipt": os.path.join(attempt.dir, RECEIPT_JSON),
             "next_action": ("probe complete; a Human may exact-copy this Receipt to %s/<date>/receipt-%s.json and update the Registry"
                             % (C.DIAGNOSTICS_DIR, attempt.attempt_id)) if classification == "probe_completed"
             else "report these facts to the Human; no fallback, no reselection"})
    return 0 if classification == "probe_completed" else 1


def _persist_call_artifacts(attempt, st, result, scan_set, prefix):
    raw = result.get("raw") or {}
    clean, _hit = _write_diagnostic(attempt, st, prefix + "reviewer_last_message.md", result["final_message"], scan_set)
    if "events" in raw:
        _write_diagnostic(attempt, st, prefix + "runtime_events.jsonl", raw["events"], scan_set)
    if "stderr" in raw:
        _write_diagnostic(attempt, st, prefix + "stderr.log", raw["stderr"], scan_set)
    if "response" in raw:
        _write_diagnostic(attempt, st, prefix + "response.json", raw["response"], scan_set)
    if "request_shape" in raw:
        _write_diagnostic(attempt, st, prefix + "request_shape.json", raw["request_shape"], scan_set)
    return clean


def _finish_review(pf, attempt, st, opts):
    rq = pf.request
    result, rec = _call(pf, attempt, st, 1, pf.instruction)
    final_clean = _persist_call_artifacts(attempt, st, result, _scan_set(st), prefix="")
    st["raw_final_message_sha256"] = base.sha256_bytes(result["final_message"])
    output_secret_hit = base.contains_secret(result["final_message"], _scan_set(st))
    st["receipt_phase"] = "called"
    extraction = {"tier": None, "repairs": [], "reemit_trigger": None, "reemit_reasons": [], "reemit_raw_message_sha256": None,
                  "reemit_unverifiable_fields": [], "invariance_check": None}
    if result["failure"] is not None:
        return _conclude_failure(attempt, st, result["failure"], result["failure"], call_index=1)
    if st["call_path_proof"] != "PROVEN" or st["profile_binding"] != "SUFFICIENT":
        st["verdict_validation"] = "NOT_REACHED"
        return _conclude_failure(attempt, st, "reviewer_output_invalid",
                                 "empty-output" if st["call_path_proof"] == "INDETERMINATE" else "profile-binding", call_index=1)
    try:
        text = result["final_message"].decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        # R2-B8 整改：不得以替换字符改写评审方字节；无效 UTF-8 即 INVALID
        st["extraction"] = _INVALID_EXTRACTION()
        st["verdict_problems"] = ["final message is not valid UTF-8: %s" % exc.reason]
        st["verdict_validation"] = "INVALID"
        return _conclude_failure(attempt, st, "reviewer_output_invalid", "verdict-invalid", call_index=1)
    json_bytes = narrative = None
    if pf.delivery == "inline":
        machine, narr = V.extract_wrapper(text)
        if machine is None:
            extraction["tier"] = "invalid"
            st["extraction"] = extraction
            st["verdict_problems"] = ["wrapper: %s" % narr]
            st["verdict_validation"] = "INVALID"
            return _conclude_failure(attempt, st, "reviewer_output_invalid", "wrapper-invalid")
        json_bytes, narrative = machine, narr
        extraction["tier"] = "strict"
        st["raw_block_sha256"] = base.sha256_bytes(machine.encode("utf-8"))
    else:
        cls = V.classify(text)
        st["raw_block_sha256"] = base.sha256_bytes(cls["raw_block"].encode("utf-8")) if cls["raw_block"] else None
        if cls["tier"] in ("strict", "repaired"):
            extraction["tier"] = cls["tier"]
            extraction["repairs"] = cls["repairs"]
            json_bytes, narrative = cls["json_bytes"], cls["narrative"]
        else:
            # 三级：信封类补发（同一 attempt、同一持有；§6.5.4）
            if not opts.reemit:
                extraction["tier"] = "invalid"
                st["extraction"] = extraction
                st["verdict_problems"] = ["envelope unparseable and re-emission disabled: %s" % cls["reason"]]
                st["verdict_validation"] = "INVALID"
                return _conclude_failure(attempt, st, "reviewer_output_invalid", "reemit-disabled")
            extraction["tier"] = "reemitted"
            extraction["reemit_trigger"] = "envelope"
            extraction["reemit_reasons"] = ["envelope: %s" % cls["reason"]]
            original_narrative = cls["narrative"]
            result2, rec2 = _call(pf, attempt, st, 2, N.reemit_instruction(text), instruction_user=text)
            _persist_call_artifacts(attempt, st, result2, _scan_set(st), prefix="reemit-")
            extraction["reemit_raw_message_sha256"] = base.sha256_bytes(result2["final_message"])
            output_secret_hit = output_secret_hit or base.contains_secret(result2["final_message"], _scan_set(st))
            if result2["failure"] is not None:
                extraction["invariance_check"] = {"status": "not_reached", "mismatched_fields": []}
                st["extraction"] = extraction
                st["verdict_validation"] = "NOT_REACHED"
                return _conclude_failure(attempt, st, result2["failure"], result2["failure"], call_index=2)
            try:
                text2 = result2["final_message"].decode("utf-8", "strict")
            except UnicodeDecodeError:
                text2 = ""
            cls2 = V.classify(text2)
            if cls2["tier"] not in ("strict", "repaired"):
                extraction["invariance_check"] = {"status": "not_reached", "mismatched_fields": []}
                st["extraction"] = extraction
                st["verdict_problems"] = ["reemit-unparseable: %s" % cls2["reason"]]
                st["verdict_validation"] = "INVALID"
                return _conclude_failure(attempt, st, "reviewer_output_invalid", "reemit-unparseable")
            json_bytes = cls2["json_bytes"]
            narrative = original_narrative
            try:
                block2 = json.loads(json_bytes)
            except ValueError:
                block2 = None
            rec_orig, _p = V.recover_narrative(original_narrative)
            mismatched = V.compare_recovered(rec_orig, block2 if isinstance(block2, dict) else {},
                                             pf.previous_finding_ids is not None)
            extraction["reemit_unverifiable_fields"] = list(C.REEMIT_UNVERIFIABLE_FIELDS)
            if mismatched:
                extraction["invariance_check"] = {"status": "mismatch", "mismatched_fields": mismatched}
                st["extraction"] = extraction
                st["verdict_problems"] = ["reemit-invariance-mismatch:%s" % f for f in mismatched]
                st["verdict_validation"] = "INVALID"
                return _conclude_failure(attempt, st, "reviewer_output_invalid", "reemit-invariance-mismatch")
            extraction["invariance_check"] = {"status": "passed", "mismatched_fields": []}
    st["extraction"] = extraction
    # ---- 校验（§6.5.2；失败项按 §6.5.2 末段分类）
    secret_now = output_secret_hit or st["diagnostics"]["secret_leak_redacted"]
    block, classified = _validate_block(pf, rq, json_bytes, narrative, secret_now)
    problems = [m for _c, m in classified]
    # ---- 校验类补发（§6.5.4，Amendment 2）：可解析而失败项全属可确定性整改项；恒至多一次补发；直接接口路径不适用
    if (problems and pf.delivery == "tool" and extraction["reemit_trigger"] is None
            and V.deterministically_remediable(classified) and not secret_now):
        if not opts.reemit:
            st["verdict_problems"] = problems
            st["verdict_validation"] = "INVALID"
            return _conclude_failure(attempt, st, "reviewer_output_invalid", "reemit-disabled")
        extraction["reemit_trigger"] = "validation"
        extraction["reemit_reasons"] = list(problems)
        vctx = _validate_ctx(pf, rq, narrative, secret_now)
        skeleton = C.narrative_skeleton(C.skeleton_values_from_block(block if isinstance(block, dict) else {},
                                                                     pf.required_questions, pf.previous_finding_ids))
        instr = N.validation_reemit_instruction(text, V.known_value_literals(vctx), skeleton, pf.task_file)
        result2, rec2 = _call(pf, attempt, st, 2, instr, instruction_user=text)
        _persist_call_artifacts(attempt, st, result2, _scan_set(st), prefix="reemit-")
        extraction["reemit_raw_message_sha256"] = base.sha256_bytes(result2["final_message"])
        output_secret_hit = output_secret_hit or base.contains_secret(result2["final_message"], _scan_set(st))
        if result2["failure"] is not None:
            extraction["invariance_check"] = {"status": "not_reached", "mismatched_fields": []}
            st["extraction"] = extraction
            st["verdict_validation"] = "NOT_REACHED"
            return _conclude_failure(attempt, st, result2["failure"], result2["failure"], call_index=2)
        try:
            text2 = result2["final_message"].decode("utf-8", "strict")
        except UnicodeDecodeError:
            text2 = ""
        cls2 = V.classify(text2)
        if cls2["tier"] not in ("strict", "repaired"):
            extraction["invariance_check"] = {"status": "not_reached", "mismatched_fields": []}
            st["extraction"] = extraction
            st["verdict_problems"] = ["reemit-unparseable: %s" % cls2["reason"]]
            st["verdict_validation"] = "INVALID"
            return _conclude_failure(attempt, st, "reviewer_output_invalid", "reemit-unparseable")
        try:
            block2 = json.loads(cls2["json_bytes"])
        except ValueError:
            block2 = None
        mismatched = V.judgment_mismatches(block, block2)
        extraction["reemit_unverifiable_fields"] = []
        extraction["tier"] = "reemitted"
        if mismatched:
            extraction["invariance_check"] = {"status": "mismatch", "mismatched_fields": mismatched}
            st["extraction"] = extraction
            st["verdict_problems"] = ["reemit-invariance-mismatch:%s" % f for f in mismatched]
            st["verdict_validation"] = "INVALID"
            return _conclude_failure(attempt, st, "reviewer_output_invalid", "reemit-invariance-mismatch")
        extraction["invariance_check"] = {"status": "passed", "mismatched_fields": []}
        st["raw_block_sha256"] = base.sha256_bytes(cls2["raw_block"].encode("utf-8")) if cls2["raw_block"] else st["raw_block_sha256"]
        json_bytes, narrative = cls2["json_bytes"], cls2["narrative"]
        st["extraction"] = extraction
        secret_now = output_secret_hit or st["diagnostics"]["secret_leak_redacted"]
        block, classified = _validate_block(pf, rq, json_bytes, narrative, secret_now)
        problems = [m for _c, m in classified]
    st["verdict_problems"] = problems
    st["verdict_validation"] = "VALID" if not problems else "INVALID"
    st["human_decision_required"] = block.get("human_decision_required") if isinstance(block, dict) else None
    published_doc = V.render_published(json_bytes, narrative)
    # 秘密终检（判词、Receipt、Profile 全部持久 artifact）
    scan_set = _scan_set(st)
    if base.contains_secret(published_doc, scan_set):
        st["verdict_validation"] = "INVALID"
        st["verdict_problems"] = problems + ["secret material detected in the verdict document"]
        st["diagnostics"]["secret_leak_redacted"] = True
    attempt.advance("validated", verdict_validation=st["verdict_validation"], extraction=extraction,
                    call_path_proof=st["call_path_proof"], profile_binding=st["profile_binding"],
                    effective_profile=RC.build_profile(st["selected"], st["calls"], None))
    if st["verdict_validation"] != "VALID":
        return _conclude_failure(attempt, st, "reviewer_output_invalid", "verdict-invalid")
    # ---- 发布前秘密终检（Receipt / Profile 字段）
    st["receipt_phase"] = "completed"
    st["published"] = {"verdict_path": pf.verdict_path, "verdict_sha256": base.sha256_bytes(published_doc)}
    st["classification"] = "completed_with_valid_verdict"
    st["attempt_outcome"] = "governed_verdict"
    st["capability_suggestion"] = "REVIEW_ENABLED"
    st["verdict_published"] = True
    receipt = RC.build_receipt(st)
    receipt_bytes = base.pretty_json(receipt)
    profile_bytes = base.pretty_json({"effective_profile": receipt["effective_profile"]})
    if base.contains_secret(receipt_bytes, scan_set) or base.contains_secret(profile_bytes, scan_set):
        st["diagnostics"]["secret_leak_in_evidence"] = True
        st["verdict_validation"] = "INVALID"
        st["verdict_problems"] = st["verdict_problems"] + ["secret material detected in receipt / profile evidence fields"]
        st["published"] = None
        st["verdict_published"] = False
        return _conclude_failure(attempt, st, "reviewer_output_invalid", "secret-leak")   # receipt_phase 由 _conclude_failure 归一为 called
    # ---- 诊断层：判词与 Profile 落 attempt 目录
    base.write_new(os.path.join(attempt.dir, pf.verdict_file), published_doc)
    base.write_new(os.path.join(attempt.dir, "effective_profile.json"), profile_bytes)
    # ---- 提交协议（§8.2）
    if attempt.archive:
        return finish_archive_round(attempt,pf,st,receipt_bytes,block)
    code = publish(attempt, pf, receipt_bytes)
    if code is not None:
        st["published"] = None
        st["verdict_published"] = False
        st["verdict_validation"] = "INVALID"
        st["verdict_problems"] = st["verdict_problems"] + [code]
        st["receipt_phase"] = "called"
        return _conclude_failure(attempt, st, "reviewer_output_invalid", code)
    # rename 之后已发布：此后任何异常只报告，不再改写分类、不产失败 Receipt（R2-B10 整改）；发布之后通道不执行任何 Git 动作
    # （§8.4 register 退役，Amendment 4：四件最低成员的 git add 与提交属 caller）
    tracked_receipt = os.path.join(attempt.repo_root, attempt.routing["tracked_round_dir"], "receipt-%s.json" % rq["round"])
    try:
        os.link(tracked_receipt, os.path.join(attempt.dir, RECEIPT_JSON))
    except OSError as exc:
        _stderr("receipt-backlink-failed: %s" % type(exc).__name__)
    _report({"attempt_id": attempt.attempt_id, "classification": "completed_with_valid_verdict", "verdict": block.get("verdict"),
             "human_decision_required": st["human_decision_required"], "verdict_path": pf.verdict_path,
             "tracked_round_dir": attempt.routing["tracked_round_dir"], "extraction_tier": extraction["tier"],
             "reemit_trigger": extraction["reemit_trigger"], "receipt": tracked_receipt})
    return 0


def _validate_ctx(pf, rq, narrative, secret_hit):
    return {"subject": rq["subject"], "stage": rq["stage"], "round": rq["round"], "candidate": pf.candidate,
            "candidates": pf.candidates,
            "required_question_ids": pf.required_questions, "previous_finding_ids": pf.previous_finding_ids,
            "residual_ids": rq["residual_ids"], "bundle_names": [i["bundle_name"] for i in pf.final_inputs],
            "task_file": pf.task_file, "delivery": pf.delivery, "narrative": narrative,
            # R5-B4 整改：任一持久 artifact（含诊断层事件流 / stderr / 原始响应）命中即 INVALID，不只看最终消息
            "secret_hit": secret_hit}


def _validate_block(pf, rq, json_bytes, narrative, secret_hit):
    """返回 (block | None, [(class, message)])。"""
    try:
        block = json.loads(json_bytes)
    except ValueError as exc:
        return None, [("other", "JSON value segment unparseable: %s" % exc)]
    return block, V.validate_machine_classified(block, _validate_ctx(pf, rq, narrative, secret_hit))


def _conclude_failure(attempt, st, classification, code, call_index=None):
    if classification in ("cancelled_by_host", "execution_port_failure"):
        st["verdict_validation"]="NOT_REACHED"
    # R6-B5 整改：review 调用后任一持久 artifact 秘密命中 ⇒ verdict_validation = INVALID（§6.11），与失败原因无关
    if st["mode"] == "review" and st["calls"] and st["diagnostics"]["secret_leak_redacted"] and st["verdict_validation"] != "INVALID":
        st["verdict_validation"] = "INVALID"
        st["verdict_problems"] = list(st["verdict_problems"]) + ["secret material detected in a persisted call artifact"]
    outcome, suggestion = C.capability_transition(st["call_path_proof"], st["profile_binding"],
                                                  st["verdict_validation"] if st["verdict_validation"] else "NOT_REACHED")
    if code not in C.FAILURE_CODES:
        raise base.ChannelError("internal-error", "failure code outside the closed set: %s" % code)
    st["classification"] = classification
    st["failure_code"] = code
    st["failure_call_index"] = call_index  # R1-B5 整改：调用序号是独立事实字段，不污染失败码闭集
    st["attempt_outcome"] = "receipt_only"
    st["capability_suggestion"] = suggestion if suggestion != "REVIEW_ENABLED" else "PROBED"
    st["verdict_published"] = False
    st["published"] = None
    if st["receipt_phase"] == "completed":
        # 自查整改（r18 前）：completed 只属终态成功（§6.6）；任何失败收口一律归一为 called，与发布失败 / 秘密终检路径同规则
        st["receipt_phase"] = "called"
    if st["receipt_phase"] == "called" and st["extraction"] is None:
        # called 阶段 extraction 必填（§6.6）：未达提取即 tier = invalid
        st["extraction"] = _INVALID_EXTRACTION()
    # R16-B1 整改：校验结论一经确定（INVALID / VALID）即先写 validated 态记录，再写 Receipt——崩溃补产取到的是截至此刻的确定事实
    if st["mode"] == "review" and st["calls"] and st["verdict_validation"] in ("INVALID", "VALID"):
        attempt.advance("validated", verdict_validation=st["verdict_validation"], extraction=st["extraction"],
                        call_path_proof=st["call_path_proof"], profile_binding=st["profile_binding"],
                        effective_profile=RC.build_profile(st["selected"], st["calls"], None))
    _persist_receipt(attempt, st)
    _report({"attempt_id": attempt.attempt_id, "classification": classification, "failure_code": st["failure_code"],
             "call_path_proof": st["call_path_proof"], "profile_binding": st["profile_binding"],
             "verdict_validation": st["verdict_validation"], "verdict_problems": st["verdict_problems"],
             "receipt": os.path.join(attempt.dir, RECEIPT_JSON),
             "next_action": "report these facts to the Human; attribute the failure before any retry (rule 7); no fallback"})
    return 1


# ---------------------------------------------------------------- 提交协议（§8.2 步骤 ⑥）

def publish(attempt, pf, receipt_bytes):
    """暂存 → 目标核对 → rename。成功返回 None，失败返回失败码（暂存已整体删除，tracked 未触碰）。"""
    repo_root = attempt.repo_root
    tracked = attempt.routing["tracked_round_dir"]
    parent_rel, name = tracked.rsplit("/", 1)
    parent_abs = os.path.join(repo_root, parent_rel)
    staging = os.path.join(attempt.dir, STAGING_PREFIX + attempt.attempt_id)
    published = False
    try:
        os.makedirs(parent_abs, exist_ok=True)  # 只建父目录，从不在目标路径上创建目录
        os.makedirs(staging, mode=0o755)
        os.link(os.path.join(attempt.dir, I.INPUTS_SUBDIR, pf.task_file), os.path.join(staging, pf.task_file))
        os.link(os.path.join(attempt.dir, C.MANIFEST_NAME), os.path.join(staging, C.MANIFEST_NAME))
        os.link(os.path.join(attempt.dir, pf.verdict_file), os.path.join(staging, pf.verdict_file))
        base.write_new(os.path.join(staging, "receipt-%s.json" % pf.request["round"]), receipt_bytes)
        base.fsync_dir(staging)
        parent_fd = os.open(parent_abs, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                exists = True
            except FileNotFoundError:
                exists = False
            expected_view = getattr(pf, 'history_view', None)
            if expected_view is not None and H.helper().head_commit(repo_root) != expected_view:
                raise base.PreflightError('history-unavailable', 'history view changed before publication')
            if exists or H.items(repo_root, tracked+"/"):
                _stderr("tracked-round-dir-occupied: %s already exists (%s); the channel never touches a pre-existing "
                        "directory - a Human must decide" % (tracked, _describe_dir(os.path.join(repo_root, tracked))))
                base.remove_tree(staging)
                return "tracked-round-dir-occupied"
            try:
                os.rename(staging, name, dst_dir_fd=parent_fd)
            except OSError as exc:
                _stderr("publish-rename-failed: %s" % type(exc).__name__)
                base.remove_tree(staging)
                return "publish-rename-failed"
            published = True  # 不可逆成功切点：此后任何异常只报告（R2-B10 / R3-B8）
        finally:
            try:
                os.close(parent_fd)
            except OSError as exc:
                _stderr("parent directory descriptor close failed%s: %s"
                        % (" (publication stands)" if published else "", type(exc).__name__))
        try:
            base.fsync_dir(parent_abs)
        except OSError as exc:  # rename 已成功：只报告（R2-B10）
            _stderr("parent fsync after publication failed (publication stands): %s" % type(exc).__name__)
        return None
    except base.PreflightError as exc:
        base.remove_tree(staging)
        _stderr(exc.message)
        return exc.code
    except OSError as exc:
        if published:
            _stderr("post-publication step failed (publication stands): %s" % type(exc).__name__)
            return None
        _stderr("publish staging failed: %s" % type(exc).__name__)
        base.remove_tree(staging)
        return "publish-rename-failed"


def _describe_dir(path):
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return "not a readable directory"
    if len(names) == 4 and any(n.startswith("receipt-r") for n in names):
        return "four members present: an already published round"
    return "%d entries: %s" % (len(names), ", ".join(names[:6]))


# ---------------------------------------------------------------- respond（§6.12，Amendment 4）

def _respond_reject(problems):
    _stderr("decisions-invalid: " + "; ".join(problems))
    _report({"mode": "respond", "state": "REJECTED", "failure_code": "decisions-invalid", "problems": problems})
    return 1


def run_respond(opts):
    """写入并校验决定文件：零提供方接触、不是 attempt、不产 Receipt、不取同轮文件锁。输入 = 决定文件候选（subject / stage / round /
    task_record 与 decisions[]；verdict_sha256 与 decided_at 由通道填写）；按 §8.1 派生两个目录值，读 tracked_round_dir 内已发布判词，
    按 §6.12 五条规则校验，原子写入 attempt_round_dir/decisions.json（已存在即整体覆盖），stdout 输出处置段与派生残留项。"""
    repo_root = opts.repo_root
    try:
        obj = base.strict_json_load(base.read_bytes(opts.decisions))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return _respond_reject(["decisions input unreadable or unparseable (%s)" % type(exc).__name__])
    try:
        if E.storage_mode(repo_root)=='archive-v1':
            ref,doc,data=E.respond(repo_root,obj,base.read_bytes(opts.decisions))
            d,files=E.evidence(repo_root,obj['source']);_,vb=E.role_file(d,files,'verdict');block,_,_=V.parse_published(vb)
            findings=block['findings'];digest=base.sha256_bytes(data)
            _report({'mode':'respond','state':'WRITTEN','source':ref,'decisions_sha256':digest,
                     'next_round':{'remediation_statement':D.dispositions_segment(doc['decisions'],findings,digest),'accepted_residuals':D.derived_residuals(doc['decisions'],findings)}})
            return 0
    except E.EvidenceError as exc:return _respond_reject([str(exc)])
    problems = D.input_problems(obj)
    if problems:
        return _respond_reject(problems)
    routing = derive_routing({"subject": obj["subject"], "stage": obj["stage"], "round": obj["round"],
                              "task_record": obj.get("task_record")})
    tracked = os.path.join(repo_root, routing["tracked_round_dir"])
    verdict_rel = routing["tracked_round_dir"] + "/" + I.verdict_filename(routing["subject"], routing["round"])
    receipt_rel = routing["tracked_round_dir"] + "/receipt-%s.json" % routing["round"]
    try:
        archived = H.round_files(repo_root, routing["tracked_round_dir"])
    except base.PreflightError as exc:
        return _respond_reject([exc.message])
    if archived is None and (not os.path.isfile(os.path.join(repo_root, verdict_rel)) or not os.path.isfile(os.path.join(repo_root, receipt_rel))):
        return _respond_reject(["published verdict not located: %s (a published round carries its four members)" % verdict_rel])
    verdict_bytes = archived[verdict_rel]["content"] if archived else base.read_bytes(os.path.join(repo_root, verdict_rel))
    verdict_sha = base.sha256_bytes(verdict_bytes)
    try:
        block, _jb, _narr = V.parse_published(verdict_bytes)
        receipt = base.strict_json_load(archived[receipt_rel]["content"] if archived else base.read_bytes(os.path.join(repo_root, receipt_rel)))
    except (ValueError, UnicodeDecodeError) as exc:
        return _respond_reject(["published verdict or receipt unparseable (%s)" % type(exc).__name__])
    if not isinstance(receipt, dict) or receipt.get("verdict_sha256") != verdict_sha:
        return _respond_reject(["published receipt verdict_sha256 does not equal the verdict bytes in %s" % tracked])
    findings = [{"id": f["id"], "severity": f["severity"], "title": f["title"]}
                for f in (block.get("findings") if isinstance(block, dict) else []) or [] if isinstance(f, dict)]
    problems = D.decision_problems(obj["decisions"], findings)
    if problems:
        return _respond_reject(problems)
    doc = D.build_document(routing, verdict_sha, obj["decisions"])
    path = D.decisions_path(repo_root, routing["attempt_round_dir"])
    data = D.write_document(path, doc)
    sha = base.sha256_bytes(data)
    _report({"mode": "respond", "state": "WRITTEN", "decisions_path": routing["attempt_round_dir"] + "/" + C.DECISIONS_FILE,
             "decisions_sha256": sha, "verdict_path": verdict_rel, "verdict_sha256": verdict_sha,
             "next_round": {"remediation_statement": D.dispositions_segment(doc["decisions"], findings, sha),
                            "accepted_residuals": D.derived_residuals(doc["decisions"], findings)}})
    return 0


# ---------------------------------------------------------------- standalone preflight

def run_preflight(opts):
    repo_root = opts.repo_root
    try:
        raw = base.read_bytes(opts.request)
        route = R.minimal_route(raw)
    except (OSError, base.UnrouteableError) as exc:
        _stderr("request-unrouteable: %s" % getattr(exc, "message", type(exc).__name__))
        _report({"mode": "preflight", "state": "REJECTED", "failure_code": "request-unrouteable"})
        return 1
    routing = derive_routing(route)
    try:
        archive=E.configured(repo_root,writable=True)
        if archive:E.check_pending(archive,routing['tracked_round_dir'])
        cleanup = [] if archive else preflight_cleanup(repo_root, routing, _recoverer_identity())
    except (base.PreflightError,E.EvidenceError) as exc:
        _report({"mode": "preflight", "state": "REJECTED", "failure_code": exc.code, "problems": [exc.message]})
        return 1
    workdir = tempfile.mkdtemp(prefix="review-channel-preflight-")
    try:
        pf = Preflight(repo_root, routing, "preflight", route["request"], opts, workdir)
        pf.run()
        pf.seal()
        report = dict(pf.report)
        report.update({"mode": "preflight", "state": "PASS", "cleanup": cleanup, "delivery": pf.delivery,
                       "task_file": pf.task_file, "input_manifest_sha256": pf.manifest["manifest_sha256"],
                       "changed_region": None if pf.changed_region is None else
                       [{"bundle_name": i["bundle_name"], "baseline": i["baseline"], "hunks": len(i["diff"]["hunks"]) if i["diff"] else None,
                         "note": i["note"]} for i in pf.changed_region],
                       "inputs": [{k: i[k] for k in ("bundle_name", "role", "bytes", "sha256")} for i in pf.final_inputs]})
        _report(report)
        return 0
    except base.PreflightError as exc:
        _stderr("preflight rejected [%s]: %s" % (exc.code, exc.message))
        _report({"mode": "preflight", "state": "REJECTED", "failure_code": exc.code, "routing": routing, "cleanup": cleanup})
        return 1
    except Exception as exc:  # noqa: BLE001
        _stderr("internal-error: %s: %s" % (type(exc).__name__, exc))
        return 2
    finally:
        base.remove_tree(workdir)


def finish_archive_round(attempt,pf,st,receipt_bytes,block):
    try:
        E.require(H.helper().head_commit(attempt.repo_root)==pf.history_view,'Git view changed before archive publication','archive-conflict')
        base.write_new(os.path.join(attempt.dir,RECEIPT_JSON),receipt_bytes)
        ref=E.capture_attempt(attempt.archive,attempt.dir,'round')
        _report({'attempt_id':attempt.attempt_id,'classification':'completed_with_valid_verdict','verdict':block['verdict'],
                 'source':ref,'review_result':E.minimal_result(attempt.repo_root,ref)})
        return 0
    except E.EvidenceError as exc:
        _report({'attempt_id':attempt.attempt_id,'classification':'reviewer_output_invalid','failure_code':exc.code,
                 'problems':[exc.message],'pending':attempt.dir,'provider_repeated':False})
        return 1
    except OSError as exc:
        _stderr('archive runtime failure; pending retained, no successful publication claimed: '+str(exc));return 2


def run_archive(opts):
    try:
        archive=E.Archive(opts.repo_root,restore=opts.operation=='restore')
        if opts.operation=='recover':result=E.recover(archive,opts.attempt)
        else:
            ref=E.strict(E.read_file(opts.ref));E.reference(ref)
            if opts.operation=='restore':result=archive.restore(ref,opts.destination)
            else:
                d,files=archive.read(ref);result={'source':ref,'kind':d['kind'],'files':len(files),'both_copies_verified':True}
        _report({'mode':'archive','operation':opts.operation,'state':'COMPLETE','result':result});return 0
    except E.EvidenceError as exc:
        _report({'mode':'archive','state':'REJECTED','failure_code':exc.code,'problems':[exc.message]});return 1
    except (OSError,ValueError,TypeError) as exc:
        _stderr('archive runtime error: '+type(exc).__name__);return 2

# ---------------------------------------------------------------- main

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        sub, opts, rest = parse_args(argv)
    except base.ChannelError as exc:
        _stderr("usage: %s" % exc.message)
        _stderr(__doc__)
        return 2
    probs = base.startup_contract_problems()
    if probs:
        _stderr("startup form violated (%s); use the sanitized launch form" % ", ".join(probs))
        return 2
    if sub=='archive':return run_archive(opts)
    if sub == "selftest":
        import review_channel_selftest
        return review_channel_selftest.main(rest)
    if sub == "preflight":
        return run_preflight(opts)
    if sub == "respond":
        return run_respond(opts)
    return run_attempt(sub, opts)


if __name__ == "__main__":
    sys.exit(main())
