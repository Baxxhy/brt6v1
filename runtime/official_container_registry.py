"""Persistent mapping between benchmark instances and official containers."""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = _PROJECT_ROOT / ".runtime"
DEFAULT_REGISTRY_PATH = DEFAULT_RUNTIME_ROOT / "official_container_registry.json"
DEFAULT_INSTANCE_LOCK_DIR = DEFAULT_RUNTIME_ROOT / "locks"


class ContainerRegistryError(RuntimeError):
    """Raised when persistent container identity cannot be resolved safely."""


@dataclass(frozen=True)
class ContainerResolution:
    name: str
    container: Any | None
    reused: bool
    adopted: bool


def lock_filename(instance_id: str) -> str:
    rendered = re.sub(r"[^0-9A-Za-z_.-]+", "_", instance_id).strip("._")
    if not rendered:
        raise ValueError("instance_id is empty after sanitization")
    return f"{rendered}.lock"


class InstanceLock:
    """Cross-process exclusive ownership of one benchmark instance."""

    def __init__(self, instance_id: str, lock_dir: Path | None = None) -> None:
        self.path = (lock_dir or DEFAULT_INSTANCE_LOCK_DIR) / lock_filename(
            instance_id
        )
        self._handle = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.release()


def _container_attrs(container: Any) -> Mapping[str, Any]:
    container.reload()
    return container.attrs or {}


def container_is_reusable(container: Any, expected_image: str) -> bool:
    attrs = _container_attrs(container)
    config = attrs.get("Config") or {}
    state = attrs.get("State") or {}
    return (
        config.get("Image") == expected_image
        and not bool(state.get("Dead"))
        and str(state.get("Status") or "").lower()
        in {"created", "running", "exited"}
    )


def ensure_container_running(container: Any) -> None:
    attrs = _container_attrs(container)
    status = str((attrs.get("State") or {}).get("Status") or "").lower()
    if status in {"created", "exited"}:
        container.start()
        container.reload()


def _is_official_name(name: str, expected_image: str) -> bool:
    prefix = expected_image.removesuffix(":latest") + "."
    return name.startswith(prefix) and ".brt6." not in name


def _not_found(error: Exception) -> bool:
    return error.__class__.__name__ in {"NotFound", "ImageNotFound"}


class OfficialContainerRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_REGISTRY_PATH
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    payload = json.loads(self.path.read_text(encoding="utf-8"))
                else:
                    payload = {"schema_version": 1, "instances": {}}
                if not isinstance(payload.get("instances"), dict):
                    raise ContainerRegistryError(
                        f"invalid official container registry: {self.path}"
                    )
                yield payload
                temporary = self.path.with_name(
                    f".{self.path.name}.tmp.{os.getpid()}"
                )
                temporary.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, self.path)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def resolve(
        self,
        client: Any,
        *,
        instance_id: str,
        expected_image: str,
        preferred_name: str,
    ) -> ContainerResolution:
        if not _is_official_name(preferred_name, expected_image):
            raise ContainerRegistryError(
                f"official harness returned an invalid container name: {preferred_name}"
            )
        with self._locked() as payload:
            entries = payload["instances"]
            entry = entries.get(instance_id)
            if entry is not None:
                name = str(entry.get("container_name") or "")
                image = str(entry.get("image") or "")
                if image != expected_image or not _is_official_name(
                    name, expected_image
                ):
                    # The official harness may legitimately change the image
                    # identity for an instance. Never reuse the stale entry;
                    # resolve or create a container for the current image.
                    entries.pop(instance_id, None)
                    entry = None
                if entry is not None:
                    try:
                        container = client.containers.get(name)
                    except Exception as error:
                        if not _not_found(error):
                            raise
                        container = None
                    if container is not None and not container_is_reusable(
                        container, expected_image
                    ):
                        raise ContainerRegistryError(
                            f"registered container is incompatible: {name}"
                        )
                    return ContainerResolution(
                        name=name,
                        container=container,
                        reused=container is not None,
                        adopted=False,
                    )

            candidates = []
            incompatible = []
            for container in client.containers.list(all=True):
                name = str(getattr(container, "name", "") or "")
                if not _is_official_name(name, expected_image):
                    continue
                attrs = _container_attrs(container)
                if (attrs.get("Config") or {}).get("Image") != expected_image:
                    continue
                if container_is_reusable(container, expected_image):
                    candidates.append(container)
                else:
                    incompatible.append(name)
            if incompatible:
                raise ContainerRegistryError(
                    f"incompatible official containers for {instance_id}: "
                    f"{sorted(incompatible)}"
                )
            if len(candidates) > 1:
                names = sorted(str(container.name) for container in candidates)
                raise ContainerRegistryError(
                    f"multiple compatible official containers for {instance_id}: {names}"
                )
            container = candidates[0] if candidates else None
            name = str(container.name) if container is not None else preferred_name
            entries[instance_id] = {
                "container_name": name,
                "image": expected_image,
            }
            return ContainerResolution(
                name=name,
                container=container,
                reused=container is not None,
                adopted=container is not None,
            )

    def confirm(
        self, *, instance_id: str, expected_image: str, container_name: str
    ) -> None:
        if not _is_official_name(container_name, expected_image):
            raise ContainerRegistryError(
                f"refusing to register non-official container name: {container_name}"
            )
        with self._locked() as payload:
            entry = payload["instances"].get(instance_id)
            expected = {
                "container_name": container_name,
                "image": expected_image,
            }
            if entry is not None and entry != expected:
                raise ContainerRegistryError(
                    f"registry changed while creating {instance_id}: {entry}"
                )
            payload["instances"][instance_id] = expected
