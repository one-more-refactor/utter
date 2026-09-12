"""Speech recognition backends.

Two shapes, and the difference matters more than the model choice:

  whisper-server  keeps the model resident and answers over HTTP. Measured at
                  190-228 ms for an 11 s utterance on an RX 9070 XT (Vulkan).
  parakeet-cli    more accurate on English, but reloads the model on every
                  invocation, which costs 700-1000 ms. No resident server exists
                  for it yet -- whisper-server cannot load Parakeet weights.

Model load is the dominant cost in naive dictation scripts. That is the whole
reason the default backend is a resident server.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .config import Config


class AsrError(RuntimeError):
    pass


def _env_for(cfg: Config) -> dict[str, str]:
    env = os.environ.copy()
    if cfg.asr.ld_library_path:
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = (
            f"{cfg.asr.ld_library_path}:{existing}" if existing else cfg.asr.ld_library_path
        )
    return env


class WhisperServerBackend:
    """Supervises a resident whisper-server and talks to it over HTTP."""

    name = "whisper-server"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.proc: subprocess.Popen[bytes] | None = None
        self.port = cfg.asr.port

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _port_open(self) -> bool:
        with socket.socket() as s:
            s.settimeout(0.2)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def start(self, timeout: float = 60.0) -> None:
        if self._port_open():
            return  # someone already serves this port; reuse it
        model = str(Path(self.cfg.asr.model).expanduser())
        cmd = [
            self.cfg.asr.whisper_server_bin,
            "-m", model,
            "--port", str(self.port),
            "--host", "127.0.0.1",
            # Greedy decoding: beam search buys little on short dictation and costs latency.
            "-bo", "1", "-bs", "1",
        ]
        if self.cfg.asr.threads:
            cmd += ["-t", str(self.cfg.asr.threads)]
        if self.cfg.asr.language:
            cmd += ["-l", self.cfg.asr.language]

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_env_for(self.cfg),
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AsrError(
                    f"whisper-server exited with code {self.proc.returncode}. "
                    "If the model is a Parakeet file, set asr.backend = \"parakeet-cli\" "
                    "-- whisper-server cannot load Parakeet weights."
                )
            if self._port_open():
                return
            time.sleep(0.15)
        raise AsrError(f"whisper-server did not come up within {timeout:.0f}s")

    def stop(self) -> None:
        if not self.proc:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None

    def transcribe(self, wav: Path) -> str:
        fields = {"response_format": "text", "temperature": "0.0"}
        if self.cfg.asr.language:
            fields["language"] = self.cfg.asr.language
        if self.cfg.asr.initial_prompt:
            fields["prompt"] = self.cfg.asr.initial_prompt
        body, content_type = _multipart(fields, wav)
        req = urllib.request.Request(
            self._url("/inference"), data=body, headers={"Content-Type": content_type}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = resp.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise AsrError(f"whisper-server request failed: {exc}") from exc
        return _maybe_json_text(payload)


class ParakeetCliBackend:
    """Runs parakeet-cli once per utterance. Accurate, but pays model load each time."""

    name = "parakeet-cli"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def start(self, timeout: float = 0.0) -> None:
        binary = self.cfg.asr.parakeet_cli_bin
        if not (Path(binary).is_file() or shutil.which(binary)):
            raise AsrError(f"{binary} not found")

    def stop(self) -> None:
        return None

    def transcribe(self, wav: Path) -> str:
        model = str(Path(self.cfg.asr.parakeet_model).expanduser())
        cmd = [self.cfg.asr.parakeet_cli_bin, "-m", model, "-f", str(wav), "-np"]
        if self.cfg.asr.threads:
            cmd += ["-t", str(self.cfg.asr.threads)]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=_env_for(self.cfg), timeout=180
        )
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
            raise AsrError(f"parakeet-cli failed: {tail[0]}")
        return proc.stdout.strip()


def build(cfg: Config):
    if cfg.asr.backend == "whisper-server":
        return WhisperServerBackend(cfg)
    if cfg.asr.backend == "parakeet-cli":
        return ParakeetCliBackend(cfg)
    raise AsrError(f"unknown asr.backend: {cfg.asr.backend!r}")


def _multipart(fields: dict[str, str], wav: Path) -> tuple[bytes, str]:
    boundary = f"----utter{uuid.uuid4().hex}"
    out = bytearray()
    for key, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode()
    out += f"--{boundary}\r\n".encode()
    out += (
        f'Content-Disposition: form-data; name="file"; filename="{wav.name}"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()
    out += wav.read_bytes()
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _maybe_json_text(payload: str) -> str:
    """whisper-server honours response_format=text, but returns JSON on some builds."""
    text = payload.strip()
    if text.startswith("{"):
        try:
            text = json.loads(text).get("text", text)
        except json.JSONDecodeError:
            pass
    return text.strip()
