# 客户事实重键工具 (Fact Re-Key Tool)

身份拆分/合并**已被主数据人员批准**之后，本工具判断仓库里**已有事实携带的
信息是否足够**把数据搬到新客户粒度。它**不推断**两个成员是不是同一个人——
那是映射版本已经决定的事；它只做一件事：对每个指标、每个分片，判断现有
事实能不能合法地重键，能就算，不能就标 `blocked` 并写明缺什么。

纯 Python 标准库 + SQLite。所有分组、`sum/cnt/min`、ticket 集合归并都在
SQLite 内（可落盘）完成，Python 只做编排，**从不把完整 ticket 集合取进
内存**。输入库以 `mode=ro` 只读挂载，永不修改；结果写入全新文件，临时文件
成功后才原子 `rename`，失败不留下可读取的成品。

## 目录

```
rekey/                 工具包
  schema.py            输入/输出 SQLite 表契约 (DDL)
  checks.py            目录与输入的全部校验（只读连接）
  engine.py            重键引擎（落盘分组、集合归并、阻断判定、原子交付）
  preview.py           重键预览（写临时库，看完即删）
  explain.py           逐格解释（结果格 + 血缘 + 缺失信息）
  cli.py               check / preview / build / explain 四个接口
rekey_run.py           隔离模式启动器（python -I）
tests/
  scenarios.py         主演练输入生成（与引擎无关）
  edge_cases.py        边界与大文件输入生成
  naive.py             独立朴素 Python 核对器（不调用引擎 SQL）
  engine_mem.py        子进程内只跑引擎，测引擎自身内存
  run_tests.py         89 条断言 + 手写预期 + 朴素枚举核对
data/                  运行产物（输入库与结果库）
```

## 数据契约

成员身份处处是复合键 `(source, member_id)`。`mapping` 两版（`version`）
成员全集相同，一版内每个成员恰属一个客户。

`catalog_shard` 为每个事实分片声明：形态 `form`、日期粒度 `date_grain`、
指标 `metric`、闭区间覆盖段 `(src_source, seq_start, seq_end)`。一段不跨
来源；同一指标的不同分片覆盖段不得相交（相邻 `end+1=start` 允许）。段内
允许序号空缺。

三种事实形态：

| form | 表 | 保留的信息 |
|---|---|---|
| `atomic` | `atomic_fact` | 逐笔贡献：source/seq（须落在覆盖段内）、成员、日期、整数 `amount`（可空）、可空 `ticket`（空串 `''` 是有效对象）、物理贡献身份 |
| `full_summary` | `summary_contribution` | 保留每笔贡献 amount/ticket 的成员级汇总 + 参与成员集合 |
| `old_summary` | `old_summary_cell` | 只有旧客户/日期/指标、sum/cnt/min、一个去重计数和参与成员集合；可选 `old_summary_ticket_set` 给出**完整**业务对象集合 |

没有完整集合时，旧汇总只有一个去重**计数**，不能假称保留了明细。

## 恢复判定（核心）

**聚合三兄弟 sum / 非 null 数量 / min：**

- `atomic` 与 `full_summary` 分片：按新成员映射**重新分组**，完全可重建。
  完全同值的不同贡献仍是不同事实（靠 `physical_id`/行号区分，计数为 2）。
- `old_summary` 单元，其参与成员**全部迁到同一新客户**：sum/数量/min
  **整体搬运**（`carried`），绝不按人数平分。
- `old_summary` 单元的成员被**拆开**而无贡献明细：该单元触及的每个目标格
  聚合部分标 `blocked`，列出分片与缺失信息；**绝不生成 15/15**。即使另一
  分片有可算的部分，也不把可算部分冒充完整指标（整格 blocked），但可算
  贡献仍保留在血缘里，且不连累其它指标/去重数。

**ticket 精确不同值数：**

- 明细分片与带完整集合的旧汇总：集合在 SQLite 内归并，跨分片相同 ticket
  只计一次（`rebuilt`）。
- 旧汇总只有计数时：该计数**仅当它是该格唯一的去重来源**才能搬运
  （`carried`）。两个计数相加、计数与精确集合混用、计数与明细 ticket 混用
  一律 `blocked`——计数可能共享对象，不能相加。
- 拆分的旧汇总：去重数 `blocked`。
- 去重数不可恢复**不会**丢掉仍可恢复的 sum。

**NULL 规则：** NULL 不参加数量/min/去重；空串是有效对象；全 NULL 的格
sum 与 min 为 NULL、数量为 0。所有整数运算保持 INTEGER，不经 float。

## 输出表（新进程可直接读取）

- `fact_cell_new`：`(new_customer, date_key, metric)` 的 sum/数量/min/
  去重数，以及 `agg_status`、`distinct_status`
  （`rebuilt|carried|blocked|none`）。
- `blocked_reason`：每个被阻断部分的分片与缺失信息。
- `metric_shard_status`：各指标使用了哪些来源分片、各自恢复状态。
- `cell_lineage`：逐笔贡献 / 搬运 / 阻断血缘，供逐格解释。
- `cell_ticket_set`：存活的精确 ticket 集合（`WITHOUT ROWID`，留在库内）。
- `catalog_snapshot`、`run_meta`：目录快照、两版映射版本号、输入哈希、
  `status=complete`。

## 运行命令（均用 `python -I` 隔离模式，路径作参数）

```bash
# 目录检查
python -I rekey_run.py check   data/tests/cd_noset.db

# 重键预览（不落地成品）
python -I rekey_run.py preview data/tests/cd_noset.db

# 构建新客户粒度结果
python -I rekey_run.py build   data/tests/cd_noset.db data/out.db
python -I rekey_run.py build   data/tests/large.db data/big.out.db --cache-mib 1

# 逐格解释 / 列出全部格
python -I rekey_run.py explain data/out.db
python -I rekey_run.py explain data/out.db --customer M --date 2026-01-01 --metric sales

# 测试（手写预期 + 独立朴素枚举核对 + 200 万行落盘）
python -I tests/run_tests.py
```

## 主演练（预期为手写，不由重键函数生成）

1. 旧客户 A 成员 a/b 各 amount 10/20、ticket 同为 T，拆成 P/Q：
   有明细时 P sum=10、Q sum=20，去重各 1；再合并到 R：sum=30、去重=1。
2. 只交 A 的 sum=30 与成员 `{a,b}`：拆分后两格聚合与去重**均 blocked**，
   不产生 15/15。
3. 旧客户 C/D 各 sum=5、去重=1 且只有汇总：合并后 sum=10 可用、去重
   blocked；补全两套 ticket 集合均为 `{X}` 后去重重建为 1。
4. 信息不足证明：两份原始小数据（a/b = 10/20 与 20/10）产生**逐字段相同**
   的旧汇总（sum=30、cnt=2、min=10、成员 a,b），但正确拆分答案相反——
   证明客户粒度汇总无法支撑拆分，这正是场景 2 必须 blocked 的原因。

边界覆盖：部分分片可算但整格 blocked、全 NULL、空串 ticket、同值不同事实、
同成员号跨来源、分片重叠整次失败且不留成品、输入次序改变结果不变、
200 万行大文件落盘且引擎自身内存有界。
