"""区域传送收敛预演（不落库）。

第 0 轮将源节点切换到发布 serial（发布值相对源节点当前 serial 必须是
RFC 1982 有效前进，该校验在请求模型中完成）；此后的每个同步轮次中，
各节点仅依据**上一轮**快照，从指向自己的上游（有向边 ``from -> to``，
``from`` 为上游）里选择相对自身 serial 为有效前进（advance）、且前向
距离最小的值；距离并列时取上游名字典序最小者。所有节点依据同一份旧
快照同时更新，节点自身不参与候选；没有任何可接受上游的节点保持不变。

某一轮没有任何节点更新时即视为稳定（收敛）；若执行满 ``max_rounds``
个同步轮次后仍有节点可继续更新，抛出 :class:`TransferSimulationError`
（由 API 层转换为 HTTP 409 并携带当前快照）。环路边与普通边适用完全
相同的规则，而每次被选中的更新都构成严格有效前进，因此有限步内必然
稳定，不会在环上无限循环。
"""
from __future__ import annotations

from .rfc1982 import REL_ADVANCE, classify, forward_distance

# TransferSimulationError.code 取值（用于 409 响应中的 reason 字段）
ERR_MAX_ROUNDS = "max_rounds_exceeded"


class TransferSimulationError(Exception):
    """执行满 max_rounds 个同步轮次后仍有节点可更新（对应 HTTP 409 Conflict）。

    :param result: 截止最后一轮的完整预演结果（含当前快照与未收敛节点）。
    """

    def __init__(self, message: str, result: dict) -> None:
        super().__init__(message)
        self.code = ERR_MAX_ROUNDS
        self.result = result


def _pick_upstream(
    name: str,
    state: dict[str, int],
    predecessors: dict[str, list[str]],
) -> tuple[str, int, int] | None:
    """从上一轮上游中为节点选择本端可接受的最佳 serial。

    仅考虑相对本端当前 serial 为 RFC 1982 有效前进的上游，取前向距离
    最小者；距离并列按上游名字典序排序取第一个。无候选时返回 None。
    """
    own = state[name]
    best_key: tuple[int, str] | None = None
    best_value: int | None = None
    for upstream in predecessors[name]:
        value = state[upstream]
        if classify(own, value) != REL_ADVANCE:
            continue
        key = (forward_distance(own, value), upstream)
        if best_key is None or key < best_key:
            best_key = key
            best_value = value
    if best_key is None:
        return None
    distance, upstream = best_key
    return upstream, best_value, distance  # type: ignore[return-value]


def run_transfer_simulation(
    zone: str,
    nodes: list[tuple[str, int]],
    edges: list[tuple[str, str]],
    source: str,
    publish_serial: int,
    max_rounds: int,
) -> dict:
    """执行区域传送收敛预演，返回逐轮变更与最终快照；不写库。

    :param nodes: ``(节点名, 当前 serial)`` 列表（按请求顺序，名字唯一）；
    :param edges: ``(上游名, 下游名)`` 有向传送边列表（引用合法性由 schema 保证）；
    :param source: 第 0 轮切换到发布值的源节点名；
    :param publish_serial: 源节点本次发布的 serial（相对源节点当前值为有效前进）；
    :param max_rounds: 第 0 轮之后允许执行的最大同步轮数。
    :return: 预演结果字典；达到上限仍可更新时抛 :class:`TransferSimulationError`，
             异常的 ``result`` 属性携带同样结构的结果与当前快照。
    """
    order = [name for name, _ in nodes]
    state: dict[str, int] = dict(nodes)

    # 邻接表：downstream -> 去重后按名字典序排列的上游列表
    predecessors: dict[str, set[str]] = {name: set() for name in order}
    for upstream, downstream in edges:
        predecessors[downstream].add(upstream)
    sorted_predecessors = {
        downstream: sorted(upstreams) for downstream, upstreams in predecessors.items()
    }

    # 第 0 轮：源节点无条件切换到发布值
    source_previous = state[source]
    rounds: list[dict] = [
        {
            "round": 0,
            "changes": [
                {
                    "node": source,
                    "previous_serial": source_previous,
                    "serial": publish_serial,
                    "upstream": None,
                    "forward_distance": forward_distance(source_previous, publish_serial),
                }
            ],
        }
    ]
    state[source] = publish_serial

    # 同步轮次：所有节点只读取上一轮快照 state，同时写入 next_state
    sync_rounds_run = 0
    converged = False
    for round_no in range(1, max_rounds + 1):
        changes: list[dict] = []
        next_state = dict(state)
        for name in order:
            picked = _pick_upstream(name, state, sorted_predecessors)
            if picked is None:
                continue
            upstream, value, distance = picked
            changes.append(
                {
                    "node": name,
                    "previous_serial": state[name],
                    "serial": value,
                    "upstream": upstream,
                    "forward_distance": distance,
                }
            )
            next_state[name] = value
        if not changes:
            # 本轮无任何节点更新：系统已稳定
            converged = True
            break
        changes.sort(key=lambda change: change["node"])
        rounds.append({"round": round_no, "changes": changes})
        state = next_state
        sync_rounds_run = round_no

    updatable: list[str] = []
    if not converged:
        # 已执行满 max_rounds 个同步轮次：再做一次只读探测，
        # 若仍有节点可更新则无法在限内确认收敛（409）
        updatable = [
            name
            for name in order
            if _pick_upstream(name, state, sorted_predecessors) is not None
        ]
        if not updatable:
            converged = True

    snapshot = [{"name": name, "serial": state[name]} for name in order]
    unconverged = [node for node in snapshot if node["serial"] != publish_serial]

    result = {
        "zone": zone,
        "source": source,
        "publish_serial": publish_serial,
        "max_rounds": max_rounds,
        "sync_rounds_run": sync_rounds_run,
        "converged": converged,
        "rounds": rounds,
        "snapshot": snapshot,
        "unconverged": unconverged,
        "updatable_nodes": updatable,
    }
    if not converged:
        raise TransferSimulationError(
            f"达到同步轮次上限 {max_rounds} 后仍有 {len(updatable)} 个节点"
            "可以继续更新，无法在限内确认收敛",
            result=result,
        )
    return result
