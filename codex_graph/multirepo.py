from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from codex_graph.config import MonoConfig


SOURCE_EXTENSIONS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".swift", ".scala",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".mm", ".dart", ".ex", ".exs",
})

SKIP_DIRS = frozenset({
    "node_modules", "dist", "build", "out", "target", "vendor", "bin", "obj",
    "__pycache__", "graphify-out", "venv", ".venv", "env", "site-packages",
    ".next", ".nuxt", "coverage", "test-results", "playwright-report",
    ".pytest_cache", ".mypy_cache", ".git", ".github", ".idea", ".vscode",
})


def _find_env_file(start: str) -> str | None:
    current = os.path.abspath(start)
    while True:
        candidate = os.path.join(current, ".env")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _parse_env_file(path: str) -> dict[str, str]:
    env_vars: dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env_vars


def _env_file_sources(root: str) -> list[str]:
    sources: list[str] = []
    seen: set[str] = set()

    def _add(path: str | None) -> None:
        if path and path not in seen and os.path.isfile(path):
            seen.add(path)
            sources.append(path)

    _add(_find_env_file(root))
    _add(_find_env_file(os.getcwd()))
    for base in (root, os.getcwd()):
        try:
            for entry in sorted(os.listdir(base)):
                _add(os.path.join(base, entry, ".env"))
        except OSError:
            pass
    return sources


def _load_env_file(root: str) -> dict[str, str]:
    env_vars: dict[str, str] = {}
    for path in _env_file_sources(root):
        for key, value in _parse_env_file(path).items():
            env_vars.setdefault(key, value)
    if "ANTHROPIC_KEY" in env_vars and "ANTHROPIC_API_KEY" not in env_vars:
        env_vars["ANTHROPIC_API_KEY"] = env_vars["ANTHROPIC_KEY"]
    return env_vars


def _build_subprocess_env(root: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(_load_env_file(root))
    return env


@dataclass
class ServiceInfo:
    name: str
    abs_path: str
    graph_path: str
    bridges_to: list[str] = field(default_factory=list)


@dataclass
class BridgeRow:
    local_file: str
    local_symbol: str
    relation: str
    remote_svc: str
    remote_file: str
    remote_symbol: str


def _has_source_files(path: str, max_depth: int = 4) -> bool:
    base = path.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(path):
        depth = dirpath.count(os.sep) - base
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".")
            ]
        for fn in filenames:
            if os.path.splitext(fn)[1] in SOURCE_EXTENSIONS:
                return True
    return False


def detect_services(root: str, marker_files: list[str]) -> list[ServiceInfo]:
    services = []
    marker_set = set(marker_files)
    try:
        entries = os.listdir(root)
    except OSError:
        return []
    for entry in sorted(entries):
        abs_path = os.path.join(root, entry)
        if not os.path.isdir(abs_path):
            continue
        if entry in SKIP_DIRS or entry.startswith("."):
            continue
        has_marker = any(
            os.path.exists(os.path.join(abs_path, marker)) for marker in marker_set
        )
        if has_marker or _has_source_files(abs_path):
            services.append(ServiceInfo(
                name=entry,
                abs_path=abs_path,
                graph_path=os.path.join(abs_path, "graphify-out", "graph.json"),
            ))
    return services


def _stream_proc(proc: subprocess.Popen, timeout: int) -> int:
    def _relay(src, dst):
        for line in src:
            dst.write(line)
            dst.flush()

    t_out = threading.Thread(target=_relay, args=(proc.stdout, sys.stderr), daemon=True)
    t_err = threading.Thread(target=_relay, args=(proc.stderr, sys.stderr), daemon=True)
    t_out.start()
    t_err.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    return proc.returncode


def run_extract(
    service: ServiceInfo,
    graphify_path: str,
    backend: str,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> int:
    print(f"[codex-graph] extracting {service.name} ...", file=sys.stderr)
    proc = subprocess.Popen(
        [graphify_path, "extract", service.abs_path, "--backend", backend, "--out", service.abs_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    return _stream_proc(proc, timeout)


def _overarching_graph_path(root: str) -> str:
    return os.path.join(root, "graphify-out", "graph.json")


def _overarching_service(root: str) -> ServiceInfo:
    return ServiceInfo(
        name="overarching (whole repo)",
        abs_path=root,
        graph_path=_overarching_graph_path(root),
    )


def build_overarching_graph(
    root: str,
    graphify_path: str,
    backend: str,
    timeout: int = 1200,
    env: dict[str, str] | None = None,
) -> int:
    return run_extract(_overarching_service(root), graphify_path, backend, timeout=timeout, env=env)


def _graph_links(graph: dict) -> list[dict]:
    links = graph.get("links")
    if links is None:
        links = graph.get("edges", [])
    return links


def partition_graph(
    overarching_graph_path: str,
    services: list[ServiceInfo],
) -> dict[str, int]:
    with open(overarching_graph_path) as f:
        graph = json.load(f)

    service_names = {s.name for s in services}
    node_svc: dict[str, str] = {}
    per_nodes: dict[str, list[dict]] = {s.name: [] for s in services}
    for node in graph.get("nodes", []):
        svc = _service_of(node.get("source_file", ""), service_names)
        if svc is not None:
            node_svc[node.get("id")] = svc
            per_nodes[svc].append(node)

    per_links: dict[str, list[dict]] = {s.name: [] for s in services}
    for link in _graph_links(graph):
        src_svc = node_svc.get(link.get("source"))
        tgt_svc = node_svc.get(link.get("target"))
        if src_svc is not None and src_svc == tgt_svc:
            per_links[src_svc].append(link)

    base_meta = {k: v for k, v in graph.items() if k not in ("nodes", "links", "edges")}
    counts: dict[str, int] = {}
    for svc in services:
        out_dir = os.path.join(svc.abs_path, "graphify-out")
        os.makedirs(out_dir, exist_ok=True)
        subgraph = dict(base_meta)
        subgraph["nodes"] = per_nodes[svc.name]
        subgraph["links"] = per_links[svc.name]
        with open(svc.graph_path, "w") as f:
            json.dump(subgraph, f, indent=2)
        counts[svc.name] = len(per_nodes[svc.name])
    return counts


def _service_of(source_file: str, service_names: set[str]) -> str | None:
    if not source_file:
        return None
    prefix = source_file.split("/")[0]
    return prefix if prefix in service_names else None


def analyze_bridges(
    overarching_graph_path: str,
    services: list[ServiceInfo],
) -> dict[str, list[BridgeRow]]:
    with open(overarching_graph_path) as f:
        graph = json.load(f)

    service_names = {s.name for s in services}
    node_by_id: dict[str, dict] = {n["id"]: n for n in graph.get("nodes", [])}
    bridges: dict[str, list[BridgeRow]] = {s.name: [] for s in services}

    for link in _graph_links(graph):
        src_node = node_by_id.get(link.get("source", ""))
        tgt_node = node_by_id.get(link.get("target", ""))
        if not src_node or not tgt_node:
            continue

        src_svc = _service_of(src_node.get("source_file", ""), service_names)
        tgt_svc = _service_of(tgt_node.get("source_file", ""), service_names)

        if not src_svc or not tgt_svc or src_svc == tgt_svc:
            continue

        link_sf = link.get("source_file", "")
        local_svc = _service_of(link_sf, service_names) or src_svc

        if local_svc == src_svc:
            local_node, remote_node, remote_svc = src_node, tgt_node, tgt_svc
        else:
            local_node, remote_node, remote_svc = tgt_node, src_node, src_svc

        local_file = local_node.get("source_file", "").removeprefix(local_svc + "/")
        bridges[local_svc].append(BridgeRow(
            local_file=local_file,
            local_symbol=local_node.get("label", ""),
            relation=link.get("relation", ""),
            remote_svc=remote_svc,
            remote_file=remote_node.get("source_file", ""),
            remote_symbol=remote_node.get("label", ""),
        ))

    for svc in services:
        remote_svcs = sorted({r.remote_svc for r in bridges[svc.name]})
        svc.bridges_to = remote_svcs

    return bridges


def write_bridges_md(service: ServiceInfo, rows: list[BridgeRow]) -> str:
    out_dir = os.path.join(service.abs_path, "graphify-out")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "BRIDGES.md")
    lines = [f"# Bridges: {service.name}", ""]
    if not rows:
        lines.append("_No cross-service connections detected._")
    else:
        lines.append("| Local File | Symbol | Relation | → Service | Remote File | Remote Symbol |")
        lines.append("|---|---|---|---|---|---|")
        for r in rows:
            lines.append(f"| {r.local_file} | {r.local_symbol} | {r.relation} | {r.remote_svc} | {r.remote_file} | {r.remote_symbol} |")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def write_monorepo_map(root: str, services: list[ServiceInfo]) -> str:
    out_dir = os.path.join(root, "graphify-out")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "MONOREPO_MAP.md")
    lines = ["# Monorepo Map", "", "| Service | Graph | Bridges To |", "|---|---|---|"]
    for svc in services:
        graph_rel = os.path.relpath(svc.graph_path, root)
        bridges_cell = ", ".join(svc.bridges_to) if svc.bridges_to else "_none_"
        lines.append(f"| {svc.name} | {graph_rel} | {bridges_cell} |")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def write_copilot_instructions(root: str, services: list[ServiceInfo]) -> str:
    gh_dir = os.path.join(root, ".github")
    os.makedirs(gh_dir, exist_ok=True)
    path = os.path.join(gh_dir, "copilot-instructions.md")

    map_rel = os.path.relpath(os.path.join(root, "graphify-out", "MONOREPO_MAP.md"), root)

    lines = [
        "# Copilot Instructions",
        "",
        "This repository uses graphify knowledge graphs for architecture-aware code navigation.",
        "Before making changes, consult the relevant architecture files below to understand",
        "cross-service dependencies and avoid breaking integrations.",
        "",
        "## Monorepo Map",
        "",
        f"[{map_rel}]({map_rel}) — overview of all services and their cross-service connections.",
        "",
        "## Per-Service Graphs and Bridges",
        "",
        "| Service | Knowledge Graph | Cross-Service Bridges |",
        "|---|---|---|",
    ]
    for svc in services:
        graph_rel = os.path.relpath(svc.graph_path, root)
        bridges_rel = os.path.relpath(
            os.path.join(svc.abs_path, "graphify-out", "BRIDGES.md"), root
        )
        lines.append(f"| {svc.name} | [{graph_rel}]({graph_rel}) | [{bridges_rel}]({bridges_rel}) |")

    lines += [
        "",
        "## How to Use",
        "",
        "- **Working in a service?** Open its `BRIDGES.md` first to see what other services it depends on.",
        "- **Refactoring a symbol?** Check the knowledge graph (`graph.json`) to find all callers and dependencies.",
        "- **Adding a cross-service feature?** Check `MONOREPO_MAP.md` to understand the full dependency graph.",
        "- Graphs are kept up-to-date automatically by `codex-graph watch`.",
    ]

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _refresh(
    root: str,
    services: list[ServiceInfo],
    overarching_graph_path: str,
) -> dict[str, list[BridgeRow]]:
    partition_graph(overarching_graph_path, services)
    bridges = analyze_bridges(overarching_graph_path, services)
    for svc in services:
        write_bridges_md(svc, bridges[svc.name])
    write_monorepo_map(root, services)
    write_copilot_instructions(root, services)
    return bridges


def run_map(
    root: str,
    mono_cfg: MonoConfig,
    backend_override: str | None = None,
    dry_run: bool = False,
) -> int:
    root = os.path.abspath(root)
    graphify_path = shutil.which("graphify")
    if graphify_path is None:
        print("Error: 'graphify' not found on PATH. Install with: pip install graphifyy", file=sys.stderr)
        return 1

    services = detect_services(root, mono_cfg.marker_files)
    if not services:
        print(f"No services detected in {root}. Add code to subdirectories (or marker files like package.json/pyproject.toml).", file=sys.stderr)
        return 1

    if dry_run:
        print(f"Detected {len(services)} service(s):")
        for svc in services:
            print(f"  {svc.name}  {svc.abs_path}")
        print("[dry-run] No graphify calls made.")
        return 0

    backend = backend_override or mono_cfg.graphify_backend
    env = _build_subprocess_env(root)
    overarching_path = _overarching_graph_path(root)

    print(f"[codex-graph] Building overarching graph across {len(services)} service(s): {', '.join(s.name for s in services)}", file=sys.stderr)
    rc = build_overarching_graph(root, graphify_path, backend, env=env)
    if rc != 0 or not os.path.exists(overarching_path):
        print(f"Error: overarching graphify extraction failed (exit {rc}).", file=sys.stderr)
        print("  Ensure an API key is available (e.g. ANTHROPIC_API_KEY or ANTHROPIC_KEY in a .env file).", file=sys.stderr)
        return 1

    bridges = _refresh(root, services, overarching_path)
    total_bridges = sum(len(rows) for rows in bridges.values())

    print(f"\nDone. {len(services)} service(s) mapped, {total_bridges} cross-service connection(s) found.")
    print(f"  Overarching graph    : {overarching_path}")
    for svc in services:
        to = ", ".join(svc.bridges_to) if svc.bridges_to else "none"
        print(f"  {svc.name}/graphify-out/  (bridges -> {to})")
    print(f"  Monorepo map         : {os.path.join(root, 'graphify-out', 'MONOREPO_MAP.md')}")
    print(f"  Copilot instructions : {os.path.join(root, '.github', 'copilot-instructions.md')}")
    return 0


def run_watch(
    root: str,
    mono_cfg: MonoConfig,
    backend_override: str | None = None,
) -> int:
    root = os.path.abspath(root)
    graphify_path = shutil.which("graphify")
    if graphify_path is None:
        print("Error: 'graphify' not found on PATH. Install with: pip install graphifyy", file=sys.stderr)
        return 1

    services = detect_services(root, mono_cfg.marker_files)
    if not services:
        print(f"No services detected in {root}.", file=sys.stderr)
        return 1

    backend = backend_override or mono_cfg.graphify_backend
    env = _build_subprocess_env(root)
    overarching_path = _overarching_graph_path(root)

    if not os.path.exists(overarching_path):
        print(f"[codex-graph] Bootstrapping overarching graph for {len(services)} service(s) ...", file=sys.stderr)
        rc = build_overarching_graph(root, graphify_path, backend, env=env)
        if rc != 0 or not os.path.exists(overarching_path):
            print(f"Error: bootstrap extraction failed (exit {rc}).", file=sys.stderr)
            return 1

    _refresh(root, services, overarching_path)

    def _start_watch() -> subprocess.Popen:
        return subprocess.Popen(
            [graphify_path, "watch", root],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

    watch_proc = _start_watch()
    try:
        last_mtime = os.stat(overarching_path).st_mtime
    except OSError:
        last_mtime = 0.0

    print(f"[codex-graph] Watching {root} ({len(services)} service(s)). Press Ctrl-C to stop.", file=sys.stderr)
    try:
        while True:
            time.sleep(mono_cfg.watch_poll_interval)

            try:
                mtime = os.stat(overarching_path).st_mtime
            except OSError:
                mtime = last_mtime
            if mtime != last_mtime:
                last_mtime = mtime
                ts = time.strftime("%H:%M:%S")
                print(f"[codex-graph] {ts} graph updated — re-partitioning and re-analyzing bridges ...", file=sys.stderr)
                _refresh(root, services, overarching_path)

            if watch_proc.poll() is not None:
                print(f"[codex-graph] WARNING: graphify watch exited (exit {watch_proc.returncode}), restarting ...", file=sys.stderr)
                watch_proc = _start_watch()

    except KeyboardInterrupt:
        print("\n[codex-graph] Stopping watch ...", file=sys.stderr)
        watch_proc.terminate()
        try:
            watch_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            watch_proc.kill()
        return 0
