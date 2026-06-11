from __future__ import annotations

import json
import sys

import pytest

from codex_graph.config import load_config_report
from codex_graph.graph_nav import GraphNav
from codex_graph.multirepo import (
    _load_env_file,
    _parse_env_file,
    build_context_pack_inline,
    resolve_services,
)
from tests.conftest import write_graph


class TestFlatRepoDetection:
    def test_src_and_tests_dirs_without_markers_is_single_project(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("def main():\n    pass\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_main.py").write_text("def test_x():\n    pass\n")
        services, single = resolve_services(str(tmp_path), ["pyproject.toml", "package.json"])
        assert single is True
        assert len(services) == 1
        assert services[0].abs_path == str(tmp_path)

    def test_root_marker_with_tests_dir_is_single_project(self, tmp_path):
        (tmp_path / "pyproject.toml").touch()
        (tmp_path / "app.py").write_text("x = 1\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_app.py").write_text("y = 2\n")
        services, single = resolve_services(str(tmp_path), ["pyproject.toml"])
        assert single is True

    def test_subdir_marker_still_monorepo(self, tmp_path):
        for name in ("backend", "frontend"):
            d = tmp_path / name
            d.mkdir()
            (d / "package.json").touch()
        services, single = resolve_services(str(tmp_path), ["package.json"])
        assert single is False
        assert [s.name for s in services] == ["backend", "frontend"]

    def test_one_marker_subdir_pulls_in_source_only_dirs(self, tmp_path):
        backend = tmp_path / "backend"
        backend.mkdir()
        (backend / "pyproject.toml").touch()
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "util.py").write_text("z = 3\n")
        services, single = resolve_services(str(tmp_path), ["pyproject.toml"])
        assert single is False
        assert {s.name for s in services} == {"backend", "shared"}


class TestConfigRobustness:
    def test_malformed_toml_falls_back_to_defaults(self, tmp_path, monkeypatch):
        (tmp_path / "config.toml").write_text("[query\ntop_k = 3\n")
        monkeypatch.chdir(tmp_path)
        cfg, source, warnings = load_config_report()
        assert cfg.query.top_k == 5
        assert any("could not parse" in w for w in warnings)

    def test_foreign_config_toml_ignored(self, tmp_path, monkeypatch):
        (tmp_path / "config.toml").write_text('[params]\ntitle = "My Hugo Site"\nbaseURL = "x"\n')
        monkeypatch.chdir(tmp_path)
        cfg, source, warnings = load_config_report()
        assert source is None
        assert warnings == []

    def test_wrong_typed_value_uses_default(self, tmp_path, monkeypatch):
        (tmp_path / "config.toml").write_text('[query]\ntop_k = "five"\n')
        monkeypatch.chdir(tmp_path)
        cfg, source, warnings = load_config_report()
        assert cfg.query.top_k == 5
        assert any("invalid value" in w for w in warnings)

    def test_wrong_typed_list_uses_default(self, tmp_path, monkeypatch):
        (tmp_path / "config.toml").write_text('[mono]\nmarker_files = "package.json"\n')
        monkeypatch.chdir(tmp_path)
        cfg, source, warnings = load_config_report()
        assert "pyproject.toml" in cfg.mono.marker_files
        assert any("list of strings" in w for w in warnings)

    def test_explicit_path_with_no_known_sections_still_loads(self, tmp_path):
        p = tmp_path / "custom.toml"
        p.write_text("")
        cfg, source, warnings = load_config_report(str(p))
        assert source == str(p)


class TestNeighborsExactMatch:
    def test_exact_label_beats_superstring(self):
        graph = {
            "nodes": [
                {"id": "test_main", "label": "test_main", "source_file": "tests/test_app.py",
                 "file_type": "code", "source_location": "L1"},
                {"id": "main", "label": "main", "source_file": "app.py",
                 "file_type": "code", "source_location": "L5"},
            ],
            "links": [],
        }
        nav = GraphNav("unused", graph=graph)
        r = nav.neighbors("main")
        assert r["symbol"] == "main"
        assert r["defined_at"] == "app.py:L5"


class TestGraphNavEdgesKey:
    def test_edges_key_fallback(self, tmp_path):
        graph = {
            "nodes": [
                {"id": "a", "label": "alpha_func", "source_file": "a.py",
                 "file_type": "code", "source_location": "L1"},
                {"id": "b", "label": "beta_func", "source_file": "b.py",
                 "file_type": "code", "source_location": "L2"},
            ],
            "edges": [{"source": "a", "target": "b", "relation": "calls"}],
        }
        nav = GraphNav("unused", graph=graph)
        r = nav.neighbors("alpha_func")
        assert r["callees"]
        assert "beta_func" in r["callees"][0]


class TestEnvFileParsing:
    def test_bom_stripped(self, tmp_path):
        p = tmp_path / ".env"
        p.write_bytes("﻿ANTHROPIC_API_KEY=sk-bom\n".encode("utf-8"))
        env = _parse_env_file(str(p))
        assert env.get("ANTHROPIC_API_KEY") == "sk-bom"

    def test_openai_key_alias(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text("OPENAI_KEY=sk-oai\n")
        monkeypatch.chdir(tmp_path)
        env = _load_env_file(str(tmp_path))
        assert env.get("OPENAI_API_KEY") == "sk-oai"


class TestDoctorFlatRepo:
    def test_flat_repo_services_check_passes(self, tmp_path, monkeypatch, capsys):
        from codex_graph import doctor

        (tmp_path / "app.py").write_text("def main():\n    pass\n")
        write_graph(
            tmp_path / "graphify-out" / "graph.json",
            [{"id": "m", "label": "main", "source_file": "app.py",
              "file_type": "code", "source_location": "L1", "community": 0}],
            [],
        )
        (tmp_path / "graphify-out" / ".graphnav-meta.json").write_text(
            json.dumps({"built_at": "x", "git_sha": None})
        )
        monkeypatch.setattr(doctor, "find_graphify", lambda: "/usr/bin/graphify")
        monkeypatch.setattr(
            doctor.subprocess, "run",
            lambda *a, **k: __import__("subprocess").CompletedProcess(a, 0, stdout="graphify 0.9", stderr=""),
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        rc = doctor.run_doctor(str(tmp_path))
        out = capsys.readouterr().out
        assert rc == 0
        assert "single project" in out


class TestCorruptGraphCli:
    def test_find_with_corrupt_graph_exits_2(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "graphify-out").mkdir()
        (tmp_path / "graphify-out" / "graph.json").write_text("{not json")
        monkeypatch.setattr(
            sys, "argv", ["graphnav", "find", "anything", "--root", str(tmp_path)]
        )
        from codex_graph.cli import main

        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "graphnav map" in err


class TestInlinePackTruncation:
    def test_truncation_balances_code_fences(self, tmp_path):
        nodes = []
        for i in range(40):
            name = f"mod_{i}"
            src = tmp_path / f"{name}.py"
            src.write_text("\n".join(f"def fn_{j}():\n    pass" for j in range(30)))
            nodes.append({
                "id": name, "label": f"search target term {i}", "source_file": f"{name}.py",
                "file_type": "code", "source_location": "L1", "community": 0,
            })
        write_graph(tmp_path / "graphify-out" / "graph.json", nodes, [])
        pack = build_context_pack_inline(
            str(tmp_path), "search target term", top_files=8, budget_tokens=100
        )
        assert "_(truncated to budget)_" in pack
        assert pack.count("```") % 2 == 0
