"""Private MCP configuration and cache, scoped to one indicator invocation."""
from contextlib import contextmanager
from pathlib import Path
import json
import sys
import tempfile

from app import config
from app.trace import VERDICTS_DIR, run_dir


@contextmanager
def indicator_mcp_config(number: int, run_id: str):
    """Только нужным индикаторам: официальный Гарант и локальное веб-чтение."""
    if number not in {3, 4, 10, 11}:
        yield None
        return
    root = run_dir(run_id) / "_garant"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"indicator{number}-", dir=root) as temporary:
        directory = Path(temporary).resolve()
        if number == 3:
            garant = {
                "type": "stdio", "command": sys.executable,
                "args": [str(config.PROJECT_ROOT / "app/mcp_garant.py"),
                         str(run_dir(run_id).resolve() / VERDICTS_DIR / "03.garant")],
                "env": {"P10_GARANT_CACHE_DIR": str(directory / "cache")},
            }
        else:
            garant = json.loads(config.GARANT_MCP_CONFIG.read_text(encoding="utf-8"))["mcpServers"]["garant"]
        web = {
            "type": "stdio", "command": sys.executable,
            "args": [str(config.PROJECT_ROOT / "app/mcp_garant_web.py")],
            "env": {"GARANT_TOKEN": ""},
        }
        path = directory / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"garant": garant, "garantweb": web}}), encoding="utf-8")
        path.chmod(0o600)
        yield str(path)
