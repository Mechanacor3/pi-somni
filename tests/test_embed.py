"""Meaning-based search: an OpenAI-compatible embeddings endpoint, an SQLite cache, and the fused ranking."""
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from somni import embed as E  # noqa: E402
from somni import store as S  # noqa: E402

# A toy model: each text becomes counts over a few "meanings", so paraphrases share a direction.
MEANINGS = {"funding": "money", "raise": "money", "investment": "money", "announcement": "news", "post": "news",
            "milestone": "news", "weather": "sky", "sunny": "sky", "oakland": "place"}
AXES = ["money", "news", "sky", "place"]


def toy_vector(text):
    words = [w.strip(".,:;?!").lower() for w in text.split()]
    return [sum(MEANINGS.get(w) == axis for w in words) + 0.01 for axis in AXES]


class Endpoint(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Endpoint.requests.append(body)
        data = [{"index": i, "embedding": toy_vector(t)} for i, t in enumerate(body["input"])]
        out = json.dumps({"data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


class EmbedSearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Endpoint)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/v1/embeddings"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = pathlib.Path(self.tmp.name)
        self.memory = S.SomniMemory.create(self.workspace, "subject", "lineage", "attempt")
        self.memory.set_phase("open")
        for text in ("Sarah: no investment news until the board signs off.",  # a paraphrase: no query word appears
                     "Weather in Oakland is sunny.",
                     "Lunch order: two bagels."):
            self.memory.record("agent_output", text)
        Endpoint.requests.clear()
        self.env = mock.patch.dict(os.environ, {"SOMNI_EMBEDDINGS_URL": self.url, "SOMNI_EMBEDDINGS_MODEL": "toy",
                                                "SOMNI_EMBEDDINGS_QUERY_PREFIX": "query: "})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def ids(self, query, limit=3):
        return [r["id"] for r in self.memory.search(query, limit)]

    def test_meaning_finds_a_paraphrase_that_words_miss(self):
        with mock.patch.dict(os.environ, {"SOMNI_EMBEDDINGS_URL": ""}):
            self.assertEqual(self.ids("funding raise milestone post"), [])
        self.assertEqual(self.ids("funding raise milestone post")[0], "t000001")
        self.assertTrue(any(body["input"] == ["query: funding raise milestone post"] for body in Endpoint.requests))
        self.assertTrue(all(body["model"] == "toy" for body in Endpoint.requests))

    def test_an_exact_phrase_still_comes_first_and_quotes_skip_meaning(self):
        self.assertEqual(self.ids("two bagels")[0], "t000003")
        Endpoint.requests.clear()
        self.assertEqual(self.ids('"two bagels"'), ["t000003"])
        self.assertEqual(Endpoint.requests, [])

    def test_vectors_are_cached_beside_the_store_and_reused(self):
        self.ids("funding")
        self.assertTrue((self.workspace / "embeddings.sqlite").is_file())
        Endpoint.requests.clear()
        fresh = S.SomniMemory(self.workspace, "subject").load()  # a new process: the cache, not the endpoint
        fresh.search("sunny weather", 3)
        self.assertEqual([body["input"] for body in Endpoint.requests], [["query: sunny weather"]])

    def test_a_dead_endpoint_falls_back_to_words(self):
        with mock.patch.dict(os.environ, {"SOMNI_EMBEDDINGS_URL": "http://127.0.0.1:9/v1/embeddings"}), \
                mock.patch("sys.stderr") as err:
            self.assertEqual(self.ids("sunny")[0], "t000002")
        self.assertIn("search ranks words only", "".join(c.args[0] for c in err.write.call_args_list))

    def test_warm_embeds_every_record_and_prunes_what_nothing_uses(self):
        index = E.Index(E.settings(), self.workspace)
        index.vectors(["text of a record that was since forgotten"])
        text_of = lambda r: r["text"]  # noqa: E731
        report = E.warm(self.memory.load(), text_of)
        self.assertEqual((report["records"], report["chunks"], report["pruned"]), (3, 3, 1))

    def test_long_records_are_embedded_in_overlapping_chunks(self):
        pieces = E.chunks("x" * (E.CHUNK_CHARS * 2))
        self.assertEqual(len(pieces), 3)
        self.assertTrue(all(len(p) <= E.CHUNK_CHARS for p in pieces))
        self.assertEqual(len(E.chunks("y" * E.CHUNK_CHARS * 20)), E.MAX_CHUNKS)


if __name__ == "__main__":
    unittest.main()
