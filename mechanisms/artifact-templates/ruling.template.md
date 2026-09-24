> Ruling: RU-<NN>
> Date: YYYY-MM-DD
> Type: <finalization | review-skip | finding-disposition | close | done>
> Object: <object identity>
> Basis: <durable references>
> Decision: <Owner decision>

<!-- template:guide
落点与文件名：`tasks/<task-record-id>/rulings/RU-<NN>-<slug>.md`；NN 自 01 起严格递增、不回收，99 后写 100；slug 文法 `[a-z0-9]+(?:-[a-z0-9]+)*`。
页首六行自首字节起逐行固定、顺序固定、值非空；第七行为空行，其后正文自由（通常一节「沿革」）。
裁定件追加后不可编辑；事实更正由更高编号的裁定件显式 supersede，旧件保留。
CL-55 候选：archive-v1 切换后，Basis 指本件 #review-result（完整评审引用）或既有 review-skip；页首六行保持。最小 review-result/v1 块文法唯一归评审通道 §8.6.5，放本件正文，保留最终结果、候选身份及当前保留项；不得放完整判词/Receipt/清单或逐轮过程。旧裁定的 Git 原路径继续按固定来源读，不倒写旧件。
Type 闭集与各自最低内容：
- finalization：Task Definition 候选定稿 / 再定稿。Object = Definition 路径 · bytes · SHA-256；Basis = 终轮判词（或先行 review-skip 裁定件）；再定稿另指上一次 finalization 与变更依据。
- review-skip：对一个精确候选显式关闭默认评审。Object = 同一候选身份；Basis = 关闭依据与 Owner 授权。
- finding-disposition：Owner 裁定 HUMAN 或非等价 finding。Object = finding ID 与所涉候选；Basis = 判词。
- close：任务终止。Object = task record ID；Basis = 终止依据（Owner 指令原话、已入库产物、评审证据）。
- done：里程碑任务验收完成的终态裁定。Object = task record ID；Basis = 验收证据、定稿 Definition、必要的 commit / MR。
Decision 逐字承载 Owner 指令原话与边界（本件不授权的后续动作一并点明）。
实例化时删除全部引导块；核对命令 = `python3 mechanisms/artifact-templates/artifact_lint.py ruling <路径>`。
-->
