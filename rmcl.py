#!/usr/bin/env python3
"""rmcl - minimal Minecraft Java launcher with a tkinter GUI (standard library only)."""
import hashlib, json, os, platform, queue, re, shutil, subprocess, sys, threading, traceback, uuid, zipfile
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VERSION = "0.3.0"
MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
UA = f"rmcl/{VERSION} (github.com launcher)"
IS_WIN = os.name == "nt"
OS_NAME = "windows" if IS_WIN else ("osx" if sys.platform == "darwin" else "linux")
NO_WINDOW = 0x08000000 if IS_WIN else 0
ROOT = (Path(os.environ["APPDATA"]) / "rmcl") if IS_WIN else (Path.home() / ".rmcl")

# ---------------------------------------------------------------- hardware tiers
LOADERS = ["Vanilla", "Fabric", "Quilt", "Forge", "NeoForge"]
MR_LOADERS = {"fabric": ["fabric"], "quilt": ["quilt", "fabric"], "forge": ["forge"], "neoforge": ["neoforge"]}

LOWEND_OPTIONS = ('renderDistance:6\nsimulationDistance:5\nmaxFps:60\ngraphicsMode:0\nfancyGraphics:false\n'
                  'particles:2\nrenderClouds:"false"\nentityShadows:false\nbiomeBlendRadius:1\n'
                  'mipmapLevels:1\nentityDistanceScaling:0.75\n')
TIERS = {
    "lowend": {"label": "Слабый ПК", "ram_frac": 0.45, "ram_cap": 4096, "options": LOWEND_OPTIONS},
    "normal": {"label": "Средний ПК", "ram_frac": 0.50, "ram_cap": 6144,
               "options": "renderDistance:10\nsimulationDistance:8\nmaxFps:120\n"},
    "high": {"label": "Мощный ПК", "ram_frac": 0.50, "ram_cap": 8192,
             "options": "renderDistance:14\nsimulationDistance:10\n"},
}

_FABRIC_LOW = ["fabric-api", "sodium", "lithium", "ferrite-core", "modernfix", "memoryleakfix", "entityculling",
               "immediatelyfast", "scalablelux", "noisium", "dynamic-fps", "sodium-extra"]
_FABRIC_MID = ["fabric-api", "sodium", "lithium", "c2me-fabric", "scalablelux", "ferrite-core", "modernfix",
               "immediatelyfast", "entityculling", "noisium"]
_FORGE_LOW = ["embeddium", "modernfix", "ferrite-core", "entityculling", "immediatelyfast", "memoryleakfix",
              "dynamic-fps"]
PRESETS = {  # lowend has no C2ME: extra worker threads fight the render thread on 2 cores
    "fabric": {"lowend": _FABRIC_LOW, "normal": _FABRIC_MID, "high": _FABRIC_MID + ["sodium-extra"]},
    "forge": {"lowend": _FORGE_LOW, "normal": _FORGE_LOW + ["noisium"], "high": _FORGE_LOW + ["noisium"]},
}
PRESETS["quilt"] = PRESETS["fabric"]
PRESETS["neoforge"] = PRESETS["forge"]


def preset_mods(loader, tier):
    return list(PRESETS.get(loader, {}).get(tier, []))


def gpu_is_weak(name):
    n = name.lower()
    pats = [r"basic render|vmware|virtualbox|llvmpipe|svga",
            r"intel.*(hd graphics|uhd graphics|gma)",
            r"geforce\s+(gt|gts)\s*\d", r"geforce\s+\d{3,4}m?\b", r"\bmx\s?[123]\d{2}\b",
            r"radeon.*(hd\s*[2-6]\d{3}|\br[2-5]\b|vega\s*(3|6|7|8|10|11)\b)"]
    return any(re.search(p, n) for p in pats)


def classify(hw):
    """Pick a tier from detected hardware. Weak = old/integrated GPUs only, <7 GB RAM or <=2 physical cores."""
    gpus = hw.get("gpus") or []
    weak_gpu = bool(gpus) and all(gpu_is_weak(g) for g in gpus)
    ram = hw.get("ram_mb") or 8192
    cores = hw.get("cores") or max(1, (hw.get("threads") or 2) // 2)
    if ram < 7000 or cores <= 2 or weak_gpu:
        return "lowend"
    if ram >= 15000 and cores >= 6:
        return "high"
    return "normal"


def auto_ram(hw, tier):
    t = TIERS[tier]
    ram = hw.get("ram_mb") or 6144
    mb = int(min(ram * t["ram_frac"], t["ram_cap"]) // 256 * 256)
    return max(1024, mb)


def detect_hardware():
    hw = {"cpu_name": "", "cores": None, "threads": os.cpu_count() or 2, "ram_mb": None, "gpus": []}
    try:
        if IS_WIN:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            hw["ram_mb"] = int(m.ullTotalPhys // (1024 * 1024))
        elif sys.platform == "darwin":
            hw["ram_mb"] = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip()) // (1024 * 1024)
        else:
            for line in open("/proc/meminfo"):
                if line.startswith("MemTotal"):
                    hw["ram_mb"] = int(line.split()[1]) // 1024
                    break
    except Exception:
        pass
    try:
        if IS_WIN:
            cmd = ("@{cpu=@(Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores);"
                   "gpu=@(Get-CimInstance Win32_VideoController | Select-Object Name)} "
                   "| ConvertTo-Json -Depth 3 -Compress")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                               capture_output=True, text=True, errors="replace", timeout=30,
                               creationflags=NO_WINDOW)
            d = json.loads(r.stdout)
            cpu = d.get("cpu") or []
            gpu = d.get("gpu") or []
            cpu = [cpu] if isinstance(cpu, dict) else cpu
            gpu = [gpu] if isinstance(gpu, dict) else gpu
            if cpu:
                hw["cpu_name"] = " ".join(str(cpu[0].get("Name", "")).split())
                hw["cores"] = sum(int(c.get("NumberOfCores") or 0) for c in cpu) or None
            hw["gpus"] = [" ".join(str(g.get("Name", "")).split()) for g in gpu if g.get("Name")]
        elif sys.platform.startswith("linux"):
            out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=10).stdout
            hw["gpus"] = [l.split(": ", 1)[-1] for l in out.splitlines() if re.search(r"VGA|3D", l)]
    except Exception:
        pass
    if not hw["cores"]:
        hw["cores"] = max(1, hw["threads"] // 2)
    return hw


def jvm_flags(tier, zgc=False, threads=None, cores=None):
    n = threads or os.cpu_count() or 4
    c = cores or max(1, n // 2)
    par, conc = (max(1, c), 1) if tier == "lowend" else (n, max(1, n // 4))
    if zgc:
        f = ["-XX:+UseZGC", "-XX:+ZGenerational"]
    elif tier == "lowend":
        f = ["-XX:+UseG1GC", "-XX:MaxGCPauseMillis=50", "-XX:G1HeapRegionSize=4M",
             "-XX:InitiatingHeapOccupancyPercent=30"]
    else:
        f = ["-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200",
             "-XX:+UnlockExperimentalVMOptions", "-XX:G1NewSizePercent=30", "-XX:G1MaxNewSizePercent=40",
             "-XX:G1HeapRegionSize=8M", "-XX:G1ReservePercent=20", "-XX:G1HeapWastePercent=5",
             "-XX:G1MixedGCCountTarget=4", "-XX:InitiatingHeapOccupancyPercent=15",
             "-XX:G1MixedGCLiveThresholdPercent=90", "-XX:G1RSetUpdatingPauseTimePercent=5",
             "-XX:SurvivorRatio=32", "-XX:MaxTenuringThreshold=1"]
    return f + ["-XX:+DisableExplicitGC", "-XX:+PerfDisableSharedMem",
                f"-XX:ParallelGCThreads={par}", f"-XX:ConcGCThreads={conc}"]


# ---------------------------------------------------------------- http / files
class Cancelled(Exception):
    pass


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url):
    return json.loads(http_get(url, 30))


def read_json(p):
    return json.loads(Path(p).read_text("utf-8"))


def download(url, dest, sha1=None, cancel=None):
    dest = Path(dest)
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for _ in range(3):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        try:
            data = http_get(url)
            if sha1 and hashlib.sha1(data).hexdigest().lower() != sha1.lower():
                raise IOError("SHA1 mismatch")
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return
        except Exception as e:  # retry
            last = e
    raise RuntimeError(f"download failed: {url}: {last}")


def unzip(zpath, dest, exclude=(), strip=0):
    dest = Path(dest).resolve()
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            name = info.filename
            if any(name.startswith(x) for x in exclude):
                continue
            parts = Path(name).parts[strip:]
            if not parts:
                continue
            out = (dest / Path(*parts)).resolve()
            if dest not in out.parents and out != dest:
                continue  # zip-slip guard
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)


# ---------------------------------------------------------------- Mojang JSON logic
def cur_arch():
    m = platform.machine().lower()
    return "x86" if m in ("x86", "i386", "i686") else ("arm64" if m in ("arm64", "aarch64") else "x86_64")


def rule_match(r, feats):
    os_ = r.get("os")
    if os_:
        if "name" in os_ and os_["name"] != OS_NAME:
            return False
        if "arch" in os_ and os_["arch"] != cur_arch():
            return False
    for k, v in (r.get("features") or {}).items():
        if bool(feats.get(k, False)) != v:
            return False
    return True


def rules_allow(rules, feats):
    if not rules:
        return True
    ok = False
    for r in rules:
        if rule_match(r, feats):
            ok = r["action"] == "allow"
    return ok


def maven_path(name):
    coords, _, ext = name.partition("@")
    p = coords.split(":")
    if len(p) < 3:
        return None
    f = f"{p[1]}-{p[2]}" + (f"-{p[3]}" if len(p) > 3 else "") + f".{ext or 'jar'}"
    return f"{p[0].replace('.', '/')}/{p[1]}/{p[2]}/{f}"


def lib_entries(lib):
    """-> list of dicts {rel,url,sha1,native}; native=None for classpath jars, else exclude list."""
    if not rules_allow(lib.get("rules"), {}):
        return []
    out, dl = [], lib.get("downloads")
    if dl and dl.get("artifact"):
        a = dl["artifact"]
        rel = a.get("path") or maven_path(lib["name"])
        if rel:
            out.append(dict(rel=rel, url=a["url"], sha1=a.get("sha1"), native=None))
    elif not dl:  # Fabric style: name + maven url
        rel = maven_path(lib["name"])
        if rel:
            base = lib.get("url", "https://libraries.minecraft.net/")
            base += "" if base.endswith("/") else "/"
            out.append(dict(rel=rel, url=base + rel, sha1=lib.get("sha1"), native=None))
    nat = lib.get("natives")
    if nat and dl and OS_NAME in nat:  # legacy natives (<= 1.18)
        cls = nat[OS_NAME].replace("${arch}", "64" if sys.maxsize > 2 ** 32 else "32")
        a = (dl.get("classifiers") or {}).get(cls)
        if a:
            rel = a.get("path") or maven_path(f"{lib['name']}:{cls}")
            ex = list((lib.get("extract") or {}).get("exclude", [])) + ["META-INF/"]
            out.append(dict(rel=rel, url=a["url"], sha1=a.get("sha1"), native=ex))
    return out


def lib_key(name):
    p = name.split("@")[0].split(":")
    return ":".join(p[:2] + p[3:4])


def merge(parent, child):
    keys = {lib_key(l["name"]) for l in child.get("libraries", [])}
    libs = list(child.get("libraries", [])) + [l for l in parent.get("libraries", []) if lib_key(l["name"]) not in keys]
    out = {**parent, **{k: v for k, v in child.items() if k not in ("libraries", "arguments")}}
    pa, ca = parent.get("arguments"), child.get("arguments")
    if pa and ca:
        out["arguments"] = {"game": pa.get("game", []) + ca.get("game", []),
                            "jvm": pa.get("jvm", []) + ca.get("jvm", [])}
    elif pa or ca:
        out["arguments"] = ca or pa
    out["libraries"] = libs
    return out


def neoforge_prefix(mc):
    p = mc.split(".")
    return f"{p[1]}.{p[2] if len(p) > 2 else 0}." if p[0] == "1" and len(p) >= 2 else mc + "."


def version_key(v):
    return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v)]


def installer_version_id(jar):
    with zipfile.ZipFile(jar) as z:
        if "version.json" in z.namelist():
            return json.loads(z.read("version.json"))["id"]
        return json.loads(z.read("install_profile.json"))["versionInfo"]["id"]


def mr_search(query, mc, loader):
    facets = [["project_type:mod"]]
    if mc:
        facets.append([f"versions:{mc}"])
    if MR_LOADERS.get(loader):
        facets.append([f"categories:{l}" for l in MR_LOADERS[loader]])
    q = urllib.parse.urlencode({"query": query, "facets": json.dumps(facets), "limit": 30})
    return [(h["slug"], h["title"], h.get("description", "")) for h in get_json(f"https://api.modrinth.com/v2/search?{q}")["hits"]]


def pick_neoforge(mc):
    xml = http_get("https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml")
    vs = [e.text for e in ET.fromstring(xml).findall("./versioning/versions/version")]
    pre = neoforge_prefix(mc)
    cands = [v for v in vs if v.startswith(pre)]
    cands = [v for v in cands if not re.search(r"alpha|beta|rc|snapshot", v)] or cands
    if not cands:
        raise RuntimeError(f"NeoForge не поддерживает Minecraft {mc}")
    return max(cands, key=version_key)


def subst(s, vs):
    return re.sub(r"\$\{(\w+)\}", lambda m: vs.get(m.group(1), m.group(0)), s)


def expand(args, feats, vs):
    out = []
    for a in args:
        if isinstance(a, str):
            out.append(subst(a, vs))
        elif rules_allow(a.get("rules"), feats):
            v = a["value"]
            out += [subst(x, vs) for x in ([v] if isinstance(v, str) else v)]
    return out


def offline_uuid(name):
    b = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode()).digest())
    b[6] = (b[6] & 0x0F) | 0x30
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


def split_host_port(s):
    h, _, p = s.rpartition(":")
    return (h, int(p)) if h and p.isdigit() else (s, 25565)


def java_info(java):
    try:
        r = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
        m = re.search(r'version "(\d+)(?:\.(\d+))?', r.stderr)
        major = int(m.group(2)) if m.group(1) == "1" else int(m.group(1))
        return major, "64-Bit" in r.stderr
    except Exception:
        return None


# ---------------------------------------------------------------- core
class Core:
    def __init__(self, log, cancel):
        self.log, self.cancel, self.proc = log, cancel, None

    def resolve(self, v):
        if v in ("latest", "latest-snapshot"):
            return get_json(MANIFEST_URL)["latest"]["release" if v == "latest" else "snapshot"]
        return v

    def vanilla(self, vid):
        d = ROOT / "versions" / vid
        jp = d / f"{vid}.json"
        if not jp.exists():
            e = next((v for v in get_json(MANIFEST_URL)["versions"] if v["id"] == vid), None)
            if not e:
                raise RuntimeError(f"Version {vid} not found")
            download(e["url"], jp, cancel=self.cancel)
        c = read_json(jp).get("downloads", {}).get("client")
        if c:
            self.log(f"Client {vid}")
            download(c["url"], d / f"{vid}.jar", c.get("sha1"), self.cancel)

    def vanilla_java(self, mc):
        self.vanilla(mc)
        return (read_json(ROOT / "versions" / mc / f"{mc}.json").get("javaVersion") or {}).get("majorVersion", 8)

    META = {"fabric": "https://meta.fabricmc.net/v2/versions/loader",
            "quilt": "https://meta.quiltmc.org/v3/versions/loader"}

    def install_loader(self, loader, mc, java, want=None):
        """-> version id ready to launch (vanilla id, or the loader's profile id)."""
        loader = loader.lower()
        if loader == "vanilla":
            return mc
        if loader in self.META:
            return self.install_meta_loader(loader, mc, want)
        return self.install_forge_like(loader, mc, java, want)

    def install_meta_loader(self, loader, mc, want=None):
        base = self.META[loader]
        try:
            ver = want
            if not ver:
                v = get_json(f"{base}/{mc}")
                if not v:
                    raise RuntimeError(f"{loader} не поддерживает Minecraft {mc}")
                stable = [e for e in v if e["loader"].get("stable", True)
                          and not re.search(r"alpha|beta|rc|pre", e["loader"]["version"])]
                ver = (stable or v)[0]["loader"]["version"]
            txt = http_get(f"{base}/{mc}/{ver}/profile/json").decode()
            vid = json.loads(txt)["id"]
            p = ROOT / "versions" / vid / f"{vid}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(txt, "utf-8")
            self.log(f"{loader}: профиль {vid}")
            return vid
        except Exception:
            have = sorted(p.name for p in (ROOT / "versions").glob(f"{loader}-loader-*-{mc}"))
            if have:
                self.log(f"{loader}: нет сети, используем установленный {have[-1]}")
                return have[-1]
            raise

    def install_forge_like(self, loader, mc, java, want=None):
        """Forge / NeoForge: download the official installer and run it headless into our data dir."""
        if loader == "forge":
            promos = get_json("https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json")["promos"]
            v = want or promos.get(f"{mc}-recommended") or promos.get(f"{mc}-latest")
            if not v:
                raise RuntimeError(f"Forge не поддерживает Minecraft {mc}")
            full = v if v.startswith(mc + "-") else f"{mc}-{v}"
            url = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{full}/forge-{full}-installer.jar"
        elif loader == "neoforge":
            v = want or pick_neoforge(mc)
            url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{v}/neoforge-{v}-installer.jar"
        else:
            raise RuntimeError(f"Неизвестный загрузчик: {loader}")
        inst = ROOT / "installers" / url.rsplit("/", 1)[1]
        self.log(f"{loader}: установщик {inst.name}")
        download(url, inst, cancel=self.cancel)
        vid = installer_version_id(inst)
        if (ROOT / "versions" / vid / f"{vid}.json").exists():
            return vid
        pf = ROOT / "launcher_profiles.json"
        if not pf.exists():
            pf.write_text('{"profiles":{}}')
        self.vanilla(mc)
        self.log(f"Запуск установщика {loader} (это может занять несколько минут)...")
        self.proc = subprocess.Popen([java, "-jar", str(inst), "--installClient", str(ROOT)],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        for line in iter(self.proc.stdout.readline, b""):
            self.log(line.decode("utf-8", "replace").rstrip())
        code = self.proc.wait()
        self.proc = None
        if self.cancel.is_set():
            raise Cancelled()
        if code != 0 or not (ROOT / "versions" / vid / f"{vid}.json").exists():
            raise RuntimeError(f"Установщик {loader} завершился с кодом {code}")
        return vid

    def import_mrpack(self, path):
        """Install a Modrinth modpack (.mrpack) into its own instance. -> {name, mc, loader, lver}"""
        with zipfile.ZipFile(path) as z:
            idx = json.loads(z.read("modrinth.index.json"))
            deps = idx.get("dependencies", {})
            name = re.sub(r'[\\/:*?"<>|]+', "_", idx.get("name", "pack")).strip() or "pack"
            gdir = (ROOT / "instances" / name).resolve()
            files = [f for f in idx.get("files", []) if (f.get("env") or {}).get("client") != "unsupported"]
            self.log(f"Сборка «{name}»: файлов {len(files)}")

            def one(f):
                dest = (gdir / f["path"]).resolve()
                if gdir in dest.parents:
                    download(f["downloads"][0], dest, (f.get("hashes") or {}).get("sha1"), self.cancel)

            with ThreadPoolExecutor(8) as ex:
                list(ex.map(one, files))
            for n in z.namelist():
                for pre in ("overrides/", "client-overrides/"):
                    if n.startswith(pre) and not n.endswith("/"):
                        dest = (gdir / n[len(pre):]).resolve()
                        if gdir in dest.parents:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(z.read(n))
        loader, lver = "vanilla", None
        for key, ld in (("fabric-loader", "fabric"), ("quilt-loader", "quilt"), ("neoforge", "neoforge"), ("forge", "forge")):
            if key in deps:
                loader, lver = ld, deps[key]
                break
        return {"name": name, "mc": deps.get("minecraft"), "loader": loader, "lver": lver}

    def load_profile(self, vid):
        vd = ROOT / "versions"
        child = read_json(vd / vid / f"{vid}.json")
        pid = child.get("inheritsFrom")
        if pid:
            prof, jar = merge(read_json(vd / pid / f"{pid}.json"), child), vd / pid / f"{pid}.jar"
        else:
            prof, jar = child, vd / vid / f"{vid}.jar"
        prof["id"] = vid
        return prof, jar

    def prepare(self, vid):
        jp = ROOT / "versions" / vid / f"{vid}.json"
        self.vanilla(read_json(jp).get("inheritsFrom") or vid if jp.exists() else vid)
        prof, jar = self.load_profile(vid)
        self.libs(prof, vid)
        self.assets(prof)
        return prof, jar

    def libs(self, prof, vid):
        entries = [e for l in prof["libraries"] for e in lib_entries(l)]
        self.log(f"Libraries: {len(entries)}")

        def one(e):
            dest = ROOT / "libraries" / e["rel"]
            if e["url"]:
                download(e["url"], dest, e["sha1"], self.cancel)
            elif not dest.exists():  # forge-like installers create these themselves
                raise RuntimeError(f"Не найдена библиотека {e['rel']} (её должен создать установщик загрузчика)")

        with ThreadPoolExecutor(16) as ex:
            list(ex.map(one, entries))
        nd = ROOT / "versions" / vid / "natives"
        nd.mkdir(parents=True, exist_ok=True)
        for e in entries:
            if e["native"] is not None:
                unzip(ROOT / "libraries" / e["rel"], nd, exclude=e["native"])

    def assets(self, prof):
        ai = prof.get("assetIndex")
        if not ai:
            return
        ip = ROOT / "assets" / "indexes" / f"{ai['id']}.json"
        download(ai["url"], ip, ai.get("sha1"), self.cancel)
        hashes = {o["hash"] for o in read_json(ip)["objects"].values()}
        todo = [h for h in hashes if not (ROOT / "assets" / "objects" / h[:2] / h).exists()]
        self.log(f"Assets: {len(todo)} to download")
        with ThreadPoolExecutor(32) as ex:
            list(ex.map(lambda h: download(f"https://resources.download.minecraft.net/{h[:2]}/{h}",
                                           ROOT / "assets" / "objects" / h[:2] / h, h, self.cancel), todo))

    def legacy_assets(self, aid, game_dir):
        ip = ROOT / "assets" / "indexes" / f"{aid}.json"
        if not ip.exists():
            return None
        idx = read_json(ip)
        target = (ROOT / "assets" / "virtual" / aid) if idx.get("virtual") else \
                 (game_dir / "resources") if idx.get("map_to_resources") else None
        if target is None:
            return None
        for name, o in idx["objects"].items():
            dst = target / name
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / "assets" / "objects" / o["hash"][:2] / o["hash"], dst)
        return target

    def mr_pick(self, key, mc, loaders):
        q = urllib.parse.urlencode({"game_versions": json.dumps([mc]), "loaders": json.dumps(loaders)})
        vs = get_json(f"https://api.modrinth.com/v2/project/{key}/version?{q}")
        return next((x for x in vs if x["version_type"] == "release"), vs[0]) if vs else None

    def install_mods(self, mc, loader, mods_dir, slugs):
        """Install `slugs` from Modrinth plus their required dependencies; remove mods we installed earlier
        that are no longer wanted. Files the user added by hand are never touched."""
        loaders = MR_LOADERS.get(loader)
        if not loaders:
            return
        mods_dir.mkdir(parents=True, exist_ok=True)
        reg_p = mods_dir / ".rmcl-mods.json"
        reg = json.loads(reg_p.read_text()) if reg_p.exists() else {}
        self.log(f"Моды для {loader} {mc} (Modrinth):")
        wanted, todo, done = {}, list(slugs), set()
        while todo:
            if self.cancel.is_set():
                raise Cancelled()
            key = todo.pop(0)
            if key in done:
                continue
            done.add(key)
            try:
                v = self.mr_pick(key, mc, loaders)
            except Exception as e:
                self.log(f"  ! {key}: {e}")
                continue
            if not v:
                self.log(f"  - {key}: нет сборки под {mc}, пропущен")
                continue
            f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
            download(f["url"], mods_dir / f["filename"], f["hashes"].get("sha1"), self.cancel)
            wanted[key] = f["filename"]
            self.log(f"  + {f['filename']}")
            for d in v.get("dependencies", []):
                if d.get("dependency_type") == "required" and d.get("project_id"):
                    todo.append(d["project_id"])
        keep = set(wanted.values())
        for fn in set(reg.values()) - keep:
            (mods_dir / fn).unlink(missing_ok=True)
        reg_p.write_text(json.dumps(wanted))

    def ensure_java(self, required):
        if not (IS_WIN and platform.machine().lower() in ("amd64", "x86_64")):
            return "java"
        major = 8 if required <= 8 else 17 if required <= 17 else required
        d = ROOT / "runtime" / f"java{major}"
        exe = d / "bin" / "java.exe"
        if exe.exists():
            return str(exe)
        self.log(f"Downloading Java {major} runtime (one-time, ~50 MB)...")
        z = ROOT / "runtime" / f"java{major}.zip"
        download(f"https://api.adoptium.net/v3/binary/latest/{major}/ga/windows/x64/jre/hotspot/normal/eclipse",
                 z, cancel=self.cancel)
        unzip(z, d, strip=1)
        z.unlink(missing_ok=True)
        if not exe.exists():
            raise RuntimeError("Java unpacked but java.exe not found")
        return str(exe)

    def build_cmd(self, prof, jar, o):
        """o: name, server, ram (MB), java, zgc, tier, game_dir (Path). -> (java, args)"""
        sep = os.pathsep
        cp, seen = [], set()
        for l in prof["libraries"]:
            for e in lib_entries(l):
                if e["native"] is None and e["rel"] not in seen:
                    seen.add(e["rel"])
                    cp.append(str(ROOT / "libraries" / e["rel"]))
        cp.append(str(jar))
        aid = (prof.get("assetIndex") or {}).get("id") or prof.get("assets") or "legacy"
        gdir = o["game_dir"]
        gassets = self.legacy_assets(aid, gdir) or (ROOT / "assets")
        vs = {"auth_player_name": o["name"], "auth_uuid": offline_uuid(o["name"]), "auth_access_token": "0",
              "auth_session": "0", "auth_xuid": "0", "clientid": "0", "user_type": "legacy",
              "user_properties": "{}", "version_name": prof["id"], "version_type": prof.get("type", "release"),
              "game_directory": str(gdir), "assets_root": str(ROOT / "assets"), "game_assets": str(gassets),
              "assets_index_name": aid, "natives_directory": str(ROOT / "versions" / prof["id"] / "natives"),
              "library_directory": str(ROOT / "libraries"), "classpath_separator": sep,
              "classpath": sep.join(cp), "launcher_name": "rmcl", "launcher_version": VERSION,
              "resolution_width": "854", "resolution_height": "480"}
        feats = {}
        if o.get("server"):
            feats["is_quick_play_multiplayer"] = True
            vs["quickPlayMultiplayer"] = o["server"]
        if prof.get("arguments"):
            jvm = expand(prof["arguments"].get("jvm", []), feats, vs)
            game = expand(prof["arguments"].get("game", []), feats, vs)
        else:  # legacy format
            jvm = [subst(x, vs) for x in ["-Djava.library.path=${natives_directory}",
                                          "-Dminecraft.launcher.brand=${launcher_name}",
                                          "-Dminecraft.launcher.version=${launcher_version}",
                                          "-cp", "${classpath}"]]
            game = [subst(x, vs) for x in prof.get("minecraftArguments", "").split()]
        if o.get("server") and "--quickPlayMultiplayer" not in game:  # < 1.20
            h, p = split_host_port(o["server"])
            game += ["--server", h, "--port", str(p)]
        hw = o.get("hw") or {}
        args = jvm_flags(o["tier"], o.get("zgc", False), hw.get("threads"), hw.get("cores")) + [f"-Xmx{o['ram']}M", "-Xms1G"] + jvm \
            + [prof["mainClass"]] + game
        return o["java"], args

    def launch(self, prof, jar, o, dry_run=False):
        req = (prof.get("javaVersion") or {}).get("majorVersion", 0)
        info = java_info(o["java"])
        if info:
            if info[0] < req:
                raise RuntimeError(f"Java {req}+ required, found {info[0]} ({o['java']})")
            if not info[1]:
                self.log("WARNING: 32-bit Java cannot use more than ~1.5 GB")
        else:
            self.log(f"WARNING: could not run '{o['java']}' to check its version")
        o["game_dir"].mkdir(parents=True, exist_ok=True)
        java, args = self.build_cmd(prof, jar, o)
        if dry_run:
            self.log(" ".join(f'"{a}"' if " " in a else a for a in [java] + args))
            return
        opt = o["game_dir"] / "options.txt"
        if not opt.exists():
            opt.write_text(TIERS[o["tier"]]["options"])
            self.log(f"Записаны настройки графики для профиля «{TIERS[o['tier']]['label']}»")
        self.log(f"Launching {prof['id']} as {o['name']} ({o['game_dir']})")
        self.proc = subprocess.Popen([java] + args, cwd=o["game_dir"], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        for line in iter(self.proc.stdout.readline, b""):
            self.log(line.decode("utf-8", "replace").rstrip())
        code = self.proc.wait()
        self.proc = None
        self.log(f"[game exited with code {code}]")


# ---------------------------------------------------------------- GUI
def write_error(text):
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        (ROOT / "error.log").write_text(text, "utf-8")
    except Exception:
        pass


LOADER_NOTES = {
    "vanilla": "Чистая игра без модов.",
    "fabric": "Лёгкий загрузчик. Лучший выбор для слабых ПК: Sodium, Lithium и др.",
    "quilt": "Форк Fabric, понимает моды Fabric.",
    "forge": "Классический загрузчик, много сборок и старых модов.",
    "neoforge": "Современный форк Forge (Minecraft 1.20.2 и новее).",
}
DEFAULT_HW = {"cpu_name": "", "cores": None, "threads": os.cpu_count() or 2, "ram_mb": None, "gpus": []}


def main():
    import tkinter as tk
    from tkinter import ttk, filedialog
    from tkinter import font as tkfont

    BG, SIDE, CARD, CARD2, FIELD, BORDER = "#12151c", "#0d1016", "#1b2029", "#262e3b", "#0f1218", "#303949"
    FG, MUTED, ACCENT, ACCENT_D, ACCENT_FG = "#e8ebf1", "#8d96a8", "#4ade80", "#34c46c", "#04210f"
    DANGER, DANGER_D, OFF = "#ef4444", "#d93636", "#2a303c"

    root = tk.Tk()
    root.title(f"rmcl {VERSION}")
    root.geometry("1060x700")
    root.minsize(960, 640)
    root.configure(bg=BG)
    try:
        fam = "Segoe UI" if "Segoe UI" in tkfont.families() else tkfont.nametofont("TkDefaultFont").actual("family")
    except Exception:
        fam = "Helvetica"
    F, FB, SM = (fam, 10), (fam, 10, "bold"), (fam, 8, "bold")
    H1, H2 = (fam, 22, "bold"), (fam, 12, "bold")
    MONO = ("Consolas" if IS_WIN else "monospace", 9)

    # ---- ttk styling (clam is the only built-in theme that lets us recolour everything)
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("D.TCombobox", fieldbackground=FIELD, background=CARD2, foreground=FG, arrowcolor=FG,
                    bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD, padding=6)
    style.map("D.TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
    style.configure("G.Horizontal.TProgressbar", troughcolor=FIELD, background=ACCENT, bordercolor=FIELD,
                    lightcolor=ACCENT, darkcolor=ACCENT)
    style.configure("D.Vertical.TScrollbar", troughcolor=FIELD, background=CARD2, bordercolor=FIELD,
                    arrowcolor=FG, lightcolor=CARD2, darkcolor=CARD2)
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", ACCENT_FG)

    # ---- small custom widgets
    class Btn(tk.Label):
        KINDS = {"primary": (ACCENT, ACCENT_D, ACCENT_FG), "secondary": (CARD2, BORDER, FG),
                 "danger": (DANGER, DANGER_D, "#ffffff")}

        def __init__(self, parent, text, command=None, kind="secondary", big=False):
            self._col = self.KINDS[kind]
            self._cmd = command
            self._on = True
            super().__init__(parent, text=text, bg=self._col[0], fg=self._col[2], cursor="hand2",
                             font=(fam, 13, "bold") if big else FB, padx=30 if big else 16, pady=11 if big else 8)
            self.bind("<Enter>", lambda e: self._on and self.config(bg=self._col[1]))
            self.bind("<Leave>", lambda e: self.config(bg=self._col[0] if self._on else OFF))
            self.bind("<Button-1>", lambda e: self._on and self._cmd and self._cmd())

        def set_enabled(self, on):
            self._on = on
            self.config(bg=self._col[0] if on else OFF, fg=self._col[2] if on else "#667085",
                        cursor="hand2" if on else "arrow")

    class Seg(tk.Frame):
        """Segmented selector bound to a StringVar; options = [(value, label), ...]."""

        def __init__(self, parent, options, var, command=None, bg=CARD):
            super().__init__(parent, bg=bg)
            self._var, self._cmd, self._btns = var, command, {}
            for val, label in options:
                b = tk.Label(self, text=label, padx=14, pady=7, font=FB, cursor="hand2")
                b.pack(side="left", padx=(0, 6))
                b.bind("<Button-1>", lambda e, v=val: self._pick(v))
                self._btns[val] = b
            var.trace_add("write", lambda *a: self._paint())
            self._paint()

        def _pick(self, v):
            self._var.set(v)
            if self._cmd:
                self._cmd()

        def _paint(self):
            for val, b in self._btns.items():
                sel = self._var.get() == val
                b.config(bg=ACCENT if sel else CARD2, fg=ACCENT_FG if sel else FG)

    def card(parent, title):
        outer = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        tk.Label(outer, text=title, bg=CARD, fg=MUTED, font=SM, anchor="w").pack(fill="x", padx=18, pady=(14, 4))
        inner = tk.Frame(outer, bg=CARD)
        inner.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        return outer, inner

    def cap(parent, text, r, c, span=1):
        tk.Label(parent, text=text, bg=CARD, fg=MUTED, font=SM).grid(row=r, column=c, columnspan=span,
                                                                     sticky="w", pady=(12, 4), padx=(0, 12))

    def ent(parent, var):
        return tk.Entry(parent, textvariable=var, bg=FIELD, fg=FG, insertbackground=FG, relief="flat", font=F,
                        highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)

    def check(parent, text, var, command=None):
        return tk.Checkbutton(parent, text=text, variable=var, command=command, bg=CARD, fg=FG,
                              activebackground=CARD, activeforeground=FG, selectcolor=FIELD, font=F,
                              highlightthickness=0, bd=0, cursor="hand2", anchor="w")

    def listbox(parent, height=12):
        return tk.Listbox(parent, bg=FIELD, fg=FG, selectbackground=ACCENT, selectforeground=ACCENT_FG, font=F,
                          relief="flat", highlightthickness=1, highlightbackground=BORDER, activestyle="none",
                          exportselection=False, height=height)

    root.report_callback_exception = lambda *a: write_error("".join(traceback.format_exception(*a)))

    # ---- settings & state
    sp = ROOT / "settings.json"
    try:
        st = json.loads(sp.read_text("utf-8")) if sp.exists() else {}
    except Exception:
        st = {}
    q, cancel = queue.Queue(), threading.Event()
    state = {"core": None, "busy": False, "hw": None, "hits": []}
    mods_by_loader = st.get("mods", {})
    inst_info = st.get("inst_info", {})

    v_ver = tk.StringVar(value=st.get("version", "1.20.1"))
    v_name = tk.StringVar(value=st.get("name", "Player"))
    v_loader = tk.StringVar(value=st.get("loader", "fabric") if st.get("loader") in [l.lower() for l in LOADERS] else "fabric")
    v_tier = tk.StringVar(value=st.get("tier", "auto") if st.get("tier") in ("auto",) + tuple(TIERS) else "auto")
    v_ram_auto = tk.BooleanVar(value=st.get("ram_auto", True))
    v_ram = tk.IntVar(value=st.get("ram") or 2560)
    v_server = tk.StringVar(value=st.get("server", ""))
    v_java = tk.StringVar(value=st.get("java", "auto"))
    v_inst = tk.StringVar(value=st.get("inst", ""))
    v_mods_on = tk.BooleanVar(value=st.get("mods_on", True))
    v_dry = tk.BooleanVar(value=False)
    v_search = tk.StringVar(value="")

    def put(t):
        q.put(("log", t))

    def effective():
        hw = state["hw"] or DEFAULT_HW
        tier = classify(hw) if v_tier.get() == "auto" else v_tier.get()
        ram = auto_ram(hw, tier) if v_ram_auto.get() else int(v_ram.get())
        return tier, ram

    def settings_save():
        try:
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(json.dumps({
                "version": v_ver.get(), "name": v_name.get(), "loader": v_loader.get(), "tier": v_tier.get(),
                "ram": int(v_ram.get()), "ram_auto": v_ram_auto.get(), "server": v_server.get(),
                "java": v_java.get(), "inst": v_inst.get(), "mods_on": v_mods_on.get(),
                "mods": mods_by_loader, "inst_info": inst_info}), "utf-8")
        except Exception:
            pass

    # ================================================================ layout skeleton
    side = tk.Frame(root, bg=SIDE, width=200)
    side.pack(side="left", fill="y")
    side.pack_propagate(False)
    content = tk.Frame(root, bg=BG)
    content.pack(side="right", fill="both", expand=True)

    tk.Label(side, text="rmcl", bg=SIDE, fg=ACCENT, font=(fam, 24, "bold")).pack(anchor="w", padx=22, pady=(24, 0))
    tk.Label(side, text=f"launcher  v{VERSION}", bg=SIDE, fg=MUTED, font=(fam, 9)).pack(anchor="w", padx=24, pady=(0, 22))

    pages, navs = {}, {}

    def show(name):
        pages[name].tkraise()
        for n, lbl in navs.items():
            lbl.config(bg=CARD if n == name else SIDE, fg=ACCENT if n == name else FG)

    for key, text in (("play", "Играть"), ("mods", "Моды"), ("hw", "Железо"), ("log", "Журнал")):
        lbl = tk.Label(side, text="   " + text, bg=SIDE, fg=FG, font=(fam, 11, "bold"), anchor="w", pady=11,
                       cursor="hand2")
        lbl.pack(fill="x")
        lbl.bind("<Button-1>", lambda e, k=key: show(k))
        navs[key] = lbl
        pg = tk.Frame(content, bg=BG)
        pg.place(relx=0, rely=0, relwidth=1, relheight=1)
        pages[key] = pg

    chip = tk.Label(side, text="", bg=SIDE, fg=MUTED, font=(fam, 9), justify="left", anchor="w", wraplength=170)
    chip.pack(side="bottom", fill="x", padx=18, pady=18)

    def header(page, title, sub):
        tk.Label(page, text=title, bg=BG, fg=FG, font=H1, anchor="w").pack(fill="x", padx=30, pady=(26, 0))
        lbl = tk.Label(page, text=sub, bg=BG, fg=MUTED, font=F, anchor="w")
        lbl.pack(fill="x", padx=30, pady=(0, 16))
        return lbl

    # ================================================================ PLAY page
    pp = pages["play"]
    header(pp, "Играть", "Выберите версию и загрузчик, остальное лаунчер сделает сам.")
    c_play, g = card(pp, "ЗАПУСК")
    c_play.pack(fill="x", padx=30)
    g.columnconfigure(0, weight=1, uniform="c")
    g.columnconfigure(1, weight=1, uniform="c")
    cap(g, "ВЕРСИЯ MINECRAFT", 0, 0)
    cb = ttk.Combobox(g, textvariable=v_ver, values=["latest", "1.21.1", "1.20.1", "1.18.2", "1.16.5"], style="D.TCombobox")
    cb.grid(row=1, column=0, sticky="ew", padx=(0, 12))
    cap(g, "НИК (ОФЛАЙН)", 0, 1)
    ent(g, v_name).grid(row=1, column=1, sticky="ew", ipady=7)
    cap(g, "ЗАГРУЗЧИК МОДОВ", 2, 0, 2)
    loader_note = tk.Label(g, text="", bg=CARD, fg=MUTED, font=(fam, 9), anchor="w")

    def on_loader():
        loader_note.config(text=LOADER_NOTES.get(v_loader.get(), ""))
        refresh_mods()
        refresh()

    Seg(g, [(l.lower(), l) for l in LOADERS], v_loader, on_loader).grid(row=3, column=0, columnspan=2, sticky="w")
    loader_note.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
    cap(g, "СЕРВЕР (ПУСТО = ОДИНОЧНАЯ ИГРА)", 5, 0)
    ent(g, v_server).grid(row=6, column=0, sticky="ew", padx=(0, 12), ipady=7)
    cap(g, "СБОРКА / ПАПКА (ПУСТО = АВТО)", 5, 1)
    ent(g, v_inst).grid(row=6, column=1, sticky="ew", ipady=7)

    actions = tk.Frame(pp, bg=BG)
    actions.pack(fill="x", padx=30, pady=18)
    b_play = Btn(actions, "▶  ИГРАТЬ", kind="primary", big=True)
    b_install = Btn(actions, "Установить")
    b_stop = Btn(actions, "Стоп", kind="danger")
    b_play.pack(side="left")
    b_install.pack(side="left", padx=(12, 0))
    b_stop.pack(side="left", padx=(12, 0))
    b_stop.set_enabled(False)

    status = tk.Label(pp, text="Готов к запуску", bg=BG, fg=MUTED, font=F, anchor="w")
    status.pack(fill="x", padx=30)
    progress = ttk.Progressbar(pp, mode="indeterminate", style="G.Horizontal.TProgressbar")
    progress.pack(fill="x", padx=30, pady=(6, 0))
    play_chip = tk.Label(pp, text="", bg=BG, fg=MUTED, font=F, anchor="w", justify="left")
    play_chip.pack(fill="x", padx=30, pady=(18, 0))

    # ================================================================ MODS page
    mp = pages["mods"]
    mods_sub = header(mp, "Моды", "")
    body = tk.Frame(mp, bg=BG)
    body.pack(fill="both", expand=True, padx=30, pady=(0, 24))
    body.columnconfigure(0, weight=1, uniform="m")
    body.columnconfigure(1, weight=1, uniform="m")
    body.rowconfigure(0, weight=1)

    c_s, s_in = card(body, "ПОИСК НА MODRINTH")
    c_s.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
    srow = tk.Frame(s_in, bg=CARD)
    srow.pack(fill="x")
    e_search = ent(srow, v_search)
    e_search.pack(side="left", fill="x", expand=True, ipady=6)
    b_search = Btn(srow, "Найти")
    b_search.pack(side="left", padx=(8, 0))
    lb_hits = listbox(s_in)
    lb_hits.pack(fill="both", expand=True, pady=10)
    b_add = Btn(s_in, "Добавить в сборку  →", kind="primary")
    b_add.pack(anchor="e")

    c_b, b_in = card(body, "СБОРКА МОДОВ")
    c_b.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
    lb_mods = listbox(b_in)
    lb_mods.pack(fill="both", expand=True, pady=(0, 10))
    brow = tk.Frame(b_in, bg=CARD)
    brow.pack(fill="x")
    b_remove = Btn(brow, "Убрать")
    b_preset = Btn(brow, "Пресет под моё ПК")
    b_clear = Btn(brow, "Очистить")
    for b in (b_remove, b_preset, b_clear):
        b.pack(side="left", padx=(0, 8))
    check(b_in, "Устанавливать моды из этого списка при запуске", v_mods_on).pack(fill="x", pady=(12, 0))
    tk.Label(b_in, text="Обязательные зависимости (например Fabric API) подтянутся автоматически.",
             bg=CARD, fg=MUTED, font=(fam, 9), anchor="w").pack(fill="x", pady=(2, 8))
    b_pack = Btn(b_in, "Импорт сборки (.mrpack)…")
    b_pack.pack(anchor="w")

    def get_mods():
        ld = v_loader.get()
        if ld == "vanilla":
            return []
        if ld not in mods_by_loader:
            mods_by_loader[ld] = preset_mods(ld, effective()[0])
        return mods_by_loader[ld]

    def refresh_mods():
        lb_mods.delete(0, "end")
        for m in get_mods():
            lb_mods.insert("end", m)
        ld = v_loader.get()
        mods_sub.config(text="Для чистой игры моды не нужны." if ld == "vanilla"
                        else f"Сборка для {dict((l.lower(), l) for l in LOADERS)[ld]} · Minecraft {v_ver.get()}")

    def do_search():
        qs, ld, ver = v_search.get().strip(), v_loader.get(), v_ver.get().strip()
        if not qs or ld == "vanilla":
            return

        def run():
            try:
                q.put(("hits", mr_search(qs, Core(put, cancel).resolve(ver), ld)))
            except Exception as e:
                put(f"Поиск не удался: {e}")
        threading.Thread(target=run, daemon=True).start()

    def add_selected():
        sel = lb_hits.curselection()
        if sel and sel[0] < len(state["hits"]) and v_loader.get() != "vanilla":
            slug = state["hits"][sel[0]][0]
            mods = get_mods()
            if slug not in mods:
                mods.append(slug)
            refresh_mods()

    def remove_selected():
        sel = lb_mods.curselection()
        if sel and sel[0] < len(get_mods()):
            del get_mods()[sel[0]]
            refresh_mods()

    def preset_click():
        if v_loader.get() != "vanilla":
            mods_by_loader[v_loader.get()] = preset_mods(v_loader.get(), effective()[0])
            refresh_mods()

    def clear_click():
        if v_loader.get() != "vanilla":
            mods_by_loader[v_loader.get()] = []
            refresh_mods()

    def pack_click():
        path = filedialog.askopenfilename(filetypes=[("Modrinth modpack", "*.mrpack"), ("Все файлы", "*.*")])
        if path:
            start("mrpack", path)

    b_search.config(); b_search._cmd = do_search
    b_add._cmd, b_remove._cmd, b_preset._cmd, b_clear._cmd, b_pack._cmd = \
        add_selected, remove_selected, preset_click, clear_click, pack_click
    e_search.bind("<Return>", lambda e: do_search())

    # ================================================================ HARDWARE page
    hp = pages["hw"]
    header(hp, "Железо и оптимизация", "Лаунчер определяет ваш ПК и подбирает память, GC и настройки графики.")
    c_hw, hw_in = card(hp, "ВАШ КОМПЬЮТЕР")
    c_hw.pack(fill="x", padx=30)
    hw_text = tk.Label(hw_in, text="Определяю железо…", bg=CARD, fg=FG, font=F, justify="left", anchor="w")
    hw_text.pack(fill="x")
    hw_rec = tk.Label(hw_in, text="", bg=CARD, fg=ACCENT, font=FB, anchor="w")
    hw_rec.pack(fill="x", pady=(8, 0))

    c_tier, t_in = card(hp, "ПРОФИЛЬ ОПТИМИЗАЦИИ")
    c_tier.pack(fill="x", padx=30, pady=14)
    Seg(t_in, [("auto", "Авто")] + [(k, v["label"]) for k, v in TIERS.items()], v_tier, lambda: refresh()).pack(anchor="w")
    tier_sum = tk.Label(t_in, text="", bg=CARD, fg=MUTED, font=F, justify="left", anchor="w")
    tier_sum.pack(fill="x", pady=(10, 4))
    check(t_in, "Подбирать память автоматически", v_ram_auto, lambda: refresh()).pack(fill="x", pady=(6, 0))
    ram_scale = tk.Scale(t_in, from_=1024, to=8192, resolution=256, orient="horizontal", showvalue=0, bg=CARD,
                         fg=FG, troughcolor=FIELD, highlightthickness=0, activebackground=ACCENT, bd=0,
                         sliderrelief="flat", length=420,
                         command=lambda v: (not v_ram_auto.get()) and v_ram.set(int(float(v))))
    ram_scale.pack(anchor="w")
    ram_lbl = tk.Label(t_in, text="", bg=CARD, fg=FG, font=FB, anchor="w")
    ram_lbl.pack(fill="x")

    c_adv, a_in = card(hp, "ДОПОЛНИТЕЛЬНО")
    c_adv.pack(fill="x", padx=30)
    cap(a_in, "JAVA (auto = СКАЧАТЬ САМОЙ)", 0, 0)
    ent(a_in, v_java).grid(row=1, column=0, sticky="ew", ipady=6)
    a_in.columnconfigure(0, weight=1)
    check(a_in, "Только показать команду запуска (не запускать игру)", v_dry).grid(row=2, column=0, sticky="w", pady=(10, 0))

    def sync_scale():
        tier, ram = effective()
        hw = state["hw"] or DEFAULT_HW
        top = max(2048, min(16384, ((hw.get("ram_mb") or 8192) // 256) * 256))
        ram_scale.config(state="normal", to=top)
        ram_scale.set(ram)
        if v_ram_auto.get():
            ram_scale.config(state="disabled")
        else:
            v_ram.set(ram)

    def refresh():
        hw = state["hw"] or DEFAULT_HW
        tier, ram = effective()
        if state["hw"]:
            gp = ", ".join(g + (" (слабая)" if gpu_is_weak(g) else "") for g in hw["gpus"]) or "не определена"
            hw_text.config(text=(f"Процессор: {hw['cpu_name'] or '—'}\n"
                                 f"Ядра / потоки: {hw['cores']} / {hw['threads']}\n"
                                 f"Оперативная память: {(hw['ram_mb'] or 0) / 1024:.1f} ГБ\n"
                                 f"Видеокарта: {gp}"))
            hw_rec.config(text=f"Рекомендуемый профиль: {TIERS[classify(hw)]['label']}")
        flags = jvm_flags(tier, False, hw.get("threads"), hw.get("cores"))
        par = next((f.split("=")[1] for f in flags if f.startswith("-XX:ParallelGCThreads=")), "?")
        rd = re.search(r"renderDistance:(\d+)", TIERS[tier]["options"]).group(1)
        n = len(preset_mods(v_loader.get(), tier))
        tier_sum.config(text=f"Профиль: {TIERS[tier]['label']}   ·   память {ram} МБ   ·   потоков GC: {par}   ·   "
                             f"дальность прорисовки: {rd}   ·   модов в пресете: {n}")
        ram_lbl.config(text=f"Память игры: {ram} МБ" + ("  (авто)" if v_ram_auto.get() else ""))
        chip.config(text=f"Профиль: {TIERS[tier]['label']}\nПамять: {ram} МБ")
        play_chip.config(text=f"Профиль оптимизации: {TIERS[tier]['label']}   ·   память: {ram} МБ   ·   "
                              f"загрузчик: {v_loader.get()}")
        sync_scale()

    # ================================================================ LOG page
    lp = pages["log"]
    header(lp, "Журнал", "Вывод установки и самой игры.")
    lrow = tk.Frame(lp, bg=BG)
    lrow.pack(fill="both", expand=True, padx=30, pady=(0, 24))
    log = tk.Text(lrow, bg=FIELD, fg=FG, insertbackground=FG, relief="flat", font=MONO, wrap="word",
                  highlightthickness=1, highlightbackground=BORDER, state="disabled")
    sb = ttk.Scrollbar(lrow, orient="vertical", command=log.yview, style="D.Vertical.TScrollbar")
    log.config(yscrollcommand=sb.set)
    lbtn = tk.Frame(lp, bg=BG)
    lbtn.pack(fill="x", padx=30, pady=(0, 20), side="bottom")
    sb.pack(side="right", fill="y")
    log.pack(side="left", fill="both", expand=True)

    def clear_log():
        log.config(state="normal")
        log.delete("1.0", "end")
        log.config(state="disabled")

    def open_data():
        try:
            ROOT.mkdir(parents=True, exist_ok=True)
            os.startfile(str(ROOT)) if IS_WIN else subprocess.Popen(["xdg-open", str(ROOT)])
        except Exception as e:
            put(f"Не удалось открыть папку: {e}")

    Btn(lbtn, "Очистить", clear_log).pack(side="left")
    Btn(lbtn, "Открыть папку данных", open_data).pack(side="left", padx=10)

    # ================================================================ jobs
    def set_busy(on):
        state["busy"] = on
        b_play.set_enabled(not on)
        b_install.set_enabled(not on)
        b_pack.set_enabled(not on)
        b_stop.set_enabled(on)
        if on:
            progress.start(12)
        else:
            progress.stop()

    def job(mode, cfg):
        core = Core(put, cancel)
        state["core"] = core
        try:
            if mode == "mrpack":
                info = core.import_mrpack(cfg["pack"])
                q.put(("pack", info))
                put(f"Сборка «{info['name']}» установлена ({info['loader']} {info['mc']}). Нажмите «Играть».")
                return
            mc, loader = core.resolve(cfg["version"]), cfg["loader"]
            java = cfg["java"].strip()
            req = core.vanilla_java(mc)
            if java.lower() in ("", "java", "auto"):
                java = core.ensure_java(req)
            inst = cfg["inst"] or (mc if loader == "vanilla" else f"{mc}-{loader}")
            info = cfg["inst_info"].get(inst) or {}
            vid = core.install_loader(loader, mc, java, info.get("lver") if info.get("loader") == loader else None)
            prof, jar = core.prepare(vid)
            game_dir = ROOT / "instances" / inst
            if loader != "vanilla" and cfg["mods_on"]:
                core.install_mods(mc, loader, game_dir / "mods", cfg["mods"])
            if mode == "play":
                core.launch(prof, jar, {"name": cfg["name"] or "Player", "server": cfg["server"].strip() or None,
                                        "ram": cfg["ram"], "java": java, "tier": cfg["tier"], "zgc": False,
                                        "game_dir": game_dir, "hw": cfg["hw"]}, dry_run=cfg["dry"])
            else:
                put(f"Готово: {vid}")
        except Cancelled:
            put("Отменено.")
        except Exception:
            put(traceback.format_exc())
        finally:
            q.put(("done",))

    def start(mode, pack=None):
        if state["busy"]:
            return
        settings_save()
        cancel.clear()
        set_busy(True)
        status.config(text="Работаю…")
        tier, ram = effective()
        cfg = {"version": v_ver.get().strip(), "name": v_name.get().strip(), "loader": v_loader.get(),
               "tier": tier, "ram": ram, "server": v_server.get(), "java": v_java.get(), "dry": v_dry.get(),
               "inst": v_inst.get().strip(), "inst_info": dict(inst_info), "mods_on": v_mods_on.get(),
               "mods": list(get_mods()), "hw": state["hw"] or DEFAULT_HW, "pack": pack}
        threading.Thread(target=job, args=(mode, cfg), daemon=True).start()

    def stop():
        cancel.set()
        c = state["core"]
        if c and c.proc:
            c.proc.terminate()

    b_play._cmd = lambda: start("play")
    b_install._cmd = lambda: start("install")
    b_stop._cmd = stop

    def bg_hw():
        q.put(("hw", detect_hardware()))

    def bg_versions():
        try:
            m = get_json(MANIFEST_URL)
            q.put(("versions", ["latest"] + [v["id"] for v in m["versions"] if v["type"] == "release"][:120]))
        except Exception as e:
            put(f"Не удалось получить список версий: {e}")

    def poll():
        try:
            while True:
                m = q.get_nowait()
                if m[0] == "log":
                    log.config(state="normal")
                    log.insert("end", m[1] + "\n")
                    log.see("end")
                    log.config(state="disabled")
                    status.config(text=m[1][:110])
                elif m[0] == "versions":
                    cb["values"] = m[1]
                elif m[0] == "hw":
                    state["hw"] = m[1]
                    refresh()
                    refresh_mods()
                elif m[0] == "hits":
                    state["hits"] = m[1]
                    lb_hits.delete(0, "end")
                    for slug, title, desc in m[1]:
                        lb_hits.insert("end", f"{title}  ({slug})")
                elif m[0] == "pack":
                    info = m[1]
                    if info.get("mc"):
                        v_ver.set(info["mc"])
                    v_loader.set(info["loader"])
                    v_inst.set(info["name"])
                    v_mods_on.set(False)
                    inst_info[info["name"]] = {"loader": info["loader"], "lver": info["lver"]}
                    settings_save()
                    on_loader()
                elif m[0] == "done":
                    set_busy(False)
                    status.config(text="Готово.")
        except queue.Empty:
            pass
        finally:
            root.after(100, poll)

    def on_close():
        settings_save()
        stop()
        root.destroy()

    put(f"Данные: {ROOT}")
    put("Первый запуск долгий: скачиваются Java, игра и моды.")
    on_loader()
    show("play")
    threading.Thread(target=bg_hw, daemon=True).start()
    threading.Thread(target=bg_versions, daemon=True).start()
    root.after(100, poll)
    root.protocol("WM_DELETE_WINDOW", on_close)
    try:
        root.mainloop()
    except Exception:
        write_error(traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.excepthook = lambda *a: write_error("".join(traceback.format_exception(*a)))
    try:
        main()
    except Exception:
        write_error(traceback.format_exc())
        try:
            from tkinter import messagebox
            messagebox.showerror("rmcl", f"Ошибка, подробности: {ROOT / 'error.log'}")
        except Exception:
            pass
