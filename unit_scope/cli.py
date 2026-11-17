"""Command line interface:  python -m unit_scope <command>

    list                       VISA resources
    idn                        *IDN? reply
    screenshot [--out DIR]     scope screen -> <datetime>.png        (default command)
    waveform  [-f csv|vcd|all] waveform data -> <datetime>.csv / .vcd
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import export
from . import __version__
from .errors import ScopeError
from .scope import UniTScope


def build_parser():
    ap = argparse.ArgumentParser(prog="unit_scope", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"unit_scope {__version__}")
    ap.add_argument("--resource", help="VISA resource string (default: first USB instrument)")
    ap.add_argument("--timeout", type=int, default=5000, help="I/O timeout in ms (default 5000)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="list USB VISA resources")
    sub.add_parser("idn", help="print the *IDN? reply")
    sp = sub.add_parser("screenshot", help="save the scope screen as PNG")
    sp.add_argument("--out", default="screenshots", help="output directory")
    wp = sub.add_parser("waveform", help="export waveform data")
    wp.add_argument("-f", "--format", choices=["csv", "vcd", "all"], default="csv",
                    help="csv (default), vcd (GTKWave/Surfer), or all")
    wp.add_argument("-c", "--channels", type=int, nargs="+", default=[1, 2], help="channels (default: 1 2)")
    wp.add_argument("--out", default="screenshots", help="output directory")
    wp.add_argument("--no-digital", action="store_true", help="VCD: omit the thresholded digital traces")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    cmd = args.cmd or "screenshot"
    try:
        if cmd == "list":
            for r in UniTScope.list_resources():
                print(r)
            return 0
        with UniTScope(args.resource, timeout_ms=args.timeout) as scope:
            if cmd == "idn":
                print(scope.idn())
            elif cmd == "screenshot":
                print(scope.save_screenshot(getattr(args, "out", "screenshots")))
            elif cmd == "waveform":
                cap = scope.capture(args.channels)
                if not cap.channels:
                    print("No enabled channels", file=sys.stderr)
                    return 1
                stem = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                for ext in ([".csv", ".vcd"] if args.format == "all" else [f".{args.format}"]):
                    opts = {"digital": not args.no_digital} if ext == ".vcd" else {}
                    print(export.export(cap, Path(args.out) / (stem + ext), **opts))
        return 0
    except ScopeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
