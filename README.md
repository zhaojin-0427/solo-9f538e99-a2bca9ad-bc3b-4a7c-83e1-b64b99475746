# 冷链温区账本

用于冷库监管核对「已出具月报为何把某批货计入冷藏或冷冻」的网页应用。
出库事实按 UTC 月、按仓一次性冻结；温区认定是一份只追加、全仓共用的知识账本，
每次整组更正生成新的知识版号。补登过去生效的区间只影响新版汇总，任何旧版号仍
返回它当初的统计口径。

## 多仓模型

* **各仓独立冻结 UTC 月结**：上传页可填仓号（字母/数字/`-`/`_`，留空为默认仓
  `default`）。同一仓同一 UTC 月只能冻结一份；不同仓的同月文件分别入账，互不影响。
* **流水号按仓唯一**：同仓流水号跨月仍须唯一；异仓之间允许重号，例如两座冷库的
  三月出库 CSV 里都可以有流水 `001`。
* **并发安全**：并发提交同仓同月的合规文件，只有一份能冻结成功，其余返回 409；
  失败时月结与流水均不落账。
* **温区知识全仓共用**：知识版号不区分仓号；月报可选仓号、月份与共用的知识版号，
  箱数与明细只统计所选仓，明细同时标明仓号和流水号。
* **旧数据原地升级**：已有 SQLite 数据卷直接重启即自动迁移，旧月结、流水、来源
  文件名与行号全部归入默认仓；不带仓号的旧链接和 API 继续读写默认仓，响应字段
  只增不删。升级在单个事务内完成，中断后用原卷重启会整体重试，旧记录不丢不重。

## 快速启动（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据保存在名为 `coldchain-data` 的卷中（容器内 `/data/coldchain.sqlite3`），
`docker compose restart` 或重新 `up` 之后旧版号与旧事实均可继续追查。
可通过环境变量改端口/密钥：

```bash
COLDCHAIN_PORT=9000 COLDCHAIN_SECRET='...' docker compose up --build -d
```

## 本地运行（不使用容器）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python run.py                       # http://127.0.0.1:8000
# 或
COLDCHAIN_DB=/tmp/cc.sqlite3 gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 run:app
```

## 使用流程

1. **上传月结**：填写仓号（留空=默认仓）并上传单月出库 CSV。列为
   `流水号,SKU,出库时刻,箱数`（也接受常见别名，如
   `出库流水号 / 商品编码 / 出库时间 / 件数`）。时刻为 ISO-8601，可带 `Z` 或偏移量，
   统一换算为 UTC；文件必须同属一个 UTC 月份，同仓流水号跨月唯一，箱数为正整数。
   校验失败时**一行都不会写入**；成功后该仓该月事实永久固定，同仓重复上传返回 409，
   异仓同月可分别入账。
2. **登记温区**：按 SKU 登记 `[生效, 失效)` 区间（起含止不含），失效留空表示持续有效。
   同一版号内同一 SKU 的区间不得重叠；整组提交即整组替换，并生成新知识版号。
   温区知识版号全仓共用。
3. **核对月报**：选择仓号、月份和知识版号（留空=最新版），查看该仓各温区箱数、
   每笔对应流水（含仓号与流水号）及其依据区间，以及未归类流水。空档期间的出库
   自动列为未归类。

示例文件：`examples/2026-03.csv`（可用 `python examples/make_example.py` 重新生成）。

## 关键规则如何落实

| 要求 | 实现 |
| --- | --- |
| 上传成功后固定该仓该月事实 | `months` 主键为 `(warehouse, month)`，与 `shipments` 在同一 `BEGIN IMMEDIATE` 事务写入；触发器禁止 `UPDATE/DELETE`；同仓同月重复上传返回 409 |
| 异仓同月同号分别入账、同仓流水跨月唯一 | `shipments` 唯一约束为 `UNIQUE(warehouse, sequence_no)`；应用层与数据库双重校验 |
| 并发提交同仓同月只冻结一份 | `BEGIN IMMEDIATE` 串行化写入，后来者在事务内查到已存在的 `(仓号, 月份)` 即 409，失败整体回滚 |
| 区间起含止不含 | 所有时点存为 UTC 微秒整数，判定 `valid_from_us <= t < valid_to_us` |
| 同版同 SKU 不重叠 | 应用层排序校验 + SQLite 触发器 `ranges_no_overlap` 双重保护 |
| 空档出库未归类 | `classify()` 返回空即为未归类，与已归类箱数之和恒等于该仓月总箱数 |
| 每次整组更正生成新版号 | 新版本 = 其他 SKU 区间原样带入 + 目标 SKU 整组替换；版本与区间同事务写入 |
| 补登过去只改新版 | 版本只追加、不修改；查询按选定版号读取该版区间快照 |
| 更正/查询交错不混用新旧 | 写用 `BEGIN IMMEDIATE` 串行化；查询在一个读事务里同时取月份事实、版号与区间 |
| 非法/失败不产生半版 | 校验先于写入；任何异常整体回滚；版本号和区间在同一事务提交 |
| 旧卷原地升级、旧记录归入默认仓 | 检测到 v1 表结构即在单事务内改名旧表、建新表、整表复制到仓号 `default`（保留来源文件名与行号）；中断回滚，重启重试，不丢不重 |
| 旧链接/旧 API 兼容 | 缺少 `warehouse` 参数一律解析为 `default`；原响应字段保留，仅新增 `warehouse` 字段 |
| 重启后旧版可追查 | 全部状态在 SQLite（WAL，`synchronous=FULL`），容器卷持久化 |

并发更正采用「基准版号」乐观并发控制：提交时必须声明所依据的版本号，若已不是
最新版则返回 409 要求刷新重试，因此不会把两次不同更正悄悄合并或覆盖。

## HTTP 接口

所有接口都接受可选的 `warehouse` 参数（查询串或表单字段；JSON 版号接口与仓号
无关）。缺省或留空时解析为默认仓 `default`，因此不带仓号的旧链接和 API 行为与
升级前完全一致。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/warehouses` | 仓号清单及默认仓号 |
| GET | `/api/months?warehouse=WH-A` | 该仓已固定月份列表（缺省=默认仓） |
| POST | `/api/months` | multipart 上传 `file`，可选表单字段 `month` 与 `warehouse` |
| GET | `/api/versions` | 知识版号列表（全仓共用） |
| POST | `/api/versions` | `{"sku","intervals":[{"zone","valid_from","valid_to"}],"reason","base_version"}` |
| GET | `/api/versions/<n>` | 某版全部 SKU 区间 |
| GET | `/api/report?warehouse=WH-A&month=YYYY-MM&version=<n>` | 该仓月报（省略 `version` 用最新版，省略 `warehouse` 用默认仓） |
| GET | `/api/audit` | 最近 200 条审计记录 |
| GET | `/healthz` | 健康检查 |

上传与月报响应在保留全部原有字段的基础上新增顶层 `warehouse`，月报中每条流水
明细也带有 `warehouse` 与 `sequence_no`。`base_version` 为 `null` 仅允许用于空账本
创建 v1；否则必须是当前最新版号。

## 数据卷升级（v1 → 多仓）

旧版数据库没有仓号概念。启动时若检测到旧表结构（`months` 缺少 `warehouse` 列），
应用会在**一个 `BEGIN IMMEDIATE` 事务**内完成：改名旧表 → 建立带仓号的新表 →
把旧月结与流水整表复制到默认仓 `default`（保留 `source_filename`、`source_row`
与原始行 id）→ 重建不可变触发器。SQLite 的 DDL 同样受事务保护，升级中途崩溃会
整体回滚，再次启动时用原卷重试；迁移成功后幂等，重复启动不会复制第二遍。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：月结冻结与重复拒绝、CSV 各类非法输入、起含止不含边界、空档未归类、
重叠/倒序区间拒绝、补登不改旧版、基准版号冲突、并发更正串行化、查询快照一致、
重启后旧版可追查、数据库层不可变触发器等；多仓部分覆盖异仓同月同号分别入账、
同仓流水跨月唯一、同仓同月并发只冻结一份且失败不落账、月报按仓取数并标明仓号、
旧链接读写默认仓、旧卷事务化升级（含中断回滚与重启恢复、不丢不重）。

## 目录

```
coldchain/
  app.py         Flask 路由与页面/API
  facts.py       月结 CSV 校验与按仓冻结
  knowledge.py   温区版本、区间校验、分类与按仓报表
  warehouses.py  仓号校验、默认仓与仓号注册表
  db.py          SQLite schema、触发器、事务与旧卷迁移
  timeutil.py    UTC 解析与规范化
  templates/     页面
tests/           单元、并发与迁移测试
examples/        示例月结 CSV
Dockerfile, docker-compose.yml
```
