from __future__ import annotations

import difflib
import json
import os
from collections import defaultdict

from codex_graph.graph_query import _tokenize


class GraphNav:
    def __init__(self, graph_path: str, skip_patterns: list[str] | None = None, graph: dict | None = None):
        if graph is None:
            with open(graph_path) as f:
                graph = json.load(f)
        self.skip = skip_patterns or []
        self._label_index = None
        self.id2node: dict = {}
        self.file2ids: dict[str, list] = defaultdict(list)
        for n in graph.get("nodes", []):
            nid = n.get("id")
            if nid is None:
                continue
            self.id2node[nid] = n
            sf = n.get("source_file", "")
            if sf:
                self.file2ids[sf].append(nid)
        self.in_edges: dict[object, list] = defaultdict(list)
        self.out_edges: dict[object, list] = defaultdict(list)
        for e in graph.get("links", []):
            s, t = e.get("source"), e.get("target")
            if s is None or t is None:
                continue
            rel = e.get("relation", "")
            self.out_edges[s].append((t, rel))
            self.in_edges[t].append((s, rel))

    def _skipped(self, sf: str) -> bool:
        return (not sf) or any(p in sf for p in self.skip)

    def _loc(self, nid) -> str:
        n = self.id2node.get(nid, {})
        sf = n.get("source_file", "?")
        loc = n.get("source_location", "")
        return f"{sf}:{loc}" if loc else sf

    def _labels(self) -> dict[str, list]:
        if getattr(self, "_label_index", None) is None:
            index: dict[str, list] = defaultdict(list)
            for nid, n in self.id2node.items():
                if n.get("file_type") != "code":
                    continue
                label = n.get("label", "")
                if not label or self._skipped(n.get("source_file", "")):
                    continue
                index[label.lower()].append(nid)
                norm = n.get("norm_label", "")
                if norm and norm.lower() != label.lower():
                    index[norm.lower()].append(nid)
            self._label_index = dict(index)
        return self._label_index

    def _fuzzy_ids(self, query: str, n: int, cutoff: float = 0.6) -> list:
        labels = self._labels()
        matches = difflib.get_close_matches(query.lower(), list(labels.keys()), n=n, cutoff=cutoff)
        ids, seen = [], set()
        for m in matches:
            for nid in labels[m]:
                if nid not in seen:
                    seen.add(nid)
                    ids.append(nid)
        return ids

    def find_symbols(self, query: str, k: int = 8) -> list[dict]:
        q = set(_tokenize(query))
        if not q:
            return []
        scored = []
        for n in self.id2node.values():
            if n.get("file_type") != "code":
                continue
            sf = n.get("source_file", "")
            label = n.get("label", "")
            if self._skipped(sf) or not label:
                continue
            toks = set(_tokenize(label)) | set(_tokenize(os.path.basename(os.path.splitext(sf)[0])))
            overlap = len(q & toks)
            if overlap:
                scored.append((overlap, label, sf, n.get("source_location", "")))
        scored.sort(key=lambda x: -x[0])
        if scored:
            return [{"symbol": l, "file": sf, "loc": loc, "fuzzy": False} for _, l, sf, loc in scored[:k]]
        hits = []
        for nid in self._fuzzy_ids(query, n=k)[:k]:
            n = self.id2node[nid]
            hits.append(
                {
                    "symbol": n.get("label", ""),
                    "file": n.get("source_file", ""),
                    "loc": n.get("source_location", ""),
                    "fuzzy": True,
                }
            )
        return hits

    def neighbors(self, symbol: str, k: int = 12) -> dict:
        q = set(_tokenize(symbol))
        best, best_ov = None, 0
        for nid, n in self.id2node.items():
            ov = len(q & set(_tokenize(n.get("label", ""))))
            if ov > best_ov:
                best, best_ov = nid, ov
        fuzzy = False
        if best is None:
            fuzzy_ids = self._fuzzy_ids(symbol, n=1)
            if fuzzy_ids:
                best, fuzzy = fuzzy_ids[0], True
        if best is None:
            return {"symbol": symbol, "found": False}
        callers, callees = [], []
        for s, rel in self.in_edges.get(best, []):
            sn = self.id2node.get(s, {})
            if self._skipped(sn.get("source_file", "")):
                continue
            callers.append(f"{sn.get('label', '?')} ({self._loc(s)}) --{rel}-->")
        for t, rel in self.out_edges.get(best, []):
            tn = self.id2node.get(t, {})
            if self._skipped(tn.get("source_file", "")):
                continue
            callees.append(f"--{rel}--> {tn.get('label', '?')} ({self._loc(t)})")
        result = {
            "symbol": self.id2node[best].get("label"),
            "defined_at": self._loc(best),
            "callers": callers[:k],
            "callees": callees[:k],
            "fuzzy": fuzzy,
        }
        if fuzzy:
            result["query"] = symbol
        return result

    def references_to(self, files: list[str], limit: int = 12) -> list[str]:
        target_ids = set()
        for sf in files:
            target_ids.update(self.file2ids.get(sf, []))
        seen, rows = set(), []
        file_set = set(files)
        for tid in target_ids:
            tnode = self.id2node.get(tid, {})
            for s, rel in self.in_edges.get(tid, []):
                sn = self.id2node.get(s, {})
                sf = sn.get("source_file", "")
                if self._skipped(sf) or sf in file_set:
                    continue
                key = (sf, sn.get("source_location", ""), tnode.get("label", ""))
                if key in seen:
                    continue
                seen.add(key)
                loc = sn.get("source_location", "")
                rows.append(
                    f"{sf}:{loc} {sn.get('label', '?')} --{rel}--> {tnode.get('label', '?')}"
                )
                if len(rows) >= limit:
                    return rows
        return rows
