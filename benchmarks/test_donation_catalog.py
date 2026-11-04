"""Keep every explicit maintainer assignment usable by the smallest donation lane.

The old checkpoint assignment exceeded the ordinary 40,000-character request
budget and failed before inference. This checks real release contents rather
than a mocked GitHub response; generated jobs have separate scheduler tests.
"""

import json
from pathlib import Path

import pytest

import donation

ROOT = Path(__file__).resolve().parents[1]
JOBS = json.loads((ROOT / "donation-tasks.json").read_text(encoding="utf-8"))


def test_explicit_queue_ids_are_unique():
    identities = [job["id"] for job in JOBS]
    assert len(identities) == len(set(identities))


@pytest.mark.parametrize("job", JOBS, ids=lambda job: job["id"])
def test_explicit_job_fits_every_lane_and_has_packaged_test(job):
    """An assigned review must reach inference and be able to submit its test."""
    manifest = json.loads((ROOT / "install-files.json").read_text(encoding="utf-8"))
    sources = {}
    assert 1 <= len(job["paths"]) <= 4
    for name in job["paths"]:
        assert donation.allowed_path(name), name
        assert name in manifest, name
        sources[name] = (ROOT / name).read_text(encoding="utf-8")
    assert any(name.startswith("Validation/test_") for name in sources)
    assert any(not name.startswith("Validation/") for name in sources)
    assert sum(map(len, sources.values())) <= 40_000, job["id"]
    # Ordinary continuous lanes also prefilter pinned Git tree byte sizes.
    assert sum((ROOT / name).stat().st_size for name in sources) <= 35_000, job["id"]

