"""
build_viz.py - run the simulator and build an interactive visualisation of the run.

    python build_viz.py                 # uses Main.java, writes visualisation.html
    python build_viz.py MyProg.java -o myprog.html

Open the .html file in any web browser. It needs no server: the whole recorded
run (every stack frame, heap object, register and system call) is embedded in the page.
Needs viz_template.html, jvm_sim.py and mini_javac.py in the same folder.
"""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from jvm_sim import Tracer, run  # noqa: E402


def build(source: str, out: str, fragment: str = None, jit_threshold: int = 2):
    with tempfile.TemporaryDirectory() as tmp:
        json_path = os.path.join(tmp, "trace.json")
        with contextlib.redirect_stdout(io.StringIO()):
            run(source, Tracer(record=True), jit_threshold, json_path)
        with open(json_path) as f:
            trace = json.load(f)

    data = json.dumps(trace, separators=(",", ":")).replace("</", "<\\/")
    with open(os.path.join(HERE, "viz_template.html"), encoding="utf-8") as f:
        page = f.read().replace("/*__TRACE__*/", data)

    if fragment:
        with open(fragment, "w", encoding="utf-8") as f:
            f.write(page)

    head, body = page.split('<div class="wrap">', 1)
    full = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            + head + '</head>\n<body>\n<div class="wrap">' + body + "\n</body>\n</html>\n")
    with open(out, "w", encoding="utf-8") as f:
        f.write(full)
    return trace


def main():
    p = argparse.ArgumentParser(description="Build an interactive visualisation of a Java program's run.")
    p.add_argument("source", nargs="?", default="Main.java")
    p.add_argument("-o", "--out", default="visualisation.html")
    p.add_argument("--fragment", help=argparse.SUPPRESS)
    p.add_argument("--jit-threshold", type=int, default=2)
    a = p.parse_args()
    trace = build(a.source, a.out, a.fragment, a.jit_threshold)
    print(f"Recorded {len(trace['events'])} steps. Program output: {trace['output'].strip()}")
    print(f"Open {os.path.abspath(a.out)} in your browser.")


if __name__ == "__main__":
    main()
