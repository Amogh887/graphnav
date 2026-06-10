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


ALLOWED_EXTENSIONS = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".java", ".kt", ".kts", ".cs", ".rb", ".rs", ".php",
    ".swift", ".scala", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
    ".hh", ".m", ".mm", ".lua", ".dart", ".ex", ".exs", ".clj",
    ".cljs", ".groovy", ".vue", ".svelte", ".sh", ".bash", ".zsh",
    ".pl", ".r", ".sql", ".proto", ".thrift", ".graphql", ".gql",
    ".md", ".mdx", ".markdown", ".rst", ".txt", ".adoc",
}

GENERATED_PATTERNS = (
    ".pb.go", ".pb.cc", ".pb.h", "_pb2.py", "_pb2.pyi", "_pb2_grpc.py",
    "pb2_grpc", "_grpc.pb.", "genproto/", "/generated/", ".generated.",
    ".g.dart", "_pb.dart", "/migrations/",
)


def _is_rankable(source_file: str) -> bool:
    lower = source_file.lower()
    if any(p in lower for p in GENERATED_PATTERNS):
        return False
    return os.path.splitext(lower)[1] in ALLOWED_EXTENSIONS


_IDENT_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z]+|[a-z]+|[A-Z]+|[0-9]+")


def _stem(t: str) -> str:
    if len(t) <= 4 or t.endswith("ss"):
        return t
    if t.endswith("ies"):
        return t[:-3] + "y"
    if t.endswith("es"):
        return t[:-2]
    if t.endswith("s"):
        return t[:-1]
    return t


def _tokenize(s: str) -> list[str]:
    toks: list[str] = []
    for word in re.split(r"[^A-Za-z0-9]+", s):
        if not word:
            continue
        for sub in (_IDENT_RE.findall(word) or [word]):
            t = sub.lower()
            if len(t) >= 2:
                toks.append(_stem(t))
    return toks


class GraphIndex:
    _TYPE_WEIGHT = {"rationale": 3, "document": 2, "concept": 2, "code": 1}

    def __init__(self, graph_path: str, skip_patterns: list[str]):
        with open(graph_path) as f:
            graph = json.load(f)

        nodes = graph.get("nodes", [])

        self.file_tokens: dict[str, list[str]] = defaultdict(list)
        self.file_communities: dict[str, set[int]] = defaultdict(set)
        self.community_tokens: dict[int, set[str]] = defaultdict(set)
        self.file_neighbors: dict[str, set[str]] = defaultdict(set)

        id2file: dict[object, str] = {}
        for n in nodes:
            nid = n.get("id")
            sf = n.get("source_file", "")
            if (
                nid is not None and sf
                and not any(p in sf for p in skip_patterns)
                and _is_rankable(sf)
            ):
                id2file[nid] = sf

        links = graph.get("links")
        if links is None:
            links = graph.get("edges", [])
        for e in links or []:
            s = id2file.get(e.get("source"))
            t = id2file.get(e.get("target"))
            if s and t and s != t:
                self.file_neighbors[s].add(t)
                self.file_neighbors[t].add(s)

        for n in nodes:
            sf = n.get("source_file", "")
            label = n.get("norm_label") or n.get("label") or ""
            cid = n.get("community")
            tokens = _tokenize(label)

            if cid is not None:
                self.community_tokens[cid].update(tokens)

            if not sf or any(p in sf for p in skip_patterns) or not _is_rankable(sf):
                continue

            weight = self._TYPE_WEIGHT.get(n.get("file_type", "code"), 1)
            self.file_tokens[sf].extend(tokens * weight)
            if cid is not None:
                self.file_communities[sf].add(cid)

        for sf in list(self.file_tokens.keys()):
            stem_path = os.path.splitext(sf)[0]
            base_tokens = _tokenize(os.path.basename(stem_path))
            dir_tokens = _tokenize(os.path.dirname(stem_path))
            self.file_tokens[sf].extend(base_tokens * 6)
            self.file_tokens[sf].extend(dir_tokens * 2)

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
        keep_ratio: float = 0.3,
        edge_boost_weight: float = 0.4,
    ) -> list[RankedFile]:
        qtoks = _tokenize(prompt)
        if not qtoks:
            return []
        base = {
            sf: self._bm25(qtoks, sf, bm25_k1, bm25_b)
            + self._community_boost(qtoks, sf, community_boost_weight)
            for sf in self.file_tokens
        }
        if edge_boost_weight > 0 and self.file_neighbors:
            scores = {
                sf: sc + edge_boost_weight * max(
                    (base.get(n, 0.0) for n in self.file_neighbors.get(sf, ())),
                    default=0.0,
                )
                for sf, sc in base.items()
            }
        else:
            scores = base
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        if not ranked or ranked[0][1] <= 0:
            return []
        floor = ranked[0][1] * keep_ratio
        return [
            RankedFile(source_file=sf, score=sc)
            for sf, sc in ranked[:top_k]
            if sc > 0 and sc >= floor
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
    keep_ratio: float = 0.3,
    edge_boost_weight: float = 0.4,
) -> list[RankedFile]:
    return index.rank(
        prompt, top_k, community_boost_weight, bm25_k1, bm25_b, keep_ratio, edge_boost_weight
    )
