from __future__ import annotations

import json
import os
import subprocess

import pytest

from codex_graph import doctor
from codex_graph.doctor import run_doctor
from codex_graph.graph_cache import clear_memo, load_bundle
from tests.conftest import write_graph


NODES = [
    {"id": "create_incident", "label": "create_incident", "source_file": "svc/views.py",
     "file_type": "code", "source_location": "L2", "community": 0},
]


@pytest.fixture(autouse=True)
def fresh_memo():
    clear_memo()
    yield
    clear_memo()


@pytest.fixture
def fake_graphify(monkeypatch):
    monkeypatch.setattr(doctor, "find_graphify", lambda: "/usr/bin/graphify")
    monkeypatch.setattr(
        doctor.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="graphify 0.9", stderr=""),
    )


@pytest.fixture
def healthy_repo(tmp_path, monkeypatch):
    svc = tmp_path / "svc"
    svc.mkdir()
    (svc / "pyproject.toml").touch()
    write_graph(tmp_path / "graphify-out" / "graph.json", NODES, [])
    meta = tmp_path / "graphify-out" / ".graphnav-meta.json"
    meta.write_text(json.dumps({"built_at": "x", "git_sha": None}))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_KEY", raising=False)
    monkeypatch.setattr(doctor, "_load_env_file", lambda root: {})
    return tmp_path


class TestDoctorAllPass:
    def test_healthy_repo_passes(self, healthy_repo, fake_graphify, monkeypatch, capsys):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        rc = run_doctor(str(healthy_repo))
        out = capsys.readouterr().out
        assert rc == 0
        assert "[ok]" in out
        assert "[fail]" not in out
        assert "fail" in out.splitlines()[-1]


class TestDoctorGraphifyMissing:
    def test_missing_binary_fails(self, healthy_repo, monkeypatch, capsys):
        monkeypatch.setattr(doctor, "find_graphify", lambda: None)
        rc = run_doctor(str(healthy_repo))
        out = capsys.readouterr().out
        assert rc == 1
        assert "[fail]" in out
        assert "pip install graphifyy" in out


class TestDoctorCorruptGraph:
    def test_corrupt_graph_fails(self, tmp_path, fake_graphify, capsys):
        (tmp_path / "graphify-out").mkdir()
        (tmp_path / "graphify-out" / "graph.json").write_text("{not json")
        rc = run_doctor(str(tmp_path))
        out = capsys.readouterr().out
        assert rc == 1
        assert "corrupt" in out


class TestDoctorMissingGraph:
    def test_no_graph_fails(self, tmp_path, fake_graphify, capsys):
        rc = run_doctor(str(tmp_path))
        out = capsys.readouterr().out
        assert rc == 1
        assert "[fail]" in out


class TestDoctorStalenessWarnsButPasses:
    def test_stale_graph_warns(self, healthy_repo, fake_graphify, monkeypatch, capsys):
        meta = healthy_repo / "graphify-out" / ".graphnav-meta.json"
        meta.write_text(json.dumps({"built_at": "x", "git_sha": "a" * 40}))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setattr("codex_graph.multirepo._git_sha", lambda root: "b" * 40)
        monkeypatch.setattr("codex_graph.multirepo._commits_between", lambda root, a, b: 3)
        rc = run_doctor(str(healthy_repo))
        out = capsys.readouterr().out
        assert rc == 0
        assert "[warn]" in out
        assert "behind HEAD" in out


class TestDoctorApiKey:
    def test_key_in_environment(self, healthy_repo, fake_graphify, monkeypatch, capsys):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        run_doctor(str(healthy_repo))
        out = capsys.readouterr().out
        assert "found in environment" in out

    def test_key_in_env_file(self, tmp_path, fake_graphify, monkeypatch, capsys):
        svc = tmp_path / "svc"
        svc.mkdir()
        (svc / "pyproject.toml").touch()
        write_graph(tmp_path / "graphify-out" / "graph.json", NODES, [])
        (tmp_path / "graphify-out" / ".graphnav-meta.json").write_text(
            json.dumps({"built_at": "x", "git_sha": None})
        )
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_KEY", raising=False)
        monkeypatch.setattr(
            doctor, "_load_env_file",
            lambda root: {"ANTHROPIC_API_KEY": "sk-fromenv"},
        )
        run_doctor(str(tmp_path))
        out = capsys.readouterr().out
        assert "found in .env" in out

    def test_no_key_warns(self, healthy_repo, fake_graphify, capsys):
        rc = run_doctor(str(healthy_repo))
        out = capsys.readouterr().out
        assert "[warn] API key" in out
        assert rc == 0

    def test_ollama_needs_no_key(self, tmp_path, fake_graphify, monkeypatch, capsys):
        svc = tmp_path / "svc"
        svc.mkdir()
        (svc / "pyproject.toml").touch()
        (tmp_path / "config.toml").write_text('[mono]\ngraphify_backend = "ollama"\n')
        write_graph(tmp_path / "graphify-out" / "graph.json", NODES, [])
        (tmp_path / "graphify-out" / ".graphnav-meta.json").write_text(
            json.dumps({"built_at": "x", "git_sha": None})
        )
        run_doctor(str(tmp_path), config_path=str(tmp_path / "config.toml"))
        out = capsys.readouterr().out
        assert "no key needed" in out


class TestDoctorNoServices:
    def test_no_services_fails(self, tmp_path, fake_graphify, monkeypatch, capsys):
        write_graph(tmp_path / "graphify-out" / "graph.json", NODES, [])
        (tmp_path / "graphify-out" / ".graphnav-meta.json").write_text(
            json.dumps({"built_at": "x", "git_sha": None})
        )
        monkeypatch.setattr(doctor, "_load_env_file", lambda root: {})
        rc = run_doctor(str(tmp_path))
        out = capsys.readouterr().out
        assert rc == 1
        assert "[fail] services" in out


class TestDoctorCacheStates:
    def test_cold_then_warm(self, healthy_repo, fake_graphify, monkeypatch, capsys):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        run_doctor(str(healthy_repo))
        assert "cold" in capsys.readouterr().out

        from codex_graph.multirepo import _overarching_graph_path

        clear_memo()
        load_bundle(_overarching_graph_path(str(healthy_repo)))
        clear_memo()
        run_doctor(str(healthy_repo))
        assert "warm" in capsys.readouterr().out

    def test_garbage_cache_self_heals(self, healthy_repo, fake_graphify, monkeypatch, capsys):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        from codex_graph.graph_cache import cache_path_for
        from codex_graph.multirepo import _overarching_graph_path

        graph_path = _overarching_graph_path(str(healthy_repo))
        with open(cache_path_for(graph_path), "wb") as f:
            f.write(b"garbage")
        clear_memo()
        rc = run_doctor(str(healthy_repo))
        out = capsys.readouterr().out
        assert rc == 0
        assert "[fail]" not in out
