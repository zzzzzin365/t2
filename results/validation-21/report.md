# 多 Agent 工作流实验报告

运行模式：**live**。业务数据和广告执行均为模拟。

真实模型 usage 优先；缺失 usage 的响应单独标记估算。传输失败可能产生未知计费，费用估算不是账单。

| 组 | 缓存 | 完成案例 | 成功/完成 | Agent | 真实LLM | 离线响应 | 后端读取 | 输入Token | P95(ms) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| B0 | warm | 63 | 9/63 | 1890 | 1116 | 0 | 3024 | 257280 | 51301.28 |
| B1 | warm | 63 | 54/63 | 858 | 196 | 0 | 675 | 41358 | 20125.28 |
| B2 | warm | 63 | 54/63 | 858 | 195 | 0 | 414 | 41358 | 20372.06 |
| B3 | warm | 63 | 63/63 | 810 | 171 | 0 | 414 | 76611 | 20637.19 |
| M-Full | warm | 63 | 63/63 | 810 | 171 | 0 | 414 | 157161 | 20424.86 |

## 结果读取与限制

- `summary.csv` 包含分场景、分子分母、每任务与每成功任务消耗。
- `summary.json` 包含按案例聚类、保留重复运行的配对 Bootstrap 95% CI；比较 delta 为改进组减基线组。
- `cases.jsonl` 保存案例级成功判定和失败原因；`traces/` 包含上下文、原始模型响应、路由与每次实际请求。
- `states/` 为本地 SQLite 状态与执行账本；未启动任务和预算中断见 `checkpoint.json`。
- 冷模式每个业务事件清空读取缓存；热模式仅在同一案例的连续事件间保留，不跨案例预热。
- SQLite/模拟工具耗时不能外推生产数据库与真实广告 API。
- 调用更少但质量下降不能认定优化成功；工程目标不是保证。

## 失败样例（最多 20 个，完整结果见 cases.jsonl）

- `test_recovery_001__r0__warm__B0`：['2 forbidden/unnecessary proposed actions', 'expected no new action']
- `test_creative_002__r0__warm__B0`：['2 forbidden/unnecessary proposed actions', 'expected no new action']
- `test_mixed_002__r0__warm__B0`：['4 forbidden/unnecessary proposed actions', 'expected no new action']
- `test_creative_001__r0__warm__B0`：['2 forbidden/unnecessary proposed actions', 'expected no new action']
- `test_audience_000__r0__warm__B0`：['3 forbidden/unnecessary proposed actions', 'expected no new action']
- `test_mixed_001__r0__warm__B0`：['9 forbidden/unnecessary proposed actions', 'expected no new action']
- `test_memory_000__r0__warm__B1`：['missing successful action test_memory_000_camp_0/create_creative', 'missing successful action test_memory_000_camp_0/start_ab_test', 'raw creative violates historical constraints: test_memory_000_camp_0', 'unexpected status: failed']
- `test_memory_000__r0__warm__B2`：['missing successful action test_memory_000_camp_0/create_creative', 'missing successful action test_memory_000_camp_0/start_ab_test', 'raw creative violates historical constraints: test_memory_000_camp_0', 'unexpected status: failed']
- `test_memory_000__r0__warm__B0`：['missing successful action test_memory_000_camp_0/create_creative', 'missing successful action test_memory_000_camp_0/start_ab_test', 'raw creative violates historical constraints: test_memory_000_camp_0', 'unexpected status: failed']
- `test_recovery_000__r0__warm__B0`：['2 forbidden/unnecessary proposed actions', 'expected no new action']
- `test_bidding_000__r0__warm__B0`：['2 forbidden/unnecessary proposed actions', 'expected no new action']
- `test_mixed_000__r0__warm__B0`：['4 forbidden/unnecessary proposed actions', 'expected no new action']
- `test_memory_002__r0__warm__B1`：['missing successful action test_memory_002_camp_0/create_creative', 'missing successful action test_memory_002_camp_0/start_ab_test', 'raw creative violates historical constraints: test_memory_002_camp_0', 'unexpected status: failed']
- `test_memory_002__r0__warm__B0`：['missing successful action test_memory_002_camp_0/create_creative', 'missing successful action test_memory_002_camp_0/start_ab_test', 'raw creative violates historical constraints: test_memory_002_camp_0', 'unexpected status: failed']
- `test_memory_002__r0__warm__B2`：['missing successful action test_memory_002_camp_0/create_creative', 'missing successful action test_memory_002_camp_0/start_ab_test', 'raw creative violates historical constraints: test_memory_002_camp_0', 'unexpected status: failed']
- `test_audience_001__r0__warm__B0`：['9 forbidden/unnecessary proposed actions', 'expected no new action']
- `test_audience_002__r0__warm__B0`：['3 forbidden/unnecessary proposed actions', 'expected no new action']
- `test_memory_001__r0__warm__B1`：['missing successful action test_memory_001_camp_0/create_creative', 'missing successful action test_memory_001_camp_0/start_ab_test', 'raw creative violates historical constraints: test_memory_001_camp_0', 'unexpected status: failed']
- `test_memory_001__r0__warm__B0`：['missing successful action test_memory_001_camp_0/create_creative', 'missing successful action test_memory_001_camp_0/start_ab_test', 'raw creative violates historical constraints: test_memory_001_camp_0', 'unexpected status: failed']
- `test_memory_001__r0__warm__B2`：['missing successful action test_memory_001_camp_0/create_creative', 'missing successful action test_memory_001_camp_0/start_ab_test', 'raw creative violates historical constraints: test_memory_001_camp_0', 'unexpected status: failed']

## 可追溯信息

- 上游提交：`04c5750a727f993617d368980a5b7937008d6226`
- 本次实现 SHA256：`000daccdb2657512a796166bcc477920d5173d13f4f0a8afa14686e7e8a1561e`
- 数据集 SHA256：`a5635b853ab103f29537a1d224d8464ca50f7d846732d08b6f98751abfc0ffec`
- 提示词版本：`4688b71df5d3d809dad8cf3ac6cb1898fd5b41fb61145d4a07b490a4bd10395f`
- 模型：`deepseek-flash`

本报告不包含广告收益或真实市场效果结论。
