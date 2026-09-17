"""EMOTIV Mind Music — control Spotify with trained mental commands.

The window is a local WebView. Python exposes an API to the page, and pushes
events back into it with evaluate_js. Everything the user configures lives in
~/.emotiv_mind_music, so an installed build needs no .env and no files beside it.
"""
import json
import os
import sys
import threading
from typing import Any, Dict

import webview

from controls import TriggerSettings
from engine import Engine
from settings import settings

APP_TITLE = "EMOTIV Mind Music"
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

# What the settings form is allowed to write. Tokens are deliberately absent:
# only the sign-in flow may set those, never a value arriving from the page.
EDITABLE = {
    "language", "client_id", "client_secret", "spotify_client_id",
    "trigger_threshold", "trigger_hold", "trigger_cooldown",
}


def resource_dir() -> str:
    """Works both from source and inside a PyInstaller bundle."""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "ui")
    return UI_DIR


class Api:
    """Everything public on this object is callable from JavaScript.

    pywebview exposes the js_api object by walking `dir()` and recursing into
    every attribute whose name does not start with an underscore. A public
    `window` or `engine` attribute sends it crawling through the whole pywebview
    Window, the asyncio loop and the thread pools before the page is allowed to
    start — measured on Windows with pywebview 6.2: the window never became
    responsive. So state lives in underscored attributes, and the only public
    names are the methods the page is meant to call.
    """

    def __init__(self):
        self._window = None
        self._engine = Engine(self._push)
        self._lock = threading.Lock()
        self._closing = False

    # ------------------------------------------------------------ to the page
    def _push(self, event: str, data: Dict[str, Any]):
        if not self._window or self._closing:
            # Never touch the webview while it is going away. pywebview runs the
            # `closing` handler synchronously on the UI thread, and evaluate_js
            # schedules onto that same thread and blocks for the result:
            # emitting from the shutdown path deadlocks the app.
            return
        payload = json.dumps({"event": event, "data": data}, ensure_ascii=False)
        try:
            self._window.evaluate_js(
                f"(function(){{var m={payload};window.pushEvent(m.event,m.data);}})()"
            )
        except Exception:
            pass

    # ------------------------------------------------------------- from the page
    def get_state(self):
        defaults = TriggerSettings()
        return {
            "settings": settings.public_dict(),
            "running": self._engine.running,
            "redirect_uri": self._engine.spotify_redirect_uri(),
            "spotify_signed_in": self._engine.tokens.signed_in,
            "trigger_defaults": {
                "threshold": defaults.threshold,
                "hold": defaults.hold,
                "cooldown": defaults.cooldown,
            },
        }

    def page_ready(self):
        """The page has wired its event handler: now it can hear about Spotify."""
        self._engine._emit_spotify()
        if self._engine.tokens.signed_in:
            self._engine.start_polling()
        return {"ok": True}

    def save_settings(self, values: Dict[str, Any]):
        values = {k: v for k, v in (values or {}).items() if k in EDITABLE}
        with self._lock:
            # A changed Spotify app invalidates the session from the old one:
            # its refresh token belongs to a different Client ID.
            new_client = values.get("spotify_client_id")
            if new_client is not None and new_client.strip() != settings.get("spotify_client_id", ""):
                self._engine.tokens.clear()
            settings.update(values)
            settings.save()
        self._engine.apply_trigger_settings()
        self._engine._emit_spotify()
        return settings.public_dict()

    def start(self):
        return self._engine.start()

    def stop(self):
        return self._engine.stop()

    def refresh_headsets(self):
        return self._engine.refresh_headsets()

    def select_headset(self, headset_id: str):
        return self._engine.select_headset(headset_id)

    def select_profile(self, name: str):
        return self._engine.select_profile(name)

    def set_sensitivity(self, values):
        return self._engine.set_sensitivity(values)

    # --------------------------------------------------------------- training
    def create_profile(self, name: str):
        return self._engine.create_profile(name)

    def set_active_actions(self, actions):
        return self._engine.set_active_actions(actions or [])

    def start_training(self, action: str):
        return self._engine.start_training(action)

    def accept_training(self):
        return self._engine.accept_training()

    def reject_training(self):
        return self._engine.reject_training()

    def erase_training(self, action: str):
        return self._engine.erase_training(action)

    def reset_training(self):
        return self._engine.reset_training()

    def training_result(self):
        return self._engine.training_result()

    # --------------------------------------------------------------- controls
    def set_binding(self, command: str, action: str):
        return self._engine.set_binding(command, action)

    def set_mind_control(self, on: bool):
        return self._engine.set_mind_control(bool(on))

    def player_action(self, action: str):
        return self._engine.player_action(action)

    # ---------------------------------------------------------------- Spotify
    def spotify_login(self):
        return self._engine.spotify_login()

    def spotify_cancel_login(self):
        return self._engine.spotify_cancel_login()

    def spotify_logout(self):
        return self._engine.spotify_logout()

    def spotify_refresh(self):
        return self._engine.spotify_refresh()

    def spotify_devices(self):
        return self._engine.spotify_devices()

    def spotify_play_on(self, device_id: str):
        return self._engine.spotify_play_on(device_id)

    def open_url(self, url: str):
        """Open a link in the user's browser rather than inside the app window."""
        import webbrowser
        if isinstance(url, str) and url.startswith(("https://", "http://127.0.0.1")):
            webbrowser.open(url)
            return {"ok": True}
        return {"ok": False}


def main():
    api = Api()
    window = webview.create_window(
        APP_TITLE,
        os.path.join(resource_dir(), "index.html"),
        js_api=api,
        width=1220,
        height=820,
        min_size=(940, 660),
        background_color="#0c0e12",
    )
    api._window = window

    def on_closing():
        # Silence the bridge first, then tear down: the teardown emits status
        # events, and any of them would deadlock the UI thread.
        api._closing = True
        try:
            api._engine.shutdown()
        except Exception:
            pass

    def on_loaded():
        try:
            error = window.evaluate_js("window.__bootError || ''")
            if error:
                print(f"[ui] bootstrap error: {error}", file=sys.stderr)
        except Exception:
            pass

    window.events.loaded += on_loaded
    window.events.closing += on_closing
    # http_server=True serves ui/ from 127.0.0.1 instead of a file:// URL, which
    # fails silently inside a packaged macOS bundle whose path has spaces.
    webview.start(http_server=True)


if __name__ == "__main__":
    main()
