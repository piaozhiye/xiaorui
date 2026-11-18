#!/data/gwvenv/bin/python
"""RockASR/RockTTS 部署自检: 拉起两个 helper, 各跑一次真实请求.

放在 ports/rk3562-es8388/ 由 deploy-rock.sh 推到板上执行。
验证链路: .so 依赖 -> license -> 模型加载 -> 帧协议 -> 真实推理。
"""
import sys

sys.path.insert(0, "/data/xiaorui-gateway/server")

from core.providers.rock.helper_proc import HelperProcess  # noqa: E402

ASR_ARGS = [
    "--lib-dir", "/data/xiaorui-server/rockasr/lib",
    "--data-dir", "/data/xiaorui-server/rockasr/data",
    "--auth-config", "/data/xiaorui-server/rockasr/rockx_auth_config.json",
    "--lang", "zh", "--timeout", "20",
]
TTS_ARGS = [
    "--lib-dir", "/data/xiaorui-server/rocktts/lib",
    "--data-dir", "/data/xiaorui-server/rocktts/data",
    "--auth-config", "/data/xiaorui-server/rocktts/rockx_auth_config.json",
    "--alpha", "1.0", "--timeout", "30",
]


def main():
    ok = True

    asr = HelperProcess("asr_helper.py", ASR_ARGS,
                        env_extra={"LD_LIBRARY_PATH": ASR_ARGS[1]})
    try:
        # 1.2s 静音(正弦渐入渐出都不要, 就静音): 验证推理管线通, 不验证识别质量
        pcm = b"\x00\x00" * 19200
        h, _ = asr.request({"cmd": "asr"}, body=pcm, timeout=60)
        print(f"ASR 自检通过: elapsed={h.get('elapsed_s')}s text={h.get('text')!r}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"ASR 自检失败: {e}")
    finally:
        asr.stop()

    tts = HelperProcess("tts_helper.py", TTS_ARGS,
                        env_extra={"LD_LIBRARY_PATH": TTS_ARGS[1]})
    try:
        h, pcm = tts.request({"cmd": "tts", "text": "部署自检", "alpha": 1.0},
                             timeout=60)
        print(f"TTS 自检通过: elapsed={h.get('elapsed_s')}s "
              f"pcm={len(pcm)}B({len(pcm)//2/24000:.2f}s@24k)")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"TTS 自检失败: {e}")
    finally:
        tts.stop()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
