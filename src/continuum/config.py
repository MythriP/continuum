"""Where Continuum keeps things at runtime.

Every location defaults to a folder under the working directory, and every one
can be overridden by an environment variable, so the same code runs from a
checkout, an installed package or a container.

  CONTINUUM_HOME   base folder                      (default: working directory)
  CONTINUUM_VAR    runtime state: databases, cache  (default: $CONTINUUM_HOME/var)
"""
import os
from pathlib import Path

HOME = Path(os.environ.get("CONTINUUM_HOME", Path.cwd()))
VAR = Path(os.environ.get("CONTINUUM_VAR", HOME / "var"))


def load_env() -> None:
    """Read API keys from $CONTINUUM_HOME/.env, if present. Never overrides the environment."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(HOME / ".env", override=False)
