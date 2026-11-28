"""在临时副本运行离线测试，避免清理真实项目、缓存或模型设置。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    repo = Path(__file__).resolve().parent.parent
    tests = sys.argv[1:] or ["test_offline.py", "test_pipeline_mock.py", "test_bench_validators.py", "test_mcp.py", "test_product.py", "test_pcb.py", "test_pcb_agent.py"]
    runs_dir = repo / ".test-results"
    runs_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="isolated-", dir=runs_dir) as tmp:
        root = Path(tmp)
        assert root.resolve().is_relative_to(runs_dir.resolve())
        # scripts/ 里有离线钩子 pcb_smoke.py。test_pcb_agent.py 会按仓库根
        # 调用它；不复制的话，隔离副本里这条路径不存在。
        for directory in ("app", "bench", "tests", "scripts"):
            shutil.copytree(repo / directory, root / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_llm_settings_test_*.json"))
        env = dict(os.environ)
        for key in ("VOLTPROOF_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "VOLTPROOF_WORKSPACE", "VOLTPROOF_ALLOW_PRIVATE_LLM", "VOLTPROOF_CACHE"):
            env.pop(key, None)
        env["VOLTPROOF_SETTINGS"] = str(root / "empty-settings.json")
        env["PYTHONIOENCODING"] = "utf-8"
        # 数值库线程帽（验收 2026-10-30 同款解法固化为默认）：OpenBLAS/MKL 多线程
        # 在本机曾致 SKiDL 构建/绘图内存分配失败与超时，隔离环境更敏感。
        for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS"):
            env.setdefault(key, "1")
        env["LOCALAPPDATA"] = str(root / "local-appdata")
        Path(env["LOCALAPPDATA"]).mkdir()
        env["TEMP"] = env["TMP"] = str(root / "tmp")
        Path(env["TEMP"]).mkdir()
        bundled = repo / "dist" / "VoltProof" / "ngspice" / "bin" / "ngspice.exe"
        if not env.get("VOLTPROOF_NGSPICE") and bundled.is_file():
            env["VOLTPROOF_NGSPICE"] = str(bundled)
        results = []
        for name in tests:
            print(f"Running {name}", flush=True)
            log = repo / ".test-results" / f"{name}.log"
            log.parent.mkdir(exist_ok=True)
            with log.open("w", encoding="utf-8") as output:
                result = subprocess.run([sys.executable, "-u", str(root / "tests" / name)],
                                        cwd=root, env=env, stdout=output,
                                        stderr=subprocess.STDOUT, timeout=480)
            print(f"{name}: exit={result.returncode}; {log}", flush=True)
            if result.returncode:
                print(log.read_text(encoding="utf-8")[-5500:], flush=True)
            results.append(result.returncode)
        return int(any(results))


if __name__ == "__main__":
    raise SystemExit(main())
