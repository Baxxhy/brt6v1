"""Append-only request accounting, independent of generation and selection.

No prompts, responses or credentials are stored here. Missing billing data is
unknown, never zero. Prices must be supplied by the experiment owner.
"""

from __future__ import annotations

import atexit
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid
import warnings


_registered: set[str] = set()
_registration_lock = threading.Lock()


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def _at(value, path):
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _load_prices(path):
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rates = data.get("rates", [])
    if not isinstance(rates, list):
        raise ValueError("pricing.rates must be a list")
    fields = {"host", "model", "currency", "input_per_million", "output_per_million",
              "cached_input_per_million", "reported_cost_field"}
    if any(not isinstance(row, dict) for row in rates):
        raise ValueError("pricing entries must be objects")
    return [{k: v for k, v in row.items() if k in fields} for row in rates]


def _rate_for(rates, host, model):
    return next((r for r in rates if r.get("host") == host and r.get("model") == model), {})


def _usage_counts(usage):
    # Reasoning tokens are generally a subset of completion tokens. Preserve
    # their detail, but do not charge for them twice.
    result = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens",
                 "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        n = _number(usage.get(name))
        if n is not None and n == int(n):
            result[name] = int(n)
    details = usage.get("prompt_tokens_details") or {}
    if isinstance(details, dict) and _number(details.get("cached_tokens")) is not None:
        result["prompt_cache_hit_tokens"] = int(details["cached_tokens"])
    details = usage.get("completion_tokens_details") or {}
    if isinstance(details, dict) and _number(details.get("reasoning_tokens")) is not None:
        result["reasoning_tokens"] = int(details["reasoning_tokens"])
    return result


def _price(usage, rate, response=None):
    currency = str(rate.get("currency") or "").strip()
    field = rate.get("reported_cost_field")
    if field and currency and response is not None:
        amount = _number(_at(response, field))
        if amount is not None:
            return {"amount": str(amount), "currency": currency, "basis": "reported"}
    prompt = _number(usage.get("prompt_tokens"))
    completion = _number(usage.get("completion_tokens"))
    ip = _number(rate.get("input_per_million"))
    op = _number(rate.get("output_per_million"))
    if not currency or None in (prompt, completion, ip, op):
        return None
    cached_price = _number(rate.get("cached_input_per_million"))
    cached = _number(usage.get("prompt_cache_hit_tokens"))
    if cached is not None and cached > prompt:
        return None
    # If cache hits have a different price, missing cache usage makes the
    # exact estimate unavailable. A uniform input rate needs no cache detail.
    if cached_price is not None and cached is None:
        return None
    cached = cached or Decimal(0)
    input_cost = ((prompt - cached) * ip + cached * cached_price
                  if cached_price is not None else prompt * ip)
    amount = (input_cost + completion * op) / Decimal(1_000_000)
    return {"amount": str(amount), "currency": currency, "basis": "estimated"}


@contextmanager
def _locked(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".api_cost.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _append(root, row):
    with _locked(root):
        with (root / "api_requests.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()


def _atomic_write(path, text):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", delete=False) as out:
        temporary = Path(out.name)
        out.write(text)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _aggregate(rows):
    tokens = Counter()
    outcomes = Counter()
    money = defaultdict(Decimal)
    bases = Counter()
    unknown = 0
    for row in rows:
        outcomes[row.get("outcome", "interrupted_or_inflight")] += 1
        tokens.update(row.get("usage") or {})
        cost = row.get("cost")
        if not cost:
            unknown += 1
        else:
            money[cost["currency"]] += Decimal(cost["amount"])
            bases[cost["basis"]] += 1
    total = None
    if rows and unknown == 0 and len(money) == 1:
        currency, amount = next(iter(money.items()))
        total = {"amount": str(amount), "currency": currency,
                 "basis": next(iter(bases)) if len(bases) == 1 else "mixed"}
    return {
        "request_attempts": len(rows),
        "logical_requests": len({r["logical_request_id"] for r in rows}),
        "retry_attempts": sum(int(r["attempt"]) > 1 for r in rows),
        "outcomes": dict(outcomes),
        "known_token_totals": dict(tokens),
        "attempts_missing_token_usage": sum(not r.get("usage") for r in rows),
        "costed_attempts": len(rows) - unknown,
        "unknown_cost_attempts": unknown,
        "known_subtotal_by_currency": {k: str(v) for k, v in money.items()},
        "cost_basis_counts": dict(bases),
        "total_cost": total,
    }


def write_cost_summary(directory, pricing_file=None):
    root = Path(directory).resolve()
    rates = _load_prices(pricing_file)
    with _locked(root):
        ledger = root / "api_requests.jsonl"
        records = {}
        malformed = 0
        if ledger.exists():
            with ledger.open(encoding="utf-8") as inp:
                for line in inp:
                    try:
                        row = json.loads(line)
                        key = row["attempt_id"]
                    except (ValueError, KeyError, TypeError):
                        malformed += 1
                        continue
                    records.setdefault(key, {}).update(row)
        required = {"logical_request_id", "attempt", "host", "requested_model"}
        rows = [row for row in records.values() if required.issubset(row)]
        malformed += len(records) - len(rows)
        for row in rows:
            if not row.get("cost") and rates:
                row["cost"] = _price(row.get("usage") or {}, _rate_for(
                    rates, row["host"], row["requested_model"]))
        result = {"schema_version": "api_cost.v1", "scope": "model_api_only",
                  "generated_at": datetime.now(timezone.utc).isoformat(),
                  "ledger_present": ledger.exists(), "malformed_ledger_lines": malformed,
                  **_aggregate(rows)}
        if malformed:
            result["total_cost"] = None
        for group in ("stage", "operation", "instance_id", "requested_model"):
            groups = defaultdict(list)
            for row in rows:
                groups[str(row.get(group) or "unknown")].append(row)
            result["by_" + group] = {k: _aggregate(v) for k, v in groups.items()}
        _atomic_write(root / "api_cost_summary.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        total = result["total_cost"]
        total_text = (f'{total["amount"]} {total["currency"]}（{total["basis"]}）'
                      if total else "未知：费用记录尚不完整或未配置单价。")
        text = (
            "# 实验 API 费用\n\n"
            f"总花费：{total_text}\n\n"
            f"实际请求尝试：{result['request_attempts']}；重试：{result['retry_attempts']}。\n\n"
            f"已知金额小计（按币种）：{json.dumps(result['known_subtotal_by_currency'], ensure_ascii=False)}。\n\n"
            f"费用未知的请求：{result['unknown_cost_attempts']}；异常账本行：{malformed}。\n\n"
            "统计范围仅为本目录账本记录的模型 API 请求；不含服务器、Docker 和复用历史产物的原始成本。\n"
            "reported 表示接口返回金额，estimated 表示按配置单价估算，mixed 表示两者混合。\n"
            "失败、超时、缺失用量不按零元处理；多个币种不直接相加。\n"
        )
        _atomic_write(root / "api_cost_summary.md", text)
    return result


def _finish_safely(directory):
    if not Path(directory).is_dir():
        return
    try:
        write_cost_summary(directory)
    except Exception as exc:
        warnings.warn(f"API cost summary could not be saved ({type(exc).__name__})")


class RequestAccounting:
    def __init__(self, directory, context=None):
        self.root = Path(directory).resolve()
        self.context = {k: str(v) for k, v in (context or {}).items()
                        if k in {"stage", "instance_id"}}
        self.rates = _load_prices(os.environ.get("BRT_LLM_PRICING_FILE"))
        with _registration_lock:
            if str(self.root) not in _registered:
                atexit.register(_finish_safely, str(self.root))
                _registered.add(str(self.root))

    def start(self, **fields):
        attempt_id = str(uuid.uuid4())
        row = {"event": "started", "attempt_id": attempt_id,
               "time": datetime.now(timezone.utc).isoformat(), **self.context, **fields}
        _append(self.root, row)
        return attempt_id

    def finish(self, attempt_id, *, host, model, response, outcome,
               status_code=None, finish_reason="", elapsed_seconds=0):
        usage = _usage_counts(response.get("usage") or {})
        rate = _rate_for(self.rates, host, model)
        cost = _price(usage, rate, response)
        row = {"event": "finished", "attempt_id": attempt_id,
               "time": datetime.now(timezone.utc).isoformat(), "outcome": outcome,
               "http_status": status_code, "finish_reason": finish_reason,
               "elapsed_seconds": round(elapsed_seconds, 3), "usage": usage,
               "cost": cost, "pricing_snapshot": rate}
        _append(self.root, row)
