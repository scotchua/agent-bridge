"""Summarize an operator-run review canary result without dispatching a model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("--fixtures", type=Path,
                        default=Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "review_canary")
    args = parser.parse_args(argv)
    expected = {case["name"]: case for case in json.loads((args.fixtures / "manifest.json").read_text())["cases"]}
    results = json.loads(args.results.read_text())
    if not isinstance(results, list):
        raise SystemExit("results must be a JSON list")
    rows = []
    for result in results:
        case = expected.get(result.get("name"))
        if case is None:
            continue
        findings = result.get("findings", [])
        hit = any(f.get("file") == case["file"] and f.get("line") == case["line"] for f in findings)
        passed = result.get("verdict") == case["expected"] and (case["line"] == 0 or hit)
        rows.append({"name": result["name"], "passed": passed,
                     "latency_seconds": result.get("latency_seconds"), "cost": result.get("cost"),
                     "false_positive": case["expected"] == "approve" and bool(findings)})
    print(json.dumps({"cases": rows, "passed": sum(row["passed"] for row in rows),
                      "total": len(rows), "false_positives": sum(row["false_positive"] for row in rows)}, indent=2))
    return 0 if len(rows) == len(expected) and all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
