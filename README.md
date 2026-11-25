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

Python 3.10+ and [ngspice](https://ngspice.sourceforge.io/) are required. Put ngspice on your `PATH`, or set `VOLTPROOF_NGSPICE` to the executable.

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Click **Example**, then **Simulate**. You should see a real RC low-pass waveform. No model API key is needed for this.

The workbench defaults to Simplified Chinese. Use **中文 / English** in the header to switch; your choice is remembered.

After `pip install .` you can also run `voltproof`.

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

PCB export is experimental and optional. It needs KiCad 10.x and Freerouting; if they are missing, simulation still works.

VoltProof is a local, single-user tool. Bind it to `127.0.0.1`. Do not expose it to the public internet without authentication and isolation.

Netlist and model-written Python checks reject dangerous `.control` commands, include paths that resolve outside the simulation directory, and Python dunder attribute access. ngspice is started with `-n` (`--no-spiceinit`) and without API keys in its environment. These checks are not an operating-system sandbox.

## License

[Apache-2.0](LICENSE)
