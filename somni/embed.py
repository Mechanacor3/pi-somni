"""Optional meaning-based search over the transcript, from an OpenAI-compatible embeddings endpoint.

Off unless SOMNI_EMBEDDINGS_URL is set (for example a local llama.cpp `llama-server --embeddings` serving a small BERT
model such as bge-small-en-v1.5). `somni_search` then fuses two rankings: BM25 over words, which finds exact items
(URLs, IDs, names), and similarity of meaning, which finds paraphrases ("milestone post" for "funding
announcement").

Vectors live in an SQLite cache beside the store, never in its hash chain. Entries are keyed by a hash of the model
and the text, so copies of a store can share one cache and a record's vector is computed once. Only the standard
library is used: urllib for the endpoint, sqlite3 for the cache.

Forgetting replaces a record's text with a stub, and search then embeds the stub. The forgotten text's vector stays
in the cache until `somni embed` prunes entries nothing current uses.
"""
from __future__ import annotations

import hashlib
import json
import math
import operator
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from array import array

CHUNK_CHARS = 1500  # about 400 tokens: inside a BERT model's 512
CHUNK_OVERLAP = 200
MAX_CHUNKS = 8  # per record; the start of a long tool output is where its subject is
BATCH = 32
TIMEOUT_SECONDS = 60
MIN_CHARS = 200  # below this a rejected text is an endpoint fault, not a length
CANDIDATES = 50  # the closest records that enter the fused ranking
_warned = set()


def settings():
    url = os.environ.get("SOMNI_EMBEDDINGS_URL", "").strip()
    if not url:
        return None
    return {"url": url, "model": os.environ.get("SOMNI_EMBEDDINGS_MODEL", "").strip(),
            "prefix": os.environ.get("SOMNI_EMBEDDINGS_QUERY_PREFIX", ""),
            "cache": os.environ.get("SOMNI_EMBEDDINGS_CACHE", "").strip()}


def chunks(text):
    text = text.strip()
    out, start = [], 0
    while start < len(text) and len(out) < MAX_CHUNKS:
        out.append(text[start:start + CHUNK_CHARS])
        start += CHUNK_CHARS - CHUNK_OVERLAP
    return out or [""]


def _unit(values):
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return array("f", (v / norm for v in values))


class Index:
    def __init__(self, conf, workspace):
        self.conf = conf
        path = conf["cache"] or os.path.join(str(workspace), "embeddings.sqlite")
        self.db = sqlite3.connect(path, timeout=120)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB NOT NULL)")
        self.memo = {}

    def key(self, text):
        return hashlib.sha256(f"{self.conf['model']}\0{text}".encode("utf-8")).hexdigest()

    def _request(self, texts):
        body = {"input": texts}
        if self.conf["model"]:
            body["model"] = self.conf["model"]
        request = urllib.request.Request(self.conf["url"], json.dumps(body).encode("utf-8"),
                                         {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.load(response)["data"]
        vectors = [None] * len(texts)
        for position, item in enumerate(data):
            vectors[item.get("index", position)] = _unit(item["embedding"])
        if any(v is None for v in vectors):
            raise ValueError("the embeddings endpoint returned fewer vectors than texts")
        return vectors

    def _fit(self, texts):
        """Vectors for `texts`, shortening any text the endpoint rejects. A chunk of dense text (URLs, IDs, code) can
        run past a small model's token limit; its start is kept, since a record's subject comes first. The cache
        still keys the vector by the whole chunk."""
        try:
            return self._request(texts)
        except urllib.error.HTTPError:
            if len(texts) > 1:
                return [self._fit([text])[0] for text in texts]
            if len(texts[0]) < MIN_CHARS:
                raise
            return self._fit([texts[0][:len(texts[0]) * 2 // 3]])

    def vectors(self, texts):
        keys = [self.key(t) for t in texts]
        unknown = [k for k in dict.fromkeys(keys) if k not in self.memo]
        for i in range(0, len(unknown), 500):
            part = unknown[i:i + 500]
            rows = self.db.execute(f"SELECT key, vector FROM vectors WHERE key IN ({','.join('?' * len(part))})", part)
            for key, blob in rows:
                vector = array("f")
                vector.frombytes(blob)
                self.memo[key] = vector
        todo = list({k: t for k, t in zip(keys, texts) if k not in self.memo}.items())
        for i in range(0, len(todo), BATCH):
            part = todo[i:i + BATCH]
            found = self._fit([t for _, t in part])
            with self.db:
                self.db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?)",
                                    [(k, v.tobytes()) for (k, _), v in zip(part, found)])
            self.memo.update((k, v) for (k, _), v in zip(part, found))
        return [self.memo[k] for k in keys]

    def prune(self, keep):
        """Delete cached vectors whose key isn't in `keep`: what forgotten or replaced text left behind."""
        keep = set(keep)
        stale = [k for (k,) in self.db.execute("SELECT key FROM vectors") if k not in keep]
        with self.db:
            self.db.executemany("DELETE FROM vectors WHERE key = ?", [(k,) for k in stale])
        return len(stale)


def _record_chunks(records, text_of):
    pieces, owners = [], []
    for record in records:
        for piece in chunks(text_of(record)):
            pieces.append(piece)
            owners.append(record["seq"])
    return pieces, owners


def rank(memory, records, text_of, query):
    """{seq: position} for the CANDIDATES records closest in meaning to `query`, or None when embeddings are off or
    the endpoint fails (search then ranks words alone and says why once on stderr)."""
    conf = settings()
    if conf is None or not records:
        return None
    try:
        index = memory.__dict__.get("_embeddings")
        if index is None or index.conf != conf:
            index = memory.__dict__["_embeddings"] = Index(conf, memory.workspace)
        pieces, owners = _record_chunks(records, text_of)
        vectors = index.vectors(pieces)
        probe = index._request([conf["prefix"] + query])[0]
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        if conf["url"] not in _warned:
            _warned.add(conf["url"])
            print(f"somni: embeddings unavailable ({error}); search ranks words only", file=sys.stderr)
        return None
    best = {}
    for seq, vector in zip(owners, vectors):
        score = sum(map(operator.mul, probe, vector))
        if score > best.get(seq, -2.0):
            best[seq] = score
    closest = sorted(best, key=lambda s: (best[s], s), reverse=True)[:CANDIDATES]
    return {seq: position for position, seq in enumerate(closest)}


def warm(memory, text_of, prune=True):
    """Embed every record now (so the first search doesn't), and drop vectors nothing current uses."""
    conf = settings()
    if conf is None:
        raise ValueError("set SOMNI_EMBEDDINGS_URL first")
    index = Index(conf, memory.workspace)
    pieces, _ = _record_chunks(memory.transcript, text_of)
    index.vectors(pieces)
    removed = index.prune(index.key(p) for p in pieces) if prune else 0
    return {"records": len(memory.transcript), "chunks": len(pieces), "pruned": removed}
