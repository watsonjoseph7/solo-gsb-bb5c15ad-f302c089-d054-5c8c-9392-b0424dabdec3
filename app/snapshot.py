"""权威快照一致性检查（不落库）。

输入为多个权威节点上报的 RRset 快照：每个节点给出一组以
``(owner, type, class)`` 为键的 RRset，取值为 ``(ttl, rdata 元组)``；
属主名小写化、类型/类大写化、rdata 去重排序等规范化由请求模型完成。
处理规则：

1. **检查域**：所有节点上报 RRset 键的并集。每个节点对每个键要么持有
   一个取值，要么**缺席**——缺席同样视为一种"取值"参与计票，因此
   "多数节点都没有该 RRset"也能形成共识（该键不进入可发布快照）；
2. **多数共识**：对每个键统计各取值的同意节点数，**唯一最高票**且票数
   达到 ``min_agreeing`` 的取值即共识值；最高票并列（即使票数达到门槛）
   或最高票未达门槛都视为冲突；
3. **结果**：共识为存在的键构成可发布共识快照；逐节点比对给出
   ``mismatch``（取值不同）/ ``missing``（节点未上报）/ ``extra``
   （共识为不存在而节点上报了）差异。任一键冲突即抛
   :class:`SnapshotConsistencyError`（API 层转换为 HTTP 409），异常的
   ``result`` 属性携带与成功响应同结构的结果（``conflicts`` 非空）。
"""
from __future__ import annotations

# SnapshotConsistencyError.code 取值（用于 409 响应中的 reason 字段）
ERR_NO_CONSENSUS = "no_rrset_consensus"

# RRset 键：(owner, type, class)
RrsetKey = tuple[str, str, str]
# RRset 取值：(ttl, 规范化后的 rdata 元组)；None 表示节点未上报该键（缺席）
RrsetValue = tuple[int, tuple[str, ...]] | None


class SnapshotConsistencyError(Exception):
    """至少一个 RRset 无法达成唯一多数共识（对应 HTTP 409 Conflict）。

    :param result: 完整检查结果（含已达成共识的快照、逐节点差异与冲突列表）。
    """

    def __init__(self, message: str, result: dict) -> None:
        super().__init__(message)
        self.code = ERR_NO_CONSENSUS
        self.result = result


def _value_sort_key(value: RrsetValue) -> tuple:
    """取值排序键：存在的取值排在缺席之前，再按 (ttl, rdata) 字典序。"""
    if value is None:
        return (1,)
    ttl, rdata = value
    return (0, ttl, rdata)


def _value_out(value: RrsetValue) -> dict | None:
    """把内部取值转换为响应结构；缺席为 None。"""
    if value is None:
        return None
    ttl, rdata = value
    return {"ttl": ttl, "rdata": list(rdata)}


def check_snapshot_consistency(
    zone: str,
    nodes: list[tuple[str, dict[RrsetKey, tuple[int, tuple[str, ...]]]]],
    min_agreeing: int,
) -> dict:
    """对多权威节点的 RRset 快照计算多数共识，返回检查结果；不写库。

    :param nodes: ``(节点名, {RRset 键: (ttl, rdata 元组)})`` 列表
        （按请求顺序，节点名唯一；键与取值须已由请求模型规范化）；
    :param min_agreeing: 达成共识所需的最小同意节点数（1..节点数，
        合法性由请求模型保证）。
    :return: 检查结果字典；任一 RRset 冲突时抛
        :class:`SnapshotConsistencyError`。
    """
    order = [name for name, _ in nodes]
    views = {name: rrsets for name, rrsets in nodes}

    keys: set[RrsetKey] = set()
    for _, rrsets in nodes:
        keys.update(rrsets)

    snapshot: list[dict] = []
    conflicts: list[dict] = []
    diffs: dict[str, list[dict]] = {name: [] for name in order}

    for owner, rtype, rrclass in sorted(keys):
        key = (owner, rtype, rrclass)
        # 统计每种取值（含缺席 None）的同意节点
        votes: dict[RrsetValue, list[str]] = {}
        for name in order:
            value = views[name].get(key)
            votes.setdefault(value, []).append(name)
        ranked = sorted(
            votes.items(), key=lambda item: (-len(item[1]), _value_sort_key(item[0]))
        )
        top_value, top_names = ranked[0]
        tied = len(ranked) > 1 and len(ranked[1][1]) == len(top_names)

        if tied or len(top_names) < min_agreeing:
            # 最高票未达门槛或达到门槛但并列：该键无唯一多数共识 => 冲突
            conflicts.append(
                {
                    "owner": owner,
                    "type": rtype,
                    "class": rrclass,
                    "cause": (
                        "below_threshold"
                        if len(top_names) < min_agreeing
                        else "tied"
                    ),
                    "values": [
                        {
                            "ttl": value[0],
                            "rdata": list(value[1]),
                            "supporters": sorted(names),
                        }
                        for value, names in ranked
                        if value is not None
                    ],
                    "absent": sorted(votes.get(None, [])),
                }
            )
            continue

        # 唯一最高票且达到门槛：形成共识
        top_set = set(top_names)
        if top_value is not None:
            snapshot.append(
                {
                    "owner": owner,
                    "type": rtype,
                    "class": rrclass,
                    "ttl": top_value[0],
                    "rdata": list(top_value[1]),
                    "supporters": sorted(top_names),
                    "dissenters": sorted(
                        name for name in order if name not in top_set
                    ),
                }
            )
        for name in order:
            node_value = views[name].get(key)
            if node_value == top_value:
                continue
            if top_value is None:
                kind = "extra"  # 共识为不存在，节点却上报了
            elif node_value is None:
                kind = "missing"  # 共识存在，节点未上报
            else:
                kind = "mismatch"  # 双方都上报但取值不同
            diffs[name].append(
                {
                    "owner": owner,
                    "type": rtype,
                    "class": rrclass,
                    "diff": kind,
                    "reported": _value_out(node_value),
                    "consensus": _value_out(top_value),
                }
            )

    result = {
        "zone": zone,
        "min_agreeing": min_agreeing,
        "node_count": len(order),
        "snapshot": snapshot,
        "nodes": [
            {"name": name, "consistent": not diffs[name], "diffs": diffs[name]}
            for name in order
        ],
        "conflicts": conflicts,
    }
    if conflicts:
        raise SnapshotConsistencyError(
            f"{len(conflicts)} 个 RRset 未达成唯一多数共识"
            f"（要求唯一最高票且同意节点数 ≥ {min_agreeing}）",
            result=result,
        )
    return result
