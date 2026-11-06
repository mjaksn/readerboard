"""The script that writes ``docs/openapi.json``, and what it does with its arguments.

It took any argument as the file to write. ``-`` was one, and the 0.7.0 release
committed a copy of the description under that name; ``--help`` was another,
and wrote 63 KB to a file called ``--help`` rather than saying how to use it.

The script is not importable as a module. It lives in ``scripts/`` with no
package around it, which is why this loads it by path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dump_openapi.py"


@pytest.fixture(scope="module")
def dump() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dump_openapi", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_help_says_how_to_use_it_and_writes_nothing(dump, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_:
        dump.main(["--help"])

    assert exit_.value.code == 0
    assert "usage: dump_openapi.py" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_an_option_it_does_not_know_is_refused_rather_than_written_to(
    dump, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_:
        dump.main(["--check"])

    assert exit_.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_a_path_gets_the_description(dump, tmp_path):
    destination = tmp_path / "docs" / "openapi.json"

    assert dump.main([str(destination)]) == 0

    assert "paths" in json.loads(destination.read_text(encoding="utf-8"))


@pytest.mark.parametrize("argv", [[], ["-"]], ids=["no path", "a hyphen"])
def test_no_path_or_a_hyphen_goes_to_stdout(dump, tmp_path, monkeypatch, capsys, argv):
    monkeypatch.chdir(tmp_path)

    assert dump.main(argv) == 0

    assert "paths" in json.loads(capsys.readouterr().out)
    assert list(tmp_path.iterdir()) == []
