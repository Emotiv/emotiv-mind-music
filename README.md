# EMOTIV Mind Music

Control your music with your mind — for Spotify.

Train a few mental commands with an EMOTIV headset, choose what each one does —
play or pause, next song, previous song, volume, shuffle — and then just think
it. The app shows, for every command, how strong the thought is right now, the
line it has to cross, and what happens when it does.

Built on [EMOTIV Brain Light](https://github.com/Emotiv/emotiv-brain-light)'s
Cortex client and training flow, and on the idea behind the
[EMOTIV Academy Spotify example](https://github.com/Emotiv/emotiv-academy/tree/main/Spotify_BCI).

---

## 📥 Install and set up

For someone installing the app. If you are working on the code, see
[Running from source](#-running-from-source).

### 1. What you need first

| | |
|---|---|
| **An EMOTIV headset** | Insight, EPOC, EPOC+ or EPOC X. |
| **An EMOTIV account** | Free, at [emotiv.com](https://www.emotiv.com/). |
| **EMOTIV Launcher** | Installed, **signed in** and running. It talks to the headset and runs the Cortex service this app connects to. |
| **Spotify Premium** | Spotify only lets apps play, pause and skip for Premium accounts. A free account can sign in, but every command will be refused. |
| **A computer** | Windows 10/11, or a Mac with Apple Silicon. There is no phone version — the Launcher is a desktop program. |

Your brain data never leaves your computer: the app reaches Cortex on
`wss://localhost:6868`, and sends Spotify nothing but player commands.

### 2. Create your EMOTIV app keys

1. Sign in at [emotiv.com](https://www.emotiv.com/) and open
   **[My Account → Cortex Apps](https://www.emotiv.com/my-account/cortex-apps/)**.
2. Create a new application. Any name works.
3. Copy the **Client ID** and the **Client Secret**.

**The secret is shown once.** Copy it somewhere safe before closing the page.

### 3. Create your Spotify app

Every person needs their own. Spotify limits an app in development mode to
**5 users**, and only grants more to registered businesses with 250,000 monthly
users — so a single shared EMOTIV app would stop working at the sixth person.

1. Open the **[Spotify Developer Dashboard](https://developer.spotify.com/dashboard)**,
   sign in, and click **Create app**.
2. Fill in:
   - **App name** — anything, but it **must not contain the word "Spotify"**
     (Spotify's rule). For example `My Mind Music`.
   - **App description** — anything.
   - **Website** — leave empty.
   - **Redirect URIs** — paste this **exactly**, then click **Add**:
     ```
     http://127.0.0.1:43917/callback
     ```
     It must be `127.0.0.1`, not `localhost` (Spotify rejects `localhost`),
     `http` not `https`, and port `43917`.
   - **Which API/SDKs** — tick **Web API** only.
3. Accept the terms and **Save**.
4. Open the app's **Settings** and copy its **Client ID**. You do **not** need
   the Client Secret — the app signs in with PKCE, which does not use one.
5. Open **User Management** and add the email address of the Spotify account
   you want to control.

### 4. Install the app

Download from the
[latest release](https://github.com/Emotiv/emotiv-mind-music/releases/latest):

| Platform | File |
|---|---|
| Windows 10/11 (x64) | `EMOTIV-Mind-Music-windows-x64-setup.exe` |
| macOS 11+ (Apple Silicon) | `EMOTIV-Mind-Music-macos-arm64.dmg` |

Intel Macs are not covered — the build is Apple Silicon only.

Neither build is **code-signed**, so both operating systems object the first
time. Nothing is wrong with the download.

**Windows.** Run the installer. It installs for your user only — no admin
rights. SmartScreen shows *"Windows protected your PC"*: click **More info** →
**Run anyway**. The window is drawn with Microsoft's WebView2 runtime, which
comes with Edge; the installer warns you if it is missing.

**macOS.** Open the `.dmg` and drag the app to **Applications**. Do not run it
from the disk image itself. macOS marks downloaded apps with a quarantine flag,
which for an unsigned app shows up as *"EMOTIV Mind Music is damaged and can't
be opened"*. It is not damaged. Clear the flag once, in Terminal:

```bash
xattr -dr com.apple.quarantine "/Applications/EMOTIV Mind Music.app"
```

Then open it normally. On macOS 15 and later the old right-click → *Open* trick
no longer works for unsigned apps, which is why the command is the one to use.
When macOS asks for **local network** access, allow it — Cortex and the Spotify
sign-in both need it.

### 5. First run

The app opens with a **Getting started** card that ticks off each step as you
finish it.

1. **Add your EMOTIV app keys** — open **Settings**, paste the Client ID and
   Secret from step 2.
2. **Connect Spotify** — in **Settings**, paste the Spotify Client ID from
   step 3, save, then press **Connect Spotify**. Your browser opens Spotify's
   sign-in page; approve access and come back. The app never sees your Spotify
   password.
3. **Connect your headset** — with EMOTIV Launcher running and the headset on,
   press **Start** and pick your headset. Check the head map: every sensor
   should be green. If it is not, the app tells you what to adjust.
4. **Train at least one command** — pick a profile or create one, then in
   **Training** record **Neutral** first (relaxed, thinking of nothing in
   particular), then a command such as **Push**. Keep a recording only if its
   score is good. A profile trained in EMOTIV's own BCI app works here too, as
   long as it was trained on the same headset model.

Then start playing something in Spotify — on this computer, your phone or a
speaker — and it appears under **Now playing**.

---

## 🧠 Using it

**What your mind controls** lists every trained command with three columns:

| Thought | Strength | Does |
|---|---|---|
| The command, in its colour | How strongly it is detected right now, against the **trigger line** | What it does — choose from the dropdown |

To make a command happen, hold the thought past the line. A thin bar fills
underneath as you hold it; when it is full, the action fires, the row flashes,
and the last action is shown below the list.

Three rules keep one thought from doing five things:

- **Hold** — the thought must stay past the line for a moment (half a second by
  default). A brief flicker does nothing.
- **Pause after an action** — nothing else can fire for two seconds, so you can
  hear what happened.
- **Let go** — a command that fired has to drop below the line before it can
  fire again. Holding *Push* for ten seconds is one play/pause, not five.

All three are adjustable under **Fine-tune when a thought counts**, next to each
command's Cortex sensitivity.

**Mind control on/off**, top right of the card, stops commands from touching
the music without disconnecting anything — switch it off before taking the
headset off. Commands are also ignored automatically while you are recording a
training.

The player buttons under **Now playing** do the same thing by hand, which is a
quick way to check that Spotify works before your headset is on.

### Choosing where the music plays

**Play on**, under the player, lists every device Spotify can play on — this
computer, your phone, a speaker, a TV. Pick one and the music moves there;
if nothing was playing, it starts there.

The app remembers your choice. If a *play* thought fires while Spotify has no
active device, it starts the music on the device you picked last, so you do not
need to reach for your phone to get it going.

Only devices that have had Spotify open recently appear. If one is missing,
open Spotify on it, then open the list again. Devices Spotify marks as
restricted are listed but cannot be chosen — they do not accept commands from
apps.

### When something does not work

| You see | Do this |
|---|---|
| *No active Spotify device* | Choose a device under **Play on**, or start playing something in Spotify first. Spotify can only control a player that is running. |
| A device is missing from **Play on** | Open Spotify on that device, then open the list again. Some speaker models are never listed by Spotify at all. |
| *Spotify only allows controlling playback on Premium accounts* | The signed-in account is not Premium. |
| *Spotify refused this account* | Add that account's email under **User Management** in the Spotify dashboard. |
| *Spotify sign-in failed* | The Redirect URI in the dashboard must be exactly `http://127.0.0.1:43917/callback`. |
| *Another program is using port 43917* | Close whatever holds that port and try again. |
| Commands fire by accident | Improve signal quality first; then raise **Strength needed** or **Hold for**. |
| A command never fires | Retrain it and check the brain map: a command drawn close to Neutral is one the detector cannot tell apart from rest. Or lower **Strength needed**. |
| *EMOTIV Launcher has not approved this app yet* | Open the Launcher and accept the access request. |

Settings, keys, the Spotify session and your command choices are stored in
`~/.emotiv_mind_music/settings.json` (readable only by your user). Uninstalling
leaves it in place, so a reinstall picks up where you left off.

---

## 🛠 Running from source

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
.venv/bin/python app.py
```

Tests — the trigger rules, the Spotify sign-in and player against fake servers,
and translation parity:

```bash
python -m unittest discover -s tests -t .
node tests/check_i18n.js
```

The interface can be exercised in an ordinary browser with no Python, headset or
Spotify at all:

```bash
python -m http.server 8765
# http://127.0.0.1:8765/tests/ui_harness.html?scenario=playing
```

Scenarios: `first-run`, `fresh`, `playing`, `untrained`, `poor-signal`,
`training`, `settings`, `zh`.

| File | What it does |
|---|---|
| `app.py` | The window, and the API the page calls |
| `engine.py` | Wires Cortex, the trigger and Spotify together, each on its own thread |
| `controls.py` | When a detection counts as a command, and which action each command does |
| `cortex_client.py` | Cortex: headsets, profiles, training, quality — from Brain Light |
| `spotify/auth.py` | Spotify sign-in: PKCE in the browser, token refresh |
| `spotify/player.py` | Play, pause, skip, volume, shuffle, and what is playing |
| `ui/` | The interface, in English and Chinese |

Read `CLAUDE.md` before changing anything — it records the traps that already
cost time.

---

## 📦 Releasing

Builds come from [`.github/workflows/build.yml`](.github/workflows/build.yml) on
`macos-14` and `windows-latest`. Tests run first; a failure stops the build.

```bash
git tag v1.0.0
git push origin v1.0.0
```

The tag builds both platforms, creates the release and attaches the `.dmg` and
the `-setup.exe`. The tag minus its `v` is the version stamped into the Windows
installer. Running the workflow from the **Actions** tab builds the same way but
leaves the files as workflow artifacts instead of publishing.

**Open the release build by hand before announcing it.** The macOS check only
proves the bundle stays up, and a frozen window stays up too; Windows is checked
for shape only, because the runner cannot draw a WebView2 window.

To build locally:

```bash
pip install -r requirements.txt pyinstaller pillow
python packaging/make_icon.py
pyinstaller packaging/EmotivMindMusic.spec --noconfirm
iscc packaging/EmotivMindMusic.iss          # Windows installer, needs Inno Setup 6
```

Icons are generated from `assets/logo.png` by `packaging/make_icon.py`. Without
that file the build still works and uses PyInstaller's default icon.

---

Spotify is a trademark of Spotify AB. This app is not affiliated with or
endorsed by Spotify.
