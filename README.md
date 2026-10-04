# 专利权利要求依附审查 API

基于 **Python + FastAPI + Pydantic + SQLite** 的服务：按版本链记录专利权利要求修订，
按目标日期重建当日有效版本，并审查从属权利要求的依附关系（引用有效性、成环、多项引用语义）。

## 一条命令启动

```bash
docker compose up --build
```

- **API 端口：`8000`**（宿主机 `8000` → 容器 `8000`，见 `docker-compose.yml`）
- 交互式文档：http://localhost:8000/docs （Swagger UI）
- 健康检查：`GET http://localhost:8000/health`
- SQLite 数据持久化在命名卷 `patent-data`（容器内 `/data/app.db`，可用环境变量 `DATABASE_PATH` 覆盖）；
  证据包签发依据 `evidence_seals.jsonl` 与数据库同目录、随同一卷持久化，重启后新包仍可复验

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
pytest tests/ -q        # 运行测试
```

## API

### POST /applications — 提交申请及修订（幂等、事务性）

```json
{
  "request_id": "req-001",
  "application_number": "CN202610001",
  "revisions": [
    {"claim_number": 1, "version": 1, "claim_type": "independent",
     "references": [], "text": "一种装置……",
     "effective_from": "2026-01-01", "effective_to": null},
    {"claim_number": 2, "version": 1, "claim_type": "dependent",
     "references": [1], "reference_mode": "all", "text": "如权利要求1所述……",
     "effective_from": "2026-01-01", "effective_to": null}
  ]
}
```

**不可跳跃的替代链**（同一 `claim_number` 的修订）：

- 版本号必须从 1 开始、逐版本连续递增（不允许 v1 → v3 跳号）；
- 时间链必须衔接：新版本的 `effective_from` 必须等于上一版本的 `effective_to`；
- 上一版本若为开放区间（`effective_to = null`），提交新版本时会被**自动封闭**为
  `effective_to = 新版本.effective_from`，保证任意日期恰好一个有效版本。

**幂等与事务**：

- `request_id` 重复且负载一致 → 返回首次响应（`201`，响应头 `X-Idempotent-Replay: true`），不重复写入；
- `request_id` 重复但负载不同 → `409`；
- 任一条修订校验失败 → 整批回滚（`422`，附全部错误明细），**不写入部分数据**。

### POST /audit — 按目标日期审查

```json
{"application_number": "CN202610001", "target_date": "2026-09-23"}
```

处理流程：取 `effective_from <= 目标日 < effective_to` 的版本重建当日权利要求集合，然后校验：

| 问题码 | 含义 |
|---|---|
| `INVALID_REFERENCE` | 交集（`all`）引用中存在无效项：编号不存在，或目标日不在其版本有效区间内 |
| `NO_VALID_REFERENCE` | 并集（`any`）引用中**任一**引用都无效 |
| `CYCLE` | 依附关系成环（含自环），链中给出闭环路径 |
| `INDEPENDENT_WITH_REFERENCES` / `DEPENDENT_WITHOUT_REFERENCE` | 结构性问题 |

**多项引用语义**（`reference_mode`）：

- `all`（交集/累计引用，“如权利要求 1 和 2 所述”）：引用的每一项都必须有效；
- `any`（并集/择一引用，“如权利要求 1 或 2 所述”）：至少一项有效即通过。

每条问题都带**引用链**（`chain`，含各环节 `claim_number` 与 `version`，无效编号版本为 `null`），
问题列表按 `(claim_number, code, 链上编号)` **确定性排序**。响应示例：

```json
{
  "status": "fail",
  "effective_claims": [{"claim_number": 1, "version": 1, "..." : "..."}],
  "issues": [{
    "code": "CYCLE",
    "message": "依附关系成环: 3 -> 4 -> 3",
    "claim_number": 3,
    "claim_version": 1,
    "chain": [{"claim_number": 3, "version": 1},
              {"claim_number": 4, "version": 1},
              {"claim_number": 3, "version": 1}]
  }]
}
```

### POST /audit/evidence — 冻结可复验的审查证据包

请求体与 `POST /audit` 完全相同（申请号 + 目标日期）。服务按**同一套 /audit 规则**审查后，
把「目标日、有效权利要求（含版本）、问题」按**固定字段顺序**冻结为规范 UTF-8 JSON
（紧凑序列化、中文不转义），保存快照并返回 `evidence_id` 与 `SHA-256` 摘要：

```bash
curl -X POST http://localhost:8000/audit/evidence \
  -H 'Content-Type: application/json' \
  -d '{"application_number": "CN202610001", "target_date": "2026-09-23"}'
```

```json
{
  "evidence_id": "bafd97da246040fca6327cd86e77529a",
  "application_number": "CN202610001",
  "target_date": "2026-09-23",
  "sha256": "fed22522a13832e3f42b3d2849b785be7df1fa26c627d6d60f7899be7862b043"
}
```

冻结载荷字段顺序固定为：顶层 `target_date, effective_claims, issues`；
权利要求 `claim_number, version, claim_type, references, reference_mode, text, effective_from, effective_to`；
问题 `code, message, claim_number, claim_version, chain`（链节点 `claim_number, version`）。
**摘要只覆盖该冻结载荷**，不含 `evidence_id`、生成时间等易变字段；
对同一数据状态下的同一输入重复生成，摘要必然相同（每次生成仍是独立的新包，`evidence_id` 不同）。

**签发依据（封存台账）**：签发每个新包时，服务把 `(evidence_id, sha256, sealed_at)`
追加写入数据库目录下的 `evidence_seals.jsonl`（与 `app.db` 同目录、随 `patent-data`
卷持久化的只追加 JSON 行文件）。该台账**不属于任何可改写的数据库记录**，
是复验时确认「这就是签发时的原内容」的依据；重启后台账仍在，新包照常可复验。

### GET /audit/evidence/{evidence_id} — 只读复验

只读取冻结快照，**不回查修订表**。复验同时核对三项，任一不符即判不可信：

1. **冻结字节 ↔ 库存摘要**：重新计算快照字节的 SHA-256，与库内 `sha256` 比对；
2. **签发依据**：台账 `evidence_seals.jsonl` 中该 `evidence_id` 封存的原摘要必须与当前摘要一致——
   攻击者即使把库内 `snapshot` 与 `sha256` **一起**替换成另一份合法快照（两者自洽），
   也无法改写台账中的签发记录，替换内容不会被判为可信；
3. **快照须为合法 JSON**。

```bash
curl http://localhost:8000/audit/evidence/bafd97da246040fca6327cd86e77529a
```

- 三项全部通过：`verified: true`，返回 `snapshot`（冻结的目标日 / 有效权利要求 / 问题）；
- 任一项不符：仍返回 `200` 与原响应结构，但 `verified: false`、`snapshot: null`，
  并在 `integrity_error` 中报告（`digest` 字段始终为现场重算值）：

| `integrity_error.code` | 含义 |
|---|---|
| `SNAPSHOT_DIGEST_MISMATCH` | 单项篡改：重算摘要与库存摘要不一致（`stored_digest` 与 `recomputed_digest` 同时给出） |
| `EVIDENCE_SEAL_MISMATCH` | 联动篡改：库内快照与摘要被整体替换，与签发时封存的原摘要不符（或台账已损坏） |
| `EVIDENCE_SEAL_MISSING` | 无可靠签发依据（修复前签发的历史旧包，见下节） |
| `SNAPSHOT_CORRUPT` | 摘要匹配但快照不是合法 JSON |

- 证据包不存在：`404`。

```json
{
  "evidence_id": "bafd97da246040fca6327cd86e77529a",
  "application_number": "CN202610001",
  "target_date": "2026-09-23",
  "digest": "fed22522a13832e3f42b3d2849b785be7df1fa26c627d6d60f7899be7862b043",
  "stored_digest": "fed22522a13832e3f42b3d2849b785be7df1fa26c627d6d60f7899be7862b043",
  "verified": true,
  "integrity_error": null,
  "snapshot": {
    "target_date": "2026-09-23",
    "effective_claims": [{"claim_number": 1, "version": 1, "...": "..."}],
    "issues": []
  }
}
```

**后续提交新修订不会改变旧包**：旧证据包永远回放冻结时的字节与摘要
（例如冻结时为开放区间的版本，其 `effective_to` 在旧包中始终是 `null`，
即使之后的修订自动封闭了该区间）。任何持有快照的一方都可以自行复算
`SHA-256(UTF-8 紧凑 JSON)` 与返回摘要比对。

#### 历史旧包的状态与调用方式

引入封存台账**之前**签发的证据包（库内只有随机 `evidence_id` 与自洽的
`snapshot`/`sha256`，台账中没有对应记录）：

- **仍可读取**：`GET /audit/evidence/{旧 evidence_id}` 照常返回 `200` 与原有字段，
  不会变成 `404`；
- **但一律不可信**：没有可靠签发依据，不得仅凭库内摘要一致判为可信，
  响应固定为 `verified: false`、`snapshot: null`、`integrity_error.code = EVIDENCE_SEAL_MISSING`；
- 若旧包还发生了单项篡改（快照或摘要其一被改），既有的
  `SNAPSHOT_DIGEST_MISMATCH` 报告继续有效、优先返回；
- **如何获得可复验的包**：用相同请求体重发 `POST /audit/evidence` 即可签发新包——
  输入不变、同一数据状态下摘要不变，新 `evidence_id` 自签发起纳入台账保护，
  重启后仍可复验。

### 其他端点

- `GET /applications/{application_number}` — 查看某申请的完整版本链（调试/核对用）
- `GET /health`

## 项目结构

```
app/
  main.py      # FastAPI 路由与异常映射
  models.py    # Pydantic 请求/响应模型（含提交期结构校验）
  store.py     # 写入路径：幂等 + 版本链校验 + 单事务
  audit.py     # 目标日重建、引用有效性、DFS 成环检测
  evidence.py  # 证据包：固定字段顺序冻结 UTF-8 JSON、SHA-256、封存台账签发依据、只读复验/篡改报告
  db.py        # SQLite 连接与 schema（含 evidence_packages 快照表）
  errors.py    # 领域错误 → HTTP 状态码
tests/test_api.py
tests/test_evidence.py
Dockerfile / docker-compose.yml / requirements.txt
```
