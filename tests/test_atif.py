"""v0.5 ATIF trajectories."""

import json
import os

import pytest

from simple_evals_mm import atif
from simple_evals_mm.agent import Direct
from simple_evals_mm.models import Image, Task, user
from tests.fakes import Arith, FakeSampler, png


def test_atif_round_trip_and_validator(tmp_path):
    img = png(tmp_path / "a.png")

    class Two(Arith):
        def prompt(self, task):
            return [user(Image(img), "first")]

        def next_turn(self, task, history):
            return [user("second")] if len(history) < 3 else None

    store = atif.ImageStore(str(tmp_path / "job"))
    traj = Direct().run(Task("t1"), Two(), FakeSampler("ok"), job_id="uuid-1", attempt=1, images=store)
    d = json.loads(json.dumps(traj.to_dict()))
    assert atif.Trajectory.from_dict(d).to_dict() == d
    assert atif.validate(d) == []
    assert d["schema_version"] == "ATIF-v1.8" and d["trajectory_id"] == "t1__a1" and d["session_id"] == "uuid-1"
    assert [s["source"] for s in d["steps"]] == ["user", "agent", "user", "agent"]
    src = d["steps"][0]["message"][0]["source"]
    assert src["path"].startswith("images/") and os.path.exists(tmp_path / "job" / src["path"])
    assert "base64" not in json.dumps(d)


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["steps"][1].update(step_id=5), "not sequential"),
    (lambda d: d["steps"][0].update(model_name="m"), "agent-only"),
    (lambda d: d["steps"][0].update(message=[{"type": "image", "source": {"media_type": "image/png", "path": "/abs/x.png"}}]), "relative"),
    (lambda d: d.pop("session_id"), "session_id"),
])
def test_atif_validator_rejects(mutate, expect):
    traj = Direct().run(Task("t"), Arith(), FakeSampler("x"), job_id="u", attempt=1, images=None)
    d = traj.to_dict()
    mutate(d)
    assert any(expect in e for e in atif.validate(d))
