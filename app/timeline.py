"""多采集源 SOA serial 观测的时间线重建（不落库）。

输入为若干采集源（collector）在不同时刻上报的 SOA serial 观测，处理流程：

1. **时间桶聚合**：按 ``bucket_seconds`` 将观测时间（纪元秒）floor 分桶，
   非空桶按时间升序排列；
2. **桶内源内归并**：同一采集源在同一桶内仅保留 ``observed_at`` 最新的观测；
   若其最新时刻同时出现多个不同 serial，抛 :class:`LatestObservationConflict`
   （API 层转换为 422）；
3. **候选值**：桶内每个不同 serial 为一个候选，支持该候选的采集源集合为
   ``supporters``，桶内其余采集源为 ``dissenters``；
4. **动态规划选线**：相邻桶选值之间只允许 RFC 1982 相等（equal）或有效前进
   （advance），倒退（regression）与相距恰好 2^31 的不可比较（incomparable）
   一律禁止。目标依次为：
     a. 最大化各桶支持采集源数之和（总支持数）；
     b. 最少跳变（相邻桶选值不同的次数）；
     c. 完全并列时按整条 serial 序列（普通整数序）字典序升序。

   未来的可延续性只取决于本桶结束值，因此每个候选值只需保留最优前缀；
   若某桶不存在任何可由上一桶可达状态延续的候选，抛 :class:`TimelineError`
   （API 层转换为 409）并给出冲突桶位置与各候选相对上一桶可达值的关系。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .rfc1982 import (
    REL_ADVANCE,
    REL_EQUAL,
    REL_INCOMPARABLE,
    REL_REGRESSION,
    classify,
)

#: 409 detail.reason：时间线在某个时间桶无法延续
ERR_UNEXTENDABLE = "unextendable_timeline"

#: 422 detail.reason：同一采集源桶内最新时刻上报了不同 serial
ERR_LATEST_CONFLICT = "conflicting_latest_observation"

#: 相邻桶选值之间允许的 RFC 1982 关系
_ALLOWED_RELATIONS = frozenset({REL_EQUAL, REL_ADVANCE})


class LatestObservationConflict(Exception):
    """同一采集源在同一时间桶内的最新观测时刻出现不同 serial（HTTP 422）。

    :param conflicts: 冲突描述列表（按桶下标、采集源名排序）。
    """

    def __init__(self, conflicts: list[dict]) -> None:
        super().__init__("同一采集源在时间桶内的最新观测时刻出现不同 serial")
        self.conflicts = conflicts


class TimelineError(Exception):
    """时间线在某个时间桶无法延续（HTTP 409 Conflict）。

    :param detail: 完整的冲突响应体（reason/message/冲突桶/前缀等）。
    """

    def __init__(self, message: str, detail: dict) -> None:
        super().__init__(message)
        self.detail = detail


def _as_utc(value: datetime) -> datetime:
    """归一化为 UTC：带时区的做转换，朴素时间戳按 UTC 处理。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    """格式化为以 Z 结尾的 UTC ISO 8601 字符串。"""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_better(candidate: tuple, best: tuple) -> bool:
    """比较两个 DP 状态 (总支持数, 跳变数, serial 序列)：支持多者优，
    支持相同跳变少者优，再相同则序列字典序小者优。"""
    if candidate[0] != best[0]:
        return candidate[0] > best[0]
    if candidate[1] != best[1]:
        return candidate[1] < best[1]
    return candidate[2] < best[2]


def build_serial_timeline(
    zone: str,
    observations: list[tuple[str, datetime, int]],
    bucket_seconds: int,
) -> dict:
    """从多源观测重建 serial 时间线，返回结果字典；不写库。

    :param zone: 已规范化的区域名；
    :param observations: ``(采集源名, 观测时间, serial)`` 列表；
    :param bucket_seconds: 时间桶大小（正整数秒）。
    :raises LatestObservationConflict: 桶内源内归并时最新时刻出现不同 serial（422）；
    :raises TimelineError: 某桶候选相对上一桶全部倒退/不可比较，无法延续（409）。
    """
    # 时间桶 key（floor 纪元秒）-> 采集源 -> {"at": 最新时刻, "serials": 该时刻 serial 集合}
    folded: dict[int, dict[str, dict]] = {}
    for collector, observed_at, serial in observations:
        at = _as_utc(observed_at)
        key = int(at.timestamp() // bucket_seconds)
        bucket = folded.setdefault(key, {})
        slot = bucket.get(collector)
        if slot is None or at > slot["at"]:
            # 更早的观测整体作废（含其 serial 集合）
            bucket[collector] = {"at": at, "serials": {serial}}
        elif at == slot["at"]:
            slot["serials"].add(serial)

    keys = sorted(folded)
    conflicts: list[dict] = []
    buckets: list[dict] = []
    for index, key in enumerate(keys):
        start = datetime.fromtimestamp(key * bucket_seconds, tz=timezone.utc)
        end = start + timedelta(seconds=bucket_seconds)
        slot_map = folded[key]

        # 最新时刻存在多个不同 serial 的采集源 => 422 冲突
        for collector in sorted(slot_map):
            serials = slot_map[collector]["serials"]
            if len(serials) > 1:
                conflicts.append(
                    {
                        "bucket_index": index,
                        "bucket_start": _format_utc(start),
                        "bucket_end": _format_utc(end),
                        "collector": collector,
                        "serials": sorted(serials),
                    }
                )

        # 候选 serial -> 支持者采集源列表
        support: dict[int, list[str]] = {}
        for collector in sorted(slot_map):
            serials = slot_map[collector]["serials"]
            if len(serials) == 1:
                support.setdefault(next(iter(serials)), []).append(collector)
        collectors = set(slot_map)
        candidates: list[dict] = []
        for serial in sorted(support):
            supporters = support[serial]
            supporters_set = set(supporters)
            dissenters = sorted(collectors - supporters_set)
            candidates.append(
                {
                    "serial": serial,
                    "supporters": supporters,
                    "dissenters": dissenters,
                }
            )
        buckets.append(
            {"index": index, "start": start, "end": end, "candidates": candidates}
        )

    if conflicts:
        raise LatestObservationConflict(conflicts)

    def bucket_out(bucket: dict, chosen: int) -> dict:
        chosen_group = next(group for group in bucket["candidates"] if group["serial"] == chosen)
        return {
            "index": bucket["index"],
            "bucket_start": _format_utc(bucket["start"]),
            "bucket_end": _format_utc(bucket["end"]),
            "serial": chosen,
            "supporters": chosen_group["supporters"],
            "dissenters": chosen_group["dissenters"],
            "candidates": bucket["candidates"],
        }

    # 动态规划：状态 = 以某候选 serial 结束的最优前缀
    # (总支持数, 跳变数, serial 序列)
    first = buckets[0]
    prev_states: dict[int, tuple[int, int, tuple[int, ...]]] = {
        candidate["serial"]: (len(candidate["supporters"]), 0, (candidate["serial"],))
        for candidate in first["candidates"]
    }

    for pos in range(1, len(buckets)):
        bucket = buckets[pos]
        states: dict[int, tuple[int, int, tuple[int, ...]]] = {}
        for candidate in bucket["candidates"]:
            serial = candidate["serial"]
            best: tuple[int, int, tuple[int, ...]] | None = None
            for prev_serial, prev_state in prev_states.items():
                relation = classify(prev_serial, serial)
                if relation not in _ALLOWED_RELATIONS:
                    continue
                candidate_state = (
                    prev_state[0] + len(candidate["supporters"]),
                    prev_state[1] + (0 if relation == REL_EQUAL else 1),
                    prev_state[2] + (serial,),
                )
                if best is None or _is_better(candidate_state, best):
                    best = candidate_state
            if best is not None:
                states[serial] = best

        if not states:
            # 本桶没有任何候选能从上一桶的可达状态延续 => 409
            best_prefix_state = min(  # type: ignore[assignment]
                prev_states.values(),
                key=lambda state: (-state[0], state[1], state[2]),
            )
            prefix_timeline = list(best_prefix_state[2])
            blocked_candidates = []
            for candidate in bucket["candidates"]:
                serial = candidate["serial"]
                # 能到达此处说明每个关系均为 regression/incomparable
                predecessor_relations = [
                    {"serial": prev_serial, "relation": classify(prev_serial, serial)}
                    for prev_serial in sorted(prev_states)
                ]
                blocked_candidates.append(
                    {
                        "serial": serial,
                        "supporters": candidate["supporters"],
                        "dissenters": candidate["dissenters"],
                        "predecessor_relations": predecessor_relations,
                    }
                )
            detail = {
                "reason": ERR_UNEXTENDABLE,
                "message": (
                    f"第 {bucket['index']} 个时间桶"
                    f"（{_format_utc(bucket['start'])}–{_format_utc(bucket['end'])}）"
                    "中不存在相对上一桶可达选值为 RFC 1982 相等或有效前进的候选 serial，"
                    "时间线无法继续延续"
                ),
                "zone": zone,
                "bucket_seconds": bucket_seconds,
                "conflict_bucket": {
                    "index": bucket["index"],
                    "bucket_start": _format_utc(bucket["start"]),
                    "bucket_end": _format_utc(bucket["end"]),
                },
                "candidates": blocked_candidates,
                "prefix_buckets": [
                    bucket_out(buckets[i], prefix_timeline[i])
                    for i in range(bucket["index"])
                ],
                "prefix_timeline": prefix_timeline,
                "prefix_total_support": best_prefix_state[0],
                "prefix_changes": best_prefix_state[1],
            }
            raise TimelineError(detail["message"], detail)

        prev_states = states

    best_state = min(
        prev_states.values(),
        key=lambda state: (-state[0], state[1], state[2]),
    )
    total_support, changes, sequence = best_state
    return {
        "zone": zone,
        "bucket_seconds": bucket_seconds,
        "buckets": [
            bucket_out(buckets[i], sequence[i]) for i in range(len(buckets))
        ],
        "timeline": list(sequence),
        "total_support": total_support,
        "changes": changes,
        "note": (
            "时间线仅依据请求中的多源观测即时重建，不写入数据库；"
            "相邻时间桶选值只允许 RFC 1982 相等或有效前进，"
            "目标依次为最大化采集源支持数、最少跳变、serial 序列字典序升序"
        ),
    }
