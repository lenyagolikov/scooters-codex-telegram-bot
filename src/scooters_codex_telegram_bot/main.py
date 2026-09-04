from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from . import __version__
from .app_server import CodexAppServer
from .bot import TelegramCodexBot
from .config import Config, ConfigError, find_config_path
from .state import StateStore
from .telegram_api import TelegramApi
from .transcription import VoiceTranscriber


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scooters-codex-telegram-bot",
        description="Bridge private Telegram chats to Codex App Server.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="path to the .env configuration file",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration without starting the bot",
    )
    parser.add_argument(
        "--show-config-path",
        action="store_true",
        help="print the configuration file path and exit",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config_path = find_config_path(args.config)
    if args.show_config_path:
        print(config_path)
        return

    try:
        config = Config.from_environment(config_path)
        _validate_optional_features(config)
    except ConfigError as error:
        raise SystemExit(f"Configuration error: {error}") from error

    if args.check:
        _print_configuration_summary(config, config_path)
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    voice_transcriber = (
        VoiceTranscriber(config.whisper_model, config.whisper_language)
        if config.voice_transcription_enabled
        else None
    )
    bot = TelegramCodexBot(
        config=config,
        telegram=TelegramApi(config.telegram_token, config.telegram_ip_family),
        app_server=CodexAppServer(config.codex_bin),
        state=StateStore(config.state_path),
        voice_transcriber=voice_transcriber,
    )
    with suppress(KeyboardInterrupt):
        asyncio.run(bot.run())


def _validate_optional_features(config: Config) -> None:
    if (
        config.voice_transcription_enabled
        and importlib.util.find_spec("faster_whisper") is None
    ):
        raise ConfigError(
            "voice transcription is enabled, but the voice extra is not installed; "
            "run: python -m pip install '.[voice]'"
        )


def _print_configuration_summary(config: Config, config_path: Path) -> None:
    print("Configuration is valid.")
    print(f"Config: {config_path}")
    print(f"Codex executable: {config.codex_bin}")
    print(f"Codex working directory: {config.codex_cwd}")
    print(f"State database: {config.state_path}")
    print(f"Allowed Telegram users: {len(config.allowed_user_ids)}")
    voice_status = (
        f"enabled ({config.whisper_model})"
        if config.voice_transcription_enabled
        else "disabled"
    )
    print(f"Voice transcription: {voice_status}")
    print(
        "Safe read-only auto-approval: "
        + ("enabled" if config.auto_approve_safe_read_only else "disabled")
    )


if __name__ == "__main__":
    main()
