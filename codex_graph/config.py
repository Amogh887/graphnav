from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field


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
    ])
    graphify_backend: str = "claude"
    watch_poll_interval: float = 3.0


@dataclass
class Config:
    graph: GraphConfig = field(default_factory=GraphConfig)
    query: QueryConfig = field(default_factory=QueryConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    mono: MonoConfig = field(default_factory=MonoConfig)


def _apply_toml(cfg: Config, data: dict) -> Config:
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
            watch_poll_interval=m.get("watch_poll_interval", cfg.mono.watch_poll_interval),
        )
    return cfg


def load_config(explicit_path: str | None = None) -> Config:
    cfg = Config()

    candidates: list[str] = []
    if explicit_path:
        candidates = [explicit_path]
    else:
        env_path = os.environ.get("CODEX_GRAPH_CONFIG")
        if env_path:
            candidates.append(env_path)
        candidates.append(os.path.join(os.getcwd(), "config.toml"))
        candidates.append(os.path.expanduser("~/.codex-graph/config.toml"))

    for path in candidates:
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = tomllib.load(f)
            cfg = _apply_toml(cfg, data)
            break
    else:
        if explicit_path:
            import sys
            print(f"Warning: config file not found: {explicit_path}", file=sys.stderr)

    return cfg
