"""adapters/codex — codex 命令行适配器（评审通道设计 §7.2；档案库 runner 设计 §6 至 §8 隔离语义的全文承载）。

本模块是该调用工具的全部专属知识所在：命令行参数、配置文件键名、事件流格式、stderr 关键词、认证文件路径。
只依赖适配器接口模块、契约常量与基座（设计 §5.2）。

ctx 字段（由入口装配）：mode · provider_id · provider · model_slug · model · effort · overrides · secrets（SecretHandle）
· attempt_dir · seal_dir · instruction · timeout_seconds · call_index · inputs（预清单）。
"""
import http.server
import hashlib
import json
import os
import shutil
import subprocess
import threading

import review_channel_base as base
import review_channel_runtime as RT

RUNTIME_ID = "codex"
SUPPORTED_KINDS = ("official-direct", "aggregator", "builtin-native")
SUPPORTED_TRANSPORTS = ("responses", "chat", "builtin")
INLINE_DELIVERY = False

PROTECTED_PREFIXES = (
    "model", "model_provider", "model_providers", "model_reasoning_effort", "shell_environment_policy", "sandbox",
    "sandbox_permissions", "approval", "approvals_reviewer", "mcp_servers", "hooks", "plugins", "rules", "memories",
    "features", "notify", "history", "projects", "experimental", "profile", "profiles", "tools", "web_search",
)
RECOGNITION_ERROR_MARKERS = ("unknown configuration field", "error loading config", "invalid type", "unknown field")
# stderr 关键词分类（§7.2 观察点；R1-B8 整改：该知识只住本模块）
AUTH_MARKERS = ("401", "unauthorized", "invalid api key", "invalid_api_key", "authentication", "not logged in", "login")
CONFIG_MARKERS = RECOGNITION_ERROR_MARKERS + ("unexpected argument", "invalid value", "unrecognized")
UNAVAILABLE_MARKERS = ("connection", "dns", "refused", "unreachable", "timed out", "timeout", "503", "502", "429", "network")


def classify_failure(stderr_text, timed_out):
    """进程失败或超时 → §6.7 四种基础设施分类之一。"""
    if timed_out:
        return "timeout_or_transport_failure"
    lowered = stderr_text.lower()
    if any(m in lowered for m in AUTH_MARKERS):
        return "authentication_failed"
    if any(m in lowered for m in CONFIG_MARKERS):
        return "runtime_rejected_config"
    if any(m in lowered for m in UNAVAILABLE_MARKERS):
        return "provider_unavailable"
    return "timeout_or_transport_failure"
BINARY_NAME = "codex"
AUTH_FILE_NAME = "auth.json"
RECOGNITION_TIMEOUT = 90


# ---------------------------------------------------------------- 专属知识：配置与环境

def binary_path():
    p = shutil.which(BINARY_NAME)
    if not p:
        raise base.PreflightError("runtime-binary-missing", "codex executable not found on PATH")
    return os.path.realpath(p)


def tool_version(binary, temp_home):
    """`codex --version`（§7.6 只记录）；在从空构造的环境下执行，HOME 指向 attempt 临时目录。"""
    try:
        env = process_env(binary, temp_home, os.path.join(temp_home, "version-codex-home"))
        os.makedirs(env["CODEX_HOME"], mode=0o700, exist_ok=True)
        proc = subprocess.run([binary, "--version"], capture_output=True, timeout=60, env=env, cwd=temp_home,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (proc.stdout or proc.stderr).decode("utf-8", "replace").strip() or "unknown"


def render_config(provider_id, provider, model, effort, base_url, retry_zero=False):
    """隔离 CODEX_HOME 内的临时 config.toml（§7.2）。端点值只在此文件中，用后删除。"""
    if provider["kind"] == "builtin-native":
        return "\n".join([
            'model = "%s"' % model,
            'model_reasoning_effort = "%s"' % effort,
            "",
            "[shell_environment_policy]",
            'inherit = "none"',
            "",
            "[tools]",
            "web_search = false",
        ]) + "\n"
    lines = [
        'model = "%s"' % model,
        'model_reasoning_effort = "%s"' % effort,
        'model_provider = "%s"' % provider_id,
        "",
        "[model_providers.%s]" % provider_id,
        'name = "%s"' % provider.get("display_name", provider_id).replace('"', ""),
        'base_url = "%s"' % base_url,
        'env_key = "%s"' % provider["key_env"],
        'wire_api = "%s"' % provider["transport"],
    ]
    if retry_zero:
        lines += ["request_max_retries = 0", "stream_max_retries = 0"]
    lines += ["", "[shell_environment_policy]", 'inherit = "none"', "", "[tools]", "web_search = false"]
    return "\n".join(lines) + "\n"


def override_args(overrides):
    args = []
    for k in sorted(overrides):
        v = overrides[k]
        if isinstance(v, bool):
            text = "true" if v else "false"
        elif isinstance(v, (int, float)):
            text = str(v)
        else:
            text = json.dumps(v, ensure_ascii=False)
        args += ["-c", "%s=%s" % (k, text)]
    return args


def check_overrides(provider, overrides):
    if provider["kind"] == "builtin-native" and overrides:
        raise base.PreflightError("runtime-key-protected", "builtin-native accepts no runtime override")
    for key in overrides:
        head = key.split(".", 1)[0]
        if head in PROTECTED_PREFIXES or key in PROTECTED_PREFIXES:
            raise base.PreflightError("runtime-key-protected", "override %r hits a governance-protected prefix" % key)


def process_env(binary, temp_home, codex_home, key_env=None):
    env = RT.base_process_env(os.path.dirname(binary), temp_home)
    env["CODEX_HOME"] = codex_home
    if key_env:
        env.update(key_env)
    return env


def exec_argv(binary, seal_dir, last_message_path, overrides, instruction, with_json=True):
    argv = [binary, "exec", "--strict-config", "--skip-git-repo-check", "--ephemeral", "--ignore-rules",
            "--sandbox", "read-only", "--color", "never"]
    if seal_dir is not None:
        argv += ["-C", seal_dir]
    if with_json:
        argv += ["--json"]
    if last_message_path is not None:
        argv += ["-o", last_message_path]
    argv += override_args(overrides)
    argv.append(instruction)
    return argv


# ---------------------------------------------------------------- 识别探针（§7.2）

class _Responder:
    def __init__(self):
        responder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _respond(self):
                responder.count += 1
                body = b'{"error": "review-channel recognition probe"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = _respond

            def log_message(self, *args):
                pass

        self.count = 0
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def classify_recognition(output_text):
    lowered = output_text.lower()
    return "unrecognized" if any(m in lowered for m in RECOGNITION_ERROR_MARKERS) else "recognized"


def recognition_probe(ctx, binary):
    provider = ctx["provider"]
    if provider["kind"] == "builtin-native":
        return "not-applicable-builtin-native"
    work = os.path.join(ctx["attempt_dir"], "recognition")
    probe_home = os.path.join(work, "codex-home")
    temp_home = os.path.join(work, "home")
    os.makedirs(probe_home, mode=0o700)
    os.makedirs(temp_home)
    responder = _Responder()
    try:
        cfg = render_config(ctx["provider_id"], provider, ctx["model_slug"], ctx["effort"],
                            "http://127.0.0.1:%d/v1" % responder.port, retry_zero=True)
        with open(os.path.join(probe_home, "config.toml"), "w", encoding="utf-8") as f:
            f.write(cfg)
        env = process_env(binary, temp_home, probe_home, {provider["key_env"]: "preflight-recognition-dummy"})
        argv = exec_argv(binary, None, None, ctx["overrides"], "recognition probe", with_json=False)
        output = ""
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=RECOGNITION_TIMEOUT, env=env, cwd=temp_home,
                                  stdin=subprocess.DEVNULL)
            output = (proc.stderr or b"").decode("utf-8", "replace") + (proc.stdout or b"").decode("utf-8", "replace")
        except subprocess.TimeoutExpired as exc:
            output = ((exc.stderr or b"") + (exc.stdout or b"")).decode("utf-8", "replace")
        if classify_recognition(output) == "unrecognized":
            raise base.PreflightError("runtime-key-unrecognized", "the codex runtime rejected the merged configuration at load")
        if responder.count < 1:
            raise base.PreflightError("runtime-recognition-indeterminate",
                                      "neither a config-load rejection nor a transport request was observed")
        return "checked"
    finally:
        responder.close()
        base.remove_tree(work)


# ---------------------------------------------------------------- 接口

def preflight(ctx):
    """零提供方接触：二进制在场、受保护前缀、识别探针。返回事实 dict（进 Receipt 诊断层）。"""
    binary = binary_path()
    check_overrides(ctx["provider"], ctx["overrides"])
    version_home = os.path.join(ctx["attempt_dir"], "version-home")
    os.makedirs(version_home, exist_ok=True)
    try:
        version = tool_version(binary, version_home)
    finally:
        base.remove_tree(version_home)
    recognition = recognition_probe(ctx, binary) if ctx.get("recognition_probe", True) else "skipped"
    return {"binary": binary, "tool_version": version, "recognition": recognition}


def extract_events(events_text):
    count = 0
    usage = None
    request_ids = []
    for line in events_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        count += 1

        def walk(node):
            nonlocal usage
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "usage" and isinstance(v, dict) and usage is None:
                        usage = v
                    elif k in ("request_id", "response_id") and isinstance(v, str):
                        if v not in request_ids:
                            request_ids.append(v)
                    else:
                        walk(v)
            elif isinstance(node, list):
                for c in node:
                    walk(c)

        walk(event)
    return {"jsonl_event_count": count, "usage": usage if usage is not None else "unknown",
            "request_ids": request_ids or "unknown", "request_count": len(request_ids) if request_ids else "unknown"}


def run(ctx):
    """一次调用（首次或补发）。返回 RT.empty_result() 形态。"""
    result = RT.empty_result()
    binary = binary_path()
    expected=ctx.get("expected_program_identity")
    if expected is not None:
        actual=os.path.realpath(binary)
        if actual!=expected["launcher"] or hashlib.sha256(base.read_bytes(actual)).hexdigest()!=expected["binaryDigest"]:
            raise base.ChannelError("execution-port-identity-unverified","program changed before adapter run")
        binary=actual
    provider = ctx["provider"]
    call_dir = os.path.join(ctx["attempt_dir"], "call-%d" % ctx["call_index"])
    codex_home = os.path.join(call_dir, "codex-home")
    temp_home = os.path.join(call_dir, "home")
    os.makedirs(codex_home, mode=0o700)
    os.makedirs(temp_home)
    result["tool_version"] = tool_version(binary, temp_home)
    last_message_path = os.path.join(call_dir, "last_message.md")
    config_path = os.path.join(codex_home, "config.toml")
    try:
        return _run_in(ctx, binary, provider, call_dir, codex_home, temp_home, last_message_path, config_path, result)
    finally:
        # R3-B3 / R4-B5 整改：调用工作区（原始最终消息、CODEX_HOME 状态库与缓存）不得持久留存——契约层只落经扫描的副本；
        # 清理异常时逐件覆写截断后再删，仍有残留即以内部异常收口（调用后 internal-error），不得静默留下未扫描字节
        destroy_workspace(call_dir)


def _unlock(path):
    """解除阻碍覆写 / 删除的属性（macOS 用户不可变标志、权限位）。"""
    if hasattr(os, "chflags"):
        try:
            os.chflags(path, 0, follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass
    try:
        os.chmod(path, 0o700 if os.path.isdir(path) else 0o600, follow_symlinks=False)
    except (OSError, NotImplementedError):
        pass


def destroy_workspace(call_dir):
    """R3-B3 / R4-B5 / R5-B1：整体删除；失败时逐件解锁 → 截断 → 删除；仍留非空字节即内部异常（不得静默留下未扫描字节）。"""
    try:
        base.remove_tree(call_dir)
    except OSError:
        pass
    if not os.path.lexists(call_dir):
        return
    # 先自顶向下解锁每一级目录（不可读 / 不可搜索 / 不可变的子目录先恢复可遍历），再自底向上截断与删除（R5-B7）
    for _round in range(2):
        for root, dirs, _files in os.walk(call_dir, topdown=True, followlinks=False):
            for name in dirs:
                path = os.path.join(root, name)
                if not os.path.islink(path):
                    _unlock(path)
    residue, unscrubbed = [], []
    for root, dirs, files in os.walk(call_dir, topdown=False, onerror=lambda exc: residue.append(exc.filename), followlinks=False):
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path):
                try:
                    os.unlink(path)
                except OSError:
                    residue.append(path)
                continue
            _unlock(path)
            try:
                with open(path, "r+b") as f:
                    f.truncate(0)
            except OSError:
                pass
            try:
                os.unlink(path)
            except OSError:
                residue.append(path)
                try:
                    if os.lstat(path).st_size > 0:
                        unscrubbed.append(path)
                except OSError:
                    pass
        for name in dirs:
            path = os.path.join(root, name)
            _unlock(path)
            try:
                os.unlink(path) if os.path.islink(path) else os.rmdir(path)
            except OSError:
                residue.append(path)
    _unlock(call_dir)
    try:
        os.rmdir(call_dir)
    except OSError:
        residue.append(call_dir)
    if residue:
        raise RuntimeError("call workspace residue could not be removed (%d entries, %d still carrying bytes): %s"
                           % (len(residue), len(unscrubbed), os.path.relpath(residue[0], call_dir)))


def _run_in(ctx, binary, provider, call_dir, codex_home, temp_home, last_message_path, config_path, result):
    try:
        key_env = {}
        if provider["kind"] == "builtin-native":
            values = base.stage_auth_copy(provider["auth_source"], os.path.join(codex_home, AUTH_FILE_NAME))
            ctx["secrets"].add_scan_values(values)
            base_url = None
        else:
            base_url = ctx["secrets"].value_of(provider["base_env"])
            key_env = {provider["key_env"]: ctx["secrets"].value_of(provider["key_env"])}
            if base_url is None or key_env[provider["key_env"]] is None:
                raise base.ChannelError("secret-missing", "secret handle lacks the provider bindings")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(render_config(ctx["provider_id"], provider, ctx["model_slug"], ctx["effort"], base_url))
        os.chmod(config_path, 0o600)
        env = process_env(binary, temp_home, codex_home, key_env)
        argv = exec_argv(binary, ctx["seal_dir"], last_message_path, ctx["overrides"], ctx["instruction"])
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=ctx["timeout_seconds"], env=env,
                                  cwd=ctx["seal_dir"], stdin=subprocess.DEVNULL)
            timed_out, exit_code, stdout, stderr = False, proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out, exit_code, stdout, stderr = True, None, exc.stdout or b"", exc.stderr or b""
    finally:
        try:
            os.unlink(config_path)
        except OSError:
            pass
        try:
            os.unlink(os.path.join(codex_home, AUTH_FILE_NAME))
        except OSError:
            pass
    try:
        final = base.read_bytes(last_message_path)
    except OSError:
        final = b""
    # 原始最终消息读取后立即原地截断并删除（R4-B5）：工作区整体销毁之前也不留原文
    try:
        with open(last_message_path, "r+b") as f:
            f.truncate(0)
        os.unlink(last_message_path)
    except OSError:
        pass
    stderr_text = stderr.decode("utf-8", "replace")
    result["final_message"] = final
    result["process"] = {"exit_code": exit_code, "timed_out": timed_out, "stderr_summary": stderr_text[-400:]}
    result["runtime_evidence"] = extract_events(stdout.decode("utf-8", "replace"))
    result["raw"] = {"events": stdout, "stderr": stderr}
    if timed_out or exit_code != 0:
        result["failure"] = classify_failure(stderr_text, timed_out)
    return result
