from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv
from .runner import BenchmarkRunner, validate
from .report import generate_report
from ..workflow.types import ExperimentConfig


def main():
    parser = argparse.ArgumentParser(description="Synthetic-business multi-agent benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["validate", "run"]:
        p = sub.add_parser(name)
        p.add_argument("--config", required=True, type=Path)
    p = sub.add_parser("report")
    p.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)
    try:
        if args.command == "report":
            print(generate_report(args.run_dir))
            return 0
        config = ExperimentConfig.model_validate_json(args.config.read_text(encoding="utf-8-sig"))
        if args.command == "validate":
            result = validate(config)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["success"] else 1
        run_dir = BenchmarkRunner(config).run()
        print(run_dir)
        checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        if checkpoint["interrupted"]:
            print(json.dumps({"status": "incomplete", **checkpoint["interrupted"]}, ensure_ascii=False))
            return 3
        return 0
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Configuration/file error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
