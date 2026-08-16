"""Which buttons a machine gets, and when it gets them.

Two separate questions, and the split matters. *Whether* a control exists is
Bambuddy's to answer — it is read off the instance's own OpenAPI document, so a
build that cannot pause a print has no Pause button rather than a broken one.
*When* it should be pressable is this shop's to answer, and it comes down to
what the machine is doing: Resume on a machine that is already printing is a
question with no answer, and drawing it live is how an operator learns to
ignore what the screen says.

The rule for a state nobody recognises is to allow rather than to block. Some
builds report no live state at all, and a farm screen whose every button is
greyed out because PrintFlow could not read a status word is a screen that has
taken the controls away over its own uncertainty. Offline is the exception:
that one PrintFlow does know, and a POST at a machine that is switched off can
only fail.
"""

from __future__ import annotations

from typing import Any

# Every state `farm.doing` can report for a machine that is switched on.
ONLINE = ("printing", "paused", "idle", "failed", "unknown")
# Nothing that moves the head or the filament belongs here mid-print.
FREE = ("idle", "failed")

# What PrintFlow knows about the controls it recognises: what to call the
# button, when it applies, whether it needs asking twice, and — when it does not
# apply — what to say instead of leaving a dead button unexplained.
#
# `group` decides how prominent it is. Only the three that act on the print in
# front of you are `primary` and earn a place on a card; everything else is
# `more` and lives behind one button, because a farm of ten machines showing
# five controls each is fifty buttons for a screen whose job is to tell you at a
# glance which machine to walk to.
#
# A control that is not in this table is not dropped. A build offering one
# PrintFlow has never heard of gets a button under its own name, available
# whenever the machine is on; that is the whole point of reading the spec rather
# than hardcoding three verbs. It goes under `more`: PrintFlow cannot know it is
# urgent, and guessing that it is would put it in front of Stop.
KNOWN: dict[str, dict[str, Any]] = {
    "pause": {
        "label": "Pause",
        "states": ("printing",),
        "why": "Nothing is printing",
        "group": "primary",
        "position": 1,
    },
    "resume": {
        "label": "Resume",
        "states": ("paused",),
        "why": "Nothing is paused",
        "group": "primary",
        "position": 2,
    },
    "stop": {
        "label": "Stop",
        "states": ("printing", "paused", "failed"),
        "why": "Nothing is running",
        # A stopped print cannot be un-stopped, and the button sits next to
        # Pause on a card the operator is reading quickly.
        "confirm": "Stop the print on {printer}? The plate will be lost.",
        "danger": True,
        "group": "primary",
        "position": 3,
    },
    "home": {
        "label": "Home",
        "states": FREE,
        "why": "Only while the machine is free",
        "position": 4,
    },
    "light": {
        "label": "Light",
        "states": ONLINE,
        "why": "The machine is offline",
        "position": 5,
    },
    "unload": {
        "label": "Unload filament",
        "states": FREE,
        "why": "Only while the machine is free",
        "position": 6,
    },
    "load": {
        "label": "Load filament",
        "states": FREE,
        "why": "Only while the machine is free",
        "position": 7,
    },
    "calibrate": {
        "label": "Calibrate",
        "states": FREE,
        "why": "Only while the machine is free",
        "position": 8,
    },
    "fan": {
        "label": "Fan",
        "states": ONLINE,
        "why": "The machine is offline",
        "position": 9,
    },
    "speed": {
        "label": "Speed",
        "states": ("printing",),
        "why": "Nothing is printing",
        "position": 10,
    },
}


def label_for(action: str) -> str:
    known = KNOWN.get(action)
    if known:
        return str(known["label"])
    return action.replace("_", " ").replace("-", " ").strip().capitalize()


def rule_for(action: str) -> dict[str, Any]:
    return KNOWN.get(action) or {
        "label": label_for(action),
        "states": ONLINE,
        "why": "The machine is offline",
        # Unrecognised, so PrintFlow cannot say what it does — which is reason
        # enough to ask before doing it.
        "confirm": "Send {action} to {printer}?",
        "group": "more",
        "position": 50,
    }


def allowed(action: str, state: str) -> bool:
    """Whether this control makes sense for a machine in this state.

    `unknown` allows everything a live machine allows: it means the build did
    not say, not that the machine is doing nothing, and PrintFlow refusing on a
    reading it never got would be an opinion dressed up as a fact.
    """
    if state == "offline":
        return False
    if state == "unknown":
        return True
    return state in rule_for(action)["states"]


def for_state(
    discovered: dict[str, str], state: str, *, printer: str = "this printer"
) -> list[dict[str, Any]]:
    """The buttons for one machine's card, in the order they should be drawn.

    Every control the instance offers appears, whether or not it applies right
    now — a Resume that vanishes while printing and reappears when paused is a
    row of buttons that moves under the cursor. Disabled, with the reason on it,
    is the version an operator can learn.
    """
    buttons: list[dict[str, Any]] = []
    for action in discovered:
        rule = rule_for(action)
        enabled = allowed(action, state)
        confirm = rule.get("confirm")
        buttons.append(
            {
                "action": action,
                "label": rule["label"],
                "group": rule.get("group", "more"),
                "enabled": enabled,
                "why": None if enabled
                else "The machine is offline" if state == "offline"
                else rule["why"],
                "danger": bool(rule.get("danger")),
                "confirm": confirm.format(printer=printer, action=rule["label"].lower())
                if confirm
                else None,
            }
        )
    buttons.sort(key=lambda row: (rule_for(row["action"])["position"], row["action"]))
    return buttons
