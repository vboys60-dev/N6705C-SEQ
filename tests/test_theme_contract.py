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


EXPECTED_LIGHT = {
    "--bg": "#f5f6f8",
    "--surface": "#ffffff",
    "--surface-hi": "#eef0f4",
    "--border": "#c9ced8",
    "--text": "#1b1f29",
    "--text-dim": "#4a5263",
    "--accent": "#0a5fc2",
    "--accent-text": "#ffffff",
    "--danger": "#b42318",
    "--ok": "#1e7f4f",
    "--warn": "#8a5a00",
    "--plot-bg": "#ffffff",
    "--plot-grid": "#d9d9d9",
    "--plot-axis": "#5c5c5c",
    "--plot-text": "#333333",
    "--ch1": "#7a6a00",
    "--ch2": "#0068a8",
    "--ch3": "#b4367f",
    "--ch4": "#00785a",
    "--ch5": "#9a5b00",
    "--ch6": "#6a4bc4",
    "--ch7": "#b8401a",
    "--ch8": "#4a7a12",
}
EXPECTED_DARK = {
    "--bg": "#1b1f29",
    "--surface": "#262b38",
    "--surface-hi": "#303645",
    "--border": "rgba(255,255,255,.16)",
    "--text": "#f2f4f8",
    "--text-dim": "#cdd3de",
    "--accent": "#8cc4ff",
    "--accent-text": "#0b1220",
    "--danger": "#ff8f87",
    "--ok": "#62dd98",
    "--warn": "#f7cd5f",
    "--plot-bg": "#000000",
    "--plot-grid": "#3a3a3a",
    "--plot-axis": "#8a8a8a",
    "--plot-text": "#d4d4d4",
    "--ch1": "#f0e442",
    "--ch2": "#56b4e9",
    "--ch3": "#e58fc4",
    "--ch4": "#3dd6a3",
    "--ch5": "#e69f00",
    "--ch6": "#b79cff",
    "--ch7": "#f4845f",
    "--ch8": "#a6d854",
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


def test_theme_token_values_are_frozen():
    light, dark = theme_tokens()
    assert light == EXPECTED_LIGHT
    assert dark == EXPECTED_DARK
