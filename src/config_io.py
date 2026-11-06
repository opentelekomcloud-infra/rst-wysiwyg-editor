"""YAML config save/load for the RST editor.

The configuration (repo URL, branches, SSH key, commit identity, API token,
provider, API base, proxy) is stored as YAML so the user can save it once and
reload it later instead of re-typing.

Security: this is a local, single-user tool. Config paths are expanded and
sanitized; files are written 0600 (owner-only) because they hold secrets.
"""
import os
import re
from typing import Dict, Any

import yaml

# Keys that are part of a saved configuration.
CONFIG_KEYS = [
    "url", "source_branch", "target_branch",
    "upstream",
    "ssh_key", "ssh_host", "ssh_port",
    "commit_name", "commit_email",
    "api_token", "provider", "api_base", "proxy",
    "work_root",
]

DEFAULT_PATH = os.path.expanduser("~/.rst-editor/config.yaml")


class ConfigError(Exception):
    pass


def _clean_path(path: str) -> str:
    """Expand ~ and return an absolute path; reject clearly unsafe values."""
    p = (path or "").strip()
    if not p:
        raise ConfigError("Config file path is required.")
    p = os.path.expanduser(p)
    p = os.path.normpath(p)
    if not os.path.isabs(p):
        raise ConfigError("Config path must be absolute (e.g. ~/.rst-editor/config.yaml).")
    if not re.search(r"\.(ya?ml)$", p, re.I):
        raise ConfigError("Config file must end in .yaml or .yml.")
    return p


def save_config(path: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Write the configuration to a YAML file (0600). Returns {path, yaml}."""
    p = _clean_path(path)
    # Only persist known keys (ignore stray values), keep order.
    clean = {k: (data.get(k, "") or "") for k in CONFIG_KEYS}
    # Drop empty ssh/port/optional fields? Keep them for fidelity; blank is fine.
    os.makedirs(os.path.dirname(p), exist_ok=True)
    content = yaml.safe_dump(clean, default_flow_style=False, sort_keys=False)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    os.chmod(p, 0o600)
    return {"path": p, "yaml": content}


def load_config(path: str) -> Dict[str, Any]:
    """Read the configuration from a YAML file. Returns {path, config}."""
    p = _clean_path(path)
    if not os.path.isfile(p):
        raise ConfigError("Config file not found: " + p)
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ConfigError("Config file is not a valid mapping.")
    config = {k: (raw.get(k, "") or "") for k in CONFIG_KEYS}
    # Coerce ssh_port to int-like string for the UI.
    if config.get("ssh_port"):
        try:
            config["ssh_port"] = str(int(str(config["ssh_port"])))
        except (ValueError, TypeError):
            config["ssh_port"] = "22"
    return {"path": p, "config": config}


def default_path() -> str:
    return DEFAULT_PATH
