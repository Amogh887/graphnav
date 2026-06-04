from __future__ import annotations

import os

import pytest

from codex_graph.config import (
    CodexConfig,
    Config,
    ContextConfig,
    GraphConfig,
    MonoConfig,
    QueryConfig,
    _apply_toml,
    load_config,
)


class TestMonoConfigDefaults:
    def test_marker_files_includes_common_markers(self):
        cfg = MonoConfig()
        for marker in ("package.json", "pyproject.toml", "go.mod", "Cargo.toml"):
            assert marker in cfg.marker_files

    def test_default_backend(self):
        assert MonoConfig().graphify_backend == "claude"

    def test_default_poll_interval(self):
        assert MonoConfig().watch_poll_interval == 3.0

    def test_marker_files_is_list(self):
        assert isinstance(MonoConfig().marker_files, list)

    def test_two_instances_dont_share_marker_list(self):
        a = MonoConfig()
        b = MonoConfig()
        a.marker_files.append("custom.txt")
        assert "custom.txt" not in b.marker_files


class TestConfigDefaults:
    def test_config_has_mono(self):
        assert isinstance(Config().mono, MonoConfig)

    def test_two_configs_dont_share_mono(self):
        a = Config()
        b = Config()
        a.mono.marker_files.append("x")
        assert "x" not in b.mono.marker_files

    def test_other_sections_unchanged(self):
        cfg = Config()
        assert isinstance(cfg.graph, GraphConfig)
        assert isinstance(cfg.query, QueryConfig)
        assert isinstance(cfg.context, ContextConfig)
        assert isinstance(cfg.codex, CodexConfig)


class TestApplyToml:
    def test_mono_full_override(self):
        cfg = Config()
        data = {
            "mono": {
                "marker_files": ["go.mod"],
                "graphify_backend": "openai",
                "watch_poll_interval": 10.0,
            }
        }
        result = _apply_toml(cfg, data)
        assert result.mono.marker_files == ["go.mod"]
        assert result.mono.graphify_backend == "openai"
        assert result.mono.watch_poll_interval == 10.0

    def test_mono_partial_override_backend_only(self):
        cfg = Config()
        original_markers = list(cfg.mono.marker_files)
        data = {"mono": {"graphify_backend": "gemini"}}
        result = _apply_toml(cfg, data)
        assert result.mono.graphify_backend == "gemini"
        assert result.mono.marker_files == original_markers

    def test_mono_partial_override_poll_interval_only(self):
        cfg = Config()
        data = {"mono": {"watch_poll_interval": 5.5}}
        result = _apply_toml(cfg, data)
        assert result.mono.watch_poll_interval == 5.5
        assert result.mono.graphify_backend == "claude"

    def test_no_mono_section_leaves_defaults(self):
        cfg = Config()
        original_backend = cfg.mono.graphify_backend
        data = {"graph": {"path": "some/path.json"}}
        result = _apply_toml(cfg, data)
        assert result.mono.graphify_backend == original_backend

    def test_empty_data_leaves_all_defaults(self):
        cfg = Config()
        result = _apply_toml(cfg, {})
        assert result.mono.graphify_backend == "claude"

    def test_graph_section_still_works(self):
        cfg = Config()
        data = {"graph": {"path": "custom/graph.json"}}
        result = _apply_toml(cfg, data)
        assert result.graph.path == "custom/graph.json"

    def test_query_section_still_works(self):
        cfg = Config()
        data = {"query": {"top_k": 10}}
        result = _apply_toml(cfg, data)
        assert result.query.top_k == 10

    def test_context_section_still_works(self):
        cfg = Config()
        data = {"context": {"show_scores": True}}
        result = _apply_toml(cfg, data)
        assert result.context.show_scores is True

    def test_codex_section_still_works(self):
        cfg = Config()
        data = {"codex": {"command": "mycli", "timeout_seconds": 60}}
        result = _apply_toml(cfg, data)
        assert result.codex.command == "mycli"
        assert result.codex.timeout_seconds == 60


class TestLoadConfig:
    def test_no_config_file_returns_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = load_config()
        assert isinstance(cfg, Config)
        assert cfg.mono.graphify_backend == "claude"

    def test_explicit_path_with_mono_section(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[mono]\ngraphify_backend = "openai"\nwatch_poll_interval = 7.0\n'
        )
        cfg = load_config(str(config_file))
        assert cfg.mono.graphify_backend == "openai"
        assert cfg.mono.watch_poll_interval == 7.0

    def test_explicit_path_with_all_sections(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[graph]\npath = \"my/graph.json\"\n"
            "[mono]\ngraphify_backend = \"gemini\"\n"
        )
        cfg = load_config(str(config_file))
        assert cfg.graph.path == "my/graph.json"
        assert cfg.mono.graphify_backend == "gemini"

    def test_missing_explicit_path_warns(self, tmp_path, capsys):
        load_config(str(tmp_path / "nonexistent.toml"))
        err = capsys.readouterr().err
        assert "Warning" in err

    def test_env_var_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "env-config.toml"
        config_file.write_text('[mono]\ngraphify_backend = "deepseek"\n')
        monkeypatch.setenv("CODEX_GRAPH_CONFIG", str(config_file))
        monkeypatch.chdir(tmp_path)
        cfg = load_config()
        assert cfg.mono.graphify_backend == "deepseek"

    def test_cwd_config_toml_loaded(self, tmp_path, monkeypatch):
        (tmp_path / "config.toml").write_text('[mono]\ngraphify_backend = "kimi"\n')
        monkeypatch.chdir(tmp_path)
        cfg = load_config()
        assert cfg.mono.graphify_backend == "kimi"

    def test_marker_files_override_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[mono]\nmarker_files = ["Makefile"]\n')
        cfg = load_config(str(config_file))
        assert cfg.mono.marker_files == ["Makefile"]
