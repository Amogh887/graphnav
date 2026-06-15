from __future__ import annotations

from pathlib import Path

import pytest

from codex_graph.graph_nav import GraphNav
from tests.conftest import make_graph_dict, write_graph


NODES = [
    {"id": "handle_request", "label": "handle_request", "source_file": "api/routes.py",
     "file_type": "code", "source_location": "L4", "community": 0},
    {"id": "create_incident", "label": "create_incident", "source_file": "api/views.py",
     "file_type": "code", "source_location": "L2", "community": 0},
    {"id": "rate_limiter", "label": "rate_limiter", "source_file": "api/limits.py",
     "file_type": "code", "source_location": "L1", "community": 0},
    {"id": "audit_log", "label": "audit_log", "source_file": "api/audit.py",
     "file_type": "code", "source_location": "L7", "community": 0},
]
LINKS = [
    {"source": "handle_request", "target": "create_incident", "relation": "calls",
     "source_file": "api/routes.py"},
    {"source": "create_incident", "target": "rate_limiter", "relation": "calls",
     "source_file": "api/views.py"},
    {"source": "audit_log", "target": "rate_limiter", "relation": "calls",
     "source_file": "api/audit.py"},
]


@pytest.fixture
def graph_path(tmp_path) -> Path:
    path = tmp_path / "graphify-out" / "graph.json"
    write_graph(path, NODES, LINKS)
    return path


@pytest.fixture
def nav(graph_path) -> GraphNav:
    return GraphNav(str(graph_path))


class TestFindSymbolsExact:
    def test_finds_by_exact_token(self, nav):
        hits = nav.find_symbols("incident")
        assert any(h["symbol"] == "create_incident" for h in hits)

    def test_exact_hits_are_not_fuzzy(self, nav):
        hits = nav.find_symbols("rate_limiter")
        assert hits
        assert all(h["fuzzy"] is False for h in hits)

    def test_empty_query_returns_nothing(self, nav):
        assert nav.find_symbols("") == []


class TestFindSymbolsFuzzy:
    def test_typo_falls_back_to_fuzzy(self, nav):
        hits = nav.find_symbols("craete_incidnet")
        assert hits
        assert hits[0]["symbol"] == "create_incident"
        assert hits[0]["file"] == "api/views.py"
        assert all(h["fuzzy"] is True for h in hits)

    def test_no_fuzzy_when_exact_match_exists(self, nav):
        hits = nav.find_symbols("create_incident")
        assert hits
        assert all(h["fuzzy"] is False for h in hits)

    def test_gibberish_returns_nothing(self, nav):
        assert nav.find_symbols("zzqqxx") == []


class TestNeighborsFuzzy:
    def test_typo_resolves_to_nearest_symbol(self, nav):
        out = nav.neighbors("craete_incidnet")
        assert out["symbol"] == "create_incident"
        assert out["fuzzy"] is True
        assert out["query"] == "craete_incidnet"
        assert any("handle_request" in c for c in out["callers"])
        assert any("rate_limiter" in c for c in out["callees"])

    def test_exact_lookup_is_not_fuzzy(self, nav):
        out = nav.neighbors("create_incident")
        assert out["symbol"] == "create_incident"
        assert out["fuzzy"] is False
        assert "query" not in out

    def test_gibberish_not_found(self, nav):
        assert nav.neighbors("zzqqxx") == {"symbol": "zzqqxx", "found": False}


class TestNeighborsStructuralEdges:
    def _nav(self):
        nodes = [
            {"id": "views.py", "label": "views.py", "source_file": "api/views.py",
             "file_type": "code", "source_location": "L1"},
            {"id": "create_incident", "label": "create_incident", "source_file": "api/views.py",
             "file_type": "code", "source_location": "L2"},
            {"id": "handle_request", "label": "handle_request", "source_file": "api/routes.py",
             "file_type": "code", "source_location": "L4"},
        ]
        links = [
            {"source": "views.py", "target": "create_incident", "relation": "contains",
             "source_file": "api/views.py"},
            {"source": "handle_request", "target": "create_incident", "relation": "calls",
             "source_file": "api/routes.py"},
        ]
        return GraphNav("", graph=make_graph_dict(nodes, links))

    def test_contains_edge_excluded_from_callers(self):
        out = self._nav().neighbors("create_incident")
        assert not any("contains" in c for c in out["callers"])
        assert not any("views.py" in c for c in out["callers"])

    def test_real_caller_still_present(self):
        out = self._nav().neighbors("create_incident")
        assert any("handle_request" in c and "calls" in c for c in out["callers"])


class TestReferencesTo:
    def test_cross_file_caller_appears(self, nav):
        rows = nav.references_to(["api/limits.py"])
        assert any("create_incident" in r and r.startswith("api/views.py") for r in rows)

    def test_files_in_input_set_excluded(self, nav):
        rows = nav.references_to(["api/limits.py", "api/views.py"])
        assert not any(r.startswith("api/views.py") for r in rows)
        assert any(r.startswith("api/audit.py") for r in rows)

    def test_limit_respected(self, nav):
        assert len(nav.references_to(["api/limits.py"], limit=1)) == 1


class TestGraphParam:
    def test_nonexistent_path_with_graph_dict(self, tmp_path, nav):
        in_memory = GraphNav(str(tmp_path / "missing.json"), graph=make_graph_dict(NODES, LINKS))
        assert in_memory.find_symbols("incident") == nav.find_symbols("incident")
        assert in_memory.find_symbols("craete_incidnet") == nav.find_symbols("craete_incidnet")
        assert in_memory.neighbors("create_incident") == nav.neighbors("create_incident")
        assert in_memory.references_to(["api/limits.py"]) == nav.references_to(["api/limits.py"])
