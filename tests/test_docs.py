"""The documentation stays in step with the code: every `sem ...` command line in the docs parses with the current
CLI (an option renamed or removed fails here), and every relative link points to an existing file."""

import re
import shlex
from pathlib import Path

import pytest

from simple_evals_mm import cli

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", ROOT / "CONTRIBUTING.md",
        *sorted(p for p in (ROOT / "docs").rglob("*.md") if "design" not in p.parts)]


def _commands(path: Path) -> list[str]:
    """The `sem ...` invocations in the fenced shell blocks of a Markdown file (continuations joined, comments and
    the `uv run --frozen` prefix dropped)."""
    out = []
    for block in re.findall(r"```(?:bash|sh)\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL):
        for line in block.replace("\\\n", " ").splitlines():
            line = re.split(r" #| [12]?> ", line, maxsplit=1)[0].strip()  # comments, shell redirects
            m = re.match(r"^(?:uv run --frozen(?: --extra [\w-]+)? )?sem (.*)$", line)
            if m and " | " not in m.group(1):  # `datasets list | download | check` lists alternatives
                out.append(m.group(1))
    return out


CASES = [(p.relative_to(ROOT).as_posix(), c) for p in DOCS for c in _commands(p)]


def test_the_docs_have_commands():
    assert len(CASES) > 30


@pytest.mark.parametrize(("doc", "command"), CASES)
def test_doc_command_parses(doc, command):
    from simple_evals_mm.serving import serve

    argv = shlex.split(command)
    if argv and argv[0] == "serve":  # engine arguments after `--`, as cli.main splits them
        argv, _ = serve.split_extra(argv)
    try:
        cli.make_parser().parse_args(argv)
    except SystemExit as e:
        if e.code != 0:  # --version / --help exit 0
            pytest.fail(f"{doc}: `sem {command}` does not parse (exit {e.code})")


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_exist(doc):
    text = doc.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)\s]+)\)", text) + re.findall(r'(?:href|src)="([^"]+)"', text):
        if re.match(r"^(?:https?:|mailto:|#)", target):
            continue
        path = (doc.parent / target.split("#", 1)[0]).resolve()
        assert path.exists(), f"{doc.relative_to(ROOT)}: link to {target} (no such file)"
