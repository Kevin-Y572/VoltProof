"""VoltProof 便携版启动器：自动挑空闲端口 → 起服务 → 开浏览器。

便携包根目录下的 start.bat 调用本脚本（py\\python.exe portable_run.py）。
按 Ctrl+C（或关掉本窗口）即停止服务；产物与会话都保存在包目录 out/ 下，
拷走整个文件夹就是完整归档。
"""

from __future__ import annotations

import socket
import threading
import time
import webbrowser


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def free_port(preferred: int = 8000) -> int:
    """优先用惯用端口；被占用（比如已开着一个 VoltProof）就顺延。"""
    if _port_free(preferred):
        return preferred
    for p in range(preferred + 1, preferred + 50):
        if _port_free(p):
            return p
    raise SystemExit(f"{preferred}-{preferred + 49} 端口都被占用，请释放后重试")


def main() -> None:
    import uvicorn

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(
        "app.main:app", host="127.0.0.1", port=port, log_level="warning"))

    def _open_browser() -> None:
        for _ in range(60):  # 最多等 15s 服务就绪再开页，避免白页
            if server.started:
                break
            time.sleep(0.25)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()
    print(f"VoltProof 已启动：{url}")
    print(f"  对话页     {url}")
    print(f"  同题对照   {url}/compare.html")
    print("关闭本窗口或按 Ctrl+C 停止服务。产物与会话保存在包目录 out/ 下。")
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
