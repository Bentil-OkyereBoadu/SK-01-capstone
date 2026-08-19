"""Typed access to pipeline configuration."""
from pathlib import Path
import yaml


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # Fail fast if a required section is missing
    for key in ("paths", "quality_gate", "dedup", "exchange_rates_to_usd"):
        if key not in cfg:
            raise KeyError(f"Config missing required section: '{key}'")
    return cfg