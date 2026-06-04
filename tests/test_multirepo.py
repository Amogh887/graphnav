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
    _load_env_file,
    _service_of,
    _stream_proc,
    analyze_bridges,
    detect_services,
    run_extract,
    run_map,
    run_merge,
    run_watch,
    write_bridges_md,
    write_monorepo_map,
)
from tests.conftest import make_mock_proc, write_graph


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

    def test_cwd_env_file_used_as_fallback(self, tmp_path, monkeypatch):
        other = tmp_path / "other"
        other.mkdir()
        (tmp_path / ".env").write_text("FROM_CWD=yes\n")
        monkeypatch.chdir(tmp_path)
        result = _load_env_file(str(other))
        assert result.get("FROM_CWD") == "yes"


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


# ── run_merge ────────────────────────────────────────────────────────────────

class TestRunMerge:
    def test_calls_graphify_merge_with_all_graphs(self, tmp_path):
        services = [
            ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a" / "graphify-out" / "graph.json")),
            ServiceInfo("svc-b", str(tmp_path / "svc-b"), str(tmp_path / "svc-b" / "graphify-out" / "graph.json")),
        ]
        merged_out = str(tmp_path / "graphify-out" / "merged-graph.json")
        mock_proc = make_mock_proc(0)
        with patch("codex_graph.multirepo.subprocess.Popen", return_value=mock_proc) as mock_popen:
            run_merge(services, "/graphify", merged_out)
        args = mock_popen.call_args[0][0]
        assert "merge-graphs" in args
        assert services[0].graph_path in args
        assert services[1].graph_path in args
        assert "--out" in args
        assert merged_out in args

    def test_creates_output_directory(self, tmp_path):
        services = [
            ServiceInfo("a", str(tmp_path / "a"), str(tmp_path / "a" / "g.json")),
        ]
        merged_out = str(tmp_path / "deep" / "nested" / "merged.json")
        mock_proc = make_mock_proc(0)
        with patch("codex_graph.multirepo.subprocess.Popen", return_value=mock_proc):
            run_merge(services, "/graphify", merged_out)
        assert os.path.isdir(str(tmp_path / "deep" / "nested"))

    def test_returns_exit_code(self, tmp_path):
        services = [ServiceInfo("a", str(tmp_path), str(tmp_path / "g.json"))]
        mock_proc = make_mock_proc(returncode=2)
        merged_out = str(tmp_path / "graphify-out" / "merged.json")
        with patch("codex_graph.multirepo.subprocess.Popen", return_value=mock_proc):
            rc = run_merge(services, "/graphify", merged_out)
        assert rc == 2


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


# ── run_map ──────────────────────────────────────────────────────────────────

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

    def test_dry_run_does_not_call_extract(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        extract_called = []
        monkeypatch.setattr("codex_graph.multirepo.run_extract", lambda *a, **kw: extract_called.append(True) or 0)
        run_map(str(two_svc_root), MonoConfig(), dry_run=True)
        assert extract_called == []

    def test_backend_override_passed_to_extract(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        backends_used = []

        def fake_extract(svc, path, backend, timeout=600, env=None):
            backends_used.append(backend)
            Path(svc.graph_path).parent.mkdir(parents=True, exist_ok=True)
            Path(svc.graph_path).write_text("{}")
            return 0

        monkeypatch.setattr("codex_graph.multirepo.run_extract", fake_extract)
        monkeypatch.setattr("codex_graph.multirepo._reanalyze_and_write", lambda *a, **kw: None)
        run_map(str(two_svc_root), MonoConfig(), backend_override="openai")
        assert all(b == "openai" for b in backends_used)

    def test_config_backend_used_when_no_override(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        backends_used = []

        def fake_extract(svc, path, backend, timeout=600, env=None):
            backends_used.append(backend)
            Path(svc.graph_path).parent.mkdir(parents=True, exist_ok=True)
            Path(svc.graph_path).write_text("{}")
            return 0

        monkeypatch.setattr("codex_graph.multirepo.run_extract", fake_extract)
        monkeypatch.setattr("codex_graph.multirepo._reanalyze_and_write", lambda *a, **kw: None)
        cfg = MonoConfig(graphify_backend="gemini")
        run_map(str(two_svc_root), cfg)
        assert all(b == "gemini" for b in backends_used)

    def test_failed_extract_skipped_continues(self, two_svc_root, monkeypatch, capsys):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        call_count = [0]

        def fake_extract(svc, path, backend, timeout=600, env=None):
            call_count[0] += 1
            if svc.name == "svc-a":
                return 1
            Path(svc.graph_path).parent.mkdir(parents=True, exist_ok=True)
            Path(svc.graph_path).write_text("{}")
            return 0

        monkeypatch.setattr("codex_graph.multirepo.run_extract", fake_extract)
        run_map(str(two_svc_root), MonoConfig())
        assert call_count[0] == 2
        assert "WARNING" in capsys.readouterr().err

    def test_only_one_success_returns_1(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")

        def fake_extract(svc, path, backend, timeout=600, env=None):
            if svc.name == "svc-a":
                Path(svc.graph_path).parent.mkdir(parents=True, exist_ok=True)
                Path(svc.graph_path).write_text("{}")
                return 0
            return 1

        monkeypatch.setattr("codex_graph.multirepo.run_extract", fake_extract)
        assert run_map(str(two_svc_root), MonoConfig()) == 1

    def test_full_success_returns_0(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")

        def fake_extract(svc, path, backend, timeout=600, env=None):
            Path(svc.graph_path).parent.mkdir(parents=True, exist_ok=True)
            Path(svc.graph_path).write_text("{}")
            return 0

        monkeypatch.setattr("codex_graph.multirepo.run_extract", fake_extract)
        monkeypatch.setattr("codex_graph.multirepo._reanalyze_and_write", lambda *a, **kw: None)
        assert run_map(str(two_svc_root), MonoConfig()) == 0

    def test_full_success_summary_in_stdout(self, two_svc_root, monkeypatch, capsys):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")

        def fake_extract(svc, path, backend, timeout=600, env=None):
            Path(svc.graph_path).parent.mkdir(parents=True, exist_ok=True)
            Path(svc.graph_path).write_text("{}")
            return 0

        monkeypatch.setattr("codex_graph.multirepo.run_extract", fake_extract)
        monkeypatch.setattr("codex_graph.multirepo._reanalyze_and_write", lambda *a, **kw: None)
        run_map(str(two_svc_root), MonoConfig())
        out = capsys.readouterr().out
        assert "Done" in out
        assert "2/2" in out

    def test_root_path_resolved_to_absolute(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo.run_extract", lambda *a, **kw: 0)
        roots_seen = []

        def fake_detect(root, markers):
            roots_seen.append(root)
            return []

        monkeypatch.setattr("codex_graph.multirepo.detect_services", fake_detect)
        run_map(".", MonoConfig())
        assert os.path.isabs(roots_seen[0])


# ── run_watch ────────────────────────────────────────────────────────────────

class TestRunWatch:
    def test_graphify_not_found_returns_1(self, tmp_path, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: None)
        assert run_watch(str(tmp_path), MonoConfig()) == 1

    def test_no_services_returns_1(self, tmp_path, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        assert run_watch(str(tmp_path), MonoConfig()) == 1

    def test_keyboard_interrupt_returns_0(self, two_svc_root_with_graphs, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo._reanalyze_and_write", lambda *a, **kw: None)

        mock_proc = make_mock_proc(returncode=None)
        mock_proc.poll.return_value = None
        monkeypatch.setattr("codex_graph.multirepo.subprocess.Popen", lambda *a, **kw: mock_proc)

        def first_sleep_raises(_):
            raise KeyboardInterrupt

        monkeypatch.setattr("codex_graph.multirepo.time.sleep", first_sleep_raises)
        assert run_watch(str(two_svc_root_with_graphs), MonoConfig()) == 0

    def test_keyboard_interrupt_terminates_watch_procs(self, two_svc_root_with_graphs, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo._reanalyze_and_write", lambda *a, **kw: None)

        mock_proc = make_mock_proc(returncode=None)
        mock_proc.poll.return_value = None
        popen_calls = []

        def capture_popen(*args, **kwargs):
            popen_calls.append(True)
            return mock_proc

        monkeypatch.setattr("codex_graph.multirepo.subprocess.Popen", capture_popen)
        monkeypatch.setattr("codex_graph.multirepo.time.sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))

        run_watch(str(two_svc_root_with_graphs), MonoConfig())
        assert mock_proc.terminate.called

    def test_bootstrap_skips_existing_graphs(self, two_svc_root_with_graphs, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo._reanalyze_and_write", lambda *a, **kw: None)

        extract_calls = []
        monkeypatch.setattr("codex_graph.multirepo.run_extract",
                            lambda *a, **kw: extract_calls.append(True) or 0)
        monkeypatch.setattr("codex_graph.multirepo.subprocess.Popen", lambda *a, **kw: make_mock_proc(None))
        monkeypatch.setattr("codex_graph.multirepo.time.sleep",
                            lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))

        run_watch(str(two_svc_root_with_graphs), MonoConfig())
        assert extract_calls == []

    def test_bootstrap_runs_extract_for_missing_graph(self, two_svc_root, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo._reanalyze_and_write", lambda *a, **kw: None)

        def fake_extract(svc, path, backend, timeout=600, env=None):
            Path(svc.graph_path).parent.mkdir(parents=True, exist_ok=True)
            Path(svc.graph_path).write_text("{}")
            return 0

        monkeypatch.setattr("codex_graph.multirepo.run_extract", fake_extract)
        monkeypatch.setattr("codex_graph.multirepo.subprocess.Popen", lambda *a, **kw: make_mock_proc(None))
        monkeypatch.setattr("codex_graph.multirepo.time.sleep",
                            lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))

        rc = run_watch(str(two_svc_root), MonoConfig())
        assert rc == 0

    def test_mtime_change_triggers_reanalyze(self, two_svc_root_with_graphs, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")

        reanalyze_calls = []
        monkeypatch.setattr("codex_graph.multirepo._reanalyze_and_write",
                            lambda *a, **kw: reanalyze_calls.append(1))
        monkeypatch.setattr("codex_graph.multirepo.subprocess.Popen", lambda *a, **kw: make_mock_proc(None))

        graph_a = two_svc_root_with_graphs / "svc-a" / "graphify-out" / "graph.json"

        sleep_count = [0]
        def fake_sleep(_):
            sleep_count[0] += 1
            if sleep_count[0] == 1:
                graph_a.write_text('{"updated": true}')
                os.utime(str(graph_a), (9_999_999_999.0, 9_999_999_999.0))
            elif sleep_count[0] >= 2:
                raise KeyboardInterrupt

        monkeypatch.setattr("codex_graph.multirepo.time.sleep", fake_sleep)

        run_watch(str(two_svc_root_with_graphs), MonoConfig(watch_poll_interval=0.001))
        assert len(reanalyze_calls) >= 2

    def test_dead_watch_proc_restarted(self, two_svc_root_with_graphs, monkeypatch):
        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")
        monkeypatch.setattr("codex_graph.multirepo._reanalyze_and_write", lambda *a, **kw: None)

        popen_calls = [0]
        procs = []

        def make_popen(*args, **kwargs):
            proc = make_mock_proc(returncode=None)
            if popen_calls[0] < 2:
                proc.poll.return_value = 1
            else:
                proc.poll.return_value = None
            popen_calls[0] += 1
            procs.append(proc)
            return proc

        monkeypatch.setattr("codex_graph.multirepo.subprocess.Popen", make_popen)

        sleep_count = [0]
        def fake_sleep(_):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                raise KeyboardInterrupt

        monkeypatch.setattr("codex_graph.multirepo.time.sleep", fake_sleep)

        run_watch(str(two_svc_root_with_graphs), MonoConfig(watch_poll_interval=0.001))
        assert popen_calls[0] > 2

    def test_single_service_skips_initial_bridge_analysis(self, tmp_path, monkeypatch):
        d = tmp_path / "only-svc"
        d.mkdir()
        (d / "pyproject.toml").touch()
        graph_path = d / "graphify-out" / "graph.json"
        graph_path.parent.mkdir(parents=True)
        graph_path.write_text("{}")

        monkeypatch.setattr("codex_graph.multirepo.shutil.which", lambda _: "/graphify")

        reanalyze_calls = []
        monkeypatch.setattr("codex_graph.multirepo._reanalyze_and_write",
                            lambda *a, **kw: reanalyze_calls.append(1))
        monkeypatch.setattr("codex_graph.multirepo.subprocess.Popen", lambda *a, **kw: make_mock_proc(None))
        monkeypatch.setattr("codex_graph.multirepo.time.sleep",
                            lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))

        run_watch(str(tmp_path), MonoConfig())
        assert reanalyze_calls == []
