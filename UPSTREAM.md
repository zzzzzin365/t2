# 上游来源与改造范围

- 上游项目：[bcefghj/multi-agent-ad-optimizer](https://github.com/bcefghj/multi-agent-ad-optimizer)。
- 基于提交：`04c5750a727f993617d368980a5b7937008d6226`。
- 许可证：MIT，原许可证保留于 [LICENSE](LICENSE)。

本仓库基于上游的广告优化主题与五角色划分，重构 Python 工作流、模拟工具、决策记忆和对照评测体系。保留 Monitor、Audience、Creative、Bidding、Optimize 五类业务职责，当前实现位于 `python/src/workflow`，评测入口位于 `python/src/benchmark`。

整理后的仓库仅维护 Python 实现；Java、Go、旧仪表盘、ClickHouse/Redis 部署示例及对应教程不纳入发布内容。原始实现可在上游固定提交查看。本地 `.research` 参考副本不提交 Git。

仓库展示的数字来自本项目保存的真实 LLM 与合成业务实验，不沿用上游 README 的宣传指标。各组共用正确性修复与幂等执行器；性能对照基线为修正后的固定流程 B0，不能将全部差异归因于对上游原始代码的优化。
