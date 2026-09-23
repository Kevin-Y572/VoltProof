"""工作区（Workspace）：用户指定的本地目录，agent 的所有读写都发生在其中。

交互模型：用户决定"在哪个目录工作"，进程锚定该目录，
产物与会话状态全部落在这里——拷走整个文件夹就是完整的项目归档。

目录布局：
    <工作区>/
    ├── sim/<task_id>/     每次运行独立：circuit.cir、*.raw、ngspice.log
    ├── docs/<task_id>/    wave.png、schematic.png、evidence.json
    └── .cp/               state.json（会话状态）+ cache/（证据缓存）

路径纪律（本模块的唯一红线）：对外服务的文件访问一律经 resolve() 守卫，
解析后必须仍在工作区根内，挡住 .. 逃逸、绝对路径与 symlink 跳出。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


class WorkspaceError(RuntimeError):
    """工作区路径非法（非绝对路径等）。"""


class Workspace:
    def __init__(self, root: str | Path):
        root = Path(root)
        if not root.is_absolute():
            raise WorkspaceError(f"工作区必须是绝对路径：{root}")
        self.root = root.resolve()

    # ------------------------------------------------------------------
    # 路径守卫
    # ------------------------------------------------------------------

    def resolve(self, rel: str | Path) -> Path:
        """唯一的对外路径出口：解析后必须仍在 root 内，否则 PermissionError。

        resolve() 会消解 .. 与 symlink；Windows 下盘符/大小写写法不一，
        用 normcase 前缀比较兜底（is_relative_to 按字面比较会误判）。"""
        p = Path(rel)
        candidate = (p if p.is_absolute() else self.root / p).resolve()
        if self._inside(candidate):
            return candidate
        raise PermissionError(f"路径越界（必须位于工作区内）：{rel}")

    def _inside(self, p: Path) -> bool:
        nc_root = os.path.normcase(str(self.root))
        nc_p = os.path.normcase(str(p))
        return nc_p == nc_root or nc_p.startswith(nc_root + os.sep)

    # ------------------------------------------------------------------
    # 目录（纯路径属性不建目录；建目录发生在 task_* / ensure / save）
    # ------------------------------------------------------------------

    @property
    def sim_dir(self) -> Path:
        return self.root / "sim"

    @property
    def docs_dir(self) -> Path:
        return self.root / "docs"

    @property
    def cp_dir(self) -> Path:
        return self.root / ".cp"

    @property
    def state_path(self) -> Path:
        return self.cp_dir / "state.json"

    @property
    def cache_dir(self) -> Path:
        return self.cp_dir / "cache"

    def ensure(self) -> None:
        """打开工作区时初始化内部目录（幂等）。"""
        self.cp_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------

    def new_task_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def task_sim_dir(self, task_id: str) -> Path:
        d = self.resolve(Path("sim") / task_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def task_docs_dir(self, task_id: str) -> Path:
        d = self.resolve(Path("docs") / task_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def list_tasks(self, limit: int = 100) -> list[dict]:
        """历史任务清单（目录名倒序，新任务在前）。条目来自 evidence.json，
        损坏/缺失时降级为仅列文件名。"""
        out: list[dict] = []
        base = self.docs_dir
        if not base.is_dir():
            return out
        for d in sorted((p for p in base.iterdir() if p.is_dir()),
                        key=lambda p: p.name, reverse=True):
            item = {"task_id": d.name}
            files = sorted(p.name for p in d.iterdir() if p.is_file())
            try:
                ev = json.loads((d / "evidence.json").read_text(encoding="utf-8"))
                item.update({
                    "ok": bool(ev.get("ok")),
                    "request": str(ev.get("request", ""))[:120],
                    "elapsed": ev.get("elapsed"),
                    "metrics": dict(list((ev.get("metrics") or {}).items())[:6]),
                    "files": files,
                })
            except (OSError, json.JSONDecodeError):
                item["files"] = files
            out.append(item)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------
    # 会话状态（持久化）
    # ------------------------------------------------------------------

    def load_sessions(self) -> dict[str, dict]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
                return data["sessions"]
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def save_sessions(self, sessions: dict[str, dict]) -> None:
        self.cp_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sessions": sessions}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.state_path)  # 原子替换，防写一半坏档


_REPO_ROOT = Path(__file__).resolve().parent.parent


def default_workspace() -> Workspace:
    """缺省工作区：仓库内 out/（bench/tests 等旧调用不传 workspace 时的落点）。"""
    return Workspace(_REPO_ROOT / "out")
