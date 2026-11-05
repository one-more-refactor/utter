"""Loading and validating config.toml."""

import pytest

from utter.cli import EXAMPLE_CONFIG
from utter.config import Config, ConfigError


def _load(tmp_path, body: str) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return Config.load(path)


def test_missing_file_means_defaults(tmp_path):
    cfg = Config.load(tmp_path / "nope.toml")
    assert cfg.trigger.mode == "hold"
    assert cfg.trigger.key == "RIGHTCTRL"
    assert cfg.stream.enabled is True
    assert cfg.unknown == []
    assert cfg.validate() == []


def test_values_override_defaults(tmp_path):
    cfg = _load(
        tmp_path,
        '[trigger]\nmode = "chord"\nmodifiers = ["CTRL"]\nkey = "SPACE"\n'
        '[output.replacements]\n"see three do" = "cr3do"\n',
    )
    assert cfg.trigger.mode == "chord"
    assert cfg.trigger.modifiers == ["CTRL"]
    assert cfg.output.replacements == {"see three do": "cr3do"}
    assert cfg.validate() == []


def test_unknown_keys_are_reported_not_fatal(tmp_path):
    cfg = _load(tmp_path, "[ui]\noverlay = true\n[nonsense]\nx = 1\n")
    assert "ui.overlay" in cfg.unknown
    assert "[nonsense]" in cfg.unknown
    assert cfg.validate() == []


def test_invalid_toml_raises_a_readable_error(tmp_path):
    with pytest.raises(ConfigError, match="config.toml"):
        _load(tmp_path, "[trigger\nmode = ")


@pytest.mark.parametrize(
    "body, fragment",
    [
        ('[trigger]\nmode = "hodl"\n', "trigger.mode"),
        ('[output]\nmode = "paste"\n', "output.mode"),
        ('[cleanup]\nintensity = "max"\n', "cleanup.intensity"),
        ('[asr]\nbackend = "cloud"\n', "asr.backend"),
        ('[trigger]\nkey = "NOT_A_KEY"\n', "trigger.key"),
        ('[trigger]\nmode = "chord"\nmodifiers = ["HYPER"]\n', "HYPER"),
        ('[audio]\nsilence_ms = "long"\n', "audio.silence_ms"),
        ("[audio]\nauto_stop = 1\n", "audio.auto_stop"),
        ("[audio]\nsilence_level = 3.0\n", "silence_level"),
        ("[stream]\ninterval_ms = 5\n", "interval_ms"),
    ],
)
def test_validation_catches_mistakes(tmp_path, body, fragment):
    problems = _load(tmp_path, body).validate()
    assert any(fragment in p for p in problems), problems


def test_an_int_is_fine_where_a_float_is_expected(tmp_path):
    assert _load(tmp_path, "[audio]\nmax_duration_secs = 30\n").validate() == []


def test_raw_keycodes_are_accepted(tmp_path):
    assert _load(tmp_path, '[trigger]\nkey = "191"\n').validate() == []


def test_the_example_config_is_valid_and_fully_known(tmp_path):
    cfg = _load(tmp_path, EXAMPLE_CONFIG)
    assert cfg.unknown == []
    assert cfg.validate() == []


def test_check_reports_missing_tools_and_model(tmp_path, only_stubs):
    cfg = Config()
    cfg.asr.model = str(tmp_path / "missing.bin")
    problems = " | ".join(cfg.check())
    assert "model not found" in problems
    assert "whisper-server" in problems
    assert "pw-record" in problems
    assert "wtype" in problems


def test_check_passes_with_everything_present(tmp_path, only_stubs):
    for tool in ("whisper-server", "pw-record", "wtype"):
        only_stubs.add(tool)
    model = tmp_path / "model.bin"
    model.write_bytes(b"ggml")
    cfg = Config()
    cfg.asr.model = str(model)
    assert cfg.check() == []
