"""Every text file read or write names its encoding (L-32). Windows defaults to cp1252, so
an implicit encoding passes on macOS and Linux and breaks on Windows CI or for Windows users."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CALLS = {"read_text", "write_text", "open"}


def offenders():
    found = []
    for base in ("src", "scripts", "tests"):
        for path in (ROOT / base).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name not in CALLS:
                    continue
                if (
                    name == "open"
                    and isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "os"
                ):
                    continue  # os.open returns a file descriptor: no text, no encoding
                if name == "open":
                    at = (
                        0 if isinstance(f, ast.Attribute) else 1
                    )  # Path.open(mode) vs open(f, mode)
                    mode = (
                        node.args[at]
                        if len(node.args) > at
                        else next((k.value for k in node.keywords if k.arg == "mode"), None)
                    )
                    if isinstance(mode, ast.Constant) and "b" in str(mode.value):
                        continue  # binary
                    if (
                        isinstance(f, ast.Attribute)
                        and f.attr == "open"
                        and not isinstance(f.value, ast.Name | ast.Call | ast.Attribute)
                    ):
                        continue
                if any(k.arg == "encoding" for k in node.keywords):
                    continue
                positional = {"read_text": 1, "write_text": 2, "open": 4}[name]
                if len(node.args) >= positional:
                    continue  # encoding passed positionally, e.g. read_text("utf-8")
                found.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}()")
    return found


def test_every_text_io_call_names_its_encoding():
    assert offenders() == []


def test_path_open_in_binary_mode_is_not_flagged(tmp_path, monkeypatch):
    import sys

    src = (
        'import os\nfrom pathlib import Path\nPath("x").open("ab")\nPath("x").open("a")\n'
        'open("x", "rb")\nos.open("x", os.O_RDONLY)\n'
    )
    for d in ("src", "scripts", "tests"):
        (tmp_path / d).mkdir()
    (tmp_path / "src" / "m.py").write_text(src, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert offenders() == ["src/m.py:4 open()"]  # only the text-mode Path.open
