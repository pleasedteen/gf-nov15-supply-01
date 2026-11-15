# qrelease — 离线质量放行本地工具

面向质量员的纯本地命令行工具：分批导入工艺路线、工单、设备合格有效期和
生产结果证据，逐件追溯“为什么能放行 / 还缺什么”，并办理整单放行。
全程离线运行，仅依赖 Python 3.10+ 标准库，数据保存在本地单个 SQLite 文件中，
关闭重开后可继续导入、查询和放行。

## 1. 运行方式

```bash
# 在仓库根目录执行（-I 为隔离模式，不加载任何第三方目录中的代码）
python3 -I qrelease/__main__.py --db ./qrelease.db <命令> ...
```

- `--db` 指定数据库文件；默认 `./qrelease.db`，也可用环境变量 `QRELEASE_DB`。
- 所有导入文件均为 UTF-8 JSON；时间统一为 ISO-8601 UTC（建议以 `Z` 结尾）。
- 资料不合规时进程以退出码 2 结束并打印中文原因；导入类操作整份/整批回滚，
  已有资料保持原样。

## 2. 数据模型与规则摘要

### 工艺路线
- 编号 + 修订号唯一；列出全部工序和交付时必须完成的交付工序（不能为空）。
- 前置组按工序组织：`"OP30": [["OP10"], ["OP20"]]` 表示 OP30 有两个前置组，
  组间都要满足，每组从候选工序中选一道作为依据；没有前置组的工序可独立完成。
- 引用必须属于同一条路线，且不能形成任何前置依赖环；不合规的路线整份不登记。

### 工单
- 登记产品件号清单，并固定开出时采用的路线修订；之后路线改版不影响旧工单。
- 件号不能为空且不能重复。

### 结果（证据）批次
- 每条含全局唯一证据号、工单、件号、工序、正整数尝试号、UTC 执行时刻、
  通过/不通过、实际设备清单（无设备工序可为空）、按前置组给出的证据号引用。
- 同一工单/件/工序以尝试号最大的结果为当前结果；旧尝试晚到只补历史。
- 相同证据号相同内容重发只算一次；证据号相同内容不同，或同一
  工单/件/工序的同一尝试号出现两份不同结果，**整批拒绝**。
- 工单、件号、工序必须已登记且相互对应，否则同样整批拒绝。
- 前置证据未到、设备资料缺失时先收下并标记不可用，补齐后自动恢复。

### 设备合格有效期
- 每台设备可有多段有效期，含起点、不含终点；同台设备各段不得重叠。
- 更正必须提交完整新修订，修订号按 1、2、3… 递增；不合规整份拒绝，旧修订保留。

### 可用性判定（实时计算，不落地状态）
一条结果只有同时满足以下条件才**可用**：
1. 结果为通过；
2. 实际使用的每台设备在执行时刻都有合格有效期（按设备最新修订）；
3. 路线要求的每个前置组都明确引用了一个具体证据号；
4. 所引证据属于同件产品、对应要求的前置工序、是各自当前且可用的结果，
   且后工序执行时刻不得早于引用证据（时间倒置不可用）。

缺组、错件、错工序、引用未到、时间倒置等都会保留结果并给出原因码，
原因沿引用关系逐层展开，可直接看到是哪一条证据导致的不可用。

- 返工产生更高尝试号后，仍引用旧尝试的下游结果不可用，**不会自动改接**
  新证据；没有依赖这些证据的其他分支照常使用。
- 设备有效期更正会重新影响已有结果及其下游；改正后符合条件的当前结果
  可以重新可用。

### 整单放行与自动撤销
- 只有工单内每件产品的全部交付工序当前都可用，才能整单放行；
  失败时不生成部分放行单。
- 正式放行按当时资料重查，成功后生成单号 `RO-000001` 形式，并冻结当时的
  路线、交付结果、逐层引用证据闭包、设备有效期修订快照。
- 此后只要单上使用的证据被替代或变得不可用，放行单自动撤销并记录触发原因；
  未用于该单的其他分支变化不撤销它。
- 已撤销放行单不会自动恢复；重新放行产生新单号，旧单仍可查看完整快照。
- 放行请求带请求编号：同号同内容重试沿用原结果，同号换内容拒绝。

## 3. 命令清单

| 命令 | 作用 |
| --- | --- |
| `import-routes <file>` | 导入工艺路线（JSON 数组） |
| `import-work-orders <file>` | 导入工单登记 |
| `import-equipment <file>` | 导入设备有效期完整修订 |
| `import-results <file>` | 按批导入结果/证据 |
| `query <wo_id> [--json]` | 每件各工序当前结果、可用性与逐层原因 |
| `history <wo_id> <part> <operation> [--json]` | 查看某件某工序全部尝试 |
| `release <request_id> <wo_id> [--json]` | 办理整单放行 |
| `revoke-sweep [--json]` | 手动执行放行单撤销扫描（导入后已自动执行） |
| `show-release <release_id> [--json]` | 查看放行单（含已撤销单当时快照） |
| `list-releases [wo_id] [--json]` | 列出行行记录，可按工单过滤 |
| `export <wo_id> <out_path>` | 导出当前状态、不可用原因、历史与放行记录 |
| `show-route <route_id> <revision> [--json]` | 查看已登记路线修订 |
| `show-equipment <equipment_id> [--json]` | 查看设备各修订有效期 |

## 4. 导入文件格式

### 工艺路线 `samples/routes.json`
```json
[
  {
    "route_id": "R-AXLE",
    "revision": 1,
    "operations": ["OP10", "OP20", "OP30", "OP40"],
    "delivery_operations": ["OP30", "OP40"],
    "prereq_groups": {
      "OP20": [["OP10"]],
      "OP30": [["OP20"]],
      "OP40": [["OP20"], ["OP30"]]
    }
  }
]
```

### 工单 `samples/work_orders.json`
```json
[
  {"wo_id": "WO-2026-001", "route_id": "R-AXLE",
   "route_revision": 1, "parts": ["P-001", "P-002"]}
]
```

### 设备有效期修订 `samples/equipment_r1.json`
```json
[
  {"equipment_id": "EQ-CMM-1", "revision": 1,
   "periods": [
     {"start": "2026-01-01T00:00:00Z", "end": "2026-06-01T00:00:00Z"},
     {"start": "2026-09-01T00:00:00Z", "end": "2027-01-01T00:00:00Z"}
   ]}
]
```

### 结果批次 `samples/results_batch1.json`
```json
[
  {
    "evidence_id": "EV-002",
    "wo_id": "WO-2026-001",
    "part": "P-001",
    "operation": "OP20",
    "attempt": 1,
    "executed_at": "2026-02-02T08:00:00Z",
    "passed": true,
    "equipment": [],
    "prereq_refs": {"0": "EV-001"}
  }
]
```
`prereq_refs` 的键是该工序前置组的序号（从 0 开始，按路线中的登记顺序），
值为具体证据号；没有前置组的工序给空对象 `{}`。

## 5. 典型操作序列

```bash
DB="--db ./qrelease.db"
python3 -I qrelease/__main__.py $DB import-routes samples/routes.json
python3 -I qrelease/__main__.py $DB import-work-orders samples/work_orders.json
python3 -I qrelease/__main__.py $DB import-equipment samples/equipment_r1.json
python3 -I qrelease/__main__.py $DB import-results samples/results_batch1.json
python3 -I qrelease/__main__.py $DB query WO-2026-001
python3 -I qrelease/__main__.py $DB release REQ-2026-001-A WO-2026-001
python3 -I qrelease/__main__.py $DB show-release RO-000001
python3 -I qrelease/__main__.py $DB export WO-2026-001 ./wo-001-export.json
```

返工导入（`samples/results_rework.json`）后，引用旧尝试的下游会显示
`ref_not_current` 原因，已生成的放行单自动撤销；用新请求编号在资料补齐后
重新放行，会生成新的放行单号。

## 6. 测试

```bash
python3 -I tests/test_qrelease.py
```

37 个用例覆盖：路线无环/交付清单校验、工单固定修订、批次幂等与整批拒绝、
设备半开区间与版本递增、各类不可用原因码、返工级联失效、放行快照、
自动撤销与不自动恢复、请求编号幂等、持久化重开与导出。

## 7. 不可用原因码

| 代码 | 含义 |
| --- | --- |
| `no_result` | 该件该工序还没有任何结果 |
| `result_failed` | 结果为不通过 |
| `equipment_not_registered` | 设备尚无有效期资料（资料补齐后自动恢复） |
| `equipment_expired` | 执行时刻不在任何有效期段内 |
| `missing_group_ref` | 某个前置组未明确引用证据号 |
| `extra_group_ref` | 引用了不存在的前置组序号 |
| `unexpected_refs` | 无前置组的工序却携带引用 |
| `missing_evidence` | 引用的证据尚未送到 |
| `ref_wrong_wo` / `ref_wrong_part` / `ref_wrong_operation` | 引用归属不匹配 |
| `ref_not_current` | 引用的证据已被更高尝试号替代（不自动改接） |
| `ref_unusable` | 所引证据自身不可用（`causes` 携带深层原因链） |
| `time_inversion` | 本结果执行时刻早于所引证据 |
| `reference_cycle` | 前置引用形成环 |
