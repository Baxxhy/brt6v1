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


def instance_scoped_container_name(preferred_name: str, instance_id: str) -> str:
    suffix = re.sub(r"[^0-9A-Za-z_.-]+", "_", instance_id).strip("._")[-80:]
    if not suffix:
        raise ValueError("instance_id is empty after sanitization")
    prefix = preferred_name[: max(1, 240 - len(suffix))].rstrip(".")
    return f"{prefix}.{suffix}"


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
    if name.startswith(prefix) and ".brt6." not in name:
        return True
    # TDD-Bench's official image includes the architecture, while its
    # get_instance_container_name() omits it. Both still identify the same
    # instance; Docker's Config.Image check below verifies the exact image.
    if expected_image.startswith("sweb.eval."):
        parts = expected_image.removesuffix(":latest").split(".", 3)
        if len(parts) == 4:
            return name.startswith(f"sweb.eval.{parts[3]}.") and ".brt6." not in name
    return False


def _not_found(error: Exception) -> bool:
    return error.__class__.__name__ in {"NotFound", "ImageNotFound"}


class OfficialContainerRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_REGISTRY_PATH
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def lookup(self, instance_id: str) -> dict[str, str] | None:
        """Return a frozen registry identity without touching Docker."""

        with self._locked() as payload:
            entry = payload["instances"].get(instance_id)
            if not isinstance(entry, dict):
                return None
            image = str(entry.get("image") or "")
            name = str(entry.get("container_name") or "")
            if not image or not name:
                return None
            return {"image": image, "container_name": name}

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
            owners_by_name: dict[str, list[str]] = {}
            for owner, value in entries.items():
                if not isinstance(value, dict):
                    continue
                registered_name = str(value.get("container_name") or "")
                if registered_name:
                    owners_by_name.setdefault(registered_name, []).append(owner)
            owned_names = {
                name: sorted(owners)[0] for name, owners in owners_by_name.items()
            }
            entry = entries.get(instance_id)
            if entry is not None:
                name = str(entry.get("container_name") or "")
                image = str(entry.get("image") or "")
                if len(owners_by_name.get(name, [])) > 1 and owned_names[name] != instance_id:
                    # Repair registries created before one-container/one-instance
                    # ownership was enforced. The lexical first owner keeps the
                    # cached container; every other owner receives a new name.
                    entries.pop(instance_id, None)
                    entry = None
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
                        # This container is exclusively owned by the current
                        # registry entry. Remove a dead/stale Docker object and
                        # reserve the same official name for clean recreation.
                        container.remove(force=True)
                        return ContainerResolution(
                            name=name,
                            container=None,
                            reused=False,
                            adopted=False,
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
                if name in owned_names and owned_names[name] != instance_id:
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
            if container is not None:
                name = str(container.name)
            elif preferred_name in owned_names and owned_names[preferred_name] != instance_id:
                name = instance_scoped_container_name(preferred_name, instance_id)
            else:
                name = preferred_name
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
            duplicate_owner = next(
                (
                    owner
                    for owner, value in payload["instances"].items()
                    if owner != instance_id
                    and isinstance(value, dict)
                    and value.get("container_name") == container_name
                ),
                None,
            )
            if duplicate_owner is not None:
                raise ContainerRegistryError(
                    f"container {container_name} is already owned by {duplicate_owner}"
                )
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
