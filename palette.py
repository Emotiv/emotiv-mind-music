"""Slot colours for mental commands.

Not invented: EmotivBCI assigns a colour per trained-action *slot*, not per
action name, and these are its values — extracted for emotiv-brain-light from
the app's own logs (`cmd: Push color: #2ec6c8`, `cmd: Neutral color: #ff0066`)
and its binary. Using the same ones means a command wears the same colour here
as in EMOTIV's own training app.
"""
from typing import Dict, Iterable

MENTAL_COMMAND_SLOT_COLORS = [
    "#2ec6c8", "#f2974e", "#a781f3", "#5ab0ee",
    # Beyond the four EmotivBCI slots, the EmotivPRO chart palette.
    "#e9cc40", "#50e17d", "#f75c46", "#bce92a",
    "#12b0da", "#de3d82", "#ffa037", "#ae72f9",
]

NEUTRAL_COLOR = "#ff0066"


def slot_colors(commands: Iterable[str]) -> Dict[str, str]:
    """Colour per command, in slot order, with neutral always its own pink."""
    colors = {"neutral": NEUTRAL_COLOR}
    slot = 0
    for command in commands:
        if command == "neutral" or command in colors:
            continue
        colors[command] = MENTAL_COMMAND_SLOT_COLORS[slot % len(MENTAL_COMMAND_SLOT_COLORS)]
        slot += 1
    return colors
