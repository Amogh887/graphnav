from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from codex_graph import GraphNotFoundError


@dataclass
class RankedFile:
    source_file: str
    score: float


def _tokenize(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) >= 2]


class GraphIndex:
    _TYPE_WEIGHT = {"rationale": 3, "document": 2, "concept": 2, "code": 1}

    def __init__(self, graph_path: str, skip_patterns: list[str]):
        with open(graph_path) as f:
            graph = json.load(f)

        nodes = graph.get("nodes", [])

        self.file_tokens: dict[str, list[str]] = defaultdict(list)
        self.file_communities: dict[str, set[int]] = defaultdict(set)
        self.community_tokens: dict[int, set[str]] = defaultdict(set)

        for n in nodes:
            sf = n.get("source_file", "")
            label = n.get("norm_label") or n.get("label") or ""
            cid = n.get("community")
            tokens = _tokenize(label)

            if cid is not None:
                self.community_tokens[cid].update(tokens)

            if not sf or any(p in sf for p in skip_patterns):
                continue

            weight = self._TYPE_WEIGHT.get(n.get("file_type", "code"), 1)
            self.file_tokens[sf].extend(tokens * weight)
            if cid is not None:
                self.file_communities[sf].add(cid)

        self._N = len(self.file_tokens)
        self._avgdl = (
            sum(len(t) for t in self.file_tokens.values()) / max(self._N, 1)
        )
        self._df: dict[str, int] = defaultdict(int)
        for tokens in self.file_tokens.values():
            for t in set(tokens):
                self._df[t] += 1

    def _bm25(self, query_tokens: list[str], sf: str, k1: float, b: float) -> float:
        doc = self.file_tokens.get(sf, [])
        dl = len(doc)
        tf_counts = Counter(doc)
        score = 0.0
        for t in query_tokens:
            df = self._df.get(t)
            if not df:
                continue
            idf = math.log((self._N - df + 0.5) / (df + 0.5) + 1)
            tf = tf_counts[t]
            tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / self._avgdl))
            score += idf * tf_norm
        return score

    def _community_boost(self, query_tokens: list[str], sf: str, boost_weight: float) -> float:
        qset = set(query_tokens)
        best = 0.0
        for cid in self.file_communities.get(sf, set()):
            ctokens = self.community_tokens[cid]
            overlap = len(qset & ctokens) / (len(qset) + 1)
            if overlap > best:
                best = overlap
        return best * boost_weight

    def rank(
        self,
        prompt: str,
        top_k: int,
        community_boost_weight: float,
        bm25_k1: float,
        bm25_b: float,
    ) -> list[RankedFile]:
        qtoks = _tokenize(prompt)
        if not qtoks:
            return []
        scores = {
            sf: self._bm25(qtoks, sf, bm25_k1, bm25_b)
            + self._community_boost(qtoks, sf, community_boost_weight)
            for sf in self.file_tokens
        }
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        return [
            RankedFile(source_file=sf, score=sc)
            for sf, sc in ranked[:top_k]
            if sc > 0
        ]


def load_index(graph_path: str, skip_patterns: list[str]) -> GraphIndex:
    if not os.path.exists(graph_path):
        raise GraphNotFoundError(
            f"graph.json not found: {graph_path}\n"
            "Run Graphify on the repo first, or set [graph] path in config.toml"
        )
    return GraphIndex(graph_path, skip_patterns)


def query_files(
    prompt: str,
    index: GraphIndex,
    top_k: int,
    community_boost_weight: float = 2.0,
    bm25_k1: float = 1.5,
    bm25_b: float = 0.75,
) -> list[RankedFile]:
    return index.rank(prompt, top_k, community_boost_weight, bm25_k1, bm25_b)
