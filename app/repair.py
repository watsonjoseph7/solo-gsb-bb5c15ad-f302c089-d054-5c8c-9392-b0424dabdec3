"""序列修复预演（不落库）。

首个 serial 保持不变；其后每项与上一个**修复后**值按 RFC 1982 比较：
有效前进（advance）则保留原值，相等/倒退/不可比较时替换为上一个修复后
值加 1，并按 32 位无符号整数（模 2^32）回绕。
"""
from __future__ import annotations

from .rfc1982 import (
    REL_ADVANCE,
    REL_EQUAL,
    REL_INCOMPARABLE,
    REL_REGRESSION,
    SERIAL_SPACE,
    classify,
)

_FIRST_REASON = "首个观测值保持不变，作为修复序列的比较基准"
_KEEP_REASON = "相对上一修复后值为 RFC 1982 有效前进，保留原值"
_REPLACE_REASONS = {
    REL_EQUAL: "与上一修复后值相等，未形成有效前进，替换为上一修复后值加 1",
    REL_REGRESSION: "相对上一修复后值倒退，从服务器将拒绝更新，替换为上一修复后值加 1",
    REL_INCOMPARABLE: "与上一修复后值相距恰好 2^31，RFC 1982 下不可比较，替换为上一修复后值加 1",
}


def build_repair_plan(serials: list[int]) -> tuple[list[dict], dict]:
    """对按观测时间排列的序列号生成修复预演，返回 (items, summary)。

    结果仅用于预演，不写回数据库。替换值恒为上一修复后值 +1
    （模 2^32），因此相邻修复后值之间一定是有效前进。
    """
    items: list[dict] = []
    replaced = 0
    prev_effective: int | None = None

    for index, serial in enumerate(serials):
        if index == 0:
            action = "keep"
            effective = serial
            reason = _FIRST_REASON
        else:
            relation = classify(prev_effective, serial)
            if relation == REL_ADVANCE:
                action = "keep"
                effective = serial
                reason = _KEEP_REASON
            else:
                action = "replace"
                effective = (prev_effective + 1) % SERIAL_SPACE
                reason = _REPLACE_REASONS[relation]
                replaced += 1

        items.append(
            {
                "index": index,
                "original": serial,
                "effective": effective,
                "action": action,
                "reason": reason,
            }
        )
        prev_effective = effective

    summary = {"total": len(serials), "replaced": replaced}
    return items, summary
