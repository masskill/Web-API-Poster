"""Platform rules loaded from platforms.yaml."""
from functools import lru_cache

import yaml

from app import config


@lru_cache
def load_platforms() -> dict[str, dict]:
    with open(config.PLATFORMS_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def platform_name(key: str) -> str:
    return load_platforms().get(key, {}).get("name", key)
