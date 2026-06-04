from __future__ import annotations

import argparse
import os
import sys

from codex_graph import CodexNotFoundError, CodexTimeoutError, GraphNotFoundError
from codex_graph.config import load_config
from codex_graph.graph_query import load_index, query_files
from codex_graph import runner


def _run_mono_command(cmd: str, argv: list[str]) -> None:
    from codex_graph import multirepo

    parser = argparse.ArgumentParser(
        prog=f"codex-graph {cmd}",
        description={
            "map": "Build per-service graphs and cross-service bridge notes for a monorepo",
            "watch": "Watch for file changes and keep per-service graphs and bridge notes up-to-date",
        }[cmd],
    )
    parser.add_argument("--root", default=".", metavar="PATH", help="Monorepo root directory (default: current directory)")
    parser.add_argument("--backend", default=None, metavar="BACKEND", help="graphify LLM backend (claude|openai|gemini|deepseek|ollama)")
    parser.add_argument("--config", default=None, metavar="PATH", help="Path to config.toml")
    if cmd == "map":
        parser.add_argument("--dry-run", action="store_true", help="Detect services and print the plan without invoking graphify")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if cmd == "map":
        rc = multirepo.run_map(
            root=args.root,
            mono_cfg=cfg.mono,
            backend_override=args.backend,
            dry_run=args.dry_run,
        )
    else:
        rc = multirepo.run_watch(
            root=args.root,
            mono_cfg=cfg.mono,
            backend_override=args.backend,
        )
    sys.exit(rc)


def _auto_map_if_needed(cfg_path: str | None) -> None:
    from codex_graph import multirepo
    from codex_graph.config import load_config

    cfg = load_config(cfg_path)
    root = os.path.abspath(".")
    services = multirepo.detect_services(root, cfg.mono.marker_files)
    if not services:
        return

    names = ", ".join(s.name for s in services)
    print(f"[codex-graph] Detected {len(services)} service(s): {names}")
    print(f"[codex-graph] Running 'codex-graph map' to build knowledge graphs ...", file=sys.stderr)
    rc = multirepo.run_map(root=root, mono_cfg=cfg.mono)
    sys.exit(rc)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("map", "watch"):
        _run_mono_command(sys.argv[1], sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        prog="codex-graph",
        description=(
            "Codex CLI with knowledge-graph context injection for monorepos.\n\n"
            "First-run (after pip install): just run 'codex-graph' or 'codex-graph map'\n"
            "in your monorepo root — services are auto-detected and graphs are built.\n\n"
            "Subcommands:\n"
            "  map    Build per-service graphs and cross-service bridge notes\n"
            "  watch  Keep graphs and bridge notes up-to-date as files change"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("prompt", nargs="?", help="Natural language task prompt")
    parser.add_argument("--config", default=None, metavar="PATH", help="Path to config.toml")
    parser.add_argument("--top-k", type=int, default=None, metavar="N", help="Number of files to inject as context")
    parser.add_argument("--graph", default=None, metavar="PATH", help="Path to graph.json")
    parser.add_argument("--dry-run", action="store_true", help="Print enriched prompt without calling codex")
    parser.add_argument("--list-files", action="store_true", help="Print ranked files and scores, then exit")
    parser.add_argument("--no-context", action="store_true", help="Pass prompt to codex with no graph context")

    args = parser.parse_args()

    prompt = args.prompt
    if not prompt:
        if sys.stdin.isatty():
            _auto_map_if_needed(args.config)
            parser.print_help()
            sys.exit(1)
        prompt = sys.stdin.read().strip()
    if not prompt:
        parser.print_help()
        sys.exit(1)

    cfg = load_config(args.config)

    if args.top_k is not None:
        cfg.query.top_k = args.top_k
    if args.graph is not None:
        cfg.graph.path = args.graph

    project_root = os.path.abspath(cfg.graph.project_root)
    graph_path = (
        cfg.graph.path
        if os.path.isabs(cfg.graph.path)
        else os.path.join(os.getcwd(), cfg.graph.path)
    )

    if args.no_context:
        ranked = []
    else:
        try:
            index = load_index(graph_path, cfg.graph.skip_patterns)
        except GraphNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(2)

        ranked = query_files(
            prompt,
            index,
            cfg.query.top_k,
            cfg.query.community_boost_weight,
            cfg.query.bm25_k1,
            cfg.query.bm25_b,
        )

    if args.list_files:
        for rf in ranked:
            print(f"{rf.score:.3f}  {rf.source_file}")
        sys.exit(0)

    if args.dry_run:
        print(runner.build_prompt(prompt, ranked, cfg, project_root))
        sys.exit(0)

    try:
        exit_code = runner.run(prompt, ranked, cfg, project_root)
        sys.exit(exit_code)
    except CodexNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(127)
    except CodexTimeoutError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(124)
