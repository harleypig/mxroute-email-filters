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
import os
from pathlib import Path

import pytest

from mxfilter import MxFilterError
from mxfilter.cli import build_parser, configure
from mxfilter.config import (
    DEFAULT_IMAP_PORT,
    DEFAULT_SIEVE_PORT,
    DEFAULT_SIEVE_TLS,
    PASSWORD_STATE_LABELS,
    Config,
    Secret,
    config_path,
    load_config,
    read_config_file,
    read_password_file,
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


# ############################################################################
# The password file
# ############################################################################


# ----------------------------------------------------------------------------
def write_password_file(path: Path, contents: str, mode: int = 0o600) -> Path:
    """Write a password file with an explicit mode and return its path.

    Every file here is a marker string under ``tmp_path``; no test in this
    module reads, writes, or names a real credential.
    """
    path.write_text(contents, encoding="utf-8")
    path.chmod(mode)

    return path


# ----------------------------------------------------------------------------
@pytest.fixture
def secret_file(tmp_path):
    """Hand tests a password-file factory rooted at ``tmp_path``."""

    def make(
        contents: str = f"{MARKER}\n", mode: int = 0o600, name="password"
    ):
        return write_password_file(tmp_path / name, contents, mode)

    return make


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        pytest.param(MARKER, MARKER, id="no-trailing-newline"),
        pytest.param(f"{MARKER}\n", MARKER, id="one-newline-goes"),
        pytest.param(f"{MARKER}\r\n", MARKER, id="crlf-goes"),
        pytest.param(f"{MARKER}\n\n", f"{MARKER}\n", id="only-one-goes"),
        pytest.param(f"  {MARKER}  \n", f"  {MARKER}  ", id="spaces-kept"),
        pytest.param(f"{MARKER}\t\n", f"{MARKER}\t", id="tab-kept"),
        pytest.param(f"a {MARKER} b\n", f"a {MARKER} b", id="inner-kept"),
    ],
)
def test_a_password_file_loses_exactly_one_trailing_newline(
    secret_file, contents, expected
):
    """One newline is an editor's; anything else may be the password.

    Stripping the surrounding whitespace too would be the friendlier-
    looking choice and the wrong one: a password ending in a space is
    legal, and eating it produces an authentication failure whose cause is
    invisible from both ends.
    """
    assert read_password_file(secret_file(contents=contents)).reveal() == (
        expected
    )


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "contents",
    [
        pytest.param("", id="empty"),
        pytest.param("\n", id="one-newline"),
        pytest.param("   \n", id="spaces-only"),
        pytest.param("\t\n\n", id="whitespace-only"),
    ],
)
def test_an_empty_password_file_is_refused_by_name(secret_file, contents):
    """A blank file is a half-written one, not a password of spaces."""
    path = secret_file(contents=contents)

    with pytest.raises(MxFilterError) as caught:
        read_password_file(path)

    message = str(caught.value)

    assert str(path) in message
    assert "empty" in message


# ----------------------------------------------------------------------------
def test_a_missing_password_file_names_the_path_and_the_reason(tmp_path):
    path = tmp_path / "no-such-file"

    with pytest.raises(MxFilterError) as caught:
        read_password_file(path)

    message = str(caught.value)

    assert str(path) in message
    assert "No such file" in message


# ----------------------------------------------------------------------------
@pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads a 0000 file regardless of its mode"
)
def test_an_unreadable_password_file_is_named_but_never_quoted(secret_file):
    """0000 passes the mode check -- nobody but the owner can read it --
    and then fails to open, which is the OS error path this pins."""
    path = secret_file(mode=0o000)

    with pytest.raises(MxFilterError) as caught:
        read_password_file(path)

    message = str(caught.value)

    assert str(path) in message
    assert "Permission denied" in message
    assert MARKER not in message


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mode", [0o600, 0o400], ids=lambda mode: f"{mode:04o}"
)
def test_an_owner_only_password_file_is_accepted(secret_file, mode):
    """The owner's own bits are not our business; shared access is."""
    assert read_password_file(secret_file(mode=mode)).reveal() == MARKER


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mode",
    [0o640, 0o604, 0o644, 0o660, 0o606, 0o666, 0o444],
    ids=lambda mode: f"{mode:04o}",
)
def test_a_group_or_world_readable_password_file_is_refused(secret_file, mode):
    """Any bit in 0o077 means somebody else can read the credential.

    A refusal rather than a warning, matching ``psql``'s ``~/.pgpass``:
    the message has to be actionable on its own, so it carries the path,
    the mode found, and the command that fixes it -- and never a byte of
    what the file holds.
    """
    path = secret_file(mode=mode)

    with pytest.raises(MxFilterError) as caught:
        read_password_file(path)

    message = str(caught.value)

    assert str(path) in message
    assert f"{mode:04o}" in message
    assert "chmod 600" in message
    assert MARKER not in message


# ----------------------------------------------------------------------------
def test_a_refused_password_file_is_never_opened(secret_file, monkeypatch):
    """The check is worth little if the credential is read anyway.

    A file whose mode is wrong may be being read by something else at this
    moment; the point of refusing is to not add ourselves to that list.
    """

    def explode(*args, **kwargs):
        raise AssertionError("the file was opened despite its mode")

    monkeypatch.setattr(Path, "read_text", explode)

    with pytest.raises(MxFilterError, match="refuses to read it"):
        read_password_file(secret_file(mode=0o644))


# ----------------------------------------------------------------------------
def test_a_password_file_supplies_the_credential(secret_file):
    config = Config(password_file=str(secret_file()))

    assert config.password_state() == "set (via file)"
    assert config.password().reveal() == MARKER


# ############################################################################
# Password source precedence
# ############################################################################

# The ladder, highest first. The three flags share one rung because
# argparse refuses more than one of them, so they are never in contention
# with each other -- only with everything below.
FLAG_LEVELS = ("flag-file", "flag-cmd", "flag-value")
AMBIENT_LEVELS = (
    "env-file",
    "env-cmd",
    "env-literal",
    "toml-file",
    "toml-cmd",
)

PASSWORD_LEVELS = FLAG_LEVELS + AMBIENT_LEVELS

PASSWORD_LEVEL_STATE = {
    "flag-file": "set (via file)",
    "flag-cmd": "set (via command)",
    "flag-value": "set (via flag)",
    "env-file": "set (via file)",
    "env-cmd": "set (via command)",
    "env-literal": "set",
    "toml-file": "set (via file)",
    "toml-cmd": "set (via command)",
}

# Every pair a rung has to win, generated rather than written out: the
# interesting property is "each beats all of the ones below it", and 25
# hand-copied tests are 25 chances to transcribe the order wrongly.
PASSWORD_PRECEDENCE_PAIRS = [
    (winner, loser) for winner in FLAG_LEVELS for loser in AMBIENT_LEVELS
] + [
    (winner, loser)
    for index, winner in enumerate(AMBIENT_LEVELS)
    for loser in AMBIENT_LEVELS[index + 1 :]
]


# ----------------------------------------------------------------------------
def printf_command(value: str) -> str:
    """Return a credential command that prints ``value`` and nothing else."""
    return f"printf %s {value}"


# ----------------------------------------------------------------------------
def write_password_toml(values: dict) -> None:
    """Write the config file holding just the credential keys given."""
    write_config_file(
        "".join(f'{key} = "{value}"\n' for key, value in values.items())
    )


# ----------------------------------------------------------------------------
def arrange_password_source(
    level: str, value: str, tmp_path, monkeypatch, flags: dict, toml: dict
) -> None:
    """Configure one rung of the ladder to supply ``value``.

    ``flags`` becomes the argparse namespace and ``toml`` the config file,
    so a caller can arrange several rungs at once and then build the
    Config exactly the way the CLI does.
    """
    match level:
        case "flag-file":
            flags["password_file"] = str(
                write_password_file(tmp_path / level, f"{value}\n")
            )

        case "flag-cmd":
            flags["password_cmd"] = printf_command(value)

        case "flag-value":
            flags["password"] = value

        case "env-file":
            monkeypatch.setenv(
                "MXROUTE_PASSWORD_FILE",
                str(write_password_file(tmp_path / level, f"{value}\n")),
            )

        case "env-cmd":
            monkeypatch.setenv("MXROUTE_PASSWORD_CMD", printf_command(value))

        case "env-literal":
            monkeypatch.setenv("MXROUTE_PASSWORD", value)

        case "toml-file":
            toml["password_file"] = str(
                write_password_file(tmp_path / level, f"{value}\n")
            )

        case "toml-cmd":
            toml["password_cmd"] = printf_command(value)

        case _:
            raise AssertionError(f"unknown password level {level!r}")


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(("winner", "loser"), PASSWORD_PRECEDENCE_PAIRS)
def test_a_higher_password_source_beats_every_lower_one(
    winner, loser, tmp_path, monkeypatch
):
    """Both rungs configured at once; only the order decides the outcome.

    Configuring one source at a time would pass against any ordering at
    all. Two at a time is what makes the assertion about precedence rather
    than about resolution.
    """
    flags: dict = {}
    toml: dict = {}

    arrange_password_source(
        loser, f"from-{loser}", tmp_path, monkeypatch, flags, toml
    )
    arrange_password_source(
        winner, f"from-{winner}", tmp_path, monkeypatch, flags, toml
    )

    write_password_toml(toml)

    config = load_config(argparse.Namespace(**flags))

    assert config.password_state() == PASSWORD_LEVEL_STATE[winner]
    assert config.password().reveal() == f"from-{winner}"


# ----------------------------------------------------------------------------
@pytest.mark.parametrize("level", PASSWORD_LEVELS)
def test_every_configured_source_beats_the_prompt(
    level, tmp_path, monkeypatch
):
    """The prompt is the last rung, below all seven of the others."""
    flags: dict = {}
    toml: dict = {}

    arrange_password_source(
        level, f"from-{level}", tmp_path, monkeypatch, flags, toml
    )
    write_password_toml(toml)

    config = load_config(argparse.Namespace(**flags))
    config.prompter = lambda _prompt: pytest.fail("the prompt was reached")

    assert config.password().reveal() == f"from-{level}"


# ----------------------------------------------------------------------------
def test_an_explicit_password_cmd_flag_beats_an_exported_password(
    monkeypatch,
):
    """The regression: a flag typed for this run beats an ambient variable.

    The failure it prevents is not cosmetic. With MXROUTE_PASSWORD
    exported for one account, a --password-cmd naming a *second* account
    used to be ignored, and the command authenticated as the first -- the
    wrong account, silently, with no error anywhere.
    """
    monkeypatch.setenv("MXROUTE_PASSWORD", "from-env")

    config = load_config(
        argparse.Namespace(password_cmd=printf_command("from-flag"))
    )

    assert config.password_state() == "set (via command)"
    assert config.password().reveal() == "from-flag"


# ----------------------------------------------------------------------------
@pytest.mark.parametrize("level", PASSWORD_LEVELS)
def test_password_state_reports_a_label_and_never_a_value(
    level, tmp_path, monkeypatch
):
    """Whichever source is in play, the state is one of four literals."""
    flags: dict = {}
    toml: dict = {}

    arrange_password_source(level, MARKER, tmp_path, monkeypatch, flags, toml)
    write_password_toml(toml)

    config = load_config(argparse.Namespace(**flags))

    assert config.password_state() in set(PASSWORD_STATE_LABELS.values())
    assert MARKER not in config.password_state()

    config.password()

    assert config.password_state() == "set"
    assert MARKER not in repr(config)


# ############################################################################
# The credential flags
# ############################################################################


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(
            ["--password", "x", "--password-file", "f"], id="value-and-file"
        ),
        pytest.param(
            ["--password", "x", "--password-cmd", "c"], id="value-and-cmd"
        ),
        pytest.param(
            ["--password-file", "f", "--password-cmd", "c"], id="file-and-cmd"
        ),
    ],
)
def test_two_credential_flags_are_refused_by_argparse(capsys, argv):
    """Three explicit instructions have no natural ranking between them.

    Saying so is better than picking one: a silent winner is a rule the
    user has to have memorised to predict what just authenticated.
    """
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["test", *argv])

    assert caught.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("argv", "warns"),
    [
        pytest.param(["--password", MARKER], True, id="inline-warns"),
        pytest.param(["--password-cmd", "true"], False, id="cmd-silent"),
        pytest.param(["--password-file", "f"], False, id="file-silent"),
        pytest.param([], False, id="nothing-silent"),
    ],
)
def test_only_the_inline_password_flag_warns(capsys, argv, warns):
    """What it gives away is not obvious, so the tool says it once.

    The value is readable in the process list by every user on the machine
    while the command runs, and the shell wrote it to history before
    mxfilter started. The warning names neither the value nor a fragment
    of it.
    """
    configure(build_parser().parse_args(["test", *argv]))

    captured = capsys.readouterr().err

    assert bool(captured) is warns
    assert MARKER not in captured

    if warns:
        assert "process list" in captured
        assert "--password-file" in captured


# ----------------------------------------------------------------------------
def test_a_flag_supplied_password_is_redacted_like_any_other():
    """The least safe source is not a less protected one."""
    config = load_config(argparse.Namespace(password=MARKER))
    secret = config.password()

    assert secret.reveal() == MARKER
    assert MARKER not in f"{secret}"
    assert MARKER not in f"{secret!r}"
    assert MARKER not in "%s" % (secret,)  # noqa: UP031
    assert MARKER not in repr(config)
