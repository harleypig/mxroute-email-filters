"""Command-line interface.

Every mutating subcommand follows the same shape: work out what would
change, show it, and only then -- after a backup and the server's own
validation -- change it. ``--dry-run`` stops after the "show it" step.
"""

import argparse
import email.utils
import re
import sys
import traceback
from contextlib import ExitStack

from . import MxFilterError, __version__
from .config import SIEVE_TLS_MODES, load_config
from .criteria import COMPARE_OPS, MATCH_MODES, Criteria, escape_sieve_string
from .imap import ImapSession, decode_header_value, normalize_folder
from .sieve import (
    MXROUTE_FORBIDDEN_ACTIONS,
    REPORTABLE_EXTENSIONS,
    UNIMPLEMENTED_ACTIONS,
    SieveSession,
    backup_script,
    merge_rule,
    parse_script,
    remove_rule,
    rule_names,
    script_diff,
)

__all__ = ["build_parser", "main"]

DEFAULT_SCRIPT_NAME = "mxfilter"
DEFAULT_MOVE_THRESHOLD = 25
DEFAULT_MAX_MESSAGES = 500
PREVIEW_LIMIT = 20


# ############################################################################
# Small output helpers
# ############################################################################


# ----------------------------------------------------------------------------
def warn(message: str) -> None:
    """Print a warning to stderr so it survives a piped stdout."""
    print(f"warning: {message}", file=sys.stderr)


# ----------------------------------------------------------------------------
def confirm(prompt: str, assume_yes: bool) -> bool:
    """Ask for confirmation, failing closed on a non-interactive stdin.

    A prompt that cannot be answered must not silently proceed, and must not
    hang either -- so a non-tty without ``--yes`` is an error naming the
    flag that would have allowed it.
    """
    if assume_yes:
        return True

    if not sys.stdin.isatty():
        raise MxFilterError(
            f"{prompt} -- refusing to continue without a terminal to ask. "
            f"Re-run with --yes to confirm, or --dry-run to preview."
        )

    answer = input(f"{prompt} [y/N] ").strip().lower()

    return answer in ("y", "yes")


# ----------------------------------------------------------------------------
def print_preview(messages: list, limit: int = PREVIEW_LIMIT) -> None:
    """Print a capped preview table of the messages that matched."""
    for message in messages[:limit]:
        print(format_summary(message))

    if len(messages) > limit:
        print(f"  ... and {len(messages) - limit} more")


# ----------------------------------------------------------------------------
def format_summary(message) -> str:
    """Render one message record as a fixed-width preview line.

    Rendering lives here rather than on the record: a MessageSummary is
    data, and a second front-end would want to lay it out differently.
    """
    return (
        f"  uid {message.uid:<8} {message.date:<20} "
        f"{clip(message.sender, 30):<30} {clip(message.subject, 50)}"
    )


# ----------------------------------------------------------------------------
def clip(value: str, width: int) -> str:
    """Collapse whitespace and truncate a display string to ``width``."""
    value = " ".join(value.split())

    if len(value) <= width:
        return value

    return value[: width - 1] + "\u2026"


# ----------------------------------------------------------------------------
def progress_for(args, label: str):
    """Return a progress callback for the core modules, or None.

    The core modules report progress by calling back rather than printing,
    so the decision to show it -- and the decoration around it -- is made
    once, here.
    """
    if not args.verbose:
        return None

    def emit(message: str) -> None:
        print(f"[{label}] {message}")

    return emit


# ----------------------------------------------------------------------------
def configure(args):
    """Load the config and give it a way to ask for a password.

    ``config.py`` deliberately cannot prompt -- a core module owning a
    terminal interaction is what stops a non-terminal front-end reusing it
    -- so the front-end supplies the prompt. ``getpass`` is imported here,
    at the one place a terminal is assumed.
    """
    from getpass import getpass

    config = load_config(args)
    config.prompter = getpass

    return config


# ############################################################################
# Criteria and actions from parsed args
# ############################################################################


# ----------------------------------------------------------------------------
def criteria_from_args(args) -> Criteria:
    """Build the Criteria model from the shared criteria flags."""
    criteria = Criteria(match=args.match, compare=args.compare)

    for header, values in (
        ("From", getattr(args, "from_addr", None)),
        ("To", args.to),
        ("Cc", args.cc),
        ("Subject", args.subject),
        ("List-Id", args.list_id),
    ):
        for value in values or []:
            criteria.add(header, value)

    for item in args.header or []:
        if "=" not in item:
            raise MxFilterError(f"--header expects NAME=VALUE, got {item!r}")

        name, value = item.split("=", 1)
        criteria.add(name, value)

    return criteria


# ----------------------------------------------------------------------------
def reject_forbidden(args) -> None:
    """Refuse actions this tool will not generate, and say why.

    Two different reasons, kept apart on purpose. ``redirect`` is refused
    because MXRoute has publicly disabled it -- a policy, so the alternative
    is named. The rest are simply not implemented here, and mxfilter has no
    evidence either way about whether this server supports them; that
    question belongs to ``mxfilter test``, which reads the answer off the
    server instead of guessing.
    """
    for name, explanation in MXROUTE_FORBIDDEN_ACTIONS.items():
        if getattr(args, name, None):
            raise MxFilterError(explanation)

    for name, label in UNIMPLEMENTED_ACTIONS.items():
        if getattr(args, name, None):
            raise MxFilterError(
                f"mxfilter does not generate the Sieve '{label}' action. "
                f"This is a conservative choice of ours, not a documented "
                f"MXRoute restriction -- the MXRoute control panel is where "
                f"this feature lives if you need it. To see whether the "
                f"server advertises the extension at all, run "
                f"'mxfilter test'."
            )


# ----------------------------------------------------------------------------
def default_rule_name(criteria: Criteria) -> str:
    """Derive a stable rule name from the first criterion."""
    term = criteria.terms[0]

    slug = re.sub(r"[^A-Za-z0-9]+", "-", term.value).strip("-").lower()

    return f"{term.header.lower()}-{slug}"[:60] or DEFAULT_SCRIPT_NAME


# ----------------------------------------------------------------------------
def sieve_actions(args, folder: str, use_create: bool) -> list[tuple]:
    """Build the sievelib action tuples for the requested actions.

    Flags are emitted before ``fileinto`` so the delivered copy carries
    them, and ``stop`` last so later rules do not also fire.
    """
    actions: list[tuple] = []
    flags = collect_flags(args)

    for flag in flags:
        actions.append(("addflag", escape_sieve_string(flag)))

    if args.discard:
        actions.append(("discard",))

    elif folder:
        if use_create:
            actions.append(
                ("fileinto", ":create", escape_sieve_string(folder))
            )

        else:
            actions.append(("fileinto", escape_sieve_string(folder)))

    if args.keep:
        actions.append(("keep",))

    if not actions:
        raise MxFilterError(
            "no action requested -- use --fileinto, --discard, --mark-read, "
            "--flag, or --keep"
        )

    if not args.no_stop:
        actions.append(("stop",))

    return actions


# ----------------------------------------------------------------------------
def collect_flags(args) -> list[str]:
    """Return the IMAP flags implied by ``--mark-read`` and ``--flag``."""
    flags = []

    if args.mark_read:
        flags.append("\\Seen")

    for flag in args.flag or []:
        if flag not in flags:
            flags.append(flag)

    return flags


# ----------------------------------------------------------------------------
def required_extensions(args, use_create: bool) -> set[str]:
    """Return the Sieve extensions the generated rule will need."""
    needed = set()

    if collect_flags(args):
        needed.add("imap4flags")

    if not args.discard and args.fileinto:
        needed.add("fileinto")

    if use_create:
        needed.add("mailbox")

    return needed


# ############################################################################
# Shared plumbing
# ############################################################################


# ----------------------------------------------------------------------------
def open_sessions(config, args, need_imap: bool, stack: ExitStack):
    """Open the sessions a command needs and return ``(sieve, imap)``."""
    imap = None

    if need_imap:
        imap = stack.enter_context(
            ImapSession(config, progress=progress_for(args, "imap"))
        )

    sieve = stack.enter_context(
        SieveSession(config, progress=progress_for(args, "sieve"))
    )

    return sieve, imap


# ----------------------------------------------------------------------------
def fetch_active(sieve: SieveSession, args) -> tuple[str, str]:
    """Return ``(script_name, source)`` for the script to edit.

    The name always comes from LISTSCRIPTS and is written back to. It is
    never guessed: whatever the webmail's managesieve plugin calls its
    script is a server-side config value (``managesieve_script_name``) that
    nothing about the account exposes, so a guess would create a *second*
    script and quietly leave the real one in charge. MXRoute has also said
    it intends to move off DirectAdmin, Crossbox, and Roundcube, and is
    mid-migration from Dovecot 2.3 to 2.4 -- what is discovered at runtime
    survives that, and a hardcoded name would not.

    ``DEFAULT_SCRIPT_NAME`` is used only when the account has no scripts at
    all, which is a genuine first run with nothing to collide with.
    """
    requested = getattr(args, "script", None)
    active = sieve.active_script_name()
    name = requested or active or DEFAULT_SCRIPT_NAME

    _active, others = sieve.list_scripts()

    if name == active or name in others:
        return (name, sieve.get_script(name))

    return (name, "")


# ----------------------------------------------------------------------------
def resolve_folder(args, imap: ImapSession | None, config) -> str:
    """Normalize the target folder and report what will happen to it."""
    requested = args.fileinto or config.default_folder

    if not requested:
        return ""

    if imap is None:
        # Without a folder list the Maildir++ heuristic is the best that
        # can be done; --no-imap is opt-in precisely for this trade.
        delimiter = args.delimiter or "."
        folder = normalize_folder(requested, delimiter, None)
        warn(
            f"no IMAP connection: assuming delimiter {delimiter!r}, "
            f"target folder {folder!r}"
        )

        return folder

    folder = imap.normalize(requested)

    if requested != folder:
        print(
            f"Folder {requested!r} resolves to {folder!r} "
            f"(delimiter {imap.delimiter!r})"
        )

    return folder


# ----------------------------------------------------------------------------
def ensure_folder(
    folder: str, args, imap: ImapSession | None, sieve: SieveSession
) -> bool:
    """Decide how the target folder gets to exist; return whether to
    emit ``fileinto :create``.

    ``:create`` is preferred when the server advertises ``mailbox``, since
    then Sieve makes the folder at delivery time. Otherwise the folder is
    made over IMAP now, and the rule stays a plain ``fileinto``.
    """
    if not folder:
        return False

    exists = imap.exists(folder) if imap else False

    if exists:
        return False

    has_mailbox = not sieve.missing_extensions({"mailbox"})

    if not args.create_folder:
        warn(
            f"target folder {folder!r} does not exist. Mail filed there may "
            f"be lost; pass --create-folder to create it."
        )

        return False

    if has_mailbox:
        print(f"Folder {folder!r} will be created by Sieve (fileinto :create)")

        return True

    if imap is None:
        raise MxFilterError(
            f"the server does not advertise the Sieve 'mailbox' extension "
            f"and --no-imap was given, so {folder!r} cannot be created"
        )

    if args.dry_run:
        print(f"[dry-run] would create IMAP folder {folder!r}")

        return False

    imap.create_folder(folder)
    print(f"Created IMAP folder {folder!r}")

    return False


# ----------------------------------------------------------------------------
def upload(
    sieve: SieveSession, config, name: str, before: str, after: str, args
) -> None:
    """Back up, validate, upload, and activate a script."""
    path = backup_script(before, name, config.backup_dir)
    print(f"Backed up current script to {path}")

    sieve.check_script(after)
    sieve.put_script(name, after)
    sieve.set_active(name)

    print(f"Uploaded and activated script {name!r}")


# ----------------------------------------------------------------------------
def warn_missing_extensions(sieve: SieveSession, needed: set[str]) -> None:
    """Warn about extensions the rule needs but the server does not list."""
    missing = sieve.missing_extensions(needed)

    if missing:
        warn(
            f"the server does not advertise: {', '.join(missing)}. The "
            f"upload will be validated with CHECKSCRIPT and may be rejected."
        )


# ############################################################################
# The existing-mail pass
# ############################################################################


# ----------------------------------------------------------------------------
def apply_to_existing(
    imap: ImapSession, criteria: Criteria, args, folder: str
) -> int:
    """Plan the existing-mail pass, show it, and run it if allowed.

    Planning is read-only, so the plan is always built first and shown
    whatever the flags say. ``--dry-run`` is simply the path that stops
    after showing it -- the decision to execute lives here, in the
    front-end, and never inside the IMAP layer.
    """
    print(f"\nSearching {args.folder!r} for existing matches...")

    plan = imap.plan_actions(
        criteria,
        source=args.folder,
        destination=folder,
        flags=collect_flags(args),
        discard=args.discard,
    )

    if plan.is_empty:
        print("No existing messages match.")

        return 0

    print(f"{plan.count} message(s) match:")
    print_preview(plan.messages)

    over_cap = plan.count > args.max_messages

    if args.dry_run:
        describe_plan(plan, prefix="[dry-run] would ")

        if over_cap:
            print(
                f"[dry-run] note: {plan.count} matches exceed "
                f"--max-messages {args.max_messages}; a real run would stop "
                f"and ask."
            )

        return 0

    if over_cap:
        # Processing the first N and reporting success would read as "it
        # handled everything". Refuse the whole batch instead, and say by
        # how much, so the choice to proceed is explicit.
        raise MxFilterError(
            f"{plan.count} message(s) match but --max-messages is "
            f"{args.max_messages}. NO existing message was touched -- a "
            f"partial batch is never processed, because handling the first "
            f"{args.max_messages} and reporting success would read as "
            f"having handled them all. Re-run with --max-messages "
            f"{plan.count} (or higher) to process every match. Note that "
            f"--yes does NOT lift this cap: it skips the confirmation "
            f"prompt, whereas the cap is a ceiling you set deliberately. "
            f"Any Sieve rule in this command was already uploaded and "
            f"applies to new mail regardless."
        )

    if not confirm(action_prompt(plan, args.move_threshold), args.yes):
        print("Aborted; no messages were touched.")

        return 0

    result = imap.execute(plan)

    report_result(result, plan)

    return result.moved or result.deleted or result.flagged


# ----------------------------------------------------------------------------
def describe_plan(plan, prefix: str = "") -> None:
    """Print what a plan does, in the caller's tense."""
    if plan.flags:
        print(f"{prefix}flag {plan.count} message(s): {', '.join(plan.flags)}")

    if plan.discard:
        print(
            f"{prefix}PERMANENTLY DELETE {plan.count} message(s) from "
            f"{plan.source!r}"
        )

    elif plan.moves:
        print(f"{prefix}move {plan.count} message(s) to {plan.destination!r}")


# ----------------------------------------------------------------------------
def report_result(result, plan) -> None:
    """Print what actually happened."""
    if result.flagged:
        print(
            f"Flagged {result.flagged} message(s) with {', '.join(plan.flags)}"
        )

    if result.deleted:
        print(f"Deleted {result.deleted} message(s) from {plan.source!r}")

    if result.moved:
        print(
            f"Moved {result.moved} message(s) from {plan.source!r} to "
            f"{plan.destination!r}"
        )


# ----------------------------------------------------------------------------
def action_prompt(plan, move_threshold: int) -> str:
    """Word the confirmation according to how reversible the action is.

    A MOVE is recoverable: the mail still exists, in another folder, and can
    be moved back. An EXPUNGE is not -- once the server drops it there is
    nothing to undo -- so deletion says so in as many words, and a large
    move says how large.
    """
    if plan.discard:
        return (
            f"PERMANENTLY DELETE {plan.count} message(s) from "
            f"{plan.source!r}? This cannot be undone"
        )

    if not plan.moves:
        return f"Flag {plan.count} message(s) in {plan.source!r}?"

    if plan.count > move_threshold:
        return (
            f"Move {plan.count} message(s) -- more than --move-threshold "
            f"{move_threshold} -- from {plan.source!r} to "
            f"{plan.destination!r}? (reversible: they can be moved back)"
        )

    return (
        f"Move {plan.count} message(s) from {plan.source!r} to "
        f"{plan.destination!r}?"
    )


# ############################################################################
# Subcommands
# ############################################################################


# ----------------------------------------------------------------------------
def cmd_list(args) -> int:
    """List the account's Sieve scripts."""
    config = configure(args)

    with SieveSession(config, progress=progress_for(args, "sieve")) as sieve:
        active, others = sieve.list_scripts()

        if not active and not others:
            print("No Sieve scripts on the server.")

            return 0

        if active:
            print(f"* {active}   (active)")

        for name in others:
            print(f"  {name}")

    return 0


# ----------------------------------------------------------------------------
def cmd_show(args) -> int:
    """Print a script's source."""
    config = configure(args)

    with SieveSession(config, progress=progress_for(args, "sieve")) as sieve:
        name = args.name or sieve.active_script_name()

        if not name:
            raise MxFilterError("no active script; name one explicitly")

        source = sieve.get_script(name)

        print(f"# ---- {name} ----")
        print(source, end="" if source.endswith("\n") else "\n")

        filters = parse_script(source)
        names = rule_names(filters)

        print(f"# ---- {len(names)} rule(s): {', '.join(names) or '(none)'}")

    return 0


# ----------------------------------------------------------------------------
def cmd_folders(args) -> int:
    """List IMAP folders and the detected hierarchy delimiter."""
    config = configure(args)

    with ImapSession(config, progress=progress_for(args, "imap")) as imap:
        print(f"Hierarchy delimiter: {imap.delimiter!r}")
        print(f"{len(imap.folders)} folder(s):")

        for folder in sorted(imap.folders):
            print(f"  {folder}")

    return 0


# ----------------------------------------------------------------------------
def cmd_test(args) -> int:
    """Connect to both services and report what they support."""
    config = configure(args)

    print(f"Host:      {config.host}")
    print(f"User:      {config.user}")
    print(f"Password:  {config.password_state()}")
    print(f"IMAP:      {config.imap_host}:{config.imap_port}")
    print(
        f"Sieve:     {config.host}:{config.sieve_port} "
        f"(tls={config.sieve_tls})"
    )

    with SieveSession(config, progress=progress_for(args, "sieve")) as sieve:
        capabilities = sieve.capabilities()

        print("\nManageSieve: connected")
        print(f"  extensions: {', '.join(sorted(capabilities)) or '(none)'}")

        # Every line below is read from this server's CAPABILITY response.
        # Nothing here asserts what MXRoute does or does not enable -- only
        # 'redirect' is a documented MXRoute policy, and a policy is not a
        # capability, so it would not show up here at all.
        advertised = {name.lower() for name in capabilities}

        print("\n  advertised extensions (from this server, not assumed):")

        for name in REPORTABLE_EXTENSIONS:
            state = "yes" if name in advertised else "not advertised"
            print(f"    {name:<12} {state}")

        active, others = sieve.list_scripts()
        print(f"\n  active script: {active or '(none)'}")
        print(f"  other scripts: {', '.join(others) or '(none)'}")
        print(
            "  (mxfilter always edits the ACTIVE script under its own "
            "name; it never guesses one.)"
        )

    with ImapSession(config, progress=progress_for(args, "imap")) as imap:
        capabilities = imap.capabilities()

        print("\nIMAP: connected")
        print(f"  delimiter: {imap.delimiter!r}")
        print(f"  folders:   {len(imap.folders)}")
        print(
            f"  MOVE:      "
            f"{'yes' if 'MOVE' in capabilities else 'no (COPY+EXPUNGE)'}"
        )
        print(f"  UIDPLUS:   {'yes' if 'UIDPLUS' in capabilities else 'no'}")

        report_filter_sieve(capabilities)

    print(
        "\nNote: MXRoute disables the Sieve 'redirect' action as a matter "
        "of policy (2024-03-21) -- use a panel forwarder, which handles "
        "SRS properly. That is the only MXRoute restriction mxfilter "
        "asserts; everything else above came from the server."
    )

    return 0


# ----------------------------------------------------------------------------
def report_filter_sieve(capabilities: list[str]) -> None:
    """Report whether the server can run Sieve retroactively itself.

    Dovecot's Pigeonhole ``imap_filter_sieve`` plugin advertises
    ``FILTER=SIEVE``, which lets a client ask the *server* to run a Sieve
    script over messages matching an IMAP search -- the retroactive pass
    done properly, server-side. It is experimental and off by default, so
    it is almost certainly absent here; one CAPABILITY line settles it
    either way, and an answer on the record beats an assumption.

    Detection only. mxfilter has no FILTER=SIEVE code path.
    """
    present = any(
        item.upper().startswith("FILTER=SIEVE") for item in capabilities
    )

    if present:
        print(
            "  FILTER=SIEVE: yes -- this server can apply a Sieve script "
            "to existing mail itself."
        )
        print(
            "                mxfilter still uses its own client-side pass; "
            "the server-side path is not implemented."
        )

    else:
        print(
            "  FILTER=SIEVE: no -- no server-side retroactive filtering "
            "(Dovecot imap_filter_sieve is not enabled)."
        )
        print(
            "                mxfilter's client-side search-and-move pass "
            "is the only option here."
        )


# ----------------------------------------------------------------------------
def cmd_add(args) -> int:
    """Add a rule to the active script, then apply it to existing mail."""
    reject_forbidden(args)

    config = configure(args)
    criteria = criteria_from_args(args)
    criteria.require_terms()

    return run_add(config, args, criteria)


# ----------------------------------------------------------------------------
def run_add(config, args, criteria: Criteria) -> int:
    """Shared body of ``add`` and ``from-message``."""
    need_imap = not args.no_imap

    with ExitStack() as stack:
        sieve, imap = open_sessions(config, args, need_imap, stack)

        folder = resolve_folder(args, imap, config)
        use_create = ensure_folder(folder, args, imap, sieve)

        actions = sieve_actions(args, folder, use_create)
        warn_missing_extensions(sieve, required_extensions(args, use_create))

        name = args.name or default_rule_name(criteria)
        script_name, before = fetch_active(sieve, args)

        after = merge_rule(
            before,
            name,
            criteria.sieve_conditions(),
            actions,
            matchtype=criteria.sieve_matchtype(),
            replace=args.replace,
        )

        print(f"\nRule {name!r} on script {script_name!r}:")
        print(f"  when:  {criteria.describe()}")
        print(f"  then:  {describe_actions(actions)}")

        diff = script_diff(before, after, script_name)

        print("\n--- sieve diff ---")
        print(diff if diff.strip() else "(no change)")
        print("--- end diff ---")

        if args.dry_run:
            print("\n[dry-run] the script was NOT uploaded.")

        else:
            upload(sieve, config, script_name, before, after, args)

        if imap is None or args.no_apply:
            if args.no_apply:
                print("\nSkipping the existing-mail pass (--no-apply).")

            return 0

        apply_to_existing(imap, criteria, args, folder)

    return 0


# ----------------------------------------------------------------------------
def describe_actions(actions: list[tuple]) -> str:
    """Render action tuples as a readable summary line.

    Sieve escaping is undone for display: the summary should say
    ``addflag \\Seen``, which is the flag the user asked for, rather than
    the ``\\\\Seen`` that has to appear in the script source. The diff
    printed underneath shows the real source, so nothing is hidden.
    """
    return "; ".join(
        " ".join(unescape_sieve_string(str(part)) for part in action)
        for action in actions
    )


# ----------------------------------------------------------------------------
def unescape_sieve_string(value: str) -> str:
    """Reverse ``escape_sieve_string`` for display purposes only."""
    return value.replace('\\"', '"').replace("\\\\", "\\")


# ----------------------------------------------------------------------------
def cmd_apply(args) -> int:
    """Apply criteria to existing mail only; touch no Sieve script."""
    reject_forbidden(args)

    config = configure(args)
    criteria = criteria_from_args(args)
    criteria.require_terms()

    with ImapSession(config, progress=progress_for(args, "imap")) as imap:
        folder = resolve_folder(args, imap, config)

        if not folder and not args.discard and not collect_flags(args):
            raise MxFilterError(
                "nothing to do -- use --fileinto, --discard, --mark-read, "
                "or --flag"
            )

        if folder and not imap.exists(folder):
            if not args.create_folder:
                raise MxFilterError(
                    f"target folder {folder!r} does not exist; pass "
                    f"--create-folder to create it"
                )

            if args.dry_run:
                print(f"[dry-run] would create IMAP folder {folder!r}")

            else:
                imap.create_folder(folder)
                print(f"Created IMAP folder {folder!r}")

        print(f"Criteria: {criteria.describe()}")

        apply_to_existing(imap, criteria, args, folder)

    return 0


# ----------------------------------------------------------------------------
def cmd_remove_rule(args) -> int:
    """Remove a named rule from the active script and re-upload."""
    config = configure(args)

    with SieveSession(config, progress=progress_for(args, "sieve")) as sieve:
        script_name, before = fetch_active(sieve, args)

        if not before.strip():
            raise MxFilterError(f"script {script_name!r} is empty")

        after = remove_rule(before, args.rule_name)
        diff = script_diff(before, after, script_name)

        print("--- sieve diff ---")
        print(diff if diff.strip() else "(no change)")
        print("--- end diff ---")

        if args.dry_run:
            print("\n[dry-run] the script was NOT uploaded.")

            return 0

        if not confirm(
            f"Remove rule {args.rule_name!r} from {script_name!r}?", args.yes
        ):
            print("Aborted; nothing was changed.")

            return 0

        upload(sieve, config, script_name, before, after, args)

    return 0


# ----------------------------------------------------------------------------
def cmd_from_message(args) -> int:
    """Derive criteria from an existing message, then behave like ``add``."""
    reject_forbidden(args)

    config = configure(args)

    if not args.uid and not args.search:
        raise MxFilterError("give either --uid N or --search EXPRESSION")

    with ImapSession(config, progress=progress_for(args, "imap")) as imap:
        uid = args.uid

        if uid is None:
            uids = imap.raw_search(args.folder, args.search)

            if not uids:
                raise MxFilterError(
                    f"no message in {args.folder!r} matched {args.search!r}"
                )

            if len(uids) > 1:
                warn(
                    f"{len(uids)} messages matched; using the most recent "
                    f"(uid {max(uids)})"
                )

            uid = max(uids)

        message = imap.fetch_message_headers(args.folder, uid)

    criteria = derive_criteria(message, args)

    print(f"Derived from uid {uid} in {args.folder!r}:")
    print(f"  {criteria.describe()}")

    return run_add(config, args, criteria)


# ----------------------------------------------------------------------------
def derive_criteria(message, args) -> Criteria:
    """Build criteria from a message's headers.

    The default order is deliberate: a List-Id identifies a mailing list far
    more reliably than a sender address does, so it wins when present.
    """
    criteria = Criteria(match=args.match, compare=args.compare)

    wanted = [
        item.strip().lower()
        for item in (args.derive or "auto").split(",")
        if item.strip()
    ]

    if wanted == ["auto"]:
        wanted = ["list-id"] if message.get("List-Id") else ["from"]

    for header in wanted:
        raw = message.get(header)

        if not raw:
            warn(f"message has no {header!r} header; skipping it")
            continue

        criteria.add(header, extract_value(header, raw))

    criteria.require_terms()

    return criteria


# ----------------------------------------------------------------------------
def extract_value(header: str, raw: str) -> str:
    """Reduce a header to the part worth matching on.

    An address header keeps only the address, and a List-Id keeps only the
    bracketed identifier -- the display name around either is cosmetic and
    changes between messages.
    """
    value = decode_header_value(raw).strip()

    if header in ("from", "to", "cc", "sender", "reply-to"):
        address = email.utils.parseaddr(value)[1]

        return address or value

    if header == "list-id":
        match = re.search(r"<([^>]+)>", value)

        return match.group(1) if match else value

    return value


# ############################################################################
# Argument parsing
# ############################################################################


# ----------------------------------------------------------------------------
def global_parser() -> argparse.ArgumentParser:
    """Flags accepted both before and after the subcommand.

    ``--verbose mxfilter add`` and ``mxfilter add --verbose`` should both
    work; people type the second. ``SUPPRESS`` is what makes that safe --
    without it the subparser's default would overwrite a value already set
    by the top-level parser, so passing the flag first would silently do
    nothing.
    """
    parser = argparse.ArgumentParser(add_help=False)

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=argparse.SUPPRESS,
        help="step-by-step progress",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="show a full traceback on failure (never prints credentials)",
    )

    return parser


# ----------------------------------------------------------------------------
def connection_parser() -> argparse.ArgumentParser:
    """Flags shared by every subcommand that connects to the server."""
    parser = argparse.ArgumentParser(add_help=False)

    group = parser.add_argument_group("connection")
    group.add_argument("--host", help="MXRoute server hostname")
    group.add_argument("--user", help="full email address (the username)")
    group.add_argument(
        "--password-cmd",
        dest="password_cmd",
        help="command whose stdout is the password (e.g. 'pass show mail')",
    )
    group.add_argument("--imap-host", dest="imap_host")
    group.add_argument("--imap-port", dest="imap_port", type=int)
    group.add_argument("--sieve-port", dest="sieve_port", type=int)
    group.add_argument(
        "--sieve-tls", dest="sieve_tls", choices=SIEVE_TLS_MODES
    )
    group.add_argument(
        "--backup-dir",
        dest="backup_dir",
        help="where pre-upload script backups are written",
    )

    return parser


# ----------------------------------------------------------------------------
def criteria_parser() -> argparse.ArgumentParser:
    """The criteria flags shared by add / apply / from-message."""
    parser = argparse.ArgumentParser(add_help=False)

    group = parser.add_argument_group("criteria")
    group.add_argument(
        "--from", dest="from_addr", action="append", metavar="VALUE"
    )
    group.add_argument("--to", action="append", metavar="VALUE")
    group.add_argument("--cc", action="append", metavar="VALUE")
    group.add_argument("--subject", action="append", metavar="VALUE")
    group.add_argument(
        "--list-id", dest="list_id", action="append", metavar="VALUE"
    )
    group.add_argument(
        "--header",
        action="append",
        metavar="NAME=VALUE",
        help="match an arbitrary header; repeatable",
    )
    group.add_argument(
        "--match",
        choices=MATCH_MODES,
        default="any",
        help="combine criteria with OR (any) or AND (all); default any",
    )
    group.add_argument(
        "--compare",
        choices=COMPARE_OPS,
        default="contains",
        help="comparison used for every criterion; default contains. "
        "Note that 'is' and 'matches' test the WHOLE header value, as "
        "Sieve does -- so --compare matches --from '*@list.org' will "
        "not match 'Name <a@list.org>'; write '*@list.org*'",
    )

    return parser


# ----------------------------------------------------------------------------
def action_parser() -> argparse.ArgumentParser:
    """The action flags shared by add / apply / from-message."""
    parser = argparse.ArgumentParser(add_help=False)

    group = parser.add_argument_group("actions")
    group.add_argument("--fileinto", metavar="FOLDER")
    group.add_argument("--discard", action="store_true")
    group.add_argument(
        "--mark-read",
        dest="mark_read",
        action="store_true",
        help="add the \\Seen flag",
    )
    group.add_argument(
        "--flag",
        action="append",
        metavar="NAME",
        help="add an IMAP flag, e.g. '\\Flagged' or a custom keyword",
    )
    group.add_argument("--keep", action="store_true")
    group.add_argument(
        "--no-stop",
        dest="no_stop",
        action="store_true",
        help="let later rules run too (omit the 'stop' action)",
    )
    group.add_argument(
        "--create-folder",
        dest="create_folder",
        action="store_true",
        help="create the target folder if it does not exist",
    )

    # Accepted only so the failure is a clear explanation rather than an
    # argparse "unrecognized arguments" message.
    group.add_argument("--redirect", metavar="ADDRESS", help=argparse.SUPPRESS)
    group.add_argument("--notify", metavar="TARGET", help=argparse.SUPPRESS)
    group.add_argument("--vacation", metavar="TEXT", help=argparse.SUPPRESS)

    return parser


# ----------------------------------------------------------------------------
def safety_parser() -> argparse.ArgumentParser:
    """Flags shared by every mutating subcommand."""
    parser = argparse.ArgumentParser(add_help=False)

    group = parser.add_argument_group("safety")
    group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="show the diff and the affected messages; change nothing",
    )
    group.add_argument(
        "--yes", action="store_true", help="skip confirmation prompts"
    )
    return parser


# ----------------------------------------------------------------------------
def mail_safety_parser() -> argparse.ArgumentParser:
    """Safety flags that only mean something for the existing-mail pass.

    Kept apart from ``safety_parser`` so a command that never touches mail
    -- ``remove-rule`` -- does not advertise flags that would do nothing.
    """
    parser = argparse.ArgumentParser(add_help=False)

    group = parser.add_argument_group("existing mail")
    group.add_argument(
        "--move-threshold",
        dest="move_threshold",
        type=int,
        default=DEFAULT_MOVE_THRESHOLD,
        help=f"treat a move of more than N messages as a large operation "
        f"in the confirmation prompt (default {DEFAULT_MOVE_THRESHOLD})",
    )
    group.add_argument(
        "--max-messages",
        dest="max_messages",
        type=int,
        default=DEFAULT_MAX_MESSAGES,
        help=f"refuse the existing-mail pass if more than N messages match, "
        f"rather than processing a partial batch "
        f"(default {DEFAULT_MAX_MESSAGES})",
    )

    return parser


# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """Construct the full argument parser."""
    common = global_parser()
    connection = connection_parser()
    criteria = criteria_parser()
    actions = action_parser()
    safety = safety_parser()
    mail_safety = mail_safety_parser()

    parser = argparse.ArgumentParser(
        prog="mxfilter",
        description="Manage MXRoute Sieve filters and apply them to "
        "existing mail.",
    )

    parser.add_argument(
        "--version", action="version", version=f"mxfilter {__version__}"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="step-by-step progress"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show a full traceback on failure (never prints credentials)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    listing = subparsers.add_parser(
        "list", parents=[common, connection], help="list Sieve scripts"
    )
    listing.set_defaults(handler=cmd_list)

    show = subparsers.add_parser(
        "show", parents=[common, connection], help="print a Sieve script"
    )
    show.add_argument("name", nargs="?", help="script name; default active")
    show.set_defaults(handler=cmd_show)

    folders = subparsers.add_parser(
        "folders", parents=[common, connection], help="list IMAP folders"
    )
    folders.set_defaults(handler=cmd_folders)

    test = subparsers.add_parser(
        "test",
        parents=[common, connection],
        help="check reachability, change nothing",
    )
    test.set_defaults(handler=cmd_test)

    add = subparsers.add_parser(
        "add",
        parents=[common, connection, criteria, actions, safety, mail_safety],
        help="add a rule and apply it to existing mail",
    )
    _add_rule_flags(add)
    add.set_defaults(handler=cmd_add)

    from_message = subparsers.add_parser(
        "from-message",
        parents=[common, connection, criteria, actions, safety, mail_safety],
        help="derive criteria from a message, then add the rule",
    )
    _add_rule_flags(from_message)
    from_message.add_argument("--uid", type=int, help="message UID to read")
    from_message.add_argument(
        "--search", help="IMAP search expression selecting one message"
    )
    from_message.add_argument(
        "--derive",
        default="auto",
        help="headers to derive from, comma separated (default: auto -- "
        "List-Id if present, else From)",
    )
    from_message.set_defaults(handler=cmd_from_message)

    apply_cmd = subparsers.add_parser(
        "apply",
        parents=[common, connection, criteria, actions, safety, mail_safety],
        help="apply criteria to existing mail only",
    )
    apply_cmd.add_argument(
        "--folder", default="INBOX", help="source folder; default INBOX"
    )
    apply_cmd.add_argument("--delimiter", help=argparse.SUPPRESS)
    apply_cmd.set_defaults(handler=cmd_apply, no_imap=False)

    remove = subparsers.add_parser(
        "remove-rule",
        parents=[common, connection, safety],
        help="remove a named rule from the active script",
    )
    remove.add_argument("rule_name", metavar="NAME")
    remove.add_argument("--script", help="script name; default active")
    remove.set_defaults(handler=cmd_remove_rule)

    return parser


# ----------------------------------------------------------------------------
def _add_rule_flags(parser: argparse.ArgumentParser) -> None:
    """Attach the rule-authoring flags shared by add and from-message."""
    group = parser.add_argument_group("rule")
    group.add_argument(
        "--name", help="rule name; derived from criteria if omitted"
    )
    group.add_argument("--script", help="script name; default active")
    group.add_argument(
        "--replace",
        action="store_true",
        help="overwrite an existing rule of the same name",
    )
    group.add_argument(
        "--no-apply",
        dest="no_apply",
        action="store_true",
        help="do not touch mail that has already been delivered",
    )
    group.add_argument(
        "--no-imap",
        dest="no_imap",
        action="store_true",
        help="skip IMAP entirely; implies --no-apply and guesses the "
        "folder delimiter",
    )
    group.add_argument(
        "--folder", default="INBOX", help="source folder; default INBOX"
    )
    group.add_argument(
        "--delimiter",
        help="folder delimiter to assume when --no-imap is used",
    )


# ############################################################################
# Entry point
# ############################################################################


# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """Parse arguments, dispatch, and turn failures into diagnostics."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "no_imap", False):
        args.no_apply = True

    try:
        return args.handler(args)

    except MxFilterError as exc:
        if args.debug:
            traceback.print_exc()

        print(f"mxfilter: {exc}", file=sys.stderr)

        return 1

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)

        return 130

    except Exception as exc:
        if args.debug:
            traceback.print_exc()

        print(
            f"mxfilter: unexpected {type(exc).__name__}: {exc} "
            f"(re-run with --debug for a traceback)",
            file=sys.stderr,
        )

        return 1
