"""Read-only audit of a completed experiment against its saved snapshots and traces."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from collections import Counter
from pathlib import Path


def digest(value):
    content = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def verify(run_dir: Path):
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    config = manifest["config"]
    scenarios = json.loads((run_dir / "scenarios.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (run_dir / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line]
    expected = {f"{s['case_id']}__r{r}__{cache}__{group}" for s in scenarios
                for r in range(config["repetitions"]) for cache in config["cache_modes"] for group in config["groups"]}
    errors = []
    if set(r["job_id"] for r in rows) != expected or len(rows) != len(expected):
        errors.append("job coverage/uniqueness differs from configured experiment")
    if digest(scenarios) != manifest["dataset_sha256"]:
        errors.append("dataset snapshot hash mismatch")
    with zipfile.ZipFile(run_dir / "source-snapshot.zip") as archive:
        source = {str(Path(info.filename)): archive.read(info).decode("utf-8") for info in archive.infolist()}
    # The experiment records native relative paths. Accept either saved host separator convention.
    candidates = [source, {k.replace("\\", "/"): v for k, v in source.items()},
                  {k.replace("/", "\\"): v for k, v in source.items()}]
    if manifest["code_sha256"] not in {digest(c) for c in candidates}:
        errors.append("source snapshot hash mismatch")
    # Read compressed published traces without extracting filesystem paths.
    if (run_dir / "traces").is_dir():
        trace_texts = {p.name: p.read_text(encoding="utf-8") for p in (run_dir / "traces").glob("*.jsonl")}
    else:
        with zipfile.ZipFile(run_dir / "traces.zip") as archive:
            trace_texts = {info.filename: archive.read(info).decode("utf-8") for info in archive.infolist()}
    requests = inputs = outputs = 0
    for row in rows:
        job = row["job_id"]
        if not row["complete"] or row["event_count"] != row["expected_event_count"]:
            errors.append(f"{job}: incomplete")
        events = [json.loads(line) for line in trace_texts[f"{job}.jsonl"].splitlines() if line]
        calls = [e for e in events if e["kind"] == "llm_request"]
        responses = [e for e in events if e["kind"] == "llm_response"]
        failed = [e for e in events if e["kind"] == "llm_failed_attempt"]
        counts = {"llm_requests": len(calls), "input_tokens": sum(e["input_tokens"] for e in responses),
                  "output_tokens": sum(e["output_tokens"] for e in responses),
                  "backend_reads": sum(e["kind"] == "backend" and e["operation"] == "read" for e in events),
                  "backend_writes": sum(e["kind"] == "backend" and e["operation"] == "write" for e in events),
                  "state_mutations": sum(e["kind"] == "mutation" for e in events)}
        for key, value in counts.items():
            if row[key] != value:
                errors.append(f"{job}: {key} mismatch")
        if len(calls) != len(responses) + len(failed):
            errors.append(f"{job}: unaccounted model attempt")
        for call in calls:
            if call["model"] != manifest["model"] or call["prompt_version"] != manifest["prompt_version"]:
                errors.append(f"{job}: model/prompt changed")
            if call.get("thinking_mode") != config["thinking_mode"] or call["max_output_tokens"] != config["max_output_tokens"]:
                errors.append(f"{job}: request configuration changed")
        requests += counts["llm_requests"]
        inputs += counts["input_tokens"]
        outputs += counts["output_tokens"]
    return {"verified": not errors, "independent_cases": len(scenarios), "jobs": len(rows),
            "jobs_by_group": dict(Counter(r["group"] for r in rows)),
            "llm_requests": requests, "input_tokens": inputs, "output_tokens": outputs, "errors": errors}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.run_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["verified"] else 1)
