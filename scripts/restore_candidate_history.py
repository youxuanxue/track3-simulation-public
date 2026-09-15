"""Restore prior native failure records before a sequential qualification job.

Only the exact history member is read from a same-repository Actions artifact.
An interrupted experiment must not reset the count by moving to a new runner.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
from urllib.parse import urlencode
import zipfile

import benchmark_candidates as benchmark


def api(endpoint: str) -> bytes:
    return subprocess.check_output(
        ["gh", "api", endpoint], timeout=600 if endpoint.endswith("/zip") else 30
    )


def validate_history(payload: bytes) -> None:
    latest = {}
    for line in payload.decode().splitlines():
        record = json.loads(line)
        unit, failure = record["unit"], record["failure"]
        previous = latest.get(unit)
        count = benchmark.next_problem_count(previous, failure)
        if record["count"] != count:
            raise ValueError("failure history count disagrees with prior records")
        latest[unit] = record


def restore(repo: str, branch: str, current_run: int, output: Path) -> dict:
    query = urlencode({"event": "workflow_dispatch", "branch": branch, "per_page": 100})
    runs = json.loads(api(f"repos/{repo}/actions/runs?{query}"))["workflow_runs"]
    # The job's concurrency group serializes native runs on this branch. Ignore
    # the current run and newer queued dispatches, regardless of API list order.
    for run in sorted(runs, key=lambda r: r["id"], reverse=True):
        if run["id"] >= current_run:
            continue
        artifacts = json.loads(api(f"repos/{repo}/actions/runs/{run['id']}/artifacts"))[
            "artifacts"
        ]
        history_name = f"native-history-{run['id']}"
        for artifact in sorted(artifacts, key=lambda a: a["name"] != history_name):
            if artifact["name"] not in {
                history_name,
                f"native-qualification-{run['id']}",
            }:
                continue
            if artifact["expired"]:
                raise ValueError(
                    "prior native evidence expired; failure history needs recovery"
                )
            archive = api(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip")
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                member = (
                    "problems.jsonl"
                    if artifact["name"] == history_name
                    else "history/problems.jsonl"
                )
                if member not in z.namelist():
                    if any(name.endswith("/record.json") for name in z.namelist()):
                        raise ValueError(
                            "prior run records exist without failure history"
                        )
                    continue
                payload = z.read(member)
            validate_history(payload)
            output.mkdir(parents=True, exist_ok=True)
            with (output / "problems.jsonl").open("xb") as f:
                f.write(payload)
            result = {
                "rankable": False,
                "source_run": run["id"],
                "source_artifact": artifact["id"],
                "archive_sha256": hashlib.sha256(archive).hexdigest(),
                "history_sha256": hashlib.sha256(payload).hexdigest(),
            }
            benchmark.save(output / "restored-history.json", result)
            return result
    raise ValueError(
        "no retained native history found; recover history before qualification"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--current-run", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(restore(args.repo, args.branch, args.current_run, args.out)))
