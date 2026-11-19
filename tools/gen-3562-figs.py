#!/usr/bin/env python3
"""生成《在 1 TOPS 的 RK3562 上做一套离线语音助手》配图。

产物(assets/article/):
  diag-3562-topology.txt/.svg/.png  链路框图(boxdiag 坐标画, 与文章§一对应)
  fig-3562-latency.png              延迟分解(调优前后两条堆叠条)
  fig-3562-mem.png                  四进程内存 + zram/OOM 注记
  cover-3562.png                    公众号封面(1085x484, 深蓝网格风)

    python3 tools/gen-3562-figs.py
"""
import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(HERE, "tools")
OUT = os.path.join(HERE, "assets", "article")
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, TOOLS)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


boxdiag = load("boxdiag", os.path.join(TOOLS, "boxdiag.py"))
figs = load("gen_article_figs", os.path.join(TOOLS, "gen-article-figs.py"))


# ================================================================ 框图
def topology():
    C = boxdiag.Canvas
    cv = C(100, 36)
    # 外框(板)
    cv.box(0, 1, 100, 32)
    cv.text_center(0, 99, 2, "RK3562 EVB2 · 4×A53@2GHz · 2GB DDR · NPU 1 TOPS")

    # 左列: 音频硬件
    cv.box(3, 6, 21, 4)
    cv.text(4, 7, "6×PDM 环形麦")
    cv.text(4, 8, "ch0-5")
    cv.box(3, 14, 21, 6)
    cv.text(4, 15, "RK809 codec")
    cv.text(4, 16, "喇叭 DAC 播放")
    cv.text(4, 17, "ADC 回采 ch6/7")
    cv.text(4, 18, "DADC 0xff(0dB)")
    cv.box(3, 24, 21, 3)
    cv.text(4, 25, "板载喇叭")

    # 中列: 采集链 + 前端
    cv.box(29, 6, 25, 6)
    cv.text(30, 7, "rkaudio_vqe (CPU)")
    cv.text(30, 8, "AEC/降噪/AGC/波束")
    cv.text(30, 9, "闭源·波束要授权")
    cv.text(30, 10, "ref=回采 ch7")
    cv.box(29, 16, 25, 8)
    cv.text(30, 17, "xiaorui_live (C)")
    cv.text(30, 18, "KWS zipformer 3.3M")
    cv.text(30, 19, "  阈值 0.15")
    cv.text(30, 20, "能量 VAD 门限 10dB")
    cv.text(30, 21, "播放/打断/跟随窗")

    # 右列: 网关 + 下游
    cv.box(61, 6, 35, 13)
    cv.text(62, 7, "板内网关 :8001")
    cv.text(62, 9, "SenseVoice ASR    CPU")
    cv.text(62, 10, "Qwen2.5-0.5B      NPU")
    cv.text(62, 11, " └ RKLLM 服务 :8112")
    cv.text(62, 12, "Matcha TTS 4线程  CPU")
    cv.text(62, 13, "语速 1.1")
    cv.text(62, 14, "意图直答: 音量/灯控")
    cv.box(61, 23, 35, 4)
    cv.text(62, 24, "Home Assistant :8123")
    cv.text(62, 25, "设备控制面 → 灯(无头引导)")
    cv.box(61, 30, 35, 3)
    cv.text(62, 31, "云端形态: dashscope(ASR+LLM)")

    # 连线
    cv.arrow_h(24, 29, 8, "8ch PDM")          # 麦 -> VQE
    cv.arrow_v(26, 13, 8, None, "up")       # RK809 回采汇入 8ch
    cv.text(18, 10, "回采")
    cv.arrow_v(41, 12, 16, "1ch 16k")         # VQE -> 前端
    cv.arrow_h(54, 61, 19, "ws 8001")         # 前端 -> 网关
    cv.arrow_h(29, 24, 17, None, "left")      # 前端 -> RK809 播放
    cv.text(23, 18, "hw:1,0")
    cv.arrow_v(13, 20, 24, "SPK")             # RK809 -> 喇叭
    cv.arrow_v(78, 19, 23, "REST")            # 网关 -> HA
    cv.arrow_v(78, 27, 30, "切换/回滚")        # HA 区 -> 云端(概念)

    # 底注
    cv.text(3, 28, "CPU: 3A/VAD/KWS/ASR/TTS")
    cv.text(3, 29, "NPU: 仅 Qwen2.5-0.5B")
    cv.text(3, 30, "内存: 四进程 1.7G+")
    cv.text(3, 31, "zram 768M(zstd)")

    txt = os.path.join(OUT, "diag-3562-topology.txt")
    cv.save(txt)
    subprocess.run([sys.executable, os.path.join(TOOLS, "ascii2svg.py"), txt,
                    "--svg", txt.replace(".txt", ".svg"),
                    "--png", txt.replace(".txt", ".png")], check=True)


# ================================================================ 延迟图
def latency():
    d, label, smart_font = figs, figs.label, figs.smart_font
    img, dr = figs.base("说完到出声:3 秒左右是怎么构成的",
                        "调优前后各段的耗时,标 TTS 4 线程/语速 1.1 那一刀")
    X0, X1 = 150, 1010
    T_MAX = 4.6          # 秒, 横轴满量程
    def sx(t):
        return int(X0 + (X1 - X0) * t / T_MAX)

    rows = [
        ("调优前", 120, [("静音判定 1.4s", 1.4, figs.BLUE_CYAN),
                          ("识别 1.0s", 1.0, figs.SLATE),
                          ("大模型+TTS 首段 2.0s", 2.0, figs.RED_ORANGE)], "4.4s"),
        ("调优后", 310, [("静音判定 1.4s", 1.4, figs.BLUE_CYAN),
                          ("识别 1.0s", 1.0, figs.SLATE),
                          ("大模型 0.5s", 0.5, figs.GREEN),
                          ("TTS 0.5s", 0.5, figs.ORANGE_RED)], "3.0s"),
    ]
    BH = 92
    for name, y, segs, total in rows:
        label(dr, (48, int(y + BH / 2)), name, 24, "#0F172A", bold=True, anchor="lm")
        x = X0
        acc = 0.0
        for text, t, (c1, c2) in segs:
            acc += t
            x_end = sx(acc)
            figs.grad_card(dr, (x, y, x_end, y + BH), c1, c2, radius=10, overlay_alpha=0.45)
            f = figs.smart_font(text, 17, bold=True)
            tw = dr.textlength(text, font=f)
            if x_end - x > tw + 20:      # 色块够宽: 标签画里面
                label(dr, (int((x + x_end) / 2), int(y + BH / 2)), text, 17, "#FFFFFF",
                      bold=True, anchor="mm")
            else:                        # 太窄: 画到条下方, 引线小竖线
                cx = int((x + x_end) / 2)
                dr.line([(cx, y + BH), (cx, y + BH + 14)], fill=c1, width=2)
                label(dr, (cx, y + BH + 18), text, 16, "#475569", anchor="mt")
            x = x_end
        label(dr, (X1 + 8, int(y + BH / 2)), total, 26, "#0F172A", bold=True, anchor="lm")

    # 时间刻度
    for t in (1, 2, 3, 4):
        x = sx(t)
        dr.line([(x, 440), (x, 448)], fill="#CBD5E1", width=2)
        label(dr, (x, 452), f"{t}s", 16, "#94A3B8", anchor="mm")
    dr.line([(X0, 444), (X1, 444)], fill="#CBD5E1", width=2)
    label(dr, (48, 470), "静音判定是防截尾的安全代价,没动;大模型+TTS 首段 2s 砍到 1s", 19, "#64748B")
    img.convert("RGB").save(os.path.join(OUT, "fig-3562-latency.png"))


# ================================================================ 内存图
def mem():
    label = figs.label
    img, dr = figs.base("2GB 板上四个常驻进程", "RSS 实测值;峰值那次是语音轮次里的网关")
    rows = [
        ("网关 (轮次峰值)", 764, figs.RED_ORANGE, "764M"),
        ("网关 (平均)", 653, figs.ORANGE_RED, "653M"),
        ("RKLLM (NPU)", 613, figs.PURPLE, "613M"),
        ("Home Assistant", 258, figs.GREEN, "258M"),
        ("前端 xiaorui_live", 68, figs.SKY_INDIGO, "68M"),
    ]
    X0 = 300
    X1 = 880          # 764M 满量程
    y = 130
    for name, mb, pal, text in rows:
        label(dr, (48, int(y + 22)), name, 21, "#0F172A", anchor="lm")
        x1 = int(X0 + (X1 - X0) * mb / 764.0)
        figs.grad_card(dr, (X0, int(y), x1, int(y + 44)), *pal, radius=9, overlay_alpha=0.5)
        label(dr, (x1 + 12, int(y + 22)), text, 20, "#0F172A", bold=True, anchor="lm")
        y += 64
    label(dr, (48, y + 28), "合计 1.7G / 板载 1963M", 24, "#0F172A", bold=True)
    label(dr, (48, y + 64), "zram swap 768M (zstd, 压缩比 4.4x —— lzo 是 1.0,别用)", 19, "#64748B")
    label(dr, (48, y + 96), "oom_score_adj: 网关/RKLLM -500 · HA +200 (挤爆先牺牲 HA)", 19, "#64748B")
    label(dr, (48, y + 128), "消停后 available 110~240M,挂一夜无重启", 19, "#64748B")
    img.convert("RGB").save(os.path.join(OUT, "fig-3562-mem.png"))


# ================================================================ 封面
def cover():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.font_manager import FontProperties
    from matplotlib.patches import FancyBboxPatch, Rectangle

    BG, GRID = "#0A1322", "#16263E"
    CYAN, BLUE, ORANGE, DIM, LINE = "#2DD4F5", "#4F9CFF", "#FF9F45", "#8FA3BF", "#2A4468"
    fp = lambda w: FontProperties(fname="/usr/share/fonts/opentype/noto/NotoSansCJK-%s.ttc" % w)

    fig = plt.figure(figsize=(10.85, 4.84), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(BG); fig.patch.set_facecolor(BG)
    ax.set_xlim(0, 1085); ax.set_ylim(0, 484); ax.invert_yaxis(); ax.axis("off")
    for x in range(0, 1085, 56):
        ax.axvline(x, color=GRID, lw=0.8, zorder=0)
    for y in range(0, 484, 56):
        ax.axhline(y, color=GRID, lw=0.8, zorder=0)

    # 左侧标题块
    ax.text(56, 128, "RK3562 · 1T NPU", fontproperties=fp("Bold"), fontsize=40, color="#E9F2FB")
    ax.text(56, 186, "打造个人端侧 AI 助手", fontproperties=fp("Bold"), fontsize=40, color="#E9F2FB")
    ax.text(58, 236, "3A / 唤醒 / 识别 / 大模型 / TTS · 端云双模", fontproperties=fp("Regular"), fontsize=17, color=CYAN)
    ax.text(58, 428, "NPU 上只住了大模型 · 2GB 才是真正的墙", fontproperties=fp("Regular"), fontsize=15, color=DIM)
    ax.text(58, 454, "代码开源 · github.com/piaozhiye/xiaorui", fontproperties=fp("Regular"), fontsize=15, color="#7C93B5")

    # 右侧示意: 6 麦 → 芯片(NPU 高亮) → 喇叭
    t = np.linspace(0, 1, 200)
    wave = lambda a: a * np.sin(2 * np.pi * 9 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 1.3 * t))
    ax.plot(700 + t * 60, 150 + wave(16), color=CYAN, lw=2)
    for i in range(6):
        ax.add_patch(Rectangle((676, 118 + i * 12), 14, 6, color=CYAN, alpha=0.85))
        ax.text(654, 121 + i * 12, "ch%d" % i, fontsize=7, color=DIM, ha="right", va="center")
    chip = FancyBboxPatch((780, 96), 200, 130, boxstyle="round,pad=0.02,rounding_size=10",
                          fc="#101E33", ec=LINE, lw=1.6)
    ax.add_patch(chip)
    ax.text(880, 116, "RK3562", ha="center", fontsize=15, color="#E9F2FB",
            fontproperties=fp("Bold"))
    for i in range(4):
        ax.add_patch(Rectangle((796 + i * 30, 136), 24, 18, fc="#1B3050", ec=LINE, lw=1))
    ax.text(860, 166, "4×A53", fontsize=9, color=DIM, ha="center")
    npu = FancyBboxPatch((800, 182), 160, 30, boxstyle="round,pad=0.02,rounding_size=6",
                         fc="#0E3A52", ec=CYAN, lw=2)
    ax.add_patch(npu)
    ax.text(880, 197, "NPU 1 TOPS · Qwen-0.5B", ha="center", fontsize=9.5, color=CYAN)
    ax.plot(972 + t * 44, 150 + wave(18), color=ORANGE, lw=2)
    for i, r in enumerate((16, 10, 5)):
        ax.add_patch(plt.Circle((1052, 150), r, fill=False, ec=ORANGE, lw=1.6, alpha=0.9 - i * 0.25))
    # 云
    ax.add_patch(FancyBboxPatch((838, 258), 118, 34, boxstyle="round,pad=0.02,rounding_size=10",
                                fc="#0D1B2E", ec=LINE, lw=1.4, ls="--"))
    ax.text(897, 275, "dashscope", ha="center", fontsize=11, color=DIM)
    ax.annotate("", xy=(897, 258), xytext=(897, 226),
                arrowprops=dict(arrowstyle="<->", color=DIM, lw=1.2, ls="--"))
    ax.text(930, 242, "一键切换", fontsize=9, color=DIM, fontproperties=fp("Regular"))

    # 人物版封面(make_cover_dma.py 合成)才是正式封面; 深蓝示意图版留作备选
    fig.savefig(os.path.join(OUT, "cover-3562-tech.png"), facecolor=BG)
    plt.close(fig)




# ================================================================ 卡片风框图
def topology_card():
    label = figs.label
    img, dr = figs.base("整条链路：CPU 干杂活，NPU 只住大模型",
                        "RK3562 EVB2 · 4×A53@2GHz · 2GB DDR · NPU 1 TOPS · 断网全离线，通网一键切云端")
    F, S = "#0F172A", "#334155"

    def box(x0, y0, x1, lines, pal, badge_text=None):
        h = figs.card_h(lines)
        figs.grad_card(dr, (x0, y0, x1, y0 + h), *pal, radius=16)
        figs.card_text(dr, (x0, y0, x1, y0 + h), lines)
        if badge_text:
            figs.badge(dr, (x0, y0, x1, y0 + h), badge_text)
        return y0 + h

    A = [("6×PDM 环形麦", 22, F, True, False), ("ch0-5", 17, S, False, False)]
    B = [("RK809 codec", 21, F, True, False), ("喇叭 DAC 播放", 17, S, False, False),
         ("ADC 回采 ch6/7", 17, S, False, False), ("DADC 0xff（0dB）", 17, S, False, False)]
    SPK = [("板载喇叭", 19, F, True, False)]
    C = [("rkaudio_vqe", 21, F, True, False), ("AEC/降噪/AGC/波束", 17, S, False, False),
         ("闭源·波束要授权", 17, S, False, False), ("ref = 回采 ch7", 17, S, False, False)]
    D = [("xiaorui_live(C)", 21, F, True, False), ("KWS zipformer 3.3M 阈值0.15", 17, S, False, False),
         ("能量 VAD 门限 10dB", 17, S, False, False), ("播放/打断/跟随窗", 17, S, False, False)]
    E = [("板内网关 :8001", 21, F, True, False), ("SenseVoice ASR      CPU", 17, S, False, False),
         ("Qwen2.5-0.5B        NPU", 17, "#7C3AED", True, False),
         (" └ RKLLM 服务 :8112", 15, S, False, False),
         ("Matcha TTS 4线程1.1 CPU", 17, S, False, False), ("意图直答：音量/灯控", 17, S, False, False)]
    Fc = [("Home Assistant :8123", 19, F, True, False), ("设备控制面 → 灯", 17, S, False, False)]
    G = [("dashscope", 19, F, True, False), ("qwen3-asr-flash + qwen-flash", 16, S, False, False),
         ("云端形态 · 一键切换/回滚", 16, S, False, False)]

    box(48, 116, 288, A, figs.SKY_INDIGO)                 # 麦
    b_end = box(48, 250, 288, B, figs.SLATE)              # RK809
    box(48, 470, 288, SPK, figs.SLATE)                    # 喇叭
    c_end = box(352, 116, 600, C, figs.BLUE_CYAN, "CPU")  # VQE
    d_end = box(352, 262, 600, D, figs.GREEN, "CPU")      # 前端
    e_end = box(680, 116, 1032, E, figs.PURPLE, "CPU+NPU")# 网关
    f_end = box(680, 330, 1032, Fc, figs.ORANGE_RED)      # HA
    box(352, 452, 600, G, figs.SLATE)             # 云

    figs.arrow(dr, (288, 140), (352, 140))
    label(dr, (320, 158), "8ch PDM", 15, "#64748B", anchor="mm")
    figs.arrow(dr, (288, 285), (352, 210))
    label(dr, (318, 236), "回采混入", 15, "#64748B", anchor="mm")
    figs.arrow(dr, (476, c_end), (476, 262))
    label(dr, (488, (c_end + 262) // 2), "1ch 16k", 15, "#64748B")
    figs.arrow(dr, (600, 300), (680, 300))
    label(dr, (640, 284), "ws 8001", 15, "#64748B", anchor="mm")
    figs.arrow(dr, (720, e_end), (720, 330))
    label(dr, (732, (e_end + 330) // 2), "REST", 15, "#64748B")
    # 网关<->云端: 斜穿中/右列间隙(该区域无卡片), 避开 HA
    figs.arrow(dr, (680, 316), (600, 450), color="#94A3B8")
    figs.arrow(dr, (600, 450), (680, 316), color="#94A3B8")
    label(dr, (640, 383), "切换/回滚", 15, "#94A3B8", anchor="mm")
    figs.arrow(dr, (352, 330), (288, 300), color="#94A3B8")
    label(dr, (320, 404), "hw:1,0 播放", 15, "#94A3B8", anchor="mm")
    figs.arrow(dr, (168, b_end), (168, 470), color="#94A3B8")
    label(dr, (180, (b_end + 482) // 2), "SPK", 15, "#94A3B8")

    label(dr, (48, 560), "CPU 侧：3A / VAD / KWS / ASR / TTS      NPU 侧：只有大模型 Qwen2.5-0.5B", 20, "#0F172A", bold=True)
    label(dr, (48, 596), "四进程 1.7G / 板载 1963M · zram 768M（zstd）· OOM 优先级：网关/RKLLM -500，HA +200", 18, "#64748B")
    label(dr, (48, 626), "开机 50 秒全链就绪 · crond 每 2 分钟看护", 18, "#64748B")
    img.convert("RGB").save(os.path.join(OUT, "fig-3562-topology.png"))


if __name__ == "__main__":
    topology()
    latency()
    mem()
    cover()
    topology_card()
    print("done ->", OUT)
