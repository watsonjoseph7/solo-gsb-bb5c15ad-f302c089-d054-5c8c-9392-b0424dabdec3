"""IXFR 增量回放预演（不落库）。

输入为起始 serial、初始 RRset 集合与一组**有序** IXFR 增量（delta）；
每个 delta 声明 ``from_serial`` / ``to_serial`` 以及一批 RR 删除与新增。
回放规则：

1. **serial 链**：首个 delta 的 ``from_serial`` 必须等于 ``start_serial``，
   其后每个 delta 的 ``from_serial`` 必须等于上一个 delta 的
   ``to_serial``；每个 ``to_serial`` 相对 ``from_serial`` 必须是
   RFC 1982 有效前进。该链式校验完全由请求体决定，在请求模型中完成
   （失败为 HTTP 422）；
2. **RR 同一性**：RR 以规范化后的 ``(owner, type, class, ttl, rdata)``
   五元组为判据（属主名小写无尾点、类型/类大写、rdata 去首尾空白，
   均由请求模型完成）；初始状态为初始 RRset 展开出的 RR 集合；
3. **单个 delta 内先删除再新增**：删除的 RR 必须存在、新增的 RR 必须
   不存在，否则抛 :class:`IxfrReplayError`（API 层转换为 HTTP 409）。
   同一 delta 内"先删后增"可用于替换同一 RR（如 TTL 变更）；
4. **原子性**：每个 delta 要么完整应用、要么整体作废——任一操作失败时
   状态回滚到该 delta 之前，409 响应携带该 delta 前的完整快照，
   不含任何半步修改。

整个回放仅在内存中进行，不写入数据库。
"""
from __future__ import annotations

# IxfrReplayError.code 取值（用于 409 响应中的 reason 字段）
ERR_DELETE_MISSING = "delete_missing_rr"
ERR_ADD_EXISTS = "add_existing_rr"

# RR 同一性判据：规范化后的 (owner, type, class, ttl, rdata) 五元组
RrKey = tuple[str, str, str, int, str]
# 初始 RRset：(owner, type, class, ttl, 规范化后的 rdata 元组)
InitialRrset = tuple[str, str, str, int, tuple[str, ...]]
# delta：(from_serial, to_serial, 删除 RR 列表, 新增 RR 列表)
Delta = tuple[int, int, list[RrKey], list[RrKey]]


class IxfrReplayError(Exception):
    """delta 内 RR 操作与当前状态冲突（对应 HTTP 409 Conflict）。

    :param code: 原因代码（``delete_missing_rr`` / ``add_existing_rr``）；
    :param result: 冲突详情（delta 下标、失败操作与该 delta 前的完整快照）。
    """

    def __init__(self, code: str, message: str, result: dict) -> None:
        super().__init__(message)
        self.code = code
        self.result = result


def _rr_out(key: RrKey) -> dict:
    """把内部 RR 五元组转换为响应结构。"""
    owner, rtype, rrclass, ttl, rdata = key
    return {
        "owner": owner,
        "type": rtype,
        "class": rrclass,
        "ttl": ttl,
        "rdata": rdata,
    }


def _rr_text(key: RrKey) -> str:
    """按主文件格式（owner ttl class type rdata）渲染 RR，用于报错信息。"""
    owner, rtype, rrclass, ttl, rdata = key
    return f"{owner} {ttl} {rrclass} {rtype} {rdata}"


def _snapshot_out(state: set[RrKey]) -> list[dict]:
    """把 RR 集合规范化为按 (owner, type, class, ttl) 分组的 RRset 列表。"""
    groups: dict[tuple[str, str, str, int], list[str]] = {}
    for owner, rtype, rrclass, ttl, rdata in state:
        groups.setdefault((owner, rtype, rrclass, ttl), []).append(rdata)
    return [
        {
            "owner": owner,
            "type": rtype,
            "class": rrclass,
            "ttl": ttl,
            "rdata": sorted(rdata),
        }
        for (owner, rtype, rrclass, ttl), rdata in sorted(groups.items())
    ]


def _conflict_result(
    zone: str,
    index: int,
    current: int,
    from_serial: int,
    to_serial: int,
    action: str,
    rr_index: int,
    key: RrKey,
    steps: list[dict],
    state: set[RrKey],
) -> dict:
    """组装 409 冲突详情；``state`` 必须是该 delta 应用前的完整状态。"""
    return {
        "zone": zone,
        "delta_index": index,
        "current_serial": current,
        "from_serial": from_serial,
        "to_serial": to_serial,
        "operation": {"action": action, "rr_index": rr_index, "rr": _rr_out(key)},
        "steps": steps,
        "snapshot": _snapshot_out(state),
    }


def run_ixfr_replay(
    zone: str,
    start_serial: int,
    initial_rrsets: list[InitialRrset],
    deltas: list[Delta],
) -> dict:
    """按顺序回放 IXFR 增量，返回最终 serial、规范化快照与每步增删计数；不写库。

    :param zone: 已规范化的区域名；
    :param start_serial: 回放起始 serial（首个 delta 的 from_serial 须等于它）；
    :param initial_rrsets: 初始 RRset 列表（键与取值须已由请求模型规范化，
        ``(owner, type, class)`` 键唯一性由请求模型保证）；
    :param deltas: 有序 delta 列表，每项为
        ``(from_serial, to_serial, deletes, adds)``，serial 链合法性由请求模型保证；
    :return: 回放结果字典；
    :raises IxfrReplayError: 删除不存在的 RR 或新增已存在的 RR；
        异常的 ``result`` 属性携带 delta 下标、失败操作与该 delta 前的完整快照。
    """
    state: set[RrKey] = set()
    for owner, rtype, rrclass, ttl, rdata_items in initial_rrsets:
        for rdata in rdata_items:
            state.add((owner, rtype, rrclass, ttl, rdata))

    steps: list[dict] = []
    current = start_serial
    for index, (from_serial, to_serial, deletes, adds) in enumerate(deltas):
        # 在副本上应用整个 delta：任一操作失败时 state 保持该 delta 前的状态
        next_state = set(state)
        for rr_index, key in enumerate(deletes):
            if key not in next_state:
                raise IxfrReplayError(
                    ERR_DELETE_MISSING,
                    f"第 {index} 个 delta 删除的第 {rr_index} 条 RR"
                    f"（{_rr_text(key)}）在当前快照中不存在",
                    _conflict_result(
                        zone, index, current, from_serial, to_serial,
                        "delete", rr_index, key, steps, state,
                    ),
                )
            next_state.discard(key)
        for rr_index, key in enumerate(adds):
            if key in next_state:
                raise IxfrReplayError(
                    ERR_ADD_EXISTS,
                    f"第 {index} 个 delta 新增的第 {rr_index} 条 RR"
                    f"（{_rr_text(key)}）在当前快照中已存在",
                    _conflict_result(
                        zone, index, current, from_serial, to_serial,
                        "add", rr_index, key, steps, state,
                    ),
                )
            next_state.add(key)
        steps.append(
            {
                "index": index,
                "from_serial": from_serial,
                "to_serial": to_serial,
                "deleted": len(deletes),
                "added": len(adds),
            }
        )
        state = next_state
        current = to_serial

    return {
        "zone": zone,
        "start_serial": start_serial,
        "final_serial": current,
        "deltas_applied": len(deltas),
        "steps": steps,
        "snapshot": _snapshot_out(state),
    }
