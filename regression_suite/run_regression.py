"""QFBench 2.0 Track-3 regression harness.

Runs a candidate Docker image against a set of public scenario configs, compares
output traces to reference traces, and checks both matching-engine semantics
(Tier A: exact fill sequence; Tier B: statistical proximity) and stylized-fact
admissibility.

Usage::

    python run_regression.py \\
        --candidate-image myregistry/mysim:latest \\
        --scenarios-dir ./scenarios \\
        --reference-dir ./reference_traces \\
        --output-dir ./run_outputs \\
        --workers 4

Exit codes:
    0  All scenarios passed.
    1  One or more scenarios failed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import tomllib
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from qfbench2_track_simulation import semantics
from throughput.timer import bounded_container_run


def _copy_regular_file(source: Path, destination: Path) -> None:
    """Copy a REGULAR file, refusing a symlink or any other node type.

    `shutil.copy` follows links. Opening with `O_NOFOLLOW` and checking `st_nlink` refuses both a
    symlink and a hard link into somewhere the participant should not be able to export from.
    """
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{source} is not a regular file")
        if info.st_nlink > 1:
            raise OSError(f"{source} is a hard link (st_nlink={info.st_nlink})")
        with open(fd, "rb", closefd=False) as handle:
            destination.write_bytes(handle.read())
    finally:
        os.close(fd)


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical event vocabulary
# ---------------------------------------------------------------------------
# These constants MUST match the sealed final scorer
# (private/scoring/final_scorer.py) and the trace schema in
# common/schemas/sim_scenario.schema.json.  A mismatch silently turns the
# Tier-A semantic check into a no-op (it would see zero fills and pass every
# submission), so they are defined here once and reused throughout.

#: Message types that constitute a fill event for Tier A comparisons.
FILL_MSG_TYPES: frozenset[str] = frozenset({"ORDER_FILLED", "PARTIAL_FILL"})

#: Message types that carry a quoted price level used for book reconstruction.
QUOTE_MSG_TYPES: frozenset[str] = frozenset({"QUOTE_UPDATE"})

#: Canonical order-book side labels.
SIDE_BID: str = "BID"
SIDE_ASK: str = "ASK"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ScenarioResult:
    """Outcome of running a single scenario through the candidate image.

    Attributes:
        scenario_id: Unique identifier from scenario.json.
        scenario_path: Absolute path to the scenario.json file.
        tier: Tolerance tier read from scenario.json ("A" or "B").
        tier_a_pass: Whether Tier A (exact fill sequence) check passed.
        tier_b_pass: Whether Tier B (statistical proximity) check passed.
        stylized_pass: Whether stylized-fact admissibility check passed.
        events_per_sec: Value from candidate events.json; None if unavailable.
        failure_labels: List of human-readable failure identifiers.
        error: Unhandled exception message if the scenario run itself errored.
    """

    scenario_id: str
    scenario_path: Path
    tier: str
    tier_a_pass: bool = False
    tier_b_pass: bool = False
    stylized_pass: bool = False
    events_per_sec: float | None = None
    failure_labels: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def semantic_pass(self) -> bool:
        """True when the relevant semantic tier(s) passed."""
        if self.tier == "A":
            return self.tier_a_pass
        # Tier B scenarios must also satisfy Tier A fill-sequence semantics.
        return self.tier_a_pass and self.tier_b_pass

    @property
    def overall_pass(self) -> bool:
        """True when all gates passed and no unhandled error occurred."""
        return self.error is None and self.semantic_pass and self.stylized_pass


@dataclass
class RegressionReport:
    """Aggregated results across all scenarios.

    Attributes:
        candidate_image: Docker image tag that was tested.
        total_scenarios: Total number of scenarios attempted.
        passed: Number of scenarios that passed every gate.
        failed: Number of scenarios that failed at least one gate.
        errored: Number of scenarios that raised an unhandled exception.
        results: Per-scenario ScenarioResult objects.
    """

    candidate_image: str
    total_scenarios: int
    passed: int
    failed: int
    errored: int
    results: list[ScenarioResult] = field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        """True when every scenario passed."""
        return self.failed == 0 and self.errored == 0


# ---------------------------------------------------------------------------
# TOML helper
# ---------------------------------------------------------------------------


def load_card_params(card_path: Path) -> dict[str, Any]:
    """Parse a card.toml file and return its contents as a nested dict.

    Falls back to an empty dict when the file is absent so that callers can
    supply defaults without special-casing missing cards.

    Args:
        card_path: Path to a card.toml unit card file.

    Returns:
        Parsed TOML contents, or ``{}`` if the file does not exist.

    Raises:
        tomllib.TOMLDecodeError: If the file exists but is not valid TOML.
    """
    if not card_path.exists():
        logger.debug("card.toml not found at %s; using defaults", card_path)
        return {}
    with card_path.open("rb") as fh:
        return tomllib.load(fh)


# ---------------------------------------------------------------------------
# Mid-price helper
# ---------------------------------------------------------------------------


def compute_mid_price_series(trace_df: pd.DataFrame) -> pd.Series:
    """Derive a mid-price time series from a trace DataFrame.

    The trace contains one row per market event.  Best bid and best ask at each
    nanosecond snapshot are reconstructed from ``QUOTE_UPDATE`` messages:
    bid-side quotes (``side == "BID"``) set the bid, ask-side quotes
    (``side == "ASK"``) set the ask.  The mid-price is the average of the two.
    Rows whose ``msg_type`` is not in :data:`QUOTE_MSG_TYPES` are ignored for
    the purpose of book reconstruction.

    When the DataFrame contains no usable quote stream (fewer than 2 levels on
    either side), the function falls back to using fill prices
    (:data:`FILL_MSG_TYPES`) and finally the raw ``price`` column as a proxy
    mid-price series.

    Args:
        trace_df: DataFrame with at minimum ``t_ns``, ``msg_type``, ``side``,
            and ``price`` columns (integer prices in ticks).

    Returns:
        A ``pd.Series`` with ``t_ns`` as the index and mid-price (float) as
        values, sorted by time.  The name is ``"mid_price"``.
    """
    required = {"t_ns", "msg_type", "side", "price"}
    if not required.issubset(trace_df.columns):
        missing = required - set(trace_df.columns)
        raise ValueError(f"trace_df missing columns: {missing}")

    # Reconstruct the book from quote updates (canonical) and fall back to
    # fill events if no quote stream is present.
    quote_events = trace_df[trace_df["msg_type"].isin(QUOTE_MSG_TYPES)].copy()
    book_events = quote_events
    if book_events.empty:
        logger.warning(
            "No QUOTE_UPDATE events found; falling back to fill prices for book reconstruction."
        )
        book_events = trace_df[trace_df["msg_type"].isin(FILL_MSG_TYPES)].copy()

    if book_events.empty:
        logger.warning("No quote/fill events found; falling back to raw price column.")
        s = trace_df.set_index("t_ns")["price"].astype(float).rename("mid_price")
        return s.sort_index()

    bids = book_events[book_events["side"] == SIDE_BID].set_index("t_ns")["price"]
    asks = book_events[book_events["side"] == SIDE_ASK].set_index("t_ns")["price"]

    if bids.empty or asks.empty:
        logger.warning("One-sided book; falling back to raw price column.")
        s = book_events.set_index("t_ns")["price"].astype(float).rename("mid_price")
        return s.sort_index()

    # Resample to a common time grid using forward-fill.
    all_times = book_events["t_ns"].sort_values().unique()
    bids_ffill = bids.reindex(all_times).ffill().bfill()
    asks_ffill = asks.reindex(all_times).ffill().bfill()

    mid = ((bids_ffill + asks_ffill) / 2.0).rename("mid_price")
    mid.index.name = "t_ns"
    return mid.sort_index().dropna()


# ---------------------------------------------------------------------------
# Tier A semantic check
# ---------------------------------------------------------------------------


def check_tier_a_semantics(
    candidate_df: pd.DataFrame,
    reference_df: pd.DataFrame,
    timestamp_tolerance_ns: int,
) -> tuple[bool, list[str]]:
    """Tier-A semantic check.

    Delegates to the shared track semantics (``qfbench2_track_simulation.semantics``) so the
    public practice harness and the official CodaBench gate run exactly ONE implementation --
    including the Kendall-tau event-ordering check (Family-3 latency), which this harness
    previously lacked.
    """
    return semantics.check_tier_a(candidate_df, reference_df, timestamp_tolerance_ns)


# ---------------------------------------------------------------------------
# Tier B semantic check
# ---------------------------------------------------------------------------


def check_tier_b_semantics(
    candidate_df: pd.DataFrame,
    reference_df: pd.DataFrame,
    params: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Tier-B statistical-proximity check (delegates to the shared track semantics)."""
    return semantics.check_tier_b(
        candidate_df,
        reference_df,
        ks_ceiling=float(params.get("stylized_fact_ceilings", {}).get("ks", 0.08)),
        spread_bps_tolerance=float(params.get("spread_bps_tolerance", 10.0)),
    )


# ---------------------------------------------------------------------------
# Per-scenario runner
# ---------------------------------------------------------------------------


def run_scenario(
    scenario_path: Path,
    candidate_image: str,
    reference_dir: Path,
    output_dir: Path,
    tolerance: dict[str, Any],
) -> ScenarioResult:
    """Run the candidate Docker image on one scenario and evaluate all gates.

    The function:

    1. Reads ``scenario.json`` to extract ``scenario_id`` and tolerance params.
    2. Optionally loads ``card.toml`` (sibling to scenario.json) for Docker
       resource limits (``cpus``, ``memory``).
    3. Spins up the candidate image with ``--network=none``, mounts the scenario
       as ``/input/scenario.json``, and writes output to a temp directory.
    4. Reads ``trace.parquet`` from the container output and the reference dir.
    5. Runs Tier A (and optionally Tier B) semantic checks.
    6. Runs the stylized-fact admissibility check via
       ``qfbench2_common.scoring.stylized_facts``.
    7. Reads ``events.json`` and validates ``events_per_sec``.

    Args:
        scenario_path: Path to the ``scenario.json`` file.
        candidate_image: Docker image tag (e.g. ``myregistry/mysim:latest``).
        reference_dir: Directory tree whose sub-directories are named by
            ``scenario_id`` and each contain ``trace.parquet``.
        output_dir: Root output directory; per-scenario artefacts are written
            to a sub-directory named by ``scenario_id``.
        tolerance: Tolerance override dict (merged on top of scenario-level
            tolerances).

    Returns:
        A fully populated :class:`ScenarioResult`.
    """
    # --- Parse scenario.json ------------------------------------------------
    try:
        scenario_data: dict[str, Any] = json.loads(scenario_path.read_text())
    except Exception as exc:  # noqa: BLE001
        return ScenarioResult(
            scenario_id="<parse-error>",
            scenario_path=scenario_path,
            tier="A",
            error=f"Failed to parse scenario.json: {exc}",
            failure_labels=["shared.schema.invalid_output"],
        )

    scenario_id: str = scenario_data.get("scenario_id", scenario_path.stem)
    tol_block: dict[str, Any] = {**scenario_data.get("tolerance", {}), **tolerance}
    tier: str = str(tol_block.get("tier", "A")).upper()
    ts_tolerance_ns: int = int(tol_block.get("timestamp_tolerance_ns", 1_000))

    result = ScenarioResult(
        scenario_id=scenario_id,
        scenario_path=scenario_path,
        tier=tier,
    )

    # --- Card-level Docker resource params ----------------------------------
    card_path = scenario_path.parent / "card.toml"
    card = load_card_params(card_path)
    env_block: dict[str, Any] = card.get("environment", {})
    cpus: str = str(env_block.get("cpus", 4))
    memory: str = (
        str(env_block.get("memory", "16g")).replace("Gi", "g").replace("G", "g")
    )

    # --- Prepare per-scenario output dir ------------------------------------
    scenario_out_dir = output_dir / scenario_id
    scenario_out_dir.mkdir(parents=True, exist_ok=True)

    # --- Run Docker ---------------------------------------------------------
    with tempfile.TemporaryDirectory(prefix="qfbench_") as tmp_out:
        tmp_out_path = Path(tmp_out)
        # Copy scenario.json into a dedicated input dir so we can mount it.
        tmp_in_path = Path(tempfile.mkdtemp(prefix="qfbench_in_"))
        try:
            shutil.copy(scenario_path, tmp_in_path / "scenario.json")
            # Outside the mounted /input and /output trees, so the submission never sees it.
            cidfile = tmp_in_path.parent / f"{tmp_in_path.name}.cid"

            docker_cmd = [
                "docker",
                "run",
                "--rm",
                "--cidfile",
                str(cidfile),
                "--network",
                "none",
                "--cpus",
                cpus,
                "--memory",
                memory,
                "-v",
                f"{tmp_in_path}:/input:ro",
                "-v",
                f"{tmp_out_path}:/output",
                candidate_image,
                "simulate",
                "--config",
                "/input/scenario.json",
                "--out",
                "/output/trace.parquet",
            ]

            logger.debug("Running: %s", " ".join(docker_cmd))
            # Bounded on BOTH axes. `capture_output=True` buffers the container's entire stdout in
            # this process's memory, which a stdout flood turns into an OOM on the machine doing
            # the measuring; the shared runner drains the pipes continuously and keeps only the
            # tail. It also kills the CONTAINER through its cidfile when the deadline fires, not
            # just the docker client -- killing the client alone leaves a detached container
            # holding the CPU quota and corrupting the next scenario's measurement.
            proc_bytes = bounded_container_run(
                docker_cmd, cidfile=cidfile, timeout_sec=3600.0
            )

            if proc_bytes.returncode != 0:
                tail = proc_bytes.stderr.decode("utf-8", errors="replace")[-2000:]
                result.error = (
                    f"Docker exited with code {proc_bytes.returncode} "
                    f"(negative means the 3600s deadline fired and the container was killed). "
                    f"stderr: {tail}"
                )
                result.failure_labels.append("shared.resource.timeout")
                return result

            # --- Load candidate trace.parquet --------------------------------
            cand_trace_path = tmp_out_path / "trace.parquet"
            if not cand_trace_path.exists():
                result.error = "candidate did not produce trace.parquet"
                result.failure_labels.append("shared.schema.invalid_output")
                return result

            candidate_df = pd.read_parquet(cand_trace_path)

            # Copy artefacts to the persistent output dir, NO-FOLLOW.
            #
            # `shutil.copy` follows symlinks, so a participant link at /output/trace.parquet would
            # have its TARGET's bytes copied into the retained tree -- the same defect that was
            # measured in the unit runner's `shutil.copytree(..., symlinks=False)`. The practice
            # harness runs untrusted images on a participant's own workstation, so it gets the
            # same treatment.
            _copy_regular_file(cand_trace_path, scenario_out_dir / "trace.parquet")
            events_src = tmp_out_path / "events.json"
            if events_src.exists():
                _copy_regular_file(events_src, scenario_out_dir / "events.json")

        finally:
            shutil.rmtree(tmp_in_path, ignore_errors=True)

    # --- Load reference trace -----------------------------------------------
    ref_trace_path = reference_dir / scenario_id / "trace.parquet"
    if not ref_trace_path.exists():
        result.error = f"Reference trace not found: {ref_trace_path}"
        result.failure_labels.append("t3.semantic_regression_fail")
        return result

    reference_df = pd.read_parquet(ref_trace_path)

    # --- Tier A semantic check -----------------------------------------------
    tier_a_pass, tier_a_failures = check_tier_a_semantics(
        candidate_df, reference_df, ts_tolerance_ns
    )
    result.tier_a_pass = tier_a_pass
    if not tier_a_pass:
        result.failure_labels.extend(tier_a_failures)
        result.failure_labels.append("t3.semantic_regression_fail")

    # --- Tier B semantic check (only if scenario tier is B) -----------------
    if tier == "B":
        tier_b_pass, tier_b_failures = check_tier_b_semantics(
            candidate_df, reference_df, tol_block
        )
        result.tier_b_pass = tier_b_pass
        if not tier_b_pass:
            result.failure_labels.extend(tier_b_failures)
            result.failure_labels.append("t3.semantic_regression_fail")
    else:
        # Tier A scenarios pass Tier B trivially (not evaluated).
        result.tier_b_pass = True

    # --- Stylized-fact admissibility ----------------------------------------
    try:
        from qfbench2_common.scoring.stylized_facts import (  # noqa: PLC0415
            admissible,
            stylized_fact_report,
        )
    except ImportError as exc:
        raise ImportError(
            "qfbench2_common is not installed or not on PYTHONPATH. "
            "Install with: pip install -e QFBench2/common  "
            f"(original error: {exc})"
        ) from exc

    try:
        cand_mid = compute_mid_price_series(candidate_df)
        ref_mid = compute_mid_price_series(reference_df)

        sf_report = stylized_fact_report(
            cand_prices=cand_mid.values.astype(np.float64),
            ref_prices=ref_mid.values.astype(np.float64),
            cand_depth_hist=semantics.depth_histogram(candidate_df),
            ref_depth_hist=semantics.depth_histogram(reference_df),
        )

        sf_ceilings: dict[str, float] = {
            "ks": float(tol_block.get("stylized_fact_ceilings", {}).get("ks", 0.08)),
            "acf_abs_l2": float(
                tol_block.get("stylized_fact_ceilings", {}).get("acf_abs_l2", 0.12)
            ),
            "hill_abs": float(
                tol_block.get("stylized_fact_ceilings", {}).get("hill_abs", 1.5)
            ),
            "depth_js": float(
                tol_block.get("stylized_fact_ceilings", {}).get("depth_js", 0.10)
            ),
        }

        sf_pass, sf_breaches = admissible(sf_report, sf_ceilings)
        result.stylized_pass = sf_pass

        # Persist the report.
        sf_report_path = scenario_out_dir / "stylized_fact_report.json"
        sf_report_path.write_text(json.dumps(sf_report, indent=2))

        if not sf_pass:
            result.failure_labels.append("t3.stylized_fact_breach")
            result.failure_labels.extend([f"t3.sf.{b}" for b in sf_breaches])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Stylized-fact check failed for %s: %s", scenario_id, exc)
        result.stylized_pass = False
        result.failure_labels.append(f"t3.stylized_fact_error: {exc}")

    # --- events.json validation ----------------------------------------------
    events_json_path = scenario_out_dir / "events.json"
    if events_json_path.exists():
        try:
            events_data: dict[str, Any] = json.loads(events_json_path.read_text())
            eps = events_data.get("events_per_sec")
            if eps is None or float(eps) <= 0:
                result.failure_labels.append(
                    "t3.events_per_sec_invalid: missing or non-positive"
                )
            else:
                result.events_per_sec = float(eps)
        except Exception as exc:  # noqa: BLE001
            result.failure_labels.append(f"t3.events_json_parse_error: {exc}")
    else:
        result.failure_labels.append("t3.events_json_missing")

    return result


# ---------------------------------------------------------------------------
# Parallel driver
# ---------------------------------------------------------------------------


def _run_scenario_worker(kwargs: dict[str, Any]) -> ScenarioResult:
    """Top-level wrapper for ProcessPoolExecutor — unpacks kwargs dict.

    Args:
        kwargs: Dictionary of keyword arguments forwarded to
            :func:`run_scenario`.

    Returns:
        Result of :func:`run_scenario`.
    """
    return run_scenario(**kwargs)


def discover_scenarios(scenarios_dir: Path) -> list[Path]:
    """Discover all ``scenario.json`` files under ``scenarios_dir``.

    Skips ``index.json`` files to avoid treating the index as a scenario.

    Args:
        scenarios_dir: Directory to search recursively.

    Returns:
        Sorted list of Path objects pointing to ``scenario.json`` files.
    """
    return sorted(
        p
        for p in scenarios_dir.rglob("*.json")
        if p.name not in {"index.json"} and p.stem != "index"
    )


def build_report(
    candidate_image: str,
    results: list[ScenarioResult],
) -> RegressionReport:
    """Aggregate per-scenario results into a :class:`RegressionReport`.

    Args:
        candidate_image: Docker image tag.
        results: List of completed :class:`ScenarioResult` objects.

    Returns:
        A fully populated :class:`RegressionReport`.
    """
    passed = sum(1 for r in results if r.overall_pass)
    errored = sum(1 for r in results if r.error is not None)
    failed = len(results) - passed - errored
    return RegressionReport(
        candidate_image=candidate_image,
        total_scenarios=len(results),
        passed=passed,
        failed=failed,
        errored=errored,
        results=results,
    )


def print_summary_table(report: RegressionReport) -> None:
    """Print a formatted per-scenario pass/fail summary to stdout.

    Columns: scenario_id | tier | semantic_pass | stylized_pass |
    events_per_sec | failure_labels

    Args:
        report: Completed :class:`RegressionReport`.
    """
    col_widths = {
        "scenario_id": 42,
        "tier": 5,
        "semantic_pass": 14,
        "stylized_pass": 14,
        "events_per_sec": 16,
        "failure_labels": 60,
    }

    header = (
        f"{'scenario_id':<{col_widths['scenario_id']}} "
        f"{'tier':<{col_widths['tier']}} "
        f"{'semantic_pass':<{col_widths['semantic_pass']}} "
        f"{'stylized_pass':<{col_widths['stylized_pass']}} "
        f"{'events_per_sec':<{col_widths['events_per_sec']}} "
        f"failure_labels"
    )
    separator = "-" * len(header)

    print()
    print(f"Regression Report — candidate: {report.candidate_image}")
    print(separator)
    print(header)
    print(separator)

    for r in sorted(report.results, key=lambda x: x.scenario_id):
        eps_str = f"{r.events_per_sec:,.0f}" if r.events_per_sec is not None else "N/A"
        labels_str = "; ".join(r.failure_labels) if r.failure_labels else "—"
        semantic_str = "PASS" if r.semantic_pass else ("ERROR" if r.error else "FAIL")
        stylized_str = "PASS" if r.stylized_pass else ("ERROR" if r.error else "FAIL")

        line = (
            f"{r.scenario_id:<{col_widths['scenario_id']}} "
            f"{r.tier:<{col_widths['tier']}} "
            f"{semantic_str:<{col_widths['semantic_pass']}} "
            f"{stylized_str:<{col_widths['stylized_pass']}} "
            f"{eps_str:<{col_widths['events_per_sec']}} "
            f"{labels_str}"
        )
        print(line)

    print(separator)
    print(
        f"Total: {report.total_scenarios}  "
        f"Passed: {report.passed}  "
        f"Failed: {report.failed}  "
        f"Errored: {report.errored}"
    )
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Parsed :class:`argparse.Namespace`.
    """
    parser = argparse.ArgumentParser(
        description="QFBench 2.0 Track-3 regression harness",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--candidate-image",
        required=True,
        help="Docker image tag of the candidate simulator.",
    )
    parser.add_argument(
        "--scenarios-dir",
        type=Path,
        default=Path("scenarios"),
        help="Directory containing scenario.json (and optional card.toml) files.",
    )
    parser.add_argument(
        "--reference-dir",
        type=Path,
        # Resolved against this script, not the CWD: the docs run from the repo root,
        # where the cache lives at regression_suite/reference_traces.
        default=Path(__file__).resolve().parent / "reference_traces",
        help=(
            "Directory whose sub-directories are named by scenario_id and each "
            "contain a reference trace.parquet."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("run_outputs"),
        help="Root directory for candidate artefacts and report.json.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel worker processes.",
    )
    parser.add_argument(
        "--tolerance-override",
        type=str,
        default=None,
        help=(
            "JSON string of tolerance overrides merged on top of per-scenario "
            "tolerances.  Example: '{\"timestamp_tolerance_ns\": 5000}'"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Discover scenarios and print the plan without launching Docker or "
            "running any checks."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Regression harness entry point.

    Args:
        argv: CLI argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Exit code: 0 if all scenarios passed, 1 otherwise.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args = parse_args(argv)

    # Parse tolerance override.
    tolerance_override: dict[str, Any] = {}
    if args.tolerance_override:
        try:
            tolerance_override = json.loads(args.tolerance_override)
        except json.JSONDecodeError as exc:
            logger.error("--tolerance-override is not valid JSON: %s", exc)
            return 1

    # Validate directories.
    if not args.scenarios_dir.exists():
        logger.error("scenarios-dir does not exist: %s", args.scenarios_dir)
        return 1
    if not args.dry_run and not args.reference_dir.exists():
        logger.error("reference-dir does not exist: %s", args.reference_dir)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Discover scenarios.
    scenario_paths = discover_scenarios(args.scenarios_dir)
    if not scenario_paths:
        logger.error("No scenario JSON files found under %s", args.scenarios_dir)
        return 1

    logger.info(
        "Discovered %d scenario(s) under %s", len(scenario_paths), args.scenarios_dir
    )

    if args.dry_run:
        print(f"[dry-run] Would run {len(scenario_paths)} scenario(s):")
        for p in scenario_paths:
            print(f"  {p}")
        return 0

    # Build worker kwargs.
    worker_kwargs_list = [
        {
            "scenario_path": p,
            "candidate_image": args.candidate_image,
            "reference_dir": args.reference_dir,
            "output_dir": args.output_dir,
            "tolerance": tolerance_override,
        }
        for p in scenario_paths
    ]

    results: list[ScenarioResult] = []

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_run_scenario_worker, kw): kw["scenario_path"]
            for kw in worker_kwargs_list
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unhandled error running scenario %s", path)
                result = ScenarioResult(
                    scenario_id=path.stem,
                    scenario_path=path,
                    tier="A",
                    error=str(exc),
                    failure_labels=["shared.resource.timeout"],
                )
            results.append(result)
            status = (
                "PASS" if result.overall_pass else ("ERROR" if result.error else "FAIL")
            )
            logger.info("[%s] %s", status, result.scenario_id)

    report = build_report(args.candidate_image, results)

    # Write report.json.
    report_path = args.output_dir / "report.json"

    def _serialise(obj: Any) -> Any:
        if isinstance(obj, Path):
            return str(obj)
        raise TypeError(f"Not serialisable: {type(obj)}")

    report_dict = asdict(report)
    report_path.write_text(json.dumps(report_dict, indent=2, default=_serialise))
    logger.info("Report written to %s", report_path)

    print_summary_table(report)

    return 0 if report.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
