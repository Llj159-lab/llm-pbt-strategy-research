#!/usr/bin/env python3
"""Apply a constrained-repair JSON DSL without allowing direct source edits."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.constrained_repair.structured_edit import (
    StructuredEditError,
    apply_structured_repair,
)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--specification", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def main() -> None:
    args = make_parser().parse_args()
    try:
        baseline = args.baseline.read_text(encoding="utf-8")
        specification = json.loads(args.specification.read_text(encoding="utf-8"))
        repair = apply_structured_repair(baseline, specification)
    except (OSError, json.JSONDecodeError, StructuredEditError) as exc:
        raise SystemExit(f"structured repair rejected: {exc}") from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(repair.source, encoding="utf-8")
    args.receipt.write_text(
        json.dumps(repair.receipt, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(repair.receipt, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
