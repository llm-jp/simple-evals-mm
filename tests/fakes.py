"""Shared fakes of the tests: a scripted sampler, a toy dataset, the `job_env` and `image_dir` fixtures, and the
helpers of the dataset tests (images, one direct trial)."""

import io
from typing import ClassVar

import pytest
from PIL import Image as PILImage

from simple_evals_mm import atif, checks, cli, sources
from simple_evals_mm import dataset as dataset_mod
from simple_evals_mm.agent import Direct
from simple_evals_mm.dataset import Dataset
from simple_evals_mm.models import Response, Task, VerifierResult, user
from simple_evals_mm.samplers import registry
from simple_evals_mm.samplers.base import message_images
from simple_evals_mm.store import JobStore

VERDICT = "extracted_final_answer: x\nreasoning: t\ncorrect: {}\nconfidence: 100"


class FakeSampler:
    """reply(prompt_text) -> str | Response; `fail` maps a prompt substring to an exception to raise; `endpoint` is the
    endpoint record config() reports (as the OpenRouter / served samplers do after prepare())."""

    def __init__(self, reply="A", fail=None, slug="sglang/fake", endpoint=None):
        self.spec = registry.resolve(slug, preset="none")
        self.slug = slug
        self.reply, self.fail, self.calls, self.endpoint, self.seeds = reply, fail or {}, [], endpoint, []

    def prepare(self):
        pass

    def config(self):
        return {"fake": True, **({"endpoint": dict(self.endpoint)} if self.endpoint else {})}

    def __call__(self, messages, tools=None, seed=None):
        self.calls.append(messages)
        self.seeds.append(seed)
        text = messages[-1].text
        for key, exc in self.fail.items():
            if key in text:
                raise exc
        out = self.reply(text) if callable(self.reply) else self.reply
        r = out if isinstance(out, Response) else Response(text=out, input_tokens=3, output_tokens=1, finish_reason="stop")
        if not r.extra:  # as a sampler that sends the images unchanged reports them
            r.extra = {"sent_images": message_images(messages), "seed": seed}
        return r


ANSWERS = {"2+2?": "4", "3+3?": "7", "capital of France?": "Paris", "cut": "partial", "refuse": "-", "flaky": "-"}


class Arith(Dataset):
    name = "arith"
    revision = "test-rev"
    questions: ClassVar = ["2+2?", "3+3?", "capital of France?"]
    truth: ClassVar = {"2+2?": "4", "3+3?": "6", "capital of France?": "Paris", "cut": "x", "refuse": "x", "flaky": "x"}

    @staticmethod
    def verifier(task, traj, judge):
        return VerifierResult(task.id, float(traj.final_text == task.answer), method="exact")

    def load(self):
        return [Task(id=q, answer=self.truth[q], category="math" if q[0].isdigit() else "other") for q in self.questions]

    def prompt(self, task):
        return [user(task.id)]


@pytest.fixture
def job_env(tmp_path, monkeypatch):
    """Fake dataset, fake samplers and stubbed start checks; returns a `sem` runner + state."""
    state = {"commit": "c1", "sampler": FakeSampler(lambda q: ANSWERS[q]), "judge": FakeSampler(VERDICT.format("yes"))}
    monkeypatch.setitem(dataset_mod.DATASETS, "arith", Arith)
    for slug in ("sglang/fake", "sglang/judge"):  # presets are exact slugs
        monkeypatch.setitem(registry.PRESETS, slug, {})
    monkeypatch.setattr(registry, "make_sampler",
                        lambda spec, base_url=None: state["judge"] if "judge" in spec.model else state["sampler"])
    monkeypatch.setattr(checks, "start_checks", lambda allow_dirty=False, **kw: {
        "version": "0.5.0", "commit": state["commit"], "dirty": False,
        "lock_check": {"ok": True, "lock_sha256": "l", "mismatches": []}})
    jobs = str(tmp_path / "jobs")

    def sem(*args):
        return cli.main(list(args))

    def run(*extra, name="j"):
        return sem("run", "-m", "sglang/fake", "-d", "arith", "--job-name", name, "-o", jobs, "-n", "1",
                   "--judge-model", "sglang/judge", *extra)

    state.update(sem=sem, run=run, jobs=jobs, store=lambda name="j": JobStore(jobs, name))
    return state


def png(path, color="red"):
    """A 4x4 PNG at path; returns the path."""
    PILImage.new("RGB", (4, 4), color).save(path)
    return str(path)


def png_bytes(color="red", size=(4, 4)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def image_dir(tmp_path, monkeypatch):
    """Dataset image files (sources.image_file) go to a temporary directory (autouse: no test writes to data/)."""
    d = tmp_path / "images"
    monkeypatch.setattr(sources, "IMAGE_DIR", str(d))
    return d


def trial(ds, task, sampler, tmp_path):
    """One trial of `task` with the direct agent (images stored under tmp_path/job)."""
    return Direct().run(task, ds, sampler, job_id="u", attempt=1, images=atif.ImageStore(str(tmp_path / "job")))
