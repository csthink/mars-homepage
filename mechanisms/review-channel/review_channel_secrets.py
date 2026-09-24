"""review_channel_secrets — 秘密来源（评审通道设计 §7.5）。

`~/.zshrc` 非执行解析（只接受 `export NAME=<literal>` 整行、三种字面量；一切 substitution / glob /
重定向 / 管道 / 控制运算符 / 行接续 / 行尾注释 / 重复声明 fail closed），认证文件三态（经基座），
以及本次 attempt 扫描集的构造。解析结果只进入进程内的 SecretHandle，绝不进入任何报告。
"""
import os
import re

import review_channel_base as base

ZSHRC_PATH = os.path.join(os.path.expanduser("~"), ".zshrc")
_BARE_LITERAL_RE = re.compile(r"^[A-Za-z0-9._:/~%-]+$")
_SINGLE_QUOTED_RE = re.compile(r"^'[^']*'$")
_DOUBLE_QUOTED_RE = re.compile(r'^"[^"$`\\]*"$')
_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
STATES = ("detected_literal", "missing", "duplicate", "dynamic_rejected")


class SecretHandle:
    """进程内秘密句柄：值只经 `value_of` 取出给适配器执行上下文；`scan_set` 供落盘前终检。"""

    def __init__(self):
        self._values = {}
        self._extra = []

    def put(self, name, value):
        self._values[name] = value

    def add_scan_values(self, values):
        for v in values:
            if v:
                self._extra.append(v)

    def value_of(self, name):
        return self._values.get(name)

    def drop_values(self, values):
        """移除与治理工件结构字节重叠、不可受值级脱敏保护的秘密值（preflight 已以 secret-structural-collision 拒绝）。"""
        drop = set(values)
        self._values = {n: v for n, v in self._values.items() if (v.encode("utf-8") if isinstance(v, str) else v) not in drop}
        self._extra = [v for v in self._extra if (v.encode("utf-8") if isinstance(v, str) else v) not in drop]

    def names(self):
        return sorted(self._values)

    def scan_set(self):
        out = []
        for v in list(self._values.values()) + self._extra:
            if isinstance(v, str):
                v = v.encode("utf-8")
            if v and v not in out:
                out.append(v)
        return out


def parse_zshrc_literals(text, names):
    """返回 {name: {"state": ..., "value": str|None}}。整行匹配：行须恰为 `export NAME=<rhs>`（允许首尾空白）。"""
    results = {n: {"state": "missing", "value": None} for n in names}
    occurrences = {n: [] for n in names}
    for raw in text.split("\n"):
        line = raw.strip()
        if not line.startswith("export "):
            continue
        rest = line[len("export "):]
        if "=" not in rest:
            continue
        name, rhs = rest.split("=", 1)
        name = name.strip()
        if name in occurrences:
            occurrences[name].append(rhs)
    for n in names:
        found = occurrences[n]
        if not found:
            continue
        if len(found) > 1:
            results[n] = {"state": "duplicate", "value": None}
            continue
        rhs = found[0]
        if _BARE_LITERAL_RE.match(rhs):
            value = rhs
        elif _SINGLE_QUOTED_RE.match(rhs) or _DOUBLE_QUOTED_RE.match(rhs):
            value = rhs[1:-1]
        else:
            results[n] = {"state": "dynamic_rejected", "value": None}
            continue
        results[n] = {"state": "detected_literal", "value": value}
    return results


def resolve_env_names(names, zshrc_path=ZSHRC_PATH):
    """解析变量名集合；返回 (handle_updates: {name: value}, report: {name: state})。
    任一非 detected_literal 即抛 PreflightError（状态只进失败码，不带值）。"""
    for n in names:
        if not _NAME_RE.match(n):
            raise base.PreflightError("registry-invalid", "environment variable name grammar: %s" % n)
    try:
        with open(zshrc_path, "rb") as f:
            text = f.read().decode("utf-8", "replace")
    except OSError:
        raise base.PreflightError("zshrc-unreadable", "configuration source ~/.zshrc unreadable")
    parsed = parse_zshrc_literals(text, list(names))
    report = {n: parsed[n]["state"] for n in names}
    for n in names:
        st = parsed[n]["state"]
        if st == "missing":
            raise base.PreflightError("secret-missing", "variable %s: missing" % n)
        if st == "duplicate":
            raise base.PreflightError("secret-duplicate", "variable %s: duplicate" % n)
        if st == "dynamic_rejected":
            raise base.PreflightError("secret-dynamic-rejected", "variable %s: dynamic_rejected" % n)
    return {n: parsed[n]["value"] for n in names}, report


def resolve_for_provider(provider, handle, zshrc_path=ZSHRC_PATH):
    """按 provider kind 解析秘密：official-direct / aggregator 解析 key_env 与 base_env；
    builtin-native 核认证文件三态并扩展扫描集。返回环境状态报告（只含状态词）。"""
    kind = provider["kind"]
    if kind == "builtin-native":
        state, values = base.inspect_auth_source(provider["auth_source"])
        if state == "missing":
            raise base.PreflightError("auth-source-missing", "authentication source: missing")
        if state == "malformed":
            raise base.PreflightError("auth-source-malformed", "authentication source: malformed")
        handle.add_scan_values(values)
        return {"auth_source": state}
    names = [provider["key_env"], provider["base_env"]]
    values, report = resolve_env_names(names, zshrc_path)
    for n, v in values.items():
        handle.put(n, v)
    return report


def union_scan_set(registry_path, zshrc_path=ZSHRC_PATH):
    """分配 attempt 之前的预扫描集（R7-B1 整改）：Registry 全部 provider 声明的秘密（key / base 字面量、builtin 认证载荷）
    的并集；任一 provider 解析失败只跳过（此处不判失败，失败仍由 preflight 清单按选中 Profile 判定）。返回 bytes 列表。"""
    try:
        reg = base.strict_json_load(base.read_bytes(registry_path))
        providers = reg["providers"]
    except (OSError, ValueError, UnicodeDecodeError, KeyError, TypeError):
        return []
    out = []
    for prov in (providers.values() if isinstance(providers, dict) else []):
        if not isinstance(prov, dict):
            continue
        try:
            if prov.get("kind") == "builtin-native":
                state, values = base.inspect_auth_source(prov["auth_source"])
                if state == "present":
                    out.extend(values)
            else:
                # R8-B1 整改：逐个变量名解析——一个名字不可解析不丢弃另一个已知字面值
                for name in (prov.get("key_env"), prov.get("base_env")):
                    if not isinstance(name, str):
                        continue
                    try:
                        values, _report = resolve_env_names([name], zshrc_path)
                        out.extend(values.values())
                    except base.PreflightError:
                        continue
        except (base.PreflightError, KeyError, TypeError, OSError):
            continue
    handle = SecretHandle()
    handle.add_scan_values(out)
    return handle.scan_set()
