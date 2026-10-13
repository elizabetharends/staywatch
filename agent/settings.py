"""Loads settings from config.toml, with STAYWATCH_<SECTION>_<KEY> environment overrides.

The same code runs on a laptop, in Docker and on GitHub Actions, so nothing is hard-coded.
"""
import os
import tomllib
from pathlib import Path

ROOT = Path(os.environ.get("STAYWATCH_ROOT", Path(__file__).resolve().parents[1]))


def load_settings(config_path: Path | None = None) -> dict:
    path = config_path or ROOT / "config.toml"
    with open(path, "rb") as f:
        settings = tomllib.load(f)
    for section, values in settings.items():
        for key, default in values.items():
            env_value = os.environ.get(f"STAYWATCH_{section}_{key}".upper())
            if env_value is not None:
                values[key] = type(default)(env_value)
    return settings


def resolve(path_value: str) -> Path:
    """Turns a config path into an absolute path under the project root."""
    path = Path(path_value)
    return path if path.is_absolute() else ROOT / path
