"""The handful of Spotify Web API calls the player needs.

Every playback-control endpoint here only works for Spotify **Premium**
accounts ("This API only works for users who have Spotify Premium"), and it
controls whichever device Spotify considers active. Both failures are common
and neither is a bug, so each gets its own translation code instead of a raw
HTTP status.

https://developer.spotify.com/documentation/web-api/reference/pause-a-users-playback
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from controls import VOLUME_STEP
from spotify.auth import SpotifyAuthError, TokenStore

API_BASE = "https://api.spotify.com/v1"


class SpotifyError(Exception):
    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = params


Transport = Callable[[urllib.request.Request, float], Any]


def _default_transport(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)


class SpotifyPlayer:
    def __init__(self, tokens: TokenStore, transport: Transport = _default_transport,
                 api_base: str = API_BASE):
        self.tokens = tokens
        self.transport = transport
        self.api_base = api_base
        # The last state read back, for the interface. Deliberately *not* used
        # to decide what a toggle does: see perform().
        self.last_state: Optional[Dict[str, Any]] = None
        # The device the user picked. Used only when Spotify has no active
        # device, so a "play" thought can still start the music hands-free.
        self.preferred_device: str = ""

    # ------------------------------------------------------------- transport
    def _request(self, method: str, path: str, query: Optional[Dict[str, Any]] = None,
                 body: Optional[Dict[str, Any]] = None,
                 retry_auth: bool = True) -> Optional[Dict[str, Any]]:
        try:
            token = self.tokens.access_token()
        except SpotifyAuthError as e:
            raise SpotifyError(e.code, **e.params)

        url = self.api_base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": f"Bearer {token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(data))
        elif method in ("PUT", "POST"):
            # A bodyless PUT or POST still needs Content-Length, or some proxies
            # answer 411 before Spotify ever sees the request.
            data = b""
            headers["Content-Length"] = "0"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)

        try:
            with self.transport(request, 10.0) as response:
                body = response.read()
                if not body:
                    return None
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry_auth:
                # The token was revoked or expired early. Refresh once; a second
                # 401 means signing in again is the only way forward.
                try:
                    self.tokens.access_token(force_refresh=True)
                except SpotifyAuthError as auth_error:
                    raise SpotifyError(auth_error.code, **auth_error.params)
                return self._request(method, path, query, body, retry_auth=False)
            raise self._translate(e)
        except (urllib.error.URLError, OSError) as e:
            raise SpotifyError("err.spotify_unreachable", detail=str(getattr(e, "reason", e)))

    @staticmethod
    def _translate(e: urllib.error.HTTPError) -> SpotifyError:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        reason = (error or {}).get("reason", "") if isinstance(error, dict) else ""
        message = (error or {}).get("message", "") if isinstance(error, dict) else ""

        if e.code == 401:
            return SpotifyError("err.spotify_session_expired")
        if e.code == 403 and reason == "PREMIUM_REQUIRED":
            return SpotifyError("err.spotify_premium")
        if e.code == 403 and "premium" in message.lower():
            return SpotifyError("err.spotify_premium")
        if e.code == 403:
            # Development-mode apps refuse anyone not on the allowlist.
            return SpotifyError("err.spotify_forbidden", detail=message)
        if e.code == 404 and (reason == "NO_ACTIVE_DEVICE" or "device" in message.lower()):
            return SpotifyError("err.spotify_no_device")
        if e.code == 429:
            retry = e.headers.get("Retry-After", "") if e.headers else ""
            return SpotifyError("err.spotify_rate_limited", seconds=retry)
        return SpotifyError("err.spotify_api", status=e.code, detail=message)

    # ----------------------------------------------------------------- reads
    def state(self) -> Optional[Dict[str, Any]]:
        """What is playing, trimmed to what the interface shows.

        None when Spotify reports no active playback at all (204), which is
        what a closed Spotify app looks like.
        """
        raw = self._request("GET", "/me/player", {"additional_types": "track,episode"})
        if not raw:
            self.last_state = None
            return None

        item = raw.get("item") or {}
        images = (item.get("album") or {}).get("images") or item.get("images") or []
        # Spotify lists images largest first. 300px is plenty for the card and
        # a fraction of the 640px download.
        image = next((i.get("url") for i in images if (i.get("width") or 0) <= 320), None) \
            or (images[0].get("url") if images else None)
        artists = ", ".join(a.get("name", "") for a in item.get("artists") or [] if a.get("name")) \
            or (item.get("show") or {}).get("name", "")
        device = raw.get("device") or {}

        self.last_state = {
            "is_playing": bool(raw.get("is_playing")),
            "shuffle": bool(raw.get("shuffle_state")),
            "volume": device.get("volume_percent"),
            "supports_volume": device.get("supports_volume", True),
            "device": device.get("name", ""),
            "track": item.get("name", ""),
            "artists": artists,
            "album": (item.get("album") or {}).get("name", ""),
            "image": image,
            # Spotify's design guidelines require metadata to link back to it.
            "url": (item.get("external_urls") or {}).get("spotify", ""),
            "progress_ms": raw.get("progress_ms") or 0,
            "duration_ms": item.get("duration_ms") or 0,
        }
        return self.last_state

    # --------------------------------------------------------------- devices
    def devices(self) -> List[Dict[str, Any]]:
        """Every device Spotify can play on right now, in its own order.

        Only devices with Spotify open recently appear: a closed desktop app or
        a phone that has been idle for a while drops off the list, and some
        speaker models are never listed at all. The interface says so, because
        "my speaker is missing" is the question this list always prompts.
        """
        raw = self._request("GET", "/me/player/devices") or {}
        out = []
        for d in raw.get("devices") or []:
            # A device without an id cannot be addressed, so it cannot be chosen.
            if not d.get("id"):
                continue
            out.append({
                "id": d["id"],
                "name": d.get("name") or "",
                # Lower-cased with separators dropped, so "CastVideo" and
                # "cast_video" land on the same label.
                "type": (d.get("type") or "").lower().replace("_", "").replace(" ", ""),
                "is_active": bool(d.get("is_active")),
                # "No Web API commands will be accepted by this device."
                "is_restricted": bool(d.get("is_restricted")),
                "volume": d.get("volume_percent"),
            })
        return out

    def transfer(self, device_id: str, play: Optional[bool] = None) -> None:
        """Move playback to one device.

        `play=None` leaves playback as it was — paused music moves paused —
        which is what someone choosing a speaker expects. Spotify accepts
        exactly one id here; more than one is a 400.
        """
        body: Dict[str, Any] = {"device_ids": [device_id]}
        if play is not None:
            body["play"] = bool(play)
        self._request("PUT", "/me/player", body=body)

    def _play(self, state: Optional[Dict[str, Any]]):
        """Start playback, on the preferred device if nothing is active.

        Without an active device a plain play answers 404, and the only way out
        would be picking up a phone — which is precisely what someone
        controlling music with their mind may not be able to do.
        """
        if not state and self.preferred_device:
            self._request("PUT", "/me/player/play", {"device_id": self.preferred_device})
            return
        try:
            self._request("PUT", "/me/player/play")
        except SpotifyError as e:
            if e.code != "err.spotify_no_device" or not self.preferred_device:
                raise
            self._request("PUT", "/me/player/play", {"device_id": self.preferred_device})

    # --------------------------------------------------------------- actions
    def perform(self, action: str) -> str:
        """Run one player action. Returns what actually happened, as an action name.

        play_pause resolves to "play" or "pause", so the interface can say which.

        Every action that depends on the current state reads it fresh. The
        poller's copy can be seconds old, and someone who paused from their
        phone a moment ago would otherwise have a "pause" thought resume the
        music — a toggle acting on stale state does the opposite of what was
        meant. One extra GET is cheap next to that.
        """
        state = None
        if action in ("play_pause", "play"):
            state = self.state()
        if action == "play_pause":
            action = "pause" if state and state.get("is_playing") else "play"

        if action == "play":
            self._play(state)
        elif action == "pause":
            self._request("PUT", "/me/player/pause")
        elif action == "next":
            self._request("POST", "/me/player/next")
        elif action == "previous":
            self._request("POST", "/me/player/previous")
        elif action in ("volume_up", "volume_down"):
            state = self.state()
            if not state:
                raise SpotifyError("err.spotify_no_device")
            if state.get("supports_volume") is False or state.get("volume") is None:
                raise SpotifyError("err.spotify_no_volume", device=state.get("device", ""))
            step = VOLUME_STEP if action == "volume_up" else -VOLUME_STEP
            volume = max(0, min(100, int(state["volume"]) + step))
            self._request("PUT", "/me/player/volume", {"volume_percent": volume})
        elif action == "shuffle":
            state = self.state()
            if not state:
                raise SpotifyError("err.spotify_no_device")
            self._request("PUT", "/me/player/shuffle",
                          {"state": "false" if state.get("shuffle") else "true"})
        elif action == "none":
            return "none"
        else:
            raise SpotifyError("err.unknown_player_action", action=action)
        return action
