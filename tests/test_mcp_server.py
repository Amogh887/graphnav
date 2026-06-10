from __future__ import annotations

from pathlib import Path

import pytest

from codex_graph.mcp_server import GraphTools, _safe_path
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


@pytest.fixture
def graph_root(tmp_path) -> Path:
    write_graph(tmp_path / "graphify-out" / "graph.json", NODES, LINKS)
    api = tmp_path / "api"
    api.mkdir()
    (api / "views.py").write_text("import limits\ndef create_incident():\n    limits.rate_limiter()\n")
    (api / "limits.py").write_text("def rate_limiter():\n    return True\n")
    return tmp_path


class TestGraphFind:
    def test_finds_symbol_by_query(self, graph_root):
        out = GraphTools(str(graph_root)).graph_find("incident")
        assert "create_incident" in out
        assert "api/views.py" in out

    def test_no_matches(self, graph_root):
        assert GraphTools(str(graph_root)).graph_find("zzz_nonexistent_zzz") == "(no matches)"


class TestGraphNeighbors:
    def test_lists_callees(self, graph_root):
        out = GraphTools(str(graph_root)).graph_neighbors("create_incident")
        assert "create_incident" in out
        assert "rate_limiter" in out


class TestImpact:
    def test_shows_callers(self, graph_root):
        out = GraphTools(str(graph_root)).impact("rate_limiter")
        assert "Blast radius" in out
        assert "create_incident" in out

    def test_symbol_not_found(self, graph_root):
        assert GraphTools(str(graph_root)).impact("zzz_nope") == "(symbol not found)"


class TestReadRegion:
    def test_numbered_lines(self, graph_root):
        out = GraphTools(str(graph_root)).read_region("api/views.py", 1, 2)
        assert "import limits" in out
        assert out.splitlines()[0].strip().startswith("1")

    def test_rejects_path_escape(self, graph_root):
        out = GraphTools(str(graph_root)).read_region("../../../etc/passwd", 1, 1)
        assert out.startswith("error:")

    def test_safe_path_blocks_traversal(self, tmp_path):
        with pytest.raises(ValueError):
            _safe_path(str(tmp_path), "../outside.txt")


class TestGraphContext:
    def test_returns_pack(self, graph_root):
        out = GraphTools(str(graph_root)).graph_context("incident rate limit")
        assert "Context for" in out


class TestNoGraph:
    def test_find_without_graph(self, tmp_path):
        out = GraphTools(str(tmp_path)).graph_find("anything")
        assert "No knowledge graph" in out

    def test_neighbors_without_graph(self, tmp_path):
        assert "No knowledge graph" in GraphTools(str(tmp_path)).graph_neighbors("x")
