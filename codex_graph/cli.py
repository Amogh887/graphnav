from __future__ import annotations

import argparse
import os
import sys

from codex_graph import CodexNotFoundError, CodexTimeoutError, GraphNotFoundError
from codex_graph.config import load_config
from codex_graph.graph_query import query_files
from codex_graph import runner


def _run_mono_command(cmd: str, argv: list[str]) -> None:
    from codex_graph import multirepo

    parser = argparse.ArgumentParser(
        prog=f"graphnav {cmd}",
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
    services = multirepo.detect_services(root, cfg.mono.marker_files, cfg.mono.extra_skip_dirs)
    if not services:
        return

    names = ", ".join(s.name for s in services)
    print(f"[graphnav] Detected {len(services)} service(s): {names}")
    print(f"[graphnav] Running 'graphnav map' to build knowledge graphs ...", file=sys.stderr)
    rc = multirepo.run_map(root=root, mono_cfg=cfg.mono)
    sys.exit(rc)


def _run_context_command(argv: list[str]) -> None:
    from codex_graph import multirepo

    parser = argparse.ArgumentParser(
        prog="graphnav context",
        description="Print a token-budgeted context pack for a coding task. Defaults to inline code regions; use --locations-only for the file:line index.",
    )
    parser.add_argument("task", nargs="?", help="The coding task, in natural language")
    parser.add_argument("--root", default=".", metavar="PATH", help="Repo root (default: current directory)")
    parser.add_argument("--budget", type=int, default=None, metavar="N", help="Approx token budget for the pack")
    parser.add_argument("--files", type=int, default=None, metavar="N", help="Max number of files to include")
    parser.add_argument("--locations-only", action="store_true", help="Emit file:line locations instead of inline code regions")
    parser.add_argument("--config", default=None, metavar="PATH", help="Path to config.toml")
    args = parser.parse_args(argv)

    task = args.task
    if not task and not sys.stdin.isatty():
        task = sys.stdin.read().strip()
    if not task:
        parser.print_help()
        sys.exit(1)

    cfg = load_config(args.config)
    if args.locations_only:
        pack = multirepo.build_context_pack(
            root=args.root,
            task=task,
            top_files=args.files if args.files is not None else cfg.mono.context_top_files,
            budget_tokens=args.budget if args.budget is not None else cfg.mono.context_budget_tokens,
            skip_patterns=cfg.graph.skip_patterns,
            query_cfg=cfg.query,
            mono_cfg=cfg.mono,
        )
    else:
        kwargs = {
            "root": args.root,
            "task": task,
            "skip_patterns": cfg.graph.skip_patterns,
            "query_cfg": cfg.query,
        }
        if args.files is not None:
            kwargs["top_files"] = args.files
        if args.budget is not None:
            kwargs["budget_tokens"] = args.budget
        pack = multirepo.build_context_pack_inline(**kwargs)
    print(pack)
    sys.exit(0)


def _run_graph_query_command(kind: str, argv: list[str]) -> None:
    from codex_graph import multirepo
    from codex_graph.graph_cache import load_bundle

    parser = argparse.ArgumentParser(prog=f"graphnav {kind}")
    parser.add_argument("term", nargs="?", help="query (find) or symbol (neighbors)")
    parser.add_argument("--root", default=".", metavar="PATH")
    parser.add_argument("--config", default=None, metavar="PATH")
    args = parser.parse_args(argv)
    if not args.term:
        parser.print_help()
        sys.exit(1)

    cfg = load_config(args.config)
    root = os.path.abspath(args.root)
    graph_path = multirepo._overarching_graph_path(root)
    if not os.path.exists(graph_path):
        print(f"Error: no knowledge graph at {graph_path}. Run `graphnav map` first.", file=sys.stderr)
        sys.exit(2)

    if kind == "impact":
        from codex_graph.mcp_server import GraphTools

        tools = GraphTools(root, cfg.graph.skip_patterns, query_cfg=cfg.query)
        print(tools.impact(args.term))
        sys.exit(0)

    nav = load_bundle(
        graph_path, cfg.graph.skip_patterns,
        relation_weights=cfg.query.edge_relation_weights, repo_root=root,
    ).nav

    if kind == "find":
        hits = nav.find_symbols(args.term, k=10)
        if not hits:
            print("(no matches)")
        elif all(h.get("fuzzy") for h in hits):
            print("(no exact match — closest symbols:)")
        for h in hits:
            print(f"{h['symbol']} — {h['file']}:{h['loc']}")
    else:
        r = nav.neighbors(args.term)
        if not r.get("found", True):
            print("(symbol not found)")
            sys.exit(0)
        print(f"{r['symbol']} defined at {r['defined_at']}")
        if r.get("fuzzy"):
            print(f'(closest match for "{r["query"]}")')
        if r.get("callers"):
            print("callers:")
            for c in r["callers"]:
                print("  " + c)
        if r.get("callees"):
            print("calls:")
            for c in r["callees"]:
                print("  " + c)
    sys.exit(0)


def _run_doctor_command(argv: list[str]) -> None:
    from codex_graph.doctor import run_doctor

    parser = argparse.ArgumentParser(
        prog="graphnav doctor",
        description="Diagnose a graphnav setup: graphify binary, config, graph, API key, services, cache",
    )
    parser.add_argument("--root", default=".", metavar="PATH", help="Repo root (default: current directory)")
    parser.add_argument("--config", default=None, metavar="PATH", help="Path to config.toml")
    args = parser.parse_args(argv)
    sys.exit(run_doctor(root=args.root, config_path=args.config))


def _run_serve_command(argv: list[str]) -> None:
    from codex_graph import mcp_server

    parser = argparse.ArgumentParser(
        prog="graphnav serve",
        description="Run the graphnav MCP server (stdio) so AI agents can call the graph tools natively",
    )
    parser.add_argument("--root", default=".", metavar="PATH", help="Repo root (default: current directory)")
    parser.add_argument("--config", default=None, metavar="PATH", help="Path to config.toml")
    args = parser.parse_args(argv)
    sys.exit(mcp_server.serve(root=args.root, config_path=args.config))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("map", "watch"):
        _run_mono_command(sys.argv[1], sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "context":
        _run_context_command(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        _run_serve_command(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("find", "neighbors", "impact"):
        _run_graph_query_command(sys.argv[1], sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        _run_doctor_command(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        prog="graphnav",
        description=(
            "Codex CLI with knowledge-graph context injection for monorepos.\n\n"
            "First-run (after pip install): just run 'graphnav' or 'graphnav map'\n"
            "in your monorepo root — services are auto-detected and graphs are built.\n\n"
            "Subcommands:\n"
            "  map      Build per-service graphs and cross-service bridge notes\n"
            "  watch    Keep graphs and bridge notes up-to-date as files change\n"
            "  context  Print a token-budgeted context pack for a task (free, no LLM)\n"
            "  serve    Run the MCP server so AI agents call the graph tools natively\n"
            "  find     Find symbols by query; neighbors/impact show a symbol's blast radius\n"
            "  doctor   Diagnose the setup (graphify binary, config, graph, API key, cache)"
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
        from codex_graph.graph_cache import load_bundle

        try:
            bundle = load_bundle(
                graph_path, cfg.graph.skip_patterns,
                relation_weights=cfg.query.edge_relation_weights,
                repo_root=project_root,
            )
        except GraphNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(2)

        ranked = query_files(
            prompt,
            bundle.index,
            cfg.query.top_k,
            cfg.query.community_boost_weight,
            cfg.query.bm25_k1,
            cfg.query.bm25_b,
            edge_boost_weight=cfg.query.edge_boost_weight,
            recency=bundle.recency,
            recency_boost_weight=cfg.query.recency_boost_weight,
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


if __name__ == "__main__":
    main()
