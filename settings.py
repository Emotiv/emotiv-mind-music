"""User settings, persisted to disk.

Everything the user types or chooses survives closing the app here — with the
installer in mind, where no .env exists. Credentials live in this file too,
which is why it is written with mode 600 and why public_dict() never hands a
secret or a token back to the interface.
"""
import json
import os
import threading
from typing import Any, Dict

APP_DIR = os.path.expanduser("~/.emotiv_mind_music")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

DEFAULTS: Dict[str, Any] = {
    "language": None,            # None = not chosen yet, triggers the language screen

    # EMOTIV Cortex application.
    "client_id": "",
    "client_secret": "",
    "headset_id": "",
    "profile": "",

    # Spotify. Only a Client ID: the PKCE flow needs no secret, so there is none
    # to leak out of an installed app.
    "spotify_client_id": "",
    "spotify_refresh_token": "",
    "spotify_access_token": "",
    "spotify_token_expires": 0.0,
    # The device the user chose to play on. Spotify ids are "persistent to some
    # extent", not guaranteed, so this is a preference, never a requirement.
    "spotify_device_id": "",

    # profile name -> {mental command -> player action}. Per profile, because
    # two profiles rarely train the same commands.
    "bindings": {},

    # Whether a detected command is allowed to touch the music at all.
    "mind_control": True,

    # When a command counts as intended. See controls.py for what each does.
    "trigger_threshold": 0.6,
    "trigger_hold": 0.5,
    "trigger_cooldown": 2.0,
}

# Never sent to the interface. The UI only learns whether each is set.
SECRET_KEYS = ("client_secret", "spotify_refresh_token", "spotify_access_token")


class Settings:
    """Thread-safe persistent dictionary."""

    def __init__(self, path: str = SETTINGS_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data = json.loads(json.dumps(DEFAULTS))
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            with self._lock:
                for key, value in stored.items():
                    if key in DEFAULTS:
                        self._data[key] = value
        except Exception as e:
            print(f"[settings] could not read {self.path}: {e}; using defaults")

    def save(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with self._lock:
                snapshot = json.loads(json.dumps(self._data))
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
            # Credentials and tokens live in here; keep other users out.
            os.chmod(self.path, 0o600)
            return True
        except Exception as e:
            print(f"[settings] failed to save: {e}")
            return False

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value):
        with self._lock:
            self._data[key] = value

    def update(self, values: Dict[str, Any]):
        with self._lock:
            for key, value in values.items():
                if key in DEFAULTS:
                    self._data[key] = value

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def public_dict(self) -> Dict[str, Any]:
        """as_dict, minus every secret. The UI gets `<key>_set` flags instead."""
        data = self.as_dict()
        for key in SECRET_KEYS:
            data[key + "_set"] = bool(data.get(key))
            data[key] = ""
        return data


settings = Settings()
