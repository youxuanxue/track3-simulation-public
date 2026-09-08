"""Recheck the participant image and package a Development submission.

Local verification is non-rankable. Official identity mapping must be supplied
before a package can be produced; website IDs are never inferred.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from importlib.resources import files
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qfbench2_common.contracts.descriptor import (  # noqa: E402
    SubmissionDescriptor,
    seal_descriptor_digest,
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def descriptor(candidate: dict) -> dict:
    allowed = {
        "image",
        "license",
        "confirmed_c5_team_id",
        "team_id_mapping_source",
        "competition_url",
    }
    if set(candidate) != allowed:
        raise ValueError(
            "candidate fields must match submission/candidate.json; never add a Team Key"
        )
    fixture = files("qfbench2_common").joinpath(
        "contracts/fixtures/c5/simulation_dev.json"
    )
    body = json.loads(fixture.read_text())
    body.update(
        schema_version="1.1.0",
        models=[],
        image=candidate["image"],
        license=candidate["license"],
        team_id=candidate["confirmed_c5_team_id"] or "unconfirmed-do-not-submit",
    )
    body.pop("descriptor_digest", None)
    sealed = seal_descriptor_digest(body)
    SubmissionDescriptor.from_mapping(sealed)
    return sealed


def blockers(candidate: dict) -> list[str]:
    missing = []
    team_id = candidate["confirmed_c5_team_id"]
    if not isinstance(team_id, str) or not team_id.strip():
        missing.append("confirmed_c5_team_id: official mapping not yet obtained")
    for key in ("competition_url", "team_id_mapping_source"):
        value = candidate[key]
        parsed = urlparse(value) if isinstance(value, str) else None
        if (
            not parsed
            or parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
        ):
            missing.append(f"{key}: official HTTPS source required")
    return missing


def public_image(image: dict) -> None:
    if image["registry"] != "ghcr.io":
        raise ValueError(
            "anonymous registry probe currently supports this candidate's GHCR registry"
        )
    base = f"https://ghcr.io/v2/{image['repository']}/manifests/{image['digest']}"
    token_url = f"https://ghcr.io/token?service=ghcr.io&scope=repository:{image['repository']}:pull"
    with urlopen(token_url, timeout=30) as response:
        token = json.load(response)["token"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": ", ".join(
            [
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            ]
        ),
    }
    with urlopen(Request(base, headers=headers), timeout=30) as response:
        payload = response.read()
        if "sha256:" + hashlib.sha256(payload).hexdigest() != image["digest"]:
            raise ValueError("registry returned different image bytes")


def run(command: list[str], env: dict | None = None) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def output_hashes(output: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(output.rglob("*.parquet")):
        with path.open("rb") as handle:
            hashes[path.relative_to(output).as_posix()] = hashlib.file_digest(
                handle, "sha256"
            ).hexdigest()
    return hashes


def batch_units() -> list[Path]:
    return sorted((ROOT / "units").glob("t3-gbatch-*"))


def repeat_units() -> list[Path]:
    return [ROOT / "units/t3-as06-throughput-fast", batch_units()[0]]


def repeat_artifacts(unit: Path) -> set[str]:
    from qfbench2_track_simulation.batch import load_subs

    prefixes = (
        [entry["sub"] + "/" for entry in load_subs(unit)]
        if (unit / "batch.json").exists()
        else [""]
    )
    return {
        prefix + name
        for prefix in prefixes
        for name in ("trace.parquet", "message_trace.parquet")
    }


def validate_verification(evidence: dict, image: str) -> None:
    from regression_suite.build_reference_cache import scenario_index

    if evidence.get("image") != image or evidence.get("status") != "passed":
        raise ValueError("verification must pass for the exact candidate image digest")
    if evidence.get("profile") != "developer" or evidence.get("rankable") is not False:
        raise ValueError("verification must declare the local developer profile")
    total = len(scenario_index())
    if evidence.get("regression") != {
        "total_scenarios": total,
        "passed": total,
        "failed": 0,
        "errored": 0,
    }:
        raise ValueError(
            "verification must cover the complete public regression roster"
        )
    if evidence.get("batch_units") != [unit.name for unit in batch_units()]:
        raise ValueError("verification must cover every public batch unit")
    if evidence.get("anonymous_registry_access") is not True:
        raise ValueError("verification must confirm anonymous registry access")
    for unit in repeat_units():
        repeat = evidence.get("repeats", {}).get(unit.name, {})
        hashes = repeat.get("parquet_sha256", {})
        if repeat.get("runs") != 3 or set(hashes) != repeat_artifacts(unit):
            raise ValueError(
                "verification must include single and batch byte-repeat checks"
            )
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
            for value in hashes.values()
        ):
            raise ValueError("verification must contain SHA-256 parquet hashes")


def verify(candidate: dict, output: Path) -> dict:
    from qfbench2_common.smoke import run_smoke
    from qfbench2_track_simulation.scoring import build_developer_verifier
    from throughput.timer import bounded_container_run

    image = SubmissionDescriptor.from_mapping(descriptor(candidate)).image_reference()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    public_image(candidate["image"])
    run(["docker", "pull", "--platform=linux/amd64", image])
    inspected = json.loads(
        subprocess.check_output(["docker", "image", "inspect", image])
    )[0]
    if inspected["Architecture"] != "amd64" or inspected["Os"] != "linux":
        raise ValueError("candidate must resolve to linux/amd64")
    labels = inspected["Config"].get("Labels") or {}
    required_labels = {
        "qfbench2.interface_version": "2.0",
        "qfbench2.track": "simulation",
        "qfbench2.category": "simulator",
    }
    if any(labels.get(key) != value for key, value in required_labels.items()):
        raise ValueError(
            "image must declare the simulation/simulator interface 2.0 labels"
        )
    run([sys.executable, ".github/validate_units.py", "simulation"])
    reference = output / "references"
    run(
        [
            sys.executable,
            "regression_suite/build_reference_cache.py",
            "--out",
            str(reference),
        ]
    )
    scratch = output / "tmp"
    scratch.mkdir()
    env = {**os.environ, "PYTHONPATH": str(ROOT), "TMPDIR": str(scratch)}
    run(
        [
            sys.executable,
            "regression_suite/run_regression.py",
            "--candidate-image",
            image,
            "--scenarios-dir",
            "regression_suite/scenarios",
            "--reference-dir",
            str(reference),
            "--output-dir",
            str(output / "regression"),
            "--workers",
            "1",
        ],
        env,
    )
    report = load_json(output / "regression/report.json")
    if (
        report["passed"] != report["total_scenarios"]
        or report["failed"]
        or report["errored"]
    ):
        raise ValueError("public regression failed")

    def simulate(unit: Path, destination: Path) -> dict[str, str]:
        destination.mkdir(parents=True)
        batch = (unit / "batch.json").exists()
        source = unit / "scenarios" if batch else unit / "scenario.json"
        target = "/input/scenarios" if batch else "/input/scenario.json"
        cidfile = destination.parent / (destination.name + ".cid")
        command = [
            "docker",
            "run",
            "--rm",
            "--platform=linux/amd64",
            "--network=none",
            "--cpus=4",
            "--memory=16g",
            "--cidfile",
            str(cidfile),
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{source}:{target}:ro",
            "-v",
            f"{destination}:/output",
            image,
        ]
        command += (
            ["simulate-batch", "--batch-dir", target, "--out-dir", "/output"]
            if batch
            else ["simulate", "--config", target, "--out", "/output/trace.parquet"]
        )
        result = bounded_container_run(command, cidfile=cidfile, timeout_sec=1800)
        destination.with_suffix(".stdout.log").write_bytes(result.stdout)
        destination.with_suffix(".stderr.log").write_bytes(result.stderr)
        result.check_returncode()
        verdict = run_smoke(unit, destination, build_developer_verifier)
        if not verdict.admissible:
            raise ValueError(f"developer verification failed: {unit.name}")
        return output_hashes(destination)

    for unit in batch_units():
        simulate(unit, output / "batch" / unit.name)
    repeated = {}
    for unit in repeat_units():
        hashes = [
            simulate(unit, output / "repeats" / unit.name / str(i)) for i in range(3)
        ]
        if not hashes[0] or any(item != hashes[0] for item in hashes):
            raise ValueError(f"repeated parquet bytes differ: {unit.name}")
        repeated[unit.name] = {"runs": len(hashes), "parquet_sha256": hashes[0]}
    evidence = {
        "image": image,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "profile": "developer",
        "rankable": False,
        "status": "passed",
        "host": platform.platform(),
        "toolkit_version": version("qfbench2-common"),
        "scorer_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "regression": {
            key: report[key]
            for key in ("total_scenarios", "passed", "failed", "errored")
        },
        "batch_units": [unit.name for unit in batch_units()],
        "repeats": repeated,
        "exemplar": "not verified: no public reference traces; full-size run remains pending",
        "anonymous_registry_access": True,
    }
    validate_verification(evidence, image)
    (output / "verification.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def package(candidate: dict, evidence: dict, output: Path) -> None:
    body = descriptor(candidate)
    pending = blockers(candidate)
    if pending:
        raise ValueError("Submission blocked: " + "; ".join(pending))
    image = SubmissionDescriptor.from_mapping(body).image_reference()
    validate_verification(evidence, image)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents replacing a previously reviewed submission.
    with output.open("xb") as handle, zipfile.ZipFile(
        handle, "w", zipfile.ZIP_DEFLATED
    ) as archive:
        entry = zipfile.ZipInfo("submission.json", date_time=(1980, 1, 1, 0, 0, 0))
        archive.writestr(entry, json.dumps(body, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "verify", "package"))
    parser.add_argument(
        "--candidate", type=Path, default=ROOT / "submission/candidate.json"
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--verification", type=Path)
    args = parser.parse_args()
    try:
        candidate = load_json(args.candidate)
        descriptor(candidate)
        if args.action == "status":
            pending = blockers(candidate)
            print(
                json.dumps(
                    {"registration_ready": not pending, "blockers": pending}, indent=2
                )
            )
            return int(bool(pending))
        if args.out is None:
            parser.error("--out is required; verification directories must be new")
        if args.action == "verify":
            print(json.dumps(verify(candidate, args.out), indent=2))
        else:
            if args.verification is None:
                parser.error("--verification is required for packaging")
            package(candidate, load_json(args.verification), args.out)
            print(f"Created {args.out}")
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
