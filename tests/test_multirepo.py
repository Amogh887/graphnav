from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from codex_graph.config import MonoConfig
from codex_graph.multirepo import (
    BridgeRow,
    ServiceInfo,
    _find_env_file,
    _has_source_files,
    _load_env_file,
    _service_of,
    _stream_proc,
    analyze_bridges,
    build_overarching_graph,
    detect_services,
    partition_graph,
    run_extract,
    run_map,
    run_watch,
    write_bridges_md,
    write_copilot_instructions,
    write_monorepo_map,
)
from tests.conftest import make_mock_proc, write_graph


# ── _find_env_file ────────────────────────────────────────────────────────────

class TestFindEnvFile:
    def test_finds_env_in_start_dir(self, tmp_path):
        (tmp_path / ".env").write_text("X=1\n")
        assert _find_env_file(str(tmp_path)) == str(tmp_path / ".env")

    def test_walks_up_to_parent(self, tmp_path):
        child = tmp_path / "a" / "b"
        child.mkdir(parents=True)
        (tmp_path / ".env").write_text("X=1\n")
        assert _find_env_file(str(child)) == str(tmp_path / ".env")

    def test_returns_none_when_not_found(self, tmp_path):
        child = tmp_path / "sub"
        child.mkdir()
        assert _find_env_file(str(child)) is None

    def test_prefers_closer_env_file(self, tmp_path):
        child = tmp_path / "sub"
        child.mkdir()
        (tmp_path / ".env").write_text("SOURCE=parent\n")
        (child / ".env").write_text("SOURCE=child\n")
        assert _find_env_file(str(child)) == str(child / ".env")


# ── _load_env_file ────────────────────────────────────────────────────────────

class TestLoadEnvFile:
    def test_returns_empty_when_no_env_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _load_env_file(str(tmp_path)) == {}

    def test_parses_simple_key_value(self, tmp_path):
        (tmp_path / ".env").write_text("FOO=bar\nBAZ=qux\n")
        result = _load_env_file(str(tmp_path))
        assert result["FOO"] == "bar"
        assert result["BAZ"] == "qux"

    def test_strips_double_quotes(self, tmp_path):
        (tmp_path / ".env").write_text('KEY="my value"\n')
        assert _load_env_file(str(tmp_path))["KEY"] == "my value"

    def test_strips_single_quotes(self, tmp_path):
        (tmp_path / ".env").write_text("KEY='my value'\n")
        assert _load_env_file(str(tmp_path))["KEY"] == "my value"

    def test_skips_comment_lines(self, tmp_path):
        (tmp_path / ".env").write_text("# comment\nKEY=val\n")
        result = _load_env_file(str(tmp_path))
        assert "# comment" not in result
        assert result["KEY"] == "val"

    def test_skips_blank_lines(self, tmp_path):
        (tmp_path / ".env").write_text("\n\nKEY=val\n\n")
        assert _load_env_file(str(tmp_path))["KEY"] == "val"

    def test_anthropic_key_mapped_to_anthropic_api_key(self, tmp_path):
        (tmp_path / ".env").write_text("ANTHROPIC_KEY=sk-test-123\n")
        result = _load_env_file(str(tmp_path))
        assert result["ANTHROPIC_API_KEY"] == "sk-test-123"

    def test_anthropic_api_key_not_overwritten_if_set(self, tmp_path):
        (tmp_path / ".env").write_text("ANTHROPIC_KEY=old\nANTHROPIC_API_KEY=new\n")
        result = _load_env_file(str(tmp_path))
        assert result["ANTHROPIC_API_KEY"] == "new"

    def test_value_with_equals_sign_preserves_rest(self, tmp_path):
        (tmp_path / ".env").write_text("KEY=val=with=equals\n")
        assert _load_env_file(str(tmp_path))["KEY"] == "val=with=equals"

    def test_walks_up_tree_when_env_not_in_root(self, tmp_path, monkeypatch):
        child = tmp_path / "sub" / "project"
        child.mkdir(parents=True)
        (tmp_path / ".env").write_text("FROM_PARENT=yes\n")
        monkeypatch.chdir(child)
        result = _load_env_file(str(child))
        assert result.get("FROM_PARENT") == "yes"

    def test_finds_env_in_immediate_subdir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        svc = tmp_path / "backend"
        svc.mkdir()
        (svc / ".env").write_text("ANTHROPIC_API_KEY=sk-from-subdir\n")
        result = _load_env_file(str(tmp_path))
        assert result.get("ANTHROPIC_API_KEY") == "sk-from-subdir"

    def test_root_env_takes_precedence_over_subdir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("KEY=root\n")
        svc = tmp_path / "svc"
        svc.mkdir()
        (svc / ".env").write_text("KEY=subdir\n")
        assert _load_env_file(str(tmp_path))["KEY"] == "root"

    def test_export_prefix_stripped(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("export ANTHROPIC_API_KEY=sk-exported\n")
        assert _load_env_file(str(tmp_path))["ANTHROPIC_API_KEY"] == "sk-exported"


# ── _service_of ──────────────────────────────────────────────────────────────

class TestServiceOf:
    def test_empty_string_returns_none(self):
        assert _service_of("", {"svc-a"}) is None

    def test_prefix_in_set_returns_prefix(self):
        assert _service_of("svc-a/models.py", {"svc-a", "svc-b"}) == "svc-a"

    def test_prefix_not_in_set_returns_none(self):
        assert _service_of("unknown/file.py", {"svc-a"}) is None

    def test_deep_path_uses_first_component(self):
        assert _service_of("svc-b/deep/nested/file.py", {"svc-b"}) == "svc-b"

    def test_no_slash_returns_none_if_not_in_set(self):
        assert _service_of("standalone.py", {"svc-a"}) is None

    def test_no_slash_matches_if_in_set(self):
        assert _service_of("svc-a", {"svc-a"}) == "svc-a"


# ── detect_services ──────────────────────────────────────────────────────────

class TestDetectServices:
    def test_empty_root(self, tmp_path):
        assert detect_services(str(tmp_path), ["pyproject.toml"]) == []

    def test_nonexistent_root(self, tmp_path):
        assert detect_services(str(tmp_path / "missing"), ["pyproject.toml"]) == []

    def test_dir_without_markers(self, tmp_path):
        (tmp_path / "svc").mkdir()
        assert detect_services(str(tmp_path), ["pyproject.toml"]) == []

    def test_single_service_detected(self, tmp_path):
        d = tmp_path / "svc-a"
        d.mkdir()
        (d / "pyproject.toml").touch()
        result = detect_services(str(tmp_path), ["pyproject.toml"])
        assert len(result) == 1
        assert result[0].name == "svc-a"
        assert result[0].abs_path == str(d)
        assert result[0].graph_path == str(d / "graphify-out" / "graph.json")
        assert result[0].bridges_to == []

    def test_services_returned_in_sorted_order(self, tmp_path):
        for name in ("zebra", "alpha", "middle"):
            d = tmp_path / name
            d.mkdir()
            (d / "package.json").touch()
        result = detect_services(str(tmp_path), ["package.json"])
        assert [s.name for s in result] == ["alpha", "middle", "zebra"]

    def test_multiple_marker_types_all_detected(self, tmp_path):
        markers = {"py-svc": "pyproject.toml", "js-svc": "package.json", "go-svc": "go.mod"}
        for name, marker in markers.items():
            d = tmp_path / name
            d.mkdir()
            (d / marker).touch()
        result = detect_services(str(tmp_path), list(markers.values()))
        assert {s.name for s in result} == set(markers.keys())

    def test_dir_with_multiple_markers_counted_once(self, tmp_path):
        d = tmp_path / "svc"
        d.mkdir()
        (d / "pyproject.toml").touch()
        (d / "package.json").touch()
        result = detect_services(str(tmp_path), ["pyproject.toml", "package.json"])
        assert len(result) == 1

    def test_root_level_files_ignored(self, tmp_path):
        (tmp_path / "pyproject.toml").touch()
        assert detect_services(str(tmp_path), ["pyproject.toml"]) == []

    def test_nested_subdirs_not_traversed(self, tmp_path):
        outer = tmp_path / "outer"
        outer.mkdir()
        (outer / "pyproject.toml").touch()
        inner = outer / "inner"
        inner.mkdir()
        (inner / "pyproject.toml").touch()
        result = detect_services(str(tmp_path), ["pyproject.toml"])
        assert len(result) == 1
        assert result[0].name == "outer"

    def test_all_common_marker_files_detected(self, tmp_path):
        markers = ["package.json", "pyproject.toml", "go.mod", "Cargo.toml",
                   "pom.xml", "build.gradle", "setup.py", "setup.cfg"]
        for i, marker in enumerate(markers):
            d = tmp_path / f"svc{i}"
            d.mkdir()
            (d / marker).touch()
        result = detect_services(str(tmp_path), markers)
        assert len(result) == len(markers)

    def test_source_only_dir_detected_without_marker(self, tmp_path):
        d = tmp_path / "api"
        d.mkdir()
        (d / "index.py").write_text("x = 1\n")
        result = detect_services(str(tmp_path), ["package.json"])
        assert [s.name for s in result] == ["api"]

    def test_nested_source_detected(self, tmp_path):
        d = tmp_path / "frontend"
        (d / "src").mkdir(parents=True)
        (d / "src" / "App.tsx").write_text("export default 1\n")
        result = detect_services(str(tmp_path), [])
        assert [s.name for s in result] == ["frontend"]

    def test_dir_with_only_data_files_not_detected(self, tmp_path):
        d = tmp_path / "data"
        d.mkdir()
        (d / "results.json").write_text("{}")
        (d / "notes.md").write_text("# notes")
        assert detect_services(str(tmp_path), ["package.json"]) == []

    def test_node_modules_skipped(self, tmp_path):
        nm = tmp_path / "node_modules"
        (nm / "pkg").mkdir(parents=True)
        (nm / "pkg" / "index.js").write_text("module.exports = 1\n")
        assert detect_services(str(tmp_path), []) == []

    def test_graphify_out_skipped(self, tmp_path):
        d = tmp_path / "graphify-out"
        d.mkdir()
        (d / "graph.json").write_text("{}")
        assert detect_services(str(tmp_path), []) == []

    def test_dotdir_skipped(self, tmp_path):
        d = tmp_path / ".git"
        d.mkdir()
        (d / "hook.py").write_text("x = 1\n")
        assert detect_services(str(tmp_path), []) == []

    def test_source_extension_inside_node_modules_of_service_still_detects_service(self, tmp_path):
        d = tmp_path / "frontend"
        d.mkdir()
        (d / "package.json").write_text("{}")
        nm = d / "node_modules" / "dep"
        nm.mkdir(parents=True)
        (nm / "x.js").write_text("1\n")
        result = detect_services(str(tmp_path), ["package.json"])
        assert [s.name for s in result] == ["frontend"]


# ── _has_source_files ────────────────────────────────────────────────────────

class TestHasSourceFiles:
    def test_detects_python(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        assert _has_source_files(str(tmp_path)) is True

    def test_detects_nested(self, tmp_path):
        sub = tmp_path / "src" / "deep"
        sub.mkdir(parents=True)
        (sub / "a.ts").write_text("1\n")
        assert _has_source_files(str(tmp_path)) is True

    def test_ignores_data_files(self, tmp_path):
        (tmp_path / "a.json").write_text("{}")
        (tmp_path / "b.csv").write_text("1,2\n")
        assert _has_source_files(str(tmp_path)) is False

    def test_prunes_skip_dirs(self, tmp_path):
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "x.js").write_text("1\n")
        assert _has_source_files(str(tmp_path)) is False

    def test_respects_max_depth(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "x.py").write_text("1\n")
        assert _has_source_files(str(tmp_path), max_depth=2) is False


# ── _stream_proc ─────────────────────────────────────────────────────────────

class TestStreamProc:
    def test_returns_zero_for_success(self):
        proc = subprocess.Popen(
            ["python3", "-c", "import sys; sys.exit(0)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert _stream_proc(proc, timeout=10) == 0

    def test_returns_nonzero_for_failure(self):
        proc = subprocess.Popen(
            ["python3", "-c", "import sys; sys.exit(2)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert _stream_proc(proc, timeout=10) == 2

    def test_streams_stderr_output(self, capsys):
        proc = subprocess.Popen(
            ["python3", "-c", "import sys; print('err-line', file=sys.stderr)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _stream_proc(proc, timeout=10)
        err = capsys.readouterr().err
        assert "err-line" in err

    def test_kills_on_timeout(self):
        proc = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(100)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        rc = _stream_proc(proc, timeout=1)
        assert proc.returncode is not None


# ── run_extract ───────────────────────────────────────────────────────────────

class TestRunExtract:
    def test_calls_graphify_with_correct_args(self, tmp_path):
        svc = ServiceInfo("my-svc", str(tmp_path / "my-svc"), str(tmp_path / "my-svc" / "graphify-out" / "graph.json"))
        mock_proc = make_mock_proc(returncode=0)
        with patch("codex_graph.multirepo.subprocess.Popen", return_value=mock_proc) as mock_popen:
            run_extract(svc, "/usr/bin/graphify", "claude")
        args = mock_popen.call_args[0][0]
        assert args[0] == "/usr/bin/graphify"
        assert args[1] == "extract"
        assert svc.abs_path in args
        assert "--backend" in args
        assert "claude" in args
        assert "--out" in args

    def test_passes_backend_override(self, tmp_path):
        svc = ServiceInfo("svc", str(tmp_path), str(tmp_path / "graphify-out" / "graph.json"))
        mock_proc = make_mock_proc(0)
        with patch("codex_graph.multirepo.subprocess.Popen", return_value=mock_proc) as mock_popen:
            run_extract(svc, "/graphify", "openai")
        args = mock_popen.call_args[0][0]
        idx = args.index("--backend")
        assert args[idx + 1] == "openai"

    def test_returns_exit_code(self, tmp_path):
        svc = ServiceInfo("svc", str(tmp_path), str(tmp_path / "graphify-out" / "graph.json"))
        mock_proc = make_mock_proc(returncode=1)
        with patch("codex_graph.multirepo.subprocess.Popen", return_value=mock_proc):
            rc = run_extract(svc, "/graphify", "claude")
        assert rc == 1


# ── build_overarching_graph ──────────────────────────────────────────────────

class TestBuildOverarchingGraph:
    def test_extracts_root_path(self, tmp_path):
        mock_proc = make_mock_proc(0)
        with patch("codex_graph.multirepo.subprocess.Popen", return_value=mock_proc) as mock_popen:
            build_overarching_graph(str(tmp_path), "/graphify", "claude")
        args = mock_popen.call_args[0][0]
        assert args[:2] == ["/graphify", "extract"]
        assert str(tmp_path) in args
        idx = args.index("--out")
        assert args[idx + 1] == str(tmp_path)

    def test_returns_exit_code(self, tmp_path):
        mock_proc = make_mock_proc(returncode=3)
        with patch("codex_graph.multirepo.subprocess.Popen", return_value=mock_proc):
            rc = build_overarching_graph(str(tmp_path), "/graphify", "claude")
        assert rc == 3

    def test_passes_env(self, tmp_path):
        mock_proc = make_mock_proc(0)
        env = {"ANTHROPIC_API_KEY": "sk-test"}
        with patch("codex_graph.multirepo.subprocess.Popen", return_value=mock_proc) as mock_popen:
            build_overarching_graph(str(tmp_path), "/graphify", "claude", env=env)
        assert mock_popen.call_args.kwargs["env"] == env


# ── partition_graph ──────────────────────────────────────────────────────────

class TestPartitionGraph:
    def _overarching(self, tmp_path):
        graph = {
            "directed": True,
            "nodes": [
                {"id": "a1", "label": "handler", "source_file": "api/index.py"},
                {"id": "b1", "label": "coach", "source_file": "backend/coach.py"},
                {"id": "b2", "label": "main", "source_file": "backend/main.py"},
                {"id": "e1", "label": "run", "source_file": "eval/run.py"},
                {"id": "r1", "label": "root_helper", "source_file": "setup.py"},
            ],
            "links": [
                {"source": "b1", "target": "b2", "relation": "calls", "source_file": "backend/coach.py"},
                {"source": "e1", "target": "b1", "relation": "calls", "source_file": "eval/run.py"},
                {"source": "a1", "target": "r1", "relation": "imports", "source_file": "api/index.py"},
            ],
        }
        p = tmp_path / "graphify-out" / "graph.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(graph))
        return str(p)

    def _services(self, tmp_path):
        return [
            ServiceInfo(n, str(tmp_path / n), str(tmp_path / n / "graphify-out" / "graph.json"))
            for n in ("api", "backend", "eval")
        ]

    def test_writes_per_service_graphs(self, tmp_path):
        overarching = self._overarching(tmp_path)
        services = self._services(tmp_path)
        counts = partition_graph(overarching, services)
        assert counts == {"api": 1, "backend": 2, "eval": 1}
        for svc in services:
            assert os.path.exists(svc.graph_path)

    def test_service_graph_contains_only_its_nodes(self, tmp_path):
        overarching = self._overarching(tmp_path)
        services = self._services(tmp_path)
        partition_graph(overarching, services)
        backend = next(s for s in services if s.name == "backend")
        g = json.loads(open(backend.graph_path).read())
        labels = {n["label"] for n in g["nodes"]}
        assert labels == {"coach", "main"}

    def test_intra_service_links_kept(self, tmp_path):
        overarching = self._overarching(tmp_path)
        services = self._services(tmp_path)
        partition_graph(overarching, services)
        backend = next(s for s in services if s.name == "backend")
        g = json.loads(open(backend.graph_path).read())
        assert len(g["links"]) == 1
        assert g["links"][0]["relation"] == "calls"

    def test_cross_service_links_excluded_from_service_graph(self, tmp_path):
        overarching = self._overarching(tmp_path)
        services = self._services(tmp_path)
        partition_graph(overarching, services)
        eval_svc = next(s for s in services if s.name == "eval")
        g = json.loads(open(eval_svc.graph_path).read())
        assert g["links"] == []

    def test_root_level_nodes_excluded(self, tmp_path):
        overarching = self._overarching(tmp_path)
        services = self._services(tmp_path)
        counts = partition_graph(overarching, services)
        assert sum(counts.values()) == 4

    def test_preserves_top_level_metadata(self, tmp_path):
        overarching = self._overarching(tmp_path)
        services = self._services(tmp_path)
        partition_graph(overarching, services)
        g = json.loads(open(services[0].graph_path).read())
        assert g.get("directed") is True

    def test_handles_edges_key(self, tmp_path):
        graph = {
            "nodes": [
                {"id": "b1", "label": "coach", "source_file": "backend/coach.py"},
                {"id": "b2", "label": "main", "source_file": "backend/main.py"},
            ],
            "edges": [
                {"source": "b1", "target": "b2", "relation": "calls", "source_file": "backend/coach.py"},
            ],
        }
        p = tmp_path / "graphify-out" / "graph.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(graph))
        services = [ServiceInfo("backend", str(tmp_path / "backend"), str(tmp_path / "backend" / "graphify-out" / "graph.json"))]
        partition_graph(str(p), services)
        g = json.loads(open(services[0].graph_path).read())
        assert len(g["links"]) == 1


# ── analyze_bridges ──────────────────────────────────────────────────────────

class TestAnalyzeBridges:
    def test_no_cross_service_links(self, tmp_path):
        nodes = [
            {"id": "n1", "label": "Foo", "source_file": "svc-a/foo.py"},
            {"id": "n2", "label": "Bar", "source_file": "svc-a/bar.py"},
        ]
        links = [{"source": "n1", "target": "n2", "relation": "calls", "source_file": "svc-a/foo.py"}]
        graph_path = tmp_path / "merged.json"
        write_graph(graph_path, nodes, links)
        services = [
            ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/graphify-out/graph.json")),
            ServiceInfo("svc-b", str(tmp_path / "svc-b"), str(tmp_path / "svc-b/graphify-out/graph.json")),
        ]
        result = analyze_bridges(str(graph_path), services)
        assert result["svc-a"] == []
        assert result["svc-b"] == []

    def test_cross_service_link_creates_bridge_row(self, cross_service_graph):
        graph_path, services = cross_service_graph
        result = analyze_bridges(str(graph_path), services)
        assert len(result["svc-a"]) == 1
        row = result["svc-a"][0]
        assert row.local_file == "client.py"
        assert row.local_symbol == "Client"
        assert row.relation == "calls"
        assert row.remote_svc == "svc-b"
        assert row.remote_file == "svc-b/server.py"
        assert row.remote_symbol == "Server"

    def test_target_side_gets_empty_list(self, cross_service_graph):
        graph_path, services = cross_service_graph
        result = analyze_bridges(str(graph_path), services)
        assert result["svc-b"] == []

    def test_bridges_to_populated_on_service(self, cross_service_graph):
        graph_path, services = cross_service_graph
        analyze_bridges(str(graph_path), services)
        svc_a = next(s for s in services if s.name == "svc-a")
        assert svc_a.bridges_to == ["svc-b"]

    def test_bridges_to_empty_for_unconnected_service(self, cross_service_graph):
        graph_path, services = cross_service_graph
        analyze_bridges(str(graph_path), services)
        svc_b = next(s for s in services if s.name == "svc-b")
        assert svc_b.bridges_to == []

    def test_stdlib_nodes_skipped(self, tmp_path):
        nodes = [
            {"id": "svc_a_foo", "label": "Foo", "source_file": "svc-a/foo.py"},
            {"id": "exception", "label": "Exception", "source_file": ""},
        ]
        links = [
            {"source": "svc_a_foo", "target": "exception", "relation": "inherits", "source_file": "svc-a/foo.py"}
        ]
        graph_path = tmp_path / "merged.json"
        write_graph(graph_path, nodes, links)
        services = [ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/g.json"))]
        result = analyze_bridges(str(graph_path), services)
        assert result["svc-a"] == []

    def test_link_with_unknown_node_id_skipped(self, tmp_path):
        nodes = [{"id": "n1", "label": "Foo", "source_file": "svc-a/foo.py"}]
        links = [{"source": "n1", "target": "nonexistent_id", "relation": "calls", "source_file": "svc-a/foo.py"}]
        graph_path = tmp_path / "merged.json"
        write_graph(graph_path, nodes, links)
        services = [
            ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/g.json")),
            ServiceInfo("svc-b", str(tmp_path / "svc-b"), str(tmp_path / "svc-b/g.json")),
        ]
        result = analyze_bridges(str(graph_path), services)
        assert result["svc-a"] == []

    def test_link_sf_attribution_flips_when_in_target_service(self, tmp_path):
        nodes = [
            {"id": "svc_a_client", "label": "Client", "source_file": "svc-a/client.py"},
            {"id": "svc_b_server", "label": "Server", "source_file": "svc-b/server.py"},
        ]
        links = [
            {
                "source": "svc_a_client",
                "target": "svc_b_server",
                "relation": "imported-by",
                "source_file": "svc-b/server.py",
            }
        ]
        graph_path = tmp_path / "merged.json"
        write_graph(graph_path, nodes, links)
        services = [
            ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/g.json")),
            ServiceInfo("svc-b", str(tmp_path / "svc-b"), str(tmp_path / "svc-b/g.json")),
        ]
        result = analyze_bridges(str(graph_path), services)
        assert result["svc-b"] != []
        row = result["svc-b"][0]
        assert row.local_symbol == "Server"
        assert row.remote_svc == "svc-a"

    def test_multiple_cross_service_links(self, tmp_path):
        nodes = [
            {"id": "svc_a_foo", "label": "Foo", "source_file": "svc-a/foo.py"},
            {"id": "svc_b_bar", "label": "Bar", "source_file": "svc-b/bar.py"},
            {"id": "svc_b_baz", "label": "Baz", "source_file": "svc-b/baz.py"},
        ]
        links = [
            {"source": "svc_a_foo", "target": "svc_b_bar", "relation": "calls", "source_file": "svc-a/foo.py"},
            {"source": "svc_a_foo", "target": "svc_b_baz", "relation": "uses", "source_file": "svc-a/foo.py"},
        ]
        graph_path = tmp_path / "merged.json"
        write_graph(graph_path, nodes, links)
        services = [
            ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/g.json")),
            ServiceInfo("svc-b", str(tmp_path / "svc-b"), str(tmp_path / "svc-b/g.json")),
        ]
        result = analyze_bridges(str(graph_path), services)
        assert len(result["svc-a"]) == 2

    def test_bridges_to_deduplicated_and_sorted(self, tmp_path):
        nodes = [
            {"id": "svc_a_foo", "label": "Foo", "source_file": "svc-a/foo.py"},
            {"id": "svc_b_bar", "label": "Bar", "source_file": "svc-b/bar.py"},
            {"id": "svc_c_baz", "label": "Baz", "source_file": "svc-c/baz.py"},
        ]
        links = [
            {"source": "svc_a_foo", "target": "svc_b_bar", "relation": "calls", "source_file": "svc-a/foo.py"},
            {"source": "svc_a_foo", "target": "svc_c_baz", "relation": "calls", "source_file": "svc-a/foo.py"},
        ]
        graph_path = tmp_path / "merged.json"
        write_graph(graph_path, nodes, links)
        services = [
            ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/g.json")),
            ServiceInfo("svc-b", str(tmp_path / "svc-b"), str(tmp_path / "svc-b/g.json")),
            ServiceInfo("svc-c", str(tmp_path / "svc-c"), str(tmp_path / "svc-c/g.json")),
        ]
        analyze_bridges(str(graph_path), services)
        svc_a = next(s for s in services if s.name == "svc-a")
        assert svc_a.bridges_to == sorted(svc_a.bridges_to)
        assert len(set(svc_a.bridges_to)) == len(svc_a.bridges_to)


# ── write_bridges_md ─────────────────────────────────────────────────────────

class TestWriteBridgesMd:
    def test_no_rows_writes_no_connections_message(self, tmp_path):
        svc = ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/g.json"))
        path = write_bridges_md(svc, [])
        content = Path(path).read_text()
        assert "# Bridges: svc-a" in content
        assert "_No cross-service connections detected._" in content

    def test_rows_written_as_table(self, tmp_path):
        svc = ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/g.json"))
        rows = [BridgeRow("client.py", "Client", "calls", "svc-b", "svc-b/server.py", "Server")]
        path = write_bridges_md(svc, rows)
        content = Path(path).read_text()
        assert "| Local File | Symbol | Relation | → Service | Remote File | Remote Symbol |" in content
        assert "| client.py | Client | calls | svc-b | svc-b/server.py | Server |" in content

    def test_creates_graphify_out_directory(self, tmp_path):
        svc = ServiceInfo("svc-x", str(tmp_path / "svc-x"), str(tmp_path / "svc-x/g.json"))
        write_bridges_md(svc, [])
        assert (tmp_path / "svc-x" / "graphify-out").is_dir()

    def test_returns_correct_path(self, tmp_path):
        svc = ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/g.json"))
        path = write_bridges_md(svc, [])
        assert path == str(tmp_path / "svc-a" / "graphify-out" / "BRIDGES.md")
        assert os.path.exists(path)

    def test_header_contains_service_name(self, tmp_path):
        svc = ServiceInfo("my-service", str(tmp_path / "my-service"), str(tmp_path / "x/g.json"))
        path = write_bridges_md(svc, [])
        assert "# Bridges: my-service" in Path(path).read_text()

    def test_multiple_rows_all_written(self, tmp_path):
        svc = ServiceInfo("svc", str(tmp_path / "svc"), str(tmp_path / "svc/g.json"))
        rows = [
            BridgeRow("a.py", "A", "calls", "svc-b", "svc-b/b.py", "B"),
            BridgeRow("c.py", "C", "imports", "svc-c", "svc-c/d.py", "D"),
        ]
        path = write_bridges_md(svc, rows)
        content = Path(path).read_text()
        assert "| a.py | A |" in content
        assert "| c.py | C |" in content

    def test_no_table_when_no_rows(self, tmp_path):
        svc = ServiceInfo("svc", str(tmp_path / "svc"), str(tmp_path / "svc/g.json"))
        path = write_bridges_md(svc, [])
        content = Path(path).read_text()
        assert "|---|" not in content


# ── write_monorepo_map ────────────────────────────────────────────────────────

class TestWriteMonorepoMap:
    def test_creates_file_at_correct_path(self, tmp_path):
        services = [ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/graphify-out/graph.json"))]
        path = write_monorepo_map(str(tmp_path), services)
        assert path == str(tmp_path / "graphify-out" / "MONOREPO_MAP.md")
        assert os.path.exists(path)

    def test_creates_graphify_out_directory(self, tmp_path):
        services = [ServiceInfo("svc", str(tmp_path / "svc"), str(tmp_path / "svc/g.json"))]
        write_monorepo_map(str(tmp_path), services)
        assert (tmp_path / "graphify-out").is_dir()

    def test_header_present(self, tmp_path):
        services = [ServiceInfo("svc", str(tmp_path / "svc"), str(tmp_path / "svc/g.json"))]
        path = write_monorepo_map(str(tmp_path), services)
        assert "# Monorepo Map" in Path(path).read_text()

    def test_service_row_present(self, tmp_path):
        svc = ServiceInfo("auth-svc", str(tmp_path / "auth-svc"), str(tmp_path / "auth-svc/graphify-out/graph.json"))
        path = write_monorepo_map(str(tmp_path), [svc])
        content = Path(path).read_text()
        assert "auth-svc" in content

    def test_no_bridges_shows_none(self, tmp_path):
        svc = ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/graphify-out/graph.json"))
        svc.bridges_to = []
        path = write_monorepo_map(str(tmp_path), [svc])
        assert "_none_" in Path(path).read_text()

    def test_bridges_listed_comma_separated(self, tmp_path):
        svc = ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/graphify-out/graph.json"))
        svc.bridges_to = ["svc-b", "svc-c"]
        path = write_monorepo_map(str(tmp_path), [svc])
        assert "svc-b, svc-c" in Path(path).read_text()

    def test_graph_path_is_relative(self, tmp_path):
        svc = ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/graphify-out/graph.json"))
        path = write_monorepo_map(str(tmp_path), [svc])
        content = Path(path).read_text()
        assert str(tmp_path) not in content
        assert "svc-a/graphify-out/graph.json" in content

    def test_multiple_services_all_listed(self, tmp_path):
        services = [
            ServiceInfo("auth-svc", str(tmp_path / "auth-svc"), str(tmp_path / "auth-svc/graphify-out/graph.json")),
            ServiceInfo("user-svc", str(tmp_path / "user-svc"), str(tmp_path / "user-svc/graphify-out/graph.json")),
        ]
        path = write_monorepo_map(str(tmp_path), services)
        content = Path(path).read_text()
        assert "auth-svc" in content
        assert "user-svc" in content


# ── write_copilot_instructions ───────────────────────────────────────────────

class TestWriteCopilotInstructions:
    def test_creates_file_at_github_path(self, tmp_path):
        svc = ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/graphify-out/graph.json"))
        path = write_copilot_instructions(str(tmp_path), [svc])
        assert path == str(tmp_path / ".github" / "copilot-instructions.md")
        assert os.path.exists(path)

    def test_creates_github_directory(self, tmp_path):
        svc = ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/graphify-out/graph.json"))
        write_copilot_instructions(str(tmp_path), [svc])
        assert (tmp_path / ".github").is_dir()

    def test_lists_all_services(self, tmp_path):
        services = [
            ServiceInfo("auth-svc", str(tmp_path / "auth-svc"), str(tmp_path / "auth-svc/graphify-out/graph.json")),
            ServiceInfo("user-svc", str(tmp_path / "user-svc"), str(tmp_path / "user-svc/graphify-out/graph.json")),
        ]
        path = write_copilot_instructions(str(tmp_path), services)
        content = Path(path).read_text()
        assert "auth-svc" in content
        assert "user-svc" in content

    def test_graph_paths_are_relative(self, tmp_path):
        svc = ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/graphify-out/graph.json"))
        path = write_copilot_instructions(str(tmp_path), [svc])
        content = Path(path).read_text()
        assert str(tmp_path) not in content

    def test_contains_bridges_reference(self, tmp_path):
        svc = ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/graphify-out/graph.json"))
        path = write_copilot_instructions(str(tmp_path), [svc])
        assert "BRIDGES.md" in Path(path).read_text()

    def test_contains_monorepo_map_reference(self, tmp_path):
        svc = ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/graphify-out/graph.json"))
        path = write_copilot_instructions(str(tmp_path), [svc])
        assert "MONOREPO_MAP.md" in Path(path).read_text()

    def test_contains_how_to_use_section(self, tmp_path):
        svc = ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a/graphify-out/graph.json"))
        path = write_copilot_instructions(str(tmp_path), [svc])
        assert "## How to Use" in Path(path).read_text()



# ── run_map ──────────────────────────────────────────────────────────────────

def _write_overarching(root, cross=True, single=False):
    if single:
        nodes = [
            {"id": "a1", "label": "handler", "source_file": "svc-a/client.py"},
            {"id": "a2", "label": "helper", "source_file": "svc-a/util.py"},
        ]
        links = [{"source": "a1", "target": "a2", "relation": "calls", "source_file": "svc-a/client.py"}]
    else:
        nodes = [
            {"id": "a1", "label": "handler", "source_file": "svc-a/client.py"},
            {"id": "b1", "label": "server", "source_file": "svc-b/server.py"},
            {"id": "r1", "label": "root_thing", "source_file": "setup.py"},
        ]
        links = []
        if cross:
            links.append({"source": "a1", "target": "b1", "relation": "calls", "source_file": "svc-a/client.py"})
    p = os.path.join(root, "graphify-out", "graph.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump({"directed": True, "nodes": nodes, "links": links}, f)
    return p


class TestRunMap:
    def test_graphify_not_found_returns_1(self, tmp_path, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: None)
        assert run_map(str(tmp_path), MonoConfig()) == 1

    def test_no_services_returns_1(self, tmp_path, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        assert run_map(str(tmp_path), MonoConfig()) == 1

    def test_dry_run_prints_services_and_returns_0(self, two_svc_root, monkeypatch, capsys):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        rc = run_map(str(two_svc_root), MonoConfig(), dry_run=True)
        assert rc == 0
        out = capsys.readouterr().out
        assert "svc-a" in out
        assert "svc-b" in out
        assert "[dry-run]" in out

    def test_dry_run_does_not_build(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        built = []
        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph",
                            lambda *a, **kw: built.append(True) or 0)
        run_map(str(two_svc_root), MonoConfig(), dry_run=True)
        assert built == []

    def test_overarching_failure_returns_1(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph", lambda *a, **kw: 1)
        assert run_map(str(two_svc_root), MonoConfig()) == 1

    def test_overarching_missing_file_returns_1(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph", lambda *a, **kw: 0)
        assert run_map(str(two_svc_root), MonoConfig()) == 1

    def test_full_success_returns_0(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")

        def fake_build(root, graphify_path, backend, timeout=1200, env=None):
            _write_overarching(root)
            return 0

        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph", fake_build)
        assert run_map(str(two_svc_root), MonoConfig()) == 0

    def test_writes_per_service_graphs(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph",
                            lambda root, *a, **kw: _write_overarching(root) and 0 or 0)
        run_map(str(two_svc_root), MonoConfig())
        assert (two_svc_root / "svc-a" / "graphify-out" / "graph.json").exists()
        assert (two_svc_root / "svc-b" / "graphify-out" / "graph.json").exists()

    def test_writes_bridges_map_and_copilot(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph",
                            lambda root, *a, **kw: _write_overarching(root) and 0 or 0)
        run_map(str(two_svc_root), MonoConfig())
        assert (two_svc_root / "svc-a" / "graphify-out" / "BRIDGES.md").exists()
        assert (two_svc_root / "graphify-out" / "MONOREPO_MAP.md").exists()
        assert (two_svc_root / ".github" / "copilot-instructions.md").exists()

    def test_cross_service_bridge_detected_and_reported(self, two_svc_root, monkeypatch, capsys):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph",
                            lambda root, *a, **kw: _write_overarching(root, cross=True) and 0 or 0)
        run_map(str(two_svc_root), MonoConfig())
        out = capsys.readouterr().out
        assert "1 cross-service connection" in out
        bridges_md = (two_svc_root / "svc-a" / "graphify-out" / "BRIDGES.md").read_text()
        assert "svc-b" in bridges_md
        assert "server" in bridges_md

    def test_no_cross_service_when_no_links(self, two_svc_root, monkeypatch, capsys):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph",
                            lambda root, *a, **kw: _write_overarching(root, cross=False) and 0 or 0)
        run_map(str(two_svc_root), MonoConfig())
        out = capsys.readouterr().out
        assert "0 cross-service connection" in out

    def test_backend_override_passed(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        backends = []

        def fake_build(root, graphify_path, backend, timeout=1200, env=None):
            backends.append(backend)
            _write_overarching(root)
            return 0

        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph", fake_build)
        run_map(str(two_svc_root), MonoConfig(), backend_override="openai")
        assert backends == ["openai"]

    def test_config_backend_used_when_no_override(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        backends = []

        def fake_build(root, graphify_path, backend, timeout=1200, env=None):
            backends.append(backend)
            _write_overarching(root)
            return 0

        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph", fake_build)
        run_map(str(two_svc_root), MonoConfig(graphify_backend="gemini"))
        assert backends == ["gemini"]

    def test_single_service_returns_0(self, tmp_path, monkeypatch, capsys):
        d = tmp_path / "svc-a"
        d.mkdir()
        (d / "pyproject.toml").touch()
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph",
                            lambda root, *a, **kw: _write_overarching(root, single=True) and 0 or 0)
        assert run_map(str(tmp_path), MonoConfig()) == 0

    def test_root_path_resolved_to_absolute(self, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        roots_seen = []

        def fake_detect(root, markers):
            roots_seen.append(root)
            return []

        monkeypatch.setattr("codex_graph.multirepo.detect_services", fake_detect)
        run_map(".", MonoConfig())
        assert os.path.isabs(roots_seen[0])

    def test_env_built_from_root(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        envs = []

        def fake_build(root, graphify_path, backend, timeout=1200, env=None):
            envs.append(env)
            _write_overarching(root)
            return 0

        (two_svc_root / ".env").write_text("ANTHROPIC_KEY=sk-xyz\n")
        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph", fake_build)
        run_map(str(two_svc_root), MonoConfig())
        assert envs[0].get("ANTHROPIC_API_KEY") == "sk-xyz"


# ── run_watch ────────────────────────────────────────────────────────────────

class TestRunWatch:
    def test_graphify_not_found_returns_1(self, tmp_path, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: None)
        assert run_watch(str(tmp_path), MonoConfig()) == 1

    def test_no_services_returns_1(self, tmp_path, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        assert run_watch(str(tmp_path), MonoConfig()) == 1

    def test_bootstrap_builds_when_overarching_missing(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        built = []

        def fake_build(root, graphify_path, backend, timeout=1200, env=None):
            built.append(True)
            _write_overarching(root)
            return 0

        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph", fake_build)
        monkeypatch.setattr("codex_graph.multirepo._refresh", lambda *a, **kw: {})
        monkeypatch.setattr("codex_graph.multirepo.subprocess.Popen", lambda *a, **kw: make_mock_proc(None))
        monkeypatch.setattr("codex_graph.multirepo.time.sleep",
                            lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        run_watch(str(two_svc_root), MonoConfig())
        assert built == [True]

    def test_skips_build_when_overarching_exists(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        _write_overarching(str(two_svc_root))
        built = []
        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph",
                            lambda *a, **kw: built.append(True) or 0)
        monkeypatch.setattr("codex_graph.multirepo._refresh", lambda *a, **kw: {})
        monkeypatch.setattr("codex_graph.multirepo.subprocess.Popen", lambda *a, **kw: make_mock_proc(None))
        monkeypatch.setattr("codex_graph.multirepo.time.sleep",
                            lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        run_watch(str(two_svc_root), MonoConfig())
        assert built == []

    def test_bootstrap_failure_returns_1(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo.build_overarching_graph", lambda *a, **kw: 1)
        assert run_watch(str(two_svc_root), MonoConfig()) == 1

    def test_keyboard_interrupt_returns_0(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        _write_overarching(str(two_svc_root))
        monkeypatch.setattr("codex_graph.multirepo._refresh", lambda *a, **kw: {})
        monkeypatch.setattr("codex_graph.multirepo.subprocess.Popen", lambda *a, **kw: make_mock_proc(None))
        monkeypatch.setattr("codex_graph.multirepo.time.sleep",
                            lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        assert run_watch(str(two_svc_root), MonoConfig()) == 0

    def test_keyboard_interrupt_terminates_watch_proc(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        _write_overarching(str(two_svc_root))
        monkeypatch.setattr("codex_graph.multirepo._refresh", lambda *a, **kw: {})
        proc = make_mock_proc(None)
        proc.poll.return_value = None
        monkeypatch.setattr("codex_graph.multirepo.subprocess.Popen", lambda *a, **kw: proc)
        monkeypatch.setattr("codex_graph.multirepo.time.sleep",
                            lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        run_watch(str(two_svc_root), MonoConfig())
        assert proc.terminate.called

    def test_mtime_change_triggers_refresh(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        overarching = _write_overarching(str(two_svc_root))

        refreshes = []
        monkeypatch.setattr("codex_graph.multirepo._refresh",
                            lambda *a, **kw: refreshes.append(1) or {})
        proc = make_mock_proc(None)
        proc.poll.return_value = None
        monkeypatch.setattr("codex_graph.multirepo.subprocess.Popen", lambda *a, **kw: proc)

        sleep_count = [0]
        def fake_sleep(_):
            sleep_count[0] += 1
            if sleep_count[0] == 1:
                os.utime(overarching, (9_999_999_999.0, 9_999_999_999.0))
            elif sleep_count[0] >= 2:
                raise KeyboardInterrupt

        monkeypatch.setattr("codex_graph.multirepo.time.sleep", fake_sleep)
        run_watch(str(two_svc_root), MonoConfig(watch_poll_interval=0.001))
        assert len(refreshes) >= 2

    def test_dead_watch_proc_restarted(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        _write_overarching(str(two_svc_root))
        monkeypatch.setattr("codex_graph.multirepo._refresh", lambda *a, **kw: {})

        popen_calls = [0]

        def make_popen(*args, **kwargs):
            proc = make_mock_proc(None)
            proc.poll.return_value = 1 if popen_calls[0] < 1 else None
            popen_calls[0] += 1
            return proc

        monkeypatch.setattr("codex_graph.multirepo.subprocess.Popen", make_popen)

        sleep_count = [0]
        def fake_sleep(_):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                raise KeyboardInterrupt

        monkeypatch.setattr("codex_graph.multirepo.time.sleep", fake_sleep)
        run_watch(str(two_svc_root), MonoConfig(watch_poll_interval=0.001))
        assert popen_calls[0] >= 2
