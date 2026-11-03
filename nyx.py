#!/usr/bin/env python3
"""Run NYX without installing it:  python nyx.py [file.nyx]"""

import sys

from nyx.cli import main

if __name__ == "__main__":
    sys.exit(main())
