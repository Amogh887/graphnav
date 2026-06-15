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
    load_config_report,
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
        (tmp_path / "config.toml").write_text('[mono]\ngraphify_backend = "openai"\n')
        monkeypatch.chdir(tmp_path)
        cfg = load_config()
        assert cfg.mono.graphify_backend == "openai"

    def test_marker_files_override_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[mono]\nmarker_files = ["Makefile"]\n')
        cfg = load_config(str(config_file))
        assert cfg.mono.marker_files == ["Makefile"]


class TestConfigValidation:
    def test_top_k_below_one_clamped(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[query]\ntop_k = 0\n")
        cfg = load_config(str(config_file))
        assert cfg.query.top_k == 1

    def test_nonpositive_bm25_k1_reset(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[query]\nbm25_k1 = 0.0\n")
        cfg = load_config(str(config_file))
        assert cfg.query.bm25_k1 == 1.5

    def test_unknown_backend_falls_back_to_default(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[mono]\ngraphify_backend = "kimi"\n')
        cfg, _, warnings = load_config_report(str(config_file))
        assert cfg.mono.graphify_backend == "claude"
        assert any("graphify_backend" in w and "kimi" in w for w in warnings)

    def test_known_backend_preserved(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[mono]\ngraphify_backend = "ollama"\n')
        cfg = load_config(str(config_file))
        assert cfg.mono.graphify_backend == "ollama"

    def test_bm25_b_clamped_into_unit_interval(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[query]\nbm25_b = 1.7\n")
        cfg = load_config(str(config_file))
        assert cfg.query.bm25_b == 1.0

    def test_bm25_b_negative_clamped_to_zero(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[query]\nbm25_b = -0.2\n")
        cfg = load_config(str(config_file))
        assert cfg.query.bm25_b == 0.0

    def test_negative_boost_weights_clamped(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[query]\n"
            "community_boost_weight = -1.0\n"
            "edge_boost_weight = -0.4\n"
            "recency_boost_weight = -0.2\n"
        )
        cfg = load_config(str(config_file))
        assert cfg.query.community_boost_weight == 0.0
        assert cfg.query.edge_boost_weight == 0.0
        assert cfg.query.recency_boost_weight == 0.0

    def test_negative_edge_relation_weight_clamped(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[query.edge_relation_weights]\nimports = -1.0\ncalls = 0.5\n"
        )
        cfg = load_config(str(config_file))
        assert cfg.query.edge_relation_weights == {"imports": 0.0, "calls": 0.5}

    def test_mono_values_clamped(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[mono]\n"
            "watch_poll_interval = 0.1\n"
            "context_top_files = 0\n"
            "context_budget_tokens = -100\n"
        )
        cfg = load_config(str(config_file))
        assert cfg.mono.watch_poll_interval == 0.5
        assert cfg.mono.context_top_files == 1
        assert cfg.mono.context_budget_tokens == 0

    def test_codex_timeout_clamped(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[codex]\ntimeout_seconds = 0\n")
        cfg = load_config(str(config_file))
        assert cfg.codex.timeout_seconds == 1

    def test_clamp_warnings_printed_to_stderr(self, tmp_path, capsys):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[query]\ntop_k = -3\nbm25_b = 2.0\n")
        load_config(str(config_file))
        err = capsys.readouterr().err
        assert "[graphnav] config warning:" in err
        assert "top_k" in err
        assert "bm25_b" in err

    def test_valid_config_prints_no_warnings(self, tmp_path, capsys):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[query]\ntop_k = 7\n")
        load_config(str(config_file))
        err = capsys.readouterr().err
        assert "[graphnav] config warning:" not in err


class TestUnknownKeyWarnings:
    def test_typoed_key_produces_warning(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[query]\nedge_bost_weight = 0.5\n")
        cfg, _, warnings = load_config_report(str(config_file))
        assert "unknown key edge_bost_weight in [query]" in warnings
        assert cfg.query.edge_boost_weight == 0.4

    def test_typoed_section_produces_warning(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[querry]\ntop_k = 9\n")
        cfg, _, warnings = load_config_report(str(config_file))
        assert "unknown section [querry]" in warnings
        assert cfg.query.top_k == 5

    def test_unknown_keys_never_fatal(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[querry]\ntop_k = 9\n"
            "[query]\nedge_bost_weight = 0.5\ntop_k = 3\n"
        )
        cfg, _, warnings = load_config_report(str(config_file))
        assert len(warnings) == 2
        assert cfg.query.top_k == 3


class TestEdgeRelationWeightsConfig:
    def test_default_is_empty_dict(self):
        assert QueryConfig().edge_relation_weights == {}

    def test_two_instances_dont_share_dict(self):
        a = QueryConfig()
        b = QueryConfig()
        a.edge_relation_weights["imports"] = 2.0
        assert "imports" not in b.edge_relation_weights

    def test_toml_table_parses_into_dict(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[query.edge_relation_weights]\nimports = 1.5\ncalls = 0.5\n"
        )
        cfg = load_config(str(config_file))
        assert cfg.query.edge_relation_weights == {"imports": 1.5, "calls": 0.5}

    def test_absent_table_leaves_empty_dict(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[query]\ntop_k = 3\n")
        cfg = load_config(str(config_file))
        assert cfg.query.edge_relation_weights == {}


class TestRecencyConfigKnob:
    def test_default(self):
        assert QueryConfig().recency_boost_weight == 0.2

    def test_toml_override(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[query]\nrecency_boost_weight = 0.5\n")
        cfg = load_config(str(config_file))
        assert cfg.query.recency_boost_weight == 0.5


class TestExtraSkipDirsConfig:
    def test_default_is_empty_list(self):
        assert MonoConfig().extra_skip_dirs == []

    def test_two_instances_dont_share_list(self):
        a = MonoConfig()
        b = MonoConfig()
        a.extra_skip_dirs.append("vendor")
        assert "vendor" not in b.extra_skip_dirs

    def test_toml_override(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[mono]\nextra_skip_dirs = ["vendor", "dist"]\n')
        cfg = load_config(str(config_file))
        assert cfg.mono.extra_skip_dirs == ["vendor", "dist"]


class TestLoadConfigReport:
    def test_returns_cfg_source_and_warnings(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[query]\ntop_k = 0\n")
        cfg, source, warnings = load_config_report(str(config_file))
        assert isinstance(cfg, Config)
        assert source == str(config_file)
        assert warnings == ["query.top_k 0 clamped to 1"]
        assert cfg.query.top_k == 1

    def test_no_config_file_returns_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg, source, warnings = load_config_report()
        assert isinstance(cfg, Config)
        assert cfg.query.top_k == 5
        assert source is None
        assert warnings == []

    def test_clean_config_returns_no_warnings(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[query]\ntop_k = 9\n")
        cfg, source, warnings = load_config_report(str(config_file))
        assert cfg.query.top_k == 9
        assert source == str(config_file)
        assert warnings == []
