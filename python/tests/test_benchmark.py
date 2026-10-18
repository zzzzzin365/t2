from __future__ import annotations

import json
from pathlib import Path

from src.benchmark.report import aggregate, paired_comparison
from src.benchmark.runner import BenchmarkRunner, validate
from src.workflow.types import ExperimentConfig


def test_smoke_fixtures_validate():
    result = validate(ExperimentConfig(cases_per_category=1))
    assert result["success"], result["failures"]
    assert result["events"] == 42


def test_report_has_offline_provenance_and_real_calls_zero(tmp_path):
    config = ExperimentConfig(groups=["B0", "B3"], cases_per_category=1, cache_modes=["warm"], output_dir=str(tmp_path))
    path = BenchmarkRunner(config).run()
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
    assert len(manifest["code_sha256"]) == 64
    assert summary["mode"] == "offline"
    assert summary["all_started_jobs_usage_including_incomplete"]["llm_requests"] == 0
    b3 = next(s for s in summary["summaries"] if s["group"] == "B3" and s["category"] == "ALL")
    assert b3["successful_cases"] == 7
    assert (path / "summary.csv").exists() and (path / "report.md").exists()


def test_bootstrap_clusters_repetitions_and_zero_baseline():
    rows = [dict(case_id=f"c{i}", repetition=rep, group=g, cache_mode="warm", complete=True, llm_requests=0)
            for i in range(3) for rep in range(2) for g in ["B0", "B3"]]
    comparison = paired_comparison(rows, "B0", "B3", "warm", "llm_requests")
    assert comparison["paired_cases"] == 3
    assert comparison["relative_reduction"] is None


def test_live_without_configuration_does_not_create_output(tmp_path, monkeypatch):
    for name in ["LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"]:
        monkeypatch.delenv(name, raising=False)
    import pytest
    with pytest.raises(ValueError, match="requires"):
        BenchmarkRunner(ExperimentConfig(mode="live", max_requests=1, max_tokens=1000, output_dir=str(tmp_path / "runs"))).run()
    assert not (tmp_path / "runs").exists()


def test_development_subset_cannot_claim_extra_unfrozen_cases():
    import pytest
    with pytest.raises(ValueError, match="10 cases"):
        ExperimentConfig(split="dev", cases_per_category=11)
    assert ExperimentConfig(split="test", cases_per_category=30).cases_per_category == 30
