from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphnav.multirepo import ServiceInfo


def make_graph_dict(nodes=None, links=None) -> dict:
    return {
        "directed": False,
        "multigraph": False,
        "nodes": nodes or [],
        "links": links or [],
        "hyperedges": [],
    }


def write_graph(path: Path, nodes=None, links=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(make_graph_dict(nodes, links)))


def make_mock_proc(returncode: int = 0):
    from unittest.mock import MagicMock
    proc = MagicMock()
    proc.stdout = iter([])
    proc.stderr = iter([])
    proc.returncode = returncode
    proc.wait.return_value = None
    proc.poll.return_value = returncode
    return proc


@pytest.fixture(autouse=True)
def _no_auto_rebuild(monkeypatch):
    monkeypatch.setenv("GRAPHNAV_NO_AUTO_REBUILD", "1")


@pytest.fixture
def two_svc_root(tmp_path) -> Path:
    for name in ("svc-a", "svc-b"):
        d = tmp_path / name
        d.mkdir()
        (d / "pyproject.toml").touch()
    return tmp_path


@pytest.fixture
def two_svc_root_with_graphs(tmp_path) -> Path:
    for name in ("svc-a", "svc-b"):
        d = tmp_path / name
        d.mkdir()
        (d / "pyproject.toml").touch()
        write_graph(d / "graphify-out" / "graph.json")
    return tmp_path


@pytest.fixture
def cross_service_graph(tmp_path) -> tuple[Path, list[ServiceInfo]]:
    nodes = [
        {"id": "svc_a_client", "label": "Client", "source_file": "svc-a/client.py", "community": 0},
        {"id": "svc_b_server", "label": "Server", "source_file": "svc-b/server.py", "community": 1},
        {"id": "stdlib_exception", "label": "Exception", "source_file": "", "community": 2},
    ]
    links = [
        {
            "source": "svc_a_client",
            "target": "svc_b_server",
            "relation": "calls",
            "source_file": "svc-a/client.py",
        }
    ]
    graph_path = tmp_path / "merged-graph.json"
    write_graph(graph_path, nodes, links)

    services = [
        ServiceInfo("svc-a", str(tmp_path / "svc-a"), str(tmp_path / "svc-a" / "graphify-out" / "graph.json")),
        ServiceInfo("svc-b", str(tmp_path / "svc-b"), str(tmp_path / "svc-b" / "graphify-out" / "graph.json")),
    ]
    return graph_path, services
