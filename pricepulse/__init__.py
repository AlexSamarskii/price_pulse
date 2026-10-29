from __future__ import annotations

import os

# Disable ambient credential-file discovery in optional provider dependencies.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

__version__ = "0.1.0"
