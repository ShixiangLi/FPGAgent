"""Render actual VCD samples with matplotlib; callable as a standalone Python tool."""
import argparse
import json
from pathlib import Path


def read_vcd(path, max_events=500000):
    variables, scopes, samples = {}, [], {}
    now, scale, definitions, events = 0, "unknown", True, 0
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
                    variables.setdefault(code, {"name": ".".join(scopes + [name]), "width": width})
                    samples.setdefault(code, [])
                elif line.startswith("$enddefinitions"):
                    definitions = False
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
                    samples[code].append((now, value))
                    events += 1
                    if events > max_events:
                        raise ValueError("VCD exceeds 500000 events; reduce simulation trace scope or duration")
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
    fig, axes = plt.subplots(len(selected), 1, figsize=(14, max(3, len(selected)*1.1)), sharex=True, squeeze=False)
    summary = []
    for ax, (code, info) in zip(axes[:, 0], selected):
        points = samples[code]
        times = [p[0] for p in points] + [end]
        values = [int(p[1], 2) if all(c in "01" for c in p[1]) else float("nan") for p in points]
        ax.step(times, values + [values[-1]], where="post", linewidth=1)
        ax.set_ylabel(".".join(info["name"].split(".")[-2:]), rotation=0, ha="right", fontsize=8)
        ax.grid(alpha=.25)
        summary.append({**info, "events": len(points), "unknown_events": sum(any(c.lower() in "xz" for c in v) for _, v in points)})
    axes[-1, 0].set_xlabel(f"VCD ticks ({scale} per tick)")
    fig.suptitle("Actual XSIM waveform (unknown values shown as gaps)")
    fig.tight_layout()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for extension in ("png", "svg"):
        fig.savefig(output / ("waveform." + extension), dpi=150)
    plt.close(fig)
    (output / "waveform_summary.json").write_text(json.dumps({"source": str(Path(source).resolve()),
        "end_tick": end, "timescale": scale, "signals": summary, "display_limit": 16}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    render(args.input, args.output)
