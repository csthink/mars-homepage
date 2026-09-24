"""adapters/http — 直接接口适配器（评审通道设计 §7.4）：通道自身经 HTTP 调用模型提供方接口。

三种线协议一个模块内分函数承载：OpenAI 兼容 chat completions（`chat`）、OpenAI responses（`responses`）、
Anthropic messages（`messages`）。投递形态 = 内联单次调用（任务书在前、被审对象次之、参考件在后，定界段携带
投递名、字节数、SHA-256）；指令注入为系统消息；不做分片、不做重试；单次请求即一次调用。
只依赖适配器接口模块、契约常量与基座。
"""
import json
import os
import urllib.error
import urllib.request

import review_channel_base as base
import review_channel_contract as C
import review_channel_runtime as RT

RUNTIME_ID = "http"
SUPPORTED_KINDS = ("official-direct", "aggregator")
SUPPORTED_TRANSPORTS = ("chat", "responses", "messages")
INLINE_DELIVERY = True
ANTHROPIC_VERSION = "2023-06-01"
THINKING_BUDGET = {"minimal": None, "low": 2048, "medium": 8192, "high": 16384, "xhigh": 32768}
MAX_OUTPUT_TOKENS = 32768
USER_AGENT = "csthink-harness-plane-review-channel/1"


def classify_http_failure(status, error_text):
    """HTTP 状态与传输错误 → §6.7 四种基础设施分类之一（R1-B8 整改：该知识只住本模块）。"""
    if status is None:
        lowered = (error_text or "").lower()
        if "timed out" in lowered or "timeout" in lowered:
            return "timeout_or_transport_failure"
        return "provider_unavailable"
    if 300 <= status < 400:
        return "provider_unavailable"   # 声明端点未给出应答而要求改道：不跟随、不算调用到达
    if status in (401, 403):
        return "authentication_failed"
    if status in (400, 404, 422):
        return "runtime_rejected_config"
    if status in (408, 429, 500, 502, 503, 504):
        return "provider_unavailable"
    return "timeout_or_transport_failure"


# 请求从空构造：不读取父进程环境与系统配置中的代理（§7.1 / §7.5，R5-B5 整改）；不做重定向以外的任何隐式路线变更
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx 不跟随（§7.4 单次请求即一次 attempt；认证头绝不带往未声明的目标，R6-B6 整改）：交由 HTTPError 分类。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def preflight(ctx):
    so = ctx["provider"].get("structured_output", "none")
    if so not in C.STRUCTURED_OUTPUTS:
        raise base.PreflightError("registry-invalid", "structured_output outside the closed set")
    if ctx["provider"].get("transport") == "messages" and so == "json_object":
        # r18 自查整改（R18-B4 线索）：messages 线协议没有 json_object 的线上形态；接受后忽略属设计禁止的 accepted-but-ignored
        raise base.PreflightError("registry-invalid", "messages transport has no wire form for structured_output = json_object; declare json_schema or none")
    # 直接接口适配器不承认任何 runtime_overrides 键（R5-B6 整改）：非空即拒，不得接受后静默忽略
    if ctx.get("overrides"):
        raise base.PreflightError("runtime-key-unrecognized",
                                  "the direct-interface adapter recognizes no runtime_overrides keys: %s" % sorted(ctx["overrides"]))
    return {"structured_output": so, "proxy": "disabled"}


# ---------------------------------------------------------------- 内联投递

def inline_user_message(seal_dir, inputs, task_file):
    """任务书在前、被审对象次之、参考件在后；每件定界段携带投递名、字节数、SHA-256。"""
    order = ([i for i in inputs if i["bundle_name"] == task_file] + [i for i in inputs if i["role"] == "candidate"]
             + [i for i in inputs if i["role"] == "reference"])
    parts = []
    for item in order:
        data = base.read_bytes(os.path.join(seal_dir, item["bundle_name"]))
        text = data.decode("utf-8", "replace")
        parts.append("===== BEGIN INPUT %s | role=%s | bytes=%d | sha256=%s =====\n%s\n===== END INPUT %s =====\n"
                     % (item["bundle_name"], item["role"], item["bytes"], item["sha256"], text, item["bundle_name"]))
    return "\n".join(parts)


def wrapper_schema():
    return {
        "type": "object",
        "properties": {"machine": {"type": "object"}, "narrative": {"type": "string"}},
        "required": ["machine", "narrative"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------- 三协议请求体

def build_chat(model, effort, system, user, structured):
    body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False, "max_tokens": MAX_OUTPUT_TOKENS, "reasoning_effort": effort}
    if structured == "json_object":
        body["response_format"] = {"type": "json_object"}
    elif structured == "json_schema":
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "review_channel_verdict", "schema": wrapper_schema()}}
    return "/chat/completions", body


def build_responses(model, effort, system, user, structured):
    body = {"model": model, "instructions": system, "input": [{"role": "user", "content": user}],
            "reasoning": {"effort": effort}, "max_output_tokens": MAX_OUTPUT_TOKENS, "store": False}
    if structured == "json_object":
        body["text"] = {"format": {"type": "json_object"}}
    elif structured == "json_schema":
        body["text"] = {"format": {"type": "json_schema", "name": "review_channel_verdict", "schema": wrapper_schema(),
                                   "strict": False}}
    return "/responses", body


def build_messages(model, effort, system, user, structured):
    body = {"model": model, "system": system, "messages": [{"role": "user", "content": user}],
            "max_tokens": MAX_OUTPUT_TOKENS}
    budget = THINKING_BUDGET[effort]
    if budget is not None:
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
    if structured == "json_schema":
        # r18 自查整改（R18-B4 线索）：物化为 Messages 接口的结构化输出（output_config.format，json_schema）；提供方若拒绝该
        # schema 形状即 HTTP 4xx → runtime_rejected_config，可见失败而非静默忽略
        body["output_config"] = {"format": {"type": "json_schema", "schema": wrapper_schema()}}
    elif structured == "json_object":
        raise base.ChannelError("internal-error", "messages transport: structured_output json_object is rejected at preflight")
    return "/messages", body


BUILDERS = {"chat": build_chat, "responses": build_responses, "messages": build_messages}


def headers_for(transport, key):
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
    if transport == "messages":
        h["x-api-key"] = key
        h["anthropic-version"] = ANTHROPIC_VERSION
    else:
        h["Authorization"] = "Bearer " + key
    return h


# ---------------------------------------------------------------- 三协议应答解析

def parse_chat(obj):
    choices = obj.get("choices") or []
    text = ""
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return text, obj.get("model"), obj.get("usage"), obj.get("id")


def parse_responses(obj):
    text = ""
    for item in obj.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text += part.get("text", "")
    if not text and isinstance(obj.get("output_text"), str):
        text = obj["output_text"]
    return text, obj.get("model"), obj.get("usage"), obj.get("id")


def parse_messages(obj):
    text = ""
    for part in obj.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            text += part.get("text", "")
    return text, obj.get("model"), obj.get("usage"), obj.get("id")


PARSERS = {"chat": parse_chat, "responses": parse_responses, "messages": parse_messages}


def endpoint_id(transport, base_url):
    return "%s:%s" % (transport, base.sha256_bytes(base_url.encode("utf-8"))[:16])


# ---------------------------------------------------------------- 接口

def run(ctx):
    result = RT.empty_result()
    provider = ctx["provider"]
    transport = provider["transport"]
    key = ctx["secrets"].value_of(provider["key_env"])
    base_url = ctx["secrets"].value_of(provider["base_env"])
    if key is None or base_url is None:
        raise base.ChannelError("secret-missing", "secret handle lacks the provider bindings")
    base_url = base_url.rstrip("/")
    result["api_endpoint_id"] = endpoint_id(transport, base_url)
    if ctx["call_index"] == 1:
        user = inline_user_message(ctx["seal_dir"], ctx["inputs"], ctx["task_file"]) if ctx["mode"] == "review" \
            else "PROBE"
    else:
        user = ctx["instruction_user"]
    path, body = BUILDERS[transport](ctx["model_slug"], ctx["effort"], ctx["instruction"], user,
                                     provider.get("structured_output", "none") if ctx["mode"] == "review" else "none")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=data, method="POST", headers=headers_for(transport, key))
    status = None
    raw = b""
    resp_headers = {}
    try:
        with OPENER.open(req, timeout=ctx["timeout_seconds"]) as resp:
            status = resp.status
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        resp_headers = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result["failure"] = classify_http_failure(None, str(exc))
        result["process"] = {"exit_code": None, "timed_out": "timed out" in str(exc).lower(),
                             "stderr_summary": type(exc).__name__}
        result["raw"] = {"response": raw, "request_shape": json.dumps({k: v for k, v in body.items() if k not in
                                                                       ("messages", "input", "system", "instructions")}).encode()}
        return result
    result["http_status"] = status
    result["raw"] = {"response": raw, "request_shape": json.dumps({k: v for k, v in body.items() if k not in
                                                                   ("messages", "input", "system", "instructions")}).encode()}
    result["process"] = {"exit_code": None, "timed_out": False, "stderr_summary": "HTTP %s" % status}
    rid = resp_headers.get("x-request-id") or resp_headers.get("request-id")
    if not (200 <= status < 300):
        result["failure"] = classify_http_failure(status, raw.decode("utf-8", "replace")[:200])
        result["runtime_evidence"] = {"jsonl_event_count": 1, "usage": "unknown", "request_ids": [rid] if rid else "unknown",
                                      "request_count": 1}
        return result
    try:
        obj = json.loads(raw.decode("utf-8"))
        text, model_id, usage, body_id = PARSERS[transport](obj)
    except (ValueError, UnicodeDecodeError, AttributeError, TypeError):
        text, model_id, usage, body_id = "", None, None, None
    ids = [x for x in (rid, body_id) if isinstance(x, str) and x]
    result["final_message"] = text.encode("utf-8")
    result["identity_claim"] = model_id if isinstance(model_id, str) and model_id else "unreported"
    result["runtime_evidence"] = {"jsonl_event_count": 1, "usage": usage if isinstance(usage, dict) else "unknown",
                                  "request_ids": ids or "unknown", "request_count": 1}
    return result
