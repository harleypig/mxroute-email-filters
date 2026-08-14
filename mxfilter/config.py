"""Endpoint and credential resolution.

Resolution order for every setting, highest priority first:

1. a CLI flag
2. an environment variable (``MXROUTE_*``)
3. the TOML config file (``$XDG_CONFIG_HOME/mxfilter/config.toml``)
4. a built-in default

The password is handled separately and never lands in a plain string that
could be printed by accident -- see the ``Secret`` class below.
"""

import os
import shlex
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import MxFilterError

__all__ = ["Config", "Secret", "config_path", "load_config"]

DEFAULT_SIEVE_PORT = 4190
DEFAULT_IMAP_PORT = 993
DEFAULT_SIEVE_TLS = "starttls"
DEFAULT_SOURCE_FOLDER = "INBOX"

SIEVE_TLS_MODES = ("starttls", "ssl", "none")


# ############################################################################
# Secret handling
# ############################################################################


class Secret:
    """A password that refuses to render itself.

    Every accidental path to disclosure -- ``print``, an f-string, ``repr``
    in a traceback frame, ``%s`` in a log line -- goes through ``__str__``
    or ``__repr__``, so overriding both turns the whole class of mistakes
    into a harmless ``<redacted>``. The real value is reachable only by
    calling ``reveal()``, which is greppable and therefore reviewable.
    """

    __slots__ = ("_value",)

    # ------------------------------------------------------------------------
    def __init__(self, value: str):
        """Wrap ``value``; it is never copied anywhere else."""
        self._value = value

    # ------------------------------------------------------------------------
    def reveal(self) -> str:
        """Return the wrapped value. Call this only when handing it to a
        connection method -- never to display, log, or format it."""
        return self._value

    # ------------------------------------------------------------------------
    def __str__(self) -> str:
        return "<redacted>"

    # ------------------------------------------------------------------------
    def __repr__(self) -> str:
        return "<Secret redacted>"

    # ------------------------------------------------------------------------
    def __bool__(self) -> bool:
        return bool(self._value)


# ############################################################################
# Config
# ############################################################################


@dataclass
class Config:
    """Resolved connection settings for one MXRoute account."""

    host: str = ""
    user: str = ""
    imap_host: str = ""
    imap_port: int = DEFAULT_IMAP_PORT
    sieve_port: int = DEFAULT_SIEVE_PORT
    sieve_tls: str = DEFAULT_SIEVE_TLS
    default_folder: str = ""
    source_folder: str = DEFAULT_SOURCE_FOLDER
    password_cmd: str = ""
    backup_dir: Path = field(default_factory=lambda: default_backup_dir())

    # Supplied by the front-end (the CLI passes getpass). Left None by
    # anything that cannot prompt, which then gets a clean error instead of
    # a process blocked on a terminal read that will never be answered.
    prompter: Callable[[str], str] | None = field(default=None, repr=False)

    # Resolved lazily by password(); never populated from a repr-able place.
    _password: Secret | None = field(default=None, repr=False)

    # ------------------------------------------------------------------------
    def __repr__(self) -> str:
        """Render without the credential.

        The dataclass-generated repr would happily print ``_password``; a
        Config lands in tracebacks and debug dumps, so the credential state
        is reported as a literal instead of a value.
        """
        return (
            f"Config(host={self.host!r}, user={self.user!r}, "
            f"imap_host={self.imap_host!r}, imap_port={self.imap_port!r}, "
            f"sieve_port={self.sieve_port!r}, sieve_tls={self.sieve_tls!r}, "
            f"default_folder={self.default_folder!r}, "
            f"source_folder={self.source_folder!r}, "
            f"password={self.password_state()})"
        )

    # ------------------------------------------------------------------------
    def password_state(self) -> str:
        """Return ``set`` or ``unset`` -- never the credential itself."""
        if self._password is not None:
            return "set"

        if os.environ.get("MXROUTE_PASSWORD"):
            return "set"

        if os.environ.get("MXROUTE_PASSWORD_CMD") or self.password_cmd:
            return "set (via command)"

        return "unset"

    # ------------------------------------------------------------------------
    def password(self) -> Secret:
        """Resolve the password, asking the prompter only as a last resort.

        Deferred until a connection is actually opened so that ``--help``
        and the offline code paths never trigger a prompt or run a
        credential command.

        The interactive prompt itself is *not* implemented here: a core
        module must not own a terminal interaction, or a non-terminal
        front-end could never reuse it. The caller supplies ``prompter``
        (the CLI passes ``getpass``); with none set, the absence of a
        credential is simply an error.
        """
        if self._password is not None:
            return self._password

        env_value = os.environ.get("MXROUTE_PASSWORD")
        command = os.environ.get("MXROUTE_PASSWORD_CMD") or self.password_cmd

        if env_value:
            self._password = Secret(env_value)

        elif command:
            self._password = run_password_command(command)

        elif self.prompter is not None:
            self._password = Secret(
                self.prompter(f"Password for {self.user or 'account'}: ")
            )

        else:
            raise MxFilterError(
                "no password available -- set MXROUTE_PASSWORD, "
                "MXROUTE_PASSWORD_CMD, or --password-cmd"
            )

        if not self._password:
            raise MxFilterError(
                "no password available -- set MXROUTE_PASSWORD, "
                "MXROUTE_PASSWORD_CMD, or --password-cmd"
            )

        return self._password

    # ------------------------------------------------------------------------
    def require(self, *names: str) -> None:
        """Fail with a single actionable message if a setting is missing."""
        missing = [name for name in names if not getattr(self, name, None)]

        if not missing:
            return

        hints = ", ".join(f"--{name.replace('_', '-')}" for name in missing)

        raise MxFilterError(
            f"missing required setting(s): {', '.join(missing)}. "
            f"Set {hints}, the matching MXROUTE_* variable, or add it to "
            f"{config_path()}"
        )


# ############################################################################
# Loading
# ############################################################################


# ----------------------------------------------------------------------------
def config_path() -> Path:
    """Return the TOML config location, honouring ``XDG_CONFIG_HOME``."""
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"

    return Path(base) / "mxfilter" / "config.toml"


# ----------------------------------------------------------------------------
def default_backup_dir() -> Path:
    """Return where pre-upload script backups are written by default."""
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"

    return Path(base) / "mxfilter" / "backups"


# ----------------------------------------------------------------------------
def read_config_file(path: Path) -> dict:
    """Parse the TOML config file, tolerating its absence."""
    if not path.is_file():
        return {}

    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)

    except tomllib.TOMLDecodeError as exc:
        raise MxFilterError(f"{path}: invalid TOML -- {exc}") from exc

    except OSError as exc:
        raise MxFilterError(f"{path}: cannot read -- {exc}") from exc


# ----------------------------------------------------------------------------
def run_password_command(command: str) -> Secret:
    """Run a credential command and capture its first stdout line.

    Split with ``shlex`` and run without a shell so the password can never
    be re-expanded by one. On failure only the exit status and the program
    name are reported: a credential helper's stderr is not a safe thing to
    echo, since it may quote the value it was asked for.
    """
    argv = shlex.split(command)

    if not argv:
        raise MxFilterError("password command is empty")

    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, check=False
        )

    except OSError as exc:
        raise MxFilterError(
            f"password command {argv[0]!r} could not be run -- {exc}"
        ) from exc

    if completed.returncode != 0:
        raise MxFilterError(
            f"password command {argv[0]!r} failed with exit "
            f"{completed.returncode} (its output is not shown, as it may "
            f"contain the credential)"
        )

    value = completed.stdout.split("\n", 1)[0].strip()

    if not value:
        raise MxFilterError(f"password command {argv[0]!r} produced no output")

    return Secret(value)


# ----------------------------------------------------------------------------
def _pick(*candidates, default=None):
    """Return the first candidate that is neither None nor empty."""
    for candidate in candidates:
        if candidate not in (None, ""):
            return candidate

    return default


# ----------------------------------------------------------------------------
def _as_port(value, label: str) -> int:
    """Coerce a port to int with a message naming which port failed."""
    try:
        return int(value)

    except (TypeError, ValueError) as exc:
        raise MxFilterError(
            f"{label}: {value!r} is not a port number"
        ) from exc


# ----------------------------------------------------------------------------
def load_config(args) -> Config:
    """Build a Config from CLI args, environment, and the config file.

    ``args`` is the parsed argparse namespace; any of the connection
    attributes may be absent or None, which simply defers to the next
    source in the resolution order.
    """
    file_values = read_config_file(config_path())
    env = os.environ

    def flag(name):
        return getattr(args, name, None)

    host = _pick(
        flag("host"),
        env.get("MXROUTE_HOST"),
        file_values.get("host"),
        default="",
    )

    user = _pick(
        flag("user"),
        env.get("MXROUTE_USER"),
        file_values.get("user"),
        default="",
    )

    imap_host = _pick(
        flag("imap_host"),
        env.get("MXROUTE_IMAP_HOST"),
        file_values.get("imap_host"),
        host,
        default="",
    )

    imap_port = _as_port(
        _pick(
            flag("imap_port"),
            env.get("MXROUTE_IMAP_PORT"),
            file_values.get("imap_port"),
            default=DEFAULT_IMAP_PORT,
        ),
        "imap_port",
    )

    sieve_port = _as_port(
        _pick(
            flag("sieve_port"),
            env.get("MXROUTE_SIEVE_PORT"),
            file_values.get("sieve_port"),
            default=DEFAULT_SIEVE_PORT,
        ),
        "sieve_port",
    )

    sieve_tls = _pick(
        flag("sieve_tls"),
        env.get("MXROUTE_SIEVE_TLS"),
        file_values.get("sieve_tls"),
        default=DEFAULT_SIEVE_TLS,
    )

    if sieve_tls not in SIEVE_TLS_MODES:
        raise MxFilterError(
            f"sieve_tls: {sieve_tls!r} is not one of "
            f"{', '.join(SIEVE_TLS_MODES)}"
        )

    backup_dir = _pick(
        flag("backup_dir"),
        env.get("MXROUTE_BACKUP_DIR"),
        file_values.get("backup_dir"),
        default=None,
    )

    return Config(
        host=host,
        user=user,
        imap_host=imap_host,
        imap_port=imap_port,
        sieve_port=sieve_port,
        sieve_tls=sieve_tls,
        default_folder=_pick(file_values.get("default_folder"), default=""),
        source_folder=_pick(
            flag("folder"),
            file_values.get("source_folder"),
            default=DEFAULT_SOURCE_FOLDER,
        ),
        password_cmd=_pick(
            flag("password_cmd"), file_values.get("password_cmd"), default=""
        ),
        backup_dir=Path(backup_dir) if backup_dir else default_backup_dir(),
    )
