"""The rename from ``evalharness`` to Nimbus breaks nothing already set up.

Old environment variables are still read (after the new ones), a login saved
under the old config directory is picked up and moved, and a local history in
the old database file keeps being used until there is a new one.
"""

import json
import stat

import pytest

from nimbus.cli import remote
from nimbus.config import DEFAULT_DB_PATH, LEGACY_DB_PATH, Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in (
        "NIMBUS_AWS_REGION",
        "EVALHARNESS_AWS_REGION",
        "NIMBUS_DB_PATH",
        "EVALHARNESS_DB_PATH",
        "NIMBUS_CONFIG_DIR",
        "EVALHARNESS_CONFIG_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    # Settings reads ./.env and resolves ./data relative to the working directory.
    monkeypatch.chdir(tmp_path)


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


def test_an_old_variable_is_still_read(monkeypatch):
    monkeypatch.setenv("EVALHARNESS_AWS_REGION", "eu-west-1")

    assert Settings().aws_region == "eu-west-1"


def test_the_new_variable_wins_over_the_old(monkeypatch):
    monkeypatch.setenv("EVALHARNESS_AWS_REGION", "eu-west-1")
    monkeypatch.setenv("NIMBUS_AWS_REGION", "us-west-2")

    assert Settings().aws_region == "us-west-2"


def test_an_old_variable_in_a_dotenv_file_is_still_read(tmp_path):
    (tmp_path / ".env").write_text("EVALHARNESS_AWS_REGION=ap-south-1\n")

    assert Settings().aws_region == "ap-south-1"


def test_a_new_dotenv_variable_wins_over_an_old_one(tmp_path):
    (tmp_path / ".env").write_text(
        "EVALHARNESS_AWS_REGION=ap-south-1\nNIMBUS_AWS_REGION=sa-east-1\n"
    )

    assert Settings().aws_region == "sa-east-1"


@pytest.mark.parametrize(
    ("field", "old", "new", "standard"),
    [
        (
            "anthropic_api_key",
            "EVALHARNESS_ANTHROPIC_API_KEY",
            "NIMBUS_ANTHROPIC_API_KEY",
            "ANTHROPIC_API_KEY",
        ),
        ("openai_api_key", "EVALHARNESS_OPENAI_API_KEY", "NIMBUS_OPENAI_API_KEY", "OPENAI_API_KEY"),
    ],
)
def test_old_provider_key_names_rank_between_the_new_and_the_standard_ones(
    monkeypatch, field, old, new, standard
):
    monkeypatch.setenv(standard, "standard")
    monkeypatch.setenv(old, "old")
    assert getattr(Settings(), field) == "old"

    monkeypatch.setenv(new, "new")
    assert getattr(Settings(), field) == "new"


def test_the_old_ollama_name_is_still_read(monkeypatch):
    monkeypatch.setenv("EVALHARNESS_OLLAMA_BASE_URL", "localhost:11434")

    assert Settings().ollama_base_url == "http://localhost:11434"


# --------------------------------------------------------------------------- #
# Local history
# --------------------------------------------------------------------------- #


def test_the_default_database_is_the_new_file():
    assert Settings().db_path == DEFAULT_DB_PATH


def test_an_existing_old_database_keeps_being_used(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "evalharness.db").write_bytes(b"")

    assert Settings().db_path == LEGACY_DB_PATH


def test_the_new_database_wins_once_it_exists(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "evalharness.db").write_bytes(b"")
    (tmp_path / "data" / "nimbus.db").write_bytes(b"")

    assert Settings().db_path == DEFAULT_DB_PATH


def test_an_explicit_database_path_is_taken_as_given(monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "evalharness.db").write_bytes(b"")
    monkeypatch.setenv("NIMBUS_DB_PATH", "./data/nimbus.db")

    assert Settings().db_path == "./data/nimbus.db"
    assert Settings(db_path="/elsewhere.db").db_path == "/elsewhere.db"


# --------------------------------------------------------------------------- #
# The saved login
# --------------------------------------------------------------------------- #


def saved(path, url="https://harness.example.com"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "email": "me@example.com"}))
    return path


def test_a_login_saved_before_the_rename_is_used_and_moved(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    old = saved(tmp_path / "xdg" / "evalharness" / "login.json")

    login = remote.load_login()

    assert login is not None
    assert login.url == "https://harness.example.com"
    assert not old.exists()
    new = tmp_path / "xdg" / "nimbus" / "login.json"
    assert stat.S_IMODE(new.stat().st_mode) == 0o600
    # Moved, so forgetting it forgets it everywhere.
    assert remote.clear_login() is True
    assert remote.load_login() is None


def test_a_current_login_wins_over_an_old_one(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    old = saved(tmp_path / "xdg" / "evalharness" / "login.json", url="https://old.example.com")
    saved(tmp_path / "xdg" / "nimbus" / "login.json", url="https://new.example.com")

    assert remote.load_login().url == "https://new.example.com"
    assert old.exists()


def test_an_unreadable_old_login_is_left_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    old = tmp_path / "xdg" / "evalharness" / "login.json"
    old.parent.mkdir(parents=True)
    old.write_text("not json")

    assert remote.load_login() is None
    assert old.exists()
    assert not (tmp_path / "xdg" / "nimbus" / "login.json").exists()


def test_the_old_config_dir_variable_is_still_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("EVALHARNESS_CONFIG_DIR", str(tmp_path / "custom"))
    saved(tmp_path / "custom" / "login.json")

    assert remote.config_dir() == tmp_path / "custom"
    assert remote.load_login() is not None

    monkeypatch.setenv("NIMBUS_CONFIG_DIR", str(tmp_path / "newer"))
    assert remote.config_dir() == tmp_path / "newer"


def test_an_explicit_config_dir_never_reaches_outside_itself(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    old = saved(tmp_path / "xdg" / "evalharness" / "login.json")
    monkeypatch.setenv("NIMBUS_CONFIG_DIR", str(tmp_path / "explicit"))

    assert remote.load_login() is None
    assert old.exists()
