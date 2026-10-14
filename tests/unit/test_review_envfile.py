import os

from packages.common.envfile import load_env_defaults


def test_sets_variables_from_a_dotenv_file(tmp_path, monkeypatch):
    monkeypatch.delenv("FP_TEST_ALPHA", raising=False)
    monkeypatch.delenv("FP_TEST_BETA", raising=False)
    env = tmp_path / ".env"
    env.write_text('# comment\nFP_TEST_ALPHA=one\n\nFP_TEST_BETA="two words"\n', encoding="utf-8")
    loaded = load_env_defaults(env)
    assert os.environ["FP_TEST_ALPHA"] == "one"
    assert os.environ["FP_TEST_BETA"] == "two words"
    assert set(loaded) == {"FP_TEST_ALPHA", "FP_TEST_BETA"}


def test_existing_environment_wins_over_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FP_TEST_GAMMA", "from-shell")
    env = tmp_path / ".env"
    env.write_text("FP_TEST_GAMMA=from-file\n", encoding="utf-8")
    assert load_env_defaults(env) == []
    assert os.environ["FP_TEST_GAMMA"] == "from-shell"


def test_missing_file_is_not_an_error(tmp_path):
    assert load_env_defaults(tmp_path / "nope.env") == []


def test_values_may_contain_equals_signs(tmp_path, monkeypatch):
    monkeypatch.delenv("FP_TEST_DELTA", raising=False)
    env = tmp_path / ".env"
    env.write_text("FP_TEST_DELTA=abc=def==\n", encoding="utf-8")
    load_env_defaults(env)
    assert os.environ["FP_TEST_DELTA"] == "abc=def=="
