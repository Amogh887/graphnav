from __future__ import annotations

import os
import pickle
import subprocess

import pytest

from graphnav import GraphNotFoundError
from graphnav.graph_cache import (
    CACHE_VERSION,
    GraphBundle,
    _git_recency,
    cache_path_for,
    clear_memo,
    graph_stamp,
    load_bundle,
)
from graphnav.graph_query import GraphIndex
from tests.conftest import write_graph


NODES = [
    {"id": "create_incident", "label": "create_incident", "source_file": "api/views.py",
     "file_type": "code", "source_location": "L2", "community": 0},
    {"id": "rate_limiter", "label": "rate_limiter", "source_file": "api/limits.py",
     "file_type": "code", "source_location": "L1", "community": 0},
]
LINKS = [
    {"source": "create_incident", "target": "rate_limiter", "relation": "calls",
     "source_file": "api/views.py"},
]


@pytest.fixture(autouse=True)
def fresh_memo():
    clear_memo()
    yield
    clear_memo()


@pytest.fixture
def no_git(monkeypatch):
    monkeypatch.setattr("graphnav.multirepo._git_sha", lambda root: None)


@pytest.fixture
def graph_root(tmp_path):
    write_graph(tmp_path / "graphify-out" / "graph.json", NODES, LINKS)
    return tmp_path


def graph_file(root) -> str:
    return str(root / "graphify-out" / "graph.json")


class TestGraphStamp:
    def test_missing_file_returns_none(self, tmp_path):
        assert graph_stamp(str(tmp_path / "nope.json")) is None

    def test_existing_file_returns_mtime_and_size(self, graph_root):
        stamp = graph_stamp(graph_file(graph_root))
        assert stamp is not None
        assert stamp[1] == os.path.getsize(graph_file(graph_root))


class TestLoadBundleBuilds:
    def test_bundle_components_from_single_parse(self, graph_root, no_git):
        bundle = load_bundle(graph_file(graph_root))
        assert bundle.index.rank("incident", 5, 2.0, 1.5, 0.75)
        assert bundle.nav.find_symbols("incident")
        assert "api/views.py" in bundle.symbols_by_file

    def test_missing_graph_raises(self, tmp_path):
        with pytest.raises(GraphNotFoundError):
            load_bundle(str(tmp_path / "graphify-out" / "graph.json"))


class TestMemoHit:
    def test_second_call_returns_same_object(self, graph_root, no_git):
        first = load_bundle(graph_file(graph_root))
        second = load_bundle(graph_file(graph_root))
        assert first is second


class TestDiskCacheRoundtrip:
    def test_second_process_reads_pickle(self, graph_root, no_git, monkeypatch):
        load_bundle(graph_file(graph_root))
        assert os.path.exists(cache_path_for(graph_file(graph_root)))
        clear_memo()
        calls = {"n": 0}
        original = GraphIndex.__init__

        def counting(self, *args, **kwargs):
            calls["n"] += 1
            original(self, *args, **kwargs)

        monkeypatch.setattr(GraphIndex, "__init__", counting)
        bundle = load_bundle(graph_file(graph_root))
        assert calls["n"] == 0
        assert bundle.nav.find_symbols("incident")


class TestInvalidation:
    def test_touched_graph_rebuilds(self, graph_root, no_git):
        first = load_bundle(graph_file(graph_root))
        write_graph(
            graph_root / "graphify-out" / "graph.json",
            NODES + [{"id": "audit_log", "label": "audit_log", "source_file": "api/audit.py",
                      "file_type": "code", "source_location": "L1", "community": 0}],
            LINKS,
        )
        os.utime(graph_file(graph_root), ns=(1, 1))
        second = load_bundle(graph_file(graph_root))
        assert second is not first
        assert second.nav.find_symbols("audit")

    def test_different_skip_patterns_rebuild(self, graph_root, no_git):
        first = load_bundle(graph_file(graph_root))
        second = load_bundle(graph_file(graph_root), skip_patterns=["vendored"])
        assert second is not first


class TestCorruptCache:
    def test_garbage_cache_rebuilt(self, graph_root, no_git):
        cache = cache_path_for(graph_file(graph_root))
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "wb") as f:
            f.write(b"not a pickle")
        bundle = load_bundle(graph_file(graph_root))
        assert bundle.nav.find_symbols("incident")
        with open(cache, "rb") as f:
            envelope = pickle.load(f)
        assert envelope["version"] == CACHE_VERSION

    def test_stale_version_rebuilt(self, graph_root, no_git):
        load_bundle(graph_file(graph_root))
        cache = cache_path_for(graph_file(graph_root))
        with open(cache, "rb") as f:
            envelope = pickle.load(f)
        envelope["version"] = CACHE_VERSION - 1
        with open(cache, "wb") as f:
            pickle.dump(envelope, f)
        clear_memo()
        bundle = load_bundle(graph_file(graph_root))
        assert bundle.nav.find_symbols("incident")


class TestNoCacheEnv:
    def test_no_pickle_written(self, graph_root, no_git, monkeypatch):
        monkeypatch.setenv("GRAPHNAV_NO_CACHE", "1")
        load_bundle(graph_file(graph_root))
        assert not os.path.exists(cache_path_for(graph_file(graph_root)))


class TestGitRecency:
    def test_decayed_scores_from_git_log(self, monkeypatch):
        log = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\napi/views.py\n\nbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\napi/limits.py\napi/views.py\n"

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=log, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        scores = _git_recency("/repo")
        assert scores["api/views.py"] == 1.0
        assert scores["api/limits.py"] == pytest.approx(0.9)

    def test_git_failure_returns_empty(self, monkeypatch):
        def fake_run(*args, **kwargs):
            raise OSError("no git")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _git_recency("/repo") == {}

    def test_recency_attached_to_bundle(self, graph_root, monkeypatch):
        monkeypatch.setattr("graphnav.multirepo._git_sha", lambda root: "a" * 40)
        log = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\napi/views.py\n"

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=log, stderr="")

        monkeypatch.setattr("graphnav.graph_cache.subprocess.run", fake_run)
        bundle = load_bundle(graph_file(graph_root))
        assert bundle.recency == {"api/views.py": 1.0}
        assert bundle.recency_sha == "a" * 40
