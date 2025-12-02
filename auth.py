import toml
from typing import Optional

from core import logger


def verify_agent_apikey(agentid: str, apikey: str) -> bool:
    """
    Verify that the given API key is valid for the given agentid.

    Returns True if a matching entry exists in conf/apikeys.toml with:
      - type = "agent"
      - table name == agentid
      - key == apikey
    """
    if not agentid or not apikey:
        return False

    try:
        config = toml.load("conf/apikeys.toml")
    except Exception as e:
        logger.error("Error while loading apikeys.toml: %s", e)
        return False

    entry = config.get(agentid)
    if not entry:
        return False

    if entry.get("type") != "agent":
        return False

    stored_key = entry.get("key")
    if not stored_key:
        return False

    return stored_key == apikey
