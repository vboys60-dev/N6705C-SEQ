"""Frozen live recording contracts; stdlib only. See docs/live-logging-contract.md.

No hardware, GUI, numpy or analysis imports. Protocol array arguments use Any
so the writer/backend implementations can use numpy without crossing this boundary.
"""

from enum import Enum
from pathlib import Path
from threading import Event
from typing import Any, Callable, Literal, NotRequired, Protocol, TypedDict

RECORD_ENABLED = False
BackendKind = Literal["elog", "dlog"]
RECORD_BACKEND: BackendKind = "elog"
SNAPSHOT_MAX_POINTS = 2000
SNAPSHOT_POLL_MS = 500
SNAPSHOT_POLL_MIN_MS = 250
DLOG_FALLBACK_MAX_BYTES = 256 * 1024 ** 2  # 256 MiB, including header and indices
MAX_CHANNELS = 4
PERIOD_MAX_S = 60.0
PERIOD_GRANULARITY_S = 20.48e-6
DURATION_MIN_S = 1.0  # shared ELOG/DLOG form; DLOG cannot request less than one second
# Must equal core.dlog.DLOG_MAX_DURATION; guaranteed by tests.
DURATION_MAX_S = 359_996_400.0
# Average V/I only: parameters = channels * enabled quantities (1..8).
PERIOD_FLOOR_TABLE = {n: n * 102.4e-6 for n in range(1, 9)}
EVENT_RECORD = "record"
# Recorder gap kinds; counts are host-time estimates, not measured losses.
ELOG_EVENT_TAIL_INCOMPLETE = "tail_incomplete"
ELOG_EVENT_TAIL_LIMIT = "tail_limit"
MAX_FETCH_FAILURES = 3

Tail = Literal["drain_then_abort", "abort_then_drain"]
# E.02.09 returns +303 for FETC after ABOR (HANDOFF 13.9-9), so B-ELOG
# must use drain_then_abort. USB/LAN fetch tuning remains pending H0 evidence.
ELOG_TAIL: Tail = "drain_then_abort"
ELOG_FETCH_RECORDS = 16_384  # request ceiling; actual request may be smaller
ELOG_FETCH_INTERVAL_S = 0.250


class NoRecordsError(ValueError):
    """A recording contains no complete persisted records."""


class RecordCode(str, Enum):
    DISABLED = "disabled"
    POWER = "power"
    ANALYSIS = "analysis"
    DRAINING = "draining"
    FAULT = "fault"
    CLOSING = "closing"
    BAD_CONFIG = "bad_config"
    CONNECT = "connect"
    ELOG_UNSUPPORTED = "elog_unsupported"
    FETCH = "fetch"
    SINK = "sink"
    STUCK = "stuck"
    NO_RECORDING = "no_recording"
    STALE = "stale"


CODE_I18N_KEY: dict[RecordCode, str] = {
    code: "record_error_" + code.value for code in RecordCode
}
RecordState = Literal["idle", "starting", "running", "stopping", "done", "failed"]
ACTIVE_STATES = frozenset({"starting", "running", "stopping"})


class Config(TypedDict):
    """Bridge input, seconds and local channel numbers; no paths or SCPI from JS.

    machine_index indexes resources (0/1). demo permits an empty resources list.
    run_id is injected by the bridge on a copy; clients must not supply it.
    duration_s is a finite bound in DURATION_MIN_S..DURATION_MAX_S, in both branches.
    """
    machine_index: int
    channels: list[int]
    sense_volt: bool
    sense_curr: bool
    period_s: float
    duration_s: float
    run_id: NotRequired[str]


RecordConfig = Config


class RecordStatus(TypedDict):
    run_id: str  # empty before the first accepted run; new opaque ID per run
    state: RecordState
    elapsed_s: float
    records: int  # complete persisted records, not preview samples
    gaps: int  # gap events, not an asserted number of lost samples
    preview: bool
    name: NotRequired[str]  # basename only; never the absolute path
    error: NotRequired[str]  # localized human text
    code: NotRequired[str]  # RecordCode.value


class EnvelopeSeries(TypedDict):
    t0: float  # first bucket's left edge, seconds
    dt: float  # bucket width; last bucket may be partial
    min: list[float | None]  # None represents nonfinite values in JSON only
    max: list[float | None]


class Snapshot(TypedDict):
    run_id: str
    seq: int  # monotonic within run, increment after each visible append
    preview: bool
    period_s: float  # actual sample period (MEAS cadence for preview)
    keys: dict[str, EnvelopeSeries]  # selected V1/I1 .. V4/I4; no P keys


class Unchanged(TypedDict):
    unchanged: Literal[True]
    seq: int


SnapshotResult = Snapshot | Unchanged


class RecordError(TypedDict):
    error: str
    code: str


class Cancelled(TypedDict):
    cancelled: Literal[True]


class RecordMeta(TypedDict):
    enabled: bool
    snapshot_poll_ms: int
    snapshot_poll_min_ms: int
    snapshot_max_points: int
    max_channels: int
    period_floor_table: dict[str, float]  # JSON string keys "1".."8", parameter count
    period_max_s: float
    period_granularity_s: float
    duration_min_s: float
    duration_max_s: float
    dlog_fallback_max_bytes: int


class RunResult(TypedDict):
    run_id: str
    final_path: Path  # worker-private, never returned directly to JS
    elapsed_s: float
    records: int
    gaps: int
    preview: bool
    notes: list[str]


EmitStatus = Callable[[RecordStatus], None]
Untimed = Callable[[], bool]
Finishing = Callable[[], None]


class Backend(Protocol):
    @property
    def preview(self) -> bool: ...

    def run(self, stop: Event, emit: EmitStatus) -> RunResult:
        """Blocking; every VISA operation stays on this calling worker.

        Emit status copies, raise on failure; do not close the caller-owned inst.
        Enter with finishing() for configuration, then untimed() for capture.
        Pass finishing as ElogRecorder.on_cleanup BEFORE tail/abort/finalize.
        DLOG transfer may use another
        untimed -> try/finally: finishing region. See C4 for ownership details.
        """
        ...

    def snapshot(self, since_seq: int) -> SnapshotResult:
        """Thread safe, detached JSON-compatible copy, no instrument/disk I/O."""
        ...


class MakeBackend(Protocol):
    """record.backend.make_backend implements this callable; no implementation here."""

    def __call__(self, inst: Any, cfg: Config, final_path: Path, *,
                 untimed: Untimed, finishing: Finishing) -> Backend: ...


class DlogWriter(Protocol):
    """record.dlogfile.DlogWriter interface. float32 arrays described by Any.

    Construction creates X.dlog.part and X.dlog.json.part exclusively.
    append converts to big endian, channel ascending then V,I; no value correction.
    finalize publishes sidecar first, then dlog; see C6 for partial failure/recovery.
    """

    def __init__(self, final_path: Path, *, channels: list[int], sense_volt: bool,
                 sense_curr: bool, tint: float, models: dict[int, str],
                 sidecar: dict) -> None: ...

    def append(self, block: Any) -> None: ...  # float32[n, n_fields]

    def finalize(self) -> Path: ...

    def abort(self, keep_part: bool = True) -> None: ...


class RecoverPart(Protocol):
    def __call__(self, part: Path) -> Path | None: ...


class ElogBlock(Protocol):
    def __call__(self, data: dict[int, Any], fields: tuple[str, ...],
                 channels: list[int]) -> Any: ...  # ndarray float32[n, n_fields]


# Required shell/bridge translation keys; text lives only in i18n.STRINGS.
# slew_warning applies only when VOLT:SLEW:MAX? returns override=0 and
# VOLT:SLEW? returns a finite rate below the plan's max |delta V / delta t|
# across positive-duration segments; rates >= INF_SENTINEL mean no limit.
# Its fields are channel, actual (current rate), required (plan rate), in V/s.
# Do not query VOLT:SLEW? MAX. Zero-duration jumps add no requirement.
# override=1 forces maximum slew; do not warn based on a stored lower rate.
I18N_KEYS: frozenset[str] = frozenset({
    "slew_warning",
    "record_mode_view",
    "record_mode_record",
    "record_title",
    "record_machine",
    "record_machine_option",
    "record_channels",
    "record_channel",
    "record_voltage",
    "record_current",
    "record_period",
    "record_duration",
    "record_start",
    "record_stop",
    "record_save_title",
    "record_file_type",
    "record_load",
    "record_elapsed",
    "record_records",
    "record_gaps",
    "record_gap_warning",
    "record_preview",
    "record_preview_note",
    "record_time_axis",
    "record_file",
    "record_note",
    "record_state_idle",
    "record_state_starting",
    "record_state_running",
    "record_state_stopping",
    "record_state_done",
    "record_state_failed",
    "record_error_disabled",
    "record_error_power",
    "record_error_analysis",
    "record_error_draining",
    "record_error_fault",
    "record_error_closing",
    "record_error_bad_config",
    "record_error_connect",
    "record_error_elog_unsupported",
    "record_error_fetch",
    "record_error_sink",
    "record_error_stuck",
    "record_error_no_recording",
    "record_error_stale",
})
