"""One-off: add the keep-running note to the launcher notebook's intro cell."""
import json
import pathlib

path = pathlib.Path(__file__).resolve().parent.parent / "colab_launcher.ipynb"
nb = json.loads(path.read_text(encoding="utf-8"))

note = [
    "\n",
    "\u23f3 **Keep the last cell running** \u2014 it holds the Colab session open (it also injects a\n",
    "keep-alive so free runtimes don't idle-disconnect while you use the Gradio link from\n",
    "another tab). Interrupt the cell twice to shut down.",
]
intro = nb["cells"][0]
joined = "".join(intro["source"])
if "Keep the last cell running" not in joined:
    intro["source"] = intro["source"] + note

path.write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("notebook updated,", len(nb["cells"]), "cells")
