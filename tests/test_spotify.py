"""Spotify sign-in and player, against fake servers speaking Spotify's shapes.

Nothing here reaches the real Spotify: the token endpoint is a local HTTP
server and the Web API is an injected transport. What is real is the loopback
redirect server, the PKCE maths and the refresh bookkeeping.
"""
import http.server
import io
import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from spotify import auth
from spotify.auth import LoginFlow, SpotifyAuthError, TokenStore
from spotify.player import SpotifyError, SpotifyPlayer


# ------------------------------------------------------------ fake token server
class FakeTokenServer:
    """Answers POST /api/token the way accounts.spotify.com does."""

    def __init__(self):
        self.requests = []
        self.response = {"access_token": "access-1", "refresh_token": "refresh-1",
                         "expires_in": 3600, "token_type": "Bearer"}
        self.status = 200
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = urllib.parse.parse_qs(self.rfile.read(length).decode("ascii"))
                server.requests.append({k: v[0] for k, v in body.items()})
                payload = json.dumps(server.response).encode("utf-8")
                self.send_response(server.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/token"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class MemoryStore:
    def __init__(self, **values):
        self.values = {"access_token": "", "expires_at": 0.0, "refresh_token": "", **values}

    def load(self):
        return dict(self.values)

    def save(self, values):
        self.values.update(values)


class PkceTests(unittest.TestCase):
    def test_challenge_matches_the_rfc_7636_example(self):
        # RFC 7636, Appendix B.
        self.assertEqual(
            auth.challenge_for("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        )

    def test_verifier_is_within_the_allowed_length_and_alphabet(self):
        verifier = auth.make_verifier()
        self.assertTrue(43 <= len(verifier) <= 128)
        self.assertRegex(verifier, r"^[A-Za-z0-9\-._~]+$")

    def test_authorize_url_carries_pkce_and_no_secret(self):
        url = auth.authorize_url("my-client", "challenge", "state-1")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], [auth.REDIRECT_URI])
        self.assertNotIn("client_secret", query)

    def test_redirect_uri_is_loopback_literal_not_localhost(self):
        # Spotify rejects "localhost"; the loopback IP literal is what it accepts.
        self.assertTrue(auth.REDIRECT_URI.startswith("http://127.0.0.1:"))


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeTokenServer()
        self.now = 1000.0

    def tearDown(self):
        self.server.close()

    def store(self, **values):
        memory = MemoryStore(**values)
        tokens = TokenStore(lambda: "my-client", memory.load, memory.save,
                            clock=lambda: self.now, token_url=self.server.url)
        return tokens, memory

    def test_a_fresh_token_is_used_without_refreshing(self):
        tokens, _ = self.store(access_token="cached", expires_at=self.now + 3000, refresh_token="r")
        self.assertEqual(tokens.access_token(), "cached")
        self.assertEqual(self.server.requests, [])

    def test_a_token_near_expiry_is_refreshed_first(self):
        tokens, memory = self.store(access_token="old", expires_at=self.now + 30, refresh_token="r")
        self.assertEqual(tokens.access_token(), "access-1")
        sent = self.server.requests[0]
        self.assertEqual(sent["grant_type"], "refresh_token")
        self.assertEqual(sent["client_id"], "my-client")
        self.assertNotIn("client_secret", sent)
        self.assertEqual(memory.values["expires_at"], self.now + 3600)

    def test_refresh_without_a_new_refresh_token_keeps_the_old_one(self):
        self.server.response = {"access_token": "access-2", "expires_in": 3600}
        tokens, memory = self.store(access_token="", expires_at=0, refresh_token="keep-me")
        tokens.access_token()
        self.assertEqual(memory.values["refresh_token"], "keep-me")

    def test_a_revoked_refresh_token_signs_the_user_out(self):
        self.server.status = 400
        self.server.response = {"error": "invalid_grant", "error_description": "Refresh token revoked"}
        tokens, memory = self.store(access_token="", expires_at=0, refresh_token="revoked")
        with self.assertRaises(SpotifyAuthError) as caught:
            tokens.access_token()
        self.assertEqual(caught.exception.code, "err.spotify_session_expired")
        self.assertFalse(tokens.signed_in)

    def test_not_signed_in_is_its_own_error(self):
        tokens, _ = self.store()
        with self.assertRaises(SpotifyAuthError) as caught:
            tokens.access_token()
        self.assertEqual(caught.exception.code, "err.spotify_not_signed_in")


class LoginFlowTests(unittest.TestCase):
    """The loopback redirect, driven the way a browser would drive it."""

    def setUp(self):
        self.server = FakeTokenServer()
        self.done = threading.Event()
        self.result = {}

    def tearDown(self):
        self.server.close()

    def on_done(self, payload, error):
        self.result = {"payload": payload, "error": error}
        self.done.set()

    def start(self):
        opened = []
        flow = LoginFlow("my-client", self.on_done, open_browser=opened.append,
                         token_url=self.server.url)
        flow.start()
        self.assertEqual(opened, [flow.url])
        return flow

    def visit(self, query):
        url = auth.REDIRECT_URI + "?" + urllib.parse.urlencode(query)
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_a_successful_redirect_exchanges_the_code_with_the_verifier(self):
        flow = self.start()
        self.assertEqual(self.visit({"code": "the-code", "state": flow.state}), 200)
        self.assertTrue(self.done.wait(5))
        self.assertIsNone(self.result["error"])
        self.assertEqual(self.result["payload"]["refresh_token"], "refresh-1")

        sent = self.server.requests[0]
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertEqual(sent["code"], "the-code")
        self.assertEqual(sent["code_verifier"], flow.verifier)
        self.assertEqual(sent["redirect_uri"], auth.REDIRECT_URI)
        self.assertNotIn("client_secret", sent)

    def test_a_forged_state_is_refused_without_exchanging(self):
        self.start()
        self.assertEqual(self.visit({"code": "stolen", "state": "not-ours"}), 400)
        self.assertTrue(self.done.wait(5))
        self.assertEqual(self.result["error"].code, "err.spotify_login_state")
        self.assertEqual(self.server.requests, [])

    def test_the_user_declining_is_reported_as_such(self):
        flow = self.start()
        self.visit({"error": "access_denied", "state": flow.state})
        self.assertTrue(self.done.wait(5))
        self.assertEqual(self.result["error"].code, "err.spotify_login_denied")

    def test_the_port_is_released_after_the_attempt(self):
        flow = self.start()
        self.visit({"code": "c", "state": flow.state})
        self.assertTrue(self.done.wait(5))
        # A second sign-in must be able to bind the same fixed port.
        flow.cancel()  # already finished; waits for the socket to close
        retry = LoginFlow("my-client", lambda p, e: None,
                          open_browser=lambda url: True, token_url=self.server.url)
        retry.start()
        retry.cancel()


class ExclusivePortTests(unittest.TestCase):
    """The redirect port must belong to exactly one listener."""

    def test_a_second_listener_cannot_bind_the_redirect_port(self):
        # On Windows, SO_REUSEADDR would let this succeed and the two listeners
        # would race for the authorization code.
        first = LoginFlow("c", lambda p, e: None, open_browser=lambda url: True)
        first.start()
        try:
            second = LoginFlow("c", lambda p, e: None, open_browser=lambda url: True)
            with self.assertRaises(SpotifyAuthError) as caught:
                second.start()
            self.assertEqual(caught.exception.code, "err.spotify_port_busy")
        finally:
            first.cancel()

    def test_cancel_returns_only_once_the_port_is_free(self):
        first = LoginFlow("c", lambda p, e: None, open_browser=lambda url: True)
        first.start()
        first.cancel()
        # No retry loop: cancel() must already have released it.
        again = LoginFlow("c", lambda p, e: None, open_browser=lambda url: True)
        again.start()
        again.cancel()


# ---------------------------------------------------------------- the player
class FakeResponse:
    def __init__(self, body=b""):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(status, payload, headers=None):
    return urllib.error.HTTPError("https://api.spotify.com", status, "error",
                                  headers or {}, io.BytesIO(json.dumps(payload).encode()))


class FakeApi:
    """Records calls and answers from a script keyed by (method, path)."""

    def __init__(self):
        self.calls = []
        self.bodies = []
        self.answers = {}

    def __call__(self, request, timeout):
        parsed = urllib.parse.urlparse(request.full_url)
        key = (request.get_method(), parsed.path.replace("/v1", "", 1))
        self.calls.append((key[0], key[1], urllib.parse.parse_qs(parsed.query)))
        self.bodies.append(json.loads(request.data) if request.data else None)
        answer = self.answers.get(key, FakeResponse())
        if isinstance(answer, Exception):
            raise answer
        return answer


PLAYING = {
    "is_playing": True, "shuffle_state": False, "progress_ms": 1000,
    "device": {"name": "Desk speaker", "volume_percent": 55, "supports_volume": True},
    "item": {"name": "Song", "duration_ms": 200000, "artists": [{"name": "A"}, {"name": "B"}],
             "album": {"name": "Album", "images": [
                 {"url": "big", "width": 640}, {"url": "mid", "width": 300}, {"url": "small", "width": 64}]},
             "external_urls": {"spotify": "https://open.spotify.com/track/x"}},
}


class PlayerTests(unittest.TestCase):
    def setUp(self):
        memory = MemoryStore(access_token="token", expires_at=10**12, refresh_token="r")
        self.api = FakeApi()
        self.player = SpotifyPlayer(TokenStore(lambda: "c", memory.load, memory.save), transport=self.api)

    def playing(self, **overrides):
        state = json.loads(json.dumps(PLAYING))
        state.update(overrides)
        self.api.answers[("GET", "/me/player")] = FakeResponse(json.dumps(state).encode())

    def test_state_is_trimmed_for_the_interface(self):
        self.playing()
        state = self.player.state()
        self.assertEqual(state["artists"], "A, B")
        self.assertEqual(state["image"], "mid")
        self.assertEqual(state["url"], "https://open.spotify.com/track/x")

    def test_nothing_playing_is_none_not_an_error(self):
        self.assertIsNone(self.player.state())

    def test_play_pause_pauses_when_playing(self):
        self.playing(is_playing=True)
        self.assertEqual(self.player.perform("play_pause"), "pause")
        self.assertEqual(self.api.calls[-1][:2], ("PUT", "/me/player/pause"))

    def test_play_pause_plays_when_paused(self):
        self.playing(is_playing=False)
        self.assertEqual(self.player.perform("play_pause"), "play")
        self.assertEqual(self.api.calls[-1][:2], ("PUT", "/me/player/play"))

    def test_play_pause_ignores_a_stale_cached_state(self):
        # The poller last saw it playing, but it has since been paused from
        # another device. The toggle must read fresh and resume, not pause.
        self.player.last_state = {"is_playing": True}
        self.playing(is_playing=False)
        self.assertEqual(self.player.perform("play_pause"), "play")

    def test_volume_steps_and_clamps(self):
        self.playing(device={"name": "d", "volume_percent": 95, "supports_volume": True})
        self.player.perform("volume_up")
        self.assertEqual(self.api.calls[-1][2]["volume_percent"], ["100"])

    def test_shuffle_flips_the_current_state(self):
        self.playing(shuffle_state=True)
        self.player.perform("shuffle")
        self.assertEqual(self.api.calls[-1][2]["state"], ["false"])

    def test_premium_required_is_named(self):
        self.api.answers[("PUT", "/me/player/pause")] = http_error(
            403, {"error": {"status": 403, "message": "Player command failed: Premium required",
                            "reason": "PREMIUM_REQUIRED"}})
        with self.assertRaises(SpotifyError) as caught:
            self.player.perform("pause")
        self.assertEqual(caught.exception.code, "err.spotify_premium")

    def test_no_active_device_is_named(self):
        self.api.answers[("POST", "/me/player/next")] = http_error(
            404, {"error": {"status": 404, "message": "Player command failed: No active device found",
                            "reason": "NO_ACTIVE_DEVICE"}})
        with self.assertRaises(SpotifyError) as caught:
            self.player.perform("next")
        self.assertEqual(caught.exception.code, "err.spotify_no_device")

    def test_rate_limit_reports_the_wait(self):
        self.api.answers[("POST", "/me/player/next")] = http_error(
            429, {"error": {"status": 429, "message": "rate limited"}}, headers={"Retry-After": "7"})
        with self.assertRaises(SpotifyError) as caught:
            self.player.perform("next")
        self.assertEqual(caught.exception.code, "err.spotify_rate_limited")
        self.assertEqual(caught.exception.params["seconds"], "7")

    def test_a_bodyless_put_still_sends_content_length(self):
        sent = []
        self.player.transport = lambda request, timeout: sent.append(request) or FakeResponse()
        self.player.perform("pause")
        self.assertEqual(sent[0].get_header("Content-length"), "0")

    # ------------------------------------------------------------- devices
    def test_devices_are_trimmed_and_unaddressable_ones_dropped(self):
        self.api.answers[("GET", "/me/player/devices")] = FakeResponse(json.dumps({"devices": [
            {"id": "laptop", "name": "My laptop", "type": "Computer", "is_active": True,
             "is_restricted": False, "volume_percent": 40, "supports_volume": True},
            {"id": None, "name": "Ghost", "type": "Speaker", "is_active": False,
             "is_restricted": False, "volume_percent": None, "supports_volume": False},
            {"id": "tv", "name": "Living room TV", "type": "TV", "is_active": False,
             "is_restricted": True, "volume_percent": None, "supports_volume": False},
        ]}).encode())
        devices = self.player.devices()
        self.assertEqual([d["id"] for d in devices], ["laptop", "tv"])
        self.assertEqual(devices[0]["type"], "computer")
        self.assertTrue(devices[1]["is_restricted"])

    def test_transfer_sends_one_device_and_keeps_play_state_by_default(self):
        self.player.transfer("kitchen")
        self.assertEqual(self.api.calls[-1][:2], ("PUT", "/me/player"))
        self.assertEqual(self.api.bodies[-1], {"device_ids": ["kitchen"]})

    def test_transfer_can_start_playback(self):
        self.player.transfer("kitchen", play=True)
        self.assertEqual(self.api.bodies[-1], {"device_ids": ["kitchen"], "play": True})

    def test_a_json_body_carries_its_own_length(self):
        sent = []
        self.player.transport = lambda request, timeout: sent.append(request) or FakeResponse()
        self.player.transfer("kitchen")
        self.assertEqual(sent[0].get_header("Content-type"), "application/json")
        self.assertEqual(sent[0].get_header("Content-length"), str(len(sent[0].data)))

    def test_play_with_nothing_active_starts_on_the_preferred_device(self):
        # 204 from /me/player: no active playback anywhere.
        self.player.preferred_device = "kitchen"
        self.assertEqual(self.player.perform("play_pause"), "play")
        method, path, query = self.api.calls[-1]
        self.assertEqual((method, path), ("PUT", "/me/player/play"))
        self.assertEqual(query["device_id"], ["kitchen"])

    def test_play_retries_on_the_preferred_device_when_spotify_has_none(self):
        self.playing(is_playing=False)
        self.player.preferred_device = "kitchen"
        attempts = []

        def api(request, timeout):
            parsed = urllib.parse.urlparse(request.full_url)
            if parsed.path.endswith("/me/player"):
                return FakeResponse(json.dumps(PLAYING | {"is_playing": False}).encode())
            attempts.append(urllib.parse.parse_qs(parsed.query))
            if len(attempts) == 1:
                raise http_error(404, {"error": {"status": 404, "message": "No active device found",
                                                 "reason": "NO_ACTIVE_DEVICE"}})
            return FakeResponse()

        self.player.transport = api
        self.assertEqual(self.player.perform("play"), "play")
        self.assertEqual(attempts, [{}, {"device_id": ["kitchen"]}])

    def test_without_a_preferred_device_no_device_is_still_reported(self):
        self.api.answers[("PUT", "/me/player/play")] = http_error(
            404, {"error": {"status": 404, "message": "No active device found", "reason": "NO_ACTIVE_DEVICE"}})
        with self.assertRaises(SpotifyError) as caught:
            self.player.perform("play")
        self.assertEqual(caught.exception.code, "err.spotify_no_device")


# ------------------------------------------------------------------ TLS roots
class TlsTests(unittest.TestCase):
    """The frozen macOS app has no system CA file (see spotify/tls.py), so every
    Spotify request must carry the bundled roots rather than Python's default."""

    def test_the_context_verifies_against_bundled_roots(self):
        import ssl
        from spotify import tls
        ctx = tls.context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        # certifi carries well over a hundred roots; an empty store is the bug.
        self.assertGreater(len(ctx.get_ca_certs()), 100)

    def test_every_spotify_request_uses_that_context(self):
        from unittest import mock
        from spotify import player, tls
        seen = []

        def fake_urlopen(request, timeout=None, context=None):
            seen.append(context)
            raise urllib.error.URLError("stop here")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(SpotifyAuthError):
                auth.refresh("client", "refresh")
            with self.assertRaises(urllib.error.URLError):
                player._default_transport(urllib.request.Request(player.API_BASE), 1)
        self.assertEqual(seen, [tls.context(), tls.context()])


if __name__ == "__main__":
    unittest.main()
