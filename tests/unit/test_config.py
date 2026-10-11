from __future__ import annotations

from pathlib import Path

import pytest

from yi_agent.config import ConfigError, default_dotenv_paths, load_settings


def test_defaults() -> None:
    s = load_settings(env={}, use_default_dotenv=False)
    assert s.anthropic_api_key is None
    assert s.es_url == "http://127.0.0.1:9200"
    assert s.data_dir == Path.home() / "yi-data"  # expanded, under the test's own HOME


def test_env_overrides_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("YI_ES_URL=http://from-dotenv:9200\nYI_DATA_DIR=/data/from-dotenv\n")
    s = load_settings(env={"YI_ES_URL": "http://from-env:9200/"}, dotenv_path=dotenv)
    assert s.es_url == "http://from-env:9200"  # env wins, trailing slash dropped
    assert s.data_dir == Path("/data/from-dotenv")  # dotenv fills what env does not set


def test_tilde_in_a_dotenv_path_is_expanded(tmp_path: Path) -> None:
    # .env.example ships YI_DATA_DIR=~/yi-data, and python-dotenv does not expand "~".
    dotenv = tmp_path / ".env"
    dotenv.write_text("YI_DATA_DIR=~/yi-data\n")
    s = load_settings(env={}, dotenv_path=dotenv)
    assert s.data_dir == Path.home() / "yi-data"
    assert "~" not in str(s.data_dir)


def test_the_shipped_env_example_loads(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / ".env.example"
    s = load_settings(env={}, dotenv_path=example)
    assert s.anthropic_api_key is None  # ANTHROPIC_API_KEY= (empty) means unset
    assert s.data_dir == Path.home() / "yi-data"


def test_unrelated_and_unknown_vars_are_ignored() -> None:
    # YI_ES_HEAP is for docker compose, not a Python setting
    s = load_settings(
        env={"YI_NOT_A_SETTING": "x", "YI_ES_HEAP": "4g", "PATH": "/bin"},
        use_default_dotenv=False,
    )
    assert s.es_url == "http://127.0.0.1:9200"


def test_api_key_read_from_dotenv_and_hidden_in_repr(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("ANTHROPIC_API_KEY=sk-test-123\n")
    s = load_settings(env={}, dotenv_path=dotenv)
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-test-123"
    assert "sk-test-123" not in repr(s)
    assert "sk-test-123" not in s.model_dump_json()


def test_prefixed_api_key_is_not_a_second_way_in() -> None:
    s = load_settings(env={"YI_ANTHROPIC_API_KEY": "k"}, use_default_dotenv=False)
    assert s.anthropic_api_key is None


def test_cwd_dotenv_overrides_home_dotenv(tmp_path: Path) -> None:
    home, local = tmp_path / "home.env", tmp_path / "local.env"
    home.write_text("ANTHROPIC_API_KEY=from-home\nYI_DATA_DIR=/home-data\n")
    local.write_text("YI_DATA_DIR=/local-data\n")
    s = load_settings(env={}, dotenv_path=[home, local])
    assert s.data_dir == Path("/local-data")
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "from-home"


def test_default_dotenv_paths_are_home_then_cwd() -> None:
    home, cwd = default_dotenv_paths()
    assert home == Path.home() / ".yi" / ".env" and cwd == Path(".env")


def test_default_load_reads_the_home_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    (Path.home() / ".yi").mkdir()
    (Path.home() / ".yi" / ".env").write_text("ANTHROPIC_API_KEY=from-home-file\n")
    monkeypatch.chdir(Path.home())  # no ./.env here
    key = load_settings(env={}).anthropic_api_key
    assert key is not None and key.get_secret_value() == "from-home-file"


@pytest.mark.parametrize(
    "data",
    [
        b"ANTHROPIC_API_KEY=k\r\n",
        "﻿ANTHROPIC_API_KEY=k\n".encode(),
        b'ANTHROPIC_API_KEY="k"\n',
    ],
)
def test_windows_style_dotenv_files_load(tmp_path: Path, data: bytes) -> None:
    p = tmp_path / ".env"
    p.write_bytes(data)
    key = load_settings(env={}, dotenv_path=p).anthropic_api_key
    assert key is not None and key.get_secret_value() == "k"


def test_utf16_dotenv_is_a_clear_config_error(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_bytes("ANTHROPIC_API_KEY=k\r\n".encode("utf-16"))
    with pytest.raises(ConfigError, match="UTF-8"):
        load_settings(env={}, dotenv_path=p)
