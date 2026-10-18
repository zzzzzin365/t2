from __future__ import annotations

import csv
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

COUNTS = ["agent_executions", "llm_requests", "offline_model_calls", "backend_reads", "backend_writes", "cache_hits",
          "coalesced_requests", "input_tokens", "output_tokens", "estimated_context_tokens", "usage_estimated_responses",
          "unknown_usage_requests", "state_mutations", "proposed_actions", "duplicate_decisions", "duplicate_executions",
          "blocked_actions", "decision_opportunities", "required_branches", "completed_required_branches", "constraint_checks", "constraint_pass"]


def ratio(n, d):
    return n / d if d else None


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    i = (len(values) - 1) * q
    lower = int(i)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (i - lower)


def aggregate(rows):
    complete = [r for r in rows if r["complete"]]
    success = sum(r["success"] for r in complete)
    counts = {key: sum(r.get(key, 0) for r in complete) for key in COUNTS}
    return {"planned_or_started_cases": len(rows), "complete_cases": len(complete), "incomplete_cases": len(rows) - len(complete),
            "successful_cases": success, "success_rate": ratio(success, len(complete)), **counts,
            "branch_recall": ratio(counts["completed_required_branches"], counts["required_branches"]),
            "constraint_adherence": ratio(counts["constraint_pass"], counts["constraint_checks"]),
            "duplicate_decision_rate": ratio(counts["duplicate_decisions"], sum(r.get("decision_opportunities", 0) for r in complete)),
            "duplicate_execution_rate": ratio(counts["duplicate_executions"], counts["state_mutations"]),
            "p50_ms": percentile([r["elapsed_ms"] for r in complete], .5),
            "p95_ms": percentile([r["elapsed_ms"] for r in complete], .95),
            "llm_per_case": ratio(counts["llm_requests"], len(complete)),
            "llm_per_successful_case": ratio(counts["llm_requests"], success),
            "tokens_per_case": ratio(counts["input_tokens"] + counts["output_tokens"], len(complete)),
            "tokens_per_successful_case": ratio(counts["input_tokens"] + counts["output_tokens"], success),
            "total_model_ms": sum(r["model_elapsed_ms"] for r in complete),
            "total_tool_ms": sum(r["tool_elapsed_ms"] for r in complete)}


def paired_comparison(rows, baseline, variant, cache, metric, seed=42):
    by_group = {}
    for group in [baseline, variant]:
        by_group[group] = {(r["case_id"], r["repetition"]): r for r in rows if r["group"] == group and r["cache_mode"] == cache and r["complete"]}
    matches = sorted(set(by_group[baseline]) & set(by_group[variant]))
    clusters = defaultdict(list)
    for key in matches:
        clusters[key[0]].append((float(by_group[baseline][key][metric]), float(by_group[variant][key][metric])))
    # All repetitions of a case remain in the same bootstrap cluster.
    pairs = [(statistics.mean(p[0] for p in cluster), statistics.mean(p[1] for p in cluster)) for cluster in clusters.values()]
    result = {"baseline": baseline, "variant": variant, "cache_mode": cache, "metric": metric, "paired_cases": len(pairs)}
    if not pairs:
        return result | {"mean_delta": None, "relative_reduction": None, "delta_ci95": None}
    base = statistics.mean(p[0] for p in pairs)
    new = statistics.mean(p[1] for p in pairs)
    rng = random.Random(seed)
    deltas, reductions = [], []
    for _ in range(1000):
        sample = rng.choices(pairs, k=len(pairs))
        a = statistics.mean(p[0] for p in sample)
        b = statistics.mean(p[1] for p in sample)
        deltas.append(b - a)
        if a:
            reductions.append((a - b) / a)
    return result | {"baseline_mean": base, "variant_mean": new, "mean_delta": new - base,
                     "relative_reduction": ratio(base - new, base),
                     "delta_ci95": [percentile(deltas, .025), percentile(deltas, .975)],
                     "relative_reduction_ci95": [percentile(reductions, .025), percentile(reductions, .975)] if reductions else None,
                     "evidence_note": "CI reflects this synthetic case distribution, not production generalization"}


def generate_report(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (run_dir / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    config = manifest["config"]
    summaries = []
    for group in config["groups"]:
        for cache in config["cache_modes"]:
            selected = [r for r in rows if r["group"] == group and r["cache_mode"] == cache]
            for category in ["ALL"] + sorted({r["category"] for r in selected}):
                subset = selected if category == "ALL" else [r for r in selected if r["category"] == category]
                summary = {"group": group, "cache_mode": cache, "category": category, **aggregate(subset)}
                if config["mode"] == "live" and config["input_price_per_million"] is not None and config["output_price_per_million"] is not None:
                    summary["configured_price_cost_estimate"] = (summary["input_tokens"] * config["input_price_per_million"] + summary["output_tokens"] * config["output_price_per_million"]) / 1_000_000
                summaries.append(summary)
    comparisons = []
    for baseline, variant in [("B0", "B1"), ("B1", "B2"), ("B2", "B3"), ("M-Full", "B3"), ("B0", "B3")]:
        if baseline not in config["groups"] or variant not in config["groups"]:
            continue
        for cache in config["cache_modes"]:
            for metric in ["success", "agent_executions", "llm_requests", "backend_reads", "input_tokens", "estimated_context_tokens", "elapsed_ms"]:
                comparisons.append(paired_comparison(rows, baseline, variant, cache, metric, config["seed"]))
    all_counts = {key: sum(r.get(key, 0) for r in rows) for key in COUNTS}
    report = {"mode": config["mode"], "summaries": summaries, "paired_comparisons": comparisons,
              "all_started_jobs_usage_including_incomplete": all_counts,
              "note": "Costs and calls from incomplete jobs retained above; quality aggregates include completed jobs only."}
    (run_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if summaries:
        with (run_dir / "summary.csv").open("w", newline="", encoding="utf-8-sig") as out:
            writer = csv.DictWriter(out, fieldnames=list(summaries[0]))
            writer.writeheader()
            writer.writerows(summaries)
    lines = ["# 多 Agent 工作流实验报告", "", f"运行模式：**{config['mode']}**。业务数据和广告执行均为模拟。", "",
             "离线模式的模型是固定响应：真实 LLM 请求和实测 Token 为 0，不能用于宣称模型质量、真实费用或真实模型延迟收益。" if config["mode"] == "offline" else "真实模型 usage 优先；缺失 usage 的响应单独标记估算。传输失败可能产生未知计费，费用估算不是账单。",
             "", "| 组 | 缓存 | 完成案例 | 成功/完成 | Agent | 真实LLM | 离线响应 | 后端读取 | 输入Token | P95(ms) |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for s in summaries:
        if s["category"] != "ALL":
            continue
        p95 = f"{s['p95_ms']:.2f}" if s["p95_ms"] is not None else "N/A"
        lines.append(f"| {s['group']} | {s['cache_mode']} | {s['complete_cases']} | {s['successful_cases']}/{s['complete_cases']} | {s['agent_executions']} | {s['llm_requests']} | {s['offline_model_calls']} | {s['backend_reads']} | {s['input_tokens']} | {p95} |")
    lines += ["", "## 结果读取与限制", "", "- `summary.csv` 包含分场景、分子分母、每任务与每成功任务消耗。", "- `summary.json` 包含按案例聚类、保留重复运行的配对 Bootstrap 95% CI；比较 delta 为改进组减基线组。", "- `cases.jsonl` 保存案例级成功判定和失败原因；`traces/` 包含上下文、原始模型响应、路由与每次实际请求。", "- `states/` 为本地 SQLite 状态与执行账本；未启动任务和预算中断见 `checkpoint.json`。", "- 冷模式每个业务事件清空读取缓存；热模式仅在同一案例的连续事件间保留，不跨案例预热。", "- SQLite/模拟工具耗时不能外推生产数据库与真实广告 API。", "- 调用更少但质量下降不能认定优化成功；工程目标不是保证。", "", "## 失败样例（最多 20 个，完整结果见 cases.jsonl）", ""]
    for r in [r for r in rows if r["complete"] and not r["success"]][:20]:
        lines.append(f"- `{r['job_id']}`：{r['failures'][0]['errors'] if r['failures'] else 'unknown'}")
    lines += ["", "## 可追溯信息", "", f"- 上游提交：`{manifest['upstream_commit']}`", f"- 本次实现 SHA256：`{manifest['code_sha256']}`", f"- 数据集 SHA256：`{manifest['dataset_sha256']}`", f"- 提示词版本：`{manifest['prompt_version']}`", f"- 模型：`{manifest['model']}`", "", "本报告不包含广告收益或真实市场效果结论。"]
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run_dir / "report.md"
