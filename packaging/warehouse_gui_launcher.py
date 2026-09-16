"""PyInstaller GUI发布入口，仅配置随包分发的Tcl/Tk资源路径。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    runtime_root = Path(sys.executable).resolve().parent / "runtime"
    os.environ["TCL_LIBRARY"] = str(runtime_root / "tcl8.6")
    os.environ["TK_LIBRARY"] = str(runtime_root / "tk8.6")

from warehouse_analysis.ui.app import main


if __name__ == "__main__":
    main()
