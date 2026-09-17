# Working on this repo

EMOTIV Cortex mental commands controlling Spotify. Read `README.md` for what it
does; this file is for what the code cannot tell you.

The Cortex client, sensor-quality display and training flow come from
`emotiv-brain-light`. **Its `CLAUDE.md` is the reference for every Cortex quirk**
(training event order, profile inheritance, the `dev`/`eq` formats, error codes)
— read it before touching `cortex_client.py` or the training UI, and port fixes
in both directions.

## Running and testing

```bash
python app.py                                  # the app
python -m unittest discover -s tests -t .      # trigger, Spotify auth + player
node tests/check_i18n.js                       # EN/ZH parity, no untranslated keys
python -m http.server 8765                     # then tests/ui_harness.html?scenario=…
```

The harness runs the real `ui/` against a stubbed `window.pywebview.api` and the
exact event shapes `engine.py` emits. Use it for any interface change, and
measure with JavaScript rather than trusting a screenshot: screenshots catch CSS
transitions mid-flight (a 74% bar once photographed at 35%).

**Prefer proving over asserting.** The worst bug found while building this app
looked correct when read, and only showed up by running the window on Windows.

## Traps that already bit

**Nothing public on `Api` except the methods the page calls.** pywebview builds
the JavaScript API by walking `dir()` of the `js_api` object and recursing into
every attribute not starting with `_`. A public `self.window` or `self.engine`
sends it crawling through the pywebview Window, the asyncio loop and the thread
pools, and the window never becomes responsive. Measured on Windows with
pywebview 6.2.1: `Responding=False` from the first second, py-spy showed the
`generate_js_object` thread deep in `get_functions`. Underscore everything else.
`emotiv-brain-light` had the same shape and froze identically.

**A launch check does not catch a frozen window.** The CI step proves the
process stays up; a hung UI thread stays up too. Open release builds by hand.

**Screenshots of the WebView2 window need real screen pixels.** `PrintWindow`
returns solid white for GPU-composited content. Pin the window topmost briefly
and copy the screen instead.

**Toggles must read fresh state.** `play_pause` and `shuffle` query
`/me/player` right before acting. The poller's copy can be seconds old; someone
who paused from their phone would have a "pause" thought resume the music.

**Never emit from the shutdown path.** Same as Brain Light: `closing` runs on
the UI thread, `evaluate_js` blocks on that thread, and the app deadlocks into
a Force Quit. `Api._closing` guards `_push`.

**The remembered device is a fallback, not a target.** `player.preferred_device`
is only used when Spotify has no active device — a plain play answers 404 then.
Aiming every play at it would yank music off a phone someone just started it on.
Spotify's device ids are only "persistent to some extent", so a stale one must
fail soft: the picker simply stops showing it.

**Spotify HTTP never runs on the engine loop.** `com` arrives there at 8 Hz; one
slow request would stall detection. Everything goes through the single-worker
`Engine._spotify` executor, which also keeps a thought and a click from racing
into play-then-pause.

## Spotify rules that shape the design

Verified against Spotify's documentation in September 2026. Re-check before
relying on them.

| Rule | Consequence here |
|---|---|
| Development-mode apps: **5 users**, allowlisted; extended quota only for registered organisations with **250k MAU** | No shared EMOTIV Client ID. Each user creates their own Spotify app. |
| PKCE needs **no client secret**, and is recommended where one "can't be safely stored" | Only a Client ID is ever stored. |
| `localhost` is **not** an accepted redirect URI; `http` is allowed for loopback IP literals | `http://127.0.0.1:43917/callback`, fixed so it is one exact string to paste. |
| Playback control "only works for users who have Spotify **Premium**" | Stated in Settings and the README before anyone gets as far as a refused command. |
| The app name must not include "Spotify"; metadata must link back to Spotify | "EMOTIV Mind Music … for Spotify"; *Open in Spotify* under now playing. |
| "You must always attribute content from Spotify with the logo" | **Not done yet** — the official logo asset has to be added. Only a text link-back exists. |

## Trigger defaults

`controls.py` documents each rule. The numbers are starting points, not
measurements: threshold 0.6 (the academy example's hard-coded 0.7 left freshly
trained profiles unable to fire), hold 0.5 s (four `com` samples at 8 Hz),
cooldown 2 s. Tune them against real headsets and record what was measured.

## Conventions

**The backend never emits user-facing text** — a translation code plus
parameters, rendered by `ui/i18n.js`. `tests/check_i18n.js` fails the build on
a missing key, a key present in only one language, or a leftover mention of a
light.

**Bindings are per profile** (`settings.bindings[profile][command]`). A stored
choice, including "none", always wins over a default.

## Still unverified

- Never run against a real headset, and never against the real Spotify API —
  sign-in and player are tested against fake servers that speak Spotify's
  documented shapes.
- The macOS build has not been opened.
- The Spotify logo attribution the design guidelines require is missing.
