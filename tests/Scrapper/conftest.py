# Puts the project root on sys.path so tests can import project
# packages (config, functions.QuesScrapper.ScrapethePDF, functions.QuesScrapper.ScrapePipeLIne) without installation.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
