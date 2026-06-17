from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphnav import GraphNotFoundError
from graphnav.graph_query import (
    DEFAULT_RELATION_WEIGHTS,
    GraphIndex,
    UNKNOWN_RELATION_WEIGHT,
    _tokenize,
    load_index,
    merge_relation_weights,
    query_files,
)
from tests.conftest import make_graph_dict, write_graph


class TestTokenize:
    def test_basic_split(self):
        assert _tokenize("hello world") == ["hello", "world"]

    def test_lowercases(self):
        assert _tokenize("Hello World") == ["hello", "world"]

    def test_strips_punctuation(self):
        tokens = _tokenize("foo.bar(baz)")
        assert "foo" in tokens
        assert "bar" in tokens
        assert "baz" in tokens

    def test_filters_short_tokens(self):
        tokens = _tokenize("a b cd ef")
        assert "a" not in tokens
        assert "b" not in tokens
        assert "cd" in tokens
        assert "ef" in tokens

    def test_digits_included(self):
        assert "42" in _tokenize("version 42")

    def test_empty_string(self):
        assert _tokenize("") == []

    def test_only_punctuation(self):
        assert _tokenize("!@#$%") == []

    def test_camel_case_split(self):
        tokens = _tokenize("buildPrompt")
        assert "build" in tokens
        assert "prompt" in tokens

    def test_pascal_and_acronym_split(self):
        tokens = _tokenize("HTTPResponseSerializer")
        assert "http" in tokens
        assert "response" in tokens
        assert "serializer" in tokens

    def test_screaming_snake_split(self):
        tokens = _tokenize("TRACKED_FIELDS")
        assert tokens == ["tracked", "field"]

    def test_plural_stemming_matches_singular(self):
        assert _tokenize("serializers") == _tokenize("serializer")
        assert _tokenize("models") == _tokenize("model")

    def test_underscore_treated_as_separator(self):
        tokens = _tokenize("build_prompt")
        assert "build" in tokens
        assert "prompt" in tokens


class TestGraphIndex:
    def _make_index(self, tmp_path: Path, nodes=None, skip=None) -> GraphIndex:
        graph_path = tmp_path / "graph.json"
        write_graph(graph_path, nodes=nodes)
        return GraphIndex(str(graph_path), skip or [])

    def test_empty_graph_no_crash(self, tmp_path):
        idx = self._make_index(tmp_path)
        assert idx._N == 0

    def test_single_node_indexed(self, tmp_path):
        nodes = [{"id": "n1", "label": "UserModel", "source_file": "models.py", "file_type": "code", "community": 0}]
        idx = self._make_index(tmp_path, nodes)
        assert "models.py" in idx.file_tokens
        assert "user" in idx.file_tokens["models.py"]
        assert "model" in idx.file_tokens["models.py"]

    def test_type_weights_applied(self, tmp_path):
        nodes = [
            {"id": "n1", "label": "doc", "source_file": "readme.py", "file_type": "document", "community": 0},
            {"id": "n2", "label": "doc", "source_file": "code.py", "file_type": "code", "community": 0},
        ]
        idx = self._make_index(tmp_path, nodes)
        readme_count = idx.file_tokens["readme.py"].count("doc")
        code_count = idx.file_tokens["code.py"].count("doc")
        assert readme_count == 2 * code_count

    def test_rationale_weight_highest(self, tmp_path):
        nodes = [
            {"id": "n1", "label": "tok", "source_file": "rationale.py", "file_type": "rationale", "community": 0},
            {"id": "n2", "label": "tok", "source_file": "code.py", "file_type": "code", "community": 0},
        ]
        idx = self._make_index(tmp_path, nodes)
        assert idx.file_tokens["rationale.py"].count("tok") == 3
        assert idx.file_tokens["code.py"].count("tok") == 1

    def test_skip_patterns_exclude_files(self, tmp_path):
        nodes = [
            {"id": "n1", "label": "Foo", "source_file": "node_modules/lib.py", "file_type": "code", "community": 0},
            {"id": "n2", "label": "Bar", "source_file": "src/bar.py", "file_type": "code", "community": 0},
        ]
        idx = self._make_index(tmp_path, nodes, skip=["node_modules"])
        assert "node_modules/lib.py" not in idx.file_tokens
        assert "src/bar.py" in idx.file_tokens

    def test_community_tokens_populated(self, tmp_path):
        nodes = [
            {"id": "n1", "label": "FooBar", "source_file": "foo.py", "file_type": "code", "community": 5},
        ]
        idx = self._make_index(tmp_path, nodes)
        assert 5 in idx.community_tokens
        assert "foo" in idx.community_tokens[5]
        assert "bar" in idx.community_tokens[5]

    def test_node_without_source_file_skipped_for_file_tokens(self, tmp_path):
        nodes = [
            {"id": "n1", "label": "Exception", "source_file": "", "file_type": "code", "community": 0},
        ]
        idx = self._make_index(tmp_path, nodes)
        assert "" not in idx.file_tokens
        assert idx._N == 0

    def test_rank_returns_empty_for_empty_query(self, tmp_path):
        nodes = [{"id": "n1", "label": "UserModel", "source_file": "models.py", "file_type": "code", "community": 0}]
        idx = self._make_index(tmp_path, nodes)
        assert idx.rank("", 5, 2.0, 1.5, 0.75) == []

    def test_rank_scores_matching_file_higher(self, tmp_path):
        nodes = [
            {"id": "n1", "label": "user model schema", "source_file": "models.py", "file_type": "code", "community": 0},
            {"id": "n2", "label": "auth service token", "source_file": "auth.py", "file_type": "code", "community": 1},
        ]
        idx = self._make_index(tmp_path, nodes)
        ranked = idx.rank("user model", 10, 0.0, 1.5, 0.75)
        assert len(ranked) >= 1
        assert ranked[0].source_file == "models.py"

    def test_rank_respects_top_k(self, tmp_path):
        nodes = [
            {"id": f"n{i}", "label": f"tok{i}", "source_file": f"file{i}.py", "file_type": "code", "community": 0}
            for i in range(5)
        ]
        idx = self._make_index(tmp_path, nodes)
        query = " ".join(f"tok{i}" for i in range(5))
        ranked = idx.rank(query, 3, 0.0, 1.5, 0.75)
        assert len(ranked) <= 3

    def test_rank_excludes_zero_scores(self, tmp_path):
        nodes = [
            {"id": "n1", "label": "completely_unrelated", "source_file": "other.py", "file_type": "code", "community": 0},
        ]
        idx = self._make_index(tmp_path, nodes)
        ranked = idx.rank("usermodel auth", 10, 0.0, 1.5, 0.75)
        assert ranked == []

    def test_community_boost_increases_score(self, tmp_path):
        nodes = [
            {"id": "n1", "label": "auth", "source_file": "auth.py", "file_type": "code", "community": 7},
            {"id": "n2", "label": "user", "source_file": "user.py", "file_type": "code", "community": 7},
        ]
        idx = self._make_index(tmp_path, nodes)
        score_with_boost = idx._bm25(["auth"], "auth.py", 1.5, 0.75) + idx._community_boost(["user"], "auth.py", 5.0)
        score_without_boost = idx._bm25(["auth"], "auth.py", 1.5, 0.75)
        assert score_with_boost >= score_without_boost

    def test_norm_label_used_when_label_missing(self, tmp_path):
        nodes = [{"id": "n1", "norm_label": "normalized", "source_file": "f.py", "file_type": "code", "community": 0}]
        idx = self._make_index(tmp_path, nodes)
        assert "normalized" in idx.file_tokens["f.py"]

    def test_file_neighbors_from_links(self, tmp_path):
        nodes = [
            {"id": "a", "label": "Foo", "source_file": "a.py", "file_type": "code", "community": 0},
            {"id": "b", "label": "Bar", "source_file": "b.py", "file_type": "code", "community": 0},
        ]
        graph_path = tmp_path / "graph.json"
        write_graph(graph_path, nodes=nodes, links=[{"source": "a", "target": "b", "relation": "calls"}])
        idx = GraphIndex(str(graph_path), [])
        assert "b.py" in idx.file_neighbors["a.py"]
        assert "a.py" in idx.file_neighbors["b.py"]

    def test_edge_boost_pulls_in_called_neighbor(self, tmp_path):
        nodes = [
            {"id": "a", "label": "rate limiter middleware", "source_file": "limits.py", "file_type": "code", "community": 0},
            {"id": "b", "label": "create incident endpoint", "source_file": "views.py", "file_type": "code", "community": 0},
        ]
        graph_path = tmp_path / "graph.json"
        write_graph(graph_path, nodes=nodes, links=[{"source": "a", "target": "b", "relation": "calls"}])
        idx = GraphIndex(str(graph_path), [])

        no_edges = [r.source_file for r in idx.rank("rate limiter", 10, 0.0, 1.5, 0.75, edge_boost_weight=0.0)]
        with_edges = [r.source_file for r in idx.rank("rate limiter", 10, 0.0, 1.5, 0.75, edge_boost_weight=0.5)]
        assert "views.py" not in no_edges
        assert "views.py" in with_edges


class TestLoadIndex:
    def test_raises_for_missing_file(self, tmp_path):
        missing = str(tmp_path / "nonexistent.json")
        with pytest.raises(GraphNotFoundError):
            load_index(missing, [])

    def test_loads_valid_graph(self, tmp_path):
        graph_path = tmp_path / "graph.json"
        write_graph(graph_path, nodes=[
            {"id": "n1", "label": "Foo", "source_file": "foo.py", "file_type": "code", "community": 0}
        ])
        idx = load_index(str(graph_path), [])
        assert isinstance(idx, GraphIndex)
        assert "foo.py" in idx.file_tokens

    def test_skip_patterns_forwarded(self, tmp_path):
        graph_path = tmp_path / "graph.json"
        write_graph(graph_path, nodes=[
            {"id": "n1", "label": "Foo", "source_file": "node_modules/foo.py", "file_type": "code", "community": 0}
        ])
        idx = load_index(str(graph_path), ["node_modules"])
        assert "node_modules/foo.py" not in idx.file_tokens


class TestQueryFiles:
    def test_returns_ranked_files(self, tmp_path):
        graph_path = tmp_path / "graph.json"
        write_graph(graph_path, nodes=[
            {"id": "n1", "label": "query engine module", "source_file": "query.py", "file_type": "code", "community": 0},
            {"id": "n2", "label": "auth module token", "source_file": "auth.py", "file_type": "code", "community": 1},
        ])
        idx = load_index(str(graph_path), [])
        results = query_files("query engine", idx, 5)
        assert len(results) >= 1
        assert results[0].source_file == "query.py"
        assert results[0].score > 0

    def test_returns_empty_for_no_match(self, tmp_path):
        graph_path = tmp_path / "graph.json"
        write_graph(graph_path, nodes=[
            {"id": "n1", "label": "FooBar", "source_file": "foo.py", "file_type": "code", "community": 0},
        ])
        idx = load_index(str(graph_path), [])
        results = query_files("completely unrelated xyz", idx, 5)
        assert results == []

    def test_top_k_limits_results(self, tmp_path):
        graph_path = tmp_path / "graph.json"
        write_graph(graph_path, nodes=[
            {"id": f"n{i}", "label": f"module{i}", "source_file": f"m{i}.py", "file_type": "code", "community": 0}
            for i in range(10)
        ])
        idx = load_index(str(graph_path), [])
        query = " ".join(f"module{i}" for i in range(10))
        results = query_files(query, idx, 3)
        assert len(results) <= 3

    def test_scores_are_positive(self, tmp_path):
        graph_path = tmp_path / "graph.json"
        write_graph(graph_path, nodes=[
            {"id": "n1", "label": "UserService", "source_file": "user.py", "file_type": "code", "community": 0},
        ])
        idx = load_index(str(graph_path), [])
        results = query_files("user service", idx, 5)
        for r in results:
            assert r.score > 0


class TestEdgeRelationWeights:
    _LINKS = [
        {"source": "c1", "target": "h1", "relation": "calls"},
        {"source": "r1", "target": "h2", "relation": "references"},
        {"source": "u1", "target": "h3", "relation": "frobnicates"},
    ]

    def _build(self, tmp_path: Path, relation_weights=None) -> GraphIndex:
        nodes = [
            {"id": "h1", "label": "alpha beta", "source_file": "hot1.py", "file_type": "code", "community": 0},
            {"id": "h2", "label": "alpha beta", "source_file": "hot2.py", "file_type": "code", "community": 0},
            {"id": "h3", "label": "alpha beta", "source_file": "hot3.py", "file_type": "code", "community": 0},
            {"id": "c1", "label": "zzz", "source_file": "calls_linked.py", "file_type": "code", "community": 0},
            {"id": "r1", "label": "zzz", "source_file": "refs_linked.py", "file_type": "code", "community": 0},
            {"id": "u1", "label": "zzz", "source_file": "unknown_linked.py", "file_type": "code", "community": 0},
        ]
        graph_path = tmp_path / "graph.json"
        write_graph(graph_path, nodes=nodes, links=self._LINKS)
        return GraphIndex(str(graph_path), [], relation_weights)

    def _scores(self, idx: GraphIndex) -> dict[str, float]:
        ranked = idx.rank("alpha beta", 10, 0.0, 1.5, 0.75, keep_ratio=0.0, edge_boost_weight=0.5)
        return {r.source_file: r.score for r in ranked}

    def test_calls_neighbor_boosts_more_than_references(self, tmp_path):
        scores = self._scores(self._build(tmp_path))
        assert scores["calls_linked.py"] > scores["refs_linked.py"]

    def test_user_override_flips_relation_priority(self, tmp_path):
        idx = self._build(tmp_path, relation_weights={"references": 1.0, "calls": 0.1})
        scores = self._scores(idx)
        assert scores["refs_linked.py"] > scores["calls_linked.py"]

    def test_unknown_relation_gets_unknown_weight(self, tmp_path):
        idx = self._build(tmp_path)
        assert idx.file_neighbors["unknown_linked.py"]["hot3.py"] == UNKNOWN_RELATION_WEIGHT
        scores = self._scores(idx)
        assert scores["calls_linked.py"] > scores["unknown_linked.py"] > scores["refs_linked.py"]

    def test_merge_keeps_defaults_for_unspecified_keys(self):
        merged = merge_relation_weights({"references": 1.0})
        assert merged["references"] == 1.0
        assert merged["calls"] == DEFAULT_RELATION_WEIGHTS["calls"]
        assert merged["imports"] == DEFAULT_RELATION_WEIGHTS["imports"]
        assert merged["uses"] == DEFAULT_RELATION_WEIGHTS["uses"]


class TestRecencyBoost:
    def _build(self, tmp_path: Path) -> GraphIndex:
        nodes = [
            {"id": "n1", "label": "alpha beta alpha", "source_file": "first.py", "file_type": "code", "community": 0},
            {"id": "n2", "label": "alpha beta", "source_file": "second.py", "file_type": "code", "community": 0},
            {"id": "n3", "label": "zzz unrelated", "source_file": "zero.py", "file_type": "code", "community": 0},
        ]
        graph_path = tmp_path / "graph.json"
        write_graph(graph_path, nodes=nodes)
        return GraphIndex(str(graph_path), [])

    def test_recency_reorders_near_equal_files(self, tmp_path):
        idx = self._build(tmp_path)
        without = idx.rank("alpha beta", 10, 0.0, 1.5, 0.75)
        boosted = idx.rank(
            "alpha beta", 10, 0.0, 1.5, 0.75,
            recency={"second.py": 1.0}, recency_boost_weight=0.5,
        )
        assert without[0].source_file == "first.py"
        assert boosted[0].source_file == "second.py"

    def test_recency_none_matches_omitting(self, tmp_path):
        idx = self._build(tmp_path)
        with_none = idx.rank(
            "alpha beta", 10, 0.0, 1.5, 0.75,
            recency=None, recency_boost_weight=0.5,
        )
        assert with_none == idx.rank("alpha beta", 10, 0.0, 1.5, 0.75)

    def test_zero_base_score_file_never_appears(self, tmp_path):
        idx = self._build(tmp_path)
        ranked = idx.rank(
            "alpha beta", 10, 0.0, 1.5, 0.75,
            recency={"zero.py": 1.0}, recency_boost_weight=5.0,
        )
        assert "zero.py" not in [r.source_file for r in ranked]

    def test_query_files_forwards_recency_params(self, tmp_path):
        idx = self._build(tmp_path)
        results = query_files(
            "alpha beta", idx, 10,
            recency={"second.py": 1.0}, recency_boost_weight=0.5,
        )
        assert results[0].source_file == "second.py"


class TestGraphParam:
    def test_graph_dict_ranks_identically_to_file(self, tmp_path):
        nodes = [
            {"id": "n1", "label": "user model schema", "source_file": "models.py", "file_type": "code", "community": 0},
            {"id": "n2", "label": "auth service token", "source_file": "auth.py", "file_type": "code", "community": 1},
        ]
        links = [{"source": "n1", "target": "n2", "relation": "calls"}]
        graph_path = tmp_path / "graph.json"
        write_graph(graph_path, nodes=nodes, links=links)
        from_file = GraphIndex(str(graph_path), [])
        from_dict = GraphIndex(
            str(tmp_path / "missing.json"), [], graph=make_graph_dict(nodes, links)
        )
        ranked_dict = from_dict.rank("user model", 10, 2.0, 1.5, 0.75)
        assert ranked_dict
        assert ranked_dict == from_file.rank("user model", 10, 2.0, 1.5, 0.75)
