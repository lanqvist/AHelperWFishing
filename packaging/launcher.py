"""PyInstaller entry script (kept outside the package so the package is imported only by name)."""

import sys

from ahelperw_fishing.app import main

if __name__ == '__main__':
    sys.exit(main())
