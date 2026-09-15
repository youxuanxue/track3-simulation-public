"""Holdouts are schema-valid, fixed before runs, and cover the public families."""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import validate_candidate_parameters as parameters  # noqa: E402


def test_holdouts_cover_families_and_all_batch_sources(tmp_path):
    result = parameters.generate(
        tmp_path / "heldout", 990000, "fixture@sha256:" + "a" * 64
    )
    cases = result["cases"]
    assert len(cases) == 27
    assert len({c["family"] for c in cases if not c["batch"]}) == 7
    assert len({c["source_unit"] for c in cases if c["batch"]}) == 6
    seeds = []
    for case in cases:
        for entry in case["inputs"]:
            config = parameters.bench.read_index(entry)
            seeds.append(config["seed"])
            assert config["horizon_ns"] % 1000000 != 0
            assert sum(config["agent_mix"].values()) == sum(
                a["count"] for a in config["agent_configs"]
            )
    assert len(set(seeds)) == len(seeds)
    with pytest.raises(FileExistsError):
        parameters.generate(tmp_path / "heldout", 990000, "fixture@sha256:" + "a" * 64)


def test_incomplete_holdout_cannot_qualify(tmp_path):
    result = parameters.generate(
        tmp_path / "heldout", 990000, "fixture@sha256:" + "a" * 64
    )
    evidence = {
        "kind": "heldout-result",
        "image": "candidate",
        "rankable": False,
        "plan": parameters.bench.index(tmp_path / "heldout/holdout.json"),
        "records": [],
    }
    assert result["cases"]
    with pytest.raises(ValueError, match="incomplete heldout"):
        parameters.validate(evidence, "candidate")


def test_seed_and_boundary_variant_does_not_mutate_original():
    source = json.loads((ROOT / "units/t3-fastlob-core/scenario.json").read_text())
    before = json.dumps(source, sort_keys=True)
    altered = parameters.variant(source, 123456, 1)
    assert altered["seed"] == 123456
    assert altered["horizon_ns"] != source["horizon_ns"]
    assert json.dumps(source, sort_keys=True) == before
