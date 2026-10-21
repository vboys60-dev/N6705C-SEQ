"""PyInstaller entry for the windowed app (a build artefact, not importable)."""

import multiprocessing
import sys

from n6705c_seq.webgui import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
