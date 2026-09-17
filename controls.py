"""Which thought does what, and when a detection counts as meant.

Cortex streams `com` at 8 Hz: one (action, power) pair every 125 ms, where
power is 0 to 1. Acting on every sample above a threshold would be useless for
a music player — a single strong push would pause, resume, pause again and skip
three tracks in the time it takes to notice. So a command has to earn it:

  1. Hold.     The same command stays at or above the threshold for `hold`
               seconds without interruption. One stray sample does nothing.
  2. Cooldown. After anything fires, nothing else can for `cooldown` seconds,
               which is long enough to hear what happened.
  3. Release.  A command that fired must drop below the threshold (or give way
               to another command) before it can fire again. Holding the same
               thought for ten seconds is one play/pause, not five — which
               matters most for the toggle, where firing twice undoes itself.

Nothing in here talks to Cortex or Spotify, so all of it is tested directly.
"""
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

# What a command can be told to do. "none" is a real choice: a command the user
# trained for another app should be able to sit here doing nothing.
PLAYER_ACTIONS: List[str] = [
    "none",
    "play_pause",
    "play",
    "pause",
    "next",
    "previous",
    "volume_up",
    "volume_down",
    "shuffle",
]

# Offered to a profile's commands in slot order the first time they are seen,
# so a freshly trained profile works without a trip through the dropdowns.
# Play/pause first because it is the one everybody wants; next before previous
# because skipping forward is what people reach for.
DEFAULT_ORDER: List[str] = ["play_pause", "next", "previous", "volume_up"]

VOLUME_STEP = 10

# `com` is 8 Hz, so consecutive samples are 125 ms apart. A gap much longer than
# that means the stream stalled — a headset dropout, a Cortex hiccup — and a
# hold that spans it was never actually continuous.
MAX_SAMPLE_GAP = 0.5


@dataclass
class TriggerSettings:
    # 0.6 rather than the 0.7 the original academy example hard-coded: that
    # left freshly trained profiles, which rarely peak much above it, unable to
    # fire at all. Adjustable in the interface either way.
    threshold: float = 0.6
    # Four consecutive samples at 8 Hz.
    hold: float = 0.5
    cooldown: float = 2.0

    @classmethod
    def clamped(cls, threshold, hold, cooldown) -> "TriggerSettings":
        def clamp(value, lo, hi, fallback):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return fallback
            return lo if value < lo else hi if value > hi else value

        d = cls()
        return cls(
            threshold=clamp(threshold, 0.05, 1.0, d.threshold),
            hold=clamp(hold, 0.0, 3.0, d.hold),
            cooldown=clamp(cooldown, 0.0, 10.0, d.cooldown),
        )


@dataclass
class TriggerState:
    """What the interface draws for the live command."""
    action: str
    power: float
    hold_progress: float      # 0..1 towards firing
    cooling: bool             # inside the cooldown after a fire
    latched: bool             # fired, waiting for release

    def as_dict(self) -> Dict[str, object]:
        return {
            "action": self.action,
            "power": round(self.power, 3),
            "hold_progress": round(self.hold_progress, 3),
            "cooling": self.cooling,
            "latched": self.latched,
        }


class CommandTrigger:
    """Turns the 8 Hz `com` stream into deliberate, one-shot command events."""

    def __init__(self, config: Optional[TriggerSettings] = None,
                 clock: Callable[[], float] = None):
        import time
        self.config = config or TriggerSettings()
        self.clock = clock or time.monotonic
        self.reset()

    def reset(self):
        self._candidate: Optional[str] = None
        self._since = 0.0
        self._latched: Optional[str] = None
        self._cooldown_until = 0.0
        self._last_sample: Optional[float] = None

    def update(self, action: str, power: float, bound: bool = True,
               now: Optional[float] = None) -> Optional[str]:
        """Feed one sample. Returns the command name if it fires on this sample.

        `bound` is whether this command is mapped to anything. An unbound
        command is reported but can neither fire nor start a cooldown — it
        would otherwise swallow the next real command for two seconds.
        """
        now = self.clock() if now is None else now
        cfg = self.config

        if self._last_sample is not None and now - self._last_sample > MAX_SAMPLE_GAP:
            self._candidate = None
        self._last_sample = now

        above = bool(bound) and action not in ("", "neutral") and power >= cfg.threshold

        # Release: the latched command let go, or something else took over.
        if self._latched and not (above and action == self._latched):
            self._latched = None

        if not above:
            self._candidate = None
            return None

        if action != self._candidate:
            self._candidate = action
            self._since = now

        if action == self._latched or now < self._cooldown_until:
            return None

        # Only time held *after* the cooldown counts. Otherwise a thought
        # started during the pause fires the instant it ends, which reads as
        # the app acting on its own.
        started = max(self._since, self._cooldown_until)
        if now - started >= cfg.hold:
            self._latched = action
            self._cooldown_until = now + cfg.cooldown
            self._candidate = None
            return action
        return None

    def state(self, action: str, power: float, now: Optional[float] = None) -> TriggerState:
        now = self.clock() if now is None else now
        cfg = self.config
        progress = 0.0
        if (self._candidate == action and self._latched != action
                and now >= self._cooldown_until):
            started = max(self._since, self._cooldown_until)
            progress = 1.0 if cfg.hold <= 0 else min(1.0, (now - started) / cfg.hold)
        return TriggerState(
            action=action,
            power=max(0.0, min(1.0, power)),
            hold_progress=progress,
            cooling=now < self._cooldown_until,
            latched=self._latched == action,
        )


def resolve_bindings(stored: Optional[Dict[str, str]], commands: Iterable[str]) -> Dict[str, str]:
    """Bindings for every command, filling defaults for ones never seen.

    A stored choice always wins, including "none" — a user who switched a
    command off must not find it switched back on after reconnecting. A
    command that has no stored choice takes the first default nothing else is
    already using, and "none" once the defaults run out.
    """
    stored = {k: v for k, v in (stored or {}).items() if v in PLAYER_ACTIONS}
    commands = [c for c in commands if c and c != "neutral"]

    result: Dict[str, str] = {}
    used = {v for k, v in stored.items() if k in commands and v != "none"}
    for command in commands:
        if command in stored:
            result[command] = stored[command]
            continue
        choice = next((a for a in DEFAULT_ORDER if a not in used), "none")
        result[command] = choice
        if choice != "none":
            used.add(choice)
    return result
