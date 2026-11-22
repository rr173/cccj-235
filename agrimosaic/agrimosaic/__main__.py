import os
import sys

# 支持以 `python3 -I .../agrimosaic/__main__.py` 方式运行：脚本目录在 sys.path[0]，
# 但隔离模式下仓库根不会自动加入，这里显式补入，保证包内绝对导入可用。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agrimosaic.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
