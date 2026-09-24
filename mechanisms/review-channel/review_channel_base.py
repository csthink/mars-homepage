"""review_channel_base — 共享基座（评审通道设计 §5.1 / §5.2）。

承载可共享的机制：错误类型、canonical JSON 与哈希、时间、原子发布原语、秘密扫描、
认证文件三态、git 调用。本模块不导入任何契约模块，也不含任何调用工具专属知识。
"""
import datetime
import hashlib
import json
import os
import secrets as _secrets
import shutil
import subprocess
import sys

UNIT_DIR = os.path.dirname(os.path.realpath(__file__))
# 仓库根发现 = 从可执行物位置向上解析（本通道设计 §5.3）
DEFAULT_REPO_ROOT = os.path.realpath(os.path.join(UNIT_DIR, os.pardir, os.pardir))
RULES_CATALOG_PATH = "mechanisms/gates/rules_catalog.json"
REDACTED = b"[REDACTED-SECRET]"

# 子进程白名单环境（本通道设计 §5.3：不继承父进程其他环境）
ENV_WHITELIST = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "LC_ALL": "C",
    "LANG": "C",
    "TZ": "UTC",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}
GIT_CONFIG_OVERRIDES = (
    "-c", "core.quotepath=off",
    "-c", "core.autocrlf=false",
    "-c", "core.safecrlf=false",
    "-c", "core.eol=lf",
    "-c", "core.ignorecase=false",
)


# ---------------------------------------------------------------- 错误类型

class ChannelError(Exception):
    """通道内有界失败；`code` 为失败码（契约常量模块闭集），`message` 只进 stderr 报告。"""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class PreflightError(ChannelError):
    """preflight 拒绝（含 Request 完整校验失败）。"""


class UnrouteableError(ChannelError):
    """真正不可路由：无 attempt、无 Receipt（设计 §6.1）。"""

    def __init__(self, message=""):
        super().__init__("request-unrouteable", message)


class StartupError(ChannelError):
    """启动期失败（运行时身份不在允许集合），退出码 2。"""


# ---------------------------------------------------------------- canonical JSON 与哈希（设计 §6.2 第 5 条）

def canonical_json(obj):
    """UTF-8、键按码点升序递归排序、无空白分隔符、非 ASCII 不转义、无末尾换行。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def pretty_json(obj):
    """人读持久化形态（Receipt、清单、状态记录）：缩进 2、非 ASCII 不转义、末尾换行。"""
    return (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def strict_json_load(data):
    """严格 JSON 解析：UTF-8、拒 BOM、拒非标准数值常量、拒成员名重复。失败抛 ValueError。"""
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError("BOM")
    text = data.decode("utf-8", "strict")

    def _reject(token):
        raise ValueError("non-standard numeric constant: %s" % token)

    def _no_dup(pairs):
        seen = set()
        for k, _v in pairs:
            if k in seen:
                raise ValueError("duplicate member: %s" % k)
            seen.add(k)
        return dict(pairs)

    obj = json.loads(text, parse_constant=_reject, object_pairs_hook=_no_dup)
    _reject_lone_surrogates(obj)   # r18 自查整改（R18-B2 线索）：合法 JSON 的孤立代理项转义不可编码为 UTF-8，视同不可解析
    return obj


def _reject_lone_surrogates(obj):
    """JSON 文本层合法但 `\\ud800` 一类孤立代理项在任何后续 UTF-8 编码点都会抛 UnicodeEncodeError；严格解析在此 fail closed。"""
    if isinstance(obj, str):
        try:
            obj.encode("utf-8", "strict")
        except UnicodeEncodeError:
            raise ValueError("lone surrogate in JSON string")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _reject_lone_surrogates(k)
            _reject_lone_surrogates(v)
    elif isinstance(obj, list):
        for v in obj:
            _reject_lone_surrogates(v)


def is_strict_int(value):
    return type(value) is int


def is_nonempty_str(value):
    return isinstance(value, str) and value != ""


# ---------------------------------------------------------------- 时间与身份

def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def new_attempt_id():
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "%s-%s" % (stamp, _secrets.token_hex(4))


def process_start_token(pid):
    """进程代际令牌（设计 §8.2 第 6 步）：macOS 取 `ps -o lstart=`，Linux 取 /proc/<pid>/stat 第 22 字段 + boot_id。
    取不到返回 None（调用方保守视为存活）。"""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/%d/stat" % pid, "rb") as f:
                fields = f.read().rsplit(b")", 1)[1].split()
            with open("/proc/sys/kernel/random/boot_id", "rb") as f:
                boot = f.read().strip().decode("ascii", "replace")
            return "%s@%s" % (fields[19].decode("ascii"), boot)
        except (OSError, IndexError, UnicodeDecodeError):
            return None
    try:
        out = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)], env=dict(ENV_WHITELIST),
                             capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.decode("utf-8", "replace").strip()
    return text or None


def process_state(pid):
    """返回 'zombie' | 'alive' | 'absent' | None（不可判）。"""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/%d/stat" % pid, "rb") as f:
                fields = f.read().rsplit(b")", 1)[1].split()
            return "zombie" if fields[0] == b"Z" else "alive"
        except FileNotFoundError:
            return "absent"
        except (OSError, IndexError):
            return None
    try:
        out = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], env=dict(ENV_WHITELIST),
                             capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.decode("utf-8", "replace").strip()
    if out.returncode != 0 and not text:
        return "absent"
    if not text:
        return "absent"
    return "zombie" if text[0] == "Z" else "alive"


def owner_alive(pid, token):
    """三值：True 存活 · False 不存活 · None 不可判（保守视为存活，由调用方报告）。
    记录内令牌为 null（分配时不可得）即恒不可判（R2-B9 整改：不得与后来取得的令牌比较）。"""
    if token is None:
        return None
    state = process_state(pid)
    if state is None:
        return None
    if state in ("absent", "zombie"):
        return False
    current = process_start_token(pid)
    if current is None:
        return None
    return current == token


# ---------------------------------------------------------------- 原子发布原语（设计 §8.2）

def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic_replace(path, data):
    """临时文件 + 同目录 rename 原子重写（阶段状态记录用）。"""
    d = os.path.dirname(path)
    tmp = os.path.join(d, ".tmp-%s-%s" % (os.path.basename(path), _secrets.token_hex(4)))
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)
    fsync_dir(d)


def write_then_link(path, data, tmp_name):
    """先写全临时文件并 fsync，再 os.link 到最终路径（对已存在目标恒 EEXIST）。
    返回 True = 本方链接成功；False = 目标已存在（他方先到）。临时文件恒清理。"""
    d = os.path.dirname(path)
    tmp = os.path.join(d, tmp_name)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        fsync_dir(d)
        return True
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def write_new(path, data):
    """写入不得覆盖：目标已存在即 FileExistsError（O_EXCL）。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        raise
    fsync_dir(os.path.dirname(path))


def remove_tree(path):
    """整体删除目录树（只用于本进程或已死所有者的暂存物）。

    恒不跟随符号链接：符号链接只 unlink，绝不对其目标 chmod / 递归（目标可能在树外，如工具二进制）。
    """
    # 并发恢复者先删即目标已达（FileNotFoundError 不是错误，R5-B3 整改）；其余 OSError 原样抛出
    try:
        if not os.path.lexists(path):
            return
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)
            return
        entries = list(os.scandir(path))
    except FileNotFoundError:
        return
    for entry in entries:
        p = entry.path
        try:
            if entry.is_symlink():
                os.unlink(p)
            elif entry.is_dir(follow_symlinks=False):
                remove_tree(p)
            else:
                try:
                    os.chmod(p, 0o600, follow_symlinks=False)
                except (OSError, NotImplementedError):
                    pass
                os.unlink(p)
        except FileNotFoundError:
            continue
    try:
        os.chmod(path, 0o700, follow_symlinks=False)
    except (OSError, NotImplementedError):
        pass
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- 秘密扫描（设计 §7.5）

REDACTED_OUTPUT = b"[REDACTED-OUTPUT: secret closure not reached]"   # 输出层未达不动点时的替代文本（本身属生成字节，preflight 保证不含秘密）


def scrub_secrets(data, secret_values):
    """命中任一秘密值即替换为 [REDACTED-SECRET]；迭代到不动点（R20-B1 / R21-B1：替换本身可能与相邻字节拼出另一秘密）。
    趟数界可证明：preflight 已拒绝任何落在生成字节语言（连续哨兵、去重后缀、替代文本的子串）内的秘密，故每一趟的每次替换
    至少消耗一个非生成字节，趟数不超过 len(data) + 1；返回 (data, hit)。调用方对仍含秘密的结果须 fail closed（contains_secret 复核）。"""
    hit = False
    for _ in range(len(data) + 1):
        changed = False
        for value in secret_values:
            if value and value in data:
                hit = changed = True
                data = data.replace(value, REDACTED)
        if not changed:
            break
    return data, hit


def contains_secret(data, secret_values):
    return any(value and value in data for value in secret_values)


def scrub_json(obj, secret_values):
    """治理 JSON 工件（attempt.json / Receipt）的结构感知脱敏（r18 R18-B1 / r19 R19-B1 整改）：替换字符串值内的秘密，
    以及**动态成员名**（提供方应答 usage 键一类在 preflight 不可预知的键）内的秘密；容器结构不动，序列化后恒为合法 JSON。
    固定 schema 成员名与 preflight 可预知的动态成员名（环境变量名、适配器事实键、诊断文件名）由 preflight 以
    secret-structural-collision 拒绝，此处不会碰到；脱敏后同名成员以 `#n` 后缀区分。返回 (新对象, hit)。"""
    texts = []
    for v in secret_values:
        if isinstance(v, bytes):
            try:
                v = v.decode("utf-8")
            except UnicodeDecodeError:
                continue   # 非 UTF-8 字节不可能出现在 JSON 字符串值内
        if v:
            texts.append(v)
    red = REDACTED.decode("ascii")
    hit = [False]

    def walk(x):
        if isinstance(x, str):
            for t in texts:
                if t in x:
                    hit[0] = True
                    x = x.replace(t, red)
            return x
        if isinstance(x, dict):
            out = {}
            for k, v in x.items():
                nk = k
                if isinstance(k, str):
                    for t in texts:
                        if t in nk:
                            hit[0] = True
                            nk = nk.replace(t, red)   # 动态成员名内的秘密同样替换（R19-B1）
                if nk in out:   # 脱敏后与已有成员同名（含原本就叫哨兵名的成员）：以 #n 后缀区分，不覆盖
                    i = 2
                    while "%s#%d" % (nk, i) in out:
                        i += 1
                    nk = "%s#%d" % (nk, i)
                out[nk] = walk(v)
            return out
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x

    return walk(obj), hit[0]


AUTH_SECRET_MIN_LENGTH = 16
AUTH_KEY_MARKERS = ("token", "key", "secret")


def collect_auth_secret_values(payload):
    """认证载荷整体是秘密（设计 §7.2 其三）：长字符串叶值与键名含 token / key / secret 的字符串值全部入扫描集。"""
    found = []

    def walk(node, key_hint):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k if isinstance(k, str) else "")
        elif isinstance(node, list):
            for item in node:
                walk(item, key_hint)
        elif isinstance(node, str) and node:
            marked = any(m in key_hint.lower() for m in AUTH_KEY_MARKERS)
            if marked or len(node) >= AUTH_SECRET_MIN_LENGTH:
                found.append(node)

    walk(payload, "")
    return found


def inspect_auth_source(path):
    """认证文件三态 `present | missing | malformed`（设计 §7.2 其二、其四）。返回 (state, secret_values)。
    只报告状态，不报告内容、长度、前后缀或摘要。"""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return "missing", []
    try:
        with open(path, "rb") as f:
            payload = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return "malformed", []
    if not isinstance(payload, dict):
        return "malformed", []
    values = collect_auth_secret_values(payload)
    if not values:
        return "malformed", []
    return "present", values


def stage_auth_copy(source_path, target_path):
    """先验后写（设计 §7.2 其二）：读得字节按三态重验，低于 present 即抛 ChannelError；
    通过后写 0600 只读副本。返回从已验字节重取的扫描集。"""
    try:
        with open(os.path.expanduser(source_path), "rb") as f:
            data = f.read()
    except OSError:
        raise ChannelError("auth-source-missing-at-staging",
                           "authentication source unreadable at staging; no copy placed")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = None
    values = collect_auth_secret_values(payload) if isinstance(payload, dict) else []
    if not values:
        raise ChannelError("auth-source-malformed-at-staging",
                           "authentication source failed staging re-validation; no copy placed")
    fd = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(target_path, 0o600)
    return values


# ---------------------------------------------------------------- git

def git_run(repo_root, args, check=False, timeout=120):
    cmd = [resolve_git_binary(), *GIT_CONFIG_OVERRIDES, *args]
    proc = subprocess.run(cmd, cwd=repo_root, env=dict(ENV_WHITELIST), capture_output=True,
                          timeout=timeout)
    if check and proc.returncode != 0:
        raise ChannelError("git-failed", "git %s failed: %s" % (args[0], proc.stderr.decode("utf-8", "replace").strip()))
    return proc


def git_tracked(repo_root, relpath):
    proc = git_run(repo_root, ["ls-files", "--error-unmatch", "--", relpath])
    return proc.returncode == 0


def git_is_ancestor(repo_root, commit):
    proc = git_run(repo_root, ["merge-base", "--is-ancestor", commit, "HEAD"])
    return proc.returncode == 0


def git_show_blob(repo_root, commit, relpath):
    proc = git_run(repo_root, ["cat-file", "-p", "%s:%s" % (commit, relpath)])
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_head(repo_root):
    proc = git_run(repo_root, ["rev-parse", "HEAD"])
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("ascii", "replace").strip()


# ---------------------------------------------------------------- 启动形态（本通道设计 §5.3）

def startup_contract_problems():
    probs = []
    f = sys.flags
    if not f.dont_write_bytecode:
        probs.append("-B missing")
    if not f.ignore_environment:
        probs.append("-E missing")
    if not f.no_user_site:
        probs.append("-s missing")
    if not f.no_site:
        probs.append("-S missing")
    pp = getattr(sys, "pycache_prefix", None)
    if not pp:
        probs.append("-X pycache_prefix missing")
    elif not os.path.isdir(pp):
        probs.append("pycache_prefix directory absent")
    return probs


def resolve_git_binary():
    """按调用进程 PATH 解析 git 主二进制并取 realpath（身份比对对象）。"""
    p = shutil.which("git")
    if p is None:
        raise ChannelError("git-missing", "git executable not resolvable on PATH")
    return os.path.realpath(p)


# ---------------------------------------------------------------- 路径工具

def rel_posix(repo_root, path):
    real = os.path.realpath(path)
    root = os.path.realpath(repo_root)
    if real == root:
        return ""
    if not real.startswith(root + os.sep):
        return None
    return real[len(root) + 1:].replace(os.sep, "/")


def under_repo(repo_root, path):
    return rel_posix(repo_root, path) is not None


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def hex64(v):
    return isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)
