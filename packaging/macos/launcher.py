"""Entry point of pianotake.app: opens the window (or handles CLI flags)."""
import sys

from pianotake.cli import main

# Old macOS versions pass a -psn_... process serial number to apps; argparse would choke on it.
sys.exit(main([a for a in sys.argv[1:] if not a.startswith("-psn_")]))
