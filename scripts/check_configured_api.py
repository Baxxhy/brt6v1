#!/usr/bin/env python3
"""Run one credential-safe health request through BRT6's configured client."""

from __future__ import annotations

import argparse
import os

from brt6.llm.llm_client import LLMClient


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("deepseek", "gpt"), default="deepseek")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    os.environ["BRT_LLM_STREAM"] = "1" if args.stream else "0"
    client = LLMClient(
        provider=args.provider,
        model=args.model,
        temperature=0,
        max_tokens=8,
    )
    response = client.chat(
        "You are a connection health checker.",
        "Reply with OK only.",
        attempt_limit=1,
    )
    print(f"API request succeeded; response={response.strip()[:40]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
