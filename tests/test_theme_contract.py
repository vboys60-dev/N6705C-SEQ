"""Frozen A0 theme tokens and iframe message contract."""

import re
from pathlib import Path

from n6705c_seq import form
from n6705c_seq.analysis import contracts


STYLE = Path(__file__).resolve().parents[1] / "src/n6705c_seq/web/style.css"
SEMANTIC_TOKENS = {
    "--bg", "--surface", "--surface-hi", "--border", "--text", "--text-dim",
    "--accent", "--accent-text", "--danger", "--ok", "--warn",
    "--plot-bg", "--plot-grid", "--plot-axis", "--plot-text",
}


def theme_tokens():
    css = STYLE.read_text(encoding="utf-8")
    begin, end = "/* theme-tokens:begin */", "/* theme-tokens:end */"
    assert css.startswith(begin)
    assert css.count(begin) == css.count(end) == 1
    block = css[len(begin):css.index(end)]
    rules = re.findall(r'(:root(?:\[data-theme="dark"\])?)\s*\{([^{}]*)\}', block)
    assert [selector for selector, _ in rules] == [":root", ':root[data-theme="dark"]']
    result = []
    for _, body in rules:
        entries = re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body)
        assert len(entries) == len(dict(entries)), "duplicate token"
        result.append(dict(entries))
    return result


def test_theme_token_names_match():
    light, dark = theme_tokens()
    expected = SEMANTIC_TOKENS | {f"--ch{i}" for i in range(1, 9)}
    assert light.keys() == dark.keys() == expected


def test_theme_colors_are_lowercase_hex_except_dark_border():
    light, dark = theme_tokens()
    for tokens in (light, dark):
        for name, value in tokens.items():
            if tokens is dark and name == "--border":
                assert value == "rgba(255,255,255,.16)"
            else:
                assert re.fullmatch(r"#[0-9a-f]{6}", value), (name, value)


def test_palette_matches_dark_channel_tokens():
    _, dark = theme_tokens()
    assert form.PALETTE == [dark[f"--ch{i}"] for i in range(1, 9)]


def test_theme_message_is_registered():
    assert contracts.THEMES == ("light", "dark")
    assert contracts.Msg.THEME == "theme"
    assert contracts.MSG_FIELDS[contracts.Msg.THEME] == {"theme"}
    assert contracts.Msg.THEME in contracts.MSG_TO_IFRAME
    assert contracts.Msg.THEME not in contracts.MSG_TO_SHELL
