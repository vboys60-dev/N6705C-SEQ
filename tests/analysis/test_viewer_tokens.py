"""Keep the standalone viewer palette aligned with the frozen shell tokens."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "src/n6705c_seq"


def tokens(text):
    block = text.split("/* theme-tokens:begin */", 1)[1].split("/* theme-tokens:end */", 1)[0]
    rules = re.findall(r'(:root(?:\[data-theme="dark"\])?)\s*\{([^}]+)\}', block)
    return {selector: dict(re.findall(r"(--[\w-]+):\s*([^;]+);", body)) for selector, body in rules}


def test_viewer_tokens_match_shell_in_both_themes():
    viewer = tokens((WEB / "analysis/web/viewer.html").read_text(encoding="utf-8"))
    shell = tokens((WEB / "web/style.css").read_text(encoding="utf-8"))
    assert set(viewer) == set(shell) == {":root", ':root[data-theme="dark"]'}
    for selector in shell:
        # Compare all frozen tokens, including the plot and channel subsets.
        assert viewer[selector] == shell[selector]
        for name in ("--ch1", "--ch2", "--ch3", "--ch4", "--plot-bg", "--plot-grid", "--plot-axis", "--plot-text"):
            assert name in viewer[selector]


def test_viewer_css_has_no_cjk():
    html = (WEB / "analysis/web/viewer.html").read_text(encoding="utf-8")
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert not re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", css)
