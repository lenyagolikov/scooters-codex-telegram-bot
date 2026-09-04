from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "scooters-codex-telegram-bot"


class ConfigError(ValueError):
    pass


def default_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME / ".env"


def default_state_path() -> Path:
    if sys.platform == "win32":
        base = Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        )
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / APP_NAME / "state.sqlite3"


def find_config_path(explicit_path: Path | None = None) -> Path:
    if explicit_path is not None:
        return explicit_path.expanduser()
    if configured_path := os.environ.get("BOT_CONFIG_PATH", "").strip():
        return Path(configured_path).expanduser()
    working_directory_config = Path.cwd() / ".env"
    if working_directory_config.is_file():
        return working_directory_config
    return default_config_path()


def load_dotenv(path: Path) -> None:
    """Load a small .env file without overwriting real environment variables."""
    if not path.is_file():
        return

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"Invalid .env entry at line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {'\"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _parse_user_ids(raw_value: str) -> frozenset[int]:
    if not raw_value.strip():
        return frozenset()

    result: set[int] = set()
    for value in raw_value.split(","):
        try:
            result.add(int(value.strip()))
        except ValueError as error:
            raise ConfigError(
                "TELEGRAM_ALLOWED_USER_IDS must contain numeric IDs"
            ) from error
    return frozenset(result)


def _parse_bool(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _parse_positive_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer") from error
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    codex_cwd: Path
    codex_bin: str
    codex_model: str | None
    reasoning_effort: str | None
    state_path: Path
    poll_timeout_seconds: int = 30
    telegram_ip_family: str = "auto"
    voice_transcription_enabled: bool = False
    whisper_model: str = "small"
    whisper_language: str | None = "ru"
    voice_max_duration_seconds: int = 600
    voice_max_file_bytes: int = 20 * 1024 * 1024
    auto_approve_safe_read_only: bool = False
    auto_approve_read_roots: tuple[Path, ...] = ()

    @classmethod
    def from_environment(cls, config_path: Path | None = None) -> Config:
        load_dotenv(find_config_path(config_path))

        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is not configured")

        codex_bin = os.environ.get("CODEX_BIN", "codex").strip() or "codex"
        if shutil.which(codex_bin) is None:
            raise ConfigError(f"Codex executable not found: {codex_bin}")

        codex_cwd = Path(os.environ.get("CODEX_CWD", str(Path.cwd()))).expanduser()
        if not codex_cwd.is_dir():
            raise ConfigError(f"CODEX_CWD is not a directory: {codex_cwd}")
        codex_cwd = codex_cwd.resolve()

        state_path_value = os.environ.get("BOT_STATE_PATH", "").strip()
        state_path = (
            Path(state_path_value).expanduser()
            if state_path_value
            else default_state_path()
        )
        model = os.environ.get("CODEX_MODEL", "").strip() or None
        effort = os.environ.get("CODEX_REASONING_EFFORT", "").strip() or None
        ip_family = os.environ.get("TELEGRAM_IP_FAMILY", "auto").strip().lower()
        if ip_family not in {"auto", "ipv4", "ipv6"}:
            raise ConfigError("TELEGRAM_IP_FAMILY must be auto, ipv4 or ipv6")

        whisper_language = os.environ.get("WHISPER_LANGUAGE", "ru").strip() or None
        read_roots_value = os.environ.get("AUTO_APPROVE_READ_ROOTS", "").strip()
        read_roots = tuple(
            Path(value.strip()).expanduser().resolve()
            for value in read_roots_value.split(",")
            if value.strip()
        ) or (codex_cwd,)

        return cls(
            telegram_token=token,
            allowed_user_ids=_parse_user_ids(
                os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
            ),
            codex_cwd=codex_cwd,
            codex_bin=codex_bin,
            codex_model=model,
            reasoning_effort=effort,
            state_path=state_path,
            poll_timeout_seconds=_parse_positive_int("POLL_TIMEOUT_SECONDS", 30),
            telegram_ip_family=ip_family,
            voice_transcription_enabled=_parse_bool("VOICE_TRANSCRIPTION_ENABLED"),
            whisper_model=os.environ.get("WHISPER_MODEL", "small").strip()
            or "small",
            whisper_language=whisper_language,
            voice_max_duration_seconds=_parse_positive_int(
                "VOICE_MAX_DURATION_SECONDS", 600
            ),
            voice_max_file_bytes=_parse_positive_int(
                "VOICE_MAX_FILE_BYTES", 20 * 1024 * 1024
            ),
            auto_approve_safe_read_only=_parse_bool(
                "AUTO_APPROVE_SAFE_READ_ONLY"
            ),
            auto_approve_read_roots=read_roots,
        )
