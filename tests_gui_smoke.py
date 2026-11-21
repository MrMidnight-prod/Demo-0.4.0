"""Smoke test for the GUI code path using a fake tkinter (catches NameError/typos, runs every button + queue message)."""
import sys, types, threading, time, tempfile
from pathlib import Path
sys.path.insert(0, ".")
import rmcl

tmp = Path(tempfile.mkdtemp()); rmcl.ROOT = tmp
ALL, AFTER = [], []

class W:
    def __init__(self, *a, **k):
        ALL.append(self); self.opts = dict(k)
    def __getattr__(self, n):
        if n.startswith("__"): raise AttributeError(n)
        return lambda *a, **k: (0 if n in ("curselection",) and False else W())
    def curselection(self): return (0,)
    def __setitem__(self, k, v): self.opts[k] = v
    def __getitem__(self, k): return self.opts.get(k)
    def after(self, ms, fn=None, *a): AFTER.append(fn)
    def mainloop(self): pass
    def trace_add(self, mode, cb): pass

class Var:
    def __init__(self, value=None, **k): self._v = value
    def get(self): return self._v
    def set(self, v): self._v = v
    def trace_add(self, mode, cb): pass

tk = types.ModuleType("tkinter")
for n in ("Tk", "Frame", "Label", "Entry", "Checkbutton", "Listbox", "Scale", "Text"): setattr(tk, n, type(n, (W,), {}))
tk.StringVar = tk.BooleanVar = tk.IntVar = Var
ttk = types.ModuleType("tkinter.ttk")
for n in ("Style", "Combobox", "Progressbar", "Scrollbar"): setattr(ttk, n, type(n, (W,), {}))
fd = types.ModuleType("tkinter.filedialog"); fd.askopenfilename = lambda **k: str(tmp / "x.mrpack")
fnt = types.ModuleType("tkinter.font"); fnt.families = lambda: ["Segoe UI"]
tk.ttk, tk.filedialog, tk.font = ttk, fd, fnt
sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.filedialog": fd, "tkinter.font": fnt})

class FakeCore:  # no network, no processes
    def __init__(self, log, cancel): self.log, self.proc = log, None
    def resolve(self, v): return "1.20.1" if v == "latest" else v
    def vanilla_java(self, mc): return 17
    def ensure_java(self, r): return "java"
    def install_loader(self, ld, mc, java, want=None): return mc if ld == "vanilla" else f"{ld}-{mc}"
    def prepare(self, vid): return {"id": vid}, tmp / "x.jar"
    def install_mods(self, mc, ld, d, mods): self.log(f"mods {ld} {mods[:2]}")
    def launch(self, prof, jar, o, dry_run=False): self.log(f"LAUNCH {o['tier']} {o['ram']} dry={dry_run}")
    def import_mrpack(self, p): return {"name": "P", "mc": "1.20.1", "loader": "forge", "lver": "47.3.0"}
rmcl.Core = FakeCore
rmcl.get_json = lambda url: {"versions": [{"id": "1.20.1", "type": "release"}]}
rmcl.mr_search = lambda q, mc, ld: [("sodium", "Sodium", "fast")]

rmcl.subprocess.Popen = lambda *a, **k: None  # open_data(): no real xdg-open
rmcl.main()
assert AFTER, "poll was not scheduled"
btns = [w for w in ALL if w.__dict__.get("_cmd")]
assert len(btns) >= 8, len(btns)
time.sleep(1.5)                     # let hardware / version threads post their messages
for _ in range(3): AFTER[0]()       # poll: hw, versions, logs
for b in btns:                      # press every button
    b._cmd(); time.sleep(0.15); AFTER[0]()
time.sleep(0.5); AFTER[0]()
print("GUI SMOKE OK; buttons pressed:", len(btns))
