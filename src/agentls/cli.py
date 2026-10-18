"""agentls: browse and manage local Claude Code, Codex and Cursor CLI sessions.

Run without arguments in a terminal to open the interactive browser.
Output is a table on a terminal and JSON otherwise (force with --json/--table).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from . import __version__, paths

EPILOG = """\
examples:
  agentls                                   open the session browser (TUI)
  agentls sessions list --agent codex --search refactor
  agentls sessions list --origin mcp --json
  agentls sessions inspect claude:1b2c3d4e --history 10
  agentls sessions open codex:<UUID> --dry-run
  agentls sessions archive claude:<UUID> cursor:<UUID>
  agentls sessions delete claude:<UUID> --dry-run      (preview, prints plan id)
  agentls sessions delete claude:<UUID> --yes --plan-id <ID>
  agentls sessions trash list

selectors are agent:UUID (inspect also accepts a unique ID prefix of 8+ chars).
"""

PROG = "agentls"
EXIT_PARTIAL = 3


def interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


class Parser(argparse.ArgumentParser):
    def error(self, message):
        if not sys.stdout.isatty():
            print(json.dumps({"error": message, "usage": True, "automatic_retry": False}), file=sys.stderr)
            self.exit(2)
        super().error(message)


def _formats(parser: argparse.ArgumentParser, depth: int) -> None:
    group = parser.add_mutually_exclusive_group()
    for flag in ("json", "table"):
        group.add_argument("--" + flag, dest=f"output_formats_{depth}", action="append_const", const=flag,
                           default=argparse.SUPPRESS,
                           help=f"{flag} output (default: table on a terminal, JSON otherwise)")


def _selector(parser, nargs=None, help_text="agent:UUID"):
    parser.add_argument("session", nargs=nargs, metavar="SESSION", help=help_text)
    parser.add_argument("--agent", choices=paths.AGENTS, help="agent, when SESSION is a bare UUID")


def build_parser() -> argparse.ArgumentParser:
    fmt = argparse.RawDescriptionHelpFormatter
    p = Parser(prog=PROG, description=__doc__, epilog=EPILOG, formatter_class=fmt)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    groups = p.add_subparsers(dest="group", metavar="COMMAND")
    shell = groups.add_parser("shell-init", help="print opt-in shell integration (source the output)")
    shell.add_argument("shell", choices=("bash",))

    s = groups.add_parser("sessions", help="native local sessions (browse, list, open, archive, delete)",
                          description="Native Claude Code, Codex and Cursor CLI sessions on this account.")
    sa = s.add_subparsers(dest="action", metavar="ACTION", required=True)
    sa.add_parser("browse", help="open the interactive session browser (TUI)")

    ls = sa.add_parser("list", help="list sessions with filters",
                       description="List sessions. JSON last_used_model is the last known native model, never "
                                   "a configured default (null if unknown). Archived sessions are excluded "
                                   "unless requested.")
    ls.add_argument("--agent", action="append", choices=paths.AGENTS, help="repeatable")
    ls.add_argument("--origin", choices=("mcp", "direct", "unknown"),
                    help="mcp = verified MCP provenance record; direct = none; unknown = unverifiable")
    ls.add_argument("--status", choices=("active", "uncertain", "recent", "idle"))
    ls.add_argument("--project", help="exact project directory")
    ls.add_argument("--mode", choices=("interactive", "noninteractive", "unknown"),
                    help="native launch mode (says nothing about MCP origin)")
    ls.add_argument("--type", choices=("main", "subagent", "unknown"))
    ls.add_argument("--search", "-s", default="", help="words that must all appear (title, project, ID, model)")
    ls.add_argument("--sort", choices=("updated", "title", "agent", "project"), default="updated")
    ls.add_argument("--reverse", action="store_true")
    ls.add_argument("--archived", choices=("hide", "include", "only"), default="hide")
    ls.add_argument("--native-archived", action="store_true", help="include sessions archived by `codex archive`")
    ls.add_argument("--all", action="store_true", help="same as --archived include --native-archived")
    ls.add_argument("--limit", type=int, default=0, help="page size (0 = all)")
    ls.add_argument("--offset", type=int, default=0)

    ins = sa.add_parser("inspect", help="show one session's details (and recent history)")
    _selector(ins, help_text="agent:UUID or a unique ID prefix (8+ chars)")
    ins.add_argument("--history", type=int, default=0, metavar="N", help="include the last N messages")

    for name, aliases, text in (("open", ["resume", "promote"], "resume in the native CLI (interactive terminal)"),
                                ("fork", [], "fork into a new native session (codex, claude)")):
        cmd = sa.add_parser(name, aliases=aliases, help=text)
        _selector(cmd)
        cmd.add_argument("--dry-run", action="store_true", help="preflight only; print the command")

    rn = sa.add_parser("rename", help="set a display alias (native title unchanged)")
    _selector(rn)
    rn.add_argument("name", nargs="?", help="new alias")
    rn.add_argument("--clear", action="store_true", help="remove the alias")

    for name, text in (("archive", "hide sessions from default listings (reversible; files untouched)"),
                       ("unarchive", "show archived sessions again")):
        _selector(sa.add_parser(name, help=text), nargs="+")

    de = sa.add_parser("delete", help="native delete into a restorable trash (preview first)",
                       description="Move sessions to the agentls trash (Claude, Cursor) or archive them "
                                   "with `codex archive` (Codex). Active or recently modified sessions are refused. "
                                   "Without --yes this is a preview.")
    _selector(de, nargs="+")
    de.add_argument("--dry-run", action="store_true", help="preview only (the default without --yes)")
    de.add_argument("--yes", action="store_true", help="perform the move; requires --plan-id from a preview")
    de.add_argument("--plan-id", help="plan ID printed by the preview; refuses if anything changed")
    de.add_argument("--allow-uninspectable", action="store_true",
                    help="HIGH RISK: proceed although some of your processes cannot be inspected for open "
                         "session files (part of the plan ID; preview and --yes must both use it)")

    tr = sa.add_parser("trash", help="list, restore or purge deleted sessions")
    ta = tr.add_subparsers(dest="trash_action", metavar="TRASH_ACTION", required=True)
    ta.add_parser("list", help="trash batches and natively archived Codex sessions")
    rs = ta.add_parser("restore", help="restore a trash batch, or codex:UUID archived natively")
    rs.add_argument("target", help="batch ID or codex:UUID")
    rs.add_argument("--session", action="append", metavar="SELECTOR", help="only these sessions of the batch")
    rs.add_argument("--dry-run", action="store_true")
    pg = ta.add_parser("purge", help="permanently remove one trash batch")
    pg.add_argument("batch")
    pg.add_argument("--yes", action="store_true", help="required")

    def add_formats(parser, depth=0):
        _formats(parser, depth)
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in set(action.choices.values()):
                    add_formats(child, depth + 1)
    add_formats(p)
    return p


# -- helpers ------------------------------------------------------------------

class Partial(Exception):
    """Command completed but some items failed or were blocked."""

    def __init__(self, result):
        super().__init__("partial")
        self.result = result


def _catalog(probe_activity=True):
    from .catalog import Catalog
    return Catalog().refresh(probe_activity=probe_activity)


def _resolve_many(catalog, args):
    return [catalog.resolve(value, args.agent) for value in args.session]


def _confirm(prompt: str, word: str) -> bool:
    try:
        return input(f"{prompt} Type '{word}' to confirm: ").strip() == word
    except EOFError:
        return False


# -- dispatch -----------------------------------------------------------------

def dispatch(args):
    """Return (render_kind, result)."""
    group, action = args.group, getattr(args, "action", None)
    if group == "sessions":
        return _sessions(args, action)
    raise ValueError("Unknown command.")


def _sessions(args, action):
    if action == "list":
        from .catalog import Query
        catalog = _catalog()
        q = Query(agents=tuple(args.agent or ()), origin=args.origin, status=args.status,
                  project=os.path.abspath(os.path.expanduser(args.project)) if args.project else None,
                  search=args.search, archived="include" if args.all else args.archived,
                  native_archived=args.native_archived or args.all, sort=args.sort, reverse=args.reverse)
        rows = [s for s in catalog.query(q)
                if (not args.mode or s.mode == args.mode) and (not args.type or s.session_type == args.type)]
        offset = max(0, args.offset)
        page = rows[offset:offset + args.limit] if args.limit > 0 else rows[offset:]
        nxt = offset + len(page) if offset + len(page) < len(rows) else None
        summary = catalog.summary()
        return "sessions", {"sessions": [s.to_json() for s in page], "total": len(rows), "offset": offset,
                            "next_offset": nxt, "errors": summary["errors"], "warnings": summary["warnings"],
                            "provenance": {"state": summary["provenance"], "note": summary["provenance_note"]}}
    if action == "inspect":
        catalog = _catalog()
        session = catalog.resolve(args.session, args.agent, allow_prefix=True)
        row = session.to_json()
        from .trash import METHODS, SUPPORT_NOTES
        row["delete_support"] = {"method": METHODS[session.agent], "note": SUPPORT_NOTES[session.agent]}
        if args.history > 0:
            from .scanners import history
            try:
                row["history"] = history(session)[-args.history:]
            except Exception as error:
                row["history_error"] = str(error)
        return "details", row
    if action in ("open", "resume", "promote", "fork"):
        from . import native
        session = _catalog().resolve(args.session, args.agent)
        plan = native.plan(session, "fork" if action == "fork" else "open")
        if args.dry_run:
            return "details", plan.to_json()
        if not interactive_terminal():
            raise ValueError("Opening a session needs an interactive terminal; use --dry-run for the plan.")
        native.recheck(plan, session)
        native.exec_(plan)  # does not return
    if action == "rename":
        catalog = _catalog(probe_activity=False)
        session = catalog.resolve(args.session, args.agent)
        if args.clear == bool(args.name):
            raise ValueError("Give a NAME, or --clear to remove the alias.")
        catalog.overlays.update([session.key], alias=None if args.clear else args.name)
        return "details", {"selector": session.selector, "alias": None if args.clear else " ".join(args.name.split()),
                           "native_title": session.title, "native_changed": False}
    if action in ("archive", "unarchive"):
        catalog = _catalog(probe_activity=False)
        sessions = _resolve_many(catalog, args)
        catalog.overlays.update([s.key for s in sessions], archived=action == "archive")
        return "details", {"archived" if action == "archive" else "unarchived": [s.selector for s in sessions],
                           "native_changed": False}
    if action == "delete":
        return _delete(args)
    if action == "trash":
        return _trash(args)
    raise ValueError("Unknown sessions action.")


def _delete(args):
    from . import trash
    catalog = _catalog()
    sessions = _resolve_many(catalog, args)
    plan = trash.build_plan(sessions, catalog.activity, allow_uninspectable=args.allow_uninspectable)
    preview = plan.to_json()
    if args.dry_run or not args.yes:
        if args.dry_run or not interactive_terminal() or not plan.allowed:
            extra = " --allow-uninspectable" if args.allow_uninspectable else ""
            preview["next"] = (f"{PROG} sessions delete {' '.join(args.session)}{extra} --yes --plan-id "
                               f"{plan.plan_id}") if plan.allowed else "Nothing can be deleted."
            return "plan", preview
        from .output import render
        print(render("plan", preview, shutil.get_terminal_size((100, 24)).columns))
        if not _confirm(f"Move {len(plan.allowed)} session(s) as shown?", "delete"):
            raise ValueError("Cancelled; nothing was changed.")
    elif not args.plan_id:
        raise ValueError("--yes requires --plan-id from a preview (run with --dry-run first).")
    result = trash.execute(plan, args.plan_id)
    if result["failed"] or result["skipped"]:
        raise Partial(result)
    return "details", result


def _trash(args):
    from . import trash
    if args.trash_action == "list":
        batches = trash.list_batches()
        from .catalog import Query
        archived = [s for s in _catalog(probe_activity=False).query(Query(agents=("codex",), native_archived=True,
                                                                            archived="include"))
                    if s.native_archived]
        if archived:
            batches.append({"batch_id": "(codex native archive)", "created": None,
                            "items": [{"agent": s.agent, "session_id": s.session_id, "title": s.display_title,
                                       "method": "codex-archive", "status": "archived"} for s in archived]})
        return "batches", batches
    if args.trash_action == "restore":
        if ":" in args.target:
            session = _catalog(probe_activity=False).resolve(args.target)
            if args.dry_run:
                return "details", {"would_restore": [session.selector], "method": "codex unarchive"}
            return "details", trash.restore_native_codex(session)
        result = trash.restore(args.target, args.session, args.dry_run)
        if result["failed"]:
            raise Partial(result)
        return "details", result
    if not args.yes:
        raise ValueError("Purging is permanent; add --yes.")
    return "details", trash.purge(args.batch)


# -- entry point --------------------------------------------------------------

def _emit_error(message: str, extra: dict | None = None) -> None:
    if sys.stderr.isatty() and sys.stdout.isatty():
        print(f"{PROG}: error: {message}", file=sys.stderr)
    else:
        print(json.dumps({"error": message, "automatic_retry": False, **(extra or {})}), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    if not argv:
        if interactive_terminal():
            return _run_tui()
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if args.group == "shell-init":
        from .shell import bash_init
        print(bash_init(), end="")
        return 0
    if not args.group:
        parser.print_help()
        return 0
    if args.group == "sessions" and args.action == "browse":
        if not interactive_terminal():
            _emit_error("The session browser needs an interactive terminal; try `sessions list`.")
            return 2
        return _run_tui()
    formats = [v for k, values in vars(args).items() if k.startswith("output_formats_") for v in values]
    if len(set(formats)) > 1:
        parser.error("--json and --table cannot be combined.")
    as_table = formats[-1] == "table" if formats else sys.stdout.isatty()
    code = 0
    try:
        kind, result = dispatch(args)
    except Partial as partial:
        kind, result, code = "details", partial.result, EXIT_PARTIAL
    except KeyboardInterrupt:
        _emit_error("Interrupted.")
        return 130
    except Exception as error:  # noqa: BLE001 - every failure becomes one clear message
        known = (OSError, ValueError, KeyError, RuntimeError)
        message = str(error.args[0]) if isinstance(error, KeyError) and error.args else str(error)
        if not isinstance(error, known):
            message = f"{type(error).__name__}: {error}"
        _emit_error(message)
        return 1
    text = None
    if as_table:
        try:
            from .output import render
            color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
            text = render(kind, result, shutil.get_terminal_size((100, 24)).columns, color)
        except Exception:
            text = None  # presentation must not turn a completed action into a failure
    print(text if text is not None else json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return code


def _run_tui() -> int:
    try:
        from .tui.app import run
    except ImportError as error:
        _emit_error(f"The session browser is unavailable ({error}). Headless commands still work.")
        return 1
    return run()
