"""`-d name@ref`: datasets that accept a ref select another revision; the others refuse `@`; the resolved ref is in
lock.json and the job config."""

import json

import pytest

from simple_evals_mm import dataset as dataset_mod
from simple_evals_mm import runner, sources
from tests.fakes import Arith

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_refused_without_accepts_ref():
    with pytest.raises(ValueError, match="does not accept a ref"):
        dataset_mod.get_dataset(f"ai2d@{SHA}")
    with pytest.raises(ValueError, match="empty ref"):
        dataset_mod.split_ref("ai2d@")
    assert dataset_mod.split_ref("ai2d") == ("ai2d", None)


def test_hf_revision_ref():
    ds = dataset_mod.get_dataset(f"hakushobench@{SHA}")
    assert ds.revision == SHA and ds.ref == SHA
    assert ds.ref_info() == {"given": SHA, "kind": "hf_revision", "revision": SHA,
                             "class_default": type(ds).revision}
    assert ds.config()["ref"] == SHA
    assert dataset_mod.get_dataset("hakushobench").ref_info() is None
    with pytest.raises(ValueError, match="40-digit commit"):
        dataset_mod.get_dataset("hakushobench@main")


def test_omnidocbench_ref():
    ds = dataset_mod.get_dataset(f"omnidocbench@{SHA}")
    assert ds.revision == SHA and ds.data_dir().endswith(f"@{SHA}")
    with pytest.raises(ValueError, match="not both"):
        dataset_mod.get_dataset(f"omnidocbench@{SHA}", revision=SHA)


def test_livecodebench_release_ref():
    from simple_evals_mm.tasks import livecodebench

    ds = dataset_mod.get_dataset("livecodebench@release_v5")
    assert ds.release == "release_v5" and ds.revision == type(ds).revision  # the pinned commit, fewer files
    info = ds.ref_info()
    assert info["files"] == list(livecodebench.LCB_FILES[:5]) and info["release"] == "release_v5"
    assert ds.data_dir().endswith("@release_v5")
    assert dataset_mod.get_dataset("livecodebench").data_dir() == livecodebench.DATA
    with pytest.raises(ValueError, match="releases: release_v1"):
        dataset_mod.get_dataset("livecodebench@release_v9")


def test_jammeval_ref_needs_a_sha_table(monkeypatch):
    with pytest.raises(ValueError, match="sha256 table"):
        dataset_mod.get_dataset(f"heronbench@{SHA}")
    monkeypatch.setitem(sources.JAMMEVAL_SHA256_BY_REVISION, SHA, {"Heron-Bench-Refined/x.parquet": "0" * 64})
    seen = []
    monkeypatch.setattr(sources, "jammeval", lambda subset, split="test", revision=None: seen.append(revision) or [])
    ds = dataset_mod.get_dataset(f"heronbench@{SHA}")
    assert ds.revision == SHA and ds.load() == [] and seen == [SHA]
    cv = dataset_mod.get_dataset(f"cvqaja@{SHA}")
    assert cv.load() == [] and seen[-1] == SHA


class RefArith(Arith):
    accepts_ref = "hf_revision"


def test_ref_in_lock_and_config_and_resume(job_env, monkeypatch, capsys):
    s = job_env
    monkeypatch.setitem(dataset_mod.DATASETS, "arith", RefArith)

    def run(ds, name="j"):
        return s["sem"]("run", "-m", "sglang/fake", "-d", ds, "--job-name", name, "-o", s["jobs"], "-n", "1",
                        "--judge-model", "sglang/judge")

    assert run(f"arith@{SHA}") == 0
    st = s["store"]()
    lock = st.read_lock()
    assert lock["datasets"]["arith"]["revision"] == SHA and lock["datasets"]["arith"]["ref"]["given"] == SHA
    assert st.read_config()["datasets"] == [f"arith@{SHA}"]
    assert st.read_meta("arith", 1)["dataset_revision"] == SHA
    assert runner.summarize_job(st)["arith"]["reward"] is not None
    assert s["sem"]("job", "regrade", st.dir) == 0
    capsys.readouterr()
    other = "f" * 40
    assert run(f"arith@{other}") == 2  # another ref = another job
    assert runner.RESUME_REFUSED in capsys.readouterr().err
    assert run(f"arith@{SHA},arith", name="dup") == 2
    assert "given twice" in capsys.readouterr().err
    monkeypatch.setitem(dataset_mod.DATASETS, "arith", Arith)
    assert run(f"arith@{SHA}", name="k") == 2
    assert "does not accept a ref" in capsys.readouterr().err


def test_dry_run_reports_the_ref(job_env, monkeypatch, capsys):
    s = job_env
    monkeypatch.setitem(dataset_mod.DATASETS, "arith", RefArith)
    capsys.readouterr()
    assert s["sem"]("run", "-m", "sglang/fake", "-d", f"arith@{SHA}", "-o", s["jobs"], "--dry-run",
                    "--judge-model", "sglang/judge") == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["datasets"]["arith"]["revision"] == SHA and plan["datasets"]["arith"]["ref"] == SHA
