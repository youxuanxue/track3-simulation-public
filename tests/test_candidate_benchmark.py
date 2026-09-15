"""Candidate selection must use complete paired host evidence, never the best run."""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import benchmark_candidates as bench  # noqa: E402


def plan():
    return {
        "images": {"A": "a", "B": "b"},
        "roster": [
            {"unit": "short", "family": "first"},
            {"unit": "long", "family": "second"},
        ],
        "analysis_seed": bench.ANALYSIS_SEED,
        "bootstrap_samples": bench.BOOTSTRAPS,
    }


def records(a=(100, 100, 100, 100, 100), b=(110, 110, 110, 110, 110)):
    return [
        {
            "arm": arm,
            "unit": unit,
            "group": i,
            "measurement": {
                "n_events": 1000,
                "reported_n_events": 1000,
                "host_wall_clock_sec": 1000 / rate,
                "events_per_sec": 1e99 if arm == "A" else 0,
            },
        }
        for arm, rates in (("A", a), ("B", b))
        for unit in ("short", "long")
        for i, rate in enumerate(rates)
    ]


def test_group_bootstrap_uses_host_clock_not_reported_rate():
    result = bench.paired_statistics(plan(), records())
    assert result["scores"] == pytest.approx({"A": 100, "B": 110})
    assert result["delta_score"] == pytest.approx(10)
    assert result["ci95"] == pytest.approx([10, 10])
    assert all(f["ci95"] == pytest.approx([10, 10]) for f in result["family"].values())


def test_one_best_run_and_cross_zero_do_not_establish_gain():
    result = bench.paired_statistics(plan(), records(b=(90, 95, 100, 105, 10000)))
    assert result["delta_score"] == 0
    assert result["ci95"][0] < 0 < result["ci95"][1]


def test_family_regression_cannot_hide_in_total():
    rows = records(b=(150,) * 5)
    for row in rows:
        if row["unit"] == "long" and row["arm"] == "B":
            row["measurement"]["host_wall_clock_sec"] = 1000 / 90
    result = bench.paired_statistics(plan(), rows)
    assert result["ci95"][0] > 0
    assert result["family"]["second"]["ci95"][1] < 0


def test_missing_unit_refuses_mean_over_subset():
    with pytest.raises(KeyError):
        bench.paired_statistics(plan(), records()[:-1])


def test_duplicate_record_refused():
    rows = records()
    with pytest.raises(ValueError, match="duplicate"):
        bench.paired_statistics(plan(), rows + [rows[0]])


@pytest.mark.parametrize(
    "field,value",
    [
        ("host_wall_clock_sec", float("nan")),
        ("host_wall_clock_sec", 0),
        ("reported_n_events", 1000000),
    ],
)
def test_invalid_host_measurement_refused(field, value):
    rows = records()
    rows[0]["measurement"][field] = value
    with pytest.raises(ValueError, match="host measurement"):
        bench.paired_statistics(plan(), rows)


def test_hash_index_detects_changed_evidence(tmp_path):
    path = tmp_path / "record.json"
    bench.save(path, {"rankable": False, "value": 1})
    entry = bench.index(path)
    assert bench.read_index(entry)["value"] == 1
    path.write_text('{"value":2}')
    with pytest.raises(ValueError, match="bytes changed"):
        bench.read_index(entry)


def decision(g2="pass", g3="pass", purpose="confirmation"):
    return {
        "G2": g2,
        "G3": g3,
        "purpose": purpose,
        "images": {"A": "a", "B": "b"} if purpose != "baseline" else {"A": "a"},
        "reasons": [],
        "evidence": {"path": "fixture", "sha256": "fixture"},
        "rankable": False,
    }


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(bench.preparation, "validate_delivery", lambda *a: None)
    archive = tmp_path / "archive"
    archive.write_bytes(b"fixture archive")
    g1s = []
    for image in ("a", "b"):
        p = tmp_path / (image + ".json")
        bench.save(
            p,
            {
                "image": image,
                "scope": "G1",
                "status": "passed",
                "rankable": False,
                "archive": str(archive),
                "archive_sha256": bench.file_digest(archive),
                "delivery": {},
            },
        )
        g1s.append(p)
    monkeypatch.setattr(
        bench, "assess", lambda _: decision(purpose="baseline", g3="missing")
    )
    directory = tmp_path / "history"
    baseline = bench.decide(directory, tmp_path / "evidence", g1s, None)
    assert baseline["stable"] == "a" and baseline["action"] == "promote"
    return directory, g1s


@pytest.mark.parametrize(
    "g2,g3",
    [("missing", "pass"), ("fail", "pass"), ("pass", "inconclusive"), ("pass", "fail")],
)
def test_incomplete_failed_or_uncertain_candidate_keeps_baseline(
    state, monkeypatch, tmp_path, g2, g3
):
    directory, g1s = state
    monkeypatch.setattr(bench, "assess", lambda _: decision(g2, g3))
    event = bench.decide(directory, tmp_path / "evidence", g1s, "a")
    assert event["stable"] == "a" and event["action"] != "promote"


def test_only_all_gates_promote_and_baseline_change_refuses(
    state, monkeypatch, tmp_path
):
    directory, g1s = state
    monkeypatch.setattr(bench, "assess", lambda _: decision())
    with pytest.raises(ValueError, match="pointer changed"):
        bench.decide(directory, tmp_path / "evidence", g1s, "wrong")
    promoted = bench.decide(directory, tmp_path / "evidence", g1s, "a")
    assert promoted["stable"] == "b" and promoted["previous_stable"] == "a"
    assert len(bench.history(directory)) == 2
    with pytest.raises(ValueError, match="already used"):
        bench.decide(directory, tmp_path / "evidence", g1s, "b")


def test_missing_g1_prevents_promotion(state, monkeypatch, tmp_path):
    directory, _ = state
    monkeypatch.setattr(bench, "assess", lambda _: decision())
    event = bench.decide(directory, tmp_path / "evidence", [], "a")
    assert event["stable"] == "a" and event["decision"]["G1"] == "missing"


def test_history_tampering_refused(state):
    directory, _ = state
    path = directory / "000000.json"
    event = json.loads(path.read_text())
    event["stable"] = "unverified"
    path.write_text(json.dumps(event))
    with pytest.raises(ValueError, match="history modified"):
        bench.history(directory)


@pytest.fixture
def full_evidence(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    import validate_candidate_parameters as parameters

    units = [
        {
            "unit": f"unit-{i:02d}",
            "family": f"family-{i % 7}",
            "batch": False,
            "input_sha256": f"input-{i}",
        }
        for i in range(72)
    ]
    identities = {
        "code_sha256": "code",
        "toolkit_version": "test",
        "toolkit_source": "test",
        "scorer_sha256": "scorer",
    }
    node = {"native": True, "node": "dedicated-test"}
    monkeypatch.setattr(bench, "identity", lambda: identities)
    monkeypatch.setattr(bench, "roster", lambda: units)
    monkeypatch.setattr(bench, "host", lambda: node)
    monkeypatch.setattr(
        bench.preparation,
        "repeat_artifacts",
        lambda _: {"trace.parquet", "message_trace.parquet"},
    )
    monkeypatch.setattr(parameters, "validate", lambda *args: None)
    heldout = tmp_path / "heldout.json"
    bench.save(heldout, {"identity": identities})
    heldout_map = tmp_path / "holdouts.json"
    bench.save(heldout_map, {"a": bench.index(heldout), "b": bench.index(heldout)})
    frozen = {
        "rankable": False,
        "profile": "developer",
        "purpose": "confirmation",
        "images": {"A": "a", "B": "b"},
        "roster": units,
        "identity": identities,
        "host": node,
        "caps": bench.CAPS,
        "repeats": bench.REPEATS,
        "warmups": 1,
        "analysis_seed": bench.ANALYSIS_SEED,
        "bootstrap_samples": bench.BOOTSTRAPS,
        "holdout": bench.index(heldout_map),
    }
    frozen["plan_sha256"] = bench.digest(frozen)
    plan_path = tmp_path / "plan.json"
    bench.save(plan_path, frozen)
    out = tmp_path / "output"
    out.mkdir()
    pq.write_table(pa.table({"t_ns": list(range(1000))}), out / "trace.parquet")
    pq.write_table(
        pa.table({"send_time_ns": list(range(1000))}), out / "message_trace.parquet"
    )
    bench.save(out / "events.json", {"n_events": 1000})
    log = tmp_path / "stdout.log"
    log.write_text("bounded container log\n")
    artifacts = [bench.index(p) for p in sorted(out.iterdir())]
    rows = []
    for u in units:
        for arm in ("A", "B"):
            for group in range(-1, bench.REPEATS):
                raw = {
                    "unit": u["unit"],
                    "arm": arm,
                    "group": group,
                    "image": frozen["images"][arm],
                    "host": node,
                    "plan_sha256": frozen["plan_sha256"],
                    "input_sha256": u["input_sha256"],
                    "rankable": False,
                    "status": "passed",
                    "verification": {
                        "admissible": True,
                        "gates": {f"g{i}": {"passed": True} for i in range(4)},
                    },
                    "measurement": {
                        "n_events": 1000,
                        "reported_n_events": 1000,
                        "host_wall_clock_sec": 10 if arm == "A" else 9,
                        "events_per_sec": 1e99,
                    },
                    "resources": {
                        "peak_memory_bytes": 10000000,
                        "peak_disk_bytes": 200000,
                        "disk_capacity_bytes": 10 * 1024**3,
                        "disk_status": "bounded",
                    },
                    "output_dir": str(out),
                    "parquet_sha256": bench.preparation.output_hashes(out),
                    "artifacts": artifacts,
                    "logs": [bench.index(log)],
                }
                path = tmp_path / "raw" / f"{u['unit']}-{arm}-{group}.json"
                bench.save(path, raw)
                rows.append(bench.index(path))
    evidence_path = tmp_path / "evidence.json"
    bench.save(
        evidence_path,
        {"plan": bench.index(plan_path), "runs": rows, "diagnostic": False},
    )
    return evidence_path, frozen


def test_full_roster_assessment_derives_positive_gain(full_evidence):
    result = bench.assess(full_evidence[0])
    assert result["G2"] == result["G3"] == "pass"
    assert len(result["statistics"]["units"]) == 72
    assert result["exemplar_semantics"] == "unknown" and result["rankable"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "missing-unit",
        "semantic-failure",
        "host-drift",
        "missing-memory",
        "missing-disk",
        "self-report",
        "missing-artifacts",
    ],
)
def test_full_roster_faults_block_promotion(full_evidence, fault):
    evidence_path, _ = full_evidence
    e = json.loads(evidence_path.read_text())
    if fault == "missing-unit":
        e["runs"] = e["runs"][:-1]
    else:
        entry = e["runs"][0]
        path = Path(entry["path"])
        raw = json.loads(path.read_text())
        if fault == "semantic-failure":
            raw["verification"]["admissible"] = False
        elif fault == "host-drift":
            raw["host"] = {"node": "another-machine"}
        elif fault == "missing-memory":
            raw["resources"]["peak_memory_bytes"] = None
        elif fault == "missing-disk":
            raw["resources"]["peak_disk_bytes"] = None
        elif fault == "missing-artifacts":
            raw["artifacts"] = []
        elif fault == "self-report":
            raw["measurement"]["n_events"] = 10**12
            raw["measurement"]["reported_n_events"] = 10**12
        path.write_text(json.dumps(raw))
        e["runs"][0] = bench.index(path)
    evidence_path.write_text(json.dumps(e))
    result = bench.assess(evidence_path)
    assert result["G2"] != "pass" and result["G3"] != "pass"


def test_stale_code_rejects_complete_evidence(full_evidence, monkeypatch):
    monkeypatch.setattr(bench, "identity", lambda: {"code_sha256": "changed"})
    with pytest.raises(ValueError, match="stale code"):
        bench.assess(full_evidence[0])


def test_rollback_requires_qualified_prior_candidate(state, monkeypatch, tmp_path):
    directory, g1s = state
    monkeypatch.setattr(bench, "assess", lambda _: decision())
    bench.decide(directory, tmp_path / "evidence", g1s, "a")
    real_read = bench.read_index
    monkeypatch.setattr(
        bench, "read_index", lambda e: {} if e["path"] == "fixture" else real_read(e)
    )
    result = bench.rollback(directory, "a", "observed regression", "b")
    assert result["stable"] == "a" and result["previous_stable"] == "b"
    assert [e["action"] for e in bench.history(directory)] == [
        "promote",
        "promote",
        "rollback",
    ]
    with pytest.raises(ValueError, match="current"):
        bench.rollback(directory, "a", "again", "a")


def test_problem_count_survives_rounds_but_resets_after_fix(tmp_path):
    assert bench.record_problem(tmp_path, "unit", "semantic") == 1
    assert bench.record_problem(tmp_path, "unrelated", None) == 0
    assert bench.record_problem(tmp_path, "unit", "semantic") == 2
    assert bench.record_problem(tmp_path, "unit", "semantic") == 3
    assert bench.record_problem(tmp_path, "unit", None) == 0
    assert bench.record_problem(tmp_path, "unit", "semantic") == 1


def test_kernel_reserved_host_memory_is_not_a_container_limit_failure():
    bench.require_native_host(
        {"native": True, "docker_cpus": 4, "docker_memory": 16766418944}
    )
    with pytest.raises(ValueError, match="native linux"):
        bench.require_native_host(
            {"native": False, "docker_cpus": 4, "docker_memory": 32 * 1024**3}
        )
