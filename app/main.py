"""DNS SOA 序列号审计 API。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Response

from . import db
from .rfc1982 import MAX_SERIAL
from .schemas import (
    AuditCreate,
    AuditListItem,
    AuditOut,
    NextSerialOut,
    ReleasePlanConflictResponse,
    ReleasePlanCreate,
    ReleasePlanOut,
    RepairPlanOut,
    SerialReservationCreate,
    SerialReservationOut,
    SerialTimelineConflictResponse,
    SerialTimelineCreate,
    SerialTimelineOut,
    SnapshotConsistencyConflictResponse,
    SnapshotConsistencyCreate,
    SnapshotConsistencyOut,
    TransferSimulationConflictResponse,
    TransferSimulationCreate,
    TransferSimulationOut,
    validate_zone,
)
from .repair import build_repair_plan
from .release_plan import ReleasePlanError, build_release_plan
from .service import analyze
from .snapshot import SnapshotConsistencyError, check_snapshot_consistency
from .suggest import SuggestionError, suggest_next_serial
from .timeline import (
    LatestObservationConflict,
    TimelineError,
    build_serial_timeline,
)
from .transfer import TransferSimulationError, run_transfer_simulation


def _normalize_zone(zone: str) -> str:
    """校验路径中的区域名，非法时返回 400（路径参数不适用 422 语义）。"""
    try:
        return validate_zone(zone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法的 DNS 区域名") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时完成数据库初始化（建表，幂等）
    db.init_db(app.state.db_path)
    yield


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="DNS SOA 序列号审计 API",
        description=(
            "提交区域名与按观测时间排列的 SOA serial，按 RFC 1982 模运算"
            "逐项判定前进/倒退/相等/不可比较，标出可能使从服务器拒绝更新的"
            "节点，并提供 YYYYMMDDnn 风格的下一序列号建议。"
        ),
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.db_path = db_path or db.default_db_path()

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {
            "service": "dns-soa-serial-audit",
            "docs": "/docs",
            "health": "/health",
        }

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/api/audits", response_model=AuditOut, status_code=201)
    def create_audit(payload: AuditCreate) -> dict:
        """提交一次审计并返回完整判定结果。"""
        items, summary = analyze(payload.serials)
        return db.insert_audit(
            app.state.db_path, payload.zone, payload.serials, items, summary
        )

    @app.get("/api/audits", response_model=list[AuditListItem])
    def list_audits(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict]:
        """按时间倒序列出审计记录（摘要）。"""
        return db.list_audits(app.state.db_path, limit=limit, offset=offset)

    @app.get("/api/audits/{audit_id}", response_model=AuditOut)
    def get_audit(audit_id: int) -> dict:
        """按 ID 查询审计记录。"""
        record = db.get_audit(app.state.db_path, audit_id)
        if record is None:
            raise HTTPException(status_code=404, detail="审计记录不存在")
        return record

    @app.get("/api/audits/{audit_id}/repair-plan", response_model=RepairPlanOut)
    def get_repair_plan(audit_id: int) -> dict:
        """基于已有审计即时生成序列修复预演（不写库）。

        首个 serial 保持不变；其后每项与上一个修复后值按 RFC 1982
        比较，有效前进则保留，否则替换为上一个修复后值加 1（模 2^32）。
        """
        record = db.get_audit(app.state.db_path, audit_id)
        if record is None:
            raise HTTPException(status_code=404, detail="审计记录不存在")
        items, summary = build_repair_plan(record["serials"])
        return {
            "id": record["id"],
            "zone": record["zone"],
            "created_at": record["created_at"],
            "items": items,
            "summary": summary,
        }

    @app.get("/api/next-serial", response_model=NextSerialOut)
    def next_serial(
        current: int = Query(ge=0, le=MAX_SERIAL, description="当前 SOA serial"),
        today: date | None = Query(
            default=None,
            description="建议所基于的日期（YYYY-MM-DD），缺省为服务器 UTC 当日",
        ),
    ) -> dict:
        """给出 YYYYMMDDnn 风格的下一序列号建议（保证为有效前进）。

        当日编号耗尽或存在半区间歧义时返回 409。
        """
        effective_today = today or datetime.now(timezone.utc).date()
        try:
            suggested = suggest_next_serial(current, effective_today)
        except SuggestionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "current": current,
            "next": suggested,
            "date": effective_today.strftime("%Y%m%d"),
            "note": "建议值相对当前值为 RFC 1982 意义下的有效前进",
        }

    @app.post(
        "/api/zones/{zone}/serial-reservations",
        response_model=SerialReservationOut,
        status_code=201,
        responses={
            200: {"model": SerialReservationOut, "description": "幂等重试，返回首次预留结果"},
            409: {"description": "current 与服务端最新预留不一致，或编号耗尽/半区间歧义"},
        },
    )
    def reserve_serial(zone: str, payload: SerialReservationCreate, response: Response) -> dict:
        """原子预留下一个 YYYYMMDDnn serial 并持久化。

        - 同一区域内相同 ``request_id`` 重试返回首次结果（200），不再次推进；
        - ``current`` 与服务端已预留的最新值不一致时返回 409，
          响应体 ``latest_serial`` 给出最新值；
        - 当日编号耗尽 / 半区间歧义等同样返回 409。
        """
        zone_name = _normalize_zone(zone)
        try:
            record, replayed = db.reserve_serial(
                app.state.db_path,
                zone_name,
                payload.request_id,
                payload.current,
            )
        except db.ReservationError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": exc.code,
                    "message": str(exc),
                    "latest_serial": exc.latest_serial,
                },
            ) from exc
        except SuggestionError as exc:
            raise HTTPException(
                status_code=409,
                detail={"reason": exc.code, "message": str(exc), "latest_serial": None},
            ) from exc
        record["replayed"] = replayed
        if replayed:
            response.status_code = 200
        return record

    @app.get(
        "/api/zones/{zone}/serial-reservations",
        response_model=list[SerialReservationOut],
    )
    def list_serial_reservations(
        zone: str,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict]:
        """按区域查询预留记录（按预留先后升序）。"""
        zone_name = _normalize_zone(zone)
        return db.list_serial_reservations(
            app.state.db_path, zone_name, limit=limit, offset=offset
        )

    @app.post(
        "/api/zones/{zone}/release-plans",
        response_model=ReleasePlanOut,
        responses={
            409: {
                "model": ReleasePlanConflictResponse,
                "description": (
                    "当日 YYYYMMDD00–99 区间无共同候选（no_common_candidate，"
                    "detail 给出各节点 equal/regression/incomparable 阻断摘要），"
                    "或规划日期超出 32 位表达范围（date_out_of_range）"
                ),
            }
        },
    )
    def create_release_plan(zone: str, payload: ReleasePlanCreate) -> dict:
        """为多权威节点规划共同发布 serial（仅规划，不写库）。

        在当日 ``YYYYMMDD00``～``YYYYMMDD99`` 候选区间内按从小到大扫描，
        返回首个相对**每个**节点当前 serial 都属于 RFC 1982 有效前进
        （advance）的候选值，并给出候选相对每个节点的关系与前向距离。

        整个候选区间都不存在共同候选时返回 409，``detail.blockers`` 给出
        各节点对候选区间的 ``equal`` / ``regression`` / ``incomparable``
        阻断计数（另附 ``advance`` 计数，四项之和为扫描候选数）。
        """
        zone_name = _normalize_zone(zone)
        effective_today = payload.today or datetime.now(timezone.utc).date()
        nodes = [(node.name, node.serial) for node in payload.nodes]
        try:
            return build_release_plan(zone_name, nodes, effective_today)
        except ReleasePlanError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": exc.code,
                    "message": str(exc),
                    "date": exc.date_str,
                    "candidate_window": (
                        {"start": exc.window[0], "end": exc.window[1]}
                        if exc.window is not None
                        else None
                    ),
                    "checked": exc.checked,
                    "blockers": exc.blockers,
                },
            ) from exc

    @app.post(
        "/api/zones/{zone}/transfer-simulations",
        response_model=TransferSimulationOut,
        responses={
            409: {
                "model": TransferSimulationConflictResponse,
                "description": (
                    "执行满 max_rounds 个同步轮次后仍有节点可继续更新"
                    "（max_rounds_exceeded，detail 携带逐轮记录、当前快照与未收敛节点）"
                ),
            }
        },
    )
    def create_transfer_simulation(zone: str, payload: TransferSimulationCreate) -> dict:
        """区域传送（AXFR/IXFR 传播）收敛预演，结果不写库。

        第 0 轮源节点切换到 ``publish_serial``（相对其当前 serial 必须是
        RFC 1982 有效前进，否则请求阶段即 422）；此后每个同步轮次中，每个
        节点只依据**上一轮**快照，从有向边（``from`` 为上游）指向自己的
        上游里选择相对自身 serial 为有效前进且前向距离最小的值，距离并列
        按上游名字典序排序，所有节点同时更新；某轮无任何更新即收敛。
        达到 ``max_rounds`` 后若仍有节点可更新，返回 409 并携带当前快照。
        """
        zone_name = _normalize_zone(zone)
        nodes = [(node.name, node.serial) for node in payload.nodes]
        edges = [(edge.from_, edge.to) for edge in payload.edges]
        try:
            result = run_transfer_simulation(
                zone_name,
                nodes,
                edges,
                payload.source,
                payload.publish_serial,
                payload.max_rounds,
            )
        except TransferSimulationError as exc:
            detail = dict(exc.result)
            detail["reason"] = exc.code
            detail["message"] = str(exc)
            raise HTTPException(status_code=409, detail=detail) from exc
        result["note"] = (
            "预演结果仅用于传送收敛分析，不写入数据库；第 0 轮为源节点发布切换，"
            "其后各轮所有节点仅依据上一轮快照同时更新"
        )
        return result

    @app.post(
        "/api/zones/{zone}/serial-timelines",
        response_model=SerialTimelineOut,
        responses={
            409: {
                "model": SerialTimelineConflictResponse,
                "description": (
                    "某时间桶的全部候选相对上一桶可达选值均为倒退或不可比较，"
                    "时间线无法延续（unextendable_timeline，detail 给出冲突桶位置、"
                    "各候选与上一桶可达值的关系及冲突前的最优前缀）"
                ),
            },
            422: {
                "description": (
                    "同一采集源在桶内的最新观测时刻同时出现不同 serial"
                    "（reason=conflicting_latest_observation，detail 列出冲突位置）；"
                    "其余请求体字段校验失败同样返回 422"
                ),
            },
        },
    )
    def create_serial_timeline(zone: str, payload: SerialTimelineCreate) -> dict:
        """多源观测 serial 时间线重建（不落库）。

        观测按 ``bucket_seconds`` 做时间桶聚合，同一采集源在桶内仅保留
        ``observed_at`` 最新的观测；其最新时刻出现不同 serial 时返回 422。
        随后用动态规划从各桶候选值中选择一条相邻桶仅含 RFC 1982 相等或有效
        前进关系的时间线，目标依次为：最大化采集源支持数（各桶支持数之和）、
        最少跳变、完全并列按整条 serial 序列字典序升序。倒退与不可比较的
        选值路径一律禁止；首个无法延续的桶返回 409 并给出冲突位置与前缀。
        """
        zone_name = _normalize_zone(zone)
        observations = [
            (item.collector, item.observed_at, item.serial)
            for item in payload.observations
        ]
        try:
            return build_serial_timeline(
                zone_name, observations, payload.bucket_seconds
            )
        except LatestObservationConflict as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "reason": "conflicting_latest_observation",
                    "message": str(exc),
                    "conflicts": exc.conflicts,
                },
            ) from exc
        except TimelineError as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc

    @app.post(
        "/api/zones/{zone}/snapshot-consistency",
        response_model=SnapshotConsistencyOut,
        responses={
            409: {
                "model": SnapshotConsistencyConflictResponse,
                "description": (
                    "任一 RRset 未达成唯一多数共识（no_rrset_consensus，detail 携带"
                    "已达成共识的快照与逐节点差异，以及每个冲突 RRset 的取值投票分布）"
                ),
            }
        },
    )
    def check_zone_snapshot_consistency(
        zone: str, payload: SnapshotConsistencyCreate
    ) -> dict:
        """多权威节点 RRset 快照一致性检查（仅计算，不写库）。

        各节点上报的 RRset 以 ``(owner, type, class)`` 为键：属主名规范化为
        小写无尾点、类型与类大写、rdata 去重并按字典序排序后，对每个键统计
        各取值（含"缺席"）的同意节点数；**唯一最高票**且达到 ``min_agreeing``
        的取值即共识值。共识为存在的键构成可发布共识快照，响应同时给出逐节点
        差异（mismatch/missing/extra）。任一键最高票并列或未达门槛时返回 409，
        ``detail.conflicts`` 列出冲突 RRset 的取值投票分布与缺席节点。
        """
        zone_name = _normalize_zone(zone)
        nodes = [
            (
                node.name,
                {
                    (rrset.owner, rrset.type, rrset.class_): (
                        rrset.ttl,
                        tuple(rrset.rdata),
                    )
                    for rrset in node.rrsets
                },
            )
            for node in payload.nodes
        ]
        try:
            result = check_snapshot_consistency(
                zone_name, nodes, payload.min_agreeing
            )
        except SnapshotConsistencyError as exc:
            detail = dict(exc.result)
            detail["reason"] = exc.code
            detail["message"] = str(exc)
            raise HTTPException(status_code=409, detail=detail) from exc
        result["note"] = (
            "共识快照仅依据本次请求中的节点快照即时计算，不写入数据库；"
            "RRset 以 (owner, type, class) 为键，属主名规范化为小写无尾点、"
            "类型与类大写、rdata 去重并按字典序排序；共识值为唯一最高票且"
            "同意节点数达到 min_agreeing 的取值"
        )
        return result

    return app


app = create_app()
