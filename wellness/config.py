"""Configuration loading. Stdlib only."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_CONFIG_NAME = "config.toml"

# Secrets can come from the environment instead of the config file, so the
# file itself can stay free of credentials. The environment always wins.
ENV_OVERRIDES = {
    "client_id": "WITHINGS_CLIENT_ID",
    "client_secret": "WITHINGS_CLIENT_SECRET",
}

# [display] units. Display only: storage keeps each source's own units.
UNITS = ("imperial", "metric")


def _expand(p: str) -> Path:
    return Path(p).expanduser()


@dataclass
class Config:
    wellness_db: Path
    garmin_db_dir: Path
    tonal_export_dir: Path
    archive_dir: Path
    withings_token: Path
    dashboard_dir: Path
    timezone: ZoneInfo
    tz_name: str
    units: str = "imperial"
    tonal: dict = field(default_factory=dict)
    garmin: dict = field(default_factory=dict)
    withings: dict = field(default_factory=dict)
    # Where each Withings secret was found: 'env' or 'config'. Never the value.
    secret_sources: dict = field(default_factory=dict)

    @property
    def tonal_archive_dir(self) -> Path:
        return self.archive_dir / "tonal"

    @property
    def withings_archive_dir(self) -> Path:
        return self.archive_dir / "withings"


def load(path: str | Path | None = None) -> Config:
    cfg_path = Path(path) if path else Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_NAME
    if not cfg_path.exists():
        raise SystemExit(
            f"No config at {cfg_path}. Copy config.example.toml to config.toml and edit it."
        )
    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)

    paths = raw.get("paths", {})
    tz_name = raw.get("local", {}).get("timezone", "UTC")
    units = str(raw.get("display", {}).get("units", "imperial")).lower()
    if units not in UNITS:
        raise SystemExit(f"[display] units must be one of {', '.join(UNITS)}, not {units!r}.")

    # Pull credentials from the environment when present. This lets the
    # config file stay credential-free - keep the real values in `pass`,
    # export them in the shell, and nothing secret is ever on disk in
    # plaintext or in a file anyone reviews.
    withings_cfg = dict(raw.get("withings", {}))
    sources: dict[str, str] = {}
    for key, env_name in ENV_OVERRIDES.items():
        env_value = os.environ.get(env_name)
        if env_value:
            withings_cfg[key] = env_value
            sources[key] = "env"
        elif withings_cfg.get(key):
            sources[key] = "config"
        else:
            sources[key] = "missing"

    return Config(
        wellness_db=_expand(paths.get("wellness_db", "~/HealthData/wellness.db")),
        garmin_db_dir=_expand(paths.get("garmin_db_dir", "~/HealthData/DBs")),
        tonal_export_dir=_expand(paths.get("tonal_export_dir", "~/HealthData/tonal_exports")),
        archive_dir=_expand(paths.get("archive_dir", "~/HealthData/raw")),
        withings_token=_expand(paths.get("withings_token", "~/.secrets/withings_token.json")),
        dashboard_dir=_expand(paths.get("dashboard_dir", "~/HealthData/dashboard")),
        timezone=ZoneInfo(tz_name),
        tz_name=tz_name,
        units=units,
        tonal=raw.get("tonal", {}),
        garmin=raw.get("garmin", {}),
        withings=withings_cfg,
        secret_sources=sources,
    )
