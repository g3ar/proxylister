"""Top-level command dispatcher for the single ``proxytools`` entrypoint."""

import sys

from proxytools import __version__

COMMANDS = {
    "scan": "proxytools.commands.scan",
    "monitor": "proxytools.commands.monitor",
}


def show_help(stream=None):
    if stream is None:
        stream = sys.stdout
    print(
        """Usage: ./proxytools <command> [options]

Commands:
  scan       Find, check, and export working proxies
  monitor    Monitor proxies until they meet stability criteria
  help       Show this help

Run ./proxytools <command> --help for command-specific options.""",
        file=stream,
    )


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"help", "-h", "--help"}:
        show_help()
        return 0
    if args[0] == "--version":
        print(__version__)
        return 0
    command = args.pop(0)
    module_name = COMMANDS.get(command)
    if module_name is None:
        print(f"proxytools: unknown command: {command}\n", file=sys.stderr)
        show_help(sys.stderr)
        return 2

    from importlib import import_module

    module = import_module(module_name)
    if any(arg in {"-h", "--help"} for arg in args):
        return module.main(args)

    from proxytools.process_lock import AlreadyRunning, ProcessLock

    try:
        with ProcessLock(command):
            return module.main(args)
    except AlreadyRunning as error:
        print(f"proxytools: {error}", file=sys.stderr)
        return 1
