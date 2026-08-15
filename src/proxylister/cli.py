"""Top-level command dispatcher for the single ``proxylister`` entrypoint."""

import sys

from proxylister import __version__
from proxylister.about import format_about
from proxylister.config import ConfigError
from proxylister.process_lock import AlreadyRunning, ProcessLock

COMMANDS = {
    "list": "proxylister.commands.list",
    "monitor": "proxylister.commands.monitor",
}


def show_help(stream=None):
    if stream is None:
        stream = sys.stdout
    print(
        """Usage: ./proxylister <command> [options]

Commands:
  list       Find and print working proxies (default)
  monitor    Monitor proxies until they meet stability criteria
  help       Show this help

Options:
  --version  Show the installed ProxyLister version
  --about    Show project information and credits
  --clear    Remove local databases, environment, locks, and generated caches

Run ./proxylister <command> --help for command-specific options.""",
        file=stream,
    )


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"help", "-h", "--help"}:
        show_help()
        return 0
    if args and args[0] == "--version":
        print(__version__)
        return 0
    if args and args[0] == "--about":
        print(format_about())
        return 0
    if args and args[0] == "--abut":
        from proxylister._project_art import show

        show()
        return 0
    if args and args[0] == "--clear":
        from proxylister.cleanup import main as clear

        return clear()
    if args and not args[0].startswith("-") and args[0] not in COMMANDS:
        print(f"proxylister: unknown command: {args[0]}\n", file=sys.stderr)
        show_help(sys.stderr)
        return 2
    command = args.pop(0) if args and args[0] in COMMANDS else "list"
    module_name = COMMANDS.get(command)
    if module_name is None:
        print(f"proxylister: unknown command: {command}\n", file=sys.stderr)
        show_help(sys.stderr)
        return 2

    from importlib import import_module

    try:
        module = import_module(module_name)
        if any(arg in {"-h", "--help"} for arg in args):
            return module.main(args)
        with ProcessLock(command):
            from proxylister.geoip import ATTRIBUTION, configure_geoip, ensure_geoip_database

            print("Checking local GeoIP database…", file=sys.stderr)
            showed_progress = False
            last_progress = -1

            def show_geoip_progress(downloaded, total):
                nonlocal showed_progress, last_progress
                current_progress = (
                    downloaded * 100 // total if total else downloaded // (1024 * 1024)
                )
                if current_progress == last_progress:
                    return
                last_progress = current_progress
                showed_progress = True
                received = downloaded / (1024 * 1024)
                if total:
                    amount = f"{received:.1f}/{total / (1024 * 1024):.1f} MiB"
                else:
                    amount = f"{received:.1f} MiB"
                print(
                    f"\rDownloading GeoIP database… {amount}",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )

            geoip = ensure_geoip_database(progress=show_geoip_progress)
            if showed_progress:
                print(file=sys.stderr)
            configure_geoip(geoip.path)
            if geoip.updated:
                print(f"Updated {geoip.path.name}. {ATTRIBUTION}", file=sys.stderr)
            if geoip.warning:
                print(f"proxylister: {geoip.warning}", file=sys.stderr)
            return module.main(args)
    except ConfigError as error:
        print(f"proxylister: configuration error: {error}", file=sys.stderr)
        return 2
    except AlreadyRunning as error:
        print(f"proxylister: {error}", file=sys.stderr)
        return 1
