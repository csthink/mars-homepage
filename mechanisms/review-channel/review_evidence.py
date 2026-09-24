"""CL-55 shared evidence storage. No provider, Registry, or Git mutation.

Public readers verify both publications and byte identities. Initialization,
publication, recovery and restore are explicit writes. Git history remains owned
by repo-layout/history_read.py. No environment-variable test bypasses.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import uuid
import review_channel_contract as C

ERRORS = (
    "archive-unconfigured",
    "archive-unavailable",
    "archive-identity-mismatch",
    "archive-integrity-failed",
    "archive-conflict",
    "archive-incomplete",
)
ROLES = {
    "taskbook",
    "verdict",
    "bundle-manifest",
    "receipt",
    "request",
    "profile",
    "candidate",
    "reference",
    "baseline",
    "raw-output",
    "decisions",
    "other-original",
}
KINDS = {"round", "attempt", "response", "legacy-import", "inventory"}
HEX = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
ROUND = re.compile(
    r"(?:reviews/[a-z0-9][a-z0-9-]*/r[1-9][0-9]*|tasks/[a-z0-9][a-z0-9-]*/reviews/(?:task|impl)-r[1-9][0-9]*)\Z"
)
_LOCKS = {}
_LOCK_GUARD = threading.RLock()
_LOCK_SCOPES = threading.local()


@contextmanager
def lock_scope():
    """Track actual acquisitions, including lock-key reuse by another thread."""
    scopes = getattr(_LOCK_SCOPES, "stack", None)
    if scopes is None:
        scopes = _LOCK_SCOPES.stack = []
    acquired = []
    scopes.append(acquired)
    try:
        yield
    finally:
        scopes.pop()
        with _LOCK_GUARD:
            for key, owned in reversed(acquired):
                if _LOCKS.get(key) is owned:
                    del _LOCKS[key]
                    os.close(owned[0])


class EvidenceError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(code + ": " + message)


def require(ok, message, code="archive-integrity-failed"):
    if not ok:
        raise EvidenceError(code, message)


def canonical(obj):
    return (
        json.dumps(
            obj,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def strict(raw):
    def pairs(items):
        out = {}
        for k, v in items:
            require(k not in out, "duplicate JSON key: " + k)
            out[k] = v
        return out

    try:
        require(not raw.startswith(b"\xef\xbb\xbf"), "JSON BOM")
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda s: require(False, "nonfinite JSON"),
        )
    except (ValueError, UnicodeError) as exc:
        raise EvidenceError("archive-integrity-failed", "invalid JSON") from exc


def closed(obj, keys, label):
    require(isinstance(obj, dict) and set(obj) == set(keys), label + " fields")


def relative(p):
    return (
        isinstance(p, str)
        and bool(p)
        and not p.startswith("/")
        and "\\" not in p
        and not any(ord(c) < 32 or ord(c) == 127 for c in p)
        and all(x not in ("", ".", "..", ".git") for x in p.split("/"))
        and not any(0xD800 <= ord(c) <= 0xDFFF for c in p)
    )


def legacy_round_path(path):
    return relative(path) and (
        ROUND.fullmatch(path)
        or re.fullmatch(
            r"tasks/[a-z0-9][a-z0-9-]*/reviews/[a-z0-9][a-z0-9-]*/r[1-9][0-9]*", path
        )
    )


def legacy_round_key(path):
    require(legacy_round_path(path), "legacy round directory")
    if ROUND.fullmatch(path):
        return path
    parts = path.split("/")
    return "reviews/" + parts[-2] + "/" + parts[-1]


def reference(ref):
    require(isinstance(ref, dict), "EvidenceRef must be object")
    if ref.get("kind") == "archive":
        closed(ref, ("kind", "repository_id", "object_sha256"), "ArchiveRef")
        require(
            isinstance(ref["repository_id"], str)
            and ID.fullmatch(ref["repository_id"]),
            "repository id",
        )
        require(
            isinstance(ref["object_sha256"], str)
            and HEX.fullmatch(ref["object_sha256"]),
            "object digest",
        )
    elif ref.get("kind") == "legacy-git":
        closed(ref, ("kind", "commit", "round_path"), "legacy EvidenceRef")
        require(
            isinstance(ref["commit"], str) and COMMIT.fullmatch(ref["commit"]),
            "fixed commit required",
        )
        require(legacy_round_path(ref["round_path"]), "round path")
    else:
        require(False, "unknown evidence kind")
    return ref


def git(repo, *args):
    env = dict(
        os.environ,
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_OPTIONAL_LOCKS="0",
    )
    p = subprocess.run(
        ["git", "-c", "protocol.allow=never", "-C", str(repo), *args],
        env=env,
        capture_output=True,
    )
    require(p.returncode == 0, "Git query failed: " + args[0], "archive-unavailable")
    return p.stdout


def history():
    name = "_review_evidence_history"
    if name not in sys.modules:
        path = Path(__file__).resolve().parent.parent / "repo-layout/history_read.py"
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def fixed_file(repo, commit, path, view="HEAD"):
    require(
        relative(path) and isinstance(commit, str) and COMMIT.fullmatch(commit),
        "fixed source identity",
    )
    try:
        return history().read_history(
            str(repo), git(repo, "rev-parse", view).decode().strip(), path, commit
        )
    except history().HistoryReadError as exc:
        raise EvidenceError(
            "archive-unavailable", exc.code + ": " + exc.message
        ) from exc


def local_config(repo):
    p = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "config",
            "--local",
            "--get-regexp",
            "^reviewArchive\\.",
        ],
        capture_output=True,
    )
    require(
        p.returncode in (0, 1),
        "cannot read clone configuration",
        "archive-unconfigured",
    )
    out = {}
    for ln in p.stdout.decode().splitlines():
        k, _, v = ln.partition(" ")
        k = k.lower().split(".", 1)[1]
        require(k not in out, "duplicate archive configuration", "archive-conflict")
        out[k] = v
    wp = Path(git(repo, "rev-parse", "--git-path", "config.worktree").decode().strip())
    if not wp.is_absolute():
        wp = Path(repo) / wp
    if wp.exists():
        p = subprocess.run(
            ["git", "config", "--file", str(wp), "--get-regexp", "^reviewArchive\\."],
            capture_output=True,
        )
        require(
            p.returncode == 1, "worktree archive override forbidden", "archive-conflict"
        )
    return out


def switch_record(repo, view="HEAD"):
    """Only a committed formal decision can bind the clone to archive mode."""
    found = []
    for path in (
        git(
            repo,
            "ls-tree",
            "-r",
            "--name-only",
            view,
            "--",
            "records/governance/review-channel",
        )
        .decode()
        .splitlines()
    ):
        if "_Owner_Decisions" not in path or not path.endswith(".md"):
            continue
        data = git(repo, "show", view + ":" + path).decode("utf-8")
        blocks = re.findall(
            r"^```review-archive-switch\n(.*?)\n```[ \t]*$", data, re.M | re.S
        )
        if not blocks:
            continue
        require(len(blocks) == 1, "multiple switch blocks")
        d = strict(blocks[0].encode())
        closed(
            d,
            (
                "schema",
                "repository_id",
                "mode",
                "implementation_commit",
                "legacy_commit",
                "inventory_ref",
                "authorized_by",
            ),
            "switch",
        )
        require(
            d["schema"] == "review-archive-switch/v1" and d["mode"] == "archive-v1",
            "switch schema/mode",
        )
        require(
            isinstance(d["repository_id"], str) and ID.fullmatch(d["repository_id"]),
            "switch repository",
        )
        reference(d["inventory_ref"])
        require(
            d["inventory_ref"]["kind"] == "archive"
            and d["inventory_ref"]["repository_id"] == d["repository_id"],
            "switch inventory identity",
        )
        require(
            isinstance(d["authorized_by"], str) and d["authorized_by"].strip(),
            "switch authority absent",
        )
        for k in ("implementation_commit", "legacy_commit"):
            require(isinstance(d[k], str) and COMMIT.fullmatch(d[k]), "switch commit")
            git(repo, "merge-base", "--is-ancestor", d[k], view)
        found.append(d)
    require(len(found) <= 1, "multiple archive switch decisions", "archive-conflict")
    return found[0] if found else None


def storage_mode(repo):
    conf = local_config(repo)
    decision = switch_record(repo)
    mode = conf.get("mode", "legacy-git")
    require(
        mode in ("legacy-git", "archive-v1"),
        "unknown archive mode",
        "archive-unconfigured",
    )
    if decision:
        require(
            mode == "archive-v1",
            "committed archive switch requires clone configuration",
            "archive-unconfigured",
        )
    elif mode == "archive-v1":
        require(False, "archive mode has no committed switch", "archive-unconfigured")
    return mode


def safe_path(root, rel, exists=True):
    require(relative(rel), "unsafe payload path")
    root = Path(root)
    p = root
    for seg in rel.split("/"):
        p = p / seg
        require(not p.is_symlink(), "symlink: " + rel)
    require(p.resolve(strict=False).is_relative_to(root.resolve()), "path escape")
    if exists:
        require(p.exists(), "missing " + rel, "archive-incomplete")
    return p


def read_file(p):
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            require(stat.S_ISREG(os.fstat(f.fileno()).st_mode), "not regular file")
            return f.read()
    except OSError as exc:
        raise EvidenceError(
            "archive-unavailable", "file unavailable: " + str(p)
        ) from exc


def sync_dir(p):
    fd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_ancestors(path, root):
    path = Path(path)
    root = Path(root)
    require(path == root or path.is_relative_to(root), "fsync ancestor boundary")
    while True:
        sync_dir(path)
        if path == root:
            break
        path = path.parent


def write_new(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    sync_dir(p.parent)


def atomic_state(p, data):
    p = Path(p)
    temp = p.with_name("." + p.name + "." + uuid.uuid4().hex)
    write_new(temp, data)
    os.replace(temp, p)
    sync_dir(p.parent)


def tree_files(root):
    require(
        Path(root).is_dir() and not Path(root).is_symlink(),
        "object directory missing",
        "archive-incomplete",
    )
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            require(not (Path(directory) / name).is_symlink(), "directory symlink")
        for name in files:
            p = Path(directory) / name
            require(not p.is_symlink(), "file symlink")
            result[p.relative_to(root).as_posix()] = p
    return result


def _local_fs(path):
    if sys.platform == "darwin":
        import ctypes

        class Statfs(ctypes.Structure):
            _fields_ = [
                ("bsize", ctypes.c_uint32),
                ("iosize", ctypes.c_int32),
                ("counts", ctypes.c_uint64 * 5),
                ("fsid", ctypes.c_int32 * 2),
                ("owner", ctypes.c_uint32),
                ("type", ctypes.c_uint32),
                ("flags", ctypes.c_uint32),
                ("subtype", ctypes.c_uint32),
                ("fstypename", ctypes.c_char * 16),
                ("mntonname", ctypes.c_char * 1024),
                ("mntfromname", ctypes.c_char * 1024),
                ("flags_ext", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32 * 7),
            ]

        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        fn = libc.statfs64
        fn.argtypes = [ctypes.c_char_p, ctypes.POINTER(Statfs)]
        fn.restype = ctypes.c_int
        info = Statfs()
        require(
            fn(os.fsencode(path), ctypes.byref(info)) == 0,
            "statfs failed",
            "archive-unavailable",
        )
        fs = info.fstypename.decode()
        require(info.flags & 0x1000, "filesystem is not local", "archive-unavailable")
    else:
        p = subprocess.run(["stat", "-f", "-c", "%T", str(path)], capture_output=True)
        require(p.returncode == 0, "filesystem query failed", "archive-unavailable")
        fs = p.stdout.decode().strip().lower()
    require(
        fs
        in (
            "apfs",
            "hfs",
            "hfs+",
            "ext2/ext3",
            "ext4",
            "xfs",
            "btrfs",
            "zfs",
            "overlayfs",
        ),
        "filesystem is not a supported local POSIX filesystem: " + fs,
        "archive-unavailable",
    )


def validate_roots(repo, primary, backup, restore=False):
    roots = [Path(primary), Path(backup)]
    worktrees = [
        Path(s[9:]).resolve()
        for s in git(repo, "worktree", "list", "--porcelain").decode().splitlines()
        if s.startswith("worktree ")
    ]
    for p in roots:
        require(
            p.is_absolute()
            and (p.is_dir() or restore and p == roots[0] and not p.exists())
            and not p.is_symlink()
            and p.resolve() == p,
            "root must be canonical existing directory",
            "archive-unconfigured",
        )
        require(
            not any(p == w or p.is_relative_to(w) for w in worktrees),
            "archive inside registered worktree",
            "archive-unconfigured",
        )
        require(
            not any(
                p == t or p.is_relative_to(t)
                for t in (
                    Path("/tmp"),
                    Path("/private/tmp"),
                    Path("/var/tmp"),
                    Path("/private/var/folders"),
                    Path("/dev/shm"),
                )
            ),
            "temporary archive root forbidden",
            "archive-unconfigured",
        )
        if p.exists():
            _local_fs(p)
    a, b = roots
    require(
        not (a == b or a.is_relative_to(b) or b.is_relative_to(a)),
        "overlapping roots",
        "archive-conflict",
    )
    if a.exists():
        require(
            (a.stat().st_dev, a.stat().st_ino) != (b.stat().st_dev, b.stat().st_ino),
            "root alias",
            "archive-conflict",
        )
    return roots


class Archive:
    def __init__(
        self, repo, repository_id=None, primary=None, backup=None, restore=False
    ):
        self.repo = Path(repo).resolve()
        if repository_id is None:
            c = local_config(repo)
            require(
                set(c) == {"repositoryid", "primaryroot", "backuproot", "mode"},
                "archive configuration incomplete",
                "archive-unconfigured",
            )
            repository_id, primary, backup = (
                c["repositoryid"],
                c["primaryroot"],
                c["backuproot"],
            )
        require(
            isinstance(repository_id, str) and ID.fullmatch(repository_id),
            "repository id",
            "archive-identity-mismatch",
        )
        self.repository_id = repository_id
        self.primary, self.backup = validate_roots(
            repo, primary, backup, restore=restore
        )
        self.identity = {
            "schema": "review-archive-repository/v1",
            "repository_id": repository_id,
        }
        if restore:
            require(
                strict(read_file(safe_path(self.backup, "repository.json")))
                == self.identity,
                "backup repository mismatch",
                "archive-identity-mismatch",
            )
        else:
            self.check_roots()

    def check_roots(self, writable=False):
        validate_roots(self.repo, self.primary, self.backup)
        for root in (self.primary, self.backup):
            data = strict(read_file(safe_path(root, "repository.json")))
            closed(data, ("schema", "repository_id"), "repository")
            require(
                data == self.identity,
                "root repository mismatch",
                "archive-identity-mismatch",
            )
            if writable:
                require(
                    os.access(root, os.R_OK | os.W_OK | os.X_OK)
                    and root.stat().st_mode & 0o222
                    and shutil.disk_usage(root).free > 0,
                    "root not writable",
                    "archive-unavailable",
                )
        a = (self.primary / "repository.json").stat()
        b = (self.backup / "repository.json").stat()
        require(
            (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino), "identity backup hardlink"
        )

    def ref(self, digest):
        return {
            "kind": "archive",
            "repository_id": self.repository_id,
            "object_sha256": digest,
        }

    def lock(self, round_key):
        """Acquire inside lock_scope; nested calls reuse only this owner's fd."""
        require(isinstance(round_key, str) and ROUND.fullmatch(round_key), "lock round key")
        scopes = getattr(_LOCK_SCOPES, "stack", [])
        require(bool(scopes), "archive lock requires explicit operation scope", "archive-conflict")
        p = safe_path(self.primary, "locks/" + sha(round_key.encode()) + ".lock", False)
        key = str(p)
        with _LOCK_GUARD:
            if key in _LOCKS:
                fd, pid, thread = _LOCKS[key]
                require(pid == os.getpid() and thread == threading.get_ident(),
                        "lock owned by another execution", "archive-conflict")
                return fd
            p.parent.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(p, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                raise EvidenceError("archive-conflict", "round lock held") from exc
            owned = (fd, os.getpid(), threading.get_ident())
            _LOCKS[key] = owned
            scopes[-1].append((key, owned))
            return fd

    def object(self, root, digest):
        require(isinstance(digest, str) and HEX.fullmatch(digest), "object digest")
        return self._object_at(safe_path(root, "objects/" + digest), digest)

    def _object_at(self, obj, digest):
        raw = read_file(safe_path(obj, "archive.json"))
        require(sha(raw) == digest, "archive identity mismatch")
        d = strict(raw)
        require(canonical(d) == raw, "archive must use canonical bytes")
        closed(
            d,
            (
                "schema",
                "repository_id",
                "kind",
                "round_key",
                "attempt_id",
                "parents",
                "files",
                "legacy_source",
                "limitations",
            ),
            "archive",
        )
        require(
            d["schema"] == "review-archive/v1"
            and d["repository_id"] == self.repository_id,
            "archive repository/schema",
            "archive-identity-mismatch",
        )
        require(isinstance(d["kind"], str) and d["kind"] in KINDS, "object kind")
        require(
            (d["round_key"] is None and d["attempt_id"] is None)
            if d["kind"] == "inventory"
            else isinstance(d["round_key"], str) and ROUND.fullmatch(d["round_key"]),
            "object routing",
        )
        require(
            d["attempt_id"] is None
            or (relative(d["attempt_id"]) and "/" not in d["attempt_id"]),
            "attempt id",
        )
        require(isinstance(d["parents"], list), "parents")
        [reference(r) for r in d["parents"]]
        require(
            all(
                r.get("repository_id", self.repository_id) == self.repository_id
                for r in d["parents"]
            ),
            "parent repository",
            "archive-identity-mismatch",
        )
        require(
            isinstance(d["limitations"], list)
            and all(isinstance(x, str) for x in d["limitations"]),
            "limitations",
        )
        if d["legacy_source"] is not None:
            closed(d["legacy_source"], ("commit", "round_path"), "legacy source")
            reference(dict(kind="legacy-git", **d["legacy_source"]))
        require(
            d["kind"] == "legacy-import" or d["legacy_source"] is None,
            "unexpected legacy source",
        )
        require(isinstance(d["files"], list) and d["files"], "empty file inventory")
        names = []
        payload = {}
        for f in d["files"]:
            closed(f, ("path", "bytes", "sha256", "role"), "file")
            name = f["path"]
            require(relative(name), "payload path")
            require(
                type(f["bytes"]) is int
                and f["bytes"] >= 0
                and isinstance(f["sha256"], str)
                and HEX.fullmatch(f["sha256"])
                and isinstance(f["role"], str)
                and f["role"] in ROLES,
                "file identity/role",
            )
            p = safe_path(obj, "payload/" + name)
            data = read_file(p)
            require(
                len(data) == f["bytes"] and sha(data) == f["sha256"],
                "payload digest: " + name,
            )
            payload[name] = data
            names.append(name)
        require(
            names == sorted(set(names), key=lambda n: n.encode("utf-8")),
            "file ordering/duplicate",
        )
        require(
            set(tree_files(obj)) == {"archive.json"} | {"payload/" + n for n in names},
            "unlisted/missing object files",
        )
        if d["kind"] == "round":
            self.check_round(d, payload)
        return d, payload

    def publication_path(self, d, digest):
        if d["kind"] == "inventory":
            return "publications/inventory/" + digest + ".json"
        return (
            "publications/" + d["round_key"] + "/" + d["kind"] + "/" + digest + ".json"
        )

    def read(self, ref):
        reference(ref)
        require(
            ref["kind"] == "archive" and ref["repository_id"] == self.repository_id,
            "foreign ArchiveRef",
            "archive-identity-mismatch",
        )
        self.check_roots()
        digest = ref["object_sha256"]
        a, files = self.object(self.primary, digest)
        b, _ = self.object(self.backup, digest)
        require(a == b, "backup object differs")
        pub = self.publication(a, digest)
        name = self.publication_path(a, digest)
        for root in (self.primary, self.backup):
            require(
                read_file(safe_path(root, name)) == canonical(pub),
                "publication mismatch",
            )
        for name in ["archive.json"] + ["payload/" + f["path"] for f in a["files"]]:
            s = (self.primary / "objects" / digest / name).stat()
            t = (self.backup / "objects" / digest / name).stat()
            require(
                (s.st_dev, s.st_ino) != (t.st_dev, t.st_ino), "backup hardlink: " + name
            )
        return a, files

    def publication(self, d, digest):
        return {
            "schema": "review-archive-publication/v1",
            "repository_id": self.repository_id,
            "round_key": d["round_key"],
            "attempt_id": d["attempt_id"],
            "object_sha256": digest,
            "kind": d["kind"],
        }

    def publications(self, round_key=None):
        self.check_roots()
        prefix = "publications"
        if round_key is not None:
            require(
                isinstance(round_key, str) and ROUND.fullmatch(round_key),
                "publication round key",
            )
            prefix += "/" + round_key
        a = safe_path(self.primary, prefix, False)
        b = safe_path(self.backup, prefix, False)
        aa = tree_files(a) if a.exists() else {}
        bb = tree_files(b) if b.exists() else {}
        require(set(aa) == set(bb), "one-sided publications", "archive-incomplete")
        out = []
        for name in sorted(aa):
            require(name.endswith(".json"), "unknown publication file")
            raw = read_file(aa[name])
            require(raw == read_file(bb[name]), "publication copies differ")
            pub = strict(raw)
            closed(
                pub,
                (
                    "schema",
                    "repository_id",
                    "round_key",
                    "attempt_id",
                    "object_sha256",
                    "kind",
                ),
                "publication",
            )
            ref = self.ref(pub["object_sha256"])
            d, files = self.read(ref)
            require(
                prefix + "/" + name == self.publication_path(d, pub["object_sha256"]),
                "misplaced publication",
            )
            out.append((ref, d, files))
        rounds = {}
        for ref, d, files in out:
            if d["kind"] in ("round", "legacy-import"):
                key = d["round_key"]
                require(
                    key not in rounds or rounds[key] == ref,
                    "multiple identities for round",
                    "archive-conflict",
                )
                rounds[key] = ref
        return out

    def check_round(self, d, files):
        roles = {role: [f for f in d["files"] if f["role"] == role] for role in ROLES}
        for role in (
            "taskbook",
            "verdict",
            "bundle-manifest",
            "receipt",
            "request",
            "profile",
        ):
            require(len(roles[role]) == 1, "round requires exactly one " + role)
        get = lambda role: files[roles[role][0]["path"]]
        receipt = strict(get("receipt"))
        manifest = strict(get("bundle-manifest"))
        import review_channel_receipt as RC
        require(not RC.receipt_shape_problems(receipt), "round Receipt shape invalid")
        require(
            receipt.get("receipt_schema") in ("review-channel-receipt/v3", C.RECEIPT_SCHEMA),
            "round archive Receipt required",
        )
        require(
            receipt.get("evidence_storage")
            == {
                "kind": "archive",
                "repository_id": self.repository_id,
                "round_key": d["round_key"],
            },
            "Receipt storage",
        )
        require(
            receipt.get("verdict_published") is True
            and receipt.get("classification") == "completed_with_valid_verdict"
            and receipt.get("verdict_validation") == "VALID",
            "round publication facts",
        )
        require(
            receipt.get("verdict_sha256") == sha(get("verdict")), "verdict identity"
        )
        require(
            receipt.get("request_sha256") == sha(get("request")), "request identity"
        )
        # Bundle identity uses the existing manifest canonical function (no trailing LF).
        inp = manifest.get("inputs")
        require(isinstance(inp, list), "bundle inputs")
        expected = sha(
            json.dumps(
                inp, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        )
        require(
            expected
            == manifest.get("manifest_sha256")
            == receipt.get("input_manifest_sha256"),
            "bundle identity",
        )
        require(
            receipt.get("verdict_path", "").startswith(d["round_key"] + "/")
            and receipt.get("input_manifest_path", "").startswith(d["round_key"] + "/"),
            "Receipt logical paths",
        )
        for i in inp:
            matches = [
                f
                for f in d["files"]
                if Path(f["path"]).name == i["bundle_name"]
                and f["bytes"] == i["bytes"]
                and f["sha256"] == i["sha256"]
            ]
            require(matches, "missing sealed input " + i["bundle_name"])
        require(not d["limitations"], "new round cannot relax completeness")
        import review_channel_receipt as RC
        import review_channel_verdict as V

        require(
            not RC.profile_shape_problems(receipt.get("effective_profile")),
            "round Profile invalid",
        )
        require(
            strict(get("profile"))
            == {"effective_profile": receipt["effective_profile"]},
            "Profile original differs",
        )
        try:
            V.parse_published(get("verdict"))
        except (ValueError, UnicodeError) as exc:
            raise EvidenceError(
                "archive-integrity-failed", "invalid verdict original"
            ) from exc
        require(
            receipt.get("call_path_proof") == "PROVEN"
            and receipt.get("profile_binding") == "SUFFICIENT",
            "round call facts invalid",
        )
        raw_final = receipt.get("raw_final_message_sha256")
        require(
            raw_final and any(f["sha256"] == raw_final for f in roles["raw-output"]),
            "raw final original absent",
        )
        request = strict(get("request"))
        require(
            request.get("request_schema") == "review-channel-request/v5",
            "round Request schema",
        )
        state = strict(files.get("attempt.json", b"{}"))
        require(
            state.get("phase") == "validated"
            and state.get("verdict_validation") == "VALID",
            "validated state absent",
        )
        require(
            state.get("attempt_id") == d["attempt_id"] == receipt.get("attempt_id"),
            "attempt identity differs",
        )
        # Persisted baseline inventory is written before the provider can run.
        baseline_inventory = strict(files.get("baseline-identities.json", b"[]"))
        require(isinstance(baseline_inventory, list), "baseline inventory")
        for row in baseline_inventory:
            require(
                any(
                    f["role"] == "baseline"
                    and f["sha256"] == row["sha256"]
                    and f["bytes"] == row["bytes"]
                    for f in d["files"]
                ),
                "frozen baseline original missing",
            )

    @lock_scope()
    def publish(
        self,
        kind,
        round_key,
        attempt_id,
        files,
        parents=(),
        legacy_source=None,
        limitations=(),
    ):
        """files maps payload path to (immutable bytes, role). Call under round lock."""
        self.check_roots(writable=True)
        if round_key is not None:
            self.lock(round_key)
        rows = [
            {"path": p, "bytes": len(data), "sha256": sha(data), "role": role}
            for p, (data, role) in sorted(files.items(), key=lambda t: t[0].encode())
        ]
        d = {
            "schema": "review-archive/v1",
            "repository_id": self.repository_id,
            "kind": kind,
            "round_key": round_key,
            "attempt_id": attempt_id,
            "parents": list(parents),
            "files": rows,
            "legacy_source": legacy_source,
            "limitations": list(limitations),
        }
        raw = canonical(d)
        digest = sha(raw)
        if kind in ("round", "legacy-import"):
            # The held lock owns this round. Unrelated rounds are checked by
            # full inventory/read verification, not re-read for each import.
            for ref, other, _ in self.publications(round_key):
                require(
                    other["kind"] not in ("round", "legacy-import")
                    or other["round_key"] != round_key
                    or ref["object_sha256"] == digest,
                    "round already published",
                    "archive-conflict",
                )
        # Save exact publication intent before either side can become visible.
        pending = safe_path(self.primary, "pending/" + (attempt_id or digest), False)
        pending.mkdir(mode=0o700, parents=True, exist_ok=True)
        import review_channel_base as base

        intent = {
            "object_sha256": digest,
            "round_key": round_key,
            "attempt_id": attempt_id,
            "kind": kind,
            "owner": {
                "pid": os.getpid(),
                "pid_start": base.process_start_token(os.getpid()),
            },
        }
        intent_path = pending / "publication-intent.json"
        if intent_path.exists():
            old = strict(read_file(intent_path))
            require(
                all(old[k] == v for k, v in intent.items() if k != "owner"),
                "pending intent conflict",
                "archive-conflict",
            )
            intent = old
        else:
            write_new(intent_path, canonical(intent))
        self._install(self.primary, digest, raw, files)
        self.complete(digest)
        atomic_state(pending / "publication-completed.json", canonical(intent))
        return self.ref(digest)

    def _install(self, root, digest, raw, files):
        dest = safe_path(root, "objects/" + digest, False)
        if dest.exists():
            self.object(root, digest)
            return
        stage = safe_path(root, "pending/object-" + uuid.uuid4().hex, False)
        stage.mkdir(parents=True, mode=0o700)
        write_new(stage / "archive.json", raw)
        for p, (data, _role) in files.items():
            write_new(safe_path(stage, "payload/" + p, False), data)
        dest.parent.mkdir(mode=0o700, exist_ok=True)
        for directory, _, _ in os.walk(stage, topdown=False):
            sync_dir(directory)
        self._object_at(stage, digest)
        os.rename(stage, dest)
        sync_ancestors(dest.parent, root)
        self.object(root, digest)

    @lock_scope()
    def complete(self, digest):
        """Explicit same-identity completion, never calls a provider."""
        self.check_roots(writable=True)
        d, files = self.object(self.primary, digest)
        if d["round_key"] is not None:
            self.lock(d["round_key"])
        # Reject even an existing single-sided different publication of this round.
        for root in (self.primary, self.backup):
            if d["kind"] in ("round", "legacy-import"):
                parent = safe_path(root, "publications/" + d["round_key"], False)
                if parent.exists():
                    for name, p in tree_files(parent).items():
                        old = strict(read_file(p))
                        require(
                            old.get("kind") not in ("round", "legacy-import")
                            or old.get("object_sha256") == digest,
                            "conflicting round publication",
                            "archive-conflict",
                        )
        self._install(
            self.backup,
            digest,
            canonical(d),
            {
                p: (data, next(f["role"] for f in d["files"] if f["path"] == p))
                for p, data in files.items()
            },
        )
        name = self.publication_path(d, digest)
        pub = canonical(self.publication(d, digest))
        for root in (self.backup, self.primary):
            p = safe_path(root, name, False)
            if p.exists():
                require(
                    read_file(p) == pub,
                    "publication exists with different bytes",
                    "archive-conflict",
                )
            else:
                p.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                temp = p.with_name("." + p.name + "." + uuid.uuid4().hex)
                write_new(temp, pub)
                os.rename(temp, p)
                sync_ancestors(p.parent, root)
            # Explicit completion may encounter a pre-rename publication file
            # from this exact identity. Remove only verified metadata duplicates.
            for temp in p.parent.glob("." + p.name + ".*"):
                require(
                    not temp.is_symlink() and read_file(temp) == pub,
                    "unfinished publication metadata differs",
                    "archive-conflict",
                )
                temp.unlink()
            sync_ancestors(p.parent, root)
        self.read(self.ref(digest))
        return self.ref(digest)

    def restore(self, ref, destination):
        reference(ref)
        require(
            ref["kind"] == "archive" and ref["repository_id"] == self.repository_id,
            "restore identity",
        )
        dest = Path(destination)
        require(
            dest.is_absolute()
            and dest.exists()
            and dest.is_dir()
            and dest.resolve() == dest
            and not any(dest.iterdir()),
            "restore requires canonical empty destination",
        )
        require(
            not any(
                dest == r or dest.is_relative_to(r) or r.is_relative_to(dest)
                for r in (self.primary, self.backup)
            ),
            "restore destination overlaps archive",
        )
        # Backup-only verification is deliberate: primary may be lost.
        d, files = self.object(self.backup, ref["object_sha256"])
        p = self.publication_path(d, ref["object_sha256"])
        require(
            read_file(safe_path(self.backup, p))
            == canonical(self.publication(d, ref["object_sha256"])),
            "backup publication",
        )
        for name, data in files.items():
            write_new(safe_path(dest, "payload/" + name, False), data)
        write_new(dest / "archive.json", canonical(d))
        for f in d["files"]:
            require(
                sha(read_file(safe_path(dest, "payload/" + f["path"]))) == f["sha256"],
                "restored bytes differ",
            )
        return {
            "files": len(files),
            "destination": str(dest),
            "same_device": self.primary.stat().st_dev == self.backup.stat().st_dev
            if self.primary.exists()
            else None,
        }


def initialize(repo, repository_id, primary, backup):
    """Explicit install helper; never called by a reader or preflight."""
    require(
        isinstance(repository_id, str) and ID.fullmatch(repository_id), "repository id"
    )
    roots = validate_roots(repo, primary, backup)
    identity = canonical(
        {"schema": "review-archive-repository/v1", "repository_id": repository_id}
    )
    for root in roots:
        path = root / "repository.json"
        if path.exists():
            require(
                read_file(path) == identity,
                "existing root identity differs",
                "archive-identity-mismatch",
            )
        else:
            write_new(path, identity)
    return Archive(repo, repository_id, primary, backup)


def legacy_files(repo, ref):
    reference(ref)
    require(ref["kind"] == "legacy-git", "legacy reference required")
    h = history()
    view = git(repo, "rev-parse", "HEAD").decode().strip()
    try:
        g = h.GitHistory(str(repo), view)
        g.ancestor(ref["commit"])
        paths = [
            p for p in g.tree(ref["commit"]) if p.startswith(ref["round_path"] + "/")
        ]
        require(paths, "legacy source has no originals", "archive-incomplete")
        result = {p: g.blob(ref["commit"], p)["content"] for p in paths}
    except h.HistoryReadError as exc:
        raise EvidenceError(
            "archive-unavailable", exc.code + ": " + exc.message
        ) from exc
    present = Path(repo) / ref["round_path"]
    if present.exists():
        for rel, p in tree_files(present).items():
            name = ref["round_path"] + "/" + rel
            require(
                name in result and read_file(p) == result[name],
                "legacy working bytes differ: " + name,
                "archive-conflict",
            )
    return result


def evidence(repo, ref, archive=None):
    reference(ref)
    if ref["kind"] == "archive":
        return (archive or Archive(repo)).read(ref)
    files = legacy_files(repo, ref)
    return {
        "kind": "legacy-import",
        "round_key": legacy_round_key(ref["round_path"]),
        "legacy_source": {"commit": ref["commit"], "round_path": ref["round_path"]},
        "parents": [],
    }, files


def role_file(d, files, role):
    rows = d.get("files")
    if rows:
        found = [f["path"] for f in rows if f["role"] == role]
    else:
        predicates = {
            "receipt": lambda n: n.startswith("receipt-r") and n.endswith(".json"),
            "bundle-manifest": lambda n: n == "bundle_manifest.json",
            "verdict": lambda n: "_Review_R" in n and "_Task_" not in n,
            "taskbook": lambda n: "_Review_Task_" in n,
        }
        require(role in predicates, "legacy role unsupported")
        found = [p for p in files if predicates[role](Path(p).name)]
    require(
        len(found) == 1,
        "original " + role + " missing or ambiguous",
        "archive-incomplete",
    )
    return found[0], files[found[0]]


def validate_legacy_inventory(repo, archive, decision):
    """The committed cutoff determines coverage; an empty local index is not proof."""
    require(
        decision["repository_id"] == archive.repository_id,
        "switch repository mismatch",
        "archive-identity-mismatch",
    )
    d, files = archive.read(decision["inventory_ref"])
    require(d["kind"] == "inventory", "switch must reference inventory")
    require(len(files) == 1, "inventory requires one original inventory document")
    inventory = strict(next(iter(files.values())))
    closed(
        inventory, ("schema", "files", "missing", "unclassified"), "inventory document"
    )
    require(
        inventory["schema"] == "review-archive-inventory/v1"
        and all(
            isinstance(inventory[k], list) for k in ("files", "missing", "unclassified")
        ),
        "inventory shape",
    )
    h = history()
    view = git(repo, "rev-parse", "HEAD").decode().strip()
    try:
        g = h.GitHistory(str(repo), view)
        g.ancestor(decision["legacy_commit"])
        tree = g.tree(decision["legacy_commit"])
        selected = {
            p
            for p in tree
            if p.startswith("reviews/")
            or re.match(r"^tasks/[^/]+/reviews/", p)
            or (
                p.startswith("records/diagnostics/review-channel/")
                and re.search(r"receipt[^/]*\.json$", p, re.I)
            )
        }
        covered = set()
        refs = []
        verified = {}
        for row in inventory["files"]:
            closed(
                row,
                (
                    "path",
                    "source_commit",
                    "blob_oid",
                    "bytes",
                    "sha256",
                    "ref",
                    "payload_path",
                ),
                "inventory row",
            )
            require(
                relative(row["path"]) and relative(row["payload_path"]),
                "inventory paths",
            )
            reference(row["ref"])
            require(
                row["ref"]["kind"] == "archive", "inventory original must be archived"
            )
            identity = canonical(row["ref"])
            if identity not in verified:
                verified[identity] = archive.read(row["ref"])
            od, originals = verified[identity]
            require(od["kind"] in ("legacy-import", "attempt"), "inventory import kind")
            data = originals.get(row["payload_path"])
            require(
                type(row["bytes"]) is int
                and data is not None
                and len(data) == row["bytes"]
                and sha(data) == row["sha256"],
                "inventory original identity",
            )
            if row["source_commit"] is not None:
                item = g.blob(row["source_commit"], row["path"])
                g.ancestor(row["source_commit"])
                require(
                    item["blob_oid"] == row["blob_oid"] and item["content"] == data,
                    "inventory fixed source differs",
                )
                if (
                    row["source_commit"] == decision["legacy_commit"]
                    and row["path"] in selected
                ):
                    require(
                        row["path"] not in covered, "duplicate cutoff inventory row"
                    )
                    covered.add(row["path"])
            else:
                require(
                    row["blob_oid"] is None, "local original cannot have blob identity"
                )
            present = Path(repo) / row["path"]
            if present.exists():
                require(
                    read_file(safe_path(repo, row["path"])) == data,
                    "present inventoried original differs",
                    "archive-conflict",
                )
            if od["kind"] == "legacy-import" and row["ref"] not in refs:
                refs.append(row["ref"])
        require(
            covered == selected,
            "inventory does not cover fixed legacy tree",
            "archive-incomplete",
        )
        require(
            {canonical(r) for r in d["parents"]} == {canonical(r) for r in refs},
            "inventory parent set differs",
        )
    except h.HistoryReadError as exc:
        raise EvidenceError(
            "archive-unavailable", exc.code + ": " + exc.message
        ) from exc
    return inventory


def configured(repo, writable=False):
    if storage_mode(repo) == "legacy-git":
        return None
    archive = Archive(repo)
    archive.check_roots(writable)
    validate_legacy_inventory(repo, archive, switch_record(repo))
    return archive


def published_rounds(repo, archive):
    """Fixed legacy cutoff plus imported/new rounds, keyed by original route."""
    decision = switch_record(repo)
    require(decision is not None, "missing switch", "archive-unconfigured")
    result = {}
    h = history()
    view = git(repo, "rev-parse", "HEAD").decode().strip()
    try:
        g = h.GitHistory(str(repo), view)
        g.ancestor(decision["legacy_commit"])
        keys = {
            p.rsplit("/", 1)[0]
            for p in g.tree(decision["legacy_commit"])
            if legacy_round_path(p.rsplit("/", 1)[0])
        }
        for key in sorted(keys):
            ref = {
                "kind": "legacy-git",
                "commit": decision["legacy_commit"],
                "round_path": key,
            }
            d, files = evidence(repo, ref, archive)
            logical = d["round_key"]
            require(
                logical not in result, "legacy round alias conflict", "archive-conflict"
            )
            result[logical] = (ref, d, files)
        # Historical paths already retired under CL-52 retain their original
        # fixed source. The common history reader owns locator interpretation.
        historical = {}
        for prefix in ("reviews/", "tasks/"):
            for row in h.working_history(str(repo), prefix):
                directory = row["path"].rsplit("/", 1)[0]
                if legacy_round_path(directory):
                    historical.setdefault(directory, []).append(row)
        for directory, rows in historical.items():
            logical = legacy_round_key(directory)
            if logical in result:
                existing = result[logical][2]
                require(
                    all(
                        row["path"] in existing
                        and sha(existing[row["path"]]) == row["sha256"]
                        for row in rows
                    ),
                    "historical version conflicts with cutoff",
                    "archive-conflict",
                )
                continue
            sources = {row["source_commit"] for row in rows}
            matches = []
            for source in sorted(sources):
                ref = {"kind": "legacy-git", "commit": source, "round_path": directory}
                original = legacy_files(repo, ref)
                if all(
                    row["path"] in original
                    and sha(original[row["path"]]) == row["sha256"]
                    for row in rows
                ):
                    matches.append((ref, evidence(repo, ref, archive)))
            require(
                matches and all(matches[0][1][1] == m[1][1] for m in matches),
                "historical round source missing or ambiguous",
                "archive-incomplete",
            )
            ref, (d, files) = matches[0]
            result[logical] = (ref, d, files)
        for item in archive.publications():
            ref, d, files = item
            if d["kind"] not in ("round", "legacy-import"):
                continue
            key = d["round_key"]
            if key in result:
                oldref, old, oldfiles = result[key]
                require(
                    d["kind"] == "legacy-import"
                    and d["legacy_source"]
                    == {
                        "commit": oldref.get("commit"),
                        "round_path": oldref.get("round_path"),
                    },
                    "same round has different origin",
                    "archive-conflict",
                )
                require(
                    all(p in files and files[p] == v for p, v in oldfiles.items()),
                    "legacy import differs from fixed originals",
                    "archive-conflict",
                )
            result[key] = item
    except h.HistoryReadError as exc:
        raise EvidenceError(
            "archive-unavailable", exc.code + ": " + exc.message
        ) from exc
    for parent in [Path(repo) / "reviews", *(Path(repo) / "tasks").glob("*/reviews")]:
        if not parent.exists():
            continue
        for rel, p in tree_files(parent).items():
            path = p.relative_to(repo).as_posix()
            directory = path.rsplit("/", 1)[0]
            if not legacy_round_path(directory):
                require(
                    False,
                    "unclassified current review original: " + path,
                    "archive-incomplete",
                )
            key = legacy_round_key(directory)
            require(
                key in result,
                "current round absent from continuity inventory: " + key,
                "archive-incomplete",
            )
            originals = result[key][2]
            require(
                path in originals and originals[path] == read_file(p),
                "current legacy original differs from fixed view",
                "archive-conflict",
            )
    return result


def scan_rounds(repo, parent, axis, stage):
    archive = configured(repo)
    require(archive is not None, "archive mode required", "archive-unconfigured")
    out = {}
    for key, (ref, d, files) in published_rounds(repo, archive).items():
        if key.rsplit("/", 1)[0] != parent:
            continue
        name = key.rsplit("/", 1)[1]
        if axis == "task" and not name.startswith(stage + "-"):
            continue
        k = int(name.rsplit("r", 1)[1])
        published = False
        try:
            _, raw = role_file(d, files, "receipt")
            r = strict(raw)
            published = (
                r.get("verdict_published") is True
                and r.get("classification") == "completed_with_valid_verdict"
            )
        except EvidenceError as exc:
            if exc.code != "archive-incomplete":
                raise
        out[k] = {"dir": key, "receipt": published, "evidence": ref}
    return out


def load_previous(repo, request, routing, latest):
    import review_channel_base as base
    import review_channel_receipt as RC
    import review_channel_verdict as V

    archive = configured(repo)
    ref = request["previous_round"]["evidence"]
    d, files = evidence(repo, ref, archive)
    key = d["round_key"]
    expected = (
        routing["tracked_parent"]
        + "/"
        + ((routing["stage"] + "-") if routing["axis"] == "task" else "")
        + "r"
        + str(latest)
    )
    require(
        key == expected and d["kind"] in ("round", "legacy-import"),
        "previous reference is not latest same-axis round",
        "archive-conflict",
    )
    view = published_rounds(repo, archive)
    require(key in view, "previous round not in publication view", "archive-incomplete")
    current_ref, current_d, current_files = view[key]
    if ref != current_ref:
        require(
            ref["kind"] == "legacy-git"
            and current_d["legacy_source"]
            == {"commit": ref["commit"], "round_path": key}
            and all(current_files.get(p) == v for p, v in files.items()),
            "previous round identity differs",
            "archive-conflict",
        )
    rp, rb = role_file(d, files, "receipt")
    vp, vb = role_file(d, files, "verdict")
    mp, mb = role_file(d, files, "bundle-manifest")
    tp, tb = role_file(d, files, "taskbook")
    r = strict(rb)
    import review_channel_receipt as RC
    require(not RC.receipt_shape_problems(r), "previous Receipt shape invalid")
    m = strict(mb)
    require(
        r.get("receipt_schema")
        in C.RECEIPT_READ_SCHEMAS,
        "previous Receipt schema unsupported",
        "archive-incomplete",
    )
    require(
        (
            r.get("classification"),
            r.get("attempt_outcome"),
            r.get("mode"),
            r.get("receipt_phase"),
            r.get("call_path_proof"),
            r.get("profile_binding"),
            r.get("verdict_validation"),
            r.get("verdict_published"),
        )
        == (
            "completed_with_valid_verdict",
            "governed_verdict",
            "review",
            "completed",
            "PROVEN",
            "SUFFICIENT",
            "VALID",
            True,
        ),
        "previous Receipt is not valid published evidence",
    )
    require(
        all(
            r.get(k) == v
            for k, v in (
                ("subject", routing["subject"]),
                ("stage", routing["stage"]),
                ("round", "r" + str(latest)),
                ("task_record", routing["task_record"]),
            )
        ),
        "previous routing mismatch",
    )
    require(
        not RC.profile_shape_problems(r.get("effective_profile")),
        "previous Profile invalid",
    )
    require(r.get("verdict_sha256") == sha(vb), "previous verdict hash")
    require(
        sha(base.canonical_json(m.get("inputs")))
        == m.get("manifest_sha256")
        == r.get("input_manifest_sha256"),
        "previous manifest hash",
    )
    require(
        r.get("verdict_path") == key + "/" + Path(vp).name
        and r.get("input_manifest_path") == key + "/" + Path(mp).name,
        "previous logical paths",
    )
    task = [i for i in m["inputs"] if i.get("role") == "review-task"]
    require(
        len(task) == 1 and task[0]["sha256"] == sha(tb) and task[0]["bytes"] == len(tb),
        "previous taskbook identity",
    )
    V.parse_published(vb)
    sealed = {}
    for i in m["inputs"]:
        matches = [
            data
            for p, data in files.items()
            if sha(data) == i["sha256"] and len(data) == i["bytes"]
        ]
        if matches:
            sealed[i["sha256"]] = matches[0]
        elif d["kind"] == "round":
            require(False, "new round sealed input missing", "archive-incomplete")
    return {
        "k": latest,
        "receipt": r,
        "receipt_bytes": rb,
        "receipt_path": key + "/" + Path(rp).name,
        "verdict_bytes": vb,
        "verdict_path": key + "/" + Path(vp).name,
        "manifest": m,
        "manifest_bytes": mb,
        "manifest_path": key + "/" + Path(mp).name,
        "tracked_round_dir": key,
        "history_sources": [
            {
                "evidence": ref,
                "path": i["source"],
                "sha256": i["sha256"],
                "bytes": i["bytes"],
            }
            for i in m["inputs"]
        ],
        "sealed_inputs": sealed,
        "evidence": ref,
    }


def equivalent_evidence(archive, left, right):
    if left == right:
        return True
    ld, lf = evidence(archive.repo, left, archive)
    rd, rf = evidence(archive.repo, right, archive)
    if ld["kind"] != "legacy-import" or rd["kind"] != "legacy-import":
        return False
    if ld["legacy_source"] is None or ld["legacy_source"] != rd["legacy_source"]:
        return False
    source = ld["legacy_source"]
    originals = legacy_files(archive.repo, dict(kind="legacy-git", **source))
    return all(lf.get(p) == raw == rf.get(p) for p, raw in originals.items())


def response_chain(archive, source):
    entries = {}
    for ref, d, files in archive.publications():
        if (
            d["kind"] == "response"
            and d["parents"]
            and equivalent_evidence(archive, d["parents"][0], source)
        ):
            require(len(d["parents"]) in (1, 2), "response parents")
            entries[canonical(ref)] = (ref, d, files)
    children = {}
    for key, (ref, d, files) in entries.items():
        parent = canonical(d["parents"][1]) if len(d["parents"]) == 2 else None
        require(
            parent is None or parent in entries,
            "response parent missing",
            "archive-incomplete",
        )
        require(parent not in children, "response chain fork", "archive-conflict")
        children[parent] = key
    chain = []
    key = children.get(None)
    while key is not None:
        require(
            key not in [canonical(x[0]) for x in chain],
            "response cycle",
            "archive-conflict",
        )
        chain.append(entries[key])
        key = children.get(key)
    require(
        len(chain) == len(entries), "disconnected response chain", "archive-conflict"
    )
    return chain


def previous_decisions(repo, request, previous, machine):
    archive = configured(repo)
    pr = request["previous_round"]
    chain = response_chain(archive, pr["evidence"])
    if machine["verdict"] != "FAIL":
        require(pr["response"] is None, "non-FAIL previous requires null response")
        return None, None
    require(
        pr["response"] is not None and chain and chain[-1][0] == pr["response"],
        "FAIL requires latest response",
        "archive-incomplete",
    )
    d, files = archive.read(pr["response"])
    raw = files.get("decisions.json")
    require(raw is not None, "response missing decisions")
    doc = strict(raw)
    require(
        doc["verdict_sha256"] == sha(previous["verdict_bytes"]),
        "response verdict differs",
    )
    return doc, raw


@lock_scope()
def respond(repo, obj, raw):
    import review_channel_decisions as D
    import review_channel_verdict as V
    import review_channel_base as base

    require(isinstance(obj, dict), "decision input object")
    allowed = {
        "schema",
        "subject",
        "stage",
        "round",
        "task_record",
        "decisions",
        "source",
        "supersedes",
    }
    require(
        set(obj) <= allowed
        and {"schema", "subject", "stage", "round", "decisions", "source", "supersedes"}
        <= set(obj),
        "decision input fields",
    )
    require(
        obj["schema"] == "review-channel-decisions-input/v2", "decision input schema"
    )
    legacy = {
        k: v for k, v in obj.items() if k not in ("schema", "source", "supersedes")
    }
    require(not D.input_problems(legacy), "decision input grammar")
    archive = configured(repo, True)
    d, files = evidence(repo, obj["source"], archive)
    key = d["round_key"]
    archive.lock(key)
    expected = (
        "tasks/" + obj["task_record"] + "/reviews/" + obj["stage"] + "-" + obj["round"]
        if obj.get("task_record")
        else "reviews/" + obj["subject"] + "/" + obj["round"]
    )
    require(key == expected, "response source route differs")
    chain = response_chain(archive, obj["source"])
    for ref, old, originals in chain:
        if originals.get("decision-input.json") == raw:
            return ref, strict(originals["decisions.json"]), originals["decisions.json"]
    latest = chain[-1][0] if chain else None
    require(
        obj["supersedes"] == latest,
        "supersedes must name latest response",
        "archive-conflict",
    )
    if latest:
        for _ref, published, _files in archive.publications():
            require(
                published["kind"] != "round" or latest not in published["parents"],
                "response already consumed by published round",
                "archive-conflict",
            )
    for pending in (
        (archive.primary / "pending").iterdir()
        if (archive.primary / "pending").exists()
        else []
    ):
        rp = pending / "request.json"
        sp = pending / "attempt.json"
        if rp.is_file() and sp.is_file():
            state = strict(read_file(sp))
            rq = strict(read_file(rp))
            if (
                state.get("phase") in ("sealed", "calling", "called", "validated")
                and latest
                and rq.get("previous_round", {}).get("response") == latest
            ):
                require(
                    False, "response already sealed by next round", "archive-conflict"
                )
    vp, vb = role_file(d, files, "verdict")
    rp, rb = role_file(d, files, "receipt")
    r = strict(rb)
    block, _, _ = V.parse_published(vb)
    require(
        block.get("verdict") == "FAIL"
        and r.get("verdict_sha256") == sha(vb)
        and r.get("verdict_published") is True,
        "response requires published FAIL",
    )
    findings = block.get("findings", [])
    problems = D.decision_problems(obj["decisions"], findings)
    require(not problems, "; ".join(problems))
    routing = {k: obj.get(k) for k in ("subject", "stage", "round", "task_record")}
    doc = D.build_document(routing, sha(vb), obj["decisions"])
    data = base.pretty_json(doc)
    parents = [obj["source"]] + ([latest] if latest else [])
    identity = canonical({"verdict_sha256": sha(vb), "receipt_sha256": sha(rb)})
    ref = archive.publish(
        "response",
        key,
        "response-" + sha(raw),
        {
            "decision-input.json": (raw, "decisions"),
            "decisions.json": (data, "decisions"),
            "source-identities.json": (identity, "other-original"),
        },
        parents,
    )
    return ref, doc, data


def minimal_result(repo, ref):
    import review_channel_verdict as V

    d, files = evidence(repo, ref)
    require(d["kind"] in ("round", "attempt", "legacy-import"), "not a result source")
    _, rb = role_file(d, files, "receipt")
    r = strict(rb)
    candidates = []
    manifest_sha = None
    outcome = "NO_VALID_VERDICT"
    if r.get("mode") == "review" and r.get("verdict_published") is True:
        _, mb = role_file(d, files, "bundle-manifest")
        m = strict(mb)
        manifest_sha = m["manifest_sha256"]
        require(manifest_sha == r["input_manifest_sha256"], "result manifest differs")
        candidates = [
            {"path": i["source"], "bytes": i["bytes"], "sha256": i["sha256"]}
            for i in m["inputs"]
            if i["role"] == "candidate"
        ]
        _, vb = role_file(d, files, "verdict")
        require(sha(vb) == r["verdict_sha256"], "result verdict differs")
        v, _, _ = V.parse_published(vb)
        outcome = v["verdict"]
    return {
        "schema": "review-result/v1",
        "source": ref,
        "input_manifest_sha256": manifest_sha,
        "candidates": candidates,
        "outcome": outcome,
        "residuals": result_residuals(repo, d),
    }


def result_residuals(repo, document):
    """Retained skip decisions carry their immutable response reference."""
    residuals = []
    for parent in document.get("parents", []):
        if parent["kind"] != "archive":
            continue
        d, files = evidence(repo, parent)
        if d["kind"] != "response":
            continue
        doc = strict(files["decisions.json"])
        for decision in doc["decisions"]:
            if decision["action"] == "skip":
                residuals.append(
                    {
                        "id": decision["finding_id"],
                        "disposition": "skip",
                        "decision_ref": parent,
                    }
                )
    return residuals


def result_block(data):
    """Offline syntax validation. It never opens private archive paths."""
    blocks = re.findall(rb"^```review-result\n(.*?)\n```[ \t]*$", data, re.M | re.S)
    require(len(blocks) == 1, "formal file must contain one review-result block")
    obj = strict(blocks[0])
    closed(
        obj,
        (
            "schema",
            "source",
            "input_manifest_sha256",
            "candidates",
            "outcome",
            "residuals",
        ),
        "review-result",
    )
    reference(obj["source"])
    require(
        obj["schema"] == "review-result/v1"
        and obj["outcome"] in ("PASS", "FAIL", "NO_VALID_VERDICT"),
        "result schema/outcome",
    )
    require(
        obj["input_manifest_sha256"] is None
        or isinstance(obj["input_manifest_sha256"], str)
        and HEX.fullmatch(obj["input_manifest_sha256"]),
        "result manifest identity",
    )
    require(
        isinstance(obj["candidates"], list) and isinstance(obj["residuals"], list),
        "result arrays",
    )
    names = []
    for c in obj["candidates"]:
        closed(c, ("path", "bytes", "sha256"), "result candidate")
        require(
            relative(c["path"])
            and type(c["bytes"]) is int
            and c["bytes"] >= 0
            and isinstance(c["sha256"], str)
            and HEX.fullmatch(c["sha256"]),
            "result candidate identity",
        )
        names.append(c["path"])
    require(len(names) == len(set(names)), "duplicate result candidates")
    for r in obj["residuals"]:
        closed(r, ("id", "disposition", "decision_ref"), "result residual")
        require(
            isinstance(r["id"], str)
            and r["id"]
            and r["disposition"] in ("fix", "skip", "approve")
            and isinstance(r["decision_ref"], (str, dict))
            and r["decision_ref"],
            "result residual",
        )
        if isinstance(r["decision_ref"], dict):
            reference(r["decision_ref"])
            require(
                r["decision_ref"]["kind"] == "archive",
                "residual decision must be response ArchiveRef",
            )
    return obj


def verify_result(repo, data):
    obj = result_block(data)
    actual = minimal_result(repo, obj["source"])
    require(
        all(obj[k] == actual[k] for k in actual if k != "residuals"),
        "formal result differs from originals",
    )
    require(
        all(r in obj["residuals"] for r in actual["residuals"]),
        "formal result omits retained response decisions",
    )
    # Additional formal decisions may retain original fixed Git locations.
    for r in obj["residuals"]:
        if isinstance(r["decision_ref"], dict):
            d, files = evidence(repo, r["decision_ref"])
            require(d["kind"] == "response", "residual reference is not response")
            decisions = strict(files["decisions.json"])["decisions"]
            require(
                any(
                    x["finding_id"] == r["id"] and x["action"] == r["disposition"]
                    for x in decisions
                ),
                "residual disposition differs from original response",
            )
            continue
        path, sep, loc = r["decision_ref"].partition("#")
        require(relative(path) and sep and loc, "residual decision locator")
        content = git(repo, "show", "HEAD:" + path)
        require(r["id"].encode() in content, "residual decision id absent")
    return obj


def check_pending(archive, round_key):
    pending = archive.primary / "pending"
    if not pending.exists():
        return
    for p in pending.iterdir():
        if not p.is_dir() or p.is_symlink():
            continue
        state = p / "attempt.json"
        if not state.is_file():
            continue
        d = strict(read_file(state))
        if d.get("routing", {}).get("tracked_round_dir") != round_key:
            continue
        if (p / "publication-completed.json").is_file():
            i = strict(read_file(p / "publication-completed.json"))
            archive.read(archive.ref(i["object_sha256"]))
            continue
        require(
            False,
            "pending attempt requires explicit archive recover: " + p.name,
            "archive-incomplete",
        )


@lock_scope()
def capture_attempt(archive, directory, kind):
    """Freeze bytes already persisted by the channel; never consult working candidates."""
    directory = Path(directory)
    state = strict(read_file(directory / "attempt.json"))
    rq = strict(read_file(directory / "request.json"))
    key = state["routing"]["tracked_round_dir"]
    archive.lock(key)
    sources = tree_files(directory)
    rows = {}
    manifest = {}
    if "bundle_manifest.json" in sources:
        manifest = strict(read_file(sources["bundle_manifest.json"]))
    by_name = {i["bundle_name"]: i for i in manifest.get("inputs", [])}
    receipt = (
        strict(read_file(sources["receipt.json"]))
        if "receipt.json" in sources
        else None
    )
    for name, p in sources.items():
        if name.startswith("publication-"):
            continue
        role = "other-original"
        if name == "request.json":
            role = "request"
        elif name == "receipt.json":
            role = "receipt"
        elif name == "bundle_manifest.json":
            role = "bundle-manifest"
        elif name == "effective_profile.json":
            role = "profile"
        elif (
            receipt
            and receipt.get("verdict_path")
            and name == Path(receipt["verdict_path"]).name
        ):
            role = "verdict"
        elif name.startswith("inputs/") and Path(name).name in by_name:
            i = by_name[Path(name).name]
            role = {
                "review-task": "taskbook",
                "candidate": "candidate",
                "reference": "reference",
            }[i["role"]]
            if (
                Path(name).name.startswith("previous--")
                and "candidate" in Path(name).name
            ):
                role = "baseline"
        elif name.startswith("baselines/"):
            role = "baseline"
        elif Path(name).name in (
            "reviewer_last_message.md",
            "reemit-reviewer_last_message.md",
            "runtime_events.jsonl",
            "reemit-runtime_events.jsonl",
            "stderr.log",
            "reemit-stderr.log",
            "response.json",
            "reemit-response.json",
            "request_shape.json",
            "reemit-request_shape.json",
        ):
            role = "raw-output"
        rows[name] = (read_file(p), role)
    if receipt and "effective_profile.json" not in rows:
        rows["effective_profile.json"] = (
            canonical({"effective_profile": receipt.get("effective_profile")}),
            "profile",
        )
    # Only the completed preflight may authorize following parents. Rejected
    # requests retain original bytes without resolving unverified external refs.
    parents=state.get('verified_previous_refs',[])
    require(isinstance(parents,list),'verified parent list')
    for parent in parents:reference(parent)
    return archive.publish(kind, key, state["attempt_id"], rows, parents)


@lock_scope()
def recover(archive, attempt_id):
    """Complete one saved identity after its owner exited. No provider dependency."""
    import review_channel_base as base

    require(relative(attempt_id) and "/" not in attempt_id, "attempt id")
    pending = safe_path(archive.primary, "pending/" + attempt_id)
    state_path = pending / "attempt.json"
    intent_path = pending / "publication-intent.json"
    require(
        state_path.is_file() or intent_path.is_file(),
        "pending owner identity missing",
        "archive-incomplete",
    )
    state = (
        strict(read_file(state_path))
        if state_path.is_file()
        else strict(read_file(intent_path)).get("owner", {})
    )
    pid = state.get("pid")
    token = state.get("pid_start")
    require(
        type(pid) is int and isinstance(token, str) and token,
        "pending generation unavailable",
        "archive-incomplete",
    )
    current = base.process_start_token(pid)
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True
    require(
        not alive or current is not None and current != token,
        "original execution still alive or generation uncertain",
        "archive-conflict",
    )
    if intent_path.is_file():
        intent = strict(read_file(intent_path))
        key = intent["round_key"]
        if key is not None:
            archive.lock(key)
        digest = intent["object_sha256"]
        require(
            intent.get("owner", {}).get("pid") == pid
            and intent.get("owner", {}).get("pid_start") == token,
            "generation differs from publication intent",
            "archive-conflict",
        )
        if not (archive.primary / "objects" / digest).exists():
            matches = []
            for stage in (archive.primary / "pending").glob("object-*"):
                if (stage / "archive.json").is_file() and sha(
                    read_file(stage / "archive.json")
                ) == digest:
                    matches.append(stage)
            require(
                len(matches) == 1,
                "saved object not uniquely recoverable",
                "archive-incomplete",
            )
            archive._object_at(matches[0], digest)
            target = safe_path(archive.primary, "objects/" + digest, False)
            target.parent.mkdir(exist_ok=True)
            os.rename(matches[0], target)
            sync_dir(target.parent)
        ref = archive.complete(digest)
        atomic_state(pending / "publication-completed.json", canonical(intent))
        return {"source": ref, "provider_repeated": False}
    key = state.get("routing", {}).get("tracked_round_dir")
    archive.lock(key)
    # A call without an already written and validated Receipt cannot become a success.
    receipt = pending / "receipt.json"
    if receipt.is_file():
        r = strict(read_file(receipt))
        kind = (
            "round"
            if r.get("verdict_validation") == "VALID"
            and r.get("verdict_published") is True
            else "attempt"
        )
        ref = capture_attempt(archive, pending, kind)
        return {"source": ref, "provider_repeated": False}
    require(
        state.get("phase") in ("routed", "sealed"),
        "call outcome uncertain; saved evidence cannot authorize replay",
        "archive-incomplete",
    )
    import review_channel_receipt as RC

    r = RC.synthesize_interrupted(
        state,
        {
            "pid": os.getpid(),
            "pid_start": base.process_start_token(os.getpid()),
            "attempt_id": None,
            "role": "archive-recover",
        },
    )
    r["receipt_schema"] = C.RECEIPT_SCHEMA
    r["evidence_storage"] = {
        "kind": "archive",
        "repository_id": archive.repository_id,
        "round_key": key,
    }
    write_new(receipt, canonical(r))
    ref = capture_attempt(archive, pending, "attempt")
    return {"source": ref, "provider_repeated": False, "classification": "interrupted"}


def capability_evidence(obj):
    require(isinstance(obj, dict), "capability evidence required")
    if obj.get("kind") == "legacy-git":
        closed(
            obj,
            ("kind", "receipt_ref", "receipt_commit", "receipt_sha256"),
            "capability legacy evidence",
        )
        require(
            relative(obj["receipt_ref"])
            and isinstance(obj["receipt_commit"], str)
            and COMMIT.fullmatch(obj["receipt_commit"]),
            "capability legacy identity",
        )
    elif obj.get("kind") == "archive":
        closed(
            obj,
            ("kind", "ref", "receipt_path", "receipt_sha256", "decision"),
            "capability archive evidence",
        )
        reference(obj["ref"])
        require(
            obj["ref"]["kind"] == "archive" and relative(obj["receipt_path"]),
            "capability archive identity",
        )
        decision = obj["decision"]
        closed(decision, ("commit", "path", "locator"), "capability formal decision")
        require(
            isinstance(decision["commit"], str)
            and COMMIT.fullmatch(decision["commit"])
            and relative(decision["path"])
            and decision["locator"] == "review-result",
            "capability fixed decision identity",
        )
    else:
        require(False, "unknown capability evidence kind")
    require(
        isinstance(obj["receipt_sha256"], str) and HEX.fullmatch(obj["receipt_sha256"]),
        "capability Receipt digest",
    )
    return obj


def forbidden_evidence_path(path):
    return (
        path.startswith(("reviews/", "review-attempts/"))
        or bool(re.match(r"^tasks/[^/]+/(?:reviews|attempts)/", path))
        or bool(
            re.match(
                r"^records/diagnostics/review-channel/.+/receipt[^/]*\.json$",
                path,
                re.I,
            )
        )
    )


def full_evidence_document(raw):
    """Recognize complete stored shapes, not a schema name quoted by source code."""
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return False
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[m.start() :])
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if {
            "receipt_schema",
            "attempt_id",
            "effective_profile",
            "classification",
            "request_sha256",
            "diagnostics",
        } <= set(obj):
            return True
        if {"verdict_schema", "verdict", "findings", "question_assessments"} <= set(
            obj
        ):
            return True
        if {
            "request_schema",
            "subject",
            "stage",
            "round",
            "inputs",
            "review_brief",
        } <= set(obj):
            # A documentation template with both routing identities still
            # unfilled is not an actual request. Known-original byte matching
            # remains independent and still catches copied malformed evidence.
            placeholder_subject = isinstance(obj["subject"], str) and re.fullmatch(
                r"<[^<>]+>", obj["subject"]
            )
            placeholder_round = isinstance(obj["round"], str) and re.fullmatch(
                r"r<[^<>]+>", obj["round"]
            )
            if not (placeholder_subject and placeholder_round):
                return True
        if {
            "manifest_sha256",
            "inputs",
            "subject",
            "stage",
            "round",
            "task_file",
        } <= set(obj):
            return True
        if {
            "profile_schema",
            "route_provider",
            "requested_model",
            "calls",
            "verdict_sha256",
        } <= set(obj):
            return True
        if {
            "decisions_schema",
            "subject",
            "round",
            "verdict_sha256",
            "decisions",
            "decided_at",
        } <= set(obj):
            return True
    return False


def verify_new_commits(repo, target, source, remote_source=None):
    """Inspect every new commit, including originals later removed or renamed."""
    require(
        COMMIT.fullmatch(target or "") and COMMIT.fullmatch(source or ""),
        "publish requires fixed source/target",
    )
    require(
        remote_source is None or COMMIT.fullmatch(remote_source),
        "remote source identity",
    )
    require(
        git(repo, "rev-parse", "--is-shallow-repository").strip() == b"false",
        "shallow history cannot prove publication boundary",
        "archive-incomplete",
    )
    git(repo, "merge-base", "--is-ancestor", target, source)
    if remote_source:
        git(repo, "merge-base", "--is-ancestor", remote_source, source)
    config_identity = canonical(local_config(repo))
    archive = configured(repo)
    known = []
    if archive:
        for ref, d, files in archive.publications():
            roles = {f["path"]: f["role"] for f in d["files"]}
            for path, raw in files.items():
                if roles[path] in (
                    "taskbook",
                    "verdict",
                    "bundle-manifest",
                    "receipt",
                    "request",
                    "profile",
                    "raw-output",
                    "decisions",
                ):
                    known.append(raw)
    hashes = {sha(raw) for raw in known}
    embedded = [raw for raw in known if len(raw) >= 128]
    h = history()
    try:
        g = h.GitHistory(str(repo), source)
        base_tree = g.tree(target)
        commits = git(repo, "rev-list", source, "^" + target).decode().split()
        checked = set()
        formal = set()
        for commit in commits:
            for path, entry in g.tree(commit).items():
                if base_tree.get(path) == entry:
                    continue
                key = (path, entry)
                if key in checked:
                    continue
                checked.add(key)
                require(
                    not forbidden_evidence_path(path),
                    "new commit contains forbidden evidence path: "
                    + commit
                    + ":"
                    + path,
                    "archive-conflict",
                )
                require(
                    entry[1] == "blob",
                    "new non-blob entry cannot be inspected: " + path,
                    "archive-incomplete",
                )
                raw = g.blob(commit, path)["content"]
                baseline_raw = (
                    g.blob(target, path)["content"] if path in base_tree else b""
                )
                require(
                    not (sha(raw) in hashes and raw not in baseline_raw)
                    and not any(x in raw and x not in baseline_raw for x in embedded)
                    and not full_evidence_document(raw),
                    "new commit contains full review evidence: " + commit + ":" + path,
                    "archive-conflict",
                )
                if re.search(rb"^```review-result\n", raw, re.M):
                    verify_result(repo, raw)
                    formal.add(path)
        require(
            git(repo, "rev-parse", "HEAD").decode().strip() == source,
            "source changed during publication verification",
            "archive-conflict",
        )
        require(
            canonical(local_config(repo)) == config_identity,
            "archive configuration changed during verification",
            "archive-conflict",
        )
        return {
            "source": source,
            "target": target,
            "remote_source": remote_source,
            "commits_checked": len(commits),
            "entries_checked": len(checked),
            "formal_results": sorted(formal),
            "limits": [
                "Unknown rewritten prose and unknown compressed or encoded copies are not mechanically recognizable."
            ],
        }
    except h.HistoryReadError as exc:
        raise EvidenceError(
            "archive-unavailable", exc.code + ": " + exc.message
        ) from exc


def formal_refs(repo, refs, working=False):
    results = []
    for ref in refs:
        if not isinstance(ref, str) or not ref.endswith("#review-result"):
            continue
        path = ref[: -len("#review-result")]
        require(relative(path), "formal path")
        raw = (
            read_file(safe_path(repo, path))
            if working
            else git(repo, "show", "HEAD:" + path)
        )
        result = verify_result(repo, raw)
        for candidate in result["candidates"]:
            content = (
                read_file(safe_path(repo, candidate["path"]))
                if working
                else git(repo, "show", "HEAD:" + candidate["path"])
            )
            require(
                len(content) == candidate["bytes"]
                and sha(content) == candidate["sha256"],
                "current candidate differs from reviewed identity: "
                + candidate["path"],
                "archive-conflict",
            )
        results.append(result)
    return results


def import_migration_plan(repo, archive, plan, restore_root):
    """Explicit, additive migration. No configuration, switch, or source deletion."""
    closed(
        plan,
        ("schema", "legacy_commit", "groups", "missing", "unclassified"),
        "migration plan",
    )
    require(plan["schema"] == "review-archive-migration/v1", "migration schema")
    require(
        isinstance(plan["legacy_commit"], str)
        and COMMIT.fullmatch(plan["legacy_commit"]),
        "fixed migration cutoff",
    )
    git(repo, "merge-base", "--is-ancestor", plan["legacy_commit"], "HEAD")
    require(
        all(isinstance(plan[k], list) for k in ("groups", "missing", "unclassified")),
        "migration arrays",
    )
    restore_root = Path(restore_root)
    require(
        restore_root.is_absolute()
        and restore_root.is_dir()
        and restore_root.resolve() == restore_root
        and not any(restore_root.iterdir()),
        "migration restore root must be empty canonical directory",
    )
    inventory = {
        "schema": "review-archive-inventory/v1",
        "files": [],
        "missing": plan["missing"],
        "unclassified": plan["unclassified"],
    }
    parents = []
    seen = set()
    restored = []
    for group in plan["groups"]:
        closed(
            group,
            ("round_key", "legacy_source", "files", "limitations"),
            "migration group",
        )
        require(
            isinstance(group["round_key"], str) and ROUND.fullmatch(group["round_key"]),
            "migration logical round",
        )
        require(
            isinstance(group["files"], list) and group["files"], "empty migration group"
        )
        payload = {}
        sources = []
        for row in group["files"]:
            closed(
                row,
                (
                    "source_root",
                    "path",
                    "payload_path",
                    "bytes",
                    "sha256",
                    "role",
                    "source_commit",
                    "blob_oid",
                ),
                "migration file",
            )
            require(
                relative(row["path"])
                and relative(row["payload_path"])
                and row["role"] in ROLES,
                "migration path/role",
            )
            identity = (row["source_commit"], row["source_root"], row["path"])
            require(identity not in seen, "duplicate migration source")
            seen.add(identity)
            require(row["payload_path"] not in payload, "duplicate migration payload")
            if row["source_commit"] is not None:
                item = fixed_file(repo, row["source_commit"], row["path"])
                raw = item["content"]
                require(item["blob_oid"] == row["blob_oid"], "migration blob identity")
            else:
                source_root = Path(row["source_root"])
                require(
                    source_root.is_absolute() and source_root.resolve() == source_root,
                    "local source root must be canonical",
                )
                require(row["blob_oid"] is None, "local original cannot claim Git blob")
                raw = read_file(safe_path(source_root, row["path"]))
            require(
                type(row["bytes"]) is int
                and len(raw) == row["bytes"]
                and sha(raw) == row["sha256"],
                "migration source differs",
            )
            payload[row["payload_path"]] = (raw, row["role"])
            sources.append(row)
        # Transitional rounds can have been published locally before archive
        # activation without ever entering Git. Preserve their original route
        # as legacy evidence; their Receipt still determines whether they count.
        local_round = any(
            row["path"].rsplit("/", 1)[0] == group["round_key"]
            and row["payload_path"] == row["path"]
            for row in sources
        )
        kind = (
            "legacy-import"
            if group["legacy_source"] is not None or local_round
            else "attempt"
        )
        # Preserve nonstandard source paths without treating old schemas as new rounds.
        if group["legacy_source"] is not None:
            legacy_ref = dict(kind="legacy-git", **group["legacy_source"])
            originals = legacy_files(repo, legacy_ref)
            require(
                all(
                    p in payload and payload[p][0] == raw
                    for p, raw in originals.items()
                ),
                "migration omits fixed round originals",
            )
            require(
                legacy_round_key(legacy_ref["round_path"]) == group["round_key"],
                "legacy logical identity differs",
            )
        batch_id = "migration-" + sha(canonical(group))
        ref = archive.publish(
            kind,
            group["round_key"],
            batch_id,
            payload,
            legacy_source=group["legacy_source"],
            limitations=group["limitations"],
        )
        if kind == "legacy-import":
            parents.append(ref)
        dest = restore_root / ref["object_sha256"]
        dest.mkdir()
        restored.append(archive.restore(ref, dest))
        for row in sources:
            inventory["files"].append(
                {
                    k: row[k]
                    for k in ("path", "source_commit", "blob_oid", "bytes", "sha256")
                }
                | {"ref": ref, "payload_path": row["payload_path"]}
            )
    inventory["files"].sort(
        key=lambda r: ((r["source_commit"] or ""), r["path"].encode())
    )
    ref = archive.publish(
        "inventory",
        None,
        None,
        {"inventory.json": (canonical(inventory), "other-original")},
        parents,
    )
    # Run the same cutoff-coverage verifier consumed after a formal switch.
    validate_legacy_inventory(
        repo,
        archive,
        {
            "repository_id": archive.repository_id,
            "legacy_commit": plan["legacy_commit"],
            "inventory_ref": ref,
        },
    )
    dest = restore_root / ref["object_sha256"]
    dest.mkdir()
    archive.restore(ref, dest)
    return {
        "inventory_ref": ref,
        "legacy_commit": plan["legacy_commit"],
        "files": len(inventory["files"]),
        "groups": len(plan["groups"]),
        "restored_groups": len(restored),
        "same_device": archive.primary.stat().st_dev == archive.backup.stat().st_dev,
        "sources_deleted": False,
    }


def retire_sources(repo, archive, paths, source_root, protected_decision=None):
    """Explicit exact-file retirement after switch, copy verification, and restore.

    The caller supplies the Owner-authorized list; an inventory never authorizes
    deleting every file in a parent directory. Non-review task attempts remain.
    """
    configured_archive = configured(repo, True)
    require(
        configured_archive
        and configured_archive.repository_id == archive.repository_id,
        "retirement requires active switch",
    )
    decision = switch_record(repo)
    inventory = validate_legacy_inventory(repo, archive, decision)
    require(
        isinstance(paths, list) and paths and len(paths) == len(set(paths)),
        "exact unique deletion list required",
    )
    source_root = Path(source_root)
    require(
        source_root.is_absolute() and source_root.resolve() == source_root,
        "canonical source root",
    )
    rows = {}
    for row in inventory["files"]:
        rows.setdefault(row["path"], []).append(row)
    selected = []
    for path in paths:
        require(
            relative(path) and forbidden_evidence_path(path),
            "path outside evidence retirement scope",
        )
        require(
            path in rows and len({(r["sha256"], r["bytes"]) for r in rows[path]}) == 1,
            "source absent or ambiguous in inventory",
            "archive-incomplete",
        )
        row = rows[path][0]
        d, files = archive.read(row["ref"])
        raw = files[row["payload_path"]]
        require(
            read_file(safe_path(source_root, path)) == raw,
            "deletion source differs from saved bytes",
            "archive-conflict",
        )
        if path.startswith(
            (
                "tasks/gov-t11/reviews/task-r1/",
                "tasks/gov-t11/reviews/task-r2/",
                "reviews/changelog-cl-41/r1/",
            )
        ):
            require(
                protected_decision is not None,
                "protected group needs successor formal disposition",
                "archive-incomplete",
            )
            require(relative(protected_decision), "protected disposition path")
            fixed = git(repo, "show", "HEAD:" + protected_decision)
            require(
                b"RU-02-close" in fixed and b"CL-55" in fixed,
                "successor disposition must name original ruling and CL-55",
            )
        check_pending(archive, d["round_key"])
        selected.append((path, row, raw))
    # A restore proof is checked by recomputing the actual restored files, not a boolean marker.
    restore_root = Path(archive.primary).parent / (
        "retirement-restore-" + uuid.uuid4().hex
    )
    restore_root.mkdir(mode=0o700)
    for ref_key in sorted({canonical(r["ref"]) for _, r, _ in selected}):
        ref = strict(ref_key)
        dest = restore_root / ref["object_sha256"]
        dest.mkdir()
        archive.restore(ref, dest)
    for path, row, raw in selected:
        p = safe_path(source_root, path)
        require(
            read_file(p) == raw,
            "source changed immediately before deletion",
            "archive-conflict",
        )
        p.unlink()
        sync_dir(p.parent)
    return {
        "removed": paths,
        "inventory_ref": decision["inventory_ref"],
        "restore_root": str(restore_root),
        "parent_directories_removed": False,
    }


def recover_snapshot(repo, digest, history_sources=None):
    archive = configured(repo)
    if archive is None:
        return None
    for ref, document, files in archive.publications():
        if document["kind"] not in ("round", "legacy-import", "attempt"):
            continue
        for item in document["files"]:
            if item["sha256"] == digest:
                if history_sources is not None:
                    history_sources.append(
                        {
                            "evidence": ref,
                            "path": item["path"],
                            "sha256": digest,
                            "bytes": item["bytes"],
                        }
                    )
                return files[item["path"]]
    return None
