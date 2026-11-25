"""Masking of secret thing configuration values (passwords, tokens, API keys).

Thing configurations can contain credentials (UniFi controller password, Protect API
token, cloud API keys, ...). Everything a tool returns ends up in the LLM context, so
by default these values are replaced with a placeholder before they leave the server.

A configuration parameter counts as secret if
  - its config description in the thing type declares context "password", or
  - its name matches one of the secret name patterns (case-insensitive).

Configuration (environment):
  OPENHAB_MCP_REDACT_SECRETS     "true" (default) / "false" — switch masking off entirely
  OPENHAB_MCP_SECRET_PARAMS      comma-separated extra parameter names to always mask
  OPENHAB_MCP_NON_SECRET_PARAMS  comma-separated parameter names never to mask
                                 (for false positives of the name patterns)

Values that only reference a variable (e.g. "${env:UNIFI_PASSWORD}") are not secret
themselves and stay visible.

When a masked thing is sent back through update_thing, placeholder values are replaced
by the stored value again, so a round trip never overwrites a credential with the
placeholder.
"""

import logging
import os
import re
from typing import Any, Dict, Iterable, Optional, Set

logger = logging.getLogger(__name__)

PLACEHOLDER = "***REDACTED***"

# Name fragments that mark a parameter as secret. Matched against the lower-cased
# parameter name with non-alphanumerics removed (apiKey, api_key, api-key → apikey).
_SECRET_FRAGMENTS = (
    "password", "passwd", "passphrase", "secret", "token", "apikey", "accesskey",
    "privatekey", "credential", "clientsecret", "authkey", "sessionkey",
)
# Exact (normalized) names that are secret but too short to match as fragments.
_SECRET_EXACT = {"pin", "pincode", "pwd", "key", "psk", "auth"}

_VARIABLE_REF = re.compile(r"^\$\{[^}]+\}$")


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_set(name: str) -> Set[str]:
    return {_normalize(v) for v in os.environ.get(name, "").split(",") if v.strip()}


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def redaction_enabled() -> bool:
    return _env_bool("OPENHAB_MCP_REDACT_SECRETS", True)


def is_secret_name(name: str) -> bool:
    n = _normalize(name)
    if n in _env_set("OPENHAB_MCP_NON_SECRET_PARAMS"):
        return False
    if n in _env_set("OPENHAB_MCP_SECRET_PARAMS"):
        return True
    return n in _SECRET_EXACT or any(f in n for f in _SECRET_FRAGMENTS)


def is_variable_reference(value: Any) -> bool:
    return isinstance(value, str) and bool(_VARIABLE_REF.match(value.strip()))


class SecretMasker:
    """Determines secret parameters per thing type and masks/unmasks configurations."""

    def __init__(self, fetch_thing_type):
        # fetch_thing_type(thing_type_uid) -> thing type dict (or None); injected so the
        # masker can be tested without HTTP and reused by the client.
        self._fetch_thing_type = fetch_thing_type
        self._password_params: Dict[str, Set[str]] = {}

    def _declared_password_params(self, thing_type_uid: Optional[str]) -> Set[str]:
        if not thing_type_uid:
            return set()
        if thing_type_uid not in self._password_params:
            names: Set[str] = set()
            try:
                thing_type = self._fetch_thing_type(thing_type_uid) or {}
                for param in thing_type.get("configParameters", []) or []:
                    if str(param.get("context", "")).lower() == "password" and param.get("name"):
                        names.add(param["name"])
            except Exception as e:  # thing type lookup is best effort; name patterns still apply
                logger.debug(f"Config description for {thing_type_uid} unavailable: {e}")
            self._password_params[thing_type_uid] = names
        return self._password_params[thing_type_uid]

    def secret_keys(self, thing_type_uid: Optional[str], keys: Iterable[str]) -> Set[str]:
        declared = self._declared_password_params(thing_type_uid)
        non_secret = _env_set("OPENHAB_MCP_NON_SECRET_PARAMS")
        return {
            k for k in keys
            if (k in declared and _normalize(k) not in non_secret) or is_secret_name(k)
        }

    def mask_thing(self, thing: Dict[str, Any]) -> Dict[str, Any]:
        """Mask secret configuration values of a thing dict in place and return it."""
        if not redaction_enabled() or not isinstance(thing, dict):
            return thing
        config = thing.get("configuration")
        if not isinstance(config, dict) or not config:
            return thing
        for key in self.secret_keys(thing.get("thingTypeUID"), config.keys()):
            value = config.get(key)
            # Only strings and numbers can hold a credential (a numeric PIN); flags such
            # as tokenAutoManaged=true stay visible.
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                continue
            if value == "" or is_variable_reference(value):
                continue
            config[key] = PLACEHOLDER
        return thing

    @staticmethod
    def restore_placeholders(new_config: Dict[str, Any], stored_config: Dict[str, Any]) -> Dict[str, Any]:
        """Replace placeholder values in new_config by the stored values."""
        restored = dict(new_config)
        for key, value in new_config.items():
            if value == PLACEHOLDER:
                if key in stored_config:
                    restored[key] = stored_config[key]
                else:
                    restored.pop(key)
        return restored
