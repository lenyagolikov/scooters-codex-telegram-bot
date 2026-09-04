from __future__ import annotations

from pathlib import Path
from typing import Any

SAFE_READ_ACTION_TYPES = frozenset({"read", "listFiles", "search"})
SENSITIVE_READ_FRAGMENTS = (
    ".env",
    "/.ssh/",
    "/.codex/",
    "/.stefania/",
    "/.zshrc",
    "/.bashrc",
    "/.bash_history",
    "/etc/shadow",
    "/proc/",
    "id_rsa",
    "id_ed25519",
)
NETWORK_OR_ENV_COMMANDS = (
    "curl ",
    "wget ",
    "ssh ",
    "scp ",
    "rsync ",
    "printenv",
    "/usr/bin/env",
    "/dev/tcp",
)


def is_safe_read_only_approval(
    params: dict[str, Any], allowed_roots: tuple[Path, ...]
) -> bool:
    """Return true only for App Server requests proven to be bounded reads."""
    actions = params.get("commandActions")
    if not isinstance(actions, list) or not actions:
        return False
    if any(
        not isinstance(action, dict)
        or action.get("type") not in SAFE_READ_ACTION_TYPES
        for action in actions
    ):
        return False

    available_decisions = params.get("availableDecisions")
    if isinstance(available_decisions, list) and "accept" not in available_decisions:
        return False
    if params.get("networkApprovalContext") is not None:
        return False

    command = str(params.get("command") or "").lower()
    if any(fragment in command for fragment in NETWORK_OR_ENV_COMMANDS):
        return False
    if any(fragment in command for fragment in SENSITIVE_READ_FRAGMENTS):
        return False

    roots = tuple(root.resolve() for root in allowed_roots)
    if not roots:
        return False
    cwd = approval_path(params.get("cwd"))
    if cwd is None or not path_is_allowed(cwd, roots):
        return False

    for action in actions:
        path = approval_path(action.get("path")) or cwd
        if not path_is_allowed(path, roots, base=Path(cwd)):
            return False

    return _additional_permissions_are_read_only(
        params.get("additionalPermissions"), roots
    )


def approval_path(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("path"), str):
        return str(value["path"])
    return None


def path_is_allowed(
    value: str,
    allowed_roots: tuple[Path, ...],
    *,
    base: Path | None = None,
) -> bool:
    normalized = value.lower().replace("\\", "/")
    if any(fragment in normalized for fragment in SENSITIVE_READ_FRAGMENTS):
        return False
    try:
        path = Path(value)
        if not path.is_absolute():
            if base is None:
                return False
            path = base / path
        path = path.resolve()
    except (OSError, RuntimeError):
        return False
    return any(path == root or root in path.parents for root in allowed_roots)


def _additional_permissions_are_read_only(
    permissions: Any, allowed_roots: tuple[Path, ...]
) -> bool:
    if permissions is None:
        return True
    if not isinstance(permissions, dict):
        return False
    network = permissions.get("network")
    if isinstance(network, dict) and network.get("enabled"):
        return False
    file_system = permissions.get("fileSystem")
    if file_system is None:
        return True
    if not isinstance(file_system, dict) or file_system.get("write"):
        return False
    for value in file_system.get("read") or []:
        path = approval_path(value)
        if path is None or not path_is_allowed(path, allowed_roots):
            return False
    for entry in file_system.get("entries") or []:
        if not isinstance(entry, dict) or entry.get("access") != "read":
            return False
        path = approval_path(entry.get("path"))
        if path is None or not path_is_allowed(path, allowed_roots):
            return False
    return True
