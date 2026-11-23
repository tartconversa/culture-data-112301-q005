"""应用命令行入口（兼容 python -I：显式注入仓库根目录后调用 app.cli）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
