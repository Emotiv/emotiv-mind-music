"""The trigger rules in controls.py, driven at the real `com` rate of 8 Hz."""
import unittest

from controls import (
    DEFAULT_ORDER,
    PLAYER_ACTIONS,
    CommandTrigger,
    TriggerSettings,
    resolve_bindings,
)

RATE = 1 / 8  # seconds between `com` samples


def feed(trigger, samples, start=0.0, bound=True):
    """Feed (action, power) pairs at 8 Hz; return [(time, fired)] for each fire."""
    fired = []
    t = start
    for action, power in samples:
        result = trigger.update(action, power, bound=bound, now=t)
        if result:
            fired.append((round(t, 3), result))
        t += RATE
    return fired, t


def hold(action, power, seconds):
    return [(action, power)] * int(round(seconds / RATE))


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.trigger = CommandTrigger(TriggerSettings(threshold=0.6, hold=0.5, cooldown=2.0))

    def test_a_single_spike_does_nothing(self):
        fired, _ = feed(self.trigger, [("push", 0.95)] + hold("neutral", 0.0, 2))
        self.assertEqual(fired, [])

    def test_fires_once_the_hold_is_met(self):
        fired, _ = feed(self.trigger, hold("push", 0.8, 1.0))
        self.assertEqual([name for _, name in fired], ["push"])
        # Four samples cover 0.375 s of elapsed time; the fifth reaches 0.5 s.
        self.assertEqual(fired[0][0], 0.5)

    def test_below_threshold_never_fires(self):
        fired, _ = feed(self.trigger, hold("push", 0.59, 3.0))
        self.assertEqual(fired, [])

    def test_a_dip_restarts_the_hold(self):
        samples = hold("push", 0.8, 0.375) + [("push", 0.3)] + hold("push", 0.8, 0.375)
        fired, _ = feed(self.trigger, samples)
        self.assertEqual(fired, [])

    def test_switching_command_restarts_the_hold(self):
        samples = hold("push", 0.8, 0.375) + hold("pull", 0.8, 0.375)
        fired, _ = feed(self.trigger, samples)
        self.assertEqual(fired, [])

    def test_a_stalled_stream_breaks_the_hold(self):
        # Two samples, a 1 s dropout, two more: never four in a row.
        self.trigger.update("push", 0.9, now=0.0)
        self.trigger.update("push", 0.9, now=0.125)
        self.assertIsNone(self.trigger.update("push", 0.9, now=1.125))
        self.assertIsNone(self.trigger.update("push", 0.9, now=1.25))

    def test_neutral_never_fires(self):
        fired, _ = feed(self.trigger, hold("neutral", 1.0, 3.0))
        self.assertEqual(fired, [])

    def test_an_unbound_command_never_fires_or_blocks(self):
        fired, t = feed(self.trigger, hold("lift", 0.9, 1.0), bound=False)
        self.assertEqual(fired, [])
        # And it must not have started a cooldown that eats the next command.
        fired, _ = feed(self.trigger, hold("push", 0.9, 1.0), start=t)
        self.assertEqual([name for _, name in fired], ["push"])


class ReleaseAndCooldownTests(unittest.TestCase):
    def setUp(self):
        self.trigger = CommandTrigger(TriggerSettings(threshold=0.6, hold=0.5, cooldown=2.0))

    def test_holding_one_thought_is_one_action(self):
        # Ten seconds of unbroken push: the toggle must not flap.
        fired, _ = feed(self.trigger, hold("push", 0.9, 10.0))
        self.assertEqual(len(fired), 1)

    def test_release_then_hold_again_fires_again_after_cooldown(self):
        samples = hold("push", 0.9, 1.0) + hold("neutral", 0.0, 2.0) + hold("push", 0.9, 1.0)
        fired, _ = feed(self.trigger, samples)
        self.assertEqual([name for _, name in fired], ["push", "push"])

    def test_nothing_fires_inside_the_cooldown(self):
        # push fires at 0.5 s; pull held straight after must wait for 2.5 s.
        samples = hold("push", 0.9, 0.625) + hold("pull", 0.9, 3.0)
        fired, _ = feed(self.trigger, samples)
        self.assertEqual([name for _, name in fired], ["push", "pull"])
        self.assertGreaterEqual(fired[1][0] - fired[0][0], 2.0)

    def test_hold_during_cooldown_does_not_count(self):
        # pull is held throughout the cooldown. It must still need its own
        # half second *after* the cooldown ends, not fire the moment it does.
        samples = hold("push", 0.9, 0.625) + hold("pull", 0.9, 3.0)
        fired, _ = feed(self.trigger, samples)
        push_at, pull_at = fired[0][0], fired[1][0]
        self.assertGreaterEqual(pull_at, push_at + 2.0 + 0.5 - 1e-9)

    def test_zero_hold_fires_on_the_first_sample(self):
        trigger = CommandTrigger(TriggerSettings(threshold=0.6, hold=0.0, cooldown=0.0))
        self.assertEqual(trigger.update("push", 0.9, now=0.0), "push")


class StateTests(unittest.TestCase):
    def test_progress_climbs_to_one_then_latches(self):
        trigger = CommandTrigger(TriggerSettings(threshold=0.6, hold=0.5, cooldown=2.0))
        trigger.update("push", 0.9, now=0.0)
        trigger.update("push", 0.9, now=0.25)
        self.assertAlmostEqual(trigger.state("push", 0.9, now=0.25).hold_progress, 0.5)
        trigger.update("push", 0.9, now=0.5)
        state = trigger.state("push", 0.9, now=0.5)
        self.assertTrue(state.latched)
        self.assertTrue(state.cooling)
        self.assertEqual(state.hold_progress, 0.0)

    def test_settings_are_clamped(self):
        s = TriggerSettings.clamped(threshold=5, hold=-1, cooldown="nonsense")
        self.assertEqual(s.threshold, 1.0)
        self.assertEqual(s.hold, 0.0)
        self.assertEqual(s.cooldown, TriggerSettings().cooldown)


class BindingTests(unittest.TestCase):
    def test_defaults_fill_in_slot_order(self):
        self.assertEqual(
            resolve_bindings({}, ["neutral", "push", "pull", "lift"]),
            {"push": "play_pause", "pull": "next", "lift": "previous"},
        )

    def test_a_stored_choice_wins_including_none(self):
        result = resolve_bindings({"push": "none", "pull": "volume_up"}, ["push", "pull", "lift"])
        self.assertEqual(result["push"], "none")
        self.assertEqual(result["pull"], "volume_up")
        # lift takes the first default not already used by a stored choice.
        self.assertEqual(result["lift"], "play_pause")

    def test_defaults_run_out_to_none(self):
        commands = ["push", "pull", "lift", "drop", "left"]
        result = resolve_bindings({}, commands)
        self.assertEqual([result[c] for c in commands], DEFAULT_ORDER + ["none"])

    def test_unknown_stored_actions_are_ignored(self):
        result = resolve_bindings({"push": "self_destruct"}, ["push"])
        self.assertEqual(result, {"push": "play_pause"})

    def test_every_default_is_a_real_action(self):
        self.assertTrue(set(DEFAULT_ORDER) <= set(PLAYER_ACTIONS))


if __name__ == "__main__":
    unittest.main()
