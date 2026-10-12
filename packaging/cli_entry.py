"""PyInstaller entry for the console app; the build's self-test runs it with --demo."""

import sys

from n6705c_seq.cli import main

if __name__ == "__main__":
    sys.exit(main())
