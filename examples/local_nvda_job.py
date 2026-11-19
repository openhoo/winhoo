"""Source-checkout entry point; the installed command is winhoo-nvda-job."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from winhoo_macos.nvda_job import main

if __name__ == '__main__':
    sys.exit(main())
