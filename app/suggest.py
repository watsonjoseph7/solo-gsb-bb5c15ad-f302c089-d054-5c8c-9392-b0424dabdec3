"""YYYYMMDDnn 风格的下一序列号建议。

保证返回值相对当前值是 RFC 1982 意义下的有效前进；
当日编号（nn = 00..99）耗尽或存在半区间歧义时抛出 SuggestionError，
由 API 层转换为 HTTP 409。
"""
from __future__ import annotations

from datetime import date

from .rfc1982 import MAX_SERIAL, SERIAL_HALF, forward_distance

#: 当日编号 nn 的最大值（YYYYMMDDnn 共 100 个可用编号）
NN_MAX = 99

# SuggestionError.code 取值（用于 409 响应中的 reason 字段）
ERR_OUT_OF_RANGE = "date_out_of_range"          # 日期超出 32 位表达范围
ERR_HALF_INTERVAL = "half_interval_ambiguous"   # 与当日基准相距恰好 2^31
ERR_AHEAD_OF_TODAY = "ahead_of_today"           # 已领先于当日可用编号区间
ERR_NN_EXHAUSTED = "nn_exhausted"               # 当日编号 00-99 已耗尽


class SuggestionError(Exception):
    """无法给出有效建议（对应 HTTP 409 Conflict）。

    :param code: 机器可读的原因代码，便于调用方区分冲突类型；
                 兼容仅有文本的旧式构造（code 缺省为 "unavailable"）。
    """

    def __init__(self, message: str, code: str = "unavailable") -> None:
        super().__init__(message)
        self.code = code


def daily_base(today: date) -> int:
    """当日 YYYYMMDD00 对应的序列号基准值。"""
    return int(today.strftime("%Y%m%d")) * 100


def suggest_next_serial(current: int, today: date) -> int:
    """返回相对 current 有效前进的下一个 YYYYMMDDnn 序列号。

    规则：
    - current 落后于今日基准（含跨 2^32 回绕）：建议 YYYYMMDD00；
    - current 落在今日区间内：建议 current + 1，nn 已达 99 则报耗尽；
    - current 领先于今日区间（未来日期等）：无法给出当日建议；
    - current 与基准距离恰好为 2^31：半区间歧义，不可比较。
    """
    base = daily_base(today)
    if base + NN_MAX > MAX_SERIAL:
        raise SuggestionError(
            "当前日期超出 32 位序列号可表达范围", code=ERR_OUT_OF_RANGE
        )

    delta = forward_distance(current, base)
    if delta == SERIAL_HALF:
        raise SuggestionError(
            "当前序列号与今日基准的距离恰好为 2^31，RFC 1982 下不可比较，拒绝给出建议",
            code=ERR_HALF_INTERVAL,
        )
    if delta == 0:
        # current == YYYYMMDD00
        return base + 1
    if delta < SERIAL_HALF:
        # current 落后于 base，base 是有效前进
        return base

    # current 领先于 base：仅当其落在今日编号区间内才能递增
    ahead = forward_distance(base, current)
    if ahead > NN_MAX:
        raise SuggestionError(
            "当前序列号已领先于今日可用编号区间，无法给出 YYYYMMDDnn 形式的建议",
            code=ERR_AHEAD_OF_TODAY,
        )
    if ahead == NN_MAX:
        raise SuggestionError(
            "当日编号 nn 已耗尽（00-99），请等待下一日期", code=ERR_NN_EXHAUSTED
        )
    return current + 1
