"""PyInstaller entry for the windowed app (a build artefact, not importable)."""

import multiprocessing

from n6705c_seq.gui import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
