"""Golden snapshots of the dataset prompts / rewards and the sampler request bodies.

`check(group, key, value)` compares `value` (as JSON) with `tests/golden/<group>.json[key]`. The snapshots were taken
from the v0.5 outputs when they still equalled v0.4's (the v0.4 parity tests passed on the same inputs), so a change
here is a change of what the framework sends or how it scores. Regenerate after an intended change with

    uv run pytest tests/test_datasets_mcq.py ... --update-golden

which rewrites the keys the run touches (review the diff of tests/golden/ before committing).
"""

from __future__ import annotations

import hashlib
import json
import re
import string
from pathlib import Path

DIR = Path(__file__).parent / "golden"
UPDATE = False  # set by conftest.py from --update-golden


def _default(o):
    if isinstance(o, bytes):
        return "sha256:" + hashlib.sha256(o).hexdigest()[:16]
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def normalize(value):
    """JSON round trip (tuples -> lists, bytes -> short sha256)."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=_default))


def fields(template: str, text: str, style: str = "format") -> dict | str:
    """The values a filled-in `template` received to give `text`, so a long judge template stays out of every snapshot
    (the snapshot keeps the template's sha256 instead). style: "format" (str.format), "percent" (`%(name)s`), "replace"
    (`{name}` substituted with str.replace). Returns `text` itself when it does not match the template."""
    if style == "format":
        parts = [(lit, name) for lit, name, _, _ in string.Formatter().parse(template)]
    else:
        pieces = re.split(r"%\((\w+)\)s" if style == "percent" else r"\{(\w+)\}", template)
        parts = [(lit.replace("%%", "%") if style == "percent" else lit, name)
                 for lit, name in zip(pieces[::2], [*pieces[1::2], None], strict=True)]
    pattern = "".join(re.escape(lit) + ("(.*?)" if name is not None else "") for lit, name in parts)
    m = re.fullmatch(pattern, text, re.DOTALL)
    names = [name for _, name in parts if name is not None]
    return dict(zip(names, m.groups(), strict=True)) if m else text


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _dump(v, indent=0) -> str:
    """Indented JSON with the short leaves (scalars, lists of scalars, small dicts) on one line."""
    flat = json.dumps(v, ensure_ascii=False, sort_keys=True)
    if not isinstance(v, (dict, list)) or len(flat) <= 100 or not v:
        return flat
    pad = " " * (indent + 1)
    if isinstance(v, list):
        return "[\n" + ",\n".join(pad + _dump(x, indent + 1) for x in v) + "\n" + " " * indent + "]"
    items = (pad + json.dumps(k, ensure_ascii=False) + ": " + _dump(v[k], indent + 1) for k in sorted(v))
    return "{\n" + ",\n".join(items) + "\n" + " " * indent + "}"


def check(group: str, key: str, value) -> None:
    path = DIR / f"{group}.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    value = normalize(value)
    if UPDATE:
        data[key] = value
        DIR.mkdir(exist_ok=True)
        path.write_text(_dump(data) + "\n")
        return
    assert key in data, f"no golden snapshot {group}[{key!r}]: run pytest --update-golden"
    assert value == data[key], f"differs from golden snapshot {group}[{key!r}] (intended? pytest --update-golden)"
