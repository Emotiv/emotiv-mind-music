"""Developer defaults. Everything the user configures lives in settings.py."""
import os
import sys


def _resource(*parts: str) -> str:
    """A read-only file shipped with the app, from source or from a bundle."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


class Config:
    CORTEX_URL = os.getenv("CORTEX_URL", "wss://localhost:6868")
    # Cortex serves wss:// with a self-signed chain; its root CA travels with
    # the app so the connection can be verified rather than trusted blindly.
    CORTEX_CERT_PATH = os.getenv("CORTEX_CERT_PATH", _resource("certificates", "rootCA.pem"))
