"""Track 3 has two submission verbs; these pin which unit takes which.

    python tests/test_verb_dispatch.py

Docker-free and ABIDES-free by construction: ``scenario_io`` is loaded straight from its file so
the package ``__init__`` (and with it numpy/pandas/abides) is never imported. That is what lets
this run in the lint job alongside the other stdlib-only tests.

The defect these exist to prevent shipped: the CodaBench ingestion program emitted
``simulate --config /input/scenario.json`` for *every* simulation unit and never emitted
``simulate-batch`` at all. The six ``t3-gbatch-*`` public dev units carry ``batch.json`` +
``scenarios/`` and no ``scenario.json``, so all six died on an unguarded FileNotFoundError inside
the image — surfacing as a bare traceback on a path nobody chose, which reads as a corrupt unit
rather than as the harness dispatching the wrong verb.

The harness half is fixed in the organizer's ingestion program, which now chooses the verb with
the same content-based rule as ``scenario_io.is_batch_unit``. This file guards the repo half:
the units must keep the shape that rule classifies, and a wrong-verb run must fail legibly.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_UNITS = _REPO / "units"


def _load_scenario_io():
    """Load scenario_io.py by path, bypassing the abides_fork package __init__."""
    path = _REPO / "baselines" / "abides_fork" / "scenario_io.py"
    spec = importlib.util.spec_from_file_location("t3_scenario_io", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sio = _load_scenario_io()

_FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        _FAILURES.append(message)


# --------------------------------------------------------------- repo shape (the real units)
def test_every_unit_is_exactly_one_shape() -> None:
    """Every unit must be classifiable, and unambiguously so.

    A unit that is neither shape gets an argv naming a file it does not have; a unit that is
    both is a unit whose verb depends on which check runs first.
    """
    for unit in sorted(p for p in _UNITS.iterdir() if p.is_dir()):
        batch = sio.is_batch_unit(unit)
        single = (unit / "scenario.json").is_file()
        check(
            batch != single,
            f"{unit.name}: not exactly one shape "
            f"(batch.json+scenarios/={batch}, scenario.json={single}). "
            f"Contains: {sorted(p.name for p in unit.iterdir())}",
        )


def test_batch_units_have_no_scenario_json() -> None:
    """The invariant that makes content-based dispatch safe, stated directly.

    If a batch unit ever gained a scenario.json, `simulate` would silently run *something* on it
    and produce a trace for the wrong workload — a wrong answer instead of a loud failure.
    """
    for unit in sorted(p for p in _UNITS.iterdir() if p.is_dir()):
        if sio.is_batch_unit(unit):
            check(
                not (unit / "scenario.json").exists(),
                f"{unit.name}: batched unit also has a scenario.json",
            )


def test_the_known_batch_units_are_still_detected() -> None:
    """The six t3-gbatch-* units are the ones the shipped harness got wrong; keep them covered."""
    named = sorted(p.name for p in _UNITS.glob("t3-gbatch-*") if p.is_dir())
    check(len(named) >= 1, "no t3-gbatch-* units found — has the family been renamed?")
    for name in named:
        check(
            sio.is_batch_unit(_UNITS / name),
            f"{name}: named as a batch unit but not detected as one",
        )


def test_detection_is_by_content_not_by_name() -> None:
    """A unit named t3-gbatch-* but shaped as a single scenario must NOT take the batch verb."""
    with tempfile.TemporaryDirectory() as td:
        impostor = Path(td) / "t3-gbatch-not-really"
        impostor.mkdir()
        (impostor / "scenario.json").write_text("{}")
        check(not sio.is_batch_unit(impostor), "name prefix alone triggered batch detection")

        # ...and the converse: an ordinary name with batch contents IS a batch unit.
        renamed = Path(td) / "t3-family-gb-01"
        (renamed / "scenarios").mkdir(parents=True)
        (renamed / "batch.json").write_text("{}")
        check(sio.is_batch_unit(renamed), "batch contents under a plain name were not detected")


# ------------------------------------------------------------------- the wrong-verb diagnostic
def test_wrong_verb_on_a_batch_unit_names_the_cause() -> None:
    """`simulate` pointed at a batched unit must say so, not raise a bare FileNotFoundError."""
    with tempfile.TemporaryDirectory() as td:
        unit = Path(td) / "t3-gbatch-x"
        (unit / "scenarios").mkdir(parents=True)
        (unit / "batch.json").write_text('{"n": 0, "subs": []}')
        try:
            sio.read_scenario(unit / "scenario.json")
        except FileNotFoundError as exc:
            msg = str(exc)
            check("simulate-batch" in msg, f"message does not name the right verb: {msg}")
            check("BATCHED" in msg, f"message does not say the unit is batched: {msg}")
        else:
            _FAILURES.append("read_scenario did not raise on a missing scenario.json")


def test_missing_scenario_on_a_normal_unit_lists_what_is_there() -> None:
    """The other direction: no batch files, so report the directory instead of guessing."""
    with tempfile.TemporaryDirectory() as td:
        unit = Path(td) / "t3-plain"
        unit.mkdir()
        (unit / "card.toml").write_text("")
        try:
            sio.read_scenario(unit / "scenario.json")
        except FileNotFoundError as exc:
            msg = str(exc)
            check("card.toml" in msg, f"message does not list the unit contents: {msg}")
            check("simulate-batch" not in msg, f"message wrongly blames the batch verb: {msg}")
        else:
            _FAILURES.append("read_scenario did not raise on a missing scenario.json")


def test_a_present_scenario_is_read_normally() -> None:
    """The guard must not change the happy path."""
    with tempfile.TemporaryDirectory() as td:
        unit = Path(td) / "t3-plain"
        unit.mkdir()
        (unit / "scenario.json").write_text('{"seed": 1}')
        check(
            sio.read_scenario(unit / "scenario.json") == '{"seed": 1}',
            "read_scenario altered a readable scenario file",
        )


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}):")
        for failure in _FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"OK: {len(tests)} verb-dispatch tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
