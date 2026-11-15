import tomllib

import pytest

from assay_pipeline import __version__
from assay_pipeline.cli import main
from harness_lib import ROOT


def test_cli_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"assay {__version__}"


def test_cli_without_command_prints_help(capsys):
    assert main([]) == 0
    assert "usage: assay" in capsys.readouterr().out


@pytest.mark.contract
def test_pyproject_metadata():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert meta["name"] == "assay-pipeline"
    assert meta["license"] == "MIT"
    assert meta["scripts"]["assay"] == "assay_pipeline.cli:main"
    assert meta["requires-python"] == ">=3.11,<3.14"
    from assay_pipeline.doctor import SUPPORTED  # one support range, stated once (L-37)

    assert SUPPORTED == ((3, 11), (3, 12), (3, 13))
    assert "MIT License" in (ROOT / "LICENSE").read_text(encoding="utf-8")
