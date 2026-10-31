"""Render actual VCD samples with matplotlib; callable as a standalone Python tool."""
import argparse
import json
from pathlib import Path


def read_vcd(path, max_events=500000):
    """Scan full evidence, retaining a bounded exact preview, never inventing samples."""
    if max_events < 1:
        raise ValueError("max_events must be positive")
    variables, scopes, samples = {}, [], {}
    now, scale, definitions, events = 0, "unknown", True, 0
    selected, preview_end = set(), None
    with Path(path).open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if definitions:
                parts = line.split()
                if line.startswith("$timescale"):
                    scale = line.replace("$timescale", "").replace("$end", "").strip()
                    if not scale:
                        scale = next(f).strip()
                elif line.startswith("$scope"):
                    scopes.append(parts[2])
                elif line.startswith("$upscope"):
                    scopes.pop()
                elif line.startswith("$var"):
                    width, code, name = int(parts[2]), parts[3], parts[4]
                    variables.setdefault(code, {"name": ".".join(scopes + [name]), "width": width,
                                                "events": 0, "unknown_events": 0})
                    samples.setdefault(code, [])
                elif line.startswith("$enddefinitions"):
                    definitions = False
                    selected = set(sorted(variables, key=lambda c: (variables[c]["name"].count("."), variables[c]["name"]))[:16])
                continue
            if line.startswith("#"):
                now = int(line[1:])
            elif line and not line.startswith("$"):
                if line[0] in "01xXzZ":
                    code, value = line[1:], line[0]
                elif line[0] in "bB":
                    parts = line[1:].split()
                    if len(parts) != 2:
                        continue
                    value, code = parts
                else:
                    continue
                if code in samples:
                    variables[code]["events"] += 1
                    variables[code]["unknown_events"] += any(c in "xXzZ" for c in value)
                    if code in selected:
                        if events < max_events:
                            samples[code].append((now, value))
                            events += 1
                        elif preview_end is None:
                            preview_end = now
    for info in variables.values():
        info.update(display_end_tick=now if preview_end is None else preview_end, preview_truncated=preview_end is not None)
    return variables, samples, now, scale


def render(source, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    variables, samples, end, scale = read_vcd(source)
    selected = [(code, v) for code, v in variables.items() if samples[code]]
    selected.sort(key=lambda item: (item[1]["name"].count("."), item[1]["name"]))
    selected = selected[:16]
    if not selected:
        raise ValueError("VCD contains no sampled signals")
    preview_end = selected[0][1]["display_end_tick"]
    truncated = selected[0][1]["preview_truncated"]
    fig, axes = plt.subplots(len(selected), 1, figsize=(14, max(3, len(selected)*1.1)), sharex=True, squeeze=False)
    summary = []
    for ax, (code, info) in zip(axes[:, 0], selected):
        points = samples[code]
        times = [p[0] for p in points] + [preview_end]
        values = [int(p[1], 2) if all(c in "01" for c in p[1]) else float("nan") for p in points]
        ax.step(times, values + [values[-1]], where="post", linewidth=1)
        ax.set_ylabel(".".join(info["name"].split(".")[-2:]), rotation=0, ha="right", fontsize=8)
        ax.grid(alpha=.25)
        summary.append({**info, "displayed_events": len(points)})
    axes[-1, 0].set_xlabel(f"VCD ticks ({scale} per tick)")
    title = "Actual XSIM waveform (unknown values shown as gaps)"
    if truncated:
        title += f"\nBounded preview: ticks 0..{preview_end}; full trace ends at {end}"
    fig.suptitle(title)
    fig.tight_layout()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for extension in ("png", "svg"):
        fig.savefig(output / ("waveform." + extension), dpi=150)
    plt.close(fig)
    (output / "waveform_summary.json").write_text(json.dumps({"source": str(Path(source).resolve()),
        "end_tick": end, "display_end_tick": preview_end, "preview_truncated": truncated,
        "note": "Counts cover the full trace. The plot is a bounded exact preview, not a full-duration functional or duty-cycle check.",
        "timescale": scale, "signals": summary, "display_limit": 16}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    render(args.input, args.output)
