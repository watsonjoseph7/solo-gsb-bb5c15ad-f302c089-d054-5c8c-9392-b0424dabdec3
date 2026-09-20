"""审计分析：逐项判定并汇总。"""
from __future__ import annotations

from .rfc1982 import (
    REL_ADVANCE,
    REL_EQUAL,
    REL_INCOMPARABLE,
    REL_INITIAL,
    REL_REGRESSION,
    SLAVE_REJECT_RELATIONS,
    classify,
    signed_delta,
)

_NOTES = {
    REL_INITIAL: "首个观测值，作为后续比较基准",
    REL_ADVANCE: "按 RFC 1982 为有效前进，从服务器可正常接受更新",
    REL_EQUAL: "与上一观测值相等，从服务器不会触发区域传输",
    REL_REGRESSION: "序列号倒退，从服务器将认为本端数据更旧而拒绝更新",
    REL_INCOMPARABLE: "与上一观测值相距恰好 2^31，RFC 1982 下不可比较，从服务器行为不确定",
}


def analyze(serials: list[int]) -> tuple[list[dict], dict]:
    """对按观测时间排列的序列号逐项判定，返回 (items, summary)。"""
    items: list[dict] = []
    counts = {REL_ADVANCE: 0, REL_EQUAL: 0, REL_REGRESSION: 0, REL_INCOMPARABLE: 0}
    risk_indexes: list[int] = []

    for index, serial in enumerate(serials):
        if index == 0:
            relation = REL_INITIAL
            delta = None
        else:
            relation = classify(serials[index - 1], serial)
            delta = signed_delta(serials[index - 1], serial)
            counts[relation] += 1

        risk = relation in SLAVE_REJECT_RELATIONS
        if risk:
            risk_indexes.append(index)

        items.append(
            {
                "index": index,
                "serial": serial,
                "relation": relation,
                "delta": delta,
                "slave_reject_risk": risk,
                "note": _NOTES[relation],
            }
        )

    summary = {
        "total": len(serials),
        "advance": counts[REL_ADVANCE],
        "equal": counts[REL_EQUAL],
        "regression": counts[REL_REGRESSION],
        "incomparable": counts[REL_INCOMPARABLE],
        "risk_indexes": risk_indexes,
    }
    return items, summary
