"""adapters/claude — claude 命令行适配器占位（评审通道设计 §7.3：形态待施工期探针，Owner 裁定后填入）。

本文件是设计 §5.1 名单成员；在 §7.3 候选 A 的探针证据在案并经 Owner 裁定采用之前，本适配器不支持任何
provider kind 或 transport（Registry 引用它即装载校验失败），`preflight` / `run` 恒以
`runtime-adapter-unmaterialized` fail closed。不得在此之前把它当作可用运行时。
"""
import review_channel_base as base

RUNTIME_ID = "claude"
SUPPORTED_KINDS = ()
SUPPORTED_TRANSPORTS = ()
INLINE_DELIVERY = False


def preflight(ctx):
    raise base.PreflightError("runtime-adapter-unmaterialized",
                              "claude adapter form awaits the §7.3 probe and Owner ruling")


def run(ctx):
    raise base.ChannelError("runtime-adapter-unmaterialized",
                            "claude adapter form awaits the §7.3 probe and Owner ruling")
