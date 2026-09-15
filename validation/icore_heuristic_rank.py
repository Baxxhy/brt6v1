"""iCoRe/LIBRO 风格的 BRT 候选启发式排序。

本模块只处理 candidate、buggy execution 和 issue 文本。不得读取 fixed-side
输出、gold patch 或历史 resolved 标签。
"""

from __future__ import annotations

import ast
from collections import defaultdict
import difflib
import io
import math
import re
import tokenize
from typing import Any


PROTOCOL = "icore_libro_heuristic_rank_v1"

_ERROR_RE = re.compile(r"\b([A-Za-z_][\w.]*(?:Error|Exception|Warning)|AssertionError|Failed)\b")
_FRAME_RE = re.compile(r'^\s*File "[^"]+", line \d+(?:, in .*)?$', re.MULTILINE)


def count_tokens(code: str) -> int:
    try:
        return len(list(tokenize.generate_tokens(io.StringIO(code).readline)))
    except (tokenize.TokenError, IndentationError):
        return len(code.split())


def count_assertions(code: str) -> int:
    try:
        tokens = tokenize.generate_tokens(io.StringIO(code).readline)
        return sum(
            token.string == "assert"
            or (token.type == tokenize.NAME and token.string.startswith("assert"))
            for token in tokens
        )
    except (tokenize.TokenError, IndentationError):
        return 0


def normalize_syntax(code: str) -> str:
    """复刻 iCoRe 的语法去重意图：忽略测试名、局部变量名和注释。"""
    try:
        tree = ast.parse(code.strip().strip("`"))
    except SyntaxError:
        return code.strip()

    class Renamer(ast.NodeTransformer):
        def __init__(self) -> None:
            self.names: dict[str, str] = {}

        def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
            if "test" in node.name.lower():
                node.name = "testMethodAutoGen"
            return self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
            if "test" in node.name.lower():
                node.name = "testMethodAutoGen"
            return self.generic_visit(node)

        def visit_Name(self, node: ast.Name) -> Any:
            if isinstance(node.ctx, ast.Store):
                self.names.setdefault(node.id, f"var{len(self.names)}")
            if node.id in self.names:
                node.id = self.names[node.id]
            return node

    normalized = Renamer().visit(tree)
    ast.fix_missing_locations(normalized)
    return ast.dump(normalized, include_attributes=False)


def combined_output(execution: dict[str, Any]) -> str:
    error_reason = str(execution.get("error_reason") or "").strip()
    if error_reason:
        return error_reason
    return "\n".join(str(execution.get(key) or "") for key in ("stdout", "stderr")).strip()


def normalize_output(code: str, execution: dict[str, Any]) -> str:
    """归一化会让同一失败的不同测试名、路径、行号落入同一簇。"""
    text = combined_output(execution)
    code_lines = [line.strip() for line in code.splitlines() if line.strip()]
    for line in sorted(code_lines, key=len, reverse=True):
        if len(line) >= 8:
            text = text.replace(line, "[CODE]")
    text = re.sub(r"0x[0-9a-fA-F]+", "[MEM_ADDR]", text)
    text = re.sub(r"\.py:\d+", ".py:[LINE_NUM]", text)
    text = re.sub(r"line \d+", "line [LINE_NUM]", text)
    text = re.sub(r"in \d+(?:\.\d+)*s", "in [TIME]s", text)
    text = re.sub(r"test_brt_[A-Za-z0-9_]+", "[TEST_NAME]", text)
    text = re.sub(r"seed_\d+", "[SEED]", text)
    text = re.sub(r"/[^\s:\"]*/test_brt_[^\s:\"]+\.py", "[TEST_PATH]", text)
    text = re.sub(r"\^+", "^", text)
    text = re.sub(r"-{2,}", "-", text)
    text = re.sub(r"_{2,}", "_", text)
    text = re.sub(r"={2,}", "=", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def exception_types(text: str) -> set[str]:
    return {match.group(1).split(".")[-1] for match in _ERROR_RE.finditer(text)}


def traceback_text(text: str) -> str:
    frames = _FRAME_RE.findall(text)
    return "\n".join(
        re.sub(r"line \d+", "line [LINE_NUM]", frame.strip()) for frame in frames
    )


def issue_match(issue: str, output: str) -> tuple[int, bool, bool]:
    issue_types = exception_types(issue)
    output_types = exception_types(output)
    exception_match = bool(issue_types & output_types)
    if not issue_types and ("AssertionError" in output_types or "Failed" in output_types):
        exception_match = True

    issue_trace = traceback_text(issue)
    output_trace = traceback_text(output)
    traceback_match = bool(
        issue_trace
        and output_trace
        and difflib.SequenceMatcher(None, output_trace, issue_trace).ratio() > 0.8
    )
    return int(exception_match) + 2 * int(traceback_match), exception_match, traceback_match


def _sort_cluster_group(keys: list[str], features: dict[str, dict[str, Any]]) -> list[str]:
    if not keys:
        return []
    maximum_size = max(features[key]["size"] for key in keys) or 1
    maximum_length = max(features[key]["minimum_length"] for key in keys) or 1
    maximum_assertions = max(features[key]["minimum_assertions"] for key in keys) or 1
    for key in keys:
        feature = features[key]
        size_score = 100 * math.log1p(feature["size"]) / math.log1p(maximum_size)
        length_score = 100 * (
            1 - math.log1p(feature["minimum_length"]) / math.log1p(maximum_length)
        )
        assertion_score = 100 * (
            1
            - math.log1p(feature["minimum_assertions"])
            / math.log1p(maximum_assertions)
        )
        feature["cluster_score"] = 0.3 * size_score + 0.5 * length_score + 0.2 * assertion_score
    return sorted(keys, key=lambda key: features[key]["cluster_score"], reverse=True)


def rank_candidates(issue: str, candidates: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """返回首选 candidate_id 和完整排序证据。"""
    ordered = sorted(candidates, key=lambda row: (row["component1_rank"], row["candidate_id"]))

    # iCoRe 先合并语法等价测试，仅保留原顺序中的第一个代表。
    syntax_representatives: dict[str, dict[str, Any]] = {}
    for candidate in ordered:
        syntax_representatives.setdefault(normalize_syntax(candidate["code"]), candidate)
    representatives = list(syntax_representatives.values())

    # 输出簇大小按所有候选计算；每个语法簇只参加一次最终排序。
    all_output_clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in ordered:
        key = normalize_output(candidate["code"], candidate["buggy_execution"])
        all_output_clusters[key].append(candidate)

    cluster_features: dict[str, dict[str, Any]] = {}
    for candidate in representatives:
        key = normalize_output(candidate["code"], candidate["buggy_execution"])
        if key not in cluster_features:
            cluster_features[key] = {"members": [], "all_members": all_output_clusters[key]}
        output = combined_output(candidate["buggy_execution"])
        match_score, exception_match, trace_match = issue_match(issue, output)
        cluster_features[key]["members"].append(
            {
                **candidate,
                "token_count": count_tokens(candidate["code"]),
                "assertion_count": count_assertions(candidate["code"]),
                "exception_type_match": exception_match,
                "traceback_match": trace_match,
                "issue_match_score": match_score,
            }
        )

    for feature in cluster_features.values():
        members = feature["members"]
        feature["issue_match_score"] = members[0]["issue_match_score"]
        feature["size"] = len(feature["all_members"])
        feature["minimum_length"] = min(row["token_count"] for row in members)
        feature["minimum_assertions"] = min(row["assertion_count"] for row in members)

    matching = [key for key, value in cluster_features.items() if value["issue_match_score"] > 0]
    nonmatching = [key for key, value in cluster_features.items() if value["issue_match_score"] == 0]
    cluster_order = _sort_cluster_group(matching, cluster_features) + _sort_cluster_group(
        nonmatching, cluster_features
    )

    ranking: list[dict[str, Any]] = []
    for cluster_position, key in enumerate(cluster_order):
        feature = cluster_features[key]
        members = sorted(
            feature["members"],
            key=lambda row: (
                -int(row["exception_type_match"]),
                row["assertion_count"],
                row["token_count"],
                row["component1_rank"],
            ),
        )
        for candidate in members:
            ranking.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "component1_rank": candidate["component1_rank"],
                    "cluster_position": cluster_position,
                    "cluster_size": feature["size"],
                    "cluster_score": feature["cluster_score"],
                    "issue_match_score": candidate["issue_match_score"],
                    "exception_type_match": candidate["exception_type_match"],
                    "traceback_match": candidate["traceback_match"],
                    "assertion_count": candidate["assertion_count"],
                    "token_count": candidate["token_count"],
                }
            )

    if not ranking:
        raise ValueError("没有可排序候选")
    return ranking[0]["candidate_id"], ranking
