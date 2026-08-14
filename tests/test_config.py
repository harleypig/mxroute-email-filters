"""Setting resolution, and the credential that must never render itself.

The ``Secret`` tests are the hard boundary of this repo. A password that
reaches stdout, a log, or a traceback is compromised, and the defence is
structural rather than careful: every path that could print it goes through
``__str__`` or ``__repr__``, so both are overridden. These tests exercise
each of those paths by name, because "we are careful" is not a testable
property and "``repr`` returns ``<redacted>``" is.

The literal below is not a credential; it is a marker string, and every
assertion is that it is *absent* from some rendering.
"""

import argparse

import pytest

from mxfilter import MxFilterError
from mxfilter.config import (
    DEFAULT_IMAP_PORT,
    DEFAULT_SIEVE_PORT,
    DEFAULT_SIEVE_TLS,
    Config,
    Secret,
    config_path,
    load_config,
    read_config_file,
    run_password_command,
)

MARKER = "s3cret-marker-value"

# ############################################################################
# Secret
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "render",
    [
        pytest.param(str, id="str"),
        pytest.param(repr, id="repr"),
        pytest.param(lambda s: f"{s}", id="f-string"),
        pytest.param(lambda s: f"{s!r}", id="f-string-repr"),
        pytest.param("{}".format, id="format"),
        # The %-forms are the point of this test, not an oversight: an
        # old-style log line is one of the paths a credential escapes by.
        pytest.param(lambda s: "%s" % (s,), id="percent-s"),  # noqa: UP031
        pytest.param(lambda s: "%r" % (s,), id="percent-r"),  # noqa: UP031
        pytest.param(lambda s: ", ".join([str(s)]), id="join"),
        pytest.param(lambda s: repr([s]), id="inside-a-list"),
        pytest.param(lambda s: repr({"pw": s}), id="inside-a-dict"),
    ],
)
def test_a_secret_never_renders_its_value(render):
    """Every accidental disclosure path, one parameter each.

    The container cases matter as much as the direct ones: a ``Secret``
    inside a dict that lands in a traceback frame is rendered with
    ``repr``, which is exactly how a credential ends up in a bug report.
    """
    rendered = render(Secret(MARKER))

    assert MARKER not in rendered
    assert "redacted" in rendered


# ----------------------------------------------------------------------------
def test_reveal_is_the_only_way_out():
    """Greppable by design: ``reveal()`` marks every real disclosure site."""
    assert Secret(MARKER).reveal() == MARKER


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "truthy"),
    [
        pytest.param(MARKER, True, id="set"),
        pytest.param("", False, id="empty"),
    ],
)
def test_a_secret_is_falsy_when_empty(value, truthy):
    """An empty credential has to be detectable without unwrapping it."""
    assert bool(Secret(value)) is truthy


# ----------------------------------------------------------------------------
def test_a_config_repr_reports_the_credential_state_not_the_credential():
    """A Config lands in tracebacks; the dataclass repr would print it."""
    config = Config(host="mail.example.com", user="me@example.com")
    config._password = Secret(MARKER)

    rendered = repr(config)

    assert MARKER not in rendered
    assert "password=set" in rendered
    assert "mail.example.com" in rendered


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("env", "password_cmd", "expected"),
    [
        pytest.param({}, "", "unset", id="nothing-configured"),
        pytest.param({"MXROUTE_PASSWORD": MARKER}, "", "set", id="from-env"),
        pytest.param(
            {"MXROUTE_PASSWORD_CMD": "echo x"},
            "",
            "set (via command)",
            id="from-env-command",
        ),
        pytest.param({}, "echo x", "set (via command)", id="from-config-cmd"),
    ],
)
def test_password_state_is_a_literal_never_a_value(
    monkeypatch, env, password_cmd, expected
):
    """The discriminator the error messages are allowed to print."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    config = Config(password_cmd=password_cmd)

    assert config.password_state() == expected


# ############################################################################
# Password resolution
# ############################################################################


# ----------------------------------------------------------------------------
def test_the_environment_supplies_the_password(monkeypatch):
    monkeypatch.setenv("MXROUTE_PASSWORD", MARKER)

    assert Config().password().reveal() == MARKER


# ----------------------------------------------------------------------------
def test_a_password_command_beats_the_prompter(monkeypatch):
    """Keeping the value out of the environment is the preferred route."""
    monkeypatch.setenv("MXROUTE_PASSWORD_CMD", f"printf {MARKER}")

    config = Config(prompter=lambda _prompt: "prompted-value")

    assert config.password().reveal() == MARKER


# ----------------------------------------------------------------------------
def test_the_prompter_is_the_last_resort():
    """The core never owns the terminal; the front-end passes getpass in."""
    prompts = []

    def prompter(prompt):
        prompts.append(prompt)

        return MARKER

    config = Config(user="me@example.com", prompter=prompter)

    assert config.password().reveal() == MARKER
    assert prompts == ["Password for me@example.com: "]


# ----------------------------------------------------------------------------
def test_the_resolved_password_is_cached():
    """Resolving twice would prompt twice, or run the helper twice."""
    calls = []

    config = Config(prompter=lambda _p: calls.append(1) or MARKER)

    first = config.password()

    assert config.password() is first
    assert len(calls) == 1


# ----------------------------------------------------------------------------
def test_no_password_and_no_prompter_is_an_actionable_error():
    """A core module with nowhere to ask must fail, never block on stdin."""
    with pytest.raises(MxFilterError, match="MXROUTE_PASSWORD_CMD"):
        Config().password()


# ----------------------------------------------------------------------------
def test_an_empty_prompt_answer_is_refused():
    config = Config(prompter=lambda _prompt: "")

    with pytest.raises(MxFilterError, match="no password available"):
        config.password()


# ############################################################################
# The password command
# ############################################################################


# ----------------------------------------------------------------------------
def test_a_password_command_returns_its_first_line():
    """Helpers habitually print a trailing newline, or a second line."""
    secret = run_password_command(f"printf '{MARKER}\\nnoise\\n'")

    assert secret.reveal() == MARKER


# ----------------------------------------------------------------------------
def test_a_password_command_is_split_without_a_shell():
    """No shell means the value can never be re-expanded by one."""
    secret = run_password_command(f"printf '%s' '{MARKER} with spaces'")

    assert secret.reveal() == f"{MARKER} with spaces"


# ----------------------------------------------------------------------------
def test_an_empty_password_command_is_refused():
    with pytest.raises(MxFilterError, match="password command is empty"):
        run_password_command("   ")


# ----------------------------------------------------------------------------
def test_a_missing_password_program_is_named():
    with pytest.raises(MxFilterError, match="could not be run"):
        run_password_command("/nonexistent/credential-helper")


# ----------------------------------------------------------------------------
def test_a_failing_password_command_does_not_echo_its_output():
    """A helper's stderr may quote the value it was asked for.

    So the failure reports the program and the exit status only -- the one
    place where being less helpful is the correct trade.
    """
    command = f"sh -c 'printf %s {MARKER} >&2; exit 3'"

    with pytest.raises(MxFilterError) as caught:
        run_password_command(command)

    assert MARKER not in str(caught.value)
    assert "exit 3" in str(caught.value)
    assert "not shown" in str(caught.value)


# ----------------------------------------------------------------------------
def test_a_silent_password_command_is_refused():
    with pytest.raises(MxFilterError, match="produced no output"):
        run_password_command("true")


# ############################################################################
# Setting resolution order
# ############################################################################


# ----------------------------------------------------------------------------
def write_config_file(text: str) -> None:
    """Write a TOML config where ``config_path()`` will look for it."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ----------------------------------------------------------------------------
def test_a_flag_beats_the_environment_beats_the_file_beats_the_default(
    monkeypatch,
):
    """The documented order, asserted as one narrowing sequence.

    Four separate tests would each pass while the *order* was wrong;
    stripping one source at a time is what actually pins the precedence.
    """
    write_config_file('host = "from-file"\n')
    monkeypatch.setenv("MXROUTE_HOST", "from-env")

    args = argparse.Namespace(host="from-flag")
    assert load_config(args).host == "from-flag"

    args = argparse.Namespace(host=None)
    assert load_config(args).host == "from-env"

    monkeypatch.delenv("MXROUTE_HOST")
    assert load_config(args).host == "from-file"

    write_config_file("")
    assert load_config(args).host == ""


# ----------------------------------------------------------------------------
def test_the_built_in_defaults_apply_when_nothing_is_configured():
    """4190/starttls is the RFC 5804 default, not a known MXroute fact."""
    config = load_config(argparse.Namespace())

    assert config.sieve_port == DEFAULT_SIEVE_PORT
    assert config.imap_port == DEFAULT_IMAP_PORT
    assert config.sieve_tls == DEFAULT_SIEVE_TLS
    assert config.source_folder == "INBOX"


# ----------------------------------------------------------------------------
def test_the_imap_host_falls_back_to_the_sieve_host():
    """One hostname is the common case; two is the exception."""
    config = load_config(argparse.Namespace(host="mail.example.com"))

    assert config.imap_host == "mail.example.com"


# ----------------------------------------------------------------------------
def test_an_explicit_imap_host_wins_over_the_fallback():
    config = load_config(
        argparse.Namespace(
            host="mail.example.com", imap_host="imap.example.com"
        )
    )

    assert config.imap_host == "imap.example.com"


# ----------------------------------------------------------------------------
def test_the_password_is_never_read_from_the_config_file():
    """A file on disk is exactly where a credential must not live."""
    write_config_file(f'host = "h"\npassword = "{MARKER}"\n')

    config = load_config(argparse.Namespace())

    assert MARKER not in repr(config)
    assert config.password_state() == "unset"

    with pytest.raises(MxFilterError, match="no password available"):
        config.password()


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "port",
    [
        pytest.param("not-a-number", id="text"),
        pytest.param("", id="empty-falls-through-to-default"),
    ],
)
def test_a_non_numeric_port_is_named(monkeypatch, port):
    monkeypatch.setenv("MXROUTE_SIEVE_PORT", port)

    if port == "":
        assert load_config(argparse.Namespace()).sieve_port == (
            DEFAULT_SIEVE_PORT
        )

        return

    with pytest.raises(MxFilterError, match="sieve_port"):
        load_config(argparse.Namespace())


# ----------------------------------------------------------------------------
def test_an_unknown_tls_mode_is_refused_with_the_valid_set(monkeypatch):
    monkeypatch.setenv("MXROUTE_SIEVE_TLS", "maybe")

    with pytest.raises(MxFilterError, match="starttls, ssl, none"):
        load_config(argparse.Namespace())


# ----------------------------------------------------------------------------
def test_an_absent_config_file_is_not_an_error():
    assert read_config_file(config_path()) == {}


# ----------------------------------------------------------------------------
def test_invalid_toml_names_the_file():
    write_config_file("this is not = = toml\n")

    with pytest.raises(MxFilterError, match="invalid TOML"):
        read_config_file(config_path())


# ----------------------------------------------------------------------------
def test_config_path_honours_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))

    assert config_path() == tmp_path / "elsewhere/mxfilter/config.toml"


# ############################################################################
# Required settings
# ############################################################################


# ----------------------------------------------------------------------------
def test_require_lists_every_missing_setting_and_its_flag():
    """One message naming all of them beats three round trips."""
    config = Config()

    with pytest.raises(MxFilterError) as caught:
        config.require("host", "user", "imap_host")

    message = str(caught.value)

    assert "host, user, imap_host" in message
    assert "--imap-host" in message
    assert str(config_path()) in message


# ----------------------------------------------------------------------------
def test_require_passes_when_everything_is_present():
    config = Config(host="h", user="u@example.com")

    assert config.require("host", "user") is None
