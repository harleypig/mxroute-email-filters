"""Backups: where they land, what they contain, and who can read them.

One property carries this file: **a backup is the server's exact bytes**.
Anything else -- a banner line, a re-render, a newline translated on the way
out -- produces a file that looks like a backup, is kept like a backup, and
cannot be put back. ``mxfilter show`` decorates its output for a reader;
``mxfilter backup`` must not, and the byte-for-byte assertion below is the
test that says so.

The rest is the handling around it: the file is the owner's to read
(``0600``, in a ``0700`` directory), it lands in the config directory beside
``config.toml`` where somebody can find it, and ``--dry-run`` writes nothing.
"""

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from mxfilter import cli
from mxfilter import sieve as sieve_module
from mxfilter.config import Config, default_backup_dir, load_config
from mxfilter.sieve import backup_path, resolve_backup_target, write_backup

# A script whose bytes are awkward on purpose: CRLF endings, as the CRLF
# protocol that fetched it produces, and no trailing newline. A text-mode
# write with the default newline handling, or any pass through the parser,
# would change both.
CRLF_SCRIPT = (
    'require ["fileinto"];\r\n'
    "# rule:[keep-boss]\r\n"
    'if header :contains "from" "boss@example.com"\r\n'
    "{\r\n"
    '\tfileinto "INBOX.Boss";\r\n'
    "\tstop;\r\n"
    "}"
)


# ############################################################################
# The ManageSieve double
# ############################################################################


class FakeSieveClient:
    """A stand-in for ``sievelib.managesieve.Client``.

    Patched in at mxfilter's import boundary, the same way ``fake_imap``
    stands in for ``IMAPClient`` -- so ``SieveSession`` and everything above
    it is the real code under test.
    """

    # ------------------------------------------------------------------------
    def __init__(self, script: str = CRLF_SCRIPT, active: str = "managesieve"):
        """Start out as an account with one active script."""
        self.script = script
        self.active = active
        self.calls: list[tuple] = []

    # ------------------------------------------------------------------------
    def connect(self, user, password, starttls=False, ssl=False) -> bool:
        self.calls.append(("connect", user))

        return True

    # ------------------------------------------------------------------------
    def logout(self) -> None:
        self.calls.append(("logout",))

    # ------------------------------------------------------------------------
    def listscripts(self):
        self.calls.append(("listscripts",))

        return (self.active, [])

    # ------------------------------------------------------------------------
    def getscript(self, name: str):
        self.calls.append(("getscript", name))

        return self.script

    # ------------------------------------------------------------------------
    def get_sieve_capabilities(self):
        return ["fileinto", "imap4flags"]


# ----------------------------------------------------------------------------
@pytest.fixture
def fake_sieve(monkeypatch) -> FakeSieveClient:
    """Patch the ManageSieve client and hand the double back."""
    client = FakeSieveClient()

    def factory(host, port, debug=False):
        return client

    monkeypatch.setattr(sieve_module, "Client", factory)

    return client


# ----------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def account_settings(monkeypatch, isolated_environment):
    """Give the CLI an account so nothing prompts for a credential.

    It depends on ``isolated_environment`` explicitly so that it runs
    *after* the fixture that clears every ``MXROUTE_*`` variable, rather
    than relying on autouse ordering to put them in that order.
    """
    monkeypatch.setenv("MXROUTE_HOST", "mail.example.com")
    monkeypatch.setenv("MXROUTE_USER", "user@example.com")
    monkeypatch.setenv("MXROUTE_PASSWORD", "not-a-real-password")


# ----------------------------------------------------------------------------
def mode_of(path: Path) -> int:
    """Return the permission bits of ``path``."""
    return stat.S_IMODE(path.stat().st_mode)


# ############################################################################
# Where the default lands
# ############################################################################


# ----------------------------------------------------------------------------
def test_the_default_backup_dir_is_under_xdg_config_home(
    monkeypatch, tmp_path
):
    """Config, not state: the directory the user already knows.

    XDG would call a backup state. Co-locating it with ``config.toml`` is a
    deliberate departure, because a backup nobody can find is not a backup.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    assert default_backup_dir() == tmp_path / "cfg" / "mxfilter" / "backups"


# ----------------------------------------------------------------------------
def test_the_default_backup_dir_falls_back_to_dot_config(
    monkeypatch, tmp_path
):
    """An unset ``XDG_CONFIG_HOME`` means ``~/.config``, as XDG specifies."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert (
        default_backup_dir() == tmp_path / ".config" / "mxfilter" / "backups"
    )


# ----------------------------------------------------------------------------
def test_the_backup_dir_sits_beside_the_config_file(monkeypatch, tmp_path):
    """The whole point of the move: one directory, both files."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    from mxfilter.config import config_path

    assert default_backup_dir().parent == config_path().parent


# ----------------------------------------------------------------------------
def test_a_config_with_no_backup_dir_takes_the_default(monkeypatch, tmp_path):
    """The dataclass default and ``load_config`` must not disagree."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    expected = tmp_path / "cfg" / "mxfilter" / "backups"

    assert Config().backup_dir == expected
    assert load_config(SimpleNamespace()).backup_dir == expected


# ----------------------------------------------------------------------------
def test_mxroute_backup_dir_still_overrides_the_default(monkeypatch, tmp_path):
    """The override is unchanged by the move; only the default shifted."""
    monkeypatch.setenv("MXROUTE_BACKUP_DIR", str(tmp_path / "elsewhere"))

    assert load_config(SimpleNamespace()).backup_dir == tmp_path / "elsewhere"


# ----------------------------------------------------------------------------
def test_the_pre_upload_backup_lands_in_the_config_dir(monkeypatch, tmp_path):
    """The automatic backup and ``mxfilter backup`` agree on one place.

    Two defaults for one kind of file is how a user ends up looking in the
    directory that does not have their backup in it.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    uploaded = []

    session = SimpleNamespace(
        check_script=lambda content: uploaded.append(("check", content)),
        put_script=lambda name, content: uploaded.append(("put", name)),
        set_active=lambda name: uploaded.append(("active", name)),
    )

    cli.upload(
        session,
        load_config(SimpleNamespace()),
        "managesieve",
        CRLF_SCRIPT,
        'require ["fileinto"];\r\n',
        SimpleNamespace(),
    )

    written = sorted((tmp_path / "cfg" / "mxfilter" / "backups").iterdir())

    assert len(written) == 1
    assert written[0].read_bytes() == CRLF_SCRIPT.encode("utf-8")
    assert [call[0] for call in uploaded] == ["check", "put", "active"]


# ############################################################################
# --output semantics
# ############################################################################


# ----------------------------------------------------------------------------
def test_no_output_uses_the_default_directory_and_name(tmp_path):
    target = resolve_backup_target(None, "managesieve", tmp_path / "backups")

    assert target.parent == tmp_path / "backups"
    assert target.name.startswith("managesieve-")
    assert target.suffix == ".sieve"


# ----------------------------------------------------------------------------
def test_an_existing_directory_gets_the_default_filename(tmp_path):
    """A path that is already a directory can only mean 'in here'."""
    somewhere = tmp_path / "somewhere"
    somewhere.mkdir()

    target = resolve_backup_target(
        str(somewhere), "managesieve", tmp_path / "unused"
    )

    assert target.parent == somewhere
    assert target.name.startswith("managesieve-")


# ----------------------------------------------------------------------------
def test_a_trailing_separator_means_a_directory_that_need_not_exist(tmp_path):
    """The one case the two rules disagree about, settled by the slash.

    Without it, a directory mxfilter is being asked to create would be
    indistinguishable from a filename, and the backup would land in a file
    named after the directory the user meant.
    """
    target = resolve_backup_target(
        f"{tmp_path / 'not-yet'}/", "managesieve", tmp_path / "unused"
    )

    assert target.parent == tmp_path / "not-yet"
    assert target.name.startswith("managesieve-")


# ----------------------------------------------------------------------------
def test_a_plain_path_is_written_exactly_as_given(tmp_path):
    target = resolve_backup_target(
        str(tmp_path / "keep.sieve"), "managesieve", tmp_path / "unused"
    )

    assert target == tmp_path / "keep.sieve"


# ############################################################################
# The bytes, the mode, and the directory
# ############################################################################


# ----------------------------------------------------------------------------
def test_write_backup_writes_the_bytes_it_was_handed(tmp_path):
    """CRLF endings and a missing final newline both survive.

    A text-mode write with default newline handling would rewrite the line
    endings on a platform whose separator is not ``\\n``, which is the kind
    of corruption nobody notices until the restore.
    """
    target = write_backup(CRLF_SCRIPT, tmp_path / "out" / "copy.sieve")

    assert target.read_bytes() == CRLF_SCRIPT.encode("utf-8")


# ----------------------------------------------------------------------------
def test_the_backup_file_is_readable_only_by_its_owner(tmp_path):
    """A Sieve script says who the user writes to and how they sort it."""
    target = write_backup(CRLF_SCRIPT, tmp_path / "out" / "copy.sieve")

    assert mode_of(target) == 0o600


# ----------------------------------------------------------------------------
def test_a_directory_mxfilter_creates_is_private(tmp_path):
    """Including the parents, which ``mkdir(parents=True)`` would not set."""
    write_backup(CRLF_SCRIPT, tmp_path / "outer" / "inner" / "copy.sieve")

    assert mode_of(tmp_path / "outer") == 0o700
    assert mode_of(tmp_path / "outer" / "inner") == 0o700


# ----------------------------------------------------------------------------
def test_an_existing_directory_keeps_the_mode_the_user_gave_it(tmp_path):
    """mxfilter decides the mode of what it creates, and nothing else."""
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o755)

    write_backup(CRLF_SCRIPT, existing / "copy.sieve")

    assert mode_of(existing) == 0o755


# ----------------------------------------------------------------------------
def test_overwriting_a_world_readable_file_closes_it(tmp_path):
    """``O_CREAT``'s mode is ignored for a file that already exists."""
    target = tmp_path / "copy.sieve"
    target.write_text("stale")
    target.chmod(0o644)

    write_backup(CRLF_SCRIPT, target)

    assert mode_of(target) == 0o600
    assert target.read_bytes() == CRLF_SCRIPT.encode("utf-8")


# ----------------------------------------------------------------------------
def test_backup_path_sanitizes_a_name_that_would_escape_the_directory(
    tmp_path,
):
    """The script name comes from the server, so it is not trusted."""
    target = backup_path("../../etc/passwd", tmp_path / "backups")

    assert target.parent == tmp_path / "backups"
    assert "/" not in target.name


# ############################################################################
# The subcommand
# ############################################################################


# ----------------------------------------------------------------------------
def test_backup_writes_the_script_verbatim_and_says_where(
    fake_sieve, tmp_path, capsys
):
    """The assertion this whole feature exists for.

    ``mxfilter show`` prints the same script wrapped in ``# ---- name ----``
    and ``# ---- N rule(s): ...``. Those lines are why redirecting ``show``
    to a file is not a backup, and why this one has to be compared as
    bytes rather than eyeballed.
    """
    target = tmp_path / "copy.sieve"

    assert cli.main(["backup", "--output", str(target)]) == 0

    assert target.read_bytes() == CRLF_SCRIPT.encode("utf-8")

    output = capsys.readouterr().out

    assert output == f"wrote 1 rule(s) to {target}\n"
    assert "# ----" not in output


# ----------------------------------------------------------------------------
def test_backup_defaults_to_the_config_directory(
    fake_sieve, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    assert cli.main(["backup"]) == 0

    backups = tmp_path / "cfg" / "mxfilter" / "backups"
    written = sorted(backups.iterdir())

    assert len(written) == 1
    assert written[0].name.startswith("managesieve-")
    assert written[0].read_bytes() == CRLF_SCRIPT.encode("utf-8")
    assert str(written[0]) in capsys.readouterr().out


# ----------------------------------------------------------------------------
def test_backup_into_a_directory_uses_the_default_filename(
    fake_sieve, tmp_path, capsys
):
    somewhere = tmp_path / "somewhere"
    somewhere.mkdir()

    assert cli.main(["backup", "--output", str(somewhere)]) == 0

    written = sorted(somewhere.iterdir())

    assert len(written) == 1
    assert written[0].name.startswith("managesieve-")
    assert written[0].suffix == ".sieve"
    assert str(written[0]) in capsys.readouterr().out


# ----------------------------------------------------------------------------
def test_backup_dry_run_writes_nothing(fake_sieve, tmp_path, capsys):
    """It reports the file it would have written, and leaves no file."""
    target = tmp_path / "out" / "copy.sieve"

    assert cli.main(["backup", "--output", str(target), "--dry-run"]) == 0

    assert not target.exists()
    assert not target.parent.exists()

    output = capsys.readouterr().out

    assert output == f"[dry-run] would write 1 rule(s) to {target}\n"


# ----------------------------------------------------------------------------
def test_backup_honours_the_backup_dir_flag(fake_sieve, tmp_path, capsys):
    """``--backup-dir`` and ``MXROUTE_BACKUP_DIR`` steer it as before."""
    assert cli.main(["backup", "--backup-dir", str(tmp_path / "chosen")]) == 0

    written = sorted((tmp_path / "chosen").iterdir())

    assert len(written) == 1
    assert written[0].read_bytes() == CRLF_SCRIPT.encode("utf-8")


# ----------------------------------------------------------------------------
def test_backup_of_a_script_that_will_not_parse_still_writes_the_file(
    fake_sieve, tmp_path, capsys
):
    """The case a copy matters most is the one the parser cannot read.

    Counting the rules is a nicety printed afterwards; refusing to save a
    broken script because it is broken would be exactly backwards.
    """
    fake_sieve.script = "if header :contains {\n"

    target = tmp_path / "copy.sieve"

    assert cli.main(["backup", "--output", str(target)]) == 0

    assert target.read_bytes() == fake_sieve.script.encode("utf-8")
    assert "could not parse" in capsys.readouterr().out


# ----------------------------------------------------------------------------
def test_backup_with_no_active_script_says_so(fake_sieve, capsys):
    """Failure path: nothing to copy is an explained error, not a traceback."""
    fake_sieve.active = None

    assert cli.main(["backup"]) == 1

    assert "nothing to back up" in capsys.readouterr().err


# ----------------------------------------------------------------------------
def test_backup_reports_an_unwritable_target(fake_sieve, tmp_path, capsys):
    """The other failure path: the file cannot be written."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")

    assert cli.main(["backup", "--output", str(blocker / "copy.sieve")]) == 1

    assert "could not write backup" in capsys.readouterr().err


# ----------------------------------------------------------------------------
def test_backup_leaves_the_server_alone(fake_sieve, tmp_path):
    """Read-only: it lists, downloads, and logs out. Nothing else."""
    cli.main(["backup", "--output", str(tmp_path / "copy.sieve")])

    verbs = {call[0] for call in fake_sieve.calls}

    assert verbs == {"connect", "listscripts", "getscript", "logout"}


# ----------------------------------------------------------------------------
def test_backup_help_names_the_missing_restore_command(capsys):
    """The gap is more conspicuous now that ``backup`` is a verb."""
    with pytest.raises(SystemExit):
        cli.main(["backup", "--help"])

    # argparse re-wraps the description to the terminal width, so the
    # phrase is matched against the text with its line breaks collapsed.
    helped = " ".join(capsys.readouterr().out.split())

    assert "has no restore command" in helped


# ----------------------------------------------------------------------------
def test_the_backup_directory_is_reachable_without_an_environment(tmp_path):
    """``os.sep`` handling is exercised, not assumed, on this platform."""
    target = resolve_backup_target(
        f"{tmp_path}{os.sep}", "managesieve", tmp_path / "unused"
    )

    assert target.parent == tmp_path
