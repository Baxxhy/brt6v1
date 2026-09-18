#!/usr/bin/env python3
"""Rebuild a run's API cost report from its append-only request ledger."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from brt6.llm.cost_tracking import write_cost_summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--pricing-file", help="Fill unpriced requests using explicit token prices")
    args = parser.parse_args()
    result = write_cost_summary(args.run_dir, args.pricing_file)
    print(json.dumps({k: result[k] for k in (
        "request_attempts", "total_cost", "known_subtotal_by_currency", "unknown_cost_attempts"
    )}, ensure_ascii=False))


if __name__ == "__main__":
    main()
