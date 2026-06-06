from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_graph import GraphNotFoundError
from codex_graph.graph_query import GraphIndex, _tokenize, load_index, query_files
from tests.conftest import write_graph


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
