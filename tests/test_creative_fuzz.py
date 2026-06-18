from __future__ import annotations

import json
import random
import string

import pytest

from graphnav import multirepo
from graphnav.config import load_config_report
from graphnav.graph_query import GraphIndex, _stem, _tokenize
from graphnav.graph_nav import GraphNav
from graphnav.mcp_server import GraphTools


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))
    return path


def _repo_with_graph(tmp_path, graph):
    _write(tmp_path / "graphify-out" / "graph.json", graph)
    return tmp_path


def _service(name, root):
    return multirepo.ServiceInfo(
        name, str(root / name), str(root / name / "graphify-out" / "graph.json")
    )


# ---------------------------------------------------------------------------
# Section 1 — adversarial graphs that are valid JSON but structurally wrong.
# The whole codebase guards graph reads with (JSONDecodeError, KeyError, OSError);
# these inputs raise AttributeError / TypeError instead and must not escape.
# ---------------------------------------------------------------------------

MALFORMED_GRAPHS = {
    "graph_is_a_list": [1, 2, 3],
    "graph_is_a_string": "not a graph",
    "nodes_is_null": {"nodes": None, "links": []},
    "nodes_is_dict": {"nodes": {"a": 1}, "links": []},
    "links_is_null": {"nodes": [{"id": 1, "source_file": "a.py", "label": "f"}], "links": None},
    "node_is_a_string": {"nodes": ["just-a-string"], "links": []},
}


@pytest.mark.parametrize("name", list(MALFORMED_GRAPHS))
def test_context_pack_survives_malformed_graph(tmp_path, monkeypatch, name):
    monkeypatch.setenv("GRAPHNAV_NO_CACHE", "1")
    root = _repo_with_graph(tmp_path, MALFORMED_GRAPHS[name])
    out = multirepo.build_context_pack(str(root), "find the thing")
    assert "Context for" in out  # returned a pack, did not raise


@pytest.mark.parametrize("name", list(MALFORMED_GRAPHS))
def test_inline_context_pack_survives_malformed_graph(tmp_path, monkeypatch, name):
    monkeypatch.setenv("GRAPHNAV_NO_CACHE", "1")
    root = _repo_with_graph(tmp_path, MALFORMED_GRAPHS[name])
    out = multirepo.build_context_pack_inline(str(root), "find the thing")
    assert "Context for" in out


def test_analyze_bridges_tolerates_node_without_id(tmp_path):
    # partition_graph uses node.get("id"); analyze_bridges must agree and not KeyError.
    graph = {
        "nodes": [
            {"source_file": "svc-a/x.py", "label": "caller"},  # no "id"
            {"id": 2, "source_file": "svc-b/y.py", "label": "callee"},
        ],
        "links": [{"source": 1, "target": 2, "relation": "calls"}],
    }
    gp = _write(tmp_path / "g.json", graph)
    services = [_service("svc-a", tmp_path), _service("svc-b", tmp_path)]
    bridges = multirepo.analyze_bridges(str(gp), services)  # must not raise
    assert set(bridges) == {"svc-a", "svc-b"}


def test_partition_and_analyze_agree_on_missing_id(tmp_path):
    graph = {
        "nodes": [{"source_file": "svc-a/x.py", "label": "caller"},
                  {"id": 2, "source_file": "svc-b/y.py", "label": "callee"}],
        "links": [{"source": 1, "target": 2, "relation": "calls"}],
    }
    gp = _write(tmp_path / "g.json", graph)
    services = [_service("svc-a", tmp_path), _service("svc-b", tmp_path)]
    (tmp_path / "svc-a").mkdir()
    (tmp_path / "svc-b").mkdir()
    multirepo.partition_graph(str(gp), services)  # tolerant today
    multirepo.analyze_bridges(str(gp), services)  # should be equally tolerant


def test_context_pack_with_services_and_node_missing_id(tmp_path, monkeypatch):
    # Reproduces the bridges path: on-disk services + a graph node missing "id".
    monkeypatch.setenv("GRAPHNAV_NO_CACHE", "1")
    for svc in ("svc-a", "svc-b"):
        d = tmp_path / svc
        d.mkdir()
        (d / "package.json").write_text("{}")
    graph = {
        "nodes": [
            {"id": 1, "source_file": "svc-a/x.py", "label": "alpha beta",
             "file_type": "code", "source_location": "L1"},
            {"source_file": "svc-b/y.py", "label": "alpha gamma", "file_type": "code"},
        ],
        "links": [{"source": 1, "target": 2, "relation": "calls"}],
    }
    _write(tmp_path / "graphify-out" / "graph.json", graph)
    out = multirepo.build_context_pack(str(tmp_path), "alpha")  # must not raise
    assert "Context for" in out


# ---------------------------------------------------------------------------
# Section 2 — tokenizer & stemmer (high-entropy fuzz + linguistic properties)
# ---------------------------------------------------------------------------

def test_tokenize_never_crashes_on_random_unicode():
    rng = random.Random(1234)
    exotic = [0x00, 0x09, 0x0A, 0xFEFF, 0x1F680, 0x1F525, 0x00E9, 0x4E2D]
    alphabet = (
        string.printable
        + "".join(chr(c) for c in range(0x80, 0x500))
        + "".join(chr(c) for c in exotic)
    )
    for _ in range(500):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        toks = _tokenize(s)
        assert isinstance(toks, list)
        assert all(isinstance(t, str) and t for t in toks)


def test_tokenize_is_deterministic():
    s = "CamelCaseHTTPServer parse_env_file v2 cafe"
    assert _tokenize(s) == _tokenize(s)


@pytest.mark.parametrize("singular,plural", [
    ("status", "statuses"),
    ("class", "classes"),
    ("query", "queries"),
    ("bus", "buses"),
])
def test_singular_and_plural_share_a_token(singular, plural):
    # A search for the singular should reach code that names the plural and vice-versa.
    assert set(_tokenize(singular)) & set(_tokenize(plural)), (
        f"{singular!r} -> {_tokenize(singular)}, {plural!r} -> {_tokenize(plural)}"
    )


@pytest.mark.parametrize("word", ["status", "analysis", "focus", "bonus", "virus"])
def test_stem_does_not_truncate_singular_s_words(word):
    # These are singular; stripping the trailing 's' corrupts the root.
    assert _stem(word) == word


# ---------------------------------------------------------------------------
# Section 3 — config fuzzing: every malformed value degrades, never crashes.
# ---------------------------------------------------------------------------

GARBAGE_VALUES = ['"text"', "true", "[1,2,3]", "-999999999", "0.0", "{a = 1}"]


@pytest.mark.parametrize("raw", GARBAGE_VALUES)
@pytest.mark.parametrize("field", [
    "query.top_k", "query.bm25_k1", "query.bm25_b", "context.max_file_chars",
    "codex.timeout_seconds", "mono.context_top_files", "mono.watch_poll_interval",
])
def test_config_numeric_fields_degrade_gracefully(tmp_path, field, raw):
    section, key = field.split(".")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f"[{section}]\n{key} = {raw}\n")
    cfg, _, _ = load_config_report(str(cfg_path))  # must not raise
    value = getattr(getattr(cfg, section), key)
    assert isinstance(value, (int, float)) and not isinstance(value, bool)


def test_max_file_chars_is_clamped_non_negative(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[context]\nmax_file_chars = -500\n")
    cfg, _, warnings = load_config_report(str(cfg_path))
    assert cfg.context.max_file_chars >= 0, (
        "negative max_file_chars should be clamped like every other numeric field"
    )


@pytest.mark.parametrize("raw", ['"not-a-table"', "[1,2,3]", "true", "42"])
def test_edge_relation_weights_garbage_is_ignored(tmp_path, raw):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f"[query]\nedge_relation_weights = {raw}\n")
    cfg, _, _ = load_config_report(str(cfg_path))
    assert isinstance(cfg.query.edge_relation_weights, dict)


# ---------------------------------------------------------------------------
# Section 4 — rank() invariants on a randomly generated graph.
# ---------------------------------------------------------------------------

def _random_graph(rng, n_nodes=40):
    words = ["auth", "user", "payment", "cart", "order", "token", "session",
             "parse", "render", "queue", "cache", "graph", "index"]
    nodes = []
    for i in range(n_nodes):
        label = " ".join(rng.sample(words, rng.randint(1, 4)))
        nodes.append({
            "id": i,
            "source_file": f"pkg/mod_{i % 7}/file_{i}.py",
            "label": label,
            "community": i % 5,
        })
    links = [{"source": rng.randrange(n_nodes), "target": rng.randrange(n_nodes),
              "relation": rng.choice(["calls", "imports", "uses"])}
             for _ in range(60)]
    return {"nodes": nodes, "links": links}


def test_rank_invariants_under_random_graphs(tmp_path):
    rng = random.Random(7)
    for trial in range(25):
        gp = _write(tmp_path / f"g{trial}.json", _random_graph(rng))
        idx = GraphIndex(str(gp), [])
        top_k = rng.randint(1, 10)
        out = idx.rank("user auth token", top_k, 2.0, 1.5, 0.75)
        assert len(out) <= top_k
        scores = [rf.score for rf in out]
        assert scores == sorted(scores, reverse=True)
        assert all(s > 0 for s in scores)
        # determinism
        again = idx.rank("user auth token", top_k, 2.0, 1.5, 0.75)
        assert [rf.source_file for rf in again] == [rf.source_file for rf in out]


def test_rank_empty_query_returns_nothing(tmp_path):
    gp = _write(tmp_path / "g.json", _random_graph(random.Random(1)))
    idx = GraphIndex(str(gp), [])
    assert idx.rank("   !!! ??? ", 5, 2.0, 1.5, 0.75) == []


# ---------------------------------------------------------------------------
# Section 5 — read_region must never escape the repo root.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "../outside.txt", "../../etc/passwd", "/etc/hostname",
    "sub/../../outside.txt", "./../outside.txt",
])
def test_read_region_cannot_escape_root(tmp_path, rel):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.txt").write_text("SECRET\n")
    (repo / "inside.txt").write_text("ok\n")
    tools = GraphTools(str(repo))
    out = tools.read_region(rel, 1, 5)
    assert "SECRET" not in out
    assert out.startswith("error:")


def test_read_region_reads_inside(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "inside.txt").write_text("a\nb\nc\n")
    tools = GraphTools(str(repo))
    assert "b" in tools.read_region("inside.txt", 1, 3)


# ---------------------------------------------------------------------------
# Section 6 — bridges & graph-nav consistency.
# ---------------------------------------------------------------------------

def test_no_service_bridges_to_itself(tmp_path):
    graph = {
        "nodes": [
            {"id": 1, "source_file": "svc-a/a.py", "label": "A"},
            {"id": 2, "source_file": "svc-a/b.py", "label": "B"},
            {"id": 3, "source_file": "svc-b/c.py", "label": "C"},
        ],
        "links": [
            {"source": 1, "target": 2, "relation": "calls"},   # intra svc-a
            {"source": 2, "target": 3, "relation": "calls", "source_file": "svc-a/b.py"},
        ],
    }
    gp = _write(tmp_path / "g.json", graph)
    services = [_service("svc-a", tmp_path), _service("svc-b", tmp_path)]
    bridges = multirepo.analyze_bridges(str(gp), services)
    for name, rows in bridges.items():
        assert all(r.remote_svc != name for r in rows)
    assert len(bridges["svc-a"]) == 1
    assert bridges["svc-a"][0].remote_svc == "svc-b"


def test_neighbors_fallback_only_returns_code_nodes(tmp_path):
    # A doc/non-code node should never be reported as a symbol's definition.
    graph = {
        "nodes": [
            {"id": 1, "source_file": "docs/readme.md", "label": "payment flow",
             "file_type": "document"},
            {"id": 2, "source_file": "src/pay.py", "label": "PaymentProcessor",
             "file_type": "code", "source_location": "L10"},
        ],
        "links": [],
    }
    gp = _write(tmp_path / "g.json", graph)
    nav = GraphNav(str(gp), [])
    res = nav.neighbors("payment")
    if res.get("found", True):
        assert "readme" not in res["defined_at"], (
            "fallback matched a non-code document node as a symbol definition"
        )
