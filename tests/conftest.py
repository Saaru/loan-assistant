"""Test environment: isolated data directory, fixed business date, no simulated latency/failures.

Set here (before any test module imports the app) so tests never touch the demo database.
"""

import os
from pathlib import Path

os.environ["LA_SIMULATE"] = "0"
os.environ["LA_BUSINESS_DATE"] = "2026-10-25"
os.environ["LA_DATA_DIR"] = str(Path(__file__).parent / ".data")
