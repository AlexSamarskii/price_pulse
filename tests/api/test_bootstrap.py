from __future__ import annotations

import os
import subprocess
import sys


def test_bootstrap_disables_ambient_config_discovery() -> None:
    script = """
import os
import sys
from types import ModuleType

def reject_discovery(*args, **kwargs):
    raise AssertionError('config discovery attempted')

dotenv = ModuleType('dotenv')
dotenv.load_dotenv = reject_discovery
dotenv.find_dotenv = reject_discovery
sys.modules['dotenv'] = dotenv

os.environ['PYTHON_DOTENV_DISABLED'] = '0'
import pricepulse
import pricepulse.collectors.probe
import pricepulse.collectors.synthesis
import pricepulse.runtime.settings
assert os.environ['PYTHON_DOTENV_DISABLED'] == '1'
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={key: value for key, value in os.environ.items() if key != "PYTHON_DOTENV_DISABLED"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
