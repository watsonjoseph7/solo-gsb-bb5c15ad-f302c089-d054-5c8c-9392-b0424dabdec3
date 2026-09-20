"""RFC 1982 序列号算术（32 位模运算）。

DNS SOA SERIAL 字段是 32 位无符号整数，比较规则见 RFC 1982：
在模 2^32 的环形空间上，若 (b - a) mod 2^32 落在 [1, 2^31 - 1]，
则 a < b；落在 (2^31, 2^32 - 1] 则 a > b；恰好等于 2^31 时二者
"不可比较"（undefined），主从同步行为不确定。
"""
from __future__ import annotations

SERIAL_BITS = 32
SERIAL_SPACE = 1 << SERIAL_BITS        # 2**32
SERIAL_HALF = SERIAL_SPACE // 2        # 2**31
MAX_SERIAL = SERIAL_SPACE - 1          # 4294967295

# 相邻两次观测之间的关系分类
REL_INITIAL = "initial"          # 首个观测值，无比较对象
REL_ADVANCE = "advance"          # 有效前进
REL_EQUAL = "equal"              # 相等
REL_REGRESSION = "regression"    # 倒退
REL_INCOMPARABLE = "incomparable"  # 距离恰好为半区间，不可比较

# 可能使从服务器（slave）拒绝/无法应用更新的关系
SLAVE_REJECT_RELATIONS = frozenset({REL_REGRESSION, REL_INCOMPARABLE})


def forward_distance(a: int, b: int) -> int:
    """从 a 到 b 的模 2^32 前向距离，取值 0 .. 2^32-1。"""
    return (b - a) % SERIAL_SPACE


def classify(prev: int, current: int) -> str:
    """按 RFC 1982 判定 current 相对 prev 的关系。"""
    delta = forward_distance(prev, current)
    if delta == 0:
        return REL_EQUAL
    if delta == SERIAL_HALF:
        return REL_INCOMPARABLE
    if delta < SERIAL_HALF:
        return REL_ADVANCE
    return REL_REGRESSION


def signed_delta(prev: int, current: int) -> int | None:
    """带符号的距离：前进为正、倒退为负、相等为 0、不可比较为 None。"""
    relation = classify(prev, current)
    delta = forward_distance(prev, current)
    if relation == REL_EQUAL:
        return 0
    if relation == REL_ADVANCE:
        return delta
    if relation == REL_REGRESSION:
        return delta - SERIAL_SPACE
    return None  # incomparable
