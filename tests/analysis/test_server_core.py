"""T4 integration with the real core/storage; runs only when T3 is done:
N6705C_ANALYSIS_REAL_CORE=1 pytest tests/analysis/test_server_core.py"""
import json
import os
import struct

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("N6705C_ANALYSIS_REAL_CORE") != "1",
                                reason="set N6705C_ANALYSIS_REAL_CORE=1 once T3 core/storage are ready")


def test_open_samples_csv_with_real_core(tmp_path, default_dlog):
    from n6705c_seq.analysis.gate import Gate
    from n6705c_seq.analysis.server import start_server
    from test_http import Env  # same helper client

    class RealEnv(Env):
        def __init__(self, tmp):
            self.tmp, self.gate, self.gen = tmp, Gate(lambda e: None), None
            self.core = None
            self.srv = start_server(self.gate, cache_root=tmp / "cache", prefs_path=tmp / "p.json")
            self.port = int(self.srv.origin.rsplit(":", 1)[1])

    e = RealEnv(tmp_path)
    try:
        e.enter()
        st, _, b = e.req("POST", "/open", {"grant": e.grant("open", "dlog", default_dlog)})
        assert st == 200
        (n,) = struct.unpack("<I", b[:4])
        meta = json.loads(b[4:4 + n])
        assert meta["fileName"] == default_dlog.name
        h = meta["handle"]
        out = tmp_path / "o.csv"
        st, d = e.j("POST", "/export/csv", {"grant": e.grant("save", "csv", out), "h": h, "lo": 0,
                                            "hi": 2000, "cols": "p:V1,p:I1"})
        assert st == 200 and out.read_bytes().startswith(b"14585A Exported Waveform\r\n")
        assert e.j("POST", "/release", {"h": h}) == (200, {"ok": True})
        assert e.srv.release_resources() is True
    finally:
        e.srv.shutdown(2)
