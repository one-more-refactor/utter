"""Recognition backends, text injection, capture and cues -- against stub tools."""

import struct
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from utter import asr, audio, inject, sound
from utter.config import Config


# -- asr ------------------------------------------------------------------------------


@pytest.mark.parametrize("secs, ctx", [(0.5, 768), (1.0, 768), (20.0, 1024), (28.0, 1408),
                                       (29.0, 0)])
def test_audio_ctx_scales_with_duration(secs, ctx):
    assert asr.audio_ctx_for(secs) == ctx


def test_server_reply_may_be_text_or_json():
    assert asr._maybe_json_text(" hello\n") == "hello"
    assert asr._maybe_json_text('{"text": " hello "}') == "hello"
    assert asr._maybe_json_text("{not json") == "{not json"


def test_unknown_backend_is_an_error():
    cfg = Config()
    cfg.asr.backend = "cloud"
    with pytest.raises(asr.AsrError):
        asr.build(cfg)


def _wav(path, seconds=0.1, rate=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


class _Whisper(BaseHTTPRequestHandler):
    bodies: list = []

    def do_POST(self):  # noqa: N802
        type(self).bodies.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
        out = b" And so, my fellow Americans.\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, format, *args):  # noqa: A002
        pass


def test_whisper_server_backend_posts_multipart(tmp_path):
    _Whisper.bodies = []
    server = HTTPServer(("127.0.0.1", 0), _Whisper)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cfg = Config()
        cfg.asr.port = server.server_port
        backend = asr.WhisperServerBackend(cfg)
        backend.start()  # port already served: reuse, do not spawn
        assert backend.proc is None
        text = backend.transcribe(_wav(tmp_path / "a.wav"), audio_ctx=768)
    finally:
        server.shutdown()
        server.server_close()
    assert text == "And so, my fellow Americans."
    path, body = _Whisper.bodies[0]
    assert path == "/inference"
    assert b'name="audio_ctx"\r\n\r\n768' in body
    assert b'name="language"\r\n\r\nen' in body
    assert b'name="file"; filename="a.wav"' in body
    assert b"RIFF" in body


def test_whisper_server_that_dies_explains_itself(stubs, tmp_path):
    stubs.add("whisper-server", "exit 3")
    cfg = Config()
    cfg.asr.port = 1  # nothing listens there
    with pytest.raises(asr.AsrError, match="exited with code 3"):
        asr.WhisperServerBackend(cfg).start(timeout=5)


def test_parakeet_cli_backend(stubs, tmp_path):
    stubs.add("parakeet-cli", 'echo "  ask not  "')
    cfg = Config()
    cfg.asr.backend = "parakeet-cli"
    backend = asr.build(cfg)
    backend.start()
    assert backend.transcribe(_wav(tmp_path / "b.wav")) == "ask not"
    argv = stubs.calls("parakeet-cli")[0]
    assert argv[argv.index("-f") + 1].endswith("b.wav")


def test_parakeet_cli_failure_is_reported(stubs, tmp_path):
    stubs.add("parakeet-cli", 'echo "model file broken" >&2; exit 1')
    cfg = Config()
    cfg.asr.backend = "parakeet-cli"
    with pytest.raises(asr.AsrError, match="model file broken"):
        asr.build(cfg).transcribe(_wav(tmp_path / "c.wav"))


# -- inject ---------------------------------------------------------------------------


def test_type_text_guards_a_leading_dash(stubs):
    stubs.add("wtype")
    inject.type_text("-rf is a flag", delay_ms=3)
    assert stubs.calls("wtype") == [["-d", "3", "--", "-rf is a flag"]]


def test_backspace(stubs):
    stubs.add("wtype")
    inject.backspace(2)
    inject.backspace(0)
    assert stubs.calls("wtype") == [["-d", "1", "-k", "BackSpace", "-k", "BackSpace"]]


def test_replace_text_keeps_the_shared_prefix(stubs):
    stubs.add("wtype")
    inject.replace_text("hello wrold", "hello world")
    calls = stubs.calls("wtype")
    assert calls[0].count("BackSpace") == 4
    assert calls[1][-1] == "orld"


def test_failing_wtype_falls_back_to_the_clipboard(stubs):
    stubs.add("wtype", "echo 'compositor does not support virtual keyboard' >&2; exit 1")
    stubs.add("wl-copy")
    with pytest.raises(inject.InjectError, match="virtual keyboard"):
        inject.deliver("hello", mode="type")
    assert stubs.calls("wl-copy") == [["--", "hello"]]


def test_missing_wtype_falls_back_to_the_clipboard(only_stubs):
    only_stubs.add("wl-copy")
    with pytest.raises(inject.InjectError, match="wtype not found"):
        inject.deliver("hello")
    assert only_stubs.calls("wl-copy") == [["--", "hello"]]


def test_clipboard_mode_never_types(stubs):
    stubs.add("wtype")
    stubs.add("wl-copy")
    inject.deliver("hello", mode="clipboard")
    assert stubs.calls("wtype") == []
    assert stubs.calls("wl-copy") == [["--", "hello"]]


# -- audio ----------------------------------------------------------------------------


def test_peak():
    assert audio._peak(b"") == 0.0
    assert audio._peak(struct.pack("<hh", 0, -32768)) == 1.0
    assert audio._peak(struct.pack("<h", 16384)) == 0.5


def test_recorder_captures_from_pw_record(stubs):
    # Exactly 16 capture chunks (1.024 s of 16 kHz s16 silence), then stay open like a
    # live microphone does.
    stubs.add("pw-record", "head -c 32768 /dev/zero\nexec sleep 30")
    levels = []
    rec = audio.Recorder(source="my_mic", on_level=levels.append)
    rec.start()
    assert rec.error is None
    deadline = time.monotonic() + 5
    while rec.duration < 1.0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert rec.snapshot()  # partials can read while capture continues
    path, duration = rec.stop_to_wav()
    try:
        assert duration == pytest.approx(1.024)
        with wave.open(str(path)) as w:
            assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (16000, 1, 16384)
    finally:
        path.unlink()
    assert levels and max(levels) == 0.0
    argv = stubs.calls("pw-record")[0]
    assert argv[argv.index("--target") + 1] == "my_mic"
    assert argv[-1] == "-"


def test_recorder_reports_missing_pw_record(only_stubs):
    rec = audio.Recorder()
    rec.start()
    assert rec.error == "pw-record not found"


def test_list_sources_reads_pw_dump(stubs):
    stubs.add(
        "pw-dump",
        """cat <<'EOF'
[{"info": {"props": {"media.class": "Audio/Source", "node.name": "alsa_input.usb-mic",
   "object.serial": 42, "node.description": "USB Mic"}}},
 {"info": {"props": {"media.class": "Audio/Sink", "node.name": "speakers"}}},
 {"type": "PipeWire:Interface:Client"}]
EOF""",
    )
    assert audio.list_sources() == [("alsa_input.usb-mic", "42", "USB Mic")]


# -- sound ----------------------------------------------------------------------------


def test_cues_are_generated_once(tmp_path, monkeypatch):
    monkeypatch.setattr(sound, "CACHE_HOME", tmp_path)
    paths = sound.ensure_cues(volume=0.25)
    assert set(paths) == {"start", "stop", "error"}
    for path in paths.values():
        with wave.open(str(path)) as w:
            assert w.getframerate() == sound.RATE
            assert w.getnframes() > 0
    mtime = paths["start"].stat().st_mtime_ns
    sound.ensure_cues()
    assert paths["start"].stat().st_mtime_ns == mtime


def test_cues_play_through_paplay(tmp_path, monkeypatch, stubs):
    monkeypatch.setattr(sound, "CACHE_HOME", tmp_path)
    stubs.add("paplay")
    cues = sound.Cues(enabled=True)
    cues.play("start")
    deadline = time.monotonic() + 5
    while not stubs.calls("paplay") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert stubs.calls("paplay")[0][0].endswith("start.wav")
