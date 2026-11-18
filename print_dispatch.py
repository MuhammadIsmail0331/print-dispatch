"""A dependency-free integrity check and packing slip for binary STL print jobs."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import json
import math
from pathlib import Path
import struct
import sys
import zipfile

VERSION = "0.1.0"
MAX_TRIANGLES = 200_000
NOTICE = "Checks cover digital handoff only, not strength, tolerances, support, material suitability or physical fit. STL units are declared by the sender, not inferred from the file."


class HandoffError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise HandoffError(message)


def positive_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def inspect_stl(path):
    """Stream binary triangles; join vertices at 1e-6 declared mm for edge checks."""
    with path.open("rb") as stream:
        header = stream.read(84)
        require(len(header) == 84, "STL is too short for a binary header")
        count = struct.unpack_from("<I", header, 80)[0]
        require(0 < count <= MAX_TRIANGLES, "Expected binary STL with 1–200,000 triangles (ASCII is not supported)")
        require(path.stat().st_size == 84 + 50 * count, "Binary triangle count does not match file length")
        digest = hashlib.sha256(header)
        lower, upper = [math.inf]*3, [-math.inf]*3
        edges, directions = Counter(), Counter()
        volumes, degenerate, anchor = [], 0, None
        for _ in range(count):
            record = stream.read(50)
            require(len(record) == 50, "STL changed or was truncated while reading")
            digest.update(record)
            values = struct.unpack("<12fH", record)
            triangle = [tuple(values[start:start+3]) for start in (3, 6, 9)]
            require(all(math.isfinite(x) for vertex in triangle for x in vertex), "STL contains NaN or infinite vertex coordinates")
            if anchor is None:
                anchor = triangle[0]
            keys = [tuple(round(x, 6) for x in vertex) for vertex in triangle]
            a, b, c = [tuple(v[i]-anchor[i] for i in range(3)) for v in triangle]
            cross = (b[1]*c[2]-b[2]*c[1], b[2]*c[0]-b[0]*c[2], b[0]*c[1]-b[1]*c[0])
            volumes.append(sum(a[i]*cross[i] for i in range(3))/6)
            ab = [triangle[1][i]-triangle[0][i] for i in range(3)]
            ac = [triangle[2][i]-triangle[0][i] for i in range(3)]
            area_cross = (ab[1]*ac[2]-ab[2]*ac[1], ab[2]*ac[0]-ab[0]*ac[2], ab[0]*ac[1]-ab[1]*ac[0])
            if len(set(keys)) != 3 or not any(area_cross):
                degenerate += 1
            for u, v in zip(keys, keys[1:]+keys[:1]):
                edge = tuple(sorted((u, v)))
                edges[edge] += 1
                directions[edge] += 1 if u < v else -1
            for vertex in triangle:
                for i, x in enumerate(vertex):
                    lower[i], upper[i] = min(lower[i], x), max(upper[i], x)
        require(not stream.read(1), "STL changed while reading")
    signed_volume = math.fsum(volumes)
    return {"triangles": count, "bounds_mm": [lower, upper],
            "size_mm": [round(upper[i]-lower[i], 6) for i in range(3)],
            "signed_volume_mm3": round(signed_volume, 6),
            "bad_edge_count": sum(n != 2 for n in edges.values()),
            "inconsistent_edge_count": sum(n != 0 for n in directions.values()),
            "degenerate_triangles": degenerate, "sha256": digest.hexdigest()}


def check_job(job_path):
    job_path = Path(job_path).resolve()
    require(job_path.stat().st_size <= 1_000_000, "Job manifest exceeds 1 MB")
    job = json.loads(job_path.read_text(encoding="utf-8"))
    require(isinstance(job, dict), "Job must be a JSON object")
    require(isinstance(job.get("title"), str) and job["title"].strip(), "A nonempty title is required")
    require(job.get("units") == "mm", "Explicit units='mm' is required; do not guess or silently rescale STL")
    bed = job.get("printer_mm")
    require(isinstance(bed, list) and len(bed) == 3 and all(positive_number(n) for n in bed), "printer_mm must be three positive finite numbers: X, Y, Z")
    parts = job.get("parts")
    require(isinstance(parts, list) and 1 <= len(parts) <= 100, "Provide 1–100 parts")
    result = {"tool": "Print Dispatch", "version": VERSION, "title": job["title"],
              "units": "mm", "printer_mm": bed, "parts": [], "errors": [], "notice": NOTICE}
    seen = set()
    for index, part in enumerate(parts, 1):
        require(isinstance(part, dict), f"Part {index} must be an object")
        name = part.get("file")
        require(isinstance(name, str) and name and "\\" not in name and ":" not in name, f"Part {index} needs a portable relative file path")
        relative = Path(name)
        require(not relative.is_absolute() and ".." not in relative.parts, f"Unsafe part path: {name}")
        source = (job_path.parent / relative).resolve()
        require(source.is_relative_to(job_path.parent), f"Part escapes job folder: {name}")
        require(source.suffix.lower() == ".stl" and source.is_file(), f"Missing STL: {name}")
        require(source not in seen, f"Duplicate STL entry: {name}; set quantity instead")
        seen.add(source)
        quantity = part.get("quantity")
        require(type(quantity) is int and 1 <= quantity <= 10_000, f"Quantity must be an integer from 1 to 10,000: {name}")
        material, notes = part.get("material"), part.get("notes", "")
        require(isinstance(material, str) and material.strip(), f"Material label required: {name}")
        require(isinstance(notes, str), f"Notes must be text: {name}")
        measured = inspect_stl(source)
        errors = []
        if measured["bad_edge_count"]: errors.append("open or non-manifold mesh edges")
        if measured["inconsistent_edge_count"]: errors.append("inconsistent triangle winding")
        if measured["degenerate_triangles"]: errors.append("degenerate triangles at 1e-6 mm vertex precision")
        if measured["signed_volume_mm3"] <= 0: errors.append("zero or inward signed volume")
        if any(size > limit + 1e-6 for size, limit in zip(measured["size_mm"], bed)): errors.append("exceeds printer envelope in supplied orientation")
        item = {"file": name, "quantity": quantity, "material": material, "notes": notes, **measured, "errors": errors}
        result["parts"].append(item)
        result["errors"].extend(f"{name}: {error}" for error in errors)
    result["total_pieces"] = sum(p["quantity"] for p in result["parts"])
    result["status"] = "checks_passed" if not result["errors"] else "needs_attention"
    return result


def render_html(report):
    esc = lambda value: html.escape(str(value), quote=True)
    cards = []
    for index, part in enumerate(report["parts"], 1):
        dimensions = " × ".join(f"{x:g}" for x in part["size_mm"])
        errors = "; ".join(part["errors"]) or "Closed edges · consistent winding · within envelope"
        cards.append(f'<article><div class="number">{index:02}</div><div><h2>{esc(part["file"])}</h2><p class="dimensions">{esc(dimensions)} <small>mm</small></p><p>{esc(part["notes"])}</p><p class="check">{esc(errors)}</p></div><div class="quantity">{part["quantity"]}<small>pcs · {esc(part["material"])}</small></div></article>')
    status = "DIGITAL CHECKS PASSED" if not report["errors"] else "NEEDS ATTENTION"
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(report['title'])} — Print Dispatch</title>
<style>:root{{--paper:#f3f0e7;--ink:#182d27;--line:#c8cec2;--accent:#cb593b}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 Georgia,serif}}main{{max-width:1050px;margin:auto;padding:48px 28px}}.kicker,.meta,.check,small,.number,footer{{font-family:'Courier New',monospace}}.kicker{{letter-spacing:.13em;text-transform:uppercase;font-size:12px}}header{{border-top:5px solid var(--ink);padding-top:20px}}h1{{font-size:clamp(36px,6vw,68px);line-height:1.05;font-weight:400;max-width:800px;margin:26px 0}}.meta{{display:flex;gap:26px;flex-wrap:wrap;padding:20px 0;border-block:1px solid var(--line);font-size:13px}}article{{display:grid;grid-template-columns:45px 1fr 135px;gap:20px;border-bottom:1px solid var(--line);padding:27px 0}}h2{{font-size:20px;margin:0;overflow-wrap:anywhere}}.number{{color:var(--accent);padding-top:5px}}p{{margin:8px 0}}.dimensions{{font-size:26px}}small{{display:block;font-size:12px;font-weight:400}}.dimensions small{{display:inline}}.quantity{{font-size:48px;text-align:right;line-height:1.1}}.check{{font-size:12px;color:#35594a}}.notice{{padding:22px;border-left:4px solid var(--accent);background:#e9e5d8;margin-top:30px}}footer{{font-size:12px;margin-top:32px}}a{{color:inherit}}@media(max-width:550px){{main{{padding:25px 18px}}article{{grid-template-columns:28px 1fr;gap:12px}}.quantity{{grid-column:2;text-align:left;font-size:32px}}.quantity small{{display:inline;margin-left:12px}}}}@media print{{main{{padding:0}}article{{break-inside:avoid}}}}</style>
<main><header><div class="kicker">Print Dispatch / workshop handoff</div><h1>{esc(report['title'])}</h1><div class="meta"><b>{status}</b><span>{report['total_pieces']} pieces / {len(report['parts'])} files</span><span>{esc(' × '.join(str(x) for x in report['printer_mm']))} mm printer</span></div></header>{''.join(cards)}<aside class="notice">{esc(report['notice'])}</aside><footer>Print Dispatch {VERSION} · supplied orientation · 100% scale · <a href="https://github.com/MuhammadIsmail0331/print-dispatch">Source &amp; usage</a><br>For a first article, print the trial quantity before approving the batch.</footer></main></html>'''


def write_pack(job_path, output):
    report = check_job(job_path)
    require(not report["errors"], "Packaging refused: " + "; ".join(report["errors"]))
    root = Path(job_path).resolve().parent
    output = Path(output).resolve()
    require(output.suffix.lower() == ".zip", "Output must be a .zip file")
    require(not output.exists(), "Output already exists; choose a new filename")
    payload = {"job.json": Path(job_path).read_bytes()}
    for part in report["parts"]:
        source = root / part["file"]
        require(output != source.resolve(), "Output cannot overwrite a source part")
        name = "parts/" + part["file"]
        data = source.read_bytes()
        require(hashlib.sha256(data).hexdigest() == part["sha256"], "STL changed after inspection")
        payload[name] = data
    # Rewrite only the packed manifest's relative paths; originals remain untouched.
    packed_job = json.loads(payload["job.json"])
    for part in packed_job["parts"]:
        part["file"] = "parts/" + part["file"]
    payload["job.json"] = (json.dumps(packed_job, indent=2)+"\n").encode()
    packed_report = json.loads(json.dumps(report))
    for part in packed_report["parts"]:
        part["file"] = "parts/" + part["file"]
    payload["report.json"] = (json.dumps(packed_report, indent=2)+"\n").encode()
    payload["START HERE.html"] = render_html(packed_report).encode()
    payload["SHA256SUMS.txt"] = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(payload.items())).encode()
    with output.open("xb") as stream, zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "pack"])
    parser.add_argument("job", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--html", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args(argv)
    try:
        require(args.command != "pack" or args.output is not None, "pack requires -o output.zip")
        for path in [args.report, args.html]:
            require(path is None or not path.exists(), "Refusing to overwrite report output; choose a new filename")
            require(path is None or args.output is None or path.resolve() != args.output.resolve(), "Report/HTML outputs cannot overwrite the ZIP output")
        require(args.report is None or args.html is None or args.report.resolve() != args.html.resolve(), "Report and HTML outputs must be different files")
        report = write_pack(args.job, args.output) if args.command == "pack" else check_job(args.job)
        if args.report: args.report.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
        if args.html: args.html.write_text(render_html(report), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0 if not report["errors"] else 1
    except (HandoffError, OSError, json.JSONDecodeError) as error:
        print(f"Print Dispatch: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
