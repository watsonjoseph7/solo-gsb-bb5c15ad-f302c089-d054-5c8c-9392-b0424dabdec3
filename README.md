# DNS SOA 序列号审计 API

基于 **Python + FastAPI + SQLite** 的 DNS SOA serial 审计服务：

- 客户端提交区域名与**按观测时间排列**的 32 位 serial 列表；
- 服务端按 **RFC 1982** 模运算逐项判定 `advance`（前进）/ `regression`（倒退）/ `equal`（相等）/ `incomparable`（不可比较），并标出**可能使从服务器拒绝更新的节点**（`slave_reject_risk`）；
- 提供 **YYYYMMDDnn** 风格的下一序列号建议，保证相对当前值为有效前进；当日编号耗尽或存在半区间歧义时返回 **409**；
- 审计记录持久化于 SQLite，可按 ID 查询；
- 支持基于已有审计**即时生成序列修复预演**（不落库）：首个 serial 不变，其后各项与上一个修复后值按 RFC 1982 比较，非有效前进即替换为上一修复后值加 1（32 位无符号回绕）；
- 提供 **SOA 序列号持久化预留**：在 SQLite 写事务内按区域原子分配下一个 YYYYMMDDnn serial，并发请求不会获得重复值；以 `request_id` 为幂等键，重试安全返回首次结果；预留记录持久化，可按区域查询；
- 提供 **多权威节点共同发布规划**（`POST /api/zones/{zone}/release-plans`，仅规划不写库）：提交各权威节点的名称与当前 serial，在当日 `YYYYMMDD00`～`99` 候选区间内从小到大找出首个相对**每个节点**都是 RFC 1982 有效前进的候选值；当日区间不存在共同候选时返回 **409**，并给出各节点的 `equal`/`regression`/`incomparable` 阻断摘要；
- 提供 **区域传送收敛预演**（`POST /api/zones/{zone}/transfer-simulations`，仅预演不写库）：给定各节点当前 serial、有向传送边、源节点与发布 serial，模拟第 0 轮源节点发布后的逐轮同步传播（每节点只从上一轮上游中选择相对自身为 RFC 1982 有效前进且前向距离最小的值，距离并列按上游名排序，所有节点同时更新），返回逐轮变更、最终快照与未收敛节点；达到 `max_rounds` 后仍有节点可更新时返回 **409** 并携带当前快照。
- 提供 **多源观测 serial 时间线重建**（`POST /api/zones/{zone}/serial-timelines`，仅计算不写库）：接收多个采集源（collector）在不同时刻上报的 SOA serial，按 `bucket_seconds` 做时间桶聚合，桶内同一采集源只取 `observed_at` 最新观测（最新时刻出现不同 serial 返回 **422**）；再用动态规划从各桶候选值中选出一条相邻桶只含 RFC 1982 相等或有效前进关系的时间线，目标依次为最大化采集源支持数、最少跳变、完全并列按整条 serial 序列字典序升序；倒退与不可比较一律禁止，首个无法延续的桶返回 **409** 并给出冲突位置与最优前缀。
- 提供 **权威快照一致性检查**（`POST /api/zones/{zone}/snapshot-consistency`，仅检查不写库）：接收多个权威节点的 RRset 快照（owner、type、class、ttl、rdata）与最小同意节点数 `min_agreeing`，将属主名规范化为小写无尾点、类型与类大写、rdata 去重排序后，按 RRset 统计各取值（含"缺席"）的同意节点数，取**唯一最高票**且达到门槛的值为共识；返回可发布的共识快照、逐节点差异（mismatch/missing/extra）与冲突 RRset；任一 RRset 最高票未达门槛或达到门槛但并列时返回 **409** 并列出各取值的投票分布。

## 快速启动（单条命令）

```bash
docker compose up --build
```

启动后在浏览器访问 **http://localhost:8000**（根路径返回服务信息；交互式 API 文档见 **http://localhost:8000/docs**，健康检查 http://localhost:8000/health）。

服务监听容器内 **8000** 端口，`docker-compose.yml` 通过 `ports: ["8000:8000"]` 映射到宿主机的 **8000** 端口；如需改用其他宿主机端口（例如 18000），将其改为 `"18000:8000"` 即可，应用与镜像无需任何修改。

依赖安装与 SQLite 建表均在启动时自动完成，无需额外初始化步骤；数据通过命名卷 `dns-audit-data` 持久化。

不使用 Docker 时也可本地运行：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

默认监听 **8000** 端口，可通过 `--port` 修改（容器方式则调整 `docker-compose.yml` 的端口映射）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/audits` | 提交审计，返回逐项判定结果（201） |
| `GET` | `/api/audits/{id}` | 按 ID 查询审计记录（不存在返回 404） |
| `GET` | `/api/audits/{id}/repair-plan` | 基于已有审计生成序列修复预演，不落库（不存在返回 404） |
| `GET` | `/api/audits` | 审计记录列表（摘要，倒序） |
| `GET` | `/api/next-serial?current=<serial>` | YYYYMMDDnn 下一序列号建议（歧义/耗尽返回 409） |
| `POST` | `/api/zones/{zone}/serial-reservations` | 原子预留下一个 serial 并持久化（201；幂等重试返回 200；冲突返回 409） |
| `GET` | `/api/zones/{zone}/serial-reservations` | 按区域查询预留记录（预留先后升序，支持 `limit`/`offset`） |
| `POST` | `/api/zones/{zone}/release-plans` | 多权威节点共同发布规划，不写库（200；当日无共同候选返回 409） |
| `POST` | `/api/zones/{zone}/transfer-simulations` | 区域传送收敛预演，不写库（200；达到轮次上限仍可更新返回 409） |
| `POST` | `/api/zones/{zone}/serial-timelines` | 多源观测 serial 时间线重建，不写库（200；桶内最新观测冲突 422；无法延续 409） |
| `POST` | `/api/zones/{zone}/snapshot-consistency` | 权威快照一致性检查，不写库（200；任一 RRset 未达成唯一多数共识返回 409） |
| `GET` | `/health` | 健康检查 |

### 提交审计

```bash
curl -s -X POST http://localhost:8000/api/audits \
  -H 'Content-Type: application/json' \
  -d '{"zone": "example.com", "serials": [2026091800, 2026091801, 2026091801, 2026091705]}'
```

响应（节选）：

```json
{
  "id": 1,
  "zone": "example.com",
  "created_at": "2026-09-18T08:00:00.000000Z",
  "items": [
    {"index": 0, "serial": 2026091800, "relation": "initial",    "delta": null,      "slave_reject_risk": false, "note": "首个观测值，作为后续比较基准"},
    {"index": 1, "serial": 2026091801, "relation": "advance",    "delta": 1,         "slave_reject_risk": false, "note": "按 RFC 1982 为有效前进，从服务器可正常接受更新"},
    {"index": 2, "serial": 2026091801, "relation": "equal",      "delta": 0,         "slave_reject_risk": false, "note": "与上一观测值相等，从服务器不会触发区域传输"},
    {"index": 3, "serial": 2026091705, "relation": "regression", "delta": -96,       "slave_reject_risk": true,  "note": "序列号倒退，从服务器将认为本端数据更旧而拒绝更新"}
  ],
  "summary": {"total": 4, "advance": 1, "equal": 1, "regression": 1, "incomparable": 0, "risk_indexes": [3]}
}
```

### 按 ID 查询

```bash
curl -s http://localhost:8000/api/audits/1
```

### 序列修复预演

对已提交审计即时生成修复方案，**结果不写入数据库**，也不修改原审计记录。首个 serial 保持不变；其后每项都与**上一个修复后值**按 RFC 1982 比较——有效前进则保留（`keep`），相等/倒退/不可比较时替换（`replace`）为上一个修复后值加 1，并按 32 位无符号整数（模 `2^32`）回绕。

```bash
curl -s http://localhost:8000/api/audits/1/repair-plan
```

```json
{
  "id": 1,
  "zone": "example.com",
  "created_at": "2026-09-18T08:00:00.000000Z",
  "items": [
    {"index": 0, "original": 2026091800, "effective": 2026091800, "action": "keep",    "reason": "首个观测值保持不变，作为修复序列的比较基准"},
    {"index": 1, "original": 2026091801, "effective": 2026091801, "action": "keep",    "reason": "相对上一修复后值为 RFC 1982 有效前进，保留原值"},
    {"index": 2, "original": 2026091801, "effective": 2026091802, "action": "replace", "reason": "与上一修复后值相等，未形成有效前进，替换为上一修复后值加 1"},
    {"index": 3, "original": 2026091705, "effective": 2026091803, "action": "replace", "reason": "相对上一修复后值倒退，从服务器将拒绝更新，替换为上一修复后值加 1"}
  ],
  "summary": {"total": 4, "replaced": 2}
}
```

- 每项返回 `original`（原始值）、`effective`（修复后生效值）、`action`（`keep`/`replace`）与 `reason`（保留或替换原因），`summary.replaced` 汇总替换数量；
- 例如 `4294967295 → 4294967295` 会替换为 `0`（回绕后仍是有效前进）；审计 ID 不存在时返回 **404**。

### 下一序列号建议

```bash
curl -s 'http://localhost:8000/api/next-serial?current=2026091807'
# {"current":2026091807,"next":2026091808,"date":"20260918","note":"建议值相对当前值为 RFC 1982 意义下的有效前进"}
```

- 当前值落后于当日基准（含 32 位回绕场景）时，建议当日的 `YYYYMMDD00`；
- 当前值落在当日区间内时，建议 `current + 1`；
- `nn` 已达 `99`（当日编号耗尽）、当前值领先于当日区间、或与当日基准相距恰好 `2^31`（半区间歧义）时返回 **409**；
- 可选参数 `today=YYYY-MM-DD` 指定建议所基于的日期（缺省为服务器 UTC 当日）。

### SOA 序列号持久化预留

`POST /api/zones/{zone}/serial-reservations` 在**单个 SQLite 写事务**（`BEGIN IMMEDIATE` 串行化写入）内完成"读取区域最新预留 → 按 YYYYMMDDnn/RFC 1982 规则分配 → 落库"，因此并发请求不会获得重复 serial。请求体：

| 字段 | 说明 |
| --- | --- |
| `current` | 调用方当前持有的最新 serial（区域首次预留时即其当前发布值） |
| `request_id` | 幂等键（同一区域内唯一）；相同值重试返回首次结果，不会再次推进 |

```bash
# 首次预留
curl -s -X POST http://localhost:8000/api/zones/example.com/serial-reservations \
  -H 'Content-Type: application/json' \
  -d '{"current": 2026091900, "request_id": "deploy-20260919-001"}'
```

```json
{
  "id": 1,
  "zone": "example.com",
  "request_id": "deploy-20260919-001",
  "current": 2026091900,
  "serial": 2026091901,
  "date": "20260919",
  "created_at": "2026-09-19T01:48:31.296258Z",
  "replayed": false
}
```

**幂等重试**：同一区域、同一 `request_id` 的重复请求返回 **200**（首次为 **201**），响应体与首次完全一致且 `replayed: true`——即使重试携带了不同的 `current`，也原样返回首次预留值，序列号不再推进。

**乐观并发控制**：`current` 必须与服务端为该区域预留的最新值一致；不一致时返回 **409**，并在响应体中给出最新值，调用方应使用该值重试：

```json
{
  "detail": {
    "reason": "stale_current",
    "message": "current 与服务端已预留的最新序列号 2026091901 不一致",
    "latest_serial": 2026091901
  }
}
```

典型的并发安全调用循环：

```bash
# 收到 409 后，用 detail.latest_serial 作为新的 current、保持/更换 request_id 重试
curl -s -X POST http://localhost:8000/api/zones/example.com/serial-reservations \
  -H 'Content-Type: application/json' \
  -d '{"current": 2026091901, "request_id": "deploy-20260919-002"}'
```

- 当日编号耗尽（`nn` 已到 99）、当前值领先当日区间、或半区间歧义时同样返回 **409**，`detail.reason` 分别为 `nn_exhausted` / `ahead_of_today` / `half_interval_ambiguous`，此时 `latest_serial` 为 `null`；
- 不同区域的预留计数互相独立，同一 `request_id` 可在不同区域分别使用；
- 区域首次预留不校验历史（无既有记录即接受），之后以已预留的最新值为准。

**按区域查询预留记录**（按预留先后升序，支持 `limit`/`offset` 分页）：

```bash
curl -s 'http://localhost:8000/api/zones/example.com/serial-reservations?limit=50&offset=0'
```

```json
[
  {"id": 1, "zone": "example.com", "request_id": "deploy-20260919-001",
   "current": 2026091900, "serial": 2026091901, "date": "20260919",
   "created_at": "2026-09-19T01:48:31.296258Z", "replayed": false}
]
```

> 预留记录写入同一个 SQLite 数据库（容器内 `/data/audits.db`，由命名卷 `dns-audit-data` 持久化），服务重启后仍然存在，`current` 校验与幂等重试在重启后继续生效。路径中区域名非法时返回 **400**，请求体校验失败返回 **422**。

### 多权威节点共同发布规划

`POST /api/zones/{zone}/release-plans` 用于多权威（multi-primary）发布前的序列号协调：提交各权威节点的名称与当前 serial，服务端在当日 `YYYYMMDD00`～`YYYYMMDD99` 候选区间内**从小到大**扫描，返回首个相对**每个**节点当前 serial 都属于 RFC 1982 有效前进（advance）的候选值。规划结果**仅用于发布预演，不写入数据库**（不会产生审计或预留记录）。

请求体：

| 字段 | 说明 |
| --- | --- |
| `nodes` | 节点列表（至少 1 个、至多 256 个）；每项含 `name`（节点名，去除首尾空白后须在请求内唯一）与 `serial`（当前 SOA serial，沿用 32 位无符号校验 `0..4294967295`） |
| `today` | 可选，规划所基于的日期（`YYYY-MM-DD`）；缺省为服务器 UTC 当日 |

```bash
curl -s -X POST http://localhost:8000/api/zones/example.com/release-plans \
  -H 'Content-Type: application/json' \
  -d '{
        "today": "2026-09-19",
        "nodes": [
          {"name": "ns1.example.com", "serial": 2026091805},
          {"name": "ns2.example.com", "serial": 2026010100}
        ]
      }'
```

```json
{
  "zone": "example.com",
  "date": "20260919",
  "candidate": 2026091900,
  "candidate_window": {"start": 2026091900, "end": 2026091999},
  "nodes": [
    {"name": "ns1.example.com", "serial": 2026091805, "relation": "advance", "forward_distance": 95},
    {"name": "ns2.example.com", "serial": 2026010100, "relation": "advance", "forward_distance": 81800}
  ],
  "note": "候选 serial 相对每个权威节点的当前 serial 均为 RFC 1982 意义下的有效前进；规划结果仅用于发布预演，不写入数据库"
}
```

- `candidate` 为候选区间（`candidate_window`）中首个对所有节点都有效的值；若较小候选对某个节点是相等/倒退/不可比较，会被自动跳过（例如节点已持有 `YYYYMMDD03` 时会从 `04` 起继续寻找）；
- `nodes[]` 与请求顺序一致，每项给出候选相对该节点的 `relation`（共同候选恒为 `advance`）与模 `2^32` 的 `forward_distance`（含 `4294967295 → 0` 这类回绕前进）；
- 当整个 `00..99` 区间都不存在共同候选时返回 **409**，`detail.blockers` 按节点列出其对全部 100 个候选值的阻断计数（`equal` / `regression` / `incomparable`，另附 `advance`，四项之和为 `checked`=100）：

```json
{
  "detail": {
    "reason": "no_common_candidate",
    "message": "当日 YYYYMMDDnn 候选区间（2026091900–2026091999）中不存在相对所有节点均为 RFC 1982 有效前进的共同候选 serial",
    "date": "20260919",
    "candidate_window": {"start": 2026091900, "end": 2026091999},
    "checked": 100,
    "blockers": [
      {"name": "ns1.example.com", "serial": 4173575618, "advance": 70, "equal": 0, "regression": 29, "incomparable": 1},
      {"name": "ns2.example.com", "serial": 2026091970, "advance": 29, "equal": 1, "regression": 70, "incomparable": 0}
    ]
  }
}
```

- 上例中 `ns1` 只接受 `00..69`（在 `70` 处与候选相距恰好 `2^31` 不可比较），而 `ns2` 已持有 `9970`（仅 `71..99` 才是前进、`70` 相等），两节点可行区间互不相交，因此需等待下一日期或先协调各节点 serial；
- 指定的 `today` 超出 32 位序列号可表达范围（如 `9999-12-31`）时返回 **409**，`detail.reason` 为 `date_out_of_range`；节点名重复/空白、`serial` 越界等请求体问题返回 **422**，路径中区域名非法返回 **400**。

### 区域传送收敛预演

`POST /api/zones/{zone}/transfer-simulations` 用于发布后的区域传送（AXFR/IXFR 传播）收敛分析：给定各节点**当前 serial**、**有向传送边**、**源节点**与**发布 serial**，模拟 serial 沿拓扑逐轮传播直到稳定。预演**不写入数据库**（不会产生审计或预留记录）。

请求体：

| 字段 | 说明 |
| --- | --- |
| `nodes` | 节点列表（至少 1 个、至多 256 个）；每项含 `name`（去除首尾空白后须在请求内唯一）与 `serial`（当前 SOA serial，`0..4294967295`） |
| `edges` | 有向传送边列表（可空、至多 4096 条，重复边视为一条）；每条为 `{"from": <上游>, "to": <下游>}`，表示下游从该上游同步 serial；自环与环路均允许，环路适用与普通边相同的规则 |
| `source` | 第 0 轮切换到发布值的源节点名（必须在 `nodes` 中声明） |
| `publish_serial` | 源节点本次发布的 serial，**相对源节点当前 serial 必须是 RFC 1982 有效前进**（相等/倒退/不可比较返回 422） |
| `max_rounds` | 可选，第 0 轮之后允许执行的最大**同步轮数**（`1..1000`，缺省 `64`） |

同步规则：

- **第 0 轮**：源节点无条件切换到 `publish_serial`（不计入 `max_rounds`）；
- 之后每个同步轮次中，每个节点只依据**上一轮**快照，在指向自己的上游（边的 `from` 端）中选择相对自身当前 serial 为**有效前进**（advance）的值；相等、倒退、相距恰好 `2^31`（不可比较）的上游一律忽略，节点自身（自环）也不构成前进；
- 候选值按模 `2^32` **前向距离最小**取胜；距离并列时按**上游节点名**字典序最小者取胜；
- **所有节点依据同一份上一轮快照同时更新**；没有任何可接受上游的节点保持不变；
- 某一轮没有任何节点更新时即视为**稳定（收敛）**，立即停止；每次被选中的更新都是严格有效前进，因此即便存在环路也必然在有限步内稳定。

```bash
curl -s -X POST http://localhost:8000/api/zones/example.com/transfer-simulations \
  -H 'Content-Type: application/json' \
  -d '{
        "nodes": [
          {"name": "s", "serial": 100},
          {"name": "a", "serial": 90},
          {"name": "b", "serial": 80}
        ],
        "edges": [
          {"from": "s", "to": "a"},
          {"from": "a", "to": "b"}
        ],
        "source": "s",
        "publish_serial": 110,
        "max_rounds": 10
      }'
```

```json
{
  "zone": "example.com",
  "source": "s",
  "publish_serial": 110,
  "max_rounds": 10,
  "sync_rounds_run": 2,
  "converged": true,
  "rounds": [
    {"round": 0, "changes": [
      {"node": "s", "previous_serial": 100, "serial": 110, "upstream": null, "forward_distance": 10}
    ]},
    {"round": 1, "changes": [
      {"node": "a", "previous_serial": 90, "serial": 110, "upstream": "s", "forward_distance": 20},
      {"node": "b", "previous_serial": 80, "serial": 90,  "upstream": "a", "forward_distance": 10}
    ]},
    {"round": 2, "changes": [
      {"node": "b", "previous_serial": 90, "serial": 110, "upstream": "a", "forward_distance": 20}
    ]}
  ],
  "snapshot": [
    {"name": "s", "serial": 110},
    {"name": "a", "serial": 110},
    {"name": "b", "serial": 110}
  ],
  "unconverged": [],
  "note": "预演结果仅用于传送收敛分析，不写入数据库；第 0 轮为源节点发布切换，其后各轮所有节点仅依据上一轮快照同时更新"
}
```

- `rounds[]` 为逐轮变更（含第 0 轮，同步轮变更按节点名字典序排列）；每项变更给出 `previous_serial`（上一轮快照值）、更新后 `serial`、取值来源 `upstream`（第 0 轮为 `null`）与 `forward_distance`；
- `snapshot` 是收敛时（409 时为达到上限时）各节点 serial 快照，按请求节点顺序排列；`unconverged` 列出快照中 serial 尚未等于发布值的节点（例如拓扑上不可达、或所有上游值相对它都不构成前进的节点）；
- `sync_rounds_run` 为实际执行的同步轮数（不含第 0 轮与无更新的终止探测轮）；系统可能在**没有任何节点可更新**时稳定，但仍有节点未拿到发布值，此时 `converged: true` 而 `unconverged` 非空；
- 执行满 `max_rounds` 个同步轮次后，若依据当前快照**仍有节点可以继续更新**，返回 **409**：`detail.reason` 为 `max_rounds_exceeded`，并携带与 200 相同结构的逐轮记录与当前快照，另附 `updatable_nodes`（下一轮仍可更新的节点名）。下例链长 2 而 `max_rounds=1`，第 1 轮后 `b` 尚可从 `a` 的新值前进：

```json
{
  "detail": {
    "reason": "max_rounds_exceeded",
    "message": "达到同步轮次上限 1 后仍有 1 个节点可以继续更新，无法在限内确认收敛",
    "zone": "example.com",
    "source": "s",
    "publish_serial": 110,
    "max_rounds": 1,
    "sync_rounds_run": 1,
    "converged": false,
    "rounds": [ {"round": 0, "changes": [ "...同前..." ]},
                {"round": 1, "changes": [ "...a 到达 110、b 仅到 90..." ]} ],
    "snapshot": [
      {"name": "s", "serial": 110},
      {"name": "a", "serial": 110},
      {"name": "b", "serial": 90}
    ],
    "unconverged": [{"name": "b", "serial": 90}],
    "updatable_nodes": ["b"]
  }
}
```

- 若最后一个被允许的同步轮恰好完成传播（再探测一轮已无节点可更新），仍按正常收敛返回 **200**；
- 节点/边引用未声明节点、源节点不存在、节点名重复或空白、`serial`/`publish_serial` 越界、发布值相对源节点非前进、`max_rounds` 越界等请求问题返回 **422**；路径中区域名非法返回 **400**。

### 多源观测 serial 时间线重建

`POST /api/zones/{zone}/serial-timelines` 用于汇总多个采集源（如各地 DNS 探测点）在不同时刻观测到的区域 SOA serial，重建一条单调有效的 serial 时间线。结果**即时计算、不写入数据库**（不会产生审计或预留记录）。

请求体：

| 字段 | 说明 |
| --- | --- |
| `observations` | 观测列表（至少 1 条、至多 4096 条）；每项含 `collector`（采集源名，去除首尾空白）、`observed_at`（ISO 8601 时间，不带时区偏移时按 UTC 处理）与 `serial`（`0..4294967295`） |
| `bucket_seconds` | 时间桶大小（正整数秒）；观测按 `floor(observed_at / bucket_seconds)` 分桶，没有观测的空桶不出现在结果中 |

处理规则：

1. **桶内源内归并**：同一采集源在同一桶内仅保留 `observed_at` 最新的一条观测（更早的观测整体作废）；若其最新时刻同时上报了**不同 serial**（采集源自身数据矛盾），返回 **422**，`detail.conflicts` 列出冲突桶、采集源与冲突 serial；完全重复的观测（同源同时刻同 serial）按一条处理；
2. **桶候选值**：桶内每个不同 serial 构成一个候选，最新观测等于该值的采集源为 `supporters`，其余采集源为 `dissenters`；
3. **动态规划选线**：相邻时间桶选值之间只允许 RFC 1982 **相等（equal）或有效前进（advance）**，倒退（regression）与相距恰好 `2^31` 的不可比较（incomparable）路径一律禁止。优化目标依次为：
   - **最大化总支持数**：各桶 `supporters` 数量之和；
   - **最少跳变**：相邻桶选值不同（advance）的次数最少（相等不产生跳变）；
   - **字典序**：以上完全并列时，按整条 serial 序列（普通整数序）字典序升序。

```bash
curl -s -X POST http://localhost:8000/api/zones/example.com/serial-timelines \
  -H 'Content-Type: application/json' \
  -d '{
        "bucket_seconds": 300,
        "observations": [
          {"collector": "probe-hkg", "observed_at": "2026-09-19T08:00:10Z", "serial": 2026091900},
          {"collector": "probe-fra", "observed_at": "2026-09-19T08:01:00Z", "serial": 2026091900},
          {"collector": "probe-sin", "observed_at": "2026-09-19T08:02:00Z", "serial": 2026091890},
          {"collector": "probe-hkg", "observed_at": "2026-09-19T08:06:00Z", "serial": 2026091901},
          {"collector": "probe-fra", "observed_at": "2026-09-19T08:07:00Z", "serial": 2026091901},
          {"collector": "probe-sin", "observed_at": "2026-09-19T08:08:00Z", "serial": 2026091900}
        ]
      }'
```

```json
{
  "zone": "example.com",
  "bucket_seconds": 300,
  "buckets": [
    {
      "index": 0,
      "bucket_start": "2026-09-19T08:00:00Z",
      "bucket_end": "2026-09-19T08:05:00Z",
      "serial": 2026091900,
      "supporters": ["probe-fra", "probe-hkg"],
      "dissenters": ["probe-sin"],
      "candidates": [
        {"serial": 2026091890, "supporters": ["probe-sin"], "dissenters": ["probe-fra", "probe-hkg"]},
        {"serial": 2026091900, "supporters": ["probe-fra", "probe-hkg"], "dissenters": ["probe-sin"]}
      ]
    },
    {
      "index": 1,
      "bucket_start": "2026-09-19T08:05:00Z",
      "bucket_end": "2026-09-19T08:10:00Z",
      "serial": 2026091901,
      "supporters": ["probe-fra", "probe-hkg"],
      "dissenters": ["probe-sin"],
      "candidates": [
        {"serial": 2026091900, "supporters": ["probe-sin"], "dissenters": ["probe-fra", "probe-hkg"]},
        {"serial": 2026091901, "supporters": ["probe-fra", "probe-hkg"], "dissenters": ["probe-sin"]}
      ]
    }
  ],
  "timeline": [2026091900, 2026091901],
  "total_support": 4,
  "changes": 1,
  "note": "时间线仅依据请求中的多源观测即时重建，不写入数据库；相邻时间桶选值只允许 RFC 1982 相等或有效前进，目标依次为最大化采集源支持数、最少跳变、serial 序列字典序升序"
}
```

- `buckets[]` 按时间升序列出所有**非空**桶（空桶自动跳过，`index` 为非空桶序号），每桶给出选中的 `serial`、`supporters`（按名字典序）、`dissenters`，以及桶内全部 `candidates`（serial 升序）以便核对；
- `timeline` 为各桶选值，`total_support` 为各桶支持采集源数之和，`changes` 为相邻桶选值不同的跳变次数；
- **时间线无法延续（409）**：某桶的全部候选相对上一桶所有可达选值都是倒退或不可比较时，返回 **409**，`detail.reason` 为 `unextendable_timeline`，并给出 `conflict_bucket`（首个断点）、各候选的 `supporters`/`dissenters` 及相对上一桶可达值的 `predecessor_relations`（全部为 `regression`/`incomparable`），以及按相同目标选出的最优前缀 `prefix_buckets` / `prefix_timeline` / `prefix_total_support` / `prefix_changes`：

```json
{
  "detail": {
    "reason": "unextendable_timeline",
    "message": "第 1 个时间桶（2026-09-19T08:05:00Z–2026-09-19T08:10:00Z）中不存在相对上一桶可达选值为 RFC 1982 相等或有效前进的候选 serial，时间线无法继续延续",
    "zone": "example.com",
    "bucket_seconds": 300,
    "conflict_bucket": {"index": 1, "bucket_start": "2026-09-19T08:05:00Z", "bucket_end": "2026-09-19T08:10:00Z"},
    "candidates": [
      {"serial": 50, "supporters": ["a"], "dissenters": [],
       "predecessor_relations": [{"serial": 100, "relation": "regression"}]}
    ],
    "prefix_buckets": [ {"index": 0, "serial": 100, "supporters": ["a"], "dissenters": [], "...": "..."} ],
    "prefix_timeline": [100],
    "prefix_total_support": 1,
    "prefix_changes": 0
  }
}
```

- **桶内最新观测冲突（422）**：同一采集源在桶内的最新时刻同时出现不同 serial 时返回 **422**，`detail.reason` 为 `conflicting_latest_observation`，`detail.conflicts` 给出每个冲突的桶位置、采集源与 serial 集合（按桶序号与采集源名排序）；
- `4294967295 → 0` 这类回绕观测属于有效前进，可正常跨桶延续；`bucket_seconds` 必须为正整数，观测列表不能为空，`serial` 越界、时间格式非法、采集源名为空白等请求问题返回 **422**；路径中区域名非法返回 **400**。

### 权威快照一致性检查

`POST /api/zones/{zone}/snapshot-consistency` 用于多权威（multi-primary/anycast）部署下的**发布前快照核对**：提交若干权威节点各自上报的 RRset 快照与最小同意节点数 `min_agreeing`，服务端按 RRset 计算多数共识，返回**可发布的共识快照**、**逐节点差异**与**冲突 RRset**。结果**仅即时计算，不写入数据库**（不会产生审计或预留记录）。

请求体：

| 字段 | 说明 |
| --- | --- |
| `nodes` | 节点列表（至少 1 个、至多 256 个）；每项含 `name`（节点名，去除首尾空白后须在请求内唯一）与 `rrsets`（该节点上报的 RRset 列表，可为空、至多 4096 条） |
| `min_agreeing` | RRset 达成共识所需的最小同意节点数（`1..节点数`，超过节点数返回 422）；**唯一最高票**且同意节点数 ≥ 该值的取值才构成共识 |

每条 RRset 快照字段：`owner`（属主名）、`type`（记录类型）、`class`（记录类）、`ttl`（`0..2147483647`，RFC 2181 的 31 位非负整数）、`rdata`（记录数据列表，至少 1 条）。规范化规则：

- `owner` 去首尾空白、去末尾根点并转小写（允许 `_dmarc`、`_sip._tcp` 这类前导下划线标签）；`type` / `class` 去首尾空白并转大写；
- `rdata` 逐条去首尾空白（空白串报错）、去重并按字典序排序——**记录顺序不影响一致性判定**；rdata 按字符串精确比较，不做类型感知的大小写归一；
- 同一节点内 `(owner, type, class)` 键重复返回 **422**。

共识规则：检查域为所有节点上报 RRset 键的并集；每个节点对每个键要么持有一个取值 `(ttl, rdata 集合)`，要么**缺席**——缺席同样作为一种"取值"参与计票，因此"多数节点都没有该 RRset"也能形成共识（该键不进入可发布快照）。对每个键统计各取值的同意节点数：

- 某一取值**唯一最高票**且票数 ≥ `min_agreeing` → 该值为共识；共识为存在的键进入可发布快照，共识为缺席的键不进入快照（仍上报了该键的节点记为 `extra` 差异）；
- 最高票**未达门槛**（`cause=below_threshold`）或达到门槛但**多值并列**（`cause=tied`，如 4 节点 2-2 分裂、`min_agreeing=2`）→ 该键冲突；只要存在任一冲突键即返回 **409**。

```bash
curl -s -X POST http://localhost:8000/api/zones/example.com/snapshot-consistency \
  -H 'Content-Type: application/json' \
  -d '{
        "min_agreeing": 2,
        "nodes": [
          {"name": "ns1", "rrsets": [
            {"owner": "example.com", "type": "SOA", "class": "IN", "ttl": 3600,
             "rdata": ["ns1.example.com. hostmaster.example.com. 2026091901 7200 3600 1209600 300"]},
            {"owner": "www.example.com", "type": "A", "class": "IN", "ttl": 300,
             "rdata": ["192.0.2.1", "192.0.2.2"]}
          ]},
          {"name": "ns2", "rrsets": [
            {"owner": "example.com.", "type": "soa", "class": "in", "ttl": 3600,
             "rdata": ["ns1.example.com. hostmaster.example.com. 2026091901 7200 3600 1209600 300"]},
            {"owner": "WWW.EXAMPLE.COM.", "type": "A", "class": "IN", "ttl": 300,
             "rdata": ["192.0.2.2", "192.0.2.1"]}
          ]},
          {"name": "ns3", "rrsets": [
            {"owner": "example.com", "type": "SOA", "class": "IN", "ttl": 3600,
             "rdata": ["ns1.example.com. hostmaster.example.com. 2026091901 7200 3600 1209600 300"]},
            {"owner": "www.example.com", "type": "A", "class": "IN", "ttl": 600,
             "rdata": ["192.0.2.1", "192.0.2.2"]}
          ]}
        ]
      }'
```

```json
{
  "zone": "example.com",
  "min_agreeing": 2,
  "node_count": 3,
  "snapshot": [
    {"owner": "example.com", "type": "SOA", "class": "IN", "ttl": 3600,
     "rdata": ["ns1.example.com. hostmaster.example.com. 2026091901 7200 3600 1209600 300"],
     "supporters": ["ns1", "ns2", "ns3"], "dissenters": []},
    {"owner": "www.example.com", "type": "A", "class": "IN", "ttl": 300,
     "rdata": ["192.0.2.1", "192.0.2.2"],
     "supporters": ["ns1", "ns2"], "dissenters": ["ns3"]}
  ],
  "nodes": [
    {"name": "ns1", "consistent": true,  "diffs": []},
    {"name": "ns2", "consistent": true,  "diffs": []},
    {"name": "ns3", "consistent": false, "diffs": [
      {"owner": "www.example.com", "type": "A", "class": "IN", "diff": "mismatch",
       "reported":  {"ttl": 600, "rdata": ["192.0.2.1", "192.0.2.2"]},
       "consensus": {"ttl": 300, "rdata": ["192.0.2.1", "192.0.2.2"]}}
    ]}
  ],
  "conflicts": [],
  "note": "共识快照仅依据本次请求中的节点快照即时计算，不写入数据库；RRset 以 (owner, type, class) 为键，属主名规范化为小写无尾点、类型与类大写、rdata 去重并按字典序排序；共识值为唯一最高票且同意节点数达到 min_agreeing 的取值"
}
```

- `snapshot[]` 按 `(owner, type, class)` 字典序排列，每项给出共识取值与支持/异议节点（`supporters`/`dissenters` 按名字典序）；上例中 ns2 的大小写、末尾根点与 rdata 顺序差异被规范化消除，ns3 的 TTL 不同则构成 `mismatch`；
- `nodes[]` 按请求顺序给出逐节点差异：`mismatch`（取值不同，`reported`/`consensus` 双方取值）、`missing`（节点未上报共识存在的 RRset，`reported` 为 `null`）、`extra`（节点上报了共识为不存在的 RRset，`consensus` 为 `null`）；差异仅覆盖**已达成共识**的键，冲突键的逐节点立场见 `conflicts`；
- **冲突（409）**：任一 RRset 无法达成唯一多数共识时返回 **409**，`detail.reason` 为 `no_rrset_consensus`，`detail` 同时携带已达成共识的 `snapshot`/`nodes`，以及每个冲突键的取值投票分布（`values[].supporters` 按支持数降序、`absent` 为未上报节点名单）：

```json
{
  "detail": {
    "reason": "no_rrset_consensus",
    "message": "1 个 RRset 未达成唯一多数共识（要求唯一最高票且同意节点数 ≥ 2）",
    "zone": "example.com",
    "min_agreeing": 2,
    "node_count": 3,
    "snapshot": [ "...已达成共识的 RRset..." ],
    "nodes": [ "...逐节点差异..." ],
    "conflicts": [
      {"owner": "www.example.com", "type": "A", "class": "IN", "cause": "below_threshold",
       "values": [
         {"ttl": 300, "rdata": ["192.0.2.1"], "supporters": ["ns1"]},
         {"ttl": 300, "rdata": ["192.0.2.2"], "supporters": ["ns2"]}
       ],
       "absent": ["ns3"]}
    ]
  }
}
```

- 节点名重复/空白、`min_agreeing` 越界（小于 1 或大于节点数）、节点内 RRset 键重复、`rdata` 为空列表或含空白串、`ttl` 越界、属主/类型/类非法等请求问题返回 **422**；路径中区域名非法返回 **400**。

## 判定规则（RFC 1982）

serial 是模 `2^32` 环形空间上的值。设前向距离 `d = (current - previous) mod 2^32`：

| 关系 | 条件 | 从服务器行为 |
| --- | --- | --- |
| `advance` | `1 ≤ d ≤ 2^31 - 1` | 接受更新 |
| `equal` | `d = 0` | 不触发传输 |
| `regression` | `2^31 + 1 ≤ d ≤ 2^32 - 1` | **拒绝更新**（风险标记） |
| `incomparable` | `d = 2^31` | 行为不确定（风险标记） |

因此 `4294967295 → 0` 属于前进（`d = 1`），而 `0 → 2147483648` 不可比较。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DNS_AUDIT_DB` | `audits.db`（容器内 `/data/audits.db`） | SQLite 数据库文件路径 |

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

## 项目结构

```
app/
  main.py      # FastAPI 应用与路由（启动时初始化数据库）
  rfc1982.py   # RFC 1982 序列号算术
  suggest.py   # YYYYMMDDnn 下一序列号建议
  repair.py    # 序列修复预演（不落库）
  release_plan.py  # 多权威节点共同发布规划（不落库）
  transfer.py  # 区域传送收敛预演（不落库）
  timeline.py  # 多源观测 serial 时间线重建（不落库）
  snapshot.py  # 权威快照一致性检查（不落库）
  service.py   # 逐项判定与汇总
  db.py        # SQLite 持久层（审计记录 + serial 预留的事务化分配与查询）
  schemas.py   # 请求/响应模型与校验
tests/
  test_api.py  # API、算术与并发预留测试
```
