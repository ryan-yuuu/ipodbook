"""Entry point: `python -m ipodbook` launches the GUI, with a `cli` escape hatch."""

import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "cli":
        from .cli import main as cli_main
        return cli_main(sys.argv[2:])
    from .gui.app import run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
