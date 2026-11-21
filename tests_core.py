import sys, json, tempfile, zipfile, threading
from pathlib import Path
sys.path.insert(0, ".")
import rmcl

tmp = Path(tempfile.mkdtemp()); rmcl.ROOT = tmp
logs = []
core = rmcl.Core(logs.append, threading.Event())

# offline uuid must equal Java's UUID.nameUUIDFromBytes("OfflinePlayer:Notch")
assert rmcl.offline_uuid("Notch") == "b50ad385-829d-3141-a216-7e7d7539ba7f", rmcl.offline_uuid("Notch")
assert rmcl.maven_path("net.fabricmc:fabric-loader:0.16.9") == "net/fabricmc/fabric-loader/0.16.9/fabric-loader-0.16.9.jar"
assert rmcl.maven_path("org.lwjgl:lwjgl:3.3.3:natives-windows") .endswith("lwjgl-3.3.3-natives-windows.jar")
assert rmcl.split_host_port("a.b:1234") == ("a.b", 1234) and rmcl.split_host_port("a.b") == ("a.b", 25565)

# rules
deny_other = [{"action": "allow"}, {"action": "disallow", "os": {"name": "osx"}}]
assert rmcl.rules_allow(deny_other, {}) is True
assert rmcl.rules_allow([{"action": "allow", "os": {"name": "osx"}}], {}) is False
assert rmcl.rules_allow([{"action": "allow", "features": {"is_demo_user": True}}], {}) is False
assert rmcl.rules_allow([{"action": "allow", "features": {"is_demo_user": True}}], {"is_demo_user": True}) is True

# modern vanilla profile (1.20-style)
vanilla = {
 "id": "1.20.1", "type": "release", "mainClass": "net.minecraft.client.main.Main",
 "javaVersion": {"majorVersion": 17}, "assetIndex": {"id": "5", "url": "x"},
 "libraries": [
   {"name": "com.mojang:logging:1.1.1", "downloads": {"artifact": {"path": "com/mojang/logging/1.1.1/logging-1.1.1.jar", "url": "u", "sha1": "a"}}},
   {"name": "org.lwjgl:lwjgl:3.3.1:natives-osx", "rules": [{"action": "allow", "os": {"name": "osx"}}],
    "downloads": {"artifact": {"path": "org/lwjgl/osx.jar", "url": "u"}}}],
 "arguments": {
   "game": ["--username", "${auth_player_name}", "--version", "${version_name}", "--gameDir", "${game_directory}",
            "--assetsDir", "${assets_root}", "--uuid", "${auth_uuid}", "--accessToken", "${auth_access_token}",
            {"rules": [{"action": "allow", "features": {"is_demo_user": True}}], "value": "--demo"},
            {"rules": [{"action": "allow", "features": {"is_quick_play_multiplayer": True}}],
             "value": ["--quickPlayMultiplayer", "${quickPlayMultiplayer}"]}],
   "jvm": [{"rules": [{"action": "allow", "os": {"name": "windows"}}], "value": "-XX:HeapDumpPath=x"},
           "-Djava.library.path=${natives_directory}", "-cp", "${classpath}"]}}
fabric = {"id": "fabric-loader-0.16.9-1.20.1", "inheritsFrom": "1.20.1", "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
          "libraries": [{"name": "net.fabricmc:fabric-loader:0.16.9", "url": "https://maven.fabricmc.net/", "sha1": "abc"}],
          "arguments": {"game": [], "jvm": ["-DFabricMcEmu= net.minecraft.client.main.Main "]}}

for vid, d in (("1.20.1", vanilla), ("fabric-loader-0.16.9-1.20.1", fabric)):
    p = tmp / "versions" / vid; p.mkdir(parents=True); (p / f"{vid}.json").write_text(json.dumps(d))
prof, jar = core.load_profile("fabric-loader-0.16.9-1.20.1")
assert prof["mainClass"].endswith("KnotClient") and jar.name == "1.20.1.jar"
names = [l["name"] for l in prof["libraries"]]
assert names[0].startswith("net.fabricmc") and len(names) == 3
ents = [e for l in prof["libraries"] for e in rmcl.lib_entries(l)]
fab = [e for e in ents if "fabric-loader" in e["rel"]][0]
assert fab["url"] == "https://maven.fabricmc.net/net/fabricmc/fabric-loader/0.16.9/fabric-loader-0.16.9.jar" and fab["sha1"] == "abc"
assert not any("osx" in e["rel"] for e in ents)  # OS rule filtered (we are on linux)

gd = tmp / "instances" / "t"
(tmp / "assets" / "indexes").mkdir(parents=True); (tmp / "assets" / "indexes" / "5.json").write_text('{"objects":{}}')
o = {"name": "Steve", "server": None, "ram": 2560, "java": "java", "tier": "lowend", "game_dir": gd}
java, args = core.build_cmd(prof, jar, o)
s = " ".join(args)
assert "-Xmx2560M" in args and "-XX:ConcGCThreads=1" in s and "--demo" not in args and "--quickPlayMultiplayer" not in args
assert args[args.index("--username") + 1] == "Steve" and "${" not in s
assert args.index("net.fabricmc.loader.impl.launch.knot.KnotClient") > args.index("-cp")
assert ("HeapDumpPath" in s) == (rmcl.OS_NAME == "windows")  # windows-only rule

o["server"] = "play.example.com:25570"
_, args = core.build_cmd(prof, jar, o)
assert args[args.index("--quickPlayMultiplayer") + 1] == "play.example.com:25570"

# legacy (<=1.12) profile: minecraftArguments + --server/--port fallback
legacy = {"id": "1.12.2", "mainClass": "net.minecraft.client.main.Main", "assets": "1.12",
          "minecraftArguments": "--username ${auth_player_name} --session ${auth_session} --gameDir ${game_directory} --userProperties ${user_properties}",
          "libraries": []}
_, args = core.build_cmd(legacy, tmp / "x.jar", {**o, "server": "mc.test"})
assert args[-4:] == ["--server", "mc.test", "--port", "25565"] and "-Djava.library.path=" + str(tmp / "versions" / "1.12.2" / "natives") in args

# zip extraction + zip-slip guard + strip
zp = tmp / "t.zip"
with zipfile.ZipFile(zp, "w") as z:
    z.writestr("jre-17/bin/java.exe", "x"); z.writestr("jre-17/META-INF/a", "x"); z.writestr("../evil.txt", "x")
rmcl.unzip(zp, tmp / "out", exclude=("jre-17/META-INF/",), strip=1)
assert (tmp / "out" / "bin" / "java.exe").exists() and not (tmp / "out" / "META-INF").exists() and not (tmp / "evil.txt").exists()

# flags per tier
assert "-XX:+UseG1GC" in rmcl.jvm_flags("lowend") and "-XX:+UseZGC" in rmcl.jvm_flags("normal", True)
print("ALL TESTS PASSED")
