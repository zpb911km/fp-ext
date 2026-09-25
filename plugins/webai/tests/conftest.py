"""conftest — 让 `plugins.webai.*` 可导入（本目录不在 sys.path 上）。

与 `lib/tests/conftest.py` 同一思路：测试要导入**插件包**，而 pytest 只会把
测试文件所在目录加进 sys.path，故显式把 `public/`（`plugins` 的父目录）补进去。
路径按 __file__ 推算，不依赖调用者从哪个 cwd 起 pytest。
"""

import sys
from pathlib import Path

_PUBLIC = Path(__file__).resolve().parents[3]      # tests → webai → plugins → public
if str(_PUBLIC) not in sys.path:
    sys.path.insert(0, str(_PUBLIC))
