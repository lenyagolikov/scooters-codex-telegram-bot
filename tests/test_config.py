from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scooters_codex_telegram_bot.config import (
    Config,
    ConfigError,
    default_config_path,
    default_state_path,
)


class ConfigTests(unittest.TestCase):
    def test_explicit_config_file_is_loaded_without_overriding_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "bot.env"
            config_path.write_text(
                "TELEGRAM_BOT_TOKEN=from-file\n"
                "TELEGRAM_ALLOWED_USER_IDS=101,202\n"
                f"CODEX_BIN={sys.executable}\n"
                f"CODEX_CWD={root}\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "from-environment"},
                clear=True,
            ):
                config = Config.from_environment(config_path)

            self.assertEqual(config.telegram_token, "from-environment")
            self.assertEqual(config.allowed_user_ids, frozenset({101, 202}))
            self.assertEqual(config.codex_cwd, root.resolve())

    def test_default_linux_paths_follow_xdg_variables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("scooters_codex_telegram_bot.config.sys.platform", "linux"),
                patch.dict(
                    os.environ,
                    {
                        "XDG_CONFIG_HOME": str(root / "config"),
                        "XDG_STATE_HOME": str(root / "state"),
                    },
                    clear=True,
                ),
            ):
                self.assertEqual(
                    default_config_path(),
                    root / "config" / "scooters-codex-telegram-bot" / ".env",
                )
                self.assertEqual(
                    default_state_path(),
                    root / "state" / "scooters-codex-telegram-bot" / "state.sqlite3",
                )

    def test_invalid_ip_family_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "test-token",
                    "CODEX_BIN": sys.executable,
                    "CODEX_CWD": str(root),
                    "TELEGRAM_IP_FAMILY": "satellite",
                },
                clear=True,
            ), self.assertRaisesRegex(ConfigError, "TELEGRAM_IP_FAMILY"):
                Config.from_environment(root / "missing.env")


if __name__ == "__main__":
    unittest.main()
