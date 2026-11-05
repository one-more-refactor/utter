"""Deterministic cleanup and the optional LLM pass (against a fake endpoint)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from utter import text
from utter.config import Config


@pytest.mark.parametrize(
    "raw, want",
    [
        ("[BLANK_AUDIO]", ""),
        ("Thank you.", ""),
        ("your country can\n do for you", "your country can do for you"),
        ("First.\n\nSecond.", "First.\n\nSecond."),
        ("hello , world .", "hello, world."),
        ("- hello", "hello"),
        ("Ask not. Ask not.", "Ask not."),
        ("  plenty   of   space  ", "plenty of space"),
    ],
)
def test_tidy(raw, want):
    assert text.tidy(raw) == want


@pytest.mark.parametrize(
    "raw, want",
    [
        ("Um, so I think", "So I think"),
        (" uh we go", " we go"),
        ("just just get it", "just get it"),
        ("an umbrella", "an umbrella"),
        ("the error", "the error"),
        ("", ""),
    ],
)
def test_live_clean(raw, want):
    assert text.live_clean(raw) == want


def test_replacements_are_whole_word_and_case_insensitive():
    rules = {"see three do": "cr3do"}
    assert text.apply_replacements("See three do rocks", rules) == "cr3do rocks"
    assert text.apply_replacements("seethree do", rules) == "seethree do"


class _Ollama(BaseHTTPRequestHandler):
    reply = ""
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(body)
        out = json.dumps({"message": {"content": type(self).reply}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, format, *args):  # noqa: A002
        pass


@pytest.fixture
def ollama():
    _Ollama.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _cfg(endpoint: str) -> Config:
    cfg = Config()
    cfg.cleanup.enabled = True
    cfg.cleanup.endpoint = endpoint
    cfg.cleanup.timeout_secs = 5
    cfg.cleanup.vocabulary = ["Proxmox"]
    return cfg


def test_llm_pass_strips_think_and_sends_vocabulary(ollama):
    _Ollama.reply = "<think>hmm</think>I deploy on Proxmox."
    url = f"http://127.0.0.1:{ollama.server_port}/api/chat"
    assert text.clean_with_llm("um I deploy on prox mox", _cfg(url)) == "I deploy on Proxmox."
    sent = _Ollama.seen[0]
    assert sent["think"] is False
    assert "Proxmox" in sent["messages"][0]["content"]
    assert "<transcript>" in sent["messages"][1]["content"]


def test_llm_pass_rejects_runaway_output(ollama):
    _Ollama.reply = "word " * 500
    url = f"http://127.0.0.1:{ollama.server_port}/api/chat"
    assert text.clean_with_llm("short", _cfg(url)) == "short"


def test_llm_pass_keeps_text_when_endpoint_is_down():
    assert text.clean_with_llm("keep me", _cfg("http://127.0.0.1:9/api/chat")) == "keep me"


def test_llm_pass_is_skipped_when_disabled():
    cfg = Config()
    assert text.clean_with_llm("as is", cfg) == "as is"
    assert text.warm(cfg) is False
