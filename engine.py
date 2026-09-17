"""Cortex in, Spotify out, and the rules in between.

Three kinds of thread meet here, and each is kept to its lane:

  * The engine loop (asyncio, daemon thread) runs the Cortex client. `com`
    samples arrive on it eight times a second, so nothing on it may block —
    in particular, no HTTP to Spotify.
  * One Spotify worker thread runs every Web API call in order. A queue of one
    means a fired command and a click on the same button can never race each
    other into play-then-pause.
  * pywebview's thread calls the synchronous methods below from the interface.
"""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from controls import PLAYER_ACTIONS, CommandTrigger, TriggerSettings, resolve_bindings
from cortex_client import CortexClient
from palette import slot_colors
from settings import settings
from spotify.auth import REDIRECT_URI, LoginFlow, SpotifyAuthError, TokenStore
from spotify.player import SpotifyError, SpotifyPlayer

STEP_SPOTIFY = "spotify"

# How often "now playing" is re-read while signed in. Spotify's limit is a
# rolling 30-second window per app; one read every three seconds is far inside
# it and still catches a track change within a breath.
POLL_SECONDS = 3.0

# The device list changes rarely — someone opens Spotify on their phone — so it
# is re-read every few polls rather than on every one, and immediately whenever
# the user opens the picker or moves playback.
DEVICE_EVERY_POLLS = 4

# The live command readout does not need all 8 Hz; 10 frames a second is
# already faster than the eye follows a bar.
LIVE_PUSH_SECONDS = 0.1


def _token_load() -> Dict[str, Any]:
    return {
        "access_token": settings.get("spotify_access_token", ""),
        "expires_at": settings.get("spotify_token_expires", 0.0),
        "refresh_token": settings.get("spotify_refresh_token", ""),
    }


def _token_save(values: Dict[str, Any]):
    settings.update({
        "spotify_access_token": values.get("access_token", ""),
        "spotify_token_expires": values.get("expires_at", 0.0),
        "spotify_refresh_token": values.get("refresh_token", ""),
    })
    settings.save()


class Engine:
    def __init__(self, emit: Callable[[str, Dict[str, Any]], None]):
        self._emit_raw = emit
        self.cortex = CortexClient(self.emit)
        self.cortex.on_command = self._on_command
        self.cortex.on_training_event = self._on_training_event

        self.tokens = TokenStore(lambda: settings.get("spotify_client_id", "").strip(),
                                 _token_load, _token_save)
        self.player = SpotifyPlayer(self.tokens)
        self.player.preferred_device = settings.get("spotify_device_id", "")
        self._devices: List[Dict[str, Any]] = []
        self._spotify = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spotify")
        self._login: Optional[LoginFlow] = None
        self._now_playing: Optional[Dict[str, Any]] = None
        self._poll_wake = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._closing = False

        self.trigger = CommandTrigger(self._trigger_settings())
        self.profile = ""
        self.commands: List[str] = []          # enabled, in slot order
        self.disabled: List[str] = []          # trained but switched off
        self.bindings: Dict[str, str] = {}
        self._training = False
        self._last_live = 0.0

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._cortex_task = None
        self.running = False

    # ------------------------------------------------------------ plumbing
    def emit(self, event: str, data: Dict[str, Any]):
        if event == "commands":
            self._on_roster(data)
        elif event == "actions":
            data = dict(data, colors=slot_colors(self.commands + self.disabled))
        elif event == "fatal":
            # Only the user can fix this one; stop instead of reconnecting in a
            # loop. On another thread, because stop() cancels the caller.
            threading.Thread(target=self.stop, daemon=True).start()
            return
        try:
            self._emit_raw(event, data)
        except Exception as e:
            print(f"[engine] failed to emit {event}: {e}")

    def _status(self, step: str, state: str, code: str = "", **params):
        self.emit("status", {"step": step, "state": state, "code": code, "params": params})

    def _log(self, level: str, code: str, **params):
        self.emit("log", {"level": level, "code": code, "params": params})

    def start_loop(self):
        if self._thread:
            return
        ready = threading.Event()

        def runner():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            ready.set()
            self.loop.run_forever()

        self._thread = threading.Thread(target=runner, name="engine-loop", daemon=True)
        self._thread.start()
        ready.wait(5.0)

    def _submit(self, coro):
        if not self.loop:
            raise RuntimeError("engine loop is not running")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> Dict[str, Any]:
        if self.running:
            return {"ok": True}
        client_id = settings.get("client_id", "").strip()
        client_secret = settings.get("client_secret", "").strip()
        if not client_id or not client_secret:
            self._status("credentials", "error", "err.no_credentials")
            return {"ok": False, "code": "err.no_credentials"}

        self.start_loop()
        self.trigger.reset()
        self.cortex.desired_profile = settings.get("profile", "")
        self.running = True
        self._cortex_task = self._submit(self.cortex.run(client_id, client_secret))
        self.emit("running", {"running": True})
        return {"ok": True}

    def stop(self) -> Dict[str, Any]:
        self.running = False
        try:
            self.cortex.stop()
        except Exception:
            pass
        if self._cortex_task:
            self._cortex_task.cancel()
            self._cortex_task = None
        self.trigger.reset()
        self._training = False
        if not self._closing:
            for step in ("cortex", "access", "headset", "session", "profile", "stream"):
                self._status(step, "idle")
            self.emit("running", {"running": False})
        return {"ok": True}

    def shutdown(self):
        """The window is closing: stop everything, and emit nothing on the way out."""
        self._closing = True
        self.stop()
        if self._login:
            self._login.cancel()
        self._poll_wake.set()
        self._spotify.shutdown(wait=False, cancel_futures=True)

    # ----------------------------------------------------- Cortex selections
    def _run_step(self, coro, fail_code: str, timeout: float = 90.0) -> Dict[str, Any]:
        """Run one user action on the engine loop and hand back the outcome."""
        if not self.running or not self.loop:
            return {"ok": False, "code": "err.not_running"}
        try:
            return {"ok": True, "result": self._submit(coro).result(timeout=timeout)}
        except Exception as e:
            code = getattr(e, "code", None) or fail_code
            params = getattr(e, "params", None) or {"detail": str(e)}
            return {"ok": False, "code": code, "params": params}

    def refresh_headsets(self):
        return self._run_step(self.cortex.refresh_headsets(force=True), "err.no_headset")

    def select_headset(self, headset_id: str):
        return self._run_step(self.cortex.select_headset(headset_id), "err.headset_not_found")

    def select_profile(self, name: str):
        return self._run_step(self.cortex.select_profile(name), "err.profile_load_failed")

    def set_sensitivity(self, values):
        return self._run_step(self.cortex.set_sensitivity(list(values)),
                              "err.sensitivity_failed", timeout=30.0)

    def create_profile(self, name: str):
        return self._run_step(self.cortex.create_profile(name), "err.profile_create_failed")

    def set_active_actions(self, actions):
        return self._run_step(self.cortex.set_active_actions(list(actions)),
                              "err.active_actions_failed")

    def start_training(self, action: str):
        # Not waited on beyond Cortex accepting it: the eight seconds end with a
        # `sys` event, and blocking here would freeze the countdown.
        return self._run_step(self.cortex.start_training(action), "err.training_failed")

    def accept_training(self):
        return self._run_step(self.cortex.accept_training(), "err.training_failed")

    def reject_training(self):
        return self._run_step(self.cortex.reject_training(), "err.training_failed")

    def erase_training(self, action: str):
        return self._run_step(self.cortex.erase_training(action), "err.training_failed")

    def reset_training(self):
        return self._run_step(self.cortex.reset_training(), "err.training_failed")

    def training_result(self):
        return self._run_step(self.cortex.emit_training_result(), "err.training_failed", timeout=30.0)

    # ----------------------------------------------------------- bindings
    def _on_roster(self, data: Dict[str, Any]):
        """A profile's commands changed: re-resolve what each one does."""
        self.profile = data.get("profile") or self.profile
        self.commands = list(data.get("enabled") or [])
        self.disabled = list(data.get("disabled") or [])
        stored = (settings.get("bindings") or {}).get(self.profile, {})
        self.bindings = resolve_bindings(stored, self.commands + self.disabled)
        # Persist the defaults the first time, so what the user sees is exactly
        # what is saved — and a later default change never silently remaps them.
        if self.profile and self.bindings != stored:
            self._save_bindings()
        self._emit_bindings()

    def _save_bindings(self):
        all_bindings = dict(settings.get("bindings") or {})
        all_bindings[self.profile] = dict(self.bindings)
        settings.set("bindings", all_bindings)
        settings.save()

    def _emit_bindings(self):
        self.emit("bindings", {
            "profile": self.profile,
            "commands": self.commands,
            "disabled": self.disabled,
            "bindings": self.bindings,
            "colors": slot_colors(self.commands + self.disabled),
            "actions": PLAYER_ACTIONS,
        })

    def set_binding(self, command: str, action: str) -> Dict[str, Any]:
        if action not in PLAYER_ACTIONS:
            return {"ok": False, "code": "err.unknown_player_action", "params": {"action": action}}
        if not self.profile:
            return {"ok": False, "code": "err.no_profile_selected", "params": {}}
        self.bindings[command] = action
        self._save_bindings()
        self._emit_bindings()
        return {"ok": True}

    def set_mind_control(self, on: bool) -> Dict[str, Any]:
        settings.set("mind_control", bool(on))
        settings.save()
        self.trigger.reset()
        self.emit("mind_control", {"on": bool(on)})
        return {"ok": True}

    def _trigger_settings(self) -> TriggerSettings:
        return TriggerSettings.clamped(settings.get("trigger_threshold"),
                                       settings.get("trigger_hold"),
                                       settings.get("trigger_cooldown"))

    def apply_trigger_settings(self):
        self.trigger.config = self._trigger_settings()
        self.trigger.reset()

    # --------------------------------------------------------- live commands
    def _on_command(self, action: str, power: float):
        """One `com` sample, on the engine loop. Must never block."""
        binding = self.bindings.get(action, "none")
        armed = bool(settings.get("mind_control", True)) and not self._training

        fired = self.trigger.update(action, power, bound=armed and binding != "none")

        now = time.monotonic()
        if fired or now - self._last_live >= LIVE_PUSH_SECONDS:
            self._last_live = now
            state = self.trigger.state(action, power).as_dict()
            state.update(binding=binding, armed=armed)
            self.emit("live", state)

        if fired:
            self._spotify.submit(self._perform, binding, fired, "mind")

    def _on_training_event(self, event: str, action: str):
        # A detection during a recording is the detector guessing against a
        # signature mid-change. It must never reach the music.
        if event == "MC_Started":
            self._training = True
            self.trigger.reset()
        elif event in ("MC_Succeeded", "MC_Failed", "MC_Completed",
                       "MC_Rejected", "MC_DataErased", "MC_Reset"):
            self._training = False

    # --------------------------------------------------------------- Spotify
    def _perform(self, action: str, command: str, source: str) -> Dict[str, Any]:
        """Runs on the Spotify worker. Reports every outcome to the interface."""
        try:
            done = self.player.perform(action)
        except SpotifyError as e:
            self.emit("fired", {"command": command, "action": action, "source": source,
                                "ok": False, "code": e.code, "params": e.params})
            self._log("error", e.code, **e.params)
            if e.code in ("err.spotify_session_expired", "err.spotify_not_signed_in"):
                self._emit_spotify()
            return {"ok": False, "code": e.code, "params": e.params}

        self.emit("fired", {"command": command, "action": done, "source": source, "ok": True})
        self._log("info", "log.fired." + source, command=command, action=done)
        # Spotify applies a command asynchronously on the playing device; reading
        # back straight away usually returns the old state. A short wait, then
        # the poller re-reads.
        time.sleep(0.35)
        self._poll_wake.set()
        return {"ok": True, "action": done}

    def player_action(self, action: str) -> Dict[str, Any]:
        """A click on one of the player buttons: the same path a thought takes."""
        if action not in PLAYER_ACTIONS or action == "none":
            return {"ok": False, "code": "err.unknown_player_action", "params": {"action": action}}
        try:
            return self._spotify.submit(self._perform, action, "", "click").result(timeout=20)
        except Exception as e:
            return {"ok": False, "code": "err.spotify_api", "params": {"detail": str(e)}}

    def spotify_redirect_uri(self) -> str:
        return REDIRECT_URI

    def spotify_login(self) -> Dict[str, Any]:
        client_id = settings.get("spotify_client_id", "").strip()
        if not client_id:
            return {"ok": False, "code": "err.spotify_no_client_id", "params": {}}
        if self._login:
            self._login.cancel()
        try:
            self._login = LoginFlow(client_id, self._on_login_done)
            self._status(STEP_SPOTIFY, "pending", "status.spotify_waiting_browser")
            self._login.start()
        except SpotifyAuthError as e:
            self._login = None
            self._status(STEP_SPOTIFY, "error", e.code, **e.params)
            return {"ok": False, "code": e.code, "params": e.params}
        return {"ok": True}

    def spotify_cancel_login(self) -> Dict[str, Any]:
        if self._login:
            self._login.cancel()
        return {"ok": True}

    def _on_login_done(self, payload, error: Optional[SpotifyAuthError]):
        self._login = None
        if error:
            self._status(STEP_SPOTIFY, "error", error.code, **error.params)
            self._emit_spotify()
            return
        self.tokens.apply(payload)
        self._log("info", "log.spotify_connected")
        self._emit_spotify()
        self.start_polling()

    def spotify_logout(self) -> Dict[str, Any]:
        self.tokens.clear()
        self._now_playing = None
        self.player.last_state = None
        self._log("info", "log.spotify_disconnected")
        self._emit_spotify()
        return {"ok": True}

    def _emit_spotify(self, error_code: str = "", params: Optional[Dict[str, Any]] = None):
        signed_in = self.tokens.signed_in
        if not signed_in:
            self._status(STEP_SPOTIFY, "idle" if not error_code else "error",
                         error_code or "status.spotify_signed_out", **(params or {}))
        elif error_code:
            self._status(STEP_SPOTIFY, "error", error_code, **(params or {}))
        else:
            self._status(STEP_SPOTIFY, "ok", "status.spotify_ready")
        self.emit("spotify", {
            "signed_in": signed_in,
            "client_id_set": bool(settings.get("spotify_client_id", "").strip()),
            "now_playing": self._now_playing if signed_in else None,
            "devices": self._devices if signed_in else [],
            "preferred_device": self.player.preferred_device,
            "error": error_code,
            "error_params": params or {},
        })

    def start_polling(self):
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_wake.set()
            return
        self._poll_thread = threading.Thread(target=self._poll, name="spotify-poll", daemon=True)
        self._poll_thread.start()

    def _poll(self):
        polls = 0
        while not self._closing and self.tokens.signed_in:
            try:
                if polls % DEVICE_EVERY_POLLS == 0:
                    self._devices = self._spotify.submit(self.player.devices).result(timeout=20)
                polls += 1
                self._now_playing = self._spotify.submit(self.player.state).result(timeout=20)
                self._emit_spotify()
            except SpotifyError as e:
                self._now_playing = None
                self._emit_spotify(e.code, e.params)
            except Exception as e:
                # The executor shuts down with the window; anything else is
                # worth a log line but not worth killing the poller over.
                if self._closing:
                    return
                self._log("warn", "err.spotify_api", detail=str(e))
            self._poll_wake.wait(POLL_SECONDS)
            self._poll_wake.clear()
        if not self._closing:
            self._emit_spotify()

    def spotify_refresh(self):
        self._poll_wake.set()
        return {"ok": True}

    # ---------------------------------------------------------------- devices
    def spotify_devices(self) -> Dict[str, Any]:
        """Re-read the device list now — the user just opened the picker."""
        try:
            self._devices = self._spotify.submit(self.player.devices).result(timeout=20)
        except SpotifyError as e:
            self._log("error", e.code, **e.params)
            return {"ok": False, "code": e.code, "params": e.params}
        except Exception as e:
            return {"ok": False, "code": "err.spotify_api", "params": {"detail": str(e)}}
        self._emit_spotify()
        return {"ok": True, "devices": self._devices}

    def spotify_play_on(self, device_id: str) -> Dict[str, Any]:
        """Move playback to a device, and remember it for next time.

        The preference is saved before the transfer is tried: even if Spotify
        refuses right now (the device went to sleep, say), the user's choice is
        what a later "play" thought should aim for.
        """
        device = next((d for d in self._devices if d["id"] == device_id), None)
        if not device:
            return {"ok": False, "code": "err.spotify_device_gone", "params": {}}
        if device["is_restricted"]:
            return {"ok": False, "code": "err.spotify_device_restricted",
                    "params": {"device": device["name"]}}

        settings.set("spotify_device_id", device_id)
        settings.save()
        self.player.preferred_device = device_id

        # Moving music that is playing or paused keeps it that way. With nothing
        # playing at all there is nothing to keep, and a transfer that keeps
        # "nothing" does nothing — so picking a device then means "play here".
        start = True if not self._now_playing else None

        def run():
            self.player.transfer(device_id, play=start)
            # Spotify applies the transfer on the device, not in the response;
            # read back after a moment, as after any other command.
            time.sleep(0.6)
            return self.player.devices()

        try:
            self._devices = self._spotify.submit(run).result(timeout=20)
        except SpotifyError as e:
            self._log("error", e.code, **e.params)
            self._emit_spotify()
            return {"ok": False, "code": e.code, "params": e.params}
        except Exception as e:
            return {"ok": False, "code": "err.spotify_api", "params": {"detail": str(e)}}

        self._log("info", "log.spotify_play_on", device=device["name"])
        self._poll_wake.set()
        self._emit_spotify()
        return {"ok": True}
