"""SCPI syntax helpers.

The N6705 Programmer's Reference Guide spells commands in the conventional
SCPI mixed case, where upper-case letters are the required short form and
lower-case letters are the optional long form::

    SYSTem:CHANnel:MODel?   matches  SYST:CHAN:MOD?  and  SYSTEM:CHANNEL:MODEL?

Square brackets mark optional nodes::

    [SOURce:]VOLTage[:LEVel]   matches  VOLT  and  SOUR:VOLT:LEV  and  ...

`scpi_regex` turns those spellings straight into a matcher, which lets the
fake instrument accept the same command forms a real one does without us
hand-writing a pattern per command.
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = ["scpi_regex", "parse_channel_list", "format_channel_list"]

_MNEMONIC = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _mnemonic_regex(word: str) -> str:
    """``SYSTem`` -> ``SYST(?:EM)?``; ``ARB`` -> ``ARB``."""
    required_len = 0
    for ch in word:
        if ch.islower():
            break
        required_len += 1
    required, optional = word[:required_len], word[required_len:]
    if not required:  # defensive: all-lowercase spelling
        return re.escape(word)
    if not optional:
        return re.escape(required)
    return f"{re.escape(required)}(?:{re.escape(optional)})?"


@lru_cache(maxsize=512)
def scpi_regex(spelling: str) -> re.Pattern[str]:
    """Compile a manual-style command spelling into a case-insensitive matcher.

    The returned pattern is anchored and matches the command header only; any
    argument text is left for the caller to handle.
    """
    out: list[str] = []
    i = 0
    while i < len(spelling):
        ch = spelling[i]
        if ch == "[":
            out.append("(?:")
            i += 1
        elif ch == "]":
            out.append(")?")
            i += 1
        elif ch == ":":
            out.append(":")
            i += 1
        elif ch == "?":
            out.append(r"\?")
            i += 1
        elif ch.isalpha():
            m = _MNEMONIC.match(spelling, i)
            assert m is not None
            out.append(_mnemonic_regex(m.group()))
            i = m.end()
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile(r"\s*:?" + "".join(out) + r"\s*$", re.IGNORECASE)


def parse_channel_list(text: str) -> list[int]:
    """Parse a SCPI channel list into channel numbers.

    Accepts the forms shown in the manual -- ``(@2)``, ``(@1,4)``, ``(@1:3)`` --
    with or without the surrounding parentheses, and any combination of
    comma-separated singles and ``a:b`` ranges.
    """
    body = text.strip()
    if body.startswith("("):
        body = body[1:]
    if body.endswith(")"):
        body = body[:-1]
    body = body.strip()
    if body.startswith("@"):
        body = body[1:]
    channels: list[int] = []
    for part in body.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            start_s, _, stop_s = part.partition(":")
            start, stop = int(start_s), int(stop_s)
            step = 1 if stop >= start else -1
            channels.extend(range(start, stop + step, step))
        else:
            channels.append(int(part))
    return channels


def format_channel_list(channels: object) -> str:
    """Render channel numbers as ``(@1,2,4)``.

    Ranges are not collapsed: the explicit form is accepted everywhere and
    keeps the emitted command easy to read in the command log.
    """
    if isinstance(channels, int):
        channels = [channels]
    return "(@" + ",".join(str(int(c)) for c in channels) + ")"  # type: ignore[union-attr]
