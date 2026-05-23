#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
GOLDEN = ROOT / "golden_emails"
EXPECTED = ROOT / "expected_outputs"


def main() -> int:
    cases = sorted(GOLDEN.glob("*.json"))
    expected = sorted(EXPECTED.glob("*.json"))
    print(
        json.dumps(
            {
                "cases": len(cases),
                "expected_outputs": len(expected),
                "metrics": {
                    "grade_relevance_accuracy": None,
                    "action_required_accuracy": None,
                    "calendar_extraction_accuracy": None,
                    "importance_routing_accuracy": None,
                    "invalid_json_rate": None,
                },
                "status": "scaffold",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
