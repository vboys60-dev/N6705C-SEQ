"""Frozen T0 contract for the offline analysis tab.

Pure constants, enums, dataclasses, TypedDicts, exceptions and Protocols.
Imports stdlib only. Prose and rationale: docs/analysis-contract.md.
Changing anything here is a contract change: reassign through T0.
"""

import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, NotRequired, Protocol, TypedDict

CONTRACT_VERSION = 1

# Feature flag: single source of truth. Delivered to JS via config()["analysis_enabled"].
# False: no analysis tab, analysis_enter always denied, server never started.
# The arbiter (fault, async closing, generation checks) runs regardless.
ANALYSIS_ENABLED = False
CONFIG_KEY_ANALYSIS = "analysis_enabled"
# --selftest flag delivered to JS (doc 1.6, 1.7). Not persisted to the SEQ config file.
CONFIG_KEY_SELFTEST = "selftest_mode"
# Exact key set of webgui config(); tests/test_webgui.py asserts against this.
CONFIG_KEYS = ("resources", "demo", "lang", CONFIG_KEY_ANALYSIS, CONFIG_KEY_SELFTEST)

# --selftest round trip (doc 1.7): webgui waits SELFTEST_REPORT_TIMEOUT_S for selftest_report;
# JS gives each step (Viewer ready, back to idle after leave) SELFTEST_STEP_TIMEOUT_MS.
SELFTEST_REPORT_TIMEOUT_S = 30.0
SELFTEST_STEP_TIMEOUT_MS = 10_000
# Selftest deny list (doc 1.7.3). Every public webgui.Api method sits in exactly one tuple
# (tests/test_selftest_api_classes.py). A NEW public method is denied by default until a human classifies it.
# Read-only: no instrument I/O, no native dialog, no state beyond the gate / selftest result / in-memory queue.
SELFTEST_JS_API_ALLOWED_READONLY = (
    "config", "strings", "poll", "selftest_report", "analysis_enter", "analysis_leave",
    "seq_meta", "seq_preview", "list_meta", "list_draft", "list_compile",
)
# Instrument work, native dialogs, or state outside the gate (config file, loaded plan/measured data).
SELFTEST_JS_API_DENIED = (
    "seq_run", "test_connection", "all_off", "list_run",              # instrument
    "analysis_open_path", "analysis_save_path",                        # native dialog + grant
    "seq_open_csv", "seq_save_csv", "list_open_json", "list_save_json",  # native dialog + file I/O
    "set_lang",                                                        # writes the SEQ config file
)


# --------------------------------------------------------------------------- arbiter

class State(str, Enum):
    IDLE = "idle"
    POWER = "power"
    ANALYSIS = "analysis"
    DRAINING = "draining"
    CLOSING = "closing"
    FAULT = "fault"


# Every legal transition. Anything else is a bug (gate must refuse, never force).
TRANSITIONS: dict[State, frozenset[State]] = {
    State.IDLE: frozenset({State.POWER, State.ANALYSIS, State.CLOSING}),
    State.POWER: frozenset({State.IDLE, State.FAULT}),  # power never goes to closing directly
    State.ANALYSIS: frozenset({State.DRAINING, State.CLOSING}),
    State.DRAINING: frozenset({State.IDLE, State.CLOSING, State.FAULT}),
    State.FAULT: frozenset({State.CLOSING}),
    State.CLOSING: frozenset(),  # terminal
}

# App.busy == state in BUSY_STATES ; App.powerAllowed == state in POWER_ALLOWED_STATES
BUSY_STATES = frozenset({State.POWER, State.DRAINING, State.CLOSING, State.FAULT})
POWER_ALLOWED_STATES = frozenset({State.IDLE})
ANALYSIS_ENTER_STATES = frozenset({State.IDLE})

DRAINING_TIMEOUT_S = 30.0         # from analysis_leave() accepted -> jobs stopped + resources closed
POWER_FINISH_TIMEOUT_S = 60.0     # from latest _finishing(token) (re-armable) -> successful _untimed(token)
                                  # or _release(token). Untimed: only run_resources / run_plan / run_functions
# _untimed(token) -> bool: atomic under the gate lock; True only if state is POWER and token == owner.
# False (timer already faulted, stale/released token) -> worker skips the main work and goes to cleanup.
# Main work is wrapped in try/finally whose finally calls _finishing(token) (re-arm on return or raise).
# Supersedes report section 5 "timed from stop/finish signal": timing starts at each _finishing call.
# T2 follow-up 2 (doc 1.3, pending user sign-off): run_resources / run_rows / run_plan / run_functions
# take keyword-only on_cleanup: Callable[[], None] | None = None, called (never raising) right before
# their own cleanup I/O; the worker passes lambda: self._finishing(token). None = old behaviour.
CLOSING_CLEANUP_TIMEOUT_S = 10.0  # from background cleanup start -> server shut down; then os._exit
CANCEL_LATENCY_S = 2.0            # cancel flag set -> long job raises AnalysisCancelled (10M x 4ch)
GRANT_TTL_S = 300.0               # path grant lifetime after the dialog returns

ReserveKind = Literal["power"]  # only kind in v1; anything else -> ValueError


@dataclass(frozen=True)
class Token:
    """Returned by _reserve(kind). Owner check is by full equality (serial is process-unique)."""
    kind: str
    gen: int
    serial: int


@dataclass(frozen=True, eq=False)
class JobToken:
    """One registered analysis HTTP request. Identity equality."""
    gen: int
    serial: int
    req: str | None
    cancel: threading.Event = field(default_factory=threading.Event)


# poll() events
EVENT_BUSY = "busy"    # ["busy", bool]  kept for compatibility; equals state in BUSY_STATES
EVENT_STATE = "state"  # ["state", {"state": str, "gen": int, "seq": int}]  authoritative on conflict


class StatePayload(TypedDict):
    state: str
    gen: int
    seq: int  # transition counter: 0 for the initial idle event, +1 per accepted transition (in lock).
              # Frontend drops any state event with seq <= last applied seq.


class DenyCode(str, Enum):
    POWER = "power"        # power work (or its confirm dialog) holds the arbiter
    ANALYSIS = "analysis"  # power request while analysis tab holds the arbiter
    DRAINING = "draining"
    CLOSING = "closing"
    FAULT = "fault"
    DISABLED = "disabled"  # ANALYSIS_ENABLED is False
    STALE = "stale"        # generation mismatch
    SERVER = "server"      # analysis server failed to start
    BAD_PURPOSE = "bad_purpose"


# i18n key for the user-facing text of each deny code
DENY_TEXT_KEY: dict[DenyCode, str] = {
    DenyCode.POWER: "analysis_denied_power",
    DenyCode.ANALYSIS: "power_denied_analysis",
    DenyCode.DRAINING: "analysis_denied_draining",
    DenyCode.CLOSING: "analysis_denied_closing",
    DenyCode.FAULT: "fault_body",
    DenyCode.DISABLED: "analysis_disabled",
    DenyCode.STALE: "analysis_stale",
    DenyCode.SERVER: "analysis_server_failed",
    DenyCode.BAD_PURPOSE: "analysis_stale",
}


# --------------------------------------------------------------------------- js_api

JS_API_METHODS = ("analysis_enter", "analysis_leave", "analysis_open_path", "analysis_save_path")


class EnterResult(TypedDict):
    ok: bool
    error: NotRequired[str]  # localized text
    code: NotRequired[str]   # DenyCode value
    gen: NotRequired[int]
    url: NotRequired[str]    # "http://127.0.0.1:<port>/viewer.html#t=<token>&gen=<gen>&lang=<zh|en>"


class LeaveResult(TypedDict):
    ok: bool
    error: NotRequired[str]
    code: NotRequired[str]
    gen: NotRequired[int]


class PathResult(TypedDict):
    ok: bool
    error: NotRequired[str]
    code: NotRequired[str]
    gen: NotRequired[int]
    cancelled: NotRequired[bool]  # user closed dialog; ok is False, no error
    grant: NotRequired[str]       # opaque single-use id
    name: NotRequired[str]        # basename only, never the full path


class OpenPurpose(str, Enum):
    DLOG = "dlog"        # primary or compare .dlog
    MARKERS = "markers"  # marker JSON import


class SavePurpose(str, Enum):
    PNG = "png"
    HTML = "html"
    MARKERS = "markers"
    CSV = "csv"


SAVE_EXT: dict[SavePurpose, str] = {
    SavePurpose.PNG: ".png", SavePurpose.HTML: ".html",
    SavePurpose.MARKERS: ".json", SavePurpose.CSV: ".csv",
}
# pywebview file_types filters (English, like the existing CSV/JSON dialogs)
OPEN_FILE_TYPES: dict[OpenPurpose, tuple[str, ...]] = {
    OpenPurpose.DLOG: ("DLOG (*.dlog)", "All files (*.*)"),
    OpenPurpose.MARKERS: ("JSON (*.json)", "All files (*.*)"),
}
SAVE_FILE_TYPES: dict[SavePurpose, tuple[str, ...]] = {
    SavePurpose.PNG: ("PNG (*.png)",), SavePurpose.HTML: ("HTML (*.html)",),
    SavePurpose.MARKERS: ("JSON (*.json)",), SavePurpose.CSV: ("CSV (*.csv)",),
}
MAX_SUGGESTED_NAME = 200  # chars; analysis_save_path rejects longer / path separators


@dataclass(frozen=True)
class Grant:
    id: str          # secrets.token_urlsafe(16)
    gen: int
    mode: Literal["open", "save"]
    purpose: str     # OpenPurpose / SavePurpose value
    path: str
    expires: float   # time.monotonic() deadline


MAX_GRANTS_PER_GEN = 16  # oldest dropped when exceeded


# --------------------------------------------------------------------------- HTTP

HOST = "127.0.0.1"  # bind address; Host header must equal f"{HOST}:{port}" exactly
HEADER_TOKEN = "X-Analysis-Token"
HEADER_GEN = "X-Analysis-Gen"
HEADER_REQ = "X-Analysis-Req"      # client request id, [A-Za-z0-9_-]{1,64}
HEADER_GRANT = "X-Analysis-Grant"  # only on POST /save (raw body)
HEADER_PURPOSE = "X-Analysis-Purpose"  # only on POST /save; one of UPLOAD_PURPOSES
REQ_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

STATIC_FILES = ("viewer.html",)         # served from analysis/web/, Host check only
DEV_STATIC_FILES = ("shell_stub.html",)  # only when start_server(dev=True)

class RouteKind(str, Enum):
    PUBLIC = "public"    # Host check only
    JOB = "job"          # token + gen + gate.job_begin; counts toward MAX_CONCURRENT_JOBS
    CONTROL = "control"  # token + gen checked via gate.state(); no job, never 503; must not block


ROUTES: dict[tuple[str, str], RouteKind] = {
    ("GET", "/health"): RouteKind.PUBLIC,
    ("POST", "/open"): RouteKind.JOB,
    ("POST", "/release"): RouteKind.CONTROL,
    ("GET", "/samples"): RouteKind.JOB,
    ("GET", "/buckets"): RouteKind.JOB,
    ("GET", "/stats"): RouteKind.JOB,
    ("POST", "/export/csv"): RouteKind.JOB,
    ("POST", "/save"): RouteKind.JOB,
    ("POST", "/import/markers"): RouteKind.JOB,
    ("GET", "/prefs"): RouteKind.JOB,
    ("POST", "/prefs"): RouteKind.JOB,
    ("POST", "/cancel"): RouteKind.CONTROL,
    ("GET", "/progress"): RouteKind.CONTROL,  # ?req=<X-Analysis-Req of an in-flight /open>
}

# GET /progress (doc 3.3): Viewer polls while its /open is in flight. Only /open registers progress,
# keyed by (gen, X-Analysis-Req); removed when /open ends. Unknown/finished req -> 200 {ok: false}.
PROGRESS_STAGES = ("hash", "parse", "lod")  # = core progress(stage, pct) stages, in order
PROGRESS_POLL_MS = 300


class ProgressResult(TypedDict):
    ok: bool                 # False: req not (or no longer) an in-flight /open of this gen
    stage: NotRequired[str]  # PROGRESS_STAGES value; present when ok
    pct: NotRequired[int]    # 0..100 within that stage (int, clamped); present when ok

# path -> (grant mode, allowed purposes). /save takes its purpose from HEADER_PURPOSE;
# the others have exactly one purpose. Passed verbatim to gate.consume_grant.
UPLOAD_PURPOSES = ("png", "html", "markers")
ROUTE_GRANT: dict[str, tuple[str, tuple[str, ...]]] = {
    "/open": ("open", ("dlog",)),
    "/import/markers": ("open", ("markers",)),
    "/save": ("save", UPLOAD_PURPOSES),
    "/export/csv": ("save", ("csv",)),
}


class ErrorCode(str, Enum):
    BAD_REQUEST = "bad_request"
    HOST = "host"
    TOKEN = "token"
    GRANT = "grant"
    NOT_FOUND = "not_found"
    HANDLE = "handle"
    METHOD = "method"
    STALE_GEN = "stale_gen"
    CANCELLED = "cancelled"
    TOO_LARGE = "too_large"
    FORMAT = "format"
    LOCKED = "locked"
    OVERLOADED = "overloaded"
    SOURCE_CHANGED = "source_changed"
    INTERNAL = "internal"
    STORAGE = "storage"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.HOST: 403, ErrorCode.TOKEN: 403, ErrorCode.GRANT: 403,
    ErrorCode.NOT_FOUND: 404, ErrorCode.HANDLE: 404,
    ErrorCode.METHOD: 405,
    ErrorCode.STALE_GEN: 409, ErrorCode.CANCELLED: 409, ErrorCode.SOURCE_CHANGED: 409,
    ErrorCode.TOO_LARGE: 413,
    ErrorCode.FORMAT: 422,
    ErrorCode.LOCKED: 423,
    ErrorCode.INTERNAL: 500,
    ErrorCode.OVERLOADED: 503,
    ErrorCode.STORAGE: 507,
}


class ErrorBody(TypedDict):
    error: str  # human text (may be zh); viewer shows it verbatim
    code: str   # ErrorCode value


# --------------------------------------------------------------------------- limits

DEFAULT_BUCKETS = 1800
MAX_BUCKETS = 8192
MAX_KEYS = 12                         # 4 channels x V/I/P (query endpoints)
MAX_CSV_KEYS = 24                     # CSV export: main + compare file, 2 x MAX_KEYS
MAX_SAMPLES_SPAN = 2_000_000          # hi - lo + 1 per /samples request
MAX_RESPONSE_BYTES = 64 * 1024 ** 2   # any non-streamed response body
SAMPLE_BYTES = 4                      # every binary payload value is float32 LE (P too)
META_RESERVE_BYTES = 64 * 1024        # upper bound for 4-byte length prefix + padded JSON meta


def samples_response_bytes(span: int, n_keys: int) -> int:
    """Worst-case /samples body size. Viewer and server use this same formula."""
    return META_RESERVE_BYTES + span * n_keys * SAMPLE_BYTES


def use_buckets(span: int, n_keys: int) -> bool:
    """Viewer: True -> call /buckets instead of /samples. Server returns 413 for the same inputs."""
    return span > MAX_SAMPLES_SPAN or samples_response_bytes(span, n_keys) > MAX_RESPONSE_BYTES

MAX_JSON_BODY_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = 64 * 1024 ** 2     # POST /save raw body (png/html/markers)
MAX_MARKERS_FILE_BYTES = 4 * 1024 ** 2
MAX_PREFS_BYTES = 64 * 1024
MAX_CONCURRENT_JOBS = 4               # JOB routes only; beyond -> 503. Below the 6-per-host browser
                                      # connection limit so CONTROL requests (/cancel) still get a socket

CORE_CHUNK_RECORDS = 1_000_000        # cancel check + materialization granularity in core
CORE_MAX_VALUES_SPAN = 4_000_000      # get_values() hard cap; larger -> LimitExceeded
HASH_CHUNK_BYTES = 8 * 1024 ** 2
CSV_ROWS_PER_CHUNK = 20_000           # iter_csv block size (= legacy _CSV_ROWS_PER_CHUNK); cancel check per block

# capacity / performance (numbers marked "pending user" in the doc)
TARGET_MAX_RECORDS = 10_000_000       # 10M x 4ch must meet every budget below
NO_OOM_RECORDS = 30_000_000           # 30M x 4ch: cold open + full-range stats/buckets without OOM
# Memory acceptance = peak RSS (primary, report section 7 T3) AND tracemalloc peak (secondary).
# Measured in a fresh child process per step. RSS includes memmap pages, so the RSS budget is
# 2 x raw V/I bytes (source memmap + cache memmap) + heap budget + 256 MiB interpreter/numpy base,
# rounded up: 10M -> 2*305+256+256=1122 -> 1536 ; 30M -> 2*916+256+256=2344 -> 4096 (= MIN_RAM_GB/2).
MEM_BUDGET_PEAK_RSS_MB = 1536         # peak RSS: 10M x 4ch cold open; 10M full-range CSV
NO_OOM_PEAK_RSS_MB = 4096             # peak RSS: every 30M x 4ch step (cold open, full stats, full buckets)
MEM_BUDGET_PEAK_HEAP_MB = 256         # tracemalloc peak: cold open (10M, 30M) and full CSV (10M)
OPEN_COLD_MAX_S = 60.0                # 10M x 4ch, cache miss
OPEN_WARM_MAX_S = 3.0                 # 10M x 4ch, cache hit
GIL_STALL_MAX_MS = 500                # max probe-thread gap during cold open of 10M
WORKER_SWITCH_HEAP_MB = 1024          # tracemalloc peak exceeded at 30M -> replan to spawn worker
                                      # (also: 30M peak RSS > NO_OOM_PEAK_RSS_MB, MemoryError, killed)
MIN_RAM_GB = 8
CACHE_MAX_BYTES = 5 * 1024 ** 3       # LRU eviction above this

# --------------------------------------------------------------------------- storage

APP_DIR_NAME = "N6705C-SEQ"
CACHE_SUBDIR = "analysis-cache"
PREFS_FILENAME = "analysis_prefs.json"
PREFS_VERSION = 1
PREFS_DEFAULT = {"version": PREFS_VERSION, "theme": "dark", "fold": {}}
PART_SUFFIX = ".part"  # atomic_write_*: tmp = f"{target}.{secrets.token_hex(4)}{PART_SUFFIX}" in the
                       # target's dir, written under a per-target (resolved path) process lock

# --------------------------------------------------------------------------- parity

ORIGINAL_HTML_SHA1 = "0117e08d330192116d39ca2e58c8f600a00d1aff"  # parity_tools/original_before_python.html
PARITY_CASE_FILES = ("cases_default.json", "cases_74445.json")
STATS_AVG_RTOL = 1e-9  # vs float64 numpy reference; min/max exact

# --------------------------------------------------------------------------- postMessage

MSG_VERSION = 1
PARENT_ORIGIN_PATTERN = r"^http://(127\.0\.0\.1|localhost):\d{1,5}$"


class Msg(str, Enum):
    READY = "ready"                # iframe -> shell
    OPEN_REQUEST = "open_request"  # iframe -> shell
    SAVE_REQUEST = "save_request"  # iframe -> shell
    OPEN_RESULT = "open_result"    # shell -> iframe
    SAVE_RESULT = "save_result"    # shell -> iframe
    LANG = "lang"                  # shell -> iframe


# Required fields per message type, besides v/type/gen. Receiver rejects unknown types, types
# from the wrong direction, and missing/ill-typed required fields. Only *_result is matched by id.
MSG_FIELDS: dict[Msg, frozenset[str]] = {
    Msg.READY: frozenset(),
    Msg.OPEN_REQUEST: frozenset({"id", "purpose"}),
    Msg.SAVE_REQUEST: frozenset({"id", "purpose", "suggested"}),
    Msg.OPEN_RESULT: frozenset({"id", "ok"}),
    Msg.SAVE_RESULT: frozenset({"id", "ok"}),
    Msg.LANG: frozenset({"lang"}),
}
MSG_TO_SHELL = frozenset({Msg.READY, Msg.OPEN_REQUEST, Msg.SAVE_REQUEST})
MSG_TO_IFRAME = frozenset({Msg.OPEN_RESULT, Msg.SAVE_RESULT, Msg.LANG})
LANGS = ("zh", "en")


class MsgBase(TypedDict):
    v: int     # MSG_VERSION
    type: str  # Msg value
    gen: int


class OpenRequest(MsgBase):
    id: str       # REQ_ID_PATTERN, unique per iframe load
    purpose: str  # OpenPurpose value


class SaveRequest(MsgBase):
    id: str
    purpose: str    # SavePurpose value
    suggested: str  # file name only


class PathReply(MsgBase):  # OPEN_RESULT / SAVE_RESULT; mirrors PathResult
    id: str
    ok: bool
    error: NotRequired[str]
    code: NotRequired[str]
    cancelled: NotRequired[bool]
    grant: NotRequired[str]
    name: NotRequired[str]


class LangMsg(MsgBase):
    lang: str  # "zh" | "en"


# --------------------------------------------------------------------------- i18n

# T2 copies these verbatim into i18n.STRINGS (zh, en). T6 may only use keys listed here.
I18N_KEYS: dict[str, tuple[str, str]] = {
    "analysis_tab": ("離線分析報告", "Offline Analysis"),
    "analysis_denied_power": ("電源工作進行中（含確認對話框），不能切到分析。",
                              "Power work (or its confirmation) is in progress; cannot open analysis."),
    "power_denied_analysis": ("分析分頁使用中，不能執行電源工作；請先切到其他分頁。",
                              "Analysis tab is active; switch to another tab before power work."),
    "analysis_denied_draining": ("分析正在結束，請稍候再試。", "Analysis is shutting down; try again shortly."),
    "analysis_denied_closing": ("程式正在關閉。", "The application is closing."),
    "analysis_disabled": ("此版本未啟用分析功能。", "Analysis is not enabled in this build."),
    "analysis_stale": ("分析工作階段已過期，請重新操作。", "Analysis session expired; please retry."),
    "analysis_server_failed": ("分析服務啟動失敗：{error}", "Analysis service failed to start: {error}"),
    "analysis_crash_note": ("分析與控制程式在同一進程；若程式當掉，儀器輸出可能維持開啟，請手動確認並關閉輸出。",
                            "Analysis runs in the same process; if the app crashes, outputs may stay on. Check and turn them off manually."),
    "fault_title": ("工作卡住", "Work Stuck"),
    "fault_body": ("儀器或分析工作未在時限內結束，已停止接受新工作。輸出可能仍開著，請手動確認後關閉程式重開。",
                   "Instrument or analysis work did not finish in time; new work is blocked. Outputs may be on: check them, then close and restart."),
    "closing_wait": ("正在結束背景工作，視窗稍後關閉。", "Finishing background work; the window will close shortly."),
}


# --------------------------------------------------------------------------- exceptions

class AnalysisCancelled(Exception):
    """Raised by core/server when JobToken.cancel is set."""


class LimitExceeded(ValueError):
    """Request or core call exceeds a limit above (-> 413)."""


class SourceChanged(OSError):
    """Source .dlog size/mtime changed during hash or parse (-> 409 source_changed)."""


class CacheWriteError(OSError):
    """Cache dir not writable / disk full (-> 507 storage)."""


# --------------------------------------------------------------------------- protocols

class CancelFlag(Protocol):
    def is_set(self) -> bool: ...


class GateForServer(Protocol):
    """What analysis.server may call on analysis.gate (implemented by T2)."""

    def job_begin(self, gen: int, req: str | None = None) -> JobToken | None:
        """Atomic check+register. None: state != analysis -> 423, or gen mismatch -> 409.
        Server distinguishes via state()."""

    def job_end(self, job: JobToken) -> None:
        """Idempotent; only removes this exact job."""

    def cancel_req(self, gen: int, req: str) -> bool:
        """Set cancel on the job with this req id in this gen. Called from the CONTROL route
        /cancel without job_begin; must never block on job work."""

    def consume_grant(self, grant: str, gen: int, mode: str, purpose: str) -> str | None:
        """Single-use: removed on first call whatever the outcome. Returns path or None.
        mode/purpose come from ROUTE_GRANT[path]; for /save purpose = HEADER_PURPOSE value
        (server rejects a value outside UPLOAD_PURPOSES with 400 before calling)."""

    def state(self) -> tuple[State, int]: ...


class ServerHandle(Protocol):
    """Returned by analysis.server.start_server (implemented by T4)."""
    origin: str  # "http://127.0.0.1:<port>"
    token: str   # secrets.token_urlsafe(32), per process start

    def url(self, gen: int, lang: str) -> str: ...

    def release_resources(self) -> bool:
        """Close memmaps/_open_cache/NpzFile. False if something is still referenced (do not force)."""

    def self_check(self) -> bool:
        """GET /health over real HTTP; used by --selftest."""

    def shutdown(self, timeout: float = CLOSING_CLEANUP_TIMEOUT_S) -> None: ...


class StartServer(Protocol):
    def __call__(self, gate: GateForServer, *, cache_root: Path | None = None,
                 prefs_path: Path | None = None, dev: bool = False) -> ServerHandle: ...


# --------------------------------------------------------------------------- core data shapes
# Mirrors 報告分析升級版/來源_DLOG_Viewer/server/dlog_core.py + dlog_server.py.
# Units: t seconds, V volts, I amps, P watts. Channels are 1..4.

Kind = Literal["V", "I", "P"]
KEY_PATTERN = r"^[VIP][1-4]$"  # "V1", "I3", "P2"
HANDLE_PATTERN = r"^[0-9a-f]{40}$"  # h / hc: sha1 hex; mismatch -> 400, well-formed but unknown -> 404 handle
# Exceptions: /release with an unknown handle -> 200 {ok:true}; optional hc absent -> not validated


class DlogMeta(TypedDict):
    """<cache>/<sha1>/meta.json (written by open_dlog)."""
    tint: float                      # sampling period, s (header <tint>)
    declaredTime: float | None       # header <time>, s; None if absent
    nRecords: int                    # trailer (pre-trigger) records + main records
    channelModels: list[str | None]  # length 4, index ch-1: <model>, else "CH<n>" if active, else None
    activeChannels: list[int]        # sorted, subset of 1..4
    fields: list[list]               # [[ch:int, "V"|"I"], ...] in file order (JSON arrays)
    minmax: bool                     # header <sense_minmax>1; record has 3 floats/field, only [0] used
    cacheVersion: int                # T3+: CACHE_VERSION; absent in T1 legacy


CACHE_VERSION = 1


class LodLayoutItem(TypedDict):
    key: str                      # KEY_PATTERN
    level: int                    # 0 (factor LOD_F0=64) | 1 (factor LOD_F1=4096)
    which: Literal["min", "max"]
    offset: int                   # byte offset in the LOD blob
    length: int                   # float32 count: level0 ceil(n/64) (>=1), level1 ceil(len0/64) (>=1)


class OpenMeta(DlogMeta):
    """/open binary meta and open_dlog() return value. Blob order: activeChannels asc x V,I,P x
    level 0,1 x min,max; each float32 LE."""
    handle: str                   # sha1 hex (40 chars) of the source bytes
    lodFactors: dict[str, int]    # {"level0": 64, "level1": 4096}
    lodLayout: list[LodLayoutItem]
    fileName: str                 # basename of the opened path


class PayloadLayoutItem(TypedDict):
    key: str     # KEY_PATTERN
    offset: int  # byte offset in payload
    length: int  # float32 count (per series; buckets: per t/min/max segment)


class SamplesMeta(TypedDict):
    """/samples: payload per key = values[lo..hi] (P computed in float64, sent as float32)."""
    lo: int   # clamped to [0, nRecords-1]
    hi: int
    layout: list[PayloadLayoutItem]


class BucketsMeta(TypedDict):
    """/buckets: per key three contiguous segments t[length], min[length], max[length];
    offset points at t. t = bucket mid-index x tint (s). length <= n."""
    layout: list[PayloadLayoutItem]


class StatsValue(TypedDict):
    min: float | None  # None when no finite value in range
    max: float | None
    avg: float | None


# Query / body parameters shared by /samples /buckets /stats /export/csv
QS_TRUE = ("1", "true", "True")  # offset=... ; anything else (or missing) -> False
DEFAULT_MAX_LIMIT = 0.0          # maxLimit, amps; <= 0 -> negative currents become 0


class ExportCsvBody(TypedDict):
    grant: str
    h: str                       # primary handle
    hc: NotRequired[str]         # compare handle; required if cols has a "c:" item
    lo: int                      # clamped to [0, nRecords-1] of primary
    hi: int
    cols: str                    # "p:V1,c:I2" ; p=primary c=compare ; output order = this order
    offset: NotRequired[bool]    # default False
    maxLimit: NotRequired[float]  # default DEFAULT_MAX_LIMIT


class Entry(TypedDict):
    """Core handle from get_handle/acquire_handle. Arrays typed Any (contracts has no numpy)."""
    meta: DlogMeta
    dir: Path                    # <cache_root>/<sha1>
    V: dict[int, Any]            # ch -> np.memmap float32 [nRecords] (read-only)
    I: dict[int, Any]            # ch -> np.memmap float32 [nRecords]; 0 where the field is not recorded
    lod: Any                     # mapping f"{key}_l{0|1}{min|max}" -> float32 array (NpzFile in legacy)
    digest: NotRequired[str]     # sha1 hex; required from T3 on (release_handle uses it)


class CsvColumn(TypedDict):
    entry: Entry                 # primary or compare Entry (identity decides "is primary")
    kind: Kind
    ch: int                      # 1..4
    label: str                   # CSV_LABEL_FMT
    missing: NotRequired[bool]   # default False; True -> whole column empty (field not recorded;
                                 # P needs both V and I)


CSV_TITLE_LINE = "14585A Exported Waveform"
CSV_LABEL_FMT = "{file} Instrument A Channel {ch} {word} Avg"  # file: "File1"(p) | "File2"(c)
CSV_KIND_WORD = {"V": "Voltage", "I": "Current", "P": "Power"}
# Lines: title, "Sampling Period:"+String(tint), "Date:"+ctime_like(now), "Time,"+labels; then rows
# "%.8f" time, "%.9f" values, "" for NaN/missing. CRLF, UTF-8 without BOM.
# Parity vs JS toFixed is NOT byte-exact: non-numeric text exact; each number has the same decimals
# and may differ by CSV_LAST_DIGIT_TOL in the last decimal (JS rounds binary halves up, Python %f
# rounds half to even). Compare as decimal.Decimal scaled to the last digit.
CSV_LAST_DIGIT_TOL = 1


class CoreApi(Protocol):
    """Final (post-T3) analysis.core surface. T1 ships the legacy bytes form of open_dlog."""

    def open_dlog(self, path: str | Path, cache_root: Path, *, cancel: CancelFlag | None = None,
                  progress: Callable[[str, float], None] | None = None) -> tuple[OpenMeta, bytes]:
        """(meta, lod_blob). meta has handle (sha1 hex), lodFactors, lodLayout, fileName.
        = open_handle then release_handle: returns holding NO reference (doc 6.5). Not for server."""

    def open_handle(self, path: str | Path, cache_root: Path, *, cancel: CancelFlag | None = None,
                    progress: Callable[[str, float], None] | None = None
                    ) -> tuple[OpenMeta, bytes, Entry]:
        """Like open_dlog, but returns holding one reference on Entry (taken under the cache lock
        together with cache validation); ownership passes to the caller on return. On any exception
        core releases it first. Server /open adopts this same reference (doc 6.5)."""

    def acquire_handle(self, digest: str, cache_root: Path) -> Entry:
        """Refcount +1; KeyError if unknown."""

    def release_handle(self, entry: Entry) -> None: ...

    def close_all(self) -> bool:
        """Close every unreferenced mapping; False if any still referenced."""

    def get_values(self, entry: Entry, kind: str, ch: int, lo: int, hi: int,
                   offset: bool, max_limit: float, *, cancel: CancelFlag | None = None): ...

    def compute_buckets(self, entry: Entry, kind: str, ch: int, idx_lo: int, idx_hi: int,
                        n_buckets: int, offset: bool, max_limit: float, *,
                        cancel: CancelFlag | None = None) -> tuple: ...

    def compute_stats(self, entry: Entry, kind: str, ch: int, idx_lo: int, idx_hi: int,
                      offset: bool, max_limit: float, *, cancel: CancelFlag | None = None) -> StatsValue: ...

    def iter_csv(self, primary: Entry, columns: list[CsvColumn], idx_lo: int, idx_hi: int,
                 offset: bool, max_limit: float, now, *,
                 cancel: CancelFlag | None = None) -> Iterator[bytes]: ...
