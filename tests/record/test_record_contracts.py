"""Live recording boundary, translation coverage and checked DLOG assumptions."""

import ast
from pathlib import Path
import re
import struct
from string import Formatter
import sys

import numpy as np
import pytest

from n6705c_seq import i18n
from n6705c_seq.analysis import contracts as analysis
from n6705c_seq.analysis.core import DlogFormatError, parse_dlog
from n6705c_seq.record import contracts as record

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/analysis/fixtures/default_20260803_092740.dlog"


def test_record_backend_defaults_and_tail_constant():
    assert record.RECORD_ENABLED is False
    assert record.RECORD_BACKEND in {"elog", "dlog"}
    assert record.RECORD_BACKEND == "dlog"
    assert record.ELOG_TAIL == "drain_then_abort"


def test_record_contracts_import_only_stdlib():
    path = Path(record.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in sys.stdlib_module_names
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "contracts must not import sibling packages"
            assert node.module.split(".")[0] in sys.stdlib_module_names
    assert path.with_name("__init__.py").read_bytes() == b""


def test_all_record_codes_have_distinct_existing_translations():
    assert {code.value for code in record.RecordCode} == {
        "disabled", "power", "analysis", "draining", "fault", "closing",
        "bad_config", "connect", "elog_unsupported", "fetch", "sink", "stuck",
        "no_recording", "stale",
    }
    assert set(record.CODE_I18N_KEY) == set(record.RecordCode)
    assert len(set(record.CODE_I18N_KEY.values())) == len(record.RecordCode)
    for key in record.CODE_I18N_KEY.values():
        assert key in record.I18N_KEYS
        assert key in i18n.STRINGS


def _placeholders(text):
    return {name for _, name, _, _ in Formatter().parse(text) if name is not None}


@pytest.mark.parametrize("key", sorted(record.I18N_KEYS))
def test_contract_i18n_keys_exist_with_matching_placeholders(key):
    assert key in i18n.STRINGS
    pair = i18n.STRINGS[key]
    assert isinstance(pair, tuple) and len(pair) == 2
    assert all(isinstance(text, str) and text for text in pair)
    assert _placeholders(pair[0]) == _placeholders(pair[1])
    values = {name: "detail" for name in _placeholders(pair[0])}
    for text in pair:
        text.format(**values)


def test_record_sources_keep_translations_in_i18n():
    assert isinstance(record.I18N_KEYS, frozenset)
    for path in Path(record.__file__).parent.glob("*.py"):
        assert not re.search(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]",
                             path.read_text(encoding="utf-8")), path


def test_slew_warning_keeps_rate_fields_in_both_languages():
    for text in i18n.STRINGS["slew_warning"]:
        assert _placeholders(text) == {"channel", "actual", "required"}
        text.format(channel="Frame 1:CH1", actual=2.0, required=500.0)


def test_open_push_is_unsolicited_and_only_to_iframe():
    assert analysis.Msg.OPEN_PUSH.value == "open_push"
    assert analysis.Msg.OPEN_PUSH in analysis.MSG_TO_IFRAME
    assert analysis.Msg.OPEN_PUSH not in analysis.MSG_TO_SHELL
    assert analysis.MSG_FIELDS[analysis.Msg.OPEN_PUSH] == {"grant", "name"}
    assert analysis.OpenPush.__required_keys__ == {"v", "type", "gen", "grant", "name"}


def _fixture_header():
    data = FIXTURE.read_bytes()
    end = data.index(b"</dlog>\n") + len(b"</dlog>\n")
    return data, end


def test_actual_dlog_fixture_structure():
    data, end = _fixture_header()
    assert end == 3968
    assert struct.unpack_from(">II", data, end) == (0, 81380)
    assert len(data) - end - 8 == 81380 * 8 * 4
    parsed = parse_dlog(data)
    assert parsed["nRecords"] == 81380
    assert parsed["fields"] == [(ch, kind) for ch in range(1, 5) for kind in ("V", "I")]
    assert parsed["minmax"] is False


@pytest.mark.parametrize("write,indices", [(3, [0, 1, 2]), (0, [0, 1, 2]), (1, [1, 2, 0])])
def test_viewer_trigger_zero_index_order_with_fixture_header(write, indices):
    data, end = _fixture_header()
    header = re.sub(rb"<time>[^<]+</time>",
                    ("<time>" + f"{0.003:024.9f}" + "</time>").encode(), data[:end])
    rows = np.arange(24, dtype=np.float32).reshape(3, 8)
    result = parse_dlog(header + struct.pack(">II", 0, write) + rows.astype(">f4").tobytes())
    assert result["nRecords"] == 3
    assert result["declaredTime"] == 0.003
    for col, (ch, kind) in enumerate(result["fields"]):
        np.testing.assert_array_equal(result[kind][ch], rows[indices, col])


def test_viewer_rejects_zero_record_part():
    data, end = _fixture_header()
    with pytest.raises(DlogFormatError, match="No records"):
        parse_dlog(data[:end] + struct.pack(">II", 0, 0))
