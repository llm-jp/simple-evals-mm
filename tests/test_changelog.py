from simple_evals_mm import changelog


def _level(name, sc, tasks, parity):
    cells = {t: {"score": v, "ci95": [v - 1, v + 1],
                 **({"parity": {"result": dict(parity)[(name, t)]}} if (name, t) in dict(parity) else {})}
             for t, v in sc.items()}
    return {"tasks": cells, "benchmarks": {"Overall": {"score": sum(sc.values()) / len(sc) if len(sc) == len(tasks) else None}}}


def _data(settings, tasks=("a", "b"), overall=None, parity=()):
    models = []
    for name, labels in settings.items():
        m = {"name": name, "maxLabel": labels[0][0], "max": _level(name, labels[0][1], tasks, parity)}
        if len(labels) > 1:
            m |= {"lowLabel": labels[1][0], "low": _level(name, labels[1][1], tasks, parity)}
        models.append(m)
    return {"models": models, "tasks": [{"key": t, "label": t.upper()} for t in tasks],
            "benchmarks": [{"key": "Overall", "overall": True, "tasks": list(overall or tasks)}]}


def test_settings_tasks_overall_and_moved_scores():
    old = _data({"M": [("max", {"a": 50.0, "b": 60.0})], "Gone": [("max", {"a": 1.0, "b": 2.0})]})
    new = _data({"M": [("max", {"a": 55.0}), ("low", {"a": 40.0})]}, tasks=("a", "b", "c"), overall=("a", "b", "c"))
    texts = [(i["category"], i["text"]) for i in changelog.diff(old, new)]
    assert ("Models", "Added: M (low).") in texts and ("Models", "No longer on the leaderboard: Gone (max).") in texts
    assert ("Models", "Not in Overall (a benchmark missing): M (max).") in texts
    assert ("Tasks", "Added: C.") in texts and ("Tasks", "Overall is the mean of 3 benchmarks (was 2).") in texts
    assert ("Scoring", "M (max), A: 50.0 → 55.0.") in texts


def test_small_moves_and_unchanged_builds_are_quiet():
    old = _data({"M": [("max", {"a": 50.0, "b": 60.0})]})
    new = _data({"M": [("max", {"a": 51.0, "b": 60.0})]})
    assert changelog.diff(old, new) == []


def test_entry_is_a_draft_and_inserts_first(tmp_path):
    js = changelog.entry([{"category": "Tasks", "text": 'Added: "X".'}], ["abc123 fix the parser"], when="2026-11-15")
    assert "draft: true" in js and '"Added: \\"X\\"."' in js and "Code change (review the wording): abc123" in js
    p = tmp_path / "changelog.js"
    p.write_text('const CHANGELOG = [\n  {\n    date: "2026-10-01",\n    items: [],\n  },\n];\n')
    changelog.insert(str(p), js)
    s = p.read_text()
    assert s.index('"2026-11-15"') < s.index('"2026-10-01"')
