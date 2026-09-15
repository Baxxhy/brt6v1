"""Component 2 允许读取的安全输入。"""
from __future__ import annotations
import json
from pathlib import Path

ISSUE_KEYS = ('instance_id','repo','version','base_commit','environment_setup_commit','problem_statement')
DELTA_KEYS = ('round_id','dimension','seed_fact','target_fact','change','preserve','avoid')


def load_safe_issues(path: Path) -> dict:
    payload = json.loads(path.read_text())
    rows = payload.values() if isinstance(payload,dict) else payload
    return {r['instance_id']:{k:r.get(k) for k in ISSUE_KEYS} for r in rows}
def safe_deltas(history) -> list:
    return [{k:d[k] for k in DELTA_KEYS if k in d} for d in history if isinstance(d,dict)]
