"""Participant packaging must not turn an unconfirmed identity into a submission."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "prepare_submission", ROOT / "scripts/prepare_submission.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def candidate():
    candidate = module.load_json(ROOT / "submission/candidate.json")
    candidate.update(
        confirmed_c5_team_id=None, team_id_mapping_source=None, competition_url=None
    )
    return candidate


@pytest.fixture
def ready(candidate):
    result = copy.deepcopy(candidate)
    result.update(
        confirmed_c5_team_id="organizer-assigned-test-team",
        team_id_mapping_source="https://example.org/official-mapping",
        competition_url="https://example.org/competition",
    )
    return result


def evidence(candidate):
    from regression_suite.build_reference_cache import scenario_index

    body = module.descriptor(candidate)
    count = len(scenario_index())
    return {
        "image": module.SubmissionDescriptor.from_mapping(body).image_reference(),
        "status": "passed",
        "profile": "developer",
        "rankable": False,
        "regression": {
            "total_scenarios": count,
            "passed": count,
            "failed": 0,
            "errored": 0,
        },
        "batch_units": [unit.name for unit in module.batch_units()],
        "anonymous_registry_access": True,
        "repeats": {
            unit.name: {
                "runs": 3,
                "parquet_sha256": {
                    name: "a" * 64 for name in module.repeat_artifacts(unit)
                },
            }
            for unit in module.repeat_units()
        },
    }


def test_pending_identity_refuses_zip(candidate, tmp_path):
    with pytest.raises(ValueError, match="Submission blocked"):
        module.package(candidate, evidence(candidate), tmp_path / "submission.zip")
    assert not list(tmp_path.iterdir())


def test_package_roundtrip_and_reproducible_bytes(ready, tmp_path):
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    module.package(ready, evidence(ready), first)
    module.package(ready, evidence(ready), second)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["submission.json"]
        parsed = module.SubmissionDescriptor.from_mapping(
            json.loads(archive.read("submission.json"))
        )
    assert parsed.team_id == ready["confirmed_c5_team_id"]
    assert parsed.track == "simulation" and parsed.phase == "dev"
    assert parsed.category == "simulator"
    assert len(parsed.models) == 1
    assert parsed.models[0].name == "none-deterministic-simulator"
    assert parsed.models[0].revision == ready["image"]["digest"]


@pytest.mark.parametrize(
    "key", ["competition_url", "team_id_mapping_source", "confirmed_c5_team_id"]
)
def test_each_registration_field_required(ready, tmp_path, key):
    ready[key] = None
    with pytest.raises(ValueError, match="Submission blocked"):
        module.package(ready, evidence(ready), tmp_path / "submission.zip")


@pytest.mark.parametrize(
    "delta",
    [
        {"image": "ghcr.io/example/wrong@sha256:" + "0" * 64},
        {"status": "failed"},
        {"profile": "official"},
        {"rankable": True},
    ],
)
def test_wrong_verification_refuses_zip(ready, tmp_path, delta):
    report = {**evidence(ready), **delta}
    with pytest.raises(ValueError, match="verification must"):
        module.package(ready, report, tmp_path / "submission.zip")


def test_existing_archive_not_overwritten(ready, tmp_path):
    archive = tmp_path / "submission.zip"
    archive.write_bytes(b"previous reviewed package")
    with pytest.raises(FileExistsError):
        module.package(ready, evidence(ready), archive)
    assert archive.read_bytes() == b"previous reviewed package"


def test_secret_field_rejected(candidate):
    candidate["team_key"] = "not-a-real-key"
    with pytest.raises(ValueError, match="never add a Team Key"):
        module.descriptor(candidate)


def test_cli_reports_pending_registration(candidate, tmp_path):
    config = tmp_path / "candidate.json"
    config.write_text(json.dumps(candidate))
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/prepare_submission.py"),
            "status",
            "--candidate",
            str(config),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["registration_ready"] is False
    assert any("confirmed_c5_team_id" in reason for reason in report["blockers"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("regression", {"total_scenarios": 0, "passed": 0, "failed": 0, "errored": 0}),
        ("batch_units", []),
        ("repeats", {}),
        ("anonymous_registry_access", False),
    ],
)
def test_incomplete_evidence_refuses_package(ready, tmp_path, field, value):
    report = evidence(ready)
    report[field] = value
    with pytest.raises(ValueError, match="verification must"):
        module.package(ready, report, tmp_path / "submission.zip")


def test_changed_repeat_bytes_are_observable(tmp_path):
    output = tmp_path / "sub_00"
    output.mkdir()
    trace = output / "trace.parquet"
    trace.write_bytes(b"original trace bytes")
    before = module.output_hashes(tmp_path)
    trace.write_bytes(b"changed trace bytes")
    assert (
        module.output_hashes(tmp_path)["sub_00/trace.parquet"]
        != before["sub_00/trace.parquet"]
    )


def test_missing_repeat_ledger_refuses_package(ready, tmp_path):
    report = evidence(ready)
    unit = module.repeat_units()[0]
    del report["repeats"][unit.name]["parquet_sha256"]["message_trace.parquet"]
    with pytest.raises(ValueError, match="byte-repeat checks"):
        module.package(ready, report, tmp_path / "submission.zip")
