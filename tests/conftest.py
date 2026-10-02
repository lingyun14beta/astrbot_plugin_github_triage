"""pytest 公共设置：把插件目录与（若存在的）AstrBot 运行时加进 sys.path。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))  # 让 astrbot_plugin_github_triage 可导入

# 工具层用例需要 AstrBot 的 core 类；找不到时那组用例整组跳过。
# 常见位置：插件装在 <AstrBot>/data/plugins/<plugin> 下时，上三级就是 AstrBot 根目录。
_candidates = [
    os.environ.get("ASTRBOT_ROOT"),
    *(str(path) for path in PLUGIN_ROOT.parents[:4]),
]
for _candidate in _candidates:
    if _candidate and (Path(_candidate) / "astrbot" / "api").is_dir():
        sys.path.insert(0, _candidate)
        break
