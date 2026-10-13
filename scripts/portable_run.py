"""VoltProof 便携版启动器（start.bat 调用）：薄包装，逻辑在 app.cli。

按 Ctrl+C（或关掉本窗口）即停止服务；产物与会话都保存在包目录 out/ 下。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
