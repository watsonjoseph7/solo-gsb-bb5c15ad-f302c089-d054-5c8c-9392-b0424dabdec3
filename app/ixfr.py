"""IXFR 增量回放预演（不落库）。

给定起始 serial、初始 RRset 快照与一组按应用顺序排列的 IXFR 增量
（delta），在内存中逐步回放，用于发布前验证增量链：

1. **serial 链**：每个 delta 的 ``from_serial`` 必须等于当前 serial（初始为
   ``start_serial``，之后为上一步的 ``to_serial``）；``to_serial`` 必须相对
   当前 serial 构成 RFC 1982 有效前进（相等/倒退/不可比较均拒绝）；
2. **先删后增**：RR 以 ``(owner, type, class, ttl, rdata)`` 五元组为身份
   （全部字段已由请求模型规范化：属主名小写无尾点、类型/类大写、rdata 去
   首尾空白）。同一 delta 内先逐条删除再逐条新增——删除不存在的 RR 或
   新增已存在的 RR 都会使该 delta 整体失败；同一 delta 内删除 X 后再新增
   X 合法（删除先生效）；
3. **整体放弃**：任一步失败即抛 :class:`IxfrReplayError`（API 层转换为
   HTTP 409），异常的 ``detail`` 携带 delta 下标、失败操作、冲突 RR 与该
   delta 前的最后一次完整快照——失败的 delta 不保留半步修改。

输出快照按 ``(owner, type, class, ttl)`` 分组，组间按该四元组字典序、
组内 rdata 按字典序排列。
"""
from __future__ import annotations

from .rfc1982 import REL_ADVANCE, classify

# IxfrReplayError.code 取值（用于 409 响应中的 reason 字段）
ERR_CHAIN_BROKEN = "serial_chain_broken"        # from_serial 与当前 serial 不一致
ERR_INVALID_ADVANCE = "invalid_serial_advance"  # to_serial 非 RFC 1982 有效前进
ERR_DELETE_MISSING = "delete_missing_rr"        # 删除的 RR 在当前快照中不存在
ERR_ADD_EXISTS = "add_existing_rr"              # 新增的 RR 已存在于当前快照

# RR 五元组：(owner, type, class, ttl, rdata)
RrTuple = tuple[str, str, str, int, str]
# 初始 RRset：(owner, type, class, ttl, rdata 元组)
RrsetTuple = tuple[str, str, str, int, tuple[str, ...]]
# 单个增量：(from_serial, to_serial, 删除 RR 列表, 新增 RR 列表)
DeltaTuple = tuple[int, int, list[RrTuple], list[RrTuple]]


class IxfrReplayError(Exception):
    """回放冲突（对应 HTTP 409 Conflict）。

    :param code: 机器可读原因代码；
    :param detail: 完整冲突响应 detail（reason/message/delta 下标/失败操作/
                   冲突 RR/冲突前完整快照等）。
    """

    def __init__(self, message: str, code: str, detail: dict) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


def _rr_out(rr: RrTuple) -> dict:
    """把内部 RR 五元组转换为响应结构。"""
    owner, rtype, rrclass, ttl, rdata = rr
    return {
        "owner": owner,
        "type": rtype,
        "class": rrclass,
        "ttl": ttl,
        "rdata": rdata,
    }


def _snapshot_out(state: set[RrTuple]) -> list[dict]:
    """规范化快照：按 (owner, type, class, ttl) 分组，组内 rdata 字典序。"""
    groups: dict[tuple[str, str, str, int], list[str]] = {}
    for owner, rtype, rrclass, ttl, rdata in state:
        groups.setdefault((owner, rtype, rrclass, ttl), []).append(rdata)
    return [
        {
            "owner": owner,
            "type": rtype,
            "class": rrclass,
            "ttl": ttl,
            "rdata": sorted(rdata_list),
        }
        for (owner, rtype, rrclass, ttl), rdata_list in sorted(groups.items())
    ]


def run_ixfr_replay(
    zone: str,
    start_serial: int,
    initial: list[RrsetTuple],
    deltas: list[DeltaTuple],
) -> dict:
    """按序回放 IXFR 增量，返回最终 serial、逐步增删计数与规范化快照；不写库。

    :param zone: 已校验的区域名；
    :param start_serial: 回放起始 serial（首个 delta 的 from_serial 须等于它）；
    :param initial: 初始 RRset 列表 ``(owner, type, class, ttl, rdata 元组)``
        （已规范化，(owner, type, class) 键唯一）；
    :param deltas: 有序增量 ``(from_serial, to_serial, 删除 RR 列表, 新增 RR 列表)``。
    :raises IxfrReplayError: serial 链断裂 / 非有效前进 / 删除不存在 / 新增已存在。
    """
    state: set[RrTuple] = set()
    for owner, rtype, rrclass, ttl, rdata_list in initial:
        for rdata in rdata_list:
            state.add((owner, rtype, rrclass, ttl, rdata))

    current = start_serial
    steps: list[dict] = []

    def conflict(
        code: str,
        message: str,
        index: int,
        from_serial: int,
        to_serial: int,
        operation: str | None = None,
        rr: RrTuple | None = None,
        relation: str | None = None,
    ) -> IxfrReplayError:
        # state 在此刻仍是失败 delta 应用前的完整快照（不保留半步修改）
        detail = {
            "reason": code,
            "message": message,
            "zone": zone,
            "start_serial": start_serial,
            "current_serial": current,
            "delta_index": index,
            "from_serial": from_serial,
            "to_serial": to_serial,
            "operation": operation,
            "rr": _rr_out(rr) if rr is not None else None,
            "relation": relation,
            "steps": [dict(step) for step in steps],
            "snapshot": _snapshot_out(state),
        }
        return IxfrReplayError(message, code, detail)

    for index, (from_serial, to_serial, deletes, adds) in enumerate(deltas):
        if from_serial != current:
            raise conflict(
                ERR_CHAIN_BROKEN,
                f"第 {index} 个 delta 的 from_serial（{from_serial}）与当前 serial"
                f"（{current}）不一致，IXFR 增量链断裂",
                index,
                from_serial,
                to_serial,
            )
        relation = classify(current, to_serial)
        if relation != REL_ADVANCE:
            raise conflict(
                ERR_INVALID_ADVANCE,
                f"第 {index} 个 delta 的 to_serial（{to_serial}）相对当前 serial"
                f"（{current}）不是 RFC 1982 有效前进（判定为 {relation}）",
                index,
                from_serial,
                to_serial,
                relation=relation,
            )

        # 在副本上先删后增；任一步失败即放弃整个 delta，不保留半步修改
        working = set(state)
        for rr in deletes:
            if rr not in working:
                raise conflict(
                    ERR_DELETE_MISSING,
                    f"第 {index} 个 delta 删除的 RR 在当前快照中不存在: "
                    f"{rr[0]}/{rr[1]}/{rr[2]} ttl={rr[3]} rdata={rr[4]!r}",
                    index,
                    from_serial,
                    to_serial,
                    operation="delete",
                    rr=rr,
                )
            working.discard(rr)
        for rr in adds:
            if rr in working:
                raise conflict(
                    ERR_ADD_EXISTS,
                    f"第 {index} 个 delta 新增的 RR 已存在于当前快照: "
                    f"{rr[0]}/{rr[1]}/{rr[2]} ttl={rr[3]} rdata={rr[4]!r}",
                    index,
                    from_serial,
                    to_serial,
                    operation="add",
                    rr=rr,
                )
            working.add(rr)

        state = working
        current = to_serial
        steps.append(
            {
                "index": index,
                "from_serial": from_serial,
                "to_serial": to_serial,
                "deleted": len(deletes),
                "added": len(adds),
            }
        )

    return {
        "zone": zone,
        "start_serial": start_serial,
        "final_serial": current,
        "delta_count": len(deltas),
        "steps": steps,
        "snapshot": _snapshot_out(state),
    }
