"""datasets.lock: source files checked on download / use, the task digest checked after load (refusal, the
--allow-data-mismatch record, refs without an entry), `sem datasets lock [--check]`, the upstream comparison, the
vendored nltk data, and the contract that the committed lock covers every registered dataset (offline)."""

import hashlib
import json
import os

import datasets
import pytest

from simple_evals_mm import datalock, dataset_admin, sources
from simple_evals_mm import dataset as dataset_mod
from tests.fakes import Arith

URL = "https://example.org/data/a.csv"


class _Resp:
    def __init__(self, data):
        self.data = data
        self.content = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        yield self.data


def _serve(monkeypatch, data: bytes):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(data))


def _lock(datasets_=None, shared=None):
    datalock.write({"datasets": datasets_ or {}, "_shared": shared or {}})


def _src(data: bytes, url=URL):
    return {"url": url, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


# ---------------------------------------------------------------- source files
def test_download_accepts_only_the_locked_sha256(tmp_path, monkeypatch):
    _lock({"x": {"revision": None, "n_tasks": 0, "tasks_sha256": "", "sources": [_src(b"good")]}})
    _serve(monkeypatch, b"good")
    dest = tmp_path / "a.csv"
    assert sources.download(URL, str(dest)) == str(dest) and dest.read_bytes() == b"good"
    _serve(monkeypatch, b"changed upstream")
    with pytest.raises(datalock.DataMismatch, match="sha256 differs"):
        sources.download(URL, str(tmp_path / "b.csv"))
    assert not (tmp_path / "b.csv").exists() and not (tmp_path / "b.csv.part").exists()
    with pytest.raises(datalock.DataMismatch, match="not in datasets.lock"):
        sources.download("https://example.org/other.zip", str(tmp_path / "c.zip"))
    with datalock.track("x", allow=True) as rec:  # --allow-data-mismatch: kept and recorded
        sources.download(URL, str(tmp_path / "b.csv"))
    assert (tmp_path / "b.csv").exists() and rec.mismatches[0]["expected"] == _src(b"good")["sha256"]
    with datalock.track("x", mode="write") as rec:  # `sem datasets lock`: recorded, not compared
        sources.fetch_bytes(URL)
    assert rec.fetched[URL] == {k: v for k, v in _src(b"changed upstream").items() if k != "url"} and not rec.mismatches


def test_fetch_caches_and_checks_every_use(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "SOURCE_DIR", str(tmp_path / "_sources"))
    _lock({"x": {"revision": None, "n_tasks": 0, "tasks_sha256": "", "sources": [_src(b"a,b\n1,2\n")]}})
    _serve(monkeypatch, b"a,b\n1,2\n")
    path = sources.fetch(URL)
    assert path == str(tmp_path / "_sources" / "example.org" / "data" / "a.csv")
    _serve(monkeypatch, b"never fetched again")
    assert sources.fetch(URL) == path
    with open(path, "w") as f:  # the cached copy edited: refused at the next use
        f.write("a,b\n1,3\n")
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # (a real edit is not within the mtime granularity)
    with pytest.raises(datalock.DataMismatch):
        sources.fetch(URL)


def test_jammeval_shards_are_checked_against_the_lock(tmp_path, monkeypatch):
    shard = tmp_path / "Heron-Bench-Refined" / "test-00000-of-00001.parquet"
    shard.parent.mkdir()
    datasets.Dataset.from_list([{"original_id": 1, "question": "q", "answer": "a"}]).to_parquet(str(shard))
    monkeypatch.setenv("JAMMEVAL_DIR", str(tmp_path))
    monkeypatch.setattr(sources, "download_jammeval", lambda *a, **k: None)
    url = sources.jammeval_url("Heron-Bench-Refined", shard.name)
    with pytest.raises(datalock.DataMismatch, match="not in datasets.lock"):
        sources.jammeval("Heron-Bench-Refined")
    _lock({"heronbench": {"sources": [_src(shard.read_bytes(), url)]}})
    assert sources.jammeval("Heron-Bench-Refined")[0]["original_id"] == 1
    (shard.parent / "extra-00000-of-00001.parquet").write_bytes(b"x")  # a shard the lock does not list
    with pytest.raises(datalock.DataMismatch, match="extra-00000"):
        sources.jammeval("Heron-Bench-Refined")
    os.remove(shard.parent / "extra-00000-of-00001.parquet")
    _lock({"heronbench": {"sources": [_src(shard.read_bytes(), url),
                                      _src(b"y", sources.jammeval_url("Heron-Bench-Refined", "test-00001.parquet"))]}})
    with pytest.raises(datalock.DataMismatch, match="shard missing"):  # a locked shard absent locally
        sources.jammeval("Heron-Bench-Refined")


# ---------------------------------------------------------------- the task digest
class Lockable(Arith):
    name = "lockable"


def _entry(ds, **over):
    return {"revision": ds.revision, **datalock.digest(ds.load()), **over}


def test_check_dataset_ok_mismatch_allow_missing_and_refs():
    ds = Lockable()
    tasks = ds.load()
    with pytest.raises(datalock.DataMismatch, match="no datasets.lock entry"):
        datalock.check_dataset(ds, tasks)
    _lock({"lockable": {"missing": "gated without a token"}})
    with pytest.raises(datalock.DataMismatch, match="gated without a token"):
        datalock.check_dataset(ds, tasks)
    _lock({"lockable": _entry(ds)})
    assert datalock.check_dataset(ds, tasks) == {"data_lock": "ok"}
    _lock({"lockable": _entry(ds, n_tasks=4, tasks_sha256="0" * 64)})
    with pytest.raises(datalock.DataMismatch) as e:
        datalock.check_dataset(ds, tasks)
    assert "n_tasks: expected 4, got 3" in str(e.value) and "tasks_sha256: expected 000" in str(e.value)
    rec = datalock.check_dataset(ds, tasks, allow=True)
    assert rec["data_lock"] == "mismatch" and {p["what"] for p in rec["data_mismatch"]} == {"n_tasks", "tasks_sha256"}
    ds.ref = "x"  # -d name@ref: no entry for another revision
    assert datalock.check_dataset(ds, tasks) == {"data_lock": "none"}


def test_run_refuses_a_mismatch_and_records_the_allow_flag(job_env, capsys):
    s = job_env
    ds = Arith()
    _lock({"arith": _entry(ds, tasks_sha256="0" * 64)})
    assert s["run"]() == 2
    err = capsys.readouterr().err
    assert "differs from datasets.lock" in err and "tasks_sha256: expected 0000" in err
    assert s["run"]("--allow-data-mismatch") == 0
    lock = s["store"]().read_lock()
    d = lock["datasets"]["arith"]
    assert d["data_lock"] == "mismatch" and d["data_mismatch"][0]["what"] == "tasks_sha256"
    assert lock["sessions"][-1]["runtime"]["allow_data_mismatch"] is True
    _lock({"arith": _entry(ds)})  # the lock fixed: a resume keeps the recorded mismatch
    assert s["run"]() == 0
    assert s["store"]().read_lock()["datasets"]["arith"]["data_lock"] == "mismatch"
    assert s["run"](name="ok") == 0
    assert s["store"]("ok").read_lock()["datasets"]["arith"]["data_lock"] == "ok"


def test_run_without_an_entry_fails_in_preflight(job_env, capsys, monkeypatch):
    monkeypatch.setattr(datalock, "entry", lambda name: {"missing": "evaluator unavailable"} if name == "arith" else None)
    assert job_env["run"]() == 2
    assert "no datasets.lock entry: evaluator unavailable" in capsys.readouterr().err


def test_a_source_mismatch_refuses_the_run(job_env, capsys, monkeypatch, tmp_path):
    class Fetching(Arith):
        def load(self):
            sources.fetch_bytes(URL)
            return super().load()

    monkeypatch.setitem(dataset_mod.DATASETS, "arith", Fetching)
    _lock({"arith": {"revision": Arith.revision, **datalock.digest(Arith().load()), "sources": [_src(b"old")]}})
    _serve(monkeypatch, b"new")
    assert job_env["run"]() == 2
    assert "source file https://example.org/data/a.csv does not match datasets.lock" in capsys.readouterr().err
    assert job_env["run"]("--allow-data-mismatch") == 0
    (m,) = job_env["store"]().read_lock()["datasets"]["arith"]["data_mismatch"]
    assert m["source"] == URL and m["got"] == hashlib.sha256(b"new").hexdigest()


# ---------------------------------------------------------------- sem datasets lock / check
def test_datasets_lock_writes_and_checks(monkeypatch, capsys):
    from simple_evals_mm import cli

    class Broken(Arith):
        name = "broken"

        def load(self):
            raise RuntimeError("gated: no token")

    class Built(Arith):  # data built earlier from an unpinned file: its digest comes from the upstream file
        name = "built"

        def unpinned_sources(self):
            return [URL]

    for name, cls in (("lockable", Lockable), ("broken", Broken), ("built", Built)):
        monkeypatch.setitem(dataset_mod.DATASETS, name, cls)
    names = ["lockable", "broken", "built"]
    remote = lambda url, tls=True: (hashlib.sha256(b"upstream").hexdigest(), 8)
    rows, code = dataset_admin.lock(names, shared=False, remote_sha256=remote)
    assert code == 0 and [r["status"] for r in rows] == ["new", "missing", "new"]
    lock = json.loads(datalock.LOCK_PATH.read_text())
    assert lock["datasets"]["lockable"] == _entry(Lockable())
    assert lock["datasets"]["broken"] == {"missing": "RuntimeError: gated: no token"}
    assert lock["datasets"]["built"]["sources"] == [{"url": URL, "sha256": hashlib.sha256(b"upstream").hexdigest(), "size": 8}]
    assert dataset_admin.lock(names, check=True, shared=False)[1] == 0  # missing is reported, not a failure
    monkeypatch.setattr(Lockable, "questions", ["2+2?"])  # the data changed
    rows, code = dataset_admin.lock(names, check=True, shared=False)
    assert code == 1 and rows[0]["status"] == "mismatch" and "n_tasks: expected 3, got 1" in rows[0]["note"]
    assert json.loads(datalock.LOCK_PATH.read_text()) == lock  # --check never writes
    assert cli.main(["datasets", "lock", "-d", "lockable", "--check"]) == 1
    assert cli.main(["datasets", "lock", "-d", "lockable"]) == 0
    assert cli.main(["datasets", "lock", "-d", "lockable", "--check"]) == 0
    capsys.readouterr()


def test_data_arg_has_its_own_entry(monkeypatch, capsys):
    """A data arg with a non-default value (MathVision's split) is locked and checked as `name[key=value]`: written by
    `sem datasets lock -d name --dataset-arg name.key=value` (or `-d 'name[key=value]'`), rechecked with all datasets."""
    from typing import ClassVar

    from simple_evals_mm import cli

    class Split(Arith):
        name = "splitty"
        accepted_args: ClassVar[dict] = {"split": str}
        data_args: ClassVar[dict] = {"split": "small"}

        def load(self):
            return super().load()[: 1 if self.args.get("split", "small") == "small" else 3]

    monkeypatch.setitem(dataset_mod.DATASETS, "splitty", Split)
    assert cli.main(["datasets", "lock", "-d", "splitty"]) == 0
    assert cli.main(["datasets", "lock", "-d", "splitty", "--dataset-arg", "splitty.split=big"]) == 0
    lock = json.loads(datalock.LOCK_PATH.read_text())["datasets"]
    assert lock["splitty"]["n_tasks"] == 1 and lock["splitty[split=big]"]["n_tasks"] == 3
    big = Split(split="big")
    assert datalock.check_dataset(big, big.load()) == {"data_lock": "ok"}
    assert datalock.check_dataset(Split(split="small"), Split().load()) == {"data_lock": "ok"}  # the default: plain entry
    assert cli.main(["datasets", "lock", "-d", "splitty[split=big]", "--check"]) == 0
    monkeypatch.setattr(dataset_mod, "DATASETS", {"splitty": Split})
    rows, code = dataset_admin.lock(None, check=True, shared=False)  # all = registered + keyed entries
    assert code == 0 and [r["dataset"] for r in rows] == ["splitty", "splitty[split=big]"]
    _lock({"splitty": lock["splitty"]})
    with pytest.raises(datalock.DataMismatch, match=r"no datasets.lock entry splitty\[split=big\]"):
        datalock.check_dataset(big, big.load())
    capsys.readouterr()


def test_upstream_check_of_locked_sources():
    big = "https://example.org/big.tar"
    _lock({"x": {"sources": [_src(b"small"), {"url": big, "sha256": "f" * 64, "size": 10**9},
                             {"url": sources.jammeval_url("S", "a.parquet"), "sha256": "e" * 64, "size": 1}]}})
    rows = dataset_admin.check_sources(remote_sha256=lambda u, tls=True: (hashlib.sha256(b"small").hexdigest(), 5),
                                       remote_head=lambda u, tls=True: {"size": 10**9 + 1, "etag": "e1"})
    assert [(r["source"], r["status"]) for r in rows] == [(f"url:{URL}", "current"), (f"url:{big}", "outdated")]
    assert "size only" in rows[1]["note"]  # JAMMEval shards (pinned commit) are skipped
    rows = dataset_admin.check_sources(remote_sha256=lambda u, tls=True: ("0" * 64, 5),
                                       remote_head=lambda u, tls=True: {"size": 10**9})
    assert [r["status"] for r in rows] == ["outdated", "current"]


# ---------------------------------------------------------------- vendored nltk data
def test_nltk_data_is_vendored(tmp_path, monkeypatch):
    """nltk reads only data/nltk_data, never downloads; the zips are checked against `_shared.nltk`; their digest is
    in the IFEval / IFBench verifier_lock deps."""
    import zipfile

    import nltk

    monkeypatch.setattr(sources, "NLTK_DIR", str(tmp_path / "nltk_data"))
    monkeypatch.setattr(nltk.data, "path", list(nltk.data.path))
    monkeypatch.setattr(nltk, "download", nltk.download)
    zips = {}
    for pkg in sources.NLTK_PACKAGES:  # a fake package: one file in <name>/ inside the zip
        p = tmp_path / "src" / (pkg + ".zip")
        p.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(pkg.split("/")[1] + "/README", pkg)
        zips[sources.nltk_url(pkg)] = p.read_bytes()
    _lock(shared={"nltk": {"commit": sources.NLTK_COMMIT, "files": [_src(b, u) for u, b in zips.items()]}})
    import requests

    monkeypatch.setattr(requests, "get", lambda url, **k: _Resp(zips[url]))
    from simple_evals_mm.tasks import ifbench, ifeval

    ds = dataset_mod.get_dataset("ifeval")
    assert "missing" in ds.preflight(verify=True)[0] and ds.preflight(verify=False) == []
    ds.download()
    assert ds.preflight(verify=True) == [] and dataset_mod.get_dataset("ifbench").preflight(verify=True) == []
    assert all(os.path.isdir(tmp_path / "nltk_data" / pkg) for pkg in sources.NLTK_PACKAGES)
    sources.use_nltk_data()
    assert nltk.data.path == [str(tmp_path / "nltk_data")] and nltk.download("punkt") is False  # no download
    assert nltk.data.find("corpora/stopwords/README")
    state = sources.nltk_state()
    assert dataset_mod.verifier_deps(ds)["nltk_data"] == state == {
        pkg: hashlib.sha256(zips[sources.nltk_url(pkg)]).hexdigest() for pkg in sources.NLTK_PACKAGES}
    assert dataset_mod.verifier_deps(ifbench.IFBench())["nltk_data"] == state
    (tmp_path / "nltk_data" / "corpora" / "stopwords.zip").write_bytes(b"other")  # a different package
    assert "sha256" in ds.preflight(verify=True)[0]
    assert ifeval.check("p", ["punctuation:no_comma"], [{}], "no commas") and nltk.data.path == [str(tmp_path / "nltk_data")]


# ---------------------------------------------------------------- the committed lock
def test_committed_lock_covers_every_registered_dataset():
    """Every registered dataset has an entry (revision, n_tasks, tasks_sha256) or an explicit `missing` reason; the
    shared artifacts are pinned; the file ships inside the package (wheel)."""
    path = datalock.COMMITTED
    assert path.parent.name == "simple_evals_mm" and path.exists()
    lock = json.loads(path.read_text())
    assert lock["format"] == datalock.FORMAT
    for key in lock["datasets"]:  # keyed entries name a registered dataset and its data args
        name, args = dataset_mod.parse_lock_key(key)
        assert name in dataset_mod.DATASETS and set(args) <= set(dataset_mod.dataset_class(name).data_args), key
        assert dataset_mod.get_dataset(name, **args).lock_key == key
    for name in dataset_mod.DATASETS:
        e = lock["datasets"].get(name)
        assert e is not None, f"{name}: no datasets.lock entry (`sem datasets lock -d {name}`)"
        if "missing" in e:
            assert e["missing"], name
            continue
        ds = dataset_mod.get_dataset(name)
        assert e["revision"] == ds.revision and e["n_tasks"] > 0 and len(e["tasks_sha256"]) == 64, name
        for s in e.get("sources", []):
            assert s["url"].startswith("https://") and len(s["sha256"]) == 64 and s["size"] > 0, (name, s)
        if ds.revision is None:  # unversioned upstream: every source file pinned by sha256
            assert e.get("sources"), name
    assert {f["url"] for f in lock["_shared"]["nltk"]["files"]} == {sources.nltk_url(p) for p in sources.NLTK_PACKAGES}
    assert len(lock["_shared"]["omnidocbench_eval_sif"]["sha256"]) == 64
