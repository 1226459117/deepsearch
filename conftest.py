"""仓库根 conftest:确保 `tests` 包可导入,且 src 布局的 deepsearch 可解析。"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
