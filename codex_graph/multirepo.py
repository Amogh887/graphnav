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


def _load_env_file(root: str) -> dict[str, str]:
    env_vars: dict[str, str] = {}
    candidates = [
        os.path.join(root, ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, _, value = line.partition("=")
                        env_vars[key.strip()] = value.strip().strip('"').strip("'")
            except OSError:
                pass
            break
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
        for marker in marker_set:
            if os.path.exists(os.path.join(abs_path, marker)):
                services.append(ServiceInfo(
                    name=entry,
                    abs_path=abs_path,
                    graph_path=os.path.join(abs_path, "graphify-out", "graph.json"),
                ))
                break
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
    t_out.join()
    t_err.join()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
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


def run_merge(
    services: list[ServiceInfo],
    graphify_path: str,
    merged_out: str,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> int:
    os.makedirs(os.path.dirname(merged_out), exist_ok=True)
    graph_paths = [s.graph_path for s in services]
    print(f"[codex-graph] merging {len(graph_paths)} graphs ...", file=sys.stderr)
    proc = subprocess.Popen(
        [graphify_path, "merge-graphs", *graph_paths, "--out", merged_out],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    return _stream_proc(proc, timeout)


def _service_of(source_file: str, service_names: set[str]) -> str | None:
    if not source_file:
        return None
    prefix = source_file.split("/")[0]
    return prefix if prefix in service_names else None


def analyze_bridges(
    merged_graph_path: str,
    services: list[ServiceInfo],
) -> dict[str, list[BridgeRow]]:
    with open(merged_graph_path) as f:
        graph = json.load(f)

    service_names = {s.name for s in services}
    node_by_id: dict[str, dict] = {n["id"]: n for n in graph.get("nodes", [])}
    bridges: dict[str, list[BridgeRow]] = {s.name: [] for s in services}

    for link in graph.get("links", []):
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


def _reanalyze_and_write(
    root: str,
    services: list[ServiceInfo],
    graphify_path: str,
    merged_out: str,
    env: dict[str, str] | None = None,
) -> None:
    rc = run_merge(services, graphify_path, merged_out, env=env)
    if rc != 0:
        print(f"[codex-graph] merge failed (exit {rc})", file=sys.stderr)
        return
    bridges = analyze_bridges(merged_out, services)
    for svc in services:
        write_bridges_md(svc, bridges[svc.name])
    write_monorepo_map(root, services)


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
        print(f"No services detected in {root}. Add marker files (package.json, pyproject.toml, etc.) to subdirectories.", file=sys.stderr)
        return 1

    if dry_run:
        print(f"Detected {len(services)} service(s):")
        for svc in services:
            print(f"  {svc.name}  {svc.abs_path}")
        print("[dry-run] No graphify calls made.")
        return 0

    backend = backend_override or mono_cfg.graphify_backend
    env = _build_subprocess_env(root)
    succeeded: list[ServiceInfo] = []
    total = len(services)
    for i, svc in enumerate(services, 1):
        print(f"[codex-graph] [{i}/{total}] extracting {svc.name}", file=sys.stderr)
        rc = run_extract(svc, graphify_path, backend, env=env)
        if rc == 0 and os.path.exists(svc.graph_path):
            succeeded.append(svc)
        else:
            print(f"[codex-graph] WARNING: extraction failed for {svc.name} (exit {rc})", file=sys.stderr)

    if not succeeded:
        print("Error: no service graphs were built successfully.", file=sys.stderr)
        return 1

    print(f"\nDone. {len(succeeded)}/{total} services mapped.")
    for svc in succeeded:
        print(f"  {svc.name} graph : {svc.graph_path}")

    if len(succeeded) < 2:
        print("  (only 1 service — skipping merge and bridge analysis)")
        return 0

    merged_out = os.path.join(root, "graphify-out", "merged-graph.json")
    _reanalyze_and_write(root, succeeded, graphify_path, merged_out, env=env)

    print(f"  Merged graph : {merged_out}")
    print(f"  Monorepo map : {os.path.join(root, 'graphify-out', 'MONOREPO_MAP.md')}")
    for svc in succeeded:
        print(f"  {svc.name} bridges : {os.path.join(svc.abs_path, 'graphify-out', 'BRIDGES.md')}")
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
    merged_out = os.path.join(root, "graphify-out", "merged-graph.json")

    print(f"[codex-graph] Bootstrapping {len(services)} service(s) ...", file=sys.stderr)
    active: list[ServiceInfo] = []
    for svc in services:
        if not os.path.exists(svc.graph_path):
            rc = run_extract(svc, graphify_path, backend, env=env)
            if rc != 0:
                print(f"[codex-graph] WARNING: bootstrap extraction failed for {svc.name}", file=sys.stderr)
                continue
        active.append(svc)

    if len(active) >= 2:
        _reanalyze_and_write(root, active, graphify_path, merged_out, env=env)
    elif active:
        print("[codex-graph] WARNING: only 1 service graph available; bridge analysis skipped until more services extract.", file=sys.stderr)

    watch_procs: dict[str, subprocess.Popen] = {}

    def _start_watch(svc: ServiceInfo) -> subprocess.Popen:
        return subprocess.Popen(
            [graphify_path, "watch", svc.abs_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    for svc in active:
        watch_procs[svc.name] = _start_watch(svc)
        print(f"[codex-graph] watching {svc.name}", file=sys.stderr)

    mtimes: dict[str, float] = {}
    for svc in active:
        try:
            mtimes[svc.name] = os.stat(svc.graph_path).st_mtime
        except OSError:
            mtimes[svc.name] = 0.0

    print("[codex-graph] Watch active. Press Ctrl-C to stop.", file=sys.stderr)
    try:
        while True:
            time.sleep(mono_cfg.watch_poll_interval)

            changed = []
            for svc in active:
                try:
                    mtime = os.stat(svc.graph_path).st_mtime
                except OSError:
                    continue
                if mtime != mtimes[svc.name]:
                    mtimes[svc.name] = mtime
                    changed.append(svc.name)

            if changed:
                ts = time.strftime("%H:%M:%S")
                print(f"[codex-graph] {ts} graph updated: {', '.join(changed)} — re-analyzing bridges ...", file=sys.stderr)
                _reanalyze_and_write(root, active, graphify_path, merged_out, env=env)

            for svc in list(active):
                proc = watch_procs.get(svc.name)
                if proc and proc.poll() is not None:
                    print(f"[codex-graph] WARNING: graphify watch exited for {svc.name} (exit {proc.returncode}), restarting ...", file=sys.stderr)
                    watch_procs[svc.name] = _start_watch(svc)

    except KeyboardInterrupt:
        print("\n[codex-graph] Stopping watch ...", file=sys.stderr)
        for proc in watch_procs.values():
            proc.terminate()
        for proc in watch_procs.values():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return 0
