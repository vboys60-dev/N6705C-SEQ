"""Every public js_api method is classified for --selftest (contract 1.7.3)."""

from n6705c_seq import webgui
from n6705c_seq.analysis import contracts


def test_every_public_js_api_method_is_classified():
    public = {name for name in dir(webgui.Api) if not name.startswith("_") and callable(getattr(webgui.Api, name))}
    allowed, denied = set(contracts.SELFTEST_JS_API_ALLOWED_READONLY), set(contracts.SELFTEST_JS_API_DENIED)
    assert not allowed & denied
    assert public - allowed - denied == set(), "classify new js_api methods in contracts.py (default: DENIED)"
    assert (allowed | denied) - public == set(), "stale names in the selftest classification"
