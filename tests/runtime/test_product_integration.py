from __future__ import annotations

import ast
import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _active_public_paths() -> list[Path]:
    paths = [PROJECT_ROOT / name for name in ("README.md", "ATTRIBUTION.md", "CHANGELOG.md", "pyproject.toml", "config.example.yaml")]
    for name in ("pricepulse", "scripts", "tests", "docs", "deploy"):
        for directory, subdirs, filenames in os.walk(PROJECT_ROOT / name, followlinks=False):
            subdirs[:] = [
                item for item in subdirs
                if not item.startswith(".") and item != "__pycache__"
                and not (Path(directory) / item).is_symlink()
                and (Path(directory) / item) != PROJECT_ROOT / "docs/upstream"
            ]
            paths.extend(
                Path(directory) / filename for filename in filenames
                if not filename.startswith(".")
                and Path(filename).suffix in {".py", ".md", ".html", ".js", ".css", ".yaml", ".yml", ".toml"}
            )
    return [path for path in paths if path.is_file() and not path.is_symlink()]


def test_pricepulse_product_integration() -> None:
    assert not (PROJECT_ROOT / "scrapeclaw").exists()
    assert (PROJECT_ROOT / "pricepulse/collectors/challenge_detector.py").is_file()
    import pricepulse
    import pricepulse.__main__
    from pricepulse.collectors.ast_checker import check_script_safety
    from pricepulse.collectors.challenge_detector import CaptchaDetector

    assert pricepulse.__version__ == "0.1.0"
    assert "name = \"pricepulse\"" in (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    han = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]")
    for path in _active_public_paths():
        source = path.read_text(encoding="utf-8")
        assert han.search(source) is None, path
        if path.suffix == ".py":
            for node in ast.walk(ast.parse(source)):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                assert not any(module.split(".")[0] == "scrapeclaw" for module in modules), path
    detector_source = (PROJECT_ROOT / "pricepulse/collectors/challenge_detector.py").read_text(encoding="utf-8")
    assert "\\u" in detector_source
    assert CaptchaDetector().detect_html("<h1>\u5b89\u5168\u9a8c\u8bc1</h1>") is not None
    assert check_script_safety("print('fixture')")[0]
    assert not check_script_safety("eval('1')")[0]
    assert "MIT License" in (PROJECT_ROOT / "LICENSE").read_text(encoding="utf-8")
