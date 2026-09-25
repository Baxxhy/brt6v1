"""Same-experiment recovery of completed external steps, using exact inputs.

This is an execution journal, not a cross-experiment response cache. Independent
calls at different positions remain independent even when their prompts match.
"""
from __future__ import annotations

import contextvars
import dataclasses
import functools
import inspect
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path


from .infrastructure_errors import infrastructure_result, InfrastructureUnavailableError


_active = contextvars.ContextVar("brt_step_journal", default=None)


def plain(value):
    if dataclasses.is_dataclass(value):
        return plain(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def atomic_json(path: Path, data):
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(plain(data), f, ensure_ascii=False)
            f.flush()
        os.replace(tmp, path)
    except OSError as exc:
        from ..llm.errors import LLMUnavailableError
        raise LLMUnavailableError(
            f"Step checkpoint could not be saved ({type(exc).__name__}); stop before semantic repair"
        ) from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def init_resume_run(output_dir: str, resume: bool) -> str:
    """Compare actual implementation/prompt bytes once before starting workers."""
    root = Path(__file__).resolve().parents[1]
    files = {}
    for folder in ("core", "context", "execution", "generation", "issue", "llm",
                   "mutation", "pipeline", "retrieval", "runtime", "validation", "io",
                   "prompts", "prompts_en"):
        for path in sorted((root / folder).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".txt", ".md"}:
                files[str(path.relative_to(root))] = path.read_bytes().decode("utf-8")
    path = Path(output_dir) / ".resume" / "implementation.json"
    previous = read_json(path) if resume else None
    if previous and previous.get("files") == files:
        return previous["run_revision"]
    revision = str(uuid.uuid4())
    atomic_json(path, {"run_revision": revision, "files": files})
    return revision


class StepJournal:
    def __init__(self, output_dir, identity, name="steps"):
        self.root = Path(output_dir) / ".resume" / name
        identity = plain(identity)
        current = read_json(self.root / "current.json")
        if not current or current.get("identity") != identity:
            current = {"identity": identity, "session": str(uuid.uuid4())}
            atomic_json(self.root / "current.json", current)
        self.directory = self.root / current["session"]
        self.directory.mkdir(parents=True, exist_ok=True)
        self.cursor = 0

    def step(self, kind, inputs, execute, reusable=None):
        index = self.cursor
        self.cursor += 1
        path = self.directory / f"step_{index:05d}.json"
        expected = {"kind": kind, "inputs": plain(inputs)}
        saved = read_json(path)
        if saved is not None and saved.get("request") == expected and saved.get("reusable", True) and not infrastructure_result(saved.get("result")):
            return saved["result"]
        # A changed/missing earlier step invalidates later dependencies. Keep
        # their records for audit; never silently replay a mismatched suffix.
        suffix = [p for p in self.directory.glob("step_*.json")
                  if int(p.stem.split("_")[1]) >= index]
        if suffix:
            archive = self.directory / ("invalidated_" + uuid.uuid4().hex)
            archive.mkdir()
            for entry in suffix:
                entry.rename(archive / entry.name)
        self.directory.joinpath("complete.json").unlink(missing_ok=True)
        result = plain(execute())  # Exceptions never create a completed step.
        atomic_json(path, {"request": expected, "result": result,
                           "reusable": True if reusable is None else bool(reusable(result))})
        return result

    @contextmanager
    def activate(self):
        token = _active.set(self)
        try:
            yield self
        finally:
            _active.reset(token)

    def completed(self, output_dir):
        saved = read_json(self.directory / "complete.json")
        if not saved or infrastructure_result(saved.get("result")):
            return None
        for name, contents in saved["artifacts"].items():
            try:
                if (Path(output_dir) / name).read_bytes().decode("utf-8") != contents:
                    return None
            except OSError:
                return None
        return saved["result"]

    def finish(self, result, output_dir):
        if infrastructure_result(plain(result)):
            return
        if result.status in {"ERROR", "PAUSED_INFRA", "PAUSED_API", "SETUP_ERROR", "ENV_UNRESOLVED", "TIMEOUT"}:
            return
        root = Path(output_dir)
        # Ranking consumes these exact artifacts on resume. Final code alone
        # is insufficient: an accepted seed is not a completed three-seed run.
        names = ["summary.json", "final_test.py", "candidate_ranking.json"]
        artifacts = {name: (root / name).read_bytes().decode("utf-8")
                     for name in names if (root / name).is_file()}
        if "summary.json" in artifacts:
            atomic_json(self.directory / "complete.json", {
                "result": result.to_dict(), "artifacts": artifacts})


def current_journal():
    return _active.get()


def instance_journal(args, context, client, output_dir):
    excluded = {"api_key", "resume", "max_workers", "instance_id", "instance_ids_file", "limit"}
    config = {k: v for k, v in vars(args).items()
              if k not in excluded and not k.startswith("_")}
    targets = {}
    for entry in os.environ.get("BRT4_BEHAVIOR_CACHE_DIR", "").split(os.pathsep):
        if entry:
            path = Path(entry) / context.instance_id / "behavior_target.json"
            if path.is_file():
                targets[str(path)] = path.read_bytes().decode("utf-8")
    return StepJournal(output_dir, {"implementation": args._resume_revision,
                                   "context": context, "config": config,
                                   "model": client.request_identity(), "targets": targets}, name="instance")


def pause_status(args, instance_id, error):
    event = getattr(args, "_api_pause_event", None)
    if event is not None:
        event.set()
    result = {"instance_id": instance_id, "status": "PAUSED_API", "error": str(error)}
    directory = Path(args.output_dir) / instance_id
    # Queued jobs may already have a completed result from an earlier pass.
    # Do not erase that evidence merely because another worker paused the API.
    atomic_json(directory / "api_pause.json", result)
    if not (directory / "summary.json").exists():
        atomic_json(directory / "summary.json", result)
    atomic_json(Path(args.output_dir) / "api_paused.json", result)
    return result


def recorded_dataclass_step(kind, function, result_type, *args, **kwargs):
    journal = current_journal()
    if journal is None:
        result = function(*args, **kwargs)
        if infrastructure_result(plain(result)):
            raise InfrastructureUnavailableError("Infrastructure execution failed; model repair blocked")
        return result
    data = journal.step(kind, {"args": args, "kwargs": kwargs},
                        lambda: function(*args, **kwargs).to_dict(),
                        reusable=lambda r: not r.get("timeout") and
                        r.get("status", r.get("seed_execution_status")) not in
                        {"TIMEOUT", "ERROR", "ENV_UNRESOLVED"})
    if infrastructure_result(data):
        raise InfrastructureUnavailableError("Infrastructure execution failed; model repair blocked")
    return result_type(**data)


def journaled_pipeline(function):
    """Each seed has its own replay cursor and completion record."""
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = dict(bound.arguments)
        client = values.pop("llm_client")
        revision = getattr(client, "resume_revision", None)
        if not isinstance(revision, str):
            return function(*args, **kwargs)
        # Preparation durations/logs are infrastructure observations, not input
        # identity. Base commit + repository path are already in the context.
        values.pop("_prepare_meta", None)
        context = values["context"]
        output_dir = values["output_dir"]
        target = None
        if values.get("enable_behavior_target"):
            roots = [Path(p) / context.instance_id for p in
                     os.environ.get("BRT4_BEHAVIOR_CACHE_DIR", "").split(os.pathsep) if p] + [Path(output_dir)]
            for root in roots:
                path = root / "behavior_target.json"
                if path.is_file():
                    target = path.read_bytes().decode("utf-8")
                    break
        journal = StepJournal(output_dir, {
            "implementation": revision, "arguments": values,
            "model": client.request_identity(), "behavior_target": target,
        })
        # Only leaf branches may skip their controller. The outer controller
        # must collect all seeds and use the unchanged ranking/fallback logic.
        is_leaf = values.get("_adaptive_disabled")
        if is_leaf:
            saved = journal.completed(output_dir)
            if saved is not None:
                from ..core.schema import FinalResult
                return FinalResult(**saved)
        with journal.activate():
            result = function(*args, **kwargs)
        if is_leaf:
            journal.finish(result, output_dir)
        return result
    return wrapped
