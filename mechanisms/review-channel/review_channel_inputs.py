"""review_channel_inputs — 被审输入：引用解析、封存、清单、禁名、缺件 fail-closed（设计 §6.2）。

- 引用解析从引用语法出发（三类形态），仓根闭集机械取自 mechanisms/gates/rules_catalog.json；
  裸文件名引用先按形态取解析域（域一 = 规格链目录、域二 = 机制区单元目录、域三 = 全仓；Amendment 1），
  每个候选引用分别 fail-closed：reference-unresolved / reference-ambiguous / reference-missing；
  豁免只对仓外对象成立（evidence_limits[].reference，kind 缺省 exemption），
  规定落点由 evidence_limits[].kind = planned-location 显式声明、通道再校验两项；`D-<NN>@rN` 形态不进入提取集（明示排除）。
- 域一引用只核路径恰一命中；该件的现行修订号按 freeze-record 设计 §4.3 读取（records/governance/<subject>/freeze-records.jsonl
  最后一行 freeze / re-freeze 行，无该文件时取编号最大的 `.md` 记录文件名），只作任务书并列标注、不作拒绝判据；
  在案修订号核对、登记本核对、档案库时代修订指称与锁值引用件整族已随减重第 3 步退役（台账 repo-od-10:KB-08，Owner 裁定 F3 甲）。
- r1 改动区基线（§6.2 第 6 条，Amendment 4）：同一反查取候选路径在现行冻结事实内的身份（jsonl 行 objects[].sha256；`.md` 记录取
  fenced JSON 的 frozen_objects[].sha256，雏形记录无机械正本即身份不可得），再按 recover_baseline 取字节；无身份即首冻、整件为改动区。
- 投递名派生整束一次性全局唯一（Amendment 1）：固定名预占、来源路径字典序、禁名表改写、逐段扩展、保留前缀隔离、
  重复来源前置去重（duplicate-reference-source）、路径段取尽即 bundle-name-collision。
- 封存：候选与参考件逐件复制进 attempt 目录、逐件 cmp、置只读；预清单从封存副本计算。
- r2+：previous-- 族固定投递名自动入束（3 + 候选数件，Amendment 2；基线逐候选、优先取 attempts 封存副本，缺失时从 Git 历史取）。
- Amendment 2：多候选逐件解析（候选互引记 co-candidate）。
本模块不导入其他契约模块。
"""
import fnmatch
import os
import re
import stat

import review_channel_base as base
import review_channel_contract as C
import review_channel_history as H

INPUTS_SUBDIR = "inputs"
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_AT_ROUND_RE = re.compile(r"^([A-Za-z0-9_.-]+)@r([1-9][0-9]*)(?:\s.*)?$")
_TEMPLATE_MARKERS = ("<", ">", "*", "…")
_INTERNAL_FORMS = (".publishing-", "publish.tmp-", ".alloc-", ".in-progress", "receipt.json.tmp-")
_SCHEMA_ID_RE = re.compile(r"/v[0-9]")


# ---------------------------------------------------------------- 仓根闭集

def root_closure(repo_root):
    path = os.path.join(repo_root, base.RULES_CATALOG_PATH)
    try:
        cat = base.strict_json_load(base.read_bytes(path))
        boot = cat["profiles"]["bootstrap"]
        dirs = list(boot["top_level_members"]["values"])
        files = list(boot["root_tracked_members"]["values"])
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise base.PreflightError("root-closure-unavailable",
                                  "%s unreadable or lacks the two bootstrap member keys" % base.RULES_CATALOG_PATH)
    members = dirs + files
    if not members or not all(isinstance(m, str) and m for m in members):
        raise base.PreflightError("root-closure-unavailable", "root closure members malformed")
    # 解析域的两个顶层目录名（域一、域二）须逐字命中 top_level_members（Amendment 1）；任一不命中即 root-closure-unavailable，不回退全仓匹配
    for d in C.DOMAIN_DIRS:
        if d not in dirs:
            raise base.PreflightError("root-closure-unavailable",
                                      "resolution-domain directory %r is not a top_level_members value in %s" % (d, base.RULES_CATALOG_PATH))
    return {"dirs": frozenset(dirs), "files": frozenset(files)}


# ---------------------------------------------------------------- 引用提取

def _strip_fences(text):
    """去掉围栏代码块内容（围栏内不扫描）。返回 (lines_outside, header_lines)。"""
    out = []
    open_char = None
    open_len = 0
    for line in text.split("\n"):
        m = C.FENCE_LINE_RE.match(line)
        if open_char:
            if m and m.group(2)[0] == open_char and len(m.group(2)) >= open_len and not m.group(3).strip():
                open_char = None
            continue
        if m:
            open_char = m.group(2)[0]
            open_len = len(m.group(2))
            continue
        out.append(line)
    return out


def _is_excluded(token):
    if any(mk in token for mk in _TEMPLATE_MARKERS):
        return True
    if _SCHEMA_ID_RE.search(token):
        return True
    if token.startswith("~"):
        return True
    if any(f in token for f in _INTERNAL_FORMS):
        return True
    if " " in token.strip() and "@r" not in token:
        return True
    return False


def is_bare_filename(name):
    """裸文件名判据：无路径分隔、含扩展名、不以点起始。`D-<NN>` 一类无扩展名的 Decision ID 不是文件名——
    `D-<NN>@rN` 形态因此不进入提取集（设计 §6.2 第 2 条明示排除，Owner 2026-09-04「域四删」）。"""
    return bool(name) and "/" not in name and "." in name.strip(".") and not name.startswith(".")


def extract_candidate_references(text, closure):
    """返回有序去重列表 [{form, token, revision}]：form ∈ path | depends-on | at-round；
    revision = at-round 形态所带修订号（整数），其余为 None。"""
    found = []
    seen = set()

    def add(form, token, revision=None):
        key = (form, token, revision)
        if key in seen:
            return
        seen.add(key)
        found.append({"form": form, "token": token, "revision": revision})

    dirs = closure["dirs"]
    files = closure["files"]

    def path_token(tok):
        first = tok.split("/", 1)[0]
        if "/" in tok and first in dirs:
            return tok.rstrip("/")
        if tok in files:
            return tok
        return None

    lines = _strip_fences(text)
    in_header = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">") and "Depends on" in stripped:
            in_header = True
            continue
        if in_header:
            if not stripped.startswith(">") or stripped == ">":
                in_header = False
            else:
                for tok in _BACKTICK_RE.findall(stripped):
                    tok = tok.strip()
                    if _is_excluded(tok):
                        continue
                    m = _AT_ROUND_RE.match(tok)
                    name = m.group(1) if m else (tok.split()[0] if tok else "")
                    p = path_token(name)
                    if p is not None:
                        add("path", p)
                    elif is_bare_filename(name):
                        if m:
                            add("at-round", name, int(m.group(2)))
                        else:
                            add("depends-on", name)
                continue
        for tok in _BACKTICK_RE.findall(line):
            tok = tok.strip()
            if not tok or _is_excluded(tok):
                continue
            m = _AT_ROUND_RE.match(tok)
            if m and is_bare_filename(m.group(1)):
                add("at-round", m.group(1), int(m.group(2)))
                continue
            p = path_token(tok)
            if p is not None:
                add("path", p)
    return found


def _repo_files(repo_root):
    proc = base.git_run(repo_root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


# ---------------------------------------------------------------- 解析域（§6.2 第 2 条，Amendment 1）

def reference_domain(ref, spec_chain_names):
    """裸文件名引用的解析域由其形态与出现位置决定：域一 = 规划基线件裸名带修订号（名字属规格链目录直下受跟踪文件）；
    域二 = 页首 Depends on 内的裸机制设计正本名（名字命中布局权威 §5.1 设计正本文法，与 layout 门同一模式）；
    域三 = 其余裸文件名（含页首内的 Task Definition 一类非设计正本裸名）；路径形态不取域。"""
    if ref["form"] == "path":
        return None
    if ref["form"] == "at-round" and ref["token"] in spec_chain_names:
        return "one"
    if ref["form"] == "depends-on" and fnmatch.fnmatchcase(ref["token"], C.DESIGN_MASTER_GLOB):
        return "two"
    return "three"


def domain_match_set(domain, token, files, by_name):
    """各域匹配集派生算法（R1-B1 整改）。"""
    if domain == "one":
        want = C.SPEC_CHAIN_DIR + "/" + token
        return [f for f in files if f == want]
    if domain == "two":
        prefix = C.MECHANISMS_DIR + "/"
        return [f for f in files if f.startswith(prefix) and f.count("/") == 2 and f.rsplit("/", 1)[-1] == token]
    return list(by_name.get(token, []))


# ---------------------------------------------------------------- 现行修订号标注（§6.2 第 2 条；freeze-record 设计 §4.3）

FREEZE_RECORDS_NAME = "freeze-records.jsonl"


def freeze_record_subject_segment(subject):
    """freeze-record 设计 §4.1 的 `<Subject>` 确定性转写：按 `-` 分段、各段首字母大写、以 `_` 连接。"""
    return "_".join(p[:1].upper() + p[1:] for p in subject.split("-"))


def freeze_record_revision(subdir, name):
    """§4.3 其二的文件名文法：`HarnessPlane_<Subject>_Freeze_Record_r<N>.md`，`<Subject>` 由所在子目录名转写而得。
    匹配即返回修订号，否则返回 None。"""
    prefix = "HarnessPlane_" + freeze_record_subject_segment(subdir) + "_Freeze_Record_r"
    suffix = ".md"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    digits = name[len(prefix):-len(suffix)]
    if not digits.isdigit() or not digits.isascii() or digits[0] == "0":
        return None
    return int(digits)


def _object_paths(row):
    objects = row.get("objects")
    return [o.get("path") for o in objects if isinstance(o, dict)] if isinstance(objects, list) else []


def _object_sha256(objects, target_rel):
    """objects[] / frozen_objects[] 内 path 命中项的 sha256（非 64 位十六进制即 None）。"""
    for o in objects or []:
        if isinstance(o, dict) and o.get("path") == target_rel:
            return o["sha256"] if base.hex64(o.get("sha256")) else None
    return None


_FREEZE_FENCE_OPEN_RE = re.compile(r"^ {0,3}```freeze-record\s*$")


def _md_record_objects(path):
    """`.md` 记录文件内唯一 `freeze-record` fenced JSON 的 frozen_objects[]（freeze-record 设计 §3.4 机械正本）；
    无围栏（雏形记录）、多围栏或不可严格解析返回 None（身份不可得，只作标注、不判失败码）。"""
    try:
        lines = base.read_bytes(path).decode("utf-8").split("\n")
    except (OSError, UnicodeDecodeError):
        return None
    blocks = []
    inside = None
    for ln in lines:
        if inside is None:
            if _FREEZE_FENCE_OPEN_RE.match(ln):
                inside = []
        elif ln.strip() == "```":
            blocks.append("\n".join(inside))
            inside = None
        else:
            inside.append(ln)
    if len(blocks) != 1:
        return None
    try:
        row = base.strict_json_load(blocks[0].encode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    objects = row.get("frozen_objects") if isinstance(row, dict) else None
    return objects if isinstance(objects, list) else None


def _jsonl_current_fact(path):
    """§4.3 其一：`freeze-records.jsonl` 内最后一行 event 取 freeze / re-freeze 的行（retire 行不改变现行修订号；其后若无
    freeze / re-freeze 行，retire 行 objects[] 所列路径自该行起不在冻结面内）。返回 (现行事实行, 已退役路径集合)；文件不可读、
    不可严格解析或无此类行返回 (None, 空集合)。只作标注来源，任何形态问题都不判失败码。"""
    try:
        lines = base.read_bytes(path).decode("utf-8").split("\n")
    except (OSError, UnicodeDecodeError):
        return None, set()
    current = None
    retired = set()
    for line in lines:
        if not line:
            continue
        try:
            row = base.strict_json_load(line.encode("utf-8"))
        except ValueError:
            return None, set()
        if not isinstance(row, dict):
            return None, set()
        if row.get("event") in ("freeze", "re-freeze"):
            current = row
            retired = set()
        elif row.get("event") == "retire":
            retired.update(_object_paths(row))
    return current, retired


def _md_current_revision(repo_root, subject):
    """§4.3 其二：该 subject 目录下编号最大的 `HarnessPlane_<Subject>_Freeze_Record_r<N>.md`；无则 None。"""
    try:
        names = os.listdir(os.path.join(repo_root, C.GOVERNANCE_DIR, subject))
    except OSError:
        return None
    revisions = [r for r in (freeze_record_revision(subject, n) for n in names) if r is not None]
    return max(revisions) if revisions else None


def current_freeze_fact(repo_root, target_rel):
    """被引用件 / 候选 target_rel 在现行冻结事实内的身份：{revision, sha256, subject} 或 None（任何冻结事实内无该路径 = 首冻）。
    先按 §4.3 其一在各 subject 的 `freeze-records.jsonl` 内按现行事实的 objects[].path 命中；无命中时按其二取候选 subject
    （机制正本 = 其单元目录名；其余 = 文件名去扩展名）目录下编号最大的 `.md` 记录（该 subject 已有 jsonl 时不回退 `.md`）：
    记录含机械正本时其 frozen_objects[] 须含该路径（不含即非该件的记录），sha256 取自其中；雏形记录无机械正本时 sha256 为 None。
    任何形态问题都不判失败码（只作标注与 r1 基线来源）。"""
    gov = os.path.join(repo_root, C.GOVERNANCE_DIR)
    try:
        subjects = sorted(n for n in os.listdir(gov) if os.path.isdir(os.path.join(gov, n)))
    except OSError:
        subjects = []
    for subject in subjects:
        fact, retired = _jsonl_current_fact(os.path.join(gov, subject, FREEZE_RECORDS_NAME))
        if fact is None or target_rel not in _object_paths(fact):
            continue
        if target_rel in retired:
            return None
        rev = fact.get("revision")
        return {"revision": rev if isinstance(rev, int) and not isinstance(rev, bool) else None,
                "sha256": _object_sha256(fact.get("objects"), target_rel), "subject": subject}
    parts = target_rel.split("/")
    candidates = []
    if len(parts) == 3 and parts[0] == C.MECHANISMS_DIR:
        candidates.append(parts[1])
    stem = parts[-1].rsplit(".", 1)[0]
    if stem not in candidates:
        candidates.append(stem)
    for subject in candidates:
        if os.path.isfile(os.path.join(gov, subject, FREEZE_RECORDS_NAME)):
            continue
        rev = _md_current_revision(repo_root, subject)
        if rev is not None:
            name = "HarnessPlane_%s_Freeze_Record_r%d.md" % (freeze_record_subject_segment(subject), rev)
            objects = _md_record_objects(os.path.join(gov, subject, name))
            if objects is not None and target_rel not in [o.get("path") for o in objects if isinstance(o, dict)]:
                continue
            return {"revision": rev, "sha256": _object_sha256(objects, target_rel) if objects is not None else None,
                    "subject": subject}
    return None


def current_revision(repo_root, target_rel):
    """被引用件 target_rel 的现行修订号（任务书并列标注用；不可得返回 None，恒不判失败码）。"""
    fact = current_freeze_fact(repo_root, target_rel)
    return fact["revision"] if fact is not None else None


def planned_location_check(token, limit, files, closure):
    """规定落点声明的两项机械校验（R28-B2 / R29-B2）：其一，路径首段逐字命中仓根闭集；其二，该首段所指的顶层成员本身在仓内不存在。
    返回 (ok, detail)。声明只对 kind = planned-location 的项成立；路径形态之外的引用不构成规定落点。"""
    if limit is None or limit.get("kind") != "planned-location":
        return False, "no planned-location declaration"
    if "/" not in token:
        return False, "planned-location applies to repository-relative paths only"
    first = token.split("/", 1)[0]
    in_closure = first in closure["dirs"] or first in closure["files"]
    top_exists = any(f == first or f.startswith(first + "/") for f in files)
    if not in_closure:
        return False, "first segment %r is not a root-closure member" % first
    if top_exists:
        return False, "top-level member %r already exists; a missing path beneath it is an ordinary missing reference" % first
    return True, "first segment in the root closure; top-level member absent"



def resolve_references(repo_root, candidates, references_rel, evidence_limits, closure):
    """逐候选、逐条解析（Amendment 2 多候选）。candidates = [(candidate_rel, candidate_text), ...]，次序即清单候选次序。
    返回 (results, failure)。failure = (code, detail) 或 None（首个命中的拒绝）。results 每项 {candidate, token, form, domain,
    revision, current_revision, status, path, reason, out_of_domain}，status ∈ delivered | exempt | planned | planned-location |
    directory | unresolved | ambiguous | missing | self | co-candidate。域一引用只核路径恰一命中；其 current_revision 为该件的
    现行修订号标注（不可得为 None），不构成拒绝判据。"""
    files = _repo_files(repo_root)
    if files is None:
        raise base.PreflightError("internal-error", "git ls-files failed while resolving references")
    by_name = {}
    for f in files:
        by_name.setdefault(f.rsplit("/", 1)[-1], []).append(f)
    spec_chain_names = frozenset(f.rsplit("/", 1)[-1] for f in files
                                 if f.startswith(C.SPEC_CHAIN_DIR + "/") and f.count("/") == 1)
    limits = {}
    for lim in evidence_limits:
        limits[lim["reference"]] = {"reason": lim["reason"], "kind": lim.get("kind", C.EVIDENCE_LIMIT_KIND_DEFAULT)}
    delivered = set(references_rel)
    historical_refs = {p for p in references_rel if not os.path.lexists(os.path.join(repo_root,p)) and H.read(repo_root,p) is not None}
    candidate_set = [c for c, _t in candidates]
    results = []
    failure = None
    revision_cache = {}

    def annotate_revision(target_rel):
        if target_rel not in revision_cache:
            revision_cache[target_rel] = current_revision(repo_root, target_rel)
        return revision_cache[target_rel]

    for candidate_rel, candidate_text in candidates:
        refs = extract_candidate_references(candidate_text, closure)
        unit_dir = candidate_rel.rsplit("/", 1)[0] if "/" in candidate_rel else ""
        cand_base = candidate_rel.rsplit("/", 1)[-1]
        for ref in refs:
            tok = ref["token"]
            entry = {"candidate": candidate_rel, "token": tok, "form": ref["form"], "domain": None, "revision": ref["revision"],
                     "current_revision": None, "status": None, "path": None, "reason": None, "out_of_domain": []}
            lim = limits.get(tok)
            if ref["form"] == "path":
                abspath = os.path.join(repo_root, tok)
                if tok == candidate_rel:
                    entry["status"] = "self"
                elif tok in candidate_set:
                    entry["status"] = "co-candidate"
                    entry["path"] = tok
                elif os.path.isdir(abspath):
                    entry["status"] = "directory"
                    entry["path"] = tok
                elif os.path.isfile(abspath) or tok in historical_refs:
                    entry["path"] = tok
                    entry["status"] = "delivered" if tok in delivered else "missing"
                else:
                    # 计划交付文件（r1 既有规则）：比较取完整单元目录加 "/" 为逐字节前缀，不取首段；候选位于仓根时不适用（R29-B3）
                    if unit_dir and tok.startswith(unit_dir + "/"):
                        entry["status"] = "planned"
                    else:
                        ok, detail = planned_location_check(tok, lim, files, closure)
                        if ok:
                            entry["status"] = "planned-location"
                            entry["reason"] = lim["reason"] if lim else None
                        elif lim is not None and lim["kind"] == "planned-location":
                            entry["status"] = "unresolved"
                            entry["reason"] = "planned-location declaration invalid: " + detail
                        elif lim is not None and lim["kind"] == "exemption":
                            entry["status"] = "exempt"
                            entry["reason"] = lim["reason"]
                        else:
                            entry["status"] = "unresolved"
            else:
                domain = reference_domain(ref, spec_chain_names)
                entry["domain"] = domain
                hits = domain_match_set(domain, tok, files, by_name)
                all_hits = by_name.get(tok, [])
                entry["out_of_domain"] = sorted(h for h in all_hits if h not in hits)
                if tok == cand_base:
                    hits = [h for h in hits if h != candidate_rel]
                    if not hits and candidate_rel in all_hits:
                        entry["status"] = "self"
                if entry["status"] is None and len(hits) == 1 and hits[0] in candidate_set:
                    entry["status"] = "co-candidate"
                    entry["path"] = hits[0]
                if entry["status"] is None:
                    if len(hits) == 0:
                        # 存在性判定恒相对选定域（R1-B1）：域外同名件只进诊断，不改变结论；豁免只对仓外对象成立
                        if lim is not None and lim["kind"] == "exemption":
                            entry["status"] = "exempt"
                            entry["reason"] = lim["reason"]
                        else:
                            entry["status"] = "unresolved"
                    elif len(hits) > 1:
                        # 域内多命中恒拒绝，不以 inputs.references 消解（R1-B2）
                        entry["status"] = "ambiguous"
                        entry["path"] = sorted(hits)
                    else:
                        entry["path"] = hits[0]
                        if domain == "one":
                            # 域一只标注现行修订号（freeze-record 设计 §4.3），引用修订号是否过时由评审方判断；不判失败码
                            entry["current_revision"] = annotate_revision(hits[0])
                        entry["status"] = "delivered" if hits[0] in delivered else "missing"
            results.append(entry)
            if failure is None:
                if entry["status"] == "unresolved":
                    extra = ""
                    if entry["out_of_domain"]:
                        extra = " (same-named files outside the selected domain, not counted: %s)" % ", ".join(entry["out_of_domain"])
                    if entry["reason"]:
                        extra += " (%s)" % entry["reason"]
                    failure = ("reference-unresolved", "reference %r not found%s%s"
                               % (tok, (" in domain %s" % entry["domain"]) if entry["domain"] else " in the repository", extra))
                elif entry["status"] == "ambiguous":
                    failure = ("reference-ambiguous", "bare filename %r matches %d files in domain %s"
                               % (tok, len(entry["path"]), entry["domain"]))
                elif entry["status"] == "missing":
                    failure = ("reference-missing", "reference %r exists (%s) but is not in inputs.references"
                               % (tok, entry["path"]))
    return results, failure


# ---------------------------------------------------------------- 投递名派生（§6.2 第 4 条，Amendment 1）

def baseline_bundle_name(previous_bundle_name):
    """每候选一件基线的固定投递名（§6.2 第 6 条，Amendment 2）：previous--candidate-baseline--<该候选上一轮投递名>。"""
    return C.PREVIOUS_PREFIX + "candidate-baseline--" + previous_bundle_name


def previous_bundle_names(previous_manifest):
    """r2+ 本束实际生成的 previous-- 族固定投递名（第 6 条：3 + 候选数件，Amendment 2），供派生预占。"""
    cands = [i for i in previous_manifest.get("inputs", []) if i.get("role") == "candidate"]
    return ([baseline_bundle_name(c["bundle_name"]) for c in cands]
            + [C.PREVIOUS_PREFIX + "verdict.md", C.PREVIOUS_PREFIX + C.MANIFEST_NAME, C.PREVIOUS_PREFIX + "receipt.json"])


def derive_bundle_names(sources, fixed_names):
    """整束一次性全局唯一派生。sources = 来源路径列表（候选与参考件）；fixed_names = 已占用的固定名（任务书、previous-- 族）。
    返回 {source: bundle_name}。次序固定：先预占固定名，其余按来源路径字典序逐件派生——初值取基名，命中禁名表改 source--<原名>，
    与已占用集合冲突或（非禁名表生成的）名字以保留前缀起始时，自末段起逐段向前扩展路径段前缀（段间 `--`），取至不冲突；
    路径段取尽仍冲突即 bundle-name-collision。派生之前先核对来源路径两两不等（duplicate-reference-source）。"""
    seen = set()
    for s in sources:
        if s in seen:
            raise base.PreflightError("duplicate-reference-source", "source path %r listed more than once" % s)
        seen.add(s)
    occupied = set(fixed_names)
    out = {}
    for src in sorted(sources, key=lambda s: s.encode("utf-8")):
        segs = src.split("/")
        name = segs[-1]
        generated = False
        if name in C.FORBIDDEN_BUNDLE_NAMES:
            name = C.RENAME_PREFIX + name
            generated = True
        k = 1
        while name in occupied or (not generated and name.startswith(C.RESERVED_BUNDLE_PREFIXES)):
            k += 1
            if k > len(segs):
                raise base.PreflightError("bundle-name-collision",
                                          "no bundle name distinguishes %r after exhausting its path segments (last tried %r)"
                                          % (src, name))
            name = "--".join(segs[-k:])
            generated = False
        occupied.add(name)
        out[src] = name
    return out


def subject_title(subject):
    parts = subject.split("-")
    if len(parts) > 1 and parts[-1] == "task":
        parts = parts[:-1]
    return "_".join(p[:1].upper() + p[1:] for p in parts if p)


def taskbook_filename(subject, rnd):
    return "HarnessPlane_%s_Review_Task_R%s.md" % (subject_title(subject), rnd[1:])


def verdict_filename(subject, rnd):
    return "HarnessPlane_%s_Review_R%s.md" % (subject_title(subject), rnd[1:])


def read_repo_file(repo_root, rel, role, history_sources=None):
    """仓界前置的唯一读取原语（R2-B1 / R3-B1 / R4-B1 整改）：任何被审输入字节都只经此读取。
    自仓根目录描述符起逐段以 O_NOFOLLOW | O_DIRECTORY 打开祖先目录、最后一段以 O_NOFOLLOW 打开文件，每一段的检查与打开都绑定于
    同一次 openat（不存在「先按路径检查、再按路径重开」的窗口）；按打开所得描述符 fstat（须常规文件）与读取；realpath 越出仓根
    另作事实核对。返回字节。"""
    code = "candidate-unreadable" if role == "candidate" else "reference-unreadable"
    if role != 'candidate' and not os.path.lexists(os.path.join(repo_root, rel)):
        item = H.read(repo_root, rel)
        if item is not None:
            if history_sources is not None:
                history_sources.append({k:v for k,v in item.items() if k != 'content'})
            return item['content']
    parts = rel.split("/")
    real = os.path.realpath(os.path.join(repo_root, rel))
    root_real = os.path.realpath(repo_root)
    if real != root_real and not real.startswith(root_real + os.sep):
        raise base.PreflightError(code, "%s %r resolves outside the repository" % (role, rel))
    nofollow = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fds = []
    try:
        try:
            fds.append(os.open(repo_root, nofollow | os.O_DIRECTORY))
            for seg in parts[:-1]:
                fds.append(os.open(seg, nofollow | os.O_DIRECTORY, dir_fd=fds[-1]))
            fd = os.open(parts[-1], nofollow, dir_fd=fds[-1])
            fds.append(fd)
        except OSError as exc:
            raise base.PreflightError(code, "%s %r is not a readable file through the repository boundary (%s)"
                                      % (role, rel, type(exc).__name__))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise base.PreflightError(code, "%s %r is not a regular file" % (role, rel))
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError as exc:
            raise base.PreflightError(code, "%s %r could not be read (%s)" % (role, rel, type(exc).__name__))
    finally:
        for f in fds:
            os.close(f)
    return b"".join(chunks)


def reject_secret(data, name, scan_set):
    """§7.5：秘密值与端点值绝不进入被审输入——写入封存目录之前核对，命中即 secret-in-input（R5-B9 / R6-B3）。"""
    if scan_set is not None and base.contains_secret(data, scan_set):
        raise base.PreflightError("secret-in-input", "input %r contains resolved secret or endpoint material; the bundle is not delivered" % name)


def seal_inputs(repo_root, attempt_dir, candidates_rel, references_rel, candidate_bytes=None, scan_set=None, fixed_names=(), history_sources=None):
    """封存：复制、cmp、置只读；返回 (seal_dir, pre_manifest)。candidates_rel = 候选路径列表（Amendment 2 多候选，
    次序即清单候选次序；单一字符串按一件处理）。投递名按 derive_bundle_names 整束一次性派生（fixed_names = 任务书名与本束
    实际生成的 previous-- 族固定名，预占）。`candidate_bytes` = 引用解析所用的候选字节（R4-B1 整改；{候选路径: 字节} 或单候选
    的字节）：封存读取的候选须与之逐字节相等，否则 seal-mismatch——所验证（引用完备性）字节即所投递字节。"""
    if isinstance(candidates_rel, str):
        candidates_rel = [candidates_rel]
    candidates_rel = list(candidates_rel)
    if isinstance(candidate_bytes, (bytes, bytearray)):
        candidate_bytes = {candidates_rel[0]: bytes(candidate_bytes)}
    names = derive_bundle_names(candidates_rel + list(references_rel), fixed_names)
    seal_dir = os.path.join(attempt_dir, INPUTS_SUBDIR)
    os.makedirs(seal_dir, exist_ok=False)
    pre = []
    items = [(c, "candidate") for c in candidates_rel] + [(r, "reference") for r in references_rel]
    for rel, role in items:
        name = names[rel]
        dst = os.path.join(seal_dir, name)
        data = read_repo_file(repo_root, rel, role, history_sources)
        if role == "candidate" and candidate_bytes is not None and rel in candidate_bytes and data != candidate_bytes[rel]:
            raise base.PreflightError("seal-mismatch", "candidate %r changed between reference resolution and sealing" % rel)
        reject_secret(data, rel, scan_set)
        base.write_new(dst, data)
        # cmp 也经仓界前置读取（不按路径重新打开源文件，R3-B1）
        if read_repo_file(repo_root, rel, role) != data:
            raise base.PreflightError("seal-mismatch", "source %r changed during sealing" % rel)
        os.chmod(dst, 0o444)
        pre.append({"source": rel, "bundle_name": name, "bytes": len(data), "sha256": base.sha256_bytes(data),
                    "role": role})
    return seal_dir, pre


def add_sealed_bytes(seal_dir, pre, bundle_name, data, role, source, scan_set=None):
    """把通道生成或从历史取得的字节封存为一件（任务书、previous-- 四件）；写入前经秘密扫描。"""
    if any(p["bundle_name"] == bundle_name for p in pre):
        raise base.PreflightError("bundle-name-collision", "bundle name %r already used" % bundle_name)
    reject_secret(data, bundle_name, scan_set)
    dst = os.path.join(seal_dir, bundle_name)
    base.write_new(dst, data)
    os.chmod(dst, 0o444)
    entry = {"source": source, "bundle_name": bundle_name, "bytes": len(data), "sha256": base.sha256_bytes(data),
             "role": role}
    pre.append(entry)
    return entry


def manifest_sha256(inputs):
    return base.sha256_bytes(base.canonical_json(inputs))


def manifest_document(subject, stage, rnd, task_file, inputs):
    inputs = [dict(i) for i in inputs]
    return {"subject": subject, "stage": stage, "round": rnd, "task_file": task_file, "inputs": inputs,
            "manifest_sha256": manifest_sha256(inputs)}


def total_bytes(inputs):
    return sum(i["bytes"] for i in inputs)


# ---------------------------------------------------------------- r2+ 上一轮闭合锚

def recover_baseline(repo_root, source_rel, sha256, previous_attempt_round_dir, history_sources=None):
    """上一轮 candidate 基线：优先 attempts 封存副本，缺失时从 Git 历史取；皆无返回 None。"""
    if previous_attempt_round_dir and os.path.isdir(previous_attempt_round_dir):
        for entry in sorted(os.scandir(previous_attempt_round_dir), key=lambda e: e.name):
            if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                continue
            sd = os.path.join(entry.path, INPUTS_SUBDIR)
            if not os.path.isdir(sd):
                continue
            for f in os.scandir(sd):
                if f.is_file(follow_symlinks=False):
                    data = base.read_bytes(f.path)
                    if base.sha256_bytes(data) == sha256:
                        return data
    h = H.helper(); view = h.head_commit(repo_root)
    if view:
        import review_evidence as E
        try:
            archived=E.recover_snapshot(repo_root,sha256,history_sources)
            if archived is not None:return archived
        except E.EvidenceError as exc:raise base.PreflightError(exc.code,exc.message) from exc
        try:
            g = h.GitHistory(repo_root, view)
            for commit in g.git('log', '--format=%H', view, '--', source_rel).decode().split():
                if source_rel not in g.tree(commit):
                    continue
                item = h.read_history(repo_root, view, source_rel, commit)
                if item['sha256'] == sha256:
                    if history_sources is not None:
                        history_sources.append({k:v for k,v in item.items() if k != 'content'})
                    return item['content']
        except h.HistoryReadError as exc:
            raise base.PreflightError('history-unavailable', exc.code+': '+exc.message)
    return None


def add_previous_round_inputs(repo_root, seal_dir, pre, previous, previous_attempt_round_dir, scan_set=None,
                              current_candidate_sources=None, history_sources=None):
    """previous = {verdict_bytes, verdict_path, receipt_bytes, receipt_path, manifest_bytes, manifest_path,
    manifest(dict)}。返回 {候选来源路径: baseline_bytes}（Amendment 2 逐候选基线）。缺基线即 baseline-unrecoverable；
    本轮候选来源路径集合与上一轮清单候选集合不等即 inherit-unanchored（§6.2 第 6 条）。"""
    man = previous["manifest"]
    cands = [i for i in man["inputs"] if i["role"] == "candidate"]
    if not cands:
        raise base.PreflightError("inherit-unanchored", "previous manifest lacks a candidate")
    if current_candidate_sources is None:
        current_candidate_sources = [p["source"] for p in pre if p["role"] == "candidate"]
    if set(c["source"] for c in cands) != set(current_candidate_sources) or len(cands) != len(set(c["source"] for c in cands)):
        raise base.PreflightError("inherit-unanchored",
                                  "candidate set changed since the previous round (previous %s, current %s); the candidate set is "
                                  "fixed within one round sequence - a changed set starts a new axis"
                                  % (sorted(c["source"] for c in cands), sorted(current_candidate_sources)))
    baselines = {}
    for cand in cands:
        baseline = previous.get('sealed_inputs',{}).get(cand['sha256'])
        if baseline is None:
            baseline = recover_baseline(repo_root, cand["source"], cand["sha256"], previous_attempt_round_dir, history_sources)
        if baseline is None:
            raise base.PreflightError("baseline-unrecoverable",
                                      "previous candidate bytes (%s) recoverable neither from attempts nor Git history"
                                      % cand["sha256"][:12])
        add_sealed_bytes(seal_dir, pre, baseline_bundle_name(cand["bundle_name"]), baseline, "reference",
                         "%s@%s" % (cand["source"], cand["sha256"][:12]), scan_set=scan_set)
        baselines[cand["source"]] = baseline
    add_sealed_bytes(seal_dir, pre, C.PREVIOUS_PREFIX + "verdict.md", previous["verdict_bytes"], "reference",
                     previous["verdict_path"], scan_set=scan_set)
    add_sealed_bytes(seal_dir, pre, C.PREVIOUS_PREFIX + C.MANIFEST_NAME, previous["manifest_bytes"], "reference",
                     previous["manifest_path"], scan_set=scan_set)
    add_sealed_bytes(seal_dir, pre, C.PREVIOUS_PREFIX + "receipt.json", previous["receipt_bytes"], "reference",
                     previous["receipt_path"], scan_set=scan_set)
    return baselines
