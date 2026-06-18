from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields

DEFAULT_BACKEND = "claude"

BACKEND_KEY_VARS = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_KEY"),
    "openai": ("OPENAI_API_KEY", "OPENAI_KEY"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_KEY"),
    "ollama": (),
}

KNOWN_BACKENDS = frozenset(BACKEND_KEY_VARS)

BACKEND_PROVIDER = {
    "claude": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Google",
    "deepseek": "DeepSeek",
    "ollama": "your local Ollama",
}


def backend_provider(backend: str) -> str:
    return BACKEND_PROVIDER.get(backend, f"the '{backend}' provider")


def backend_has_key(backend: str, env: dict[str, str]) -> bool:
    key_vars = BACKEND_KEY_VARS.get(backend, ())
    if not key_vars:
        return True
    return any((env.get(var) or "").strip() for var in key_vars)


@dataclass
class GraphConfig:
    path: str = "graphify-out/graph.json"
    project_root: str = "."
    skip_patterns: list[str] = field(default_factory=lambda: ["playwright-report", "node_modules", ".git"])


@dataclass
class QueryConfig:
    top_k: int = 5
    community_boost_weight: float = 2.0
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    edge_boost_weight: float = 0.4
    edge_relation_weights: dict[str, float] = field(default_factory=dict)
    recency_boost_weight: float = 0.2


@dataclass
class ContextConfig:
    max_file_chars: int = 8000
    show_scores: bool = False


@dataclass
class CodexConfig:
    command: str = "codex"
    subcommand: str = "exec"
    extra_args: list[str] = field(default_factory=list)
    inject_via: str = "stdin"
    timeout_seconds: int = 300


@dataclass
class MonoConfig:
    marker_files: list[str] = field(default_factory=lambda: [
        "package.json", "pyproject.toml", "go.mod", "Cargo.toml",
        "pom.xml", "build.gradle", "setup.py", "setup.cfg",
        "requirements.txt", "Gemfile", "composer.json", "tsconfig.json",
    ])
    graphify_backend: str = "claude"
    semantic: bool = False
    watch_poll_interval: float = 3.0
    context_budget_tokens: int = 2000
    context_top_files: int = 8
    extra_skip_dirs: list[str] = field(default_factory=list)
    auto_rebuild: bool = True


@dataclass
class Config:
    graph: GraphConfig = field(default_factory=GraphConfig)
    query: QueryConfig = field(default_factory=QueryConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    mono: MonoConfig = field(default_factory=MonoConfig)


_SECTION_TYPES = {
    "graph": GraphConfig,
    "query": QueryConfig,
    "context": ContextConfig,
    "codex": CodexConfig,
    "mono": MonoConfig,
}


def _collect_unknown_keys(data: dict, warnings: list[str]) -> None:
    for section, values in data.items():
        section_type = _SECTION_TYPES.get(section)
        if section_type is None:
            warnings.append(f"unknown section [{section}]")
            continue
        known_keys = {f.name for f in fields(section_type)}
        for key in values:
            if key not in known_keys:
                warnings.append(f"unknown key {key} in [{section}]")


def _apply_toml(cfg: Config, data: dict, warnings: list[str] | None = None) -> Config:
    if warnings is None:
        warnings = []
    _collect_unknown_keys(data, warnings)
    if "graph" in data:
        g = data["graph"]
        cfg.graph = GraphConfig(
            path=g.get("path", cfg.graph.path),
            project_root=g.get("project_root", cfg.graph.project_root),
            skip_patterns=g.get("skip_patterns", cfg.graph.skip_patterns),
        )
    if "query" in data:
        q = data["query"]
        cfg.query = QueryConfig(
            top_k=q.get("top_k", cfg.query.top_k),
            community_boost_weight=q.get("community_boost_weight", cfg.query.community_boost_weight),
            bm25_k1=q.get("bm25_k1", cfg.query.bm25_k1),
            bm25_b=q.get("bm25_b", cfg.query.bm25_b),
            edge_boost_weight=q.get("edge_boost_weight", cfg.query.edge_boost_weight),
            edge_relation_weights=q.get("edge_relation_weights", cfg.query.edge_relation_weights),
            recency_boost_weight=q.get("recency_boost_weight", cfg.query.recency_boost_weight),
        )
    if "context" in data:
        c = data["context"]
        cfg.context = ContextConfig(
            max_file_chars=c.get("max_file_chars", cfg.context.max_file_chars),
            show_scores=c.get("show_scores", cfg.context.show_scores),
        )
    if "codex" in data:
        cx = data["codex"]
        cfg.codex = CodexConfig(
            command=cx.get("command", cfg.codex.command),
            subcommand=cx.get("subcommand", cfg.codex.subcommand),
            extra_args=cx.get("extra_args", cfg.codex.extra_args),
            inject_via=cx.get("inject_via", cfg.codex.inject_via),
            timeout_seconds=cx.get("timeout_seconds", cfg.codex.timeout_seconds),
        )
    if "mono" in data:
        m = data["mono"]
        cfg.mono = MonoConfig(
            marker_files=m.get("marker_files", cfg.mono.marker_files),
            graphify_backend=m.get("graphify_backend", cfg.mono.graphify_backend),
            semantic=m.get("semantic", cfg.mono.semantic),
            watch_poll_interval=m.get("watch_poll_interval", cfg.mono.watch_poll_interval),
            context_budget_tokens=m.get("context_budget_tokens", cfg.mono.context_budget_tokens),
            context_top_files=m.get("context_top_files", cfg.mono.context_top_files),
            extra_skip_dirs=m.get("extra_skip_dirs", cfg.mono.extra_skip_dirs),
            auto_rebuild=m.get("auto_rebuild", cfg.mono.auto_rebuild),
        )
    return cfg


_NUMERIC_FIELDS = (
    ("query", "top_k", int), ("query", "community_boost_weight", float),
    ("query", "bm25_k1", float), ("query", "bm25_b", float),
    ("query", "edge_boost_weight", float), ("query", "recency_boost_weight", float),
    ("context", "max_file_chars", int),
    ("codex", "timeout_seconds", int),
    ("mono", "watch_poll_interval", float), ("mono", "context_budget_tokens", int),
    ("mono", "context_top_files", int),
)


def _coerce_types(cfg: Config, warnings: list[str]) -> None:
    defaults = Config()
    for section, key, typ in _NUMERIC_FIELDS:
        value = getattr(getattr(cfg, section), key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            fallback = getattr(getattr(defaults, section), key)
            warnings.append(f"{section}.{key} has invalid value {value!r} — using default {fallback}")
            setattr(getattr(cfg, section), key, fallback)
        elif typ is int and not isinstance(value, int):
            setattr(getattr(cfg, section), key, int(value))
    if not isinstance(cfg.query.edge_relation_weights, dict):
        warnings.append("query.edge_relation_weights must be a table — ignoring")
        cfg.query.edge_relation_weights = {}
    else:
        for rel in list(cfg.query.edge_relation_weights):
            w = cfg.query.edge_relation_weights[rel]
            if isinstance(w, bool) or not isinstance(w, (int, float)):
                warnings.append(f"query.edge_relation_weights.{rel} has invalid value {w!r} — ignoring")
                del cfg.query.edge_relation_weights[rel]
    for section, key in (("mono", "auto_rebuild"), ("context", "show_scores")):
        value = getattr(getattr(cfg, section), key)
        if not isinstance(value, bool):
            fallback = getattr(getattr(Config(), section), key)
            warnings.append(f"{section}.{key} must be true or false — using default {fallback}")
            setattr(getattr(cfg, section), key, fallback)
    for section, key in (("mono", "marker_files"), ("mono", "extra_skip_dirs"), ("graph", "skip_patterns"), ("codex", "extra_args")):
        value = getattr(getattr(cfg, section), key)
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            fallback = getattr(getattr(Config(), section), key)
            warnings.append(f"{section}.{key} must be a list of strings — using default")
            setattr(getattr(cfg, section), key, fallback)
    backend = cfg.mono.graphify_backend
    if not isinstance(backend, str) or backend not in KNOWN_BACKENDS:
        warnings.append(
            f"mono.graphify_backend has unknown value {backend!r} — "
            f"using default {DEFAULT_BACKEND!r} (known: {', '.join(sorted(KNOWN_BACKENDS))})"
        )
        cfg.mono.graphify_backend = DEFAULT_BACKEND


def _validate(cfg: Config) -> list[str]:
    warnings: list[str] = []
    _coerce_types(cfg, warnings)
    if cfg.query.top_k < 1:
        warnings.append(f"query.top_k {cfg.query.top_k} clamped to 1")
        cfg.query.top_k = 1
    if cfg.query.bm25_k1 <= 0:
        warnings.append(f"query.bm25_k1 {cfg.query.bm25_k1} clamped to 1.5")
        cfg.query.bm25_k1 = 1.5
    if cfg.query.bm25_b < 0.0:
        warnings.append(f"query.bm25_b {cfg.query.bm25_b} clamped to 0.0")
        cfg.query.bm25_b = 0.0
    elif cfg.query.bm25_b > 1.0:
        warnings.append(f"query.bm25_b {cfg.query.bm25_b} clamped to 1.0")
        cfg.query.bm25_b = 1.0
    if cfg.query.community_boost_weight < 0:
        warnings.append(f"query.community_boost_weight {cfg.query.community_boost_weight} clamped to 0.0")
        cfg.query.community_boost_weight = 0.0
    if cfg.query.edge_boost_weight < 0:
        warnings.append(f"query.edge_boost_weight {cfg.query.edge_boost_weight} clamped to 0.0")
        cfg.query.edge_boost_weight = 0.0
    if cfg.query.recency_boost_weight < 0:
        warnings.append(f"query.recency_boost_weight {cfg.query.recency_boost_weight} clamped to 0.0")
        cfg.query.recency_boost_weight = 0.0
    for relation, weight in cfg.query.edge_relation_weights.items():
        if weight < 0:
            warnings.append(f"query.edge_relation_weights.{relation} {weight} clamped to 0.0")
            cfg.query.edge_relation_weights[relation] = 0.0
    if cfg.mono.watch_poll_interval < 0.5:
        warnings.append(f"mono.watch_poll_interval {cfg.mono.watch_poll_interval} clamped to 0.5")
        cfg.mono.watch_poll_interval = 0.5
    if cfg.mono.context_top_files < 1:
        warnings.append(f"mono.context_top_files {cfg.mono.context_top_files} clamped to 1")
        cfg.mono.context_top_files = 1
    if cfg.mono.context_budget_tokens < 0:
        warnings.append(f"mono.context_budget_tokens {cfg.mono.context_budget_tokens} clamped to 0")
        cfg.mono.context_budget_tokens = 0
    if cfg.codex.timeout_seconds < 1:
        warnings.append(f"codex.timeout_seconds {cfg.codex.timeout_seconds} clamped to 1")
        cfg.codex.timeout_seconds = 1
    if cfg.context.max_file_chars < 0:
        warnings.append(f"context.max_file_chars {cfg.context.max_file_chars} clamped to 0")
        cfg.context.max_file_chars = 0
    return warnings


def load_config_report(explicit_path: str | None = None) -> tuple[Config, str | None, list[str]]:
    cfg = Config()
    warnings: list[str] = []

    candidates: list[str] = []
    if explicit_path:
        candidates = [explicit_path]
    else:
        env_path = os.environ.get("GRAPHNAV_CONFIG") or os.environ.get("CODEX_GRAPH_CONFIG")
        if env_path:
            candidates.append(env_path)
        candidates.append(os.path.join(os.getcwd(), "config.toml"))
        candidates.append(os.path.expanduser("~/.graphnav/config.toml"))
        candidates.append(os.path.expanduser("~/.codex-graph/config.toml"))

    source_path: str | None = None
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except (tomllib.TOMLDecodeError, OSError) as exc:
            warnings.append(f"could not parse {path} ({exc}) — using defaults")
            source_path = path
            break
        if not explicit_path and not any(section in data for section in _SECTION_TYPES):
            continue
        cfg = _apply_toml(cfg, data, warnings)
        source_path = path
        break
    else:
        if explicit_path:
            print(f"Warning: config file not found: {explicit_path}", file=sys.stderr)

    warnings.extend(_validate(cfg))
    return cfg, source_path, warnings


def load_config(explicit_path: str | None = None) -> Config:
    cfg, _, warnings = load_config_report(explicit_path)
    for w in warnings:
        print(f"[graphnav] config warning: {w}", file=sys.stderr)
    return cfg
