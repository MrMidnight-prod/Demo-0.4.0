"""Offline tests: skins (validation, resource pack, options.txt, Mojang lookup, preview geometry) and live mod search."""
import sys, json, base64, struct, zlib, tempfile, zipfile, urllib.error, io
from pathlib import Path
sys.path.insert(0, ".")
import rmcl

tmp = Path(tempfile.mkdtemp()); rmcl.ROOT = tmp

def png(w, h):
    raw = b"".join(b"\x00" + b"\x00\x00\x00\xff" * w for _ in range(h))
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")

# ---------- validation
assert rmcl.validate_skin(png(64, 64)) == (64, 64) and rmcl.validate_skin(png(128, 128)) == (128, 128)
for bad, word in [(png(64, 32), "64×32"), (png(100, 100), "100×100"), (b"GIF89a....", "PNG"), (png(64, 128), "64×128")]:
    try:
        rmcl.validate_skin(bad); raise SystemExit("should reject " + word)
    except ValueError as e:
        assert word in str(e), (word, str(e))

# ---------- pack_format table
assert rmcl.pack_format("1.20.1") == 15 and rmcl.pack_format("1.21.1") == 34 and rmcl.pack_format("1.16.5") == 6
assert rmcl.pack_format("1.12.2") == 3 and rmcl.pack_format("1.8.9") == 1 and rmcl.pack_format("weird") == 64

# ---------- resource pack contents
skin = png(64, 64)
pk = tmp / "res" / rmcl.SKIN_PACK
rmcl.build_skin_pack(skin, "1.20.1", pk)
with zipfile.ZipFile(pk) as z:
    names = set(z.namelist())
    meta = json.loads(z.read("pack.mcmeta"))
    assert meta["pack"]["pack_format"] == 15 and meta["pack"]["supported_formats"]["max_inclusive"] >= 1000
    assert len(names) == 1 + 18 + 2, len(names)
    for n in rmcl.SKIN_NAMES:
        for kind in ("slim", "wide"):
            assert z.read(f"assets/minecraft/textures/entity/player/{kind}/{n}.png") == skin
    assert z.read("assets/minecraft/textures/entity/steve.png") == skin
assert not list((tmp / "res").glob("*.part"))
try:
    rmcl.build_skin_pack(png(64, 32), "1.20.1", tmp / "x.zip"); raise SystemExit("legacy skin must be rejected")
except ValueError:
    pass
assert not (tmp / "x.zip").exists()

# ---------- options.txt editing
o = tmp / "options.txt"
pid = "file/" + rmcl.SKIN_PACK
rmcl.set_pack_in_options(o, pid, True)                       # no file at all
assert o.read_text() == f'resourcePacks:["vanilla","{pid}"]\nincompatibleResourcePacks:["{pid}"]\n', o.read_text()
rmcl.set_pack_in_options(o, pid, True)                       # idempotent
assert o.read_text().count(pid) == 2
o.write_text('fov:0.5\nresourcePacks:["vanilla","file/Other.zip"]\nincompatibleResourcePacks:[]\nlang:ru_ru\n')
rmcl.set_pack_in_options(o, pid, True)                       # keeps other settings and packs, ours goes on top
t = o.read_text().splitlines()
assert t[0] == "fov:0.5" and t[3] == "lang:ru_ru"
assert json.loads(t[1].split(":", 1)[1]) == ["vanilla", "file/Other.zip", pid] and json.loads(t[2].split(":", 1)[1]) == [pid]
rmcl.set_pack_in_options(o, pid, False)                      # removal
t = o.read_text().splitlines()
assert json.loads(t[1].split(":", 1)[1]) == ["vanilla", "file/Other.zip"] and json.loads(t[2].split(":", 1)[1]) == []
o.write_text("resourcePacks:garbage\n")                      # corrupt value must not crash
rmcl.set_pack_in_options(o, pid, True); assert pid in o.read_text()
absent = tmp / "none.txt"
rmcl.set_pack_in_options(absent, pid, False)                 # disabling with no file: nothing created
assert not absent.exists()

# ---------- Mojang skin lookup (network stubbed)
tex = base64.b64encode(json.dumps({"textures": {"SKIN": {"url": "http://textures.minecraft.net/texture/abc",
                                                         "metadata": {"model": "slim"}}}}).encode()).decode()
fetched = []
def fake_gj(url):
    if "profiles/minecraft/Notch" in url: return {"id": "069a79f444e94726a5befca90e38aaf5", "name": "Notch"}
    if "profile/069a79f4" in url: return {"properties": [{"name": "textures", "value": tex}]}
    if "profiles/minecraft/Ghost" in url: raise json.JSONDecodeError("empty", "", 0)
    if "profiles/minecraft/Busy" in url: raise urllib.error.HTTPError(url, 429, "Too many", {}, io.BytesIO())
    if "profiles/minecraft/NoSkin" in url: return {"id": "ff"}
    if "profile/ff" in url: return {"properties": []}
    raise OSError("offline")
rmcl.get_json = fake_gj
rmcl.http_get = lambda url, timeout=60: (fetched.append(url), skin)[1]
data, model = rmcl.fetch_skin_by_name(" Notch ")
assert data == skin and model == "slim" and fetched == ["https://textures.minecraft.net/texture/abc"], fetched
for nick, word in [("Ghost", "не найден"), ("Busy", "ограничил"), ("NoSkin", "нет собственного"), ("bad name!", "Некорректный"), ("Other", "Нет связи")]:
    try:
        rmcl.fetch_skin_by_name(nick); raise SystemExit("expected error for " + nick)
    except (RuntimeError, ValueError) as e:
        assert word in str(e), (nick, str(e))

# ---------- preview geometry stays inside the 64x64 skin and the 16x32 canvas
for slim in (False, True):
    parts = rmcl.preview_parts(slim)
    assert len(parts) == 12
    for (x1, y1, x2, y2), (dx, dy), ov in parts:
        assert 0 <= x1 < x2 <= 64 and 0 <= y1 < y2 <= 64, (x1, y1, x2, y2)
        assert 0 <= dx and dx + (x2 - x1) <= 16 and 0 <= dy and dy + (y2 - y1) <= 32, (dx, dy)
arm_w = lambda slim: [b[2] - b[0] for b, d, o in rmcl.preview_parts(slim) if d == (4 - (3 if slim else 4), 8)][0]
assert arm_w(True) == 3 and arm_w(False) == 4

# ---------- live search request + formatting
seen = []
def spy(url):
    seen.append(url)
    return {"hits": [{"slug": "sodium", "title": "Sodium", "description": "fast", "downloads": 1234567, "author": "jelly"}]}
rmcl.get_json = spy
hits = rmcl.mr_search("sod", "1.20.1", "quilt", offset=40)
assert hits == [{"slug": "sodium", "title": "Sodium", "desc": "fast", "downloads": 1234567, "author": "jelly"}]
import urllib.parse as up
qs = up.parse_qs(up.urlparse(seen[0]).query)
assert qs["index"] == ["relevance"] and qs["offset"] == ["40"] and qs["limit"] == ["40"] and qs["query"] == ["sod"]
facets = json.loads(qs["facets"][0])
assert ["versions:1.20.1"] in facets and ["categories:quilt", "categories:fabric"] in facets and ["project_type:mod"] in facets
rmcl.mr_search("", "1.20.1", "forge")                      # empty query = popular mods
assert up.parse_qs(up.urlparse(seen[1]).query)["index"] == ["downloads"]
assert rmcl.fmt_num(999) == "999" and rmcl.fmt_num(12345) == "12K" and rmcl.fmt_num(1234567) == "1.2M"
print("UPDATE TESTS PASSED")
