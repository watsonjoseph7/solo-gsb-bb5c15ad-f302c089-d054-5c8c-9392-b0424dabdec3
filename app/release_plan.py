"""多权威节点共同发布规划（不落库）。

给定一组权威节点当前各自持有的 SOA serial，在当日 YYYYMMDDnn
（nn = 00..99）候选区间内，按候选值**从小到大**寻找首个相对**每个**
节点都属于 RFC 1982 有效前进（advance）的值，作为多节点共同发布 serial。

若整个候选区间都不存在共同候选，抛出 :class:`ReleasePlanError`
（由 API 层转换为 HTTP 409），并给出各节点相对候选区间的
equal / regression / incomparable 阻断统计。
"""
from __future__ import annotations

from datetime import date

from .rfc1982 import (
    MAX_SERIAL,
    REL_ADVANCE,
    REL_EQUAL,
    REL_INCOMPARABLE,
    REL_REGRESSION,
    classify,
    forward_distance,
)
from .suggest import NN_MAX, daily_base

# ReleasePlanError.code 取值（用于 409 响应中的 reason 字段）
ERR_NO_COMMON_CANDIDATE = "no_common_candidate"  # 当日区间无共同候选
ERR_DATE_OUT_OF_RANGE = "date_out_of_range"      # 日期超出 32 位表达范围


class ReleasePlanError(Exception):
    """无法给出共同发布规划（对应 HTTP 409 Conflict）。

    :param code: 机器可读的原因代码；
    :param blockers: 无共同候选时各节点对候选区间的阻断摘要（请求顺序）；
    :param date_str: 规划所基于的日期（YYYYMMDD）；
    :param window: 候选区间 (start, end)；
    :param checked: 扫描过的候选值数量。
    """

    def __init__(
        self,
        message: str,
        code: str,
        blockers: list[dict] | None = None,
        date_str: str | None = None,
        window: tuple[int, int] | None = None,
        checked: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.blockers = blockers or []
        self.date_str = date_str
        self.window = window
        self.checked = checked


def build_release_plan(
    zone: str, nodes: list[tuple[str, int]], today: date
) -> dict:
    """为多权威节点规划共同发布 serial。

    :param nodes: ``(节点名, 当前 serial)`` 列表（按请求顺序）；
    :param today: 规划所基于的日期。
    :return: 规划结果字典；失败时抛 :class:`ReleasePlanError`。
    """
    date_str = today.strftime("%Y%m%d")
    base = daily_base(today)
    window_end = base + NN_MAX
    if window_end > MAX_SERIAL:
        raise ReleasePlanError(
            "规划日期超出 32 位序列号可表达范围",
            code=ERR_DATE_OUT_OF_RANGE,
            date_str=date_str,
        )

    # 从小到大扫描，首个对所有节点均为有效前进的候选即答案
    for nn in range(NN_MAX + 1):
        candidate = base + nn
        results: list[dict] = []
        for name, serial in nodes:
            relation = classify(serial, candidate)
            if relation != REL_ADVANCE:
                break
            results.append(
                {
                    "name": name,
                    "serial": serial,
                    "relation": relation,
                    "forward_distance": forward_distance(serial, candidate),
                }
            )
        if len(results) == len(nodes):
            return {
                "zone": zone,
                "date": date_str,
                "candidate": candidate,
                "candidate_window": {"start": base, "end": window_end},
                "nodes": results,
                "note": "候选 serial 相对每个权威节点的当前 serial 均为 RFC 1982 意义下的有效前进；"
                "规划结果仅用于发布预演，不写入数据库",
            }

    # 整个区间无共同候选：汇总各节点对全部候选值的阻断情况
    blockers: list[dict] = []
    for name, serial in nodes:
        counts = {
            REL_ADVANCE: 0,
            REL_EQUAL: 0,
            REL_REGRESSION: 0,
            REL_INCOMPARABLE: 0,
        }
        for nn in range(NN_MAX + 1):
            counts[classify(serial, base + nn)] += 1
        blockers.append(
            {
                "name": name,
                "serial": serial,
                "advance": counts[REL_ADVANCE],
                "equal": counts[REL_EQUAL],
                "regression": counts[REL_REGRESSION],
                "incomparable": counts[REL_INCOMPARABLE],
            }
        )
    raise ReleasePlanError(
        f"当日 YYYYMMDDnn 候选区间（{base}–{window_end}）中不存在相对所有节点"
        "均为 RFC 1982 有效前进的共同候选 serial",
        code=ERR_NO_COMMON_CANDIDATE,
        blockers=blockers,
        date_str=date_str,
        window=(base, window_end),
        checked=NN_MAX + 1,
    )
