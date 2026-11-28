<div align="center">

# VoltProof

AI circuit answers, verified by real ngspice simulation.

[English](README.md) · [简体中文](README.zh-CN.md)

[![Version 0.4.0](https://img.shields.io/badge/version-0.4.0-0ea5e9)](https://github.com/Kevin-Y572/VoltProof)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB)](https://www.python.org/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

<img src="docs/demo.gif" alt="Load the built-in RC low-pass example and run an ngspice simulation" width="90%">

</div>

VoltProof is a local workbench that checks AI-generated circuits with real [ngspice](https://ngspice.sourceforge.io/) simulation. Describe a circuit in natural language, or draw it on a canvas. VoltProof writes a SPICE netlist, runs ngspice, and shows waveforms, measured values, and a schematic. If a target is missed, it can send the error back to the model and try again.

## Features

- Run every AI answer through ngspice, and inspect the waveforms and measured values
- Send check or simulation errors back to the model for another attempt
- Draw and simulate on the canvas with no API key, or describe a circuit in chat
- Sweep one component across several values and resimulate each point
- Export a self-contained HTML lab report and print it as PDF
- Use any OpenAI-compatible provider, including a local Ollama instance; keys stay on your machine

## Installation

VoltProof runs on Linux and Windows. You need Python 3.10+ and [ngspice](https://ngspice.sourceforge.io/). Put ngspice on your `PATH`, or set `VOLTPROOF_NGSPICE` to the executable (`/usr/bin/ngspice` on Linux, `ngspice.exe` on Windows).

### Linux

On Debian or Ubuntu, from a source checkout:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ngspice
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

You can also build a `.deb`. It installs VoltProof, its Python dependencies, a `voltproof` command, and a desktop entry. ngspice is a package dependency. KiCad, Java, and Freerouting are not inside the package.

```bash
python3 scripts/build_linux_deb.py
sudo apt install ./dist/voltproof_*.deb
voltproof
```

Build it on the machine that will install it, with the same Python minor version. The script is reproducible: `scripts/build_linux_deb.py`. Project data goes to `~/.local/share/voltproof`. Model settings go to `~/.config/voltproof/settings.json`. Step-by-step package and PCB notes are in [docs/LINUX.md](docs/LINUX.md).

### Windows

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

A Windows portable zip is a separate build: `python scripts/build_portable.py`.

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Click **Example**, then **Simulate**. You should see a real RC low-pass waveform. No model API key is needed for this.

The workbench defaults to Simplified Chinese. Use **中文 / English** in the header to switch; your choice is remembered.

After `pip install .` you can also run `voltproof`. The Linux `.deb` provides the same command.

| Page | Address |
| --- | --- |
| Workbench (canvas + chat) | http://127.0.0.1:8000 |
| Canvas only | http://127.0.0.1:8000/draw.html |
| Side-by-side comparison | http://127.0.0.1:8000/compare.html |

## Usage

**Draw and simulate.** Place parts on the left canvas, wire them, pick DC, transient, or AC analysis, and click Simulate. Dangling pins and a missing ground are flagged on the canvas. You can export a `.cir` netlist. The circuit is saved with the project.

**Ask the model.** In the chat, describe what you want, for example: “Design a bridge rectifier with capacitor filtering: 220 V / 50 Hz down to 12 V, ripple < 1 V.” Follow-ups continue from the last verified netlist. You can also type a request under the canvas; the result is previewed until you apply or discard it.

A model key is only needed for AI design. Set `VOLTPROOF_API_KEY` (or `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`), or enter it in the in-page model panel.

**What-if.** Sweep one component across several values and run a real simulation at each point.

**Reports.** Export a self-contained HTML lab report from a conversation, then print it as PDF.

## MCP

VoltProof includes a stdio MCP server with two tools: `design_circuit` (design and verify) and `verify_netlist` (verify a netlist as-is, or repair it when `repair=true`).

```json
{
  "mcpServers": {
    "voltproof": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "<VoltProof repo root>"
    }
  }
}
```

If you installed with pip, you can use `"command": "voltproof-mcp"`.

## PCB export (experimental)

The workbench, canvas, comparison page, and ngspice simulation do not need this. With the tools below installed, the full board-export chain runs on Linux and on Windows: preflight, mapping, KiCad board, Freerouting, SES import and zone fill, DRC and connectivity, then Gerber, drill, and BOM. If a tool is missing, **Generate PCB** stays disabled and the job stops before routing, with a setup hint.

This is experimental. An optional layout agent can propose footprint positions from the verified circuit, and a later bounded edit if routing or DRC fails. A deterministic legalizer checks every proposal. Freerouting still draws the copper. The model does not emit tracks or a `.kicad_pcb`, and a finished job is not a fab-ready board. If the model is off, unavailable, or rejected, the existing signal-flow placement is used. Ideal op-amps are not placed as-is: **Make an LM358 candidate** adds supply pins, re-simulates, and stores a new project candidate without changing the adopted circuit. Details and the offline check (`scripts/pcb_smoke.py --agent-offline`) are in [docs/LINUX.md](docs/LINUX.md).

| Tool | Required | How to install |
| --- | --- | --- |
| KiCad | 10.x. The current stable line is 10.0.8 from `ppa:kicad/kicad-10.0-releases`. | Linux: the `kicad` package plus `kicad-footprints` (`/usr/bin/kicad-cli`, pcbnew on `/usr/bin/python3`, footprints in `/usr/share/kicad/footprints`). Windows: the installer, or `VOLTPROOF_KICAD` set to the root that contains `bin/kicad-cli.exe` and `bin/python.exe`. |
| Java | 21 or newer | Linux: `openjdk-21-jre-headless`. `VOLTPROOF_JAVA` may point at the `java` executable. |
| Freerouting | JVM JAR `freerouting-2.1.0.jar` | Put it in `/opt/freerouting/` or set `VOLTPROOF_FREEROUTING` to the jar path. |

Do not use the Freerouting 2.5.0 native executable: it can crash and hang. JAR releases from 2.2 onward are built for Java 25. `freerouting-2.1.0.jar` is the JVM JAR that runs on Java 21.

Flatpak KiCad is not detected. Use the distro or PPA packages, or point `VOLTPROOF_KICAD` at a prefix with `bin/kicad-cli` and a Python that can import `pcbnew`.

Copy-paste setup for a Debian/Ubuntu machine, and the commands to run before a real board export, are in [docs/LINUX.md](docs/LINUX.md).

## Notes

If ngspice is not on your `PATH`:

```bash
# Windows (setx applies to new terminals only)
setx VOLTPROOF_NGSPICE "C:\path\to\ngspice-47\Spice64\bin\ngspice.exe"

# macOS / Linux
export VOLTPROOF_NGSPICE=/usr/bin/ngspice
```

The command-line pipeline is:

```bash
python -m app.pipeline "Design an RC low-pass filter with a 1 kHz cutoff"
```

VoltProof is a local, single-user tool. Bind it to `127.0.0.1`. Do not expose it to the public internet without authentication and isolation.

Netlist and model-written Python checks reject dangerous `.control` commands, include paths that resolve outside the simulation directory, and Python dunder attribute access. ngspice is started with `-n` (`--no-spiceinit`) and without API keys in its environment. These checks are not an operating-system sandbox.

## License

[Apache-2.0](LICENSE)
