"""Keep shell styles themeable outside the frozen token definitions."""

import re
from pathlib import Path

import pytest


WEB = Path(__file__).resolve().parents[1] / "src/n6705c_seq/web"
CSS_FILES = ("style.css", "seq.css", "list.css", "record.css")


@pytest.mark.parametrize("name", CSS_FILES)
def test_shell_css_has_no_hardcoded_colors(name):
    css = (WEB / name).read_text(encoding="utf-8")
    if name == "style.css":
        begin, end = "/* theme-tokens:begin */", "/* theme-tokens:end */"
        assert css.count(begin) == css.count(end) == 1
        before, tokens = css.split(begin)
        _, after = tokens.split(end)
        css = before + after
    assert not re.search(r"#[0-9a-f]{3,6}", css, re.IGNORECASE), name
    assert not re.search(r":\s*(?:white|black)\b|(?:rgb|hsl)a?\(", css, re.IGNORECASE), name


@pytest.mark.parametrize("name", CSS_FILES)
def test_shell_css_has_no_cjk(name):
    css = (WEB / name).read_text(encoding="utf-8")
    assert not re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", css), name
