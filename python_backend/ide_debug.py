"""Visual Studio startup adapter. CLI parsing and computation stay in drt_backend."""
import sys

from drt_backend.application.cli import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["--help"]))
