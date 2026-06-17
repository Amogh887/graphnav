from __future__ import annotations

import os
import time
from unittest.mock import MagicMock

import pytest

from graphnav import multirepo
from graphnav.config import load_config_report
from graphnav.multirepo import graph_is_stale, maybe_auto_rebuild
from tests.conftest import write_graph


@pytest.fixture
def allow_auto(monkeypatch):
    monkeypatch.delenv("GRAPHNAV_NO_AUTO_REBUILD", raising=False)


@pytest.fixture
def fake_popen(monkeypatch):
    proc = MagicMock()
    proc.pid = 99999999
    popen = MagicMock(return_value=proc)
    monkeypatch.setattr(multirepo.subprocess, "Popen", popen)
    monkeypatch.setattr(multirepo, "_git_sha", lambda root: None)
    monkeypatch.setattr("graphnav.graph_cache._git_recency", lambda root: {})
    return popen


def _make_repo(tmp_path, stale: bool):
    src = tmp_path / "app.py"
    src.write_text("def main():\n    pass\n")
    write_graph(tmp_path / "graphify-out" / "graph.json", [], [])
    graph = tmp_path / "graphify-out" / "graph.json"
    if stale:
        os.utime(graph, (time.time() - 100, time.time() - 100))
    else:
        os.utime(src, (time.time() - 100, time.time() - 100))
    return tmp_path


class TestStaleness:
    def test_missing_graph_is_stale(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1\n")
        assert graph_is_stale(str(tmp_path)) is True

    def test_fresh_graph_not_stale(self, tmp_path):
        _make_repo(tmp_path, stale=False)
        assert graph_is_stale(str(tmp_path)) is False

    def test_edited_source_is_stale(self, tmp_path):
        _make_repo(tmp_path, stale=True)
        assert graph_is_stale(str(tmp_path)) is True


class TestMaybeAutoRebuild:
    def test_spawns_on_stale(self, tmp_path, allow_auto, fake_popen):
        _make_repo(tmp_path, stale=True)
        assert maybe_auto_rebuild(str(tmp_path)) is True
        fake_popen.assert_called_once()
        argv = fake_popen.call_args[0][0]
        assert argv[-2:] == ["--root", str(tmp_path)]
        assert "map" in argv
        pid_file = tmp_path / "graphify-out" / ".graphnav-rebuild.pid"
        assert pid_file.read_text() == "99999999"

    def test_no_spawn_when_fresh(self, tmp_path, allow_auto, fake_popen):
        _make_repo(tmp_path, stale=False)
        assert maybe_auto_rebuild(str(tmp_path)) is False
        fake_popen.assert_not_called()

    def test_no_spawn_when_disabled(self, tmp_path, allow_auto, fake_popen):
        _make_repo(tmp_path, stale=True)
        assert maybe_auto_rebuild(str(tmp_path), enabled=False) is False
        fake_popen.assert_not_called()

    def test_env_escape_hatch(self, tmp_path, monkeypatch, fake_popen):
        monkeypatch.setenv("GRAPHNAV_NO_AUTO_REBUILD", "1")
        _make_repo(tmp_path, stale=True)
        assert maybe_auto_rebuild(str(tmp_path)) is False
        fake_popen.assert_not_called()

    def test_skips_when_rebuild_running(self, tmp_path, allow_auto, fake_popen):
        _make_repo(tmp_path, stale=True)
        pid_file = tmp_path / "graphify-out" / ".graphnav-rebuild.pid"
        pid_file.write_text(str(os.getpid()))
        assert maybe_auto_rebuild(str(tmp_path)) is False
        fake_popen.assert_not_called()

    def test_cooldown_after_dead_rebuild(self, tmp_path, allow_auto, fake_popen):
        _make_repo(tmp_path, stale=True)
        pid_file = tmp_path / "graphify-out" / ".graphnav-rebuild.pid"
        pid_file.write_text("999999999")
        assert maybe_auto_rebuild(str(tmp_path)) is False
        fake_popen.assert_not_called()
        old = time.time() - 120
        os.utime(pid_file, (old, old))
        assert maybe_auto_rebuild(str(tmp_path)) is True
        fake_popen.assert_called_once()


class TestPackNotes:
    def test_inline_pack_notes_rebuild(self, tmp_path, allow_auto, fake_popen, monkeypatch):
        _make_repo(tmp_path, stale=True)
        write_graph(
            tmp_path / "graphify-out" / "graph.json",
            [{"id": "m", "label": "main", "source_file": "app.py",
              "file_type": "code", "source_location": "L1", "community": 0}],
            [],
        )
        graph = tmp_path / "graphify-out" / "graph.json"
        os.utime(graph, (time.time() - 100, time.time() - 100))
        pack = multirepo.build_context_pack_inline(str(tmp_path), "main")
        assert "automatic graph rebuild started" in pack

    def test_no_graph_pack_reports_building(self, tmp_path, allow_auto, fake_popen):
        (tmp_path / "app.py").write_text("def main():\n    pass\n")
        pack = multirepo.build_context_pack_inline(str(tmp_path), "main")
        assert "being built automatically" in pack


class TestConfigKnob:
    def test_auto_rebuild_default_true(self):
        cfg, _, _ = load_config_report()
        assert cfg.mono.auto_rebuild is True

    def test_auto_rebuild_from_toml(self, tmp_path, monkeypatch):
        (tmp_path / "config.toml").write_text("[mono]\nauto_rebuild = false\n")
        monkeypatch.chdir(tmp_path)
        cfg, _, warnings = load_config_report()
        assert cfg.mono.auto_rebuild is False
        assert warnings == []

    def test_wrong_typed_auto_rebuild(self, tmp_path, monkeypatch):
        (tmp_path / "config.toml").write_text('[mono]\nauto_rebuild = "yes"\n')
        monkeypatch.chdir(tmp_path)
        cfg, _, warnings = load_config_report()
        assert cfg.mono.auto_rebuild is True
        assert any("true or false" in w for w in warnings)
