import os

from wizz_finder.envfile import load_env, mask, read_env, set_env_value


def test_set_env_value_creates_file(tmp_path):
    env = tmp_path / ".env"
    set_env_value("WIZZ_SUBSCRIPTION_ID", "abc", env)
    assert env.read_text() == "WIZZ_SUBSCRIPTION_ID=abc\n"


def test_set_env_value_replaces_in_place_and_keeps_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# a comment\nWIZZ_EMAIL=me@example.com\nWIZZ_SUBSCRIPTION_ID=old\n")
    set_env_value("WIZZ_SUBSCRIPTION_ID", "new", env)
    lines = env.read_text().splitlines()
    assert lines == ["# a comment", "WIZZ_EMAIL=me@example.com", "WIZZ_SUBSCRIPTION_ID=new"]


def test_set_env_value_appends_missing_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("WIZZ_EMAIL=me@example.com\n")
    set_env_value("WIZZ_SUBSCRIPTION_ID", "new", env)
    assert read_env(env) == {"WIZZ_EMAIL": "me@example.com", "WIZZ_SUBSCRIPTION_ID": "new"}


def test_read_env_strips_quotes_and_skips_junk(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# c\n\nWIZZ_PASSWORD="s3cret"\nnonsense\nWIZZ_EMAIL=me@example.com\n')
    assert read_env(env) == {"WIZZ_PASSWORD": "s3cret", "WIZZ_EMAIL": "me@example.com"}


def test_load_env_does_not_override_real_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("WIZZ_SUBSCRIPTION_ID=from-file\n")
    monkeypatch.setenv("WIZZ_SUBSCRIPTION_ID", "from-shell")
    load_env(env)
    assert os.environ["WIZZ_SUBSCRIPTION_ID"] == "from-shell"


def test_mask_hides_the_middle():
    masked = mask("9da7635e-870f-45ff-8105-3c66635b08ea")
    assert masked.startswith("9da7635e") and masked.endswith("08ea")
    assert "870f" not in masked
