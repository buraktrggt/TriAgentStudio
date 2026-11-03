"""Entry script used by PyInstaller and the launchers."""
import sys

from triagent.app import main

if __name__ == "__main__":
    sys.exit(main())
