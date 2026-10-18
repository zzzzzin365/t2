# 第二轮实验：可核验结果包

本目录收录 2026-09-19 启动、2026-09-20 确认完成的实验记录：21 个独立测试案例 × 3 次重复 × 5 组，315/315 组完成。详细解释见 [实验结果](../../docs/validation-21-results.md)。所有业务数据均为合成数据。

| 文件 | 用途 |
|---|---|
| `manifest.json` | 原实验配置、模型标识、依赖与代码/数据/提示词哈希 |
| `scenarios.json` | 冻结的案例输入及评价规则；规则不提供给 Agent |
| `cases.jsonl` | 315 组案例的原始计数、成功判定与失败原因 |
| `summary.json` / `summary.csv` | 总体与分场景统计、配对 Bootstrap 置信区间 |
| `report.md` | 原运行自动生成的报告 |
| `checkpoint.json` | 完成状态、请求数与预算计量 |
| `traces.zip` | 315 份原始 JSONL 轨迹，含模型输入输出和实际调用记录 |
| `source-snapshot.zip` | 该次实验使用的 Python 工作流和评测代码 |
| `verification.json` | 原始轨迹审计结果 |
| `SHA256SUMS.json` | 本结果包数据文件的 SHA256 |

原运行目录为 `artifacts/deepseek-validation-21/20260919T125601751916Z`。此处复制原始文件，JSONL 仅做无损压缩，不重新计算或调整历史结果。原报告提到的 `traces/` 对应这里的 `traces.zip`；`states/` SQLite 数据库保留在本地原目录，没有纳入此包。旧 manifest 中的 `output_dir` 是当时运行位置，不是公开包路径。

整理后的当前代码可能与历史快照在目录说明等方面不同。复核已报告数字时使用保存的快照与 manifest；重新运行产生新的目录与代码哈希，不覆盖本次实验。

在项目根目录运行以下命令，无需模型密钥、无需付费请求：

```bash
python scripts/verify-experiment.py --run-dir results/validation-21
```

脚本直接读取压缩轨迹，核验任务覆盖、代码与数据哈希、请求/响应/失败次数、Token 与后端计数。预期 `verified: true`，315 组、21 个独立案例、1,849 次模型请求。

已知 Token 总量为 972,563；一次传输失败没有 usage，预算计量额外保留 1,427 Token，不等于实际计费。未设置单价，不报告货币费用。
