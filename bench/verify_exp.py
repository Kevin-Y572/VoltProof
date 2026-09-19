"""综合实验验收器 v2（测试仪器，非 agent 的一部分）。

修正点：直接手工解析 ASCII raw（已用 v(div)=5·sin(2π·1000t) 解析校验列对齐），
保留信号符号；非均匀时间轴先重采样到均匀网格再做谱分析。
对照《综合实验要求.docx》判定：频率 / 峰峰值 / 谐波构成 / 失真。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

EXP = Path(__file__).resolve().parent / "exp"


def parse_raw(path: Path):
    names, vals, sec = [], [], None
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = ln.strip()
        if s == "Variables:":
            sec = "n"
            continue
        if s == "Values:":
            sec = "v"
            continue
        if sec == "n":
            p = s.split()
            if len(p) >= 3 and p[0].isdigit():
                names.append(p[1])
        elif sec == "v" and s:
            p = s.split()
            if p[0].isdigit() and len(p) > 1:
                p = p[1:]          # 行首是点索引
            elif p[0].isdigit() and len(p) == 1:
                continue           # 裸索引行
            vals += [float(x) for x in p]
    arr = np.array(vals).reshape(-1, len(names))
    t = arr[:, 0]
    return {n: arr[:, j] for j, n in enumerate(names) if j > 0}, t


def resample(t: np.ndarray, y: np.ndarray, dt: float = 5e-6):
    tu = np.arange(t[0], t[-1], dt)
    return tu, np.interp(tu, t, y)


def analyze(y: np.ndarray, t: np.ndarray) -> dict:
    tu, yu = resample(t, y)
    i0 = int(len(tu) * 0.4)  # 去起始暂态
    tt, yy = tu[i0:], yu[i0:]
    n = len(yy)
    f = np.fft.rfftfreq(n, tu[1] - tu[0])
    a = np.abs(np.fft.rfft(yy - yy.mean())) * 2 / n
    k = int(np.argmax(a[1:]) + 1)
    f0, a0 = float(f[k]), float(a[k])
    top = sorted(zip(f, a), key=lambda x: -x[1])[:6]
    # 谐波失真：2..9 次谐波幅度平方和开方 / 基波幅度
    thd = float(np.sqrt(sum(
        a[np.argmin(np.abs(f - k * f0))] ** 2 for k in range(2, 10)))) / max(a0, 1e-12)
    return {
        "f0_Hz": round(f0, 1),
        "f0_amp": round(a0, 4),
        "vpp_V": round(float(np.ptp(yy)), 3),
        "mean_V": round(float(yy.mean()), 4),
        "thd_ratio": round(thd, 4),
        "top_peaks": [(round(ff, 0), round(aa, 4)) for ff, aa in top],
    }


def near(v, target, tol):
    return abs(v - target) <= tol * target


def harmonic_amp(s: dict, freq: float) -> float:
    return max((aa for ff, aa in s["top_peaks"] if near(ff, freq, 0.06)), default=0.0)


def main() -> None:
    report, verdicts = {}, {}
    for tdir in sorted(EXP.glob("exp*")):
        if not tdir.is_dir():
            continue
        raws = sorted(tdir.glob("*.raw"))
        if not raws:
            verdicts[tdir.name] = "无 raw 数据"
            continue
        sigs_raw, t = parse_raw(raws[-1])
        sigs = {n: analyze(y, t) for n, y in sigs_raw.items()}
        report[tdir.name] = {"raw": raws[-1].name, "n_signals": len(sigs), "signals": sigs}

        # 输出节点选择：排除激励/中间节点（osc/div），合成任务优先 sum/synth/tri/square 命名
        SRC_PAT = ("osc", "div")

        def outputs():
            named = [n for n in sigs if any(k in n.lower() for k in ("sum", "synth", "tri"))]
            pool = [n for n in sigs if not any(p in n.lower() for p in SRC_PAT)]
            return [sigs[n] for n in (named or pool)]

        def find(f_target, tol=0.06):
            c = [s for s in outputs() if near(s["f0_Hz"], f_target, tol)]
            return max(c, key=lambda x: x["f0_amp"]) if c else None

        v = {"checks": []}
        if tdir.name.startswith("exp1"):
            s1, s3 = find(1000), find(3000)
            v["checks"] += [
                ("存在1kHz正弦", s1 is not None, s1 and f"f0={s1['f0_Hz']}Hz"),
                ("1kHz峰峰值≈6V(±15%)", bool(s1 and near(s1["vpp_V"], 6, 0.15)),
                 s1 and f"实测vpp={s1['vpp_V']}V"),
                ("存在3kHz正弦", s3 is not None, s3 and f"f0={s3['f0_Hz']}Hz"),
                ("3kHz峰峰值≈2V(±15%)", bool(s3 and near(s3["vpp_V"], 2, 0.15)),
                 s3 and f"实测vpp={s3['vpp_V']}V"),
                ("1kHz失真THD<5%", bool(s1 and s1["thd_ratio"] < 0.05),
                 s1 and f"thd={s1['thd_ratio']}"),
                ("3kHz失真THD<5%", bool(s3 and s3["thd_ratio"] < 0.05),
                 s3 and f"thd={s3['thd_ratio']}"),
            ]
        elif tdir.name.startswith(("exp2", "exp3", "exp4")):
            s = find(1000)
            v["checks"].append(("存在合成输出(基波1kHz)", s is not None,
                                s and f"f0={s['f0_Hz']}Hz vpp={s['vpp_V']}V"))
            if s:
                a1 = s["f0_amp"]
                a3 = harmonic_amp(s, 3000)
                a5 = harmonic_amp(s, 5000)
                if tdir.name.startswith("exp2"):
                    v["checks"] += [
                        ("含明显3次谐波", a3 > 0.05 * a1, f"a3/a1={a3/max(a1,1e-9):.3f}(方波理想1/3)"),
                        ("幅度5V(文档口径歧义,从严vpp=5V)", near(s["vpp_V"], 5, 0.2),
                         f"实测vpp={s['vpp_V']}V"),
                    ]
                elif tdir.name.startswith("exp3"):
                    v["checks"] += [
                        ("含5次谐波成分", a5 > 0.05 * a1, f"a5/a1={a5/max(a1,1e-9):.3f}"),
                        ("奇次谐波构成1k/3k/5k",
                         a3 > 0.05 * a1 and a5 > 0.05 * a1,
                         f"a3/a1={a3/max(a1,1e-9):.3f} a5/a1={a5/max(a1,1e-9):.3f}"),
                    ]
                else:
                    v["checks"] = [
                        ("合成输出存在", s is not None, f"f0={s['f0_Hz']}Hz"),
                        ("谐波衰减接近1/n²(三角波特征,a3/a1≈0.111)",
                         bool(s and 0.04 < a3 / max(a1, 1e-9) < 0.20),
                         f"a3/a1={a3/max(a1,1e-9):.3f}(理想0.111) a5/a1={a5/max(a1,1e-9):.3f}(理想0.04)"),
                    ]
        verdicts[tdir.name] = {k: v[k] for k in ("checks",)} if "checks" in v else v

    for v in verdicts.values():
        if isinstance(v, dict) and "checks" in v:
            v["checks"] = [(n, bool(o), d) for n, o, d in v["checks"]]
    out = {"measurements": report, "verdicts": verdicts}
    (EXP / "verify_report.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    for task, data in report.items():
        print(f"========== {task} ({data['raw']}) ==========")
        for n, s in data["signals"].items():
            print(f"  {n:12s} f0={s['f0_Hz']:7.0f}Hz vpp={s['vpp_V']:7.3f}V "
                  f"thd={s['thd_ratio']:.3f} peaks={s['top_peaks'][:3]}")
    print()
    for task, v in verdicts.items():
        print(f"---- {task} ----")
        v["checks"] = [(n, bool(o), d) for n, o, d in v.get("checks", [])]
        for name, ok, detail in v["checks"]:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name} | {detail}")


if __name__ == "__main__":
    main()
