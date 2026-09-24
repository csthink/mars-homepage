"""review_channel_instruction — 评审方指令注入：判词契约文本 + 五题闭集（设计 §6.4；与判词模块同源常量）。

指令形态：review（首次调用）、reemit（信封类补发）、validation reemit（校验类补发，Amendment 2）、probe。
inline 投递（直接接口）另附单一输出模型要求（§7.4）。叙述约定以逐行填空的叙述骨架注入（§6.4 同源渲染，Amendment 2）。
恒附加、不设豁免通道；判词格式契约只由本模块承载，不出现在任务书。
本模块不导入其他契约模块。
"""
import review_channel_contract as C


def member_sequence(tree):
    """成员名序列（§6.4「嵌套必填成员」形态）：`{a, b, c{d, e}}`，递归至 §6.5.1 声明的最深层。"""
    parts = []
    for name, sub in tree.items():
        parts.append(name + (member_sequence(sub) if sub else ""))
    return "{" + ", ".join(parts) + "}"


def _field(name, text):
    """字段行：以 `- ` 起始、其后至首个 `:` 为字段名位（§6.4 机械判据）。"""
    return "- %s: %s" % (name, text)


def verdict_contract_text(required_question_ids, round_tag, task_file, previous_finding_ids, residual_ids, delivery):
    """§6.5.2 第 9 / 10 条的允许集合 = 投递名集合（锁值引用件路径随减重第 3 步退役）。"""
    tree = C.VERDICT_MEMBER_TREE
    lines = []
    lines.append("## 判词契约（通道注入，机械校验）")
    lines.append("")
    lines.append("你的最终消息即判词。判词以机读块开头，叙述正文在后。机读块 = 恰一个围栏代码块：")
    lines.append("")
    lines.append("```" + C.FENCE_INFO)
    lines.append("{ ...一个 JSON 对象，字段如下... }")
    lines.append("```")
    lines.append("")
    lines.append("机读块字段（每个字段一行；花括号内为该字段的必填成员名序列，逐层完整写出）：")
    lines.append(_field("verdict_schema", "固定 \"%s\"。" % C.VERDICT_SCHEMA))
    lines.append(_field("verdict", "\"PASS\" | \"FAIL\"。PASS ⇔ 无 severity=blocking 的 finding；FAIL ⇔ 至少一条 blocking。"))
    lines.append(_field("human_decision_required", "布尔；恒等于「存在 severity=human 的 finding」。"))
    lines.append(_field("subject", "与任务书状态头逐字相等。"))
    lines.append(_field("stage", "与任务书状态头逐字相等。"))
    lines.append(_field("round", "与任务书状态头逐字相等（round = \"%s\"）。" % round_tag))
    lines.append(_field("candidate", "%s，首候选（任务书第 2 节首行）的身份，须与投递材料表相等；恒等于 candidates[0]。" % member_sequence(tree["candidate"])))
    lines.append(_field("candidates", "数组，每件 %s：全部被审对象的身份，须与投递材料表 role 为 candidate 的各行精确等集、次序同第 2 节。" % member_sequence(tree["candidates"])))
    lines.append(_field("question_assessments", "数组，每题恰一条 %s；assessment 取 %s；evidence_refs 为字符串数组、finding_ids 为 finding id 数组。"
                        "题号集合必须恰为本 stage 的五题闭集：%s（每题一条，不多不少；缺题、多题或重复即判词无效、不补发）。"
                        "非 SATISFIED 的题必须引用至少一条 finding；PASS 要求每题 SATISFIED，或 INDETERMINATE 且只引用 severity=human 的 finding。"
                        % (member_sequence(tree["question_assessments"]), " | ".join(C.ASSESSMENTS), ", ".join(required_question_ids))))
    lines.append(_field("findings", "数组，每条 %s；severity 取 %s；origin 取 %s。"
                        "id 文法 = %s-B<n> / %s-N<n> / %s-H<n>（B = blocking、N = non_blocking、H = human），三类各自从 1 连续编号；"
                        "location.file 须是投递名之一；每条 finding 至少被一题引用；引用的 finding 必须已声明。"
                        "任务书第 3 节改动区（差异块）之外的发现 origin 取 unchanged_region；origin = unchanged_region 的 finding 其 severity "
                        "不得为 blocking（规则 14），只能取 non_blocking 或 human。"
                        % (member_sequence(tree["findings"]), " | ".join(C.SEVERITIES), " | ".join(C.ORIGINS),
                           round_tag.upper(), round_tag.upper(), round_tag.upper())))
    if previous_finding_ids is not None:
        lines.append(_field("previous_findings_disposition", "数组，上一轮每条 finding 恰一条 %s；disposition 取 %s；id 集合必须恰为：%s。"
                            % (member_sequence(tree["previous_findings_disposition"]), " | ".join(C.DISPOSITIONS),
                               ", ".join(previous_finding_ids) or "（空）")))
    else:
        lines.append(_field("previous_findings_disposition", "数组，成员形态 %s；首轮取空数组。"
                            % member_sequence(tree["previous_findings_disposition"])))
    lines.append(_field("accepted_residuals_acknowledged", "字符串数组，必须恰为任务书「已接受残留」的 id 集合：%s；只确认不重报。"
                        % (", ".join(residual_ids) if residual_ids else "（空）")))
    lines.append(_field("scope_files_read", "字符串数组，你读过的投递名子集，必须含任务书 \"%s\"。" % task_file))
    if delivery == "inline":
        lines.append(_field("tools_used", "必须为空数组（内联投递，你没有文件工具）。"))
    else:
        lines.append(_field("tools_used", "你使用过的工具名，非空数组。"))
    lines.append(_field("authorization_disclaimer", "固定 true（判词不构成任何批准、Freeze、commit 或 push 的权威）。"))
    lines.append("")
    lines.append("叙述正文（机读块之后）的机械可恢复约定，逐条校验、与机读块不一致即判词无效：")
    lines.append("- 正文首行 `**Verdict: PASS|FAIL**`，第二行 `**Human decision required: yes|no**`（物理第 1、2 行，之间与之前无空行）。")
    lines.append("- 每条 finding 一个标题级小节，标题形态 `### <id> · <severity> · <title>`（severity 取 blocking / non_blocking / human；分隔符为半角空格加 `·` 加半角空格）。")
    lines.append("- 一个 `## Question assessments` 节，每题一行 `- <question_id>: SATISFIED|NOT_SATISFIED|INDETERMINATE`（半角冒号）。")
    if previous_finding_ids is not None:
        lines.append("- 一个 `## Previous findings` 节，每条一行 `- <prev-id>: RESOLVED|UNRESOLVED|INDETERMINATE`。")
    lines.append("- 正文不得含 raw HTML（任何以 `<` 后接字母、`!`、`/` 或 `?` 开头的行）。")
    lines.append("")
    lines.append(SKELETON_INTRO)
    lines.append("")
    lines.extend(narrative_skeleton_lines(required_question_ids, round_tag, previous_finding_ids))
    lines.append("")
    if delivery == "inline":
        lines.append("单一输出模型（内联投递）：你的最终消息必须恰为一个 JSON 对象 {\"machine\": <上述机读块对象>, "
                     "\"narrative\": <满足上述约定的 Markdown 叙述正文字符串>}，不要再加围栏、不要输出其他文本。")
        lines.append("")
    return "\n".join(lines)


SKELETON_INTRO = ("叙述骨架（逐行填空；行序、节名、标点逐字保持，只把尖括号内的取值位换成实际值；"
                  "每条 finding 各复制一次标题模板行并在其下写叙述；不要输出尖括号本身）：")


def narrative_skeleton_lines(required_question_ids, round_tag, previous_finding_ids):
    """注入用叙述骨架（§6.4 骨架行判据的正例来源）：以占位值渲染 C.narrative_skeleton。"""
    values = {"verdict": None, "human": None, "findings": None,
              "questions": [(q, None) for q in required_question_ids],
              "previous": None if previous_finding_ids is None else [(p, None) for p in previous_finding_ids],
              "round_tag": round_tag}
    return C.narrative_skeleton(values)


def skeleton_line_problems(text, required_question_ids, round_tag, previous_finding_ids):
    """§6.4「骨架行判据」（Amendment 2）：注入文本须逐字含骨架各行——两条首行、模板行、节名行、每个 required question id
    与上一轮 finding id 各恰一行；缺失或重复即判红。返回 problems（空 = 通过）。"""
    problems = []
    lines = [ln.rstrip() for ln in text.split("\n")]
    expected = [ln for ln in narrative_skeleton_lines(required_question_ids, round_tag, previous_finding_ids)
                if ln and ln != "<本条 finding 的叙述>"]
    for ln in expected:
        n = lines.count(ln)
        if n != 1:
            problems.append("skeleton line %r appears %d times (expected exactly one)" % (ln, n))
    return problems


def _parse_member_sequence(text):
    """取行内首个由 `{` `}` 界定、`,` 分隔、可递归的成员名序列；返回 {name: subtree|None}，无序列返回 None，不平衡返回 False。"""
    i = text.find("{")
    if i < 0:
        return None

    def parse(pos):
        tree = {}
        name = ""
        while pos < len(text):
            ch = text[pos]
            if ch == "{":
                sub, pos = parse(pos + 1)
                if sub is False:
                    return False, pos
                tree[name.strip()] = sub
                name = ""
                continue
            if ch == ",":
                if name.strip():
                    tree[name.strip()] = None
                name = ""
            elif ch == "}":
                if name.strip():
                    tree[name.strip()] = None
                return tree, pos + 1
            else:
                name += ch
            pos += 1
        return False, pos

    tree, _ = parse(i + 1)
    return tree


def field_line_problems(text):
    """§6.4「全部字段」机械判据（Amendment 1）：字段行 = 以 `- ` 起始、其后至首个 `:` 之间为字段名位的行；
    §6.5.1 每个字段各恰一条字段行（subject / stage / round 各占一条；previous_findings_disposition 在 r1 与 r2+ 均在场）；
    带嵌套必填成员的字段，其成员名序列逐层恰等于 C.VERDICT_MEMBER_TREE。返回 problems 列表（空 = 通过）。"""
    problems = []
    counts = {}
    seqs = {}
    for line in text.split("\n"):
        if not line.startswith("- ") or ":" not in line:
            continue
        name = line[2:].split(":", 1)[0].strip()
        if name in C.VERDICT_FIELDS:
            counts[name] = counts.get(name, 0) + 1
            seqs[name] = line[2:].split(":", 1)[1]
    for f in C.VERDICT_FIELDS:
        n = counts.get(f, 0)
        if n != 1:
            problems.append("field line for %r appears %d times (expected exactly one)" % (f, n))

    def compare(path, want, got):
        if got is False:
            problems.append("%s: unbalanced member sequence" % path)
            return
        if got is None:
            problems.append("%s: member sequence missing" % path)
            return
        if set(got.keys()) != set(want.keys()):
            problems.append("%s: member set %s != declared %s" % (path, sorted(got.keys()), sorted(want.keys())))
            return
        for k, sub in want.items():
            if sub:
                compare(path + "." + k, sub, got.get(k) if got.get(k) is not None else None)

    for f, tree in C.VERDICT_MEMBER_TREE.items():
        if counts.get(f, 0) == 1:
            compare(f, tree, _parse_member_sequence(seqs[f]))
    return problems


def review_instruction(ctx):
    """ctx: task_file, stage, required_question_ids, round_tag, previous_finding_ids (None | list), residual_ids,
    delivery ('tool' | 'inline')"""
    lines = []
    lines.append("# 评审指令（通道注入）")
    lines.append("")
    lines.append("1. 先读任务书 `%s`，再按其投递材料表阅读被审对象与参考件。被审输入即全部输入；只读工作，不写任何文件，不访问网络。"
                 "评审面默认是任务书第 3 节列出的改动区（差异块）；差异块之外的发现 origin 取 unchanged_region，且不得为 blocking。"
                 % ctx["task_file"])
    lines.append("2. 你的最终消息即判词；按下列契约输出，机读块在前、叙述正文在后。")
    lines.append("3. 本轮问题集（%s 轮五题闭集，与任务书第 8 节相同；题面由通道常量渲染，对题面本身不开 finding）：" % ctx["stage"])
    for qid, text in C.QUESTIONS_BY_STAGE[ctx["stage"]]:
        lines.append("   - %s：%s" % (qid, text))
    lines.append("")
    lines.append(verdict_contract_text(ctx["required_question_ids"], ctx["round_tag"], ctx["task_file"],
                                       ctx["previous_finding_ids"], ctx["residual_ids"], ctx["delivery"]))
    return "\n".join(lines)


def validation_reemit_instruction(original_message, known_values, skeleton_lines, task_file):
    """校验类补发（§6.5.4，Amendment 2）：已知值字段正确取值清单 + 以原机读块实际值渲染的叙述骨架 + 重发完整判词的指令；
    不重读被审输入。known_values = [(字段名, JSON 字面)]。"""
    lines = []
    lines.append("你上一条最终消息的机读块可解析，但校验失败只落在通道已知值字段或叙述排版上。请重发**完整判词**（机读块 + 叙述正文）：")
    lines.append("- 机读块内的判断字段（verdict、human_decision_required、question_assessments、findings、previous_findings_disposition、"
                 "scope_files_read、tools_used）逐字保持原值，不得改动任何判断，不得新增或删除 finding；")
    lines.append("- 下列已知值字段按所给值填写（JSON 字面）：")
    for name, literal in known_values:
        lines.append("  - %s: %s" % (name, literal))
    lines.append("- 叙述正文按下列骨架逐行输出（行序、节名、标点逐字保持），只在各 finding 标题之下填入你原叙述中该 finding 的内容；"
                 "正文不得含 raw HTML；不要输出尖括号本身。")
    lines.append("- 以一个 ```%s 围栏代码块开头，内含完整、可解析的 JSON 对象；围栏块之后一个空行，随后是叙述正文。" % C.FENCE_INFO)
    lines.append("")
    lines.append("--- 叙述骨架如下 ---")
    lines.extend(skeleton_lines)
    lines.append("")
    lines.append("--- 你的原始最终消息如下 ---")
    lines.append(original_message)
    return "\n".join(lines)


def reemit_instruction(original_message):
    """信封类补发调用（§6.5.4）：输入只有评审方自己的原始最终消息与固定指令。"""
    return ("你上一条最终消息的机读块无法被机械解析。请原样重发机读块：以一个 ```%s 围栏代码块开头，内含完整、可解析的 JSON 对象；"
            "不得改动任何判断，不得新增或删除 finding，不得改变任何 assessment、severity、title、disposition。"
            "围栏块之后不要再输出叙述正文。\n\n--- 你的原始最终消息如下 ---\n%s"
            % (C.FENCE_INFO, original_message))


def probe_instruction():
    return ("这是一次通道探针。不要读取任何文件。你的最终消息必须恰为一行文本：%s（不加任何其他字符、标点、围栏或说明）。"
            % C.PROBE_PAYLOAD)
