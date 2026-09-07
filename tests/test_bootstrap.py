"""The bootstrap script: mirror + full index, without any LLM call."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bootstrap_repo.py"


def load_script():
    spec = importlib.util.spec_from_file_location("bootstrap_repo", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bootstrap_repo"] = module
    spec.loader.exec_module(module)
    return module


bootstrap = load_script()


@pytest.fixture
def populated(sandbox):
    sandbox.write("app/listener.py", "class Listener:\n    def handle(self):\n        pass\n")
    sandbox.write("app/util.py", "def helper():\n    return 1\n")
    sandbox.write("web/index.ts", "export function boot() {}\n")
    sandbox.write("node_modules/dep/index.js", "module.exports = 1;\n")
    sandbox.write("README.md", "# docs\n")
    sandbox.commit("initial")
    return sandbox


def test_bootstrap_builds_the_repo_map(populated, tmp_path, capsys) -> None:
    data_dir = tmp_path / "data"
    code = bootstrap.main(
        [
            "--project-id",
            "5",
            "--repo-url",
            populated.url,
            "--ref",
            "main",
            "--project-name",
            "acme/app",
            "--data-dir",
            str(data_dir),
        ]
    )
    assert code == 0

    assert (data_dir / "repos" / "5.git" / "HEAD").exists()
    index = data_dir / "indexes" / "5" / "repo-map.json"
    assert index.exists()

    from app.models import RepoMap

    repo_map = RepoMap.model_validate_json(index.read_text())
    assert set(repo_map.files) == {"app/listener.py", "app/util.py", "web/index.ts"}
    assert "Listener.handle" in repo_map.files["app/listener.py"].symbol_names
    assert repo_map.project_name == "acme/app"

    output = capsys.readouterr().out
    assert "No LLM calls were made." in output
    assert "indexed files      : 3" in output


def test_bootstrap_is_repeatable(populated, tmp_path) -> None:
    args = ["--project-id", "6", "--repo-url", populated.url, "--data-dir", str(tmp_path / "d")]
    assert bootstrap.main(args) == 0
    populated.write("app/extra.py", "def extra():\n    return 2\n")
    populated.commit("second")
    assert bootstrap.main(args) == 0

    from app.models import RepoMap

    index = tmp_path / "d" / "indexes" / "6" / "repo-map.json"
    assert "app/extra.py" in RepoMap.model_validate_json(index.read_text()).files


def test_bootstrap_reports_unknown_ref(populated, tmp_path, capsys) -> None:
    code = bootstrap.main(
        [
            "--project-id",
            "7",
            "--repo-url",
            populated.url,
            "--ref",
            "does-not-exist",
            "--data-dir",
            str(tmp_path / "d"),
        ]
    )
    assert code == 2
    assert "not found" in capsys.readouterr().err
