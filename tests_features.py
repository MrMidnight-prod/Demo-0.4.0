"""Offline tests for hardware profiles, loaders, Modrinth dependency resolution and .mrpack import."""
import sys, json, tempfile, zipfile, threading, hashlib
from pathlib import Path
sys.path.insert(0, ".")
import rmcl

tmp = Path(tempfile.mkdtemp()); rmcl.ROOT = tmp
logs = []
core = rmcl.Core(logs.append, threading.Event())

# ---------- hardware classification (the author's laptop and a few others)
mine = {"cpu_name": "Intel(R) Core(TM) i3-3110M", "cores": 2, "threads": 4, "ram_mb": 5900,
        "gpus": ["Intel(R) HD Graphics 4000", "NVIDIA GeForce GT 720M"]}
assert rmcl.classify(mine) == "lowend"
assert rmcl.auto_ram(mine, "lowend") == 2560, rmcl.auto_ram(mine, "lowend")
mid = {"cores": 4, "threads": 8, "ram_mb": 8192, "gpus": ["NVIDIA GeForce GTX 1650"]}
assert rmcl.classify(mid) == "normal" and rmcl.auto_ram(mid, "normal") == 4096
big = {"cores": 8, "threads": 16, "ram_mb": 32000, "gpus": ["NVIDIA GeForce RTX 4070"]}
assert rmcl.classify(big) == "high" and rmcl.auto_ram(big, "high") == 8192
assert rmcl.classify({"cores": 8, "threads": 16, "ram_mb": 32000, "gpus": ["Intel(R) UHD Graphics 620"]}) == "lowend"
assert rmcl.classify({"cores": 6, "threads": 12, "ram_mb": 16000, "gpus": ["Intel(R) UHD Graphics", "NVIDIA GeForce RTX 3060"]}) == "high"
for w in ["Intel(R) HD Graphics 4000", "NVIDIA GeForce GT 720M", "NVIDIA GeForce GT 1030", "Microsoft Basic Render Driver",
          "AMD Radeon HD 6520G", "NVIDIA GeForce MX130", "AMD Radeon(TM) R5 Graphics"]:
    assert rmcl.gpu_is_weak(w), w
for ok in ["NVIDIA GeForce GTX 1660", "NVIDIA GeForce RTX 3060", "AMD Radeon RX 6600", "Intel(R) Arc(TM) A770"]:
    assert not rmcl.gpu_is_weak(ok), ok
assert rmcl.auto_ram({"ram_mb": 2048}, "lowend") == 1024  # never below 1 GB
assert rmcl.preset_mods("forge", "lowend")[0] == "embeddium" and "c2me-fabric" not in rmcl.preset_mods("fabric", "lowend")
assert "c2me-fabric" in rmcl.preset_mods("fabric", "normal") and rmcl.preset_mods("vanilla", "normal") == []
f = rmcl.jvm_flags("lowend", threads=4, cores=2)
assert "-XX:ParallelGCThreads=2" in f and "-XX:ConcGCThreads=1" in f
assert "-XX:ParallelGCThreads=16" in rmcl.jvm_flags("high", threads=16, cores=8)
hw = rmcl.detect_hardware()
assert hw["threads"] >= 1 and hw["cores"] >= 1

# ---------- merge: child wins by group:artifact, Forge-style empty urls
parent = {"libraries": [{"name": "org.apache.logging.log4j:log4j-core:2.19.0"}, {"name": "a:b:1"}]}
child = {"libraries": [{"name": "org.apache.logging.log4j:log4j-core:2.17.1"}]}
names = [l["name"] for l in rmcl.merge(parent, child)["libraries"]]
assert names == ["org.apache.logging.log4j:log4j-core:2.17.1", "a:b:1"], names
ents = rmcl.lib_entries({"name": "net.minecraftforge:forge:1.20.1-47.3.0:client",
                         "downloads": {"artifact": {"path": "net/minecraftforge/forge/x-client.jar", "url": "", "sha1": ""}}})
assert ents[0]["url"] == ""
try:
    core.libs({"libraries": [{"name": "net.minecraftforge:forge:1.20.1-47.3.0:client",
                              "downloads": {"artifact": {"path": "net/mf/x-client.jar", "url": ""}}}]}, "v")
    raise SystemExit("expected failure for missing installer-made library")
except RuntimeError as e:
    assert "установщик" in str(e)

# ---------- neoforge version picking
assert rmcl.neoforge_prefix("1.21.1") == "21.1." and rmcl.neoforge_prefix("1.21") == "21.0." and rmcl.neoforge_prefix("1.20.4") == "20.4."
xml = b"<metadata><versioning><versions>" + b"".join(f"<version>{v}</version>".encode() for v in
      ["20.4.9", "21.0.5", "21.1.2", "21.1.17", "21.1.172", "21.1.173-beta", "21.3.1"]) + b"</versions></versioning></metadata>"
orig_http = rmcl.http_get
rmcl.http_get = lambda url, timeout=60: xml
assert rmcl.pick_neoforge("1.21.1") == "21.1.172"       # numeric order, beta skipped
assert rmcl.pick_neoforge("1.20.4") == "20.4.9"
try:
    rmcl.pick_neoforge("1.19.2"); raise SystemExit("expected failure")
except RuntimeError:
    pass
rmcl.http_get = orig_http

# ---------- installer version id (new and legacy layout)
for name, files, want in [("new.jar", {"version.json": json.dumps({"id": "neoforge-21.1.172"})}, "neoforge-21.1.172"),
                          ("old.jar", {"install_profile.json": json.dumps({"versionInfo": {"id": "1.12.2-forge-14.23.5.2860"}})}, "1.12.2-forge-14.23.5.2860")]:
    with zipfile.ZipFile(tmp / name, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    assert rmcl.installer_version_id(tmp / name) == want

# ---------- Fabric/Quilt profile install (network stubbed)
calls = []
def fake_get_json(url):
    calls.append(url)
    if url.endswith("/1.20.1"):
        return [{"loader": {"version": "0.17.0-beta.1", "stable": False}}, {"loader": {"version": "0.16.9", "stable": True}}]
    raise RuntimeError("unexpected " + url)
def fake_http(url, timeout=60):
    return json.dumps({"id": "quilt-loader-0.16.9-1.20.1" if "quilt" in url else "fabric-loader-0.16.9-1.20.1", "libraries": []}).encode()
orig_gj, rmcl.get_json, rmcl.http_get = rmcl.get_json, fake_get_json, fake_http
assert core.install_loader("fabric", "1.20.1", "java") == "fabric-loader-0.16.9-1.20.1"
assert any("meta.fabricmc.net" in c for c in calls)
assert core.install_loader("quilt", "1.20.1", "java") == "quilt-loader-0.16.9-1.20.1"
assert any("meta.quiltmc.org" in c for c in calls)
assert (tmp / "versions" / "fabric-loader-0.16.9-1.20.1" / "fabric-loader-0.16.9-1.20.1.json").exists()
assert core.install_loader("vanilla", "1.20.1", "java") == "1.20.1"
# offline fallback uses what is installed
def boom(*a, **k): raise OSError("offline")
rmcl.get_json = boom
assert core.install_loader("fabric", "1.20.1", "java") == "fabric-loader-0.16.9-1.20.1"
rmcl.get_json, rmcl.http_get = orig_gj, orig_http

# ---------- Modrinth: dependencies, loader filter, pruning of removed mods
projects = {
    "sodium": {"id": "sodium", "file": "sodium-1.jar", "deps": [{"dependency_type": "required", "project_id": "P7dR8mSH"},
                                                              {"dependency_type": "optional", "project_id": "OPT"}]},
    "P7dR8mSH": {"file": "fabric-api-1.jar", "deps": []},
    "lithium": {"file": "lithium-1.jar", "deps": [{"dependency_type": "required", "project_id": "P7dR8mSH"}]},
    "nobuild": None,
}
seen_q = []
def mr_json(url):
    key = url.split("/project/")[1].split("/version")[0]
    seen_q.append(url)
    p = projects[key]
    if not p:
        return []
    return [{"version_type": "beta", "files": [{"url": "u", "filename": "beta.jar", "primary": True, "hashes": {}}], "dependencies": []},
            {"version_type": "release", "files": [{"url": f"file://{key}", "filename": p["file"], "primary": True, "hashes": {}}],
             "dependencies": p["deps"]}]
downloaded = []
def fake_dl(url, dest, sha1=None, cancel=None):
    Path(dest).parent.mkdir(parents=True, exist_ok=True); Path(dest).write_text("x"); downloaded.append(Path(dest).name)
rmcl.get_json, rmcl.download = mr_json, fake_dl
md = tmp / "inst" / "mods"
core.install_mods("1.20.1", "quilt", md, ["sodium", "nobuild"])
assert sorted(p.name for p in md.glob("*.jar")) == ["fabric-api-1.jar", "sodium-1.jar"], list(md.glob("*"))
assert "loaders=%5B%22quilt%22%2C+%22fabric%22%5D" in seen_q[0] or "quilt" in seen_q[0]
(md / "my-own-mod.jar").write_text("manual")
core.install_mods("1.20.1", "quilt", md, ["lithium"])  # sodium removed from the list -> its jar pruned, deps kept once
names = sorted(p.name for p in md.glob("*.jar"))
assert names == ["fabric-api-1.jar", "lithium-1.jar", "my-own-mod.jar"], names
assert core.install_mods("1.20.1", "vanilla", md, ["x"]) is None
import importlib; importlib.reload(rmcl); rmcl.ROOT = tmp  # restore real get_json/download
core = rmcl.Core(logs.append, threading.Event())
src = tmp / "payload.jar"; src.write_bytes(b"jar-bytes")
sha = hashlib.sha1(b"jar-bytes").hexdigest()
pack = tmp / "My Pack.mrpack"
with zipfile.ZipFile(pack, "w") as z:
    z.writestr("modrinth.index.json", json.dumps({
        "name": "Cool: Pack", "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.15.7"},
        "files": [{"path": "mods/payload.jar", "downloads": [src.as_uri()], "hashes": {"sha1": sha}},
                  {"path": "mods/server-only.jar", "downloads": ["file:///nonexistent"], "env": {"client": "unsupported"}},
                  {"path": "../../escape.jar", "downloads": [src.as_uri()], "hashes": {}}]}))
    z.writestr("overrides/config/a.toml", "x=1"); z.writestr("client-overrides/options.txt", "fov:90")
    z.writestr("overrides/../../evil.txt", "bad")
info = core.import_mrpack(pack)
gd = tmp / "instances" / "Cool_ Pack"
assert info == {"name": "Cool_ Pack", "mc": "1.20.1", "loader": "fabric", "lver": "0.15.7"}, info
assert (gd / "mods" / "payload.jar").read_bytes() == b"jar-bytes"
assert (gd / "config" / "a.toml").exists() and (gd / "options.txt").read_text() == "fov:90"
assert not (gd / "mods" / "server-only.jar").exists() and not (tmp / "escape.jar").exists() and not (tmp / "evil.txt").exists()
with zipfile.ZipFile(tmp / "nf.mrpack", "w") as z:
    z.writestr("modrinth.index.json", json.dumps({"name": "N", "dependencies": {"minecraft": "1.21.1", "neoforge": "21.1.172"}, "files": []}))
assert core.import_mrpack(tmp / "nf.mrpack")["loader"] == "neoforge"
print("FEATURE TESTS PASSED")
