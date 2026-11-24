#!/usr/bin/env python3
"""给板端网关配置补上音乐相关的两段（幂等）。

1) plugins.play_music —— 离线库目录与扩展名。
2) Intent.function_call.functions 里加上 play_music —— **这一步不能漏**。
   这套 fork 的工具暴露是白名单: core/providers/tools/server_plugins/
   plugin_executor.py 的 get_tools() 只暴露
   ["handle_exit_intent", "get_lunar"] 加上配置里列出的名字, 其余函数
   虽然被 auto_import_modules 注册进 all_function_registry, 但不会进
   给 LLM 的 tools 列表。列表是空的话, LLM 压根看不到 play_music 这个工具,
   问"播放音乐"只会得到一句"对不起, 我无法直接播放音乐"(2026-11-23 实测)。

为什么 music_dir 必须是绝对路径:
  网关的 cwd 是 /data/xiaorui-gateway/work, play_music 插件默认取
  os.path.abspath("./music") -> work/music, 而在线歌单的路径是按
  music_dir 的**同级目录**推出来的(join(dirname(music_dir), "music-online",
  "playlist.json"))。写成相对路径时歌单会落到 work/music-online, 离线库落到
  work/music —— 两处都得靠软链兜着, 很容易只补一处就"电台没声音"。
  绝对路径把两者固定到 server/ 同一层: server/music 与
  server/music-online/playlist.json。

用法:
  python3 patch-music-config.py --config /path/to/.config.yaml
  python3 patch-music-config.py --config ... --music-dir /data/.../music
"""
import argparse
from pathlib import Path

import yaml


DEFAULT_MUSIC_DIR = "/data/xiaorui-gateway/server/music"
MUSIC_EXT = [".p3", ".wav", ".mp3"]
MUSIC_TOOL = "play_music"


def patch(config, music_dir=DEFAULT_MUSIC_DIR):
    """就地补音乐配置; 返回是否真的改了。"""
    changed = False

    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        config["plugins"] = plugins
    existing = plugins.get(MUSIC_TOOL)
    if not (isinstance(existing, dict) and existing.get("music_dir") == music_dir):
        plugins[MUSIC_TOOL] = {
            "music_dir": music_dir,
            "music_ext": list(MUSIC_EXT),
            "refresh_time": 300,
        }
        changed = True

    # 工具白名单: 把 play_music 暴露给 LLM
    intent = config.get("Intent")
    if not isinstance(intent, dict):
        intent = {}
        config["Intent"] = intent
    function_call = intent.get("function_call")
    if not isinstance(function_call, dict):
        function_call = {"type": "function_call"}
        intent["function_call"] = function_call
    functions = function_call.get("functions")
    if not isinstance(functions, list):
        functions = []
        function_call["functions"] = functions
    if MUSIC_TOOL not in functions:
        functions.append(MUSIC_TOOL)
        changed = True

    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True,
                        help="板端 .config.yaml 路径")
    parser.add_argument("--music-dir", default=DEFAULT_MUSIC_DIR,
                        help="离线音乐库目录(绝对路径)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印结果, 不写回文件")
    args = parser.parse_args()

    with args.config.open(encoding="utf-8") as source:
        config = yaml.safe_load(source)
    if not isinstance(config, dict):
        raise SystemExit(f"配置不是 YAML mapping: {args.config}")

    changed = patch(config, args.music_dir)
    if args.dry_run:
        print(f"dry-run: {'需要改' if changed else '已是目标状态'}")
        print(yaml.safe_dump(
            {"plugins": config.get("plugins", {}),
             "Intent": config.get("Intent", {})},
            allow_unicode=True))
        return
    if changed:
        args.config.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
    print(f"{args.config}: {'已补音乐配置' if changed else '已是目标状态'}")


if __name__ == "__main__":
    main()
