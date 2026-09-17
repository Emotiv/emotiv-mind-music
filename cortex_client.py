"""EMOTIV Cortex API client: headsets, profiles, training and mental commands.

Carried over from emotiv-brain-light, where every quirk in here was found the
hard way (see CLAUDE.md). That app also drives Performance Metrics; this one
only needs mental commands, so the metrics path is gone and nothing else is.

Nothing here returns user-facing text: events carry a *code* plus parameters,
and the UI renders them in the selected language. That is what makes English
and Chinese possible without scattering strings through the backend.
"""
import asyncio
import json
import os
import ssl
from typing import Any, Callable, Dict, List, Optional

import websockets

from config import Config
from settings import settings

# Every mental command Cortex knows, in the order `getDetectionInfo` returns
# them. Queried at runtime; this is the fallback for when that call fails.
MENTAL_COMMAND_ACTIONS = [
    "neutral", "push", "pull", "lift", "drop", "left", "right",
    "rotateLeft", "rotateRight", "rotateClockwise", "rotateCounterClockwise",
    "rotateForwards", "rotateReverse", "disappear",
]

# Cortex accepts at most four commands besides neutral in the active set —
# the same four slots EmotivBCI draws.
MAX_ACTIVE_ACTIONS = 4

# What goes on the wire. `dev` and `eq` ride along: sensor quality is the first
# thing to check when a command misfires, and it costs two samples a second.
COMMAND_STREAMS = ["com", "sys", "dev", "eq"]

# Losing these costs the quality panel, not the session.
OPTIONAL_STREAMS = {"dev", "eq"}

# Contact quality is graded 0-4 per sensor, but the last entry of the `dev`
# array is an overall *percentage* under the name OVERALL. Verified live on an
# Insight 2: cols ["Battery","Signal",["AF3",...,"OVERALL"],"BatteryPercent"],
# sample [3, 1.0, [4,4,4,4,4,100], 80].
CQ_OVERALL_KEY = "OVERALL"

# Steps the UI draws as a status pipeline.
STEP_CREDENTIALS = "credentials"
STEP_CORTEX = "cortex"
STEP_ACCESS = "access"
STEP_HEADSET = "headset"
STEP_SESSION = "session"
STEP_PROFILE = "profile"
STEP_STREAM = "stream"


# Errors reconnecting cannot fix — only the user can. Retrying every 5s would
# just stack the same message in the log.
FATAL_CODES = {
    "err.no_credentials",
    "err.bad_credentials",
    "err.no_profile_selected",
    "err.profile_not_found",
}


class CortexError(Exception):
    """Error carrying a code the UI can translate."""

    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = params


class CortexClient:
    def __init__(self, emit: Callable[[str, Dict[str, Any]], None]):
        self.emit = emit
        self.url = Config.CORTEX_URL

        self.ws = None
        self.token: Optional[str] = None
        self.session_id: Optional[str] = None
        self.headset_id: Optional[str] = None
        self.loaded_profile: Optional[str] = None

        # Column names per subscribed stream, as Cortex described them.
        self.cols: Dict[str, List[Any]] = {}
        self.active_actions: List[str] = []
        # action -> how many accepted trainings sit in the signature.
        self.trained_actions: Dict[str, int] = {}
        self.available_actions: List[str] = list(MENTAL_COMMAND_ACTIONS)
        self.training_action: Optional[str] = None
        self.quality: Dict[str, Any] = {}
        self.sensitivity: List[int] = []
        self.headsets: List[Dict[str, Any]] = []
        self.profiles: List[str] = []

        self.desired_profile: str = ""
        self.client_id = ""
        self.client_secret = ""

        self._next_req_id = 1
        self._pending: Dict[int, asyncio.Future] = {}
        self._device_connected: Optional[asyncio.Future] = None
        self._running = False
        self._restart = asyncio.Event()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        # Data callbacks, wired up by the engine.
        self.on_command: Optional[Callable[[str, float], None]] = None
        self.on_training_event: Optional[Callable[[str, str], None]] = None

    # ---------------------------------------------------------------- helpers
    def _status(self, step: str, state: str, code: str = "", **params):
        self.emit("status", {"step": step, "state": state, "code": code, "params": params})

    def _log(self, level: str, code: str, **params):
        self.emit("log", {"level": level, "code": code, "params": params})

    # ------------------------------------------------------------------- loop
    async def run(self, client_id: str, client_secret: str):
        self.loop = asyncio.get_running_loop()
        self.client_id = client_id
        self.client_secret = client_secret
        self._running = True

        while self._running:
            try:
                await self._session_cycle()
            except CortexError as e:
                self._log("error", e.code, **e.params)
                if e.code in FATAL_CODES:
                    self.emit("fatal", {"code": e.code, "params": e.params})
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log("error", "err.unexpected", detail=str(e))

            if not self._running:
                break

            self._clear_pending(Exception("disconnected"))
            self.ws = None
            self.session_id = None
            self.token = None
            self._status(STEP_CORTEX, "error", "status.reconnecting")
            try:
                await asyncio.wait_for(self._restart.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            self._restart.clear()

    def _reset_steps(self):
        """Clear the pipeline before each attempt.

        Without this, a reconnect leaves the UI showing 'App access: pending'
        next to 'Session: ok' — leftovers from the previous cycle that make the
        user believe in a state that no longer exists.
        """
        for step in (STEP_CORTEX, STEP_ACCESS, STEP_HEADSET, STEP_SESSION, STEP_PROFILE, STEP_STREAM):
            self._status(step, "idle")

    async def _session_cycle(self):
        self._reset_steps()

        if not self.client_id or not self.client_secret:
            self._status(STEP_CREDENTIALS, "error", "err.no_credentials")
            raise CortexError("err.no_credentials")
        self._status(STEP_CREDENTIALS, "ok")

        self._status(STEP_CORTEX, "pending", "status.connecting")
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if os.path.exists(Config.CORTEX_CERT_PATH):
            ssl_ctx.load_verify_locations(cafile=Config.CORTEX_CERT_PATH)
        else:
            ssl_ctx.verify_mode = ssl.CERT_NONE
        ssl_ctx.check_hostname = False

        try:
            ws_ctx = websockets.connect(self.url, ssl=ssl_ctx, open_timeout=10)
        except Exception as e:
            raise CortexError("err.cortex_unreachable", detail=str(e))

        try:
            async with ws_ctx as ws:
                self.ws = ws
                self._status(STEP_CORTEX, "ok")

                reader = asyncio.create_task(self._read_loop(ws))
                try:
                    await self._request_access()
                    await self._authorize()
                    await self.load_available_actions()
                    await self._load_profiles()
                    await self.refresh_headsets(force=False)

                    # Convenience: if the user picked a headset before and it
                    # is still listed, reconnect to it automatically. Every
                    # other case waits for a choice on screen.
                    remembered = settings.get("headset_id")
                    if remembered and any(h.get("id") == remembered for h in self.headsets):
                        await self.select_headset(remembered)
                    else:
                        self._status(STEP_HEADSET, "idle", "status.pick_headset")

                    # The reader keeps the cycle alive while the user decides.
                    await reader
                finally:
                    reader.cancel()
        except (OSError, websockets.exceptions.WebSocketException) as e:
            raise CortexError("err.cortex_unreachable", detail=str(e))

    async def _read_loop(self, ws):
        async for message in ws:
            self._handle_message(message)

    def stop(self):
        self._running = False
        if self.ws and self.loop:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)

    # --------------------------------------------------------------- JSON-RPC
    async def _send(self, method: str, params: Dict[str, Any] = None, timeout: float = 30.0) -> Any:
        if not self.ws:
            raise CortexError("err.not_connected")

        req_id = self._next_req_id
        self._next_req_id += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": req_id}

        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        await self.ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise CortexError("err.timeout", method=method)

    def _clear_pending(self, error: Exception):
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def _handle_message(self, message: str):
        try:
            data = json.loads(message)

            if "id" in data:
                future = self._pending.pop(data["id"], None)
                if future and not future.done():
                    if "error" in data:
                        err = data["error"]
                        # `api_code`, not `code`: `code` is CortexError's first
                        # positional parameter and would collide with the kwarg.
                        future.set_exception(
                            CortexError(
                                "err.cortex_api",
                                api_code=err.get("code"),
                                detail=err.get("message", ""),
                            )
                        )
                    else:
                        future.set_result(data.get("result", {}))
                return

            if "warning" in data:
                self._handle_warning(data["warning"])
                return

            if "com" in data:
                self._handle_com(data["com"])
            elif "dev" in data:
                self._handle_dev(data["dev"])
            elif "eq" in data:
                self._handle_eq(data["eq"])
            elif "sys" in data:
                self._handle_sys(data["sys"])

        except Exception as e:
            self._log("warn", "err.parse", detail=str(e))

    def _handle_warning(self, warning: Dict[str, Any]):
        code = warning.get("code")
        message = str(warning.get("message", ""))

        if code == 104 and self._device_connected and not self._device_connected.done():
            self._device_connected.set_result(True)
            return
        if code == 142:
            return
        if code == 11:
            # "The APIs cannot be registered. Please re-open the connection."
            # The connection went zombie: it accepts bytes but never answers.
            # Only reopening helps — insisting on it times out requestAccess.
            self._log("warn", "err.stale_connection")
            self._clear_pending(CortexError("err.stale_connection"))
            if self.ws:
                asyncio.create_task(self.ws.close())
            return
        if code == 1:  # headset disconnected
            self._status(STEP_HEADSET, "error", "err.headset_disconnected")
            self._log("warn", "err.headset_disconnected")
            return
        self._log("info", "log.cortex_warning", detail=message, warning_code=code)

    # -------------------------------------------------------------- handshake
    async def _request_access(self):
        self._status(STEP_ACCESS, "pending", "status.requesting_access")
        res = await self._send(
            "requestAccess", {"clientId": self.client_id, "clientSecret": self.client_secret}
        )
        if not res.get("accessGranted", False):
            self._status(STEP_ACCESS, "error", "err.access_pending")
            raise CortexError("err.access_pending", detail=res.get("message", ""))
        self._status(STEP_ACCESS, "ok")

    async def _authorize(self):
        try:
            res = await self._send(
                "authorize",
                {"clientId": self.client_id, "clientSecret": self.client_secret, "debit": 1},
            )
        except CortexError as e:
            self._status(STEP_ACCESS, "error", "err.bad_credentials", **e.params)
            raise CortexError("err.bad_credentials", **e.params)

        self.token = res.get("cortexToken")
        if not self.token:
            self._status(STEP_ACCESS, "error", "err.no_token")
            raise CortexError("err.no_token")

    async def refresh_headsets(self, force: bool = True):
        """List headsets. `force` also triggers the Bluetooth scan."""
        self._status(STEP_HEADSET, "pending", "status.searching_headsets")
        if force:
            await self._send("controlDevice", {"command": "refresh"})
            await asyncio.sleep(3)

        headsets = await self._send("queryHeadsets")
        self.headsets = headsets if isinstance(headsets, list) else []
        self._emit_headsets()

        if not self.headsets:
            self._status(STEP_HEADSET, "error", "err.no_headset")
        elif force:
            self._status(STEP_HEADSET, "idle", "status.pick_headset")
        return [h.get("id") for h in self.headsets]

    def _emit_headsets(self):
        self.emit("headsets", {
            "items": [
                {
                    "id": h.get("id"),
                    "status": h.get("status"),
                    "connectedBy": h.get("connectedBy"),
                    "virtual": bool(h.get("virtualHeadsetId")),
                }
                for h in self.headsets
            ],
            "selected": self.headset_id or "",
        })

    async def select_headset(self, headset_id: str):
        """Connect the headset the user picked and open the session."""
        target = next((h for h in self.headsets if h.get("id") == headset_id), None)
        if not target:
            self._status(STEP_HEADSET, "error", "err.headset_not_found", headset=headset_id)
            raise CortexError("err.headset_not_found", headset=headset_id)

        self._status(STEP_HEADSET, "pending", "status.connecting_headset", headset=headset_id)
        self.headset_id = headset_id
        settings.set("headset_id", headset_id)
        settings.save()

        if target.get("status") != "connected":
            self._device_connected = asyncio.get_running_loop().create_future()
            await self._send("controlDevice", {"command": "connect", "headset": headset_id})
            try:
                await asyncio.wait_for(self._device_connected, timeout=30.0)
            except asyncio.TimeoutError:
                self._log("warn", "err.headset_connect_timeout", headset=headset_id)
            finally:
                self._device_connected = None

        self._status(STEP_HEADSET, "ok", "status.headset_ready", headset=headset_id)
        self._emit_headsets()

        await self._create_or_reuse_session()
        await self._after_session()

    async def _after_session(self):
        """After the session, a profile: nothing is detected without one."""
        remembered = self.desired_profile or settings.get("profile")
        if remembered and remembered in self.profiles:
            try:
                await self.select_profile(remembered)
            except CortexError:
                # The "Profile" step already shows the exact reason. Letting
                # the exception bubble would only stack a generic failure on
                # top of it, burying the real cause.
                pass
        else:
            self._status(STEP_PROFILE, "idle", "status.pick_profile")

    async def _create_or_reuse_session(self):
        self._status(STEP_SESSION, "pending")
        sessions = await self._send("querySessions", {"cortexToken": self.token})
        mine = (
            [s for s in sessions if s.get("headsetId") == self.headset_id]
            if isinstance(sessions, list)
            else []
        )

        active = next((s for s in mine if s.get("status") in ("active", "opened")), None)
        if active:
            self.session_id = active["id"]
            self._status(STEP_SESSION, "ok", "status.session_reused")
            return

        for s in mine:
            try:
                await self._send(
                    "updateSession",
                    {"cortexToken": self.token, "session": s["id"], "status": "close"},
                )
            except Exception:
                pass

        try:
            res = await self._send(
                "createSession",
                {"cortexToken": self.token, "headset": self.headset_id, "status": "active"},
            )
            self.session_id = res.get("id")
        except CortexError as e:
            # Defensive recovery from -32005 ("session already exists"). Cortex
            # takes a moment to retire a session it just closed, so a single
            # re-query can come back empty and leave the user clicking the
            # headset twice. Give it a couple of tries before giving up.
            if str(e.params.get("api_code")) == "-32005" or "exist" in str(e.params.get("detail", "")).lower():
                for attempt in range(3):
                    await asyncio.sleep(1.0 * attempt)
                    sessions = await self._send("querySessions", {"cortexToken": self.token})
                    mine = [s for s in sessions if s.get("headsetId") == self.headset_id] if isinstance(sessions, list) else []
                    if mine:
                        self.session_id = mine[0]["id"]
                        self._status(STEP_SESSION, "ok", "status.session_recovered")
                        return
                    try:
                        res = await self._send(
                            "createSession",
                            {"cortexToken": self.token, "headset": self.headset_id, "status": "active"},
                        )
                        self.session_id = res.get("id")
                        self._status(STEP_SESSION, "ok", "status.session_created")
                        return
                    except CortexError:
                        continue
            self._status(STEP_SESSION, "error", e.code, **e.params)
            raise

        self._status(STEP_SESSION, "ok", "status.session_created")

    # -------------------------------------------------------------- profiles
    async def _load_profiles(self):
        try:
            res = await self._send("queryProfile", {"cortexToken": self.token})
            self.profiles = [p.get("name") for p in res if p.get("name")] if isinstance(res, list) else []
        except CortexError as e:
            self.profiles = []
            self._log("warn", "err.profiles_failed", **e.params)
        self.emit("profiles", {"items": self.profiles})

    async def select_profile(self, name: str):
        """Load the chosen profile and read the action order — that order is
        what determines each slot colour, exactly like EmotivBCI does, and so
        which colour each command's row carries on the controls card."""
        if not name:
            self._status(STEP_PROFILE, "error", "err.no_profile_selected")
            raise CortexError("err.no_profile_selected")
        if name not in self.profiles:
            self._status(STEP_PROFILE, "error", "err.profile_not_found", profile=name)
            raise CortexError("err.profile_not_found", profile=name)
        if not self.headset_id or not self.session_id:
            self._status(STEP_PROFILE, "error", "err.no_headset_selected")
            raise CortexError("err.no_headset_selected")

        self._status(STEP_PROFILE, "pending", "status.loading_profile", profile=name)
        self.desired_profile = name
        settings.set("profile", name)
        settings.save()

        # Cortex refuses to load a profile while another one sits on the
        # headset ("A profile is already loaded for this headset"). Without
        # this, switching profiles would only work once per session.
        await self._unload_current_profile()

        try:
            await self._load_profile_once(name)
        except CortexError as e:
            # -32127: "a profile is already loaded". Happens when another app
            # or session loaded it — getCurrentProfile then returns name=None
            # and the pre-emptive unload finds nothing to drop. Unload by target
            # name and retry once.
            if str(e.params.get("api_code")) == "-32127":
                await self._force_unload(name)
                try:
                    await self._load_profile_once(name)
                except CortexError as retry_error:
                    self._fail_profile(name, retry_error)
            else:
                self._fail_profile(name, e)

        self.loaded_profile = name

        actions = await self._active_actions(name)
        self.active_actions = list(actions)
        self.emit("actions", {"items": actions, "profile": name})

        if not actions or actions == ["neutral"]:
            # Not a failure any more. This is exactly the state a profile the
            # user just created is in, and the way out is the training panel,
            # not a different profile — so load it, say what is missing, and
            # leave the session up for training to use.
            self._status(STEP_PROFILE, "ok", "status.profile_untrained", profile=name)
            self._log("warn", "log.profile_untrained", profile=name)
        else:
            self._status(STEP_PROFILE, "ok", "status.profile_loaded", profile=name)

        await self._emit_sensitivity()
        await self.refresh_commands()
        await self._subscribe(COMMAND_STREAMS)
        return actions

    def _fail_profile(self, name: str, error: CortexError):
        """Turn a load failure into a coded error and stop."""
        # -32226: profile trained on a different headset model. It deserves its
        # own message, because the way out is another profile, not a retry.
        if str(error.params.get("api_code")) == "-32226":
            self._status(STEP_PROFILE, "error", "err.profile_incompatible",
                         profile=name, headset=self.headset_id)
            raise CortexError("err.profile_incompatible",
                              profile=name, headset=self.headset_id)
        self._status(STEP_PROFILE, "error", "err.profile_load_failed", profile=name,
                     detail=error.params.get("detail", ""))
        raise error

    async def _load_profile_once(self, name: str):
        await self._send(
            "setupProfile",
            {
                "cortexToken": self.token,
                "headset": self.headset_id,
                "profile": name,
                "status": "load",
            },
            timeout=45.0,
        )

    async def _force_unload(self, name: str):
        """Unload by name, ignoring the error when nothing was loaded."""
        try:
            await self._send(
                "setupProfile",
                {
                    "cortexToken": self.token,
                    "headset": self.headset_id,
                    "profile": name,
                    "status": "unload",
                },
                timeout=30.0,
            )
        except CortexError:
            pass

    async def _unload_current_profile(self):
        """Unload whatever sits on the headset, no matter which app loaded it."""
        try:
            current = await self._send(
                "getCurrentProfile", {"cortexToken": self.token, "headset": self.headset_id}
            )
        except CortexError:
            return

        name = (current or {}).get("name")
        if not name:
            return

        try:
            await self._send(
                "setupProfile",
                {
                    "cortexToken": self.token,
                    "headset": self.headset_id,
                    "profile": name,
                    "status": "unload",
                },
                timeout=30.0,
            )
            self.loaded_profile = None
        except CortexError as e:
            self._log("warn", "err.profile_unload_failed", profile=name,
                      detail=e.params.get("detail", ""))

    # ------------------------------------------------------ command roster
    # EmotivBCI shows one row per command: its name, how many accepted
    # trainings sit behind it, and whether it is switched on. Three calls
    # supply that. `mentalCommandActiveAction` is the enabled set and the
    # order the slot colours follow; `getTrainedSignatureActions` is the
    # training count; `getDetectionInfo` is everything that could be added.

    async def load_available_actions(self):
        """Ask Cortex which commands exist, once per connection."""
        try:
            info = await self._send("getDetectionInfo", {"detection": "mentalCommand"})
            actions = info.get("actions") if isinstance(info, dict) else None
            if actions:
                self.available_actions = [a for a in actions if isinstance(a, str)]
        except CortexError as e:
            self._log("warn", "err.detection_info_failed", **e.params)

    async def get_trained_actions(self) -> Dict[str, int]:
        """action -> accepted trainings currently in the signature."""
        if not self.loaded_profile:
            return {}
        try:
            res = await self._send(
                "getTrainedSignatureActions",
                {"cortexToken": self.token, "detection": "mentalCommand", **self._scope()},
            )
        except CortexError as e:
            self._log("warn", "err.trained_actions_failed", **e.params)
            return {}
        entries = (res or {}).get("trainedActions", []) if isinstance(res, dict) else []
        return {
            e.get("action"): int(e.get("times", 0))
            for e in entries
            if isinstance(e, dict) and e.get("action")
        }

    async def refresh_commands(self):
        """Rebuild and publish the command roster."""
        if not self.loaded_profile:
            return
        self.active_actions = await self._active_actions(self.loaded_profile)
        self.trained_actions = await self.get_trained_actions()

        # A command that was trained and then switched off still belongs on
        # screen — with its training intact and its toggle down. Dropping it
        # would look like the training was lost.
        enabled = [a for a in self.active_actions if a != "neutral"]
        disabled = [a for a in self.trained_actions if a != "neutral" and a not in enabled]

        self.emit("commands", {
            "profile": self.loaded_profile,
            "enabled": enabled,
            "disabled": disabled,
            "trained": self.trained_actions,
            "available": [a for a in self.available_actions if a != "neutral"],
            "max_active": MAX_ACTIVE_ACTIONS,
        })
        # The slot colours follow the enabled order, so the rest of the app
        # has to hear about a reorder too.
        self.emit("actions", {"items": self.active_actions, "profile": self.loaded_profile})

    async def set_active_actions(self, actions: List[str]) -> List[str]:
        """Switch commands on or off, and persist it into the profile.

        The list Cortex is *set* with excludes neutral (a `get` puts it back at
        the front). Confirmed in EmotivBCI's own log: removing the only trained
        command sets `actions: []`, and the following `get` returns
        `["neutral"]`.
        """
        self._require_session()
        wanted = [a for a in actions if a != "neutral"][:MAX_ACTIVE_ACTIONS]
        await self._send(
            "mentalCommandActiveAction",
            {"cortexToken": self.token, "status": "set",
             "session": self.session_id, "actions": wanted},
        )
        await self.save_profile()
        await self.refresh_commands()
        return wanted

    # ------------------------------------------------------------- profiles
    async def create_profile(self, name: str) -> str:
        """Make a new, empty profile and load it, ready to be trained."""
        name = (name or "").strip()
        if not name:
            raise CortexError("err.profile_name_empty")
        if name in self.profiles:
            raise CortexError("err.profile_exists", profile=name)
        if not self.headset_id or not self.session_id:
            raise CortexError("err.no_headset_selected")

        self._status(STEP_PROFILE, "pending", "status.creating_profile", profile=name)
        # Cortex builds the new profile out of whatever detection state is
        # loaded on the headset, so creating one while another profile sits
        # there hands the "empty" profile that profile's training. Unload
        # first and the new profile really does start at zero — verified: it
        # comes back with active ["neutral"] and no trained actions.
        await self._unload_current_profile()
        # `headset` is required here, whatever the docs say: creating without
        # it answers -32602 Invalid Parameters on Cortex 4.8. Deleting, by
        # contrast, does not want it.
        await self._send(
            "setupProfile",
            {"cortexToken": self.token, "headset": self.headset_id,
             "profile": name, "status": "create"},
            timeout=45.0,
        )
        await self._load_profiles()
        await self.select_profile(name)
        return name

    async def save_profile(self):
        """Persist the loaded profile. Training is only kept once this runs."""
        if not self.loaded_profile or not self.headset_id:
            return
        try:
            await self._send(
                "setupProfile",
                {"cortexToken": self.token, "headset": self.headset_id,
                 "profile": self.loaded_profile, "status": "save"},
                timeout=45.0,
            )
        except CortexError as e:
            self._log("error", "err.profile_save_failed",
                      profile=self.loaded_profile, **e.params)
            raise

    # ------------------------------------------------------------- training
    # The whole cycle, as EmotivBCI runs it and as the logs confirm:
    #
    #   training status=start   -> sys MC_Started -> eight seconds of EEG
    #                           -> sys MC_Succeeded (or MC_Failed)
    #   training status=accept  -> sys MC_Completed -> setupProfile save
    #   training status=reject  -> sys MC_Rejected, nothing changes
    #
    # Nothing is written to the profile until accept *and* save. A rejected or
    # failed recording leaves the signature exactly as it was.

    def _scope(self) -> Dict[str, Any]:
        """How to address the loaded detection state.

        Every read-back below accepts either a session or a profile name, and
        after a training completes they agree — but only the session is
        guaranteed to be the state Cortex is actually running. Prefer it, and
        fall back to the name when there is no session to point at.
        """
        if self.session_id:
            return {"session": self.session_id}
        return {"profile": self.loaded_profile}

    def _require_session(self):
        if not self.session_id or not self.token:
            raise CortexError("err.no_headset_selected")
        if not self.loaded_profile:
            raise CortexError("err.no_profile_selected")

    async def _training(self, status: str, action: str = "") -> Dict[str, Any]:
        self._require_session()
        params = {
            "cortexToken": self.token,
            "session": self.session_id,
            "detection": "mentalCommand",
            "status": status,
        }
        if action:
            params["action"] = action
        return await self._send("training", params, timeout=45.0)

    async def start_training(self, action: str) -> Dict[str, Any]:
        if action not in self.available_actions:
            raise CortexError("err.unknown_action", action=action)

        # Cortex will not record a command that is not in the active set, so
        # switching it on is part of starting, not a separate step the user
        # has to remember.
        if action != "neutral" and action not in self.active_actions:
            enabled = [a for a in self.active_actions if a != "neutral"]
            if len(enabled) >= MAX_ACTIVE_ACTIONS:
                raise CortexError("err.too_many_actions", max=MAX_ACTIVE_ACTIONS)
            await self.set_active_actions(enabled + [action])

        self.training_action = action
        try:
            return await self._training("start", action)
        except CortexError:
            self.training_action = None
            raise

    async def accept_training(self) -> Dict[str, Any]:
        """Keep the recording.

        Saving and re-reading deliberately do *not* happen here. Cortex
        rebuilds the signature after this call returns and announces it with
        MC_Completed; anything read before that event reports the state from
        before the recording — which is how the training counts ended up
        permanently one step behind. `_after_accept` picks it up there.
        """
        return await self._training("accept", self.training_action or "")

    async def reject_training(self) -> Dict[str, Any]:
        action = self.training_action or ""
        res = await self._training("reject", action)
        self.training_action = None
        return res

    async def erase_training(self, action: str) -> Dict[str, Any]:
        """Throw away everything recorded for one command.

        EmotivBCI does the same on its bin icon: erase, drop the command from
        the active set, save. Erasing without the second step would leave a
        command listed, enabled and holding nothing.
        """
        res = await self._training("erase", action)
        if action != "neutral":
            await self.set_active_actions(
                [a for a in self.active_actions if a not in ("neutral", action)]
            )
        else:
            await self.save_profile()
            await self.refresh_commands()
        await self.emit_training_result()
        return res

    async def reset_training(self) -> Dict[str, Any]:
        """Wipe the whole signature — every command, back to an empty profile.

        Cortex has no whole-signature reset. `training status=reset` is
        per-action and refuses without one ("This parameter is required:
        action"), so the way to empty a profile is to erase each command that
        holds anything and then save once.
        """
        self.training_action = None
        trained = await self.get_trained_actions()
        for action in list(trained):
            try:
                await self._training("erase", action)
            except CortexError as e:
                self._log("warn", "err.training_failed", **e.params)
        # Emptying the active set saves the profile and republishes the roster.
        await self.set_active_actions([])
        await self.emit_training_result()
        return {"status": "reset", "actions": list(trained)}

    async def _after_accept(self):
        """The signature is rebuilt: persist it and re-read what it now holds."""
        self.training_action = None
        try:
            await self.save_profile()
        except CortexError:
            return  # save_profile already logged and told the UI
        await self._resync()

    async def _resync(self):
        """Re-read the roster and the result after Cortex rebuilds a signature."""
        try:
            await self.refresh_commands()
            await self.emit_training_result()
        except CortexError as e:
            self._log("warn", "err.commands_failed", **e.params)

    async def _emit_training_score(self, action: str):
        """Score of the recording that just finished, before it is kept."""
        try:
            res = await self._send(
                "mentalCommandTrainingThreshold",
                {"cortexToken": self.token, "session": self.session_id},
            )
        except CortexError as e:
            self._log("warn", "err.threshold_failed", **e.params)
            return
        if not isinstance(res, dict):
            return
        self.emit("training_score", {
            "action": action,
            "score": res.get("lastTrainingScore"),
            "threshold": res.get("currentThreshold"),
        })

    # --------------------------------------------------------- training result
    async def emit_training_result(self):
        """Publish what the profile now knows: brain map, threshold, skill.

        `mentalCommandBrainMap` gives one point per command — neutral pinned at
        the origin, every other command at its distance from it. That distance
        is the whole story: a command sitting on top of neutral is one the
        detector cannot tell apart from doing nothing.
        """
        if not self.loaded_profile:
            return
        result: Dict[str, Any] = {"profile": self.loaded_profile}

        try:
            brain_map = await self._send(
                "mentalCommandBrainMap",
                {"cortexToken": self.token, **self._scope()},
            )
            result["brain_map"] = [
                {"action": e.get("action"), "coordinates": e.get("coordinates")}
                for e in brain_map or []
                if isinstance(e, dict) and isinstance(e.get("coordinates"), list)
            ]
        except CortexError as e:
            self._log("warn", "err.brain_map_failed", **e.params)
            result["brain_map"] = []

        try:
            threshold = await self._send(
                "mentalCommandTrainingThreshold",
                {"cortexToken": self.token, **self._scope()},
            )
            if isinstance(threshold, dict):
                result["threshold"] = threshold.get("currentThreshold")
                result["last_score"] = threshold.get("lastTrainingScore")
        except CortexError as e:
            self._log("warn", "err.threshold_failed", **e.params)

        try:
            skill = await self._send(
                "mentalCommandGetSkillRating",
                {"cortexToken": self.token, **self._scope()},
            )
            if isinstance(skill, (int, float)):
                result["skill"] = float(skill)
        except CortexError as e:
            self._log("warn", "err.skill_failed", **e.params)

        result["trained"] = self.trained_actions
        self.emit("training_result", result)

    # -------------------------------------------------------- sensitivity
    # Cortex keeps one sensitivity per trainable action, 1 (least sensitive) to
    # 10. The array is always four long and lines up with the active actions in
    # order, ignoring `neutral` — verified on a profile with two trained
    # actions, which read back [1, 1, 5, 5].
    #
    # `get` accepts either a profile or a session, but `set` only works with a
    # session that has the profile loaded; passing a profile name returns
    # -32007 "the session does not exist".

    def trainable_actions(self) -> List[str]:
        """Active actions minus neutral — the ones a slider maps onto."""
        return [a for a in self.active_actions if a != "neutral"]

    async def get_sensitivity(self) -> List[int]:
        if not self.session_id:
            raise CortexError("err.no_headset_selected")
        res = await self._send(
            "mentalCommandActionSensitivity",
            {"cortexToken": self.token, "status": "get", "session": self.session_id},
        )
        return [int(v) for v in res] if isinstance(res, list) else []

    async def set_sensitivity(self, values: List[int]) -> List[int]:
        if not self.session_id:
            raise CortexError("err.no_headset_selected")

        # Cortex insists on exactly four values, so pad from what is already
        # stored rather than inventing defaults for slots we do not show.
        try:
            current = await self.get_sensitivity()
        except CortexError:
            current = []
        merged = list(current) + [5] * (4 - len(current))
        for i, v in enumerate(values[:4]):
            merged[i] = max(1, min(10, int(v)))

        await self._send(
            "mentalCommandActionSensitivity",
            {"cortexToken": self.token, "status": "set", "session": self.session_id,
             "values": merged[:4]},
        )
        self.sensitivity = merged[:4]
        self.emit("sensitivity", {"values": self.sensitivity,
                                  "actions": self.trainable_actions()})
        return self.sensitivity

    async def _emit_sensitivity(self) -> None:
        try:
            self.sensitivity = await self.get_sensitivity()
        except CortexError as e:
            self._log("warn", "err.sensitivity_failed", **e.params)
            return
        self.emit("sensitivity", {"values": self.sensitivity,
                                  "actions": self.trainable_actions()})

    async def _active_actions(self, profile: str) -> List[str]:
        """Trained action order — this is what defines each slot colour."""
        try:
            res = await self._send(
                "mentalCommandActiveAction",
                {"cortexToken": self.token, "status": "get", "profile": profile},
            )
            if isinstance(res, list):
                return [a for a in res if isinstance(a, str)]
        except CortexError as e:
            self._log("warn", "err.actions_failed", **e.params)
        return []

    # ------------------------------------------------------------- streams
    async def _subscribe(self, streams: List[str]):
        self._status(STEP_STREAM, "pending")
        res = await self._send(
            "subscribe",
            {"cortexToken": self.token, "session": self.session_id, "streams": streams},
        )

        for ok in res.get("success", []):
            self.cols[ok.get("streamName")] = ok.get("cols", [])

        # `dev` and `eq` are a convenience — they drive the sensor-quality panel.
        # A headset or firmware that does not offer them must not take the whole
        # session down with it, which is what refusing every failure would do.
        for fail in res.get("failure", []):
            stream = fail.get("streamName")
            if stream in OPTIONAL_STREAMS:
                self._log("warn", "err.quality_unavailable",
                          stream=stream, detail=fail.get("message", ""))
                continue
            self._status(STEP_STREAM, "error", "err.subscribe_failed",
                         stream=stream, detail=fail.get("message", ""))
            raise CortexError("err.subscribe_failed",
                              stream=stream, detail=fail.get("message", ""))

        self._status(STEP_STREAM, "ok", "status.streaming")

    async def _unsubscribe_all(self):
        streams = list(self.cols)
        if not streams or not self.session_id:
            return
        try:
            await self._send(
                "unsubscribe",
                {"cortexToken": self.token, "session": self.session_id, "streams": streams},
            )
        except Exception:
            pass
        self.cols.clear()

    # ------------------------------------------------------------- quality
    # Two streams describe the sensors, and they answer different questions.
    # `dev` is contact quality: is the electrode touching skin well enough to
    # read anything. `eq` is EEG quality: is what arrives actually brain
    # signal rather than muscle, movement or mains hum. A headset can sit at a
    # perfect contact score and still deliver unusable EEG, which is why the
    # training screen shows both before it lets anyone record.

    def _handle_dev(self, raw: List[Any]):
        cols = self.cols.get("dev") or []
        # cols look like ["Battery", "Signal", [<sensor names>, "OVERALL"],
        # "BatteryPercent"] — the sensor names arrive nested inside the header.
        names_at = next((i for i, c in enumerate(cols) if isinstance(c, list)), None)
        if names_at is None or len(raw) <= names_at:
            return
        names = cols[names_at]
        values = raw[names_at] or []

        contact: Dict[str, int] = {}
        overall = None
        for name, value in zip(names, values):
            if name == CQ_OVERALL_KEY:
                overall = float(value)      # a percentage, unlike the rest
            elif isinstance(value, (int, float)):
                contact[name] = int(value)  # 0 (nothing) to 4 (good)

        self.quality.update({"cq": contact, "cq_overall": overall})
        for key, index in (("battery", 0), ("signal", 1), ("battery_percent", 3)):
            if index < len(raw) and not isinstance(raw[index], list):
                self.quality[key] = raw[index]
        self._emit_quality()

    def _handle_eq(self, raw: List[Any]):
        cols = self.cols.get("eq") or []
        # ["batteryPercent", "overall", "sampleRateQuality", <sensor names>]
        if not cols or len(raw) < 3:
            return
        by_name = dict(zip(cols, raw))
        self.quality.update({
            # `overall` is already a percentage (92 on a headset reading 4/4/4/4/3),
            # while the per-sensor entries share the 0-4 grading `dev` uses.
            "eq_overall": float(by_name.get("overall", 0) or 0),
            "sample_rate_quality": float(by_name.get("sampleRateQuality", 0) or 0),
            "eq": {
                name: int(value)
                for name, value in list(by_name.items())[3:]
                if isinstance(value, (int, float))
            },
        })
        self._emit_quality()

    def _emit_quality(self):
        self.emit("quality", dict(self.quality))

    # ------------------------------------------------------------- training
    def _handle_sys(self, raw: List[Any]):
        """Training events. `sys` carries [detection, event]."""
        if not isinstance(raw, list) or len(raw) < 2:
            return
        detection, event = str(raw[0]), str(raw[1])
        if detection != "mentalCommand":
            self._log("info", "log.sys_event", detail=str(raw))
            return

        action = self.training_action or ""
        self.emit("training", {"event": event, "action": action})

        if event == "MC_Completed":
            asyncio.create_task(self._after_accept())
        elif event in ("MC_SignatureUpdated", "MC_DataErased"):
            asyncio.create_task(self._resync())

        if event == "MC_Succeeded":
            # EmotivBCI asks for the threshold the moment a recording lands,
            # and shows the score before offering accept or discard. Do the
            # same: deciding without it is guessing.
            asyncio.create_task(self._emit_training_score(action))

        if self.on_training_event:
            # Anything timed to the recording hangs off MC_Started rather than
            # off the request returning: `start` only means Cortex accepted the
            # setup, and the eight seconds it records are counted from this event.
            self.on_training_event(event, action)

    def _handle_com(self, raw: List[Any]):
        """`com` arrives as [action, power]."""
        if not raw:
            return
        action = str(raw[0]) if len(raw) > 0 else "neutral"
        try:
            power = float(raw[1]) if len(raw) > 1 else 0.0
        except (TypeError, ValueError):
            power = 0.0
        if self.on_command:
            self.on_command(action, power)
