"""review_channel_runtime — 适配器接口、数据驱动发现与装载、运行证据的统一形态（设计 §5.2 / §7.1）。

适配器 = `adapters/<x>.py`，以文件路径装载、模块名 `review_channel_adapter_<x>`（不依赖包导入，
不与标准库同名模块冲突）。每个适配器导出：
  RUNTIME_ID            = 去扩展名的文件名
  SUPPORTED_KINDS       = provider kind 子集
  SUPPORTED_TRANSPORTS  = transport 子集
  INLINE_DELIVERY       = 布尔（True = 内联投递，评审方无工具；Receipt delivery = inline）
  preflight(ctx)        = 零提供方接触的适配器侧检查（受保护前缀、二进制在场、识别探针…），失败抛 PreflightError
  run(ctx)              = 一次调用；返回 CallResult dict（见 `empty_result`）
本模块只依赖基座与契约常量，不认识任何具体适配器。
"""
import importlib.util
import os
import sys

import review_channel_base as base
import review_channel_contract as C

ADAPTERS_DIR = os.path.join(base.UNIT_DIR, "adapters")
ADAPTER_MODULE_PREFIX = "review_channel_adapter_"
REQUIRED_ATTRS = ("RUNTIME_ID", "SUPPORTED_KINDS", "SUPPORTED_TRANSPORTS", "INLINE_DELIVERY", "preflight", "run")


def load_adapter(runtime_id, adapters_dir=ADAPTERS_DIR):
    """按 Registry runtime 值装载 adapters/<x>.py；文件不存在返回 None。"""
    if not runtime_id or not all(c.isalnum() or c == "_" for c in runtime_id):
        return None
    path = os.path.join(adapters_dir, runtime_id + ".py")
    if not os.path.isfile(path):
        return None
    modname = ADAPTER_MODULE_PREFIX + runtime_id
    cached = sys.modules.get(modname)
    if cached is not None and getattr(cached, "__file__", None) == path:
        return cached
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(modname, None)
        raise
    for attr in REQUIRED_ATTRS:
        if not hasattr(mod, attr):
            raise base.PreflightError("registry-invalid", "adapter %s lacks %s" % (runtime_id, attr))
    return mod


def empty_result():
    """CallResult 统一形态；适配器填充后返回。"""
    return {
        "final_message": b"",           # 评审方最终消息全文（bytes）
        "runtime_evidence": {"jsonl_event_count": "unknown", "usage": "unknown",
                             "request_ids": "unknown", "request_count": "unknown"},
        "identity_claim": "unreported",  # 应答侧模型标识
        "process": {"exit_code": None, "timed_out": False, "stderr_summary": ""},
        "tool_version": None,            # 命令行工具版本（调用工具适配器）
        "api_endpoint_id": None,         # 端点标识（直接接口适配器）
        "diagnostics": {},               # {名: attempt 目录内相对路径}
        "failure": None,                 # None | INFRA_CLASSIFICATIONS 之一
        "http_status": None,
        "retry": {"transport_retry_state": "unknown", "provider_retry_state": "unknown"},
    }


def assess_call_path(mode, result):
    """§6.11 三维之一（单次调用）。"""
    if result["failure"] is not None:
        return "NOT_PROVEN"
    proc = result["process"]
    if proc["timed_out"] or (proc["exit_code"] not in (None, 0)):
        return "NOT_PROVEN"
    if result["http_status"] is not None and not (200 <= result["http_status"] < 300):
        return "NOT_PROVEN"
    text = result["final_message"].decode("utf-8", "replace")
    if not text.strip():
        return "INDETERMINATE"
    if mode == "probe":
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        return "PROVEN" if lines == [C.PROBE_PAYLOAD] else "INDETERMINATE"
    return "PROVEN"


def assess_profile_binding(kind, identity_claim, requested_model, registered_equivalents):
    """§6.11 三维之二（单次调用）。返回 (profile_binding, logical_model_match)。"""
    claim = identity_claim if isinstance(identity_claim, str) and identity_claim else "unreported"
    if claim == "unreported":
        if kind == "aggregator":
            return "INSUFFICIENT", "unreported"
        return "SUFFICIENT", "unreported"
    if claim == requested_model:
        return "SUFFICIENT", "exact"
    equivalents = registered_equivalents or {}
    registered = equivalents.get(requested_model)
    # R5-B2 整改：登记值为字符串 → 只认全等；为数组 → 只认成员全等；其他类型不构成登记
    if (isinstance(registered, str) and claim == registered) or \
            (isinstance(registered, list) and any(isinstance(x, str) and claim == x for x in registered)):
        return "SUFFICIENT", "registered_equivalent"
    return "MISMATCH", "mismatch"


def failure_from_adapter_error(code):
    """适配器在调用中抛出的有界错误 → 基础设施分类（R1-B9 整改：认证 staging 失败是认证失败事实）。"""
    if code.startswith("auth-source") or code == "secret-missing":
        return "authentication_failed"
    return "runtime_rejected_config"


def route_provenance(kind, lmm):
    """聚合商路由来源事实（§6.11 / R1-B10 整改）：应答侧模型标识不可见时 unverifiable。"""
    if kind != "aggregator":
        return "direct"
    if lmm in ("exact", "registered_equivalent"):
        return lmm
    if lmm == "mismatch":
        return "conflicting"
    return "unverifiable"


def build_call_record(call_index, mode, result, kind, requested_model, registered_equivalents,
                      requested_effort, effort_source):
    """把一次调用折算为 effective Profile `calls[]` 的一项（§6.6 调用组）。"""
    cpp = assess_call_path(mode, result)
    pb, lmm = assess_profile_binding(kind, result["identity_claim"], requested_model, registered_equivalents)
    claim = result["identity_claim"] if result["identity_claim"] != "unreported" else None
    return {
        "call_index": call_index,
        "response_model": claim,
        "logical_model_match": lmm,
        "upstream_route_visibility": "reported" if claim is not None else "unreported",
        "route_provenance": route_provenance(kind, lmm),
        "effective_effort": requested_effort,
        "effort_source": effort_source,
        "provider_effective_behavior": "unverified",
        "runtime_version": result["tool_version"] if result["tool_version"] is not None else result["api_endpoint_id"],
        "process": dict(result["process"]),
        "usage": result["runtime_evidence"].get("usage", "unknown"),
        "jsonl_event_count": result["runtime_evidence"].get("jsonl_event_count", "unknown"),
        "request_count": result["runtime_evidence"].get("request_count", "unknown"),
        "request_ids": result["runtime_evidence"].get("request_ids", "unknown"),
        "retry": dict(result["retry"]),
        "http_status": result["http_status"],
        "failure": result["failure"],
        "call_path_proof": cpp,
        "profile_binding": pb,
    }


def base_process_env(binary_dir, temp_home):
    """从空构造的子进程环境（§7.2）：PATH = 系统目录 + 工具目录；HOME / TMPDIR 指向 attempt 临时目录。"""
    tmp = os.path.join(temp_home, "tmp")
    os.makedirs(tmp, exist_ok=True)
    path = os.pathsep.join(dict.fromkeys([binary_dir, "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    return {"PATH": path, "HOME": temp_home, "TMPDIR": tmp, "LC_ALL": "C", "LANG": "C"}
