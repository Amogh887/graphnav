from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import write_graph


class TestMonoSubcommandDispatch:
    def test_map_dispatched_to_run_map(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["codex-graph", "map", "--root", str(tmp_path), "--dry-run"])
        calls = []
        monkeypatch.setattr(
            "codex_graph.multirepo.shutil.which", lambda _: "/graphify"
        )
        with pytest.raises(SystemExit) as exc:
            from codex_graph.cli import main
            main()
        assert exc.value.code in (0, 1)

    def test_watch_dispatched_to_run_watch(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["codex-graph", "watch", "--root", str(tmp_path)])
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: None)
        with pytest.raises(SystemExit) as exc:
            from codex_graph.cli import main
            main()
        assert exc.value.code == 1

    def test_map_help_exits_0(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["codex-graph", "map", "--help"])
        with pytest.raises(SystemExit) as exc:
            from codex_graph.cli import main
            main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "map" in out.lower() or "monorepo" in out.lower()

    def test_watch_help_exits_0(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["codex-graph", "watch", "--help"])
        with pytest.raises(SystemExit) as exc:
            from codex_graph.cli import main
            main()
        assert exc.value.code == 0

    def test_map_dry_run_flag(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "svc-a").mkdir()
        (tmp_path / "svc-a" / "pyproject.toml").touch()
        (tmp_path / "svc-b").mkdir()
        (tmp_path / "svc-b" / "package.json").touch()
        monkeypatch.setattr(sys, "argv", ["codex-graph", "map", "--root", str(tmp_path), "--dry-run"])
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        with pytest.raises(SystemExit) as exc:
            from codex_graph.cli import main
            main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "svc-a" in out
        assert "[dry-run]" in out

    def test_backend_flag_forwarded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["codex-graph", "map", "--root", str(tmp_path), "--backend", "openai", "--dry-run"])
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        with pytest.raises(SystemExit):
            from codex_graph.cli import main
            main()


class TestExistingPromptPathUnaffected:
    def test_list_files_uses_existing_graph(self, tmp_path, monkeypatch, capsys):
        graph_dir = tmp_path / "graphify-out"
        graph_dir.mkdir()
        write_graph(
            graph_dir / "graph.json",
            nodes=[{"id": "n1", "label": "user model schema", "source_file": "models.py",
                    "file_type": "code", "community": 0}],
        )
        config_file = tmp_path / "config.toml"
        config_file.write_text(f'[graph]\npath = "graphify-out/graph.json"\nproject_root = "."\n')
        monkeypatch.setattr(sys, "argv", [
            "codex-graph", "--config", str(config_file), "--graph", str(graph_dir / "graph.json"),
            "--list-files", "user model"
        ])
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            from codex_graph.cli import main
            main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "models.py" in out

    def test_no_context_flag_skips_graph(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "codex-graph", "--no-context", "--dry-run", "my task"
        ])
        graph_dir = tmp_path / "graphify-out"
        graph_dir.mkdir()
        write_graph(graph_dir / "graph.json")
        config_file = tmp_path / "config.toml"
        config_file.write_text(f'[graph]\npath = "graphify-out/graph.json"\nproject_root = "."\n')
        monkeypatch.setattr(sys, "argv", [
            "codex-graph", "--config", str(config_file), "--graph", str(graph_dir / "graph.json"),
            "--no-context", "--dry-run", "do something"
        ])
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            from codex_graph.cli import main
            main()
        assert exc.value.code == 0

    def test_prompt_with_missing_graph_exits_2(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "codex-graph", "--graph", str(tmp_path / "nonexistent.json"), "my prompt"
        ])
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            from codex_graph.cli import main
            main()
        assert exc.value.code == 2
