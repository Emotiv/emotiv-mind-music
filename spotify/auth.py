"""Signing in to Spotify: Authorization Code with PKCE, through the browser.

Why this flow, and why each person brings their own Client ID — both forced by
Spotify's rules rather than chosen:

  * PKCE needs no client secret. Spotify recommends it for "any other type of
    application where the client secret can't be safely stored", which is
    exactly an installed desktop app: anything baked into it can be extracted.
  * An app in Spotify's development mode serves at most five allowlisted users,
    and extended quota is only granted to registered organisations with 250k
    monthly active users. One shared EMOTIV Client ID would stop working at the
    sixth person, so each user registers their own — the same arrangement
    Cortex already asks for.
  * `localhost` is not an accepted redirect URI; the loopback literal
    127.0.0.1 over plain HTTP is. The port is fixed so the URI the user pastes
    into the Spotify dashboard is one exact string that always matches.

https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow
https://developer.spotify.com/documentation/web-api/concepts/redirect_uri
"""
import base64
import hashlib
import http.server
import json
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Callable, Dict, Optional

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 43917
REDIRECT_PATH = "/callback"
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"

# Only what the player needs: read what is playing, and change it.
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"

# How long the browser window has before the sign-in is abandoned.
LOGIN_TIMEOUT = 300.0

# Refresh this long before the token actually expires, so a command never
# lands on a token that dies in flight.
EXPIRY_MARGIN = 60.0


class SpotifyAuthError(Exception):
    """Carries a translation code, like everything the backend reports."""

    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = params


# ------------------------------------------------------------------ PKCE
def make_verifier() -> str:
    """A code verifier: 43-128 characters from the unreserved set (RFC 7636)."""
    # 64 random bytes -> 86 base64url characters, comfortably inside the range.
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(client_id: str, challenge: str, state: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "scope": SCOPES,
        "state": state,
    })


# ---------------------------------------------------------------- tokens
def _post_token(body: Dict[str, str], token_url: str = TOKEN_URL) -> Dict[str, object]:
    data = urllib.parse.urlencode(body).encode("ascii")
    request = urllib.request.Request(
        token_url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {}
        error = payload.get("error", "")
        # invalid_grant is a refresh token Spotify no longer honours — revoked
        # from the account page, or the app's Client ID changed. Only signing
        # in again fixes it, so it gets its own code.
        if error == "invalid_grant":
            raise SpotifyAuthError("err.spotify_session_expired")
        if error == "invalid_client":
            raise SpotifyAuthError("err.spotify_bad_client_id")
        raise SpotifyAuthError("err.spotify_token_failed",
                               detail=payload.get("error_description") or error or str(e.code))
    except (urllib.error.URLError, OSError) as e:
        raise SpotifyAuthError("err.spotify_unreachable", detail=str(getattr(e, "reason", e)))


def exchange_code(client_id: str, code: str, verifier: str,
                  token_url: str = TOKEN_URL) -> Dict[str, object]:
    return _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }, token_url)


def refresh(client_id: str, refresh_token: str, token_url: str = TOKEN_URL) -> Dict[str, object]:
    return _post_token({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }, token_url)


class TokenStore:
    """The access token, its expiry, and the refresh token behind it.

    Persistence is injected rather than imported so the refresh logic can be
    tested without touching the user's real settings file.
    """

    def __init__(self, client_id_getter: Callable[[], str],
                 load: Callable[[], Dict[str, object]],
                 save: Callable[[Dict[str, object]], None],
                 clock: Callable[[], float] = time.time,
                 token_url: str = TOKEN_URL):
        self._client_id = client_id_getter
        self._load = load
        self._save = save
        self._clock = clock
        self._token_url = token_url
        self._lock = threading.Lock()

    @property
    def signed_in(self) -> bool:
        return bool(self._load().get("refresh_token"))

    def apply(self, payload: Dict[str, object]):
        """Store a token response. Spotify may omit a new refresh token."""
        current = self._load()
        self._save({
            "access_token": payload.get("access_token", ""),
            "expires_at": self._clock() + float(payload.get("expires_in", 3600) or 3600),
            # "If a new refresh token is not returned, continue using the
            # existing token" — Spotify's PKCE tutorial.
            "refresh_token": payload.get("refresh_token") or current.get("refresh_token", ""),
        })

    def clear(self):
        self._save({"access_token": "", "expires_at": 0.0, "refresh_token": ""})

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            stored = self._load()
            refresh_token = stored.get("refresh_token")
            if not refresh_token:
                raise SpotifyAuthError("err.spotify_not_signed_in")
            fresh = self._clock() < float(stored.get("expires_at") or 0) - EXPIRY_MARGIN
            if stored.get("access_token") and fresh and not force_refresh:
                return str(stored["access_token"])

            client_id = self._client_id()
            if not client_id:
                raise SpotifyAuthError("err.spotify_no_client_id")
            try:
                payload = refresh(client_id, str(refresh_token), self._token_url)
            except SpotifyAuthError as e:
                if e.code == "err.spotify_session_expired":
                    self.clear()
                raise
            self.apply(payload)
            return str(self._load()["access_token"])


# ------------------------------------------------------------- browser login
class _ExclusiveServer(http.server.HTTPServer):
    """An HTTP server that owns its port outright.

    HTTPServer sets SO_REUSEADDR, and on Windows that flag does not mean what
    it means on macOS: it lets a *second* socket bind a port that is already
    listening. Two listeners on the redirect port then race for Spotify's
    authorization code — a stale one from a cancelled attempt, or any other
    program that asked. Found as an intermittent connection reset in the tests.
    So: no address reuse, and Windows' exclusive-use flag on top.
    """

    allow_reuse_address = False

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


_PAGE = """<!doctype html><meta charset="utf-8"><title>EMOTIV Mind Music</title>
<body style="font-family:-apple-system,Segoe UI,sans-serif;background:#0c0e12;color:#f2f4f8;
display:grid;place-items:center;height:100vh;margin:0"><div style="text-align:center;max-width:420px">
<h1 style="font-size:20px;font-weight:600">{title}</h1>
<p style="color:#8b93a3;line-height:1.6">{body}</p></div></body>"""


class LoginFlow:
    """One sign-in attempt: open the browser, catch the redirect, swap the code.

    The loopback server lives only for the length of the attempt and answers
    exactly one path. It binds 127.0.0.1, never 0.0.0.0, so nothing else on the
    network can hand it a code.
    """

    def __init__(self, client_id: str, on_done: Callable[[Optional[Dict[str, object]], Optional[SpotifyAuthError]], None],
                 open_browser: Callable[[str], bool] = webbrowser.open,
                 token_url: str = TOKEN_URL):
        self.client_id = client_id
        self.on_done = on_done
        self.open_browser = open_browser
        self.token_url = token_url
        self.verifier = make_verifier()
        self.state = secrets.token_urlsafe(24)
        self.url = authorize_url(client_id, challenge_for(self.verifier), self.state)
        self._server: Optional[http.server.HTTPServer] = None
        self._server_lock = threading.Lock()
        self._finished = threading.Event()
        # Set once the listening socket is actually closed, not merely asked to.
        self._closed = threading.Event()

    def start(self):
        if not self.client_id:
            raise SpotifyAuthError("err.spotify_no_client_id")
        flow = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the console quiet
                pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != REDIRECT_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                query = urllib.parse.parse_qs(parsed.query)
                ok, title, body = flow._handle(query)
                page = _PAGE.format(title=title, body=body).encode("utf-8")
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

        try:
            self._server = _ExclusiveServer((REDIRECT_HOST, REDIRECT_PORT), Handler)
        except OSError as e:
            raise SpotifyAuthError("err.spotify_port_busy", port=REDIRECT_PORT, detail=str(e))

        threading.Thread(target=self._server.serve_forever, name="spotify-login", daemon=True).start()
        threading.Thread(target=self._timeout, daemon=True).start()
        self.open_browser(self.url)

    def cancel(self):
        """Abandon the attempt and wait until the port is free again.

        Waiting matters: a new sign-in started straight after would otherwise
        find the port still held, because the server shuts down on its own
        thread.
        """
        if not self._finished.is_set():
            self._finish(None, SpotifyAuthError("err.spotify_login_cancelled"))
        self._shutdown()
        self._closed.wait(5.0)

    def _handle(self, query):
        """Returns (ok, title, body) for the page shown in the browser."""
        # English only: this page is on screen for the second it takes to
        # switch back to the app, which reports the outcome in the user's
        # language.
        if query.get("state", [""])[0] != self.state:
            self._finish(None, SpotifyAuthError("err.spotify_login_state"))
            return False, "Sign-in could not be verified", "Please return to EMOTIV Mind Music and try again."
        if "error" in query:
            reason = query["error"][0]
            code = "err.spotify_login_denied" if reason == "access_denied" else "err.spotify_login_failed"
            self._finish(None, SpotifyAuthError(code, detail=reason))
            return False, "Spotify was not connected", "You can close this tab and return to EMOTIV Mind Music."
        code = query.get("code", [""])[0]
        if not code:
            self._finish(None, SpotifyAuthError("err.spotify_login_failed", detail="no code"))
            return False, "Spotify was not connected", "You can close this tab and return to EMOTIV Mind Music."
        try:
            payload = exchange_code(self.client_id, code, self.verifier, self.token_url)
        except SpotifyAuthError as e:
            self._finish(None, e)
            return False, "Spotify was not connected", "You can close this tab and return to EMOTIV Mind Music."
        self._finish(payload, None)
        return True, "Spotify is connected", "You can close this tab and return to EMOTIV Mind Music."

    def _timeout(self):
        if not self._finished.wait(LOGIN_TIMEOUT):
            self._finish(None, SpotifyAuthError("err.spotify_login_timeout"))

    def _finish(self, payload, error):
        if self._finished.is_set():
            return
        self._finished.set()
        try:
            self.on_done(payload, error)
        finally:
            threading.Thread(target=self._shutdown, daemon=True).start()

    def _shutdown(self):
        with self._server_lock:
            server, self._server = self._server, None
        if not server:
            return
        try:
            server.shutdown()
        except Exception:
            pass
        finally:
            try:
                server.server_close()
            finally:
                self._closed.set()
