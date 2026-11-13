# LVGL 8.4 构建依赖：来源与限制

`ui/build.sh` 默认使用本目录的配套头文件与预编译库，`XR_LVGL_ROOT` 可指定其他 SDK。
本目录保留已发布的两份 `.so`，没有用新构建产物替换它们。

## 文件来源

| 路径 | 实际内容 |
| --- | --- |
| `include/lvgl/` | 自报版本为 8.4.0 的厂商 LVGL 分支头文件，来源可追溯到 Rockchip SDK 的 `rkaudio_vqe/examples/vqe_display/include/lvgl`；不是纯净上游头文件 |
| `include/lv_conf.h` | 基于 LVGL 配置模板的项目配置，32-bit 色深、FreeType、单调时钟 tick；这是唯一配置入口 |
| `include/lvgl/src/misc/lv_systick.h` | 本项目修正的 MIT tick adapter，提供 `static inline` 实现；不是上游原文件 |
| `lib/aarch64/liblvgl.so` | 从 `rk-eqdrc-tuner` 取得的 AArch64 预编译库 |
| `lib/x86/liblvgl.so` | 从同一来源取得的 x86-64 预编译库；`x86` 是历史目录名，不表示 32-bit |
| `LICENCE.txt` | 上游 LVGL 的 MIT 许可，Copyright (c) 2021 LVGL Kft |

未使用的 `lv_drivers/` 头文件和内层重复 `lv_conf.h` 已删除。本 UI 使用自己的
Wayland 端口，不使用这些驱动。

库的可核对来源为 [rk-eqdrc-tuner 的固定提交](https://github.com/piaozhiye/rk-eqdrc-tuner/tree/0281a1b976de100e0aa11f0b84e04779c13b66b4)。
该仓 NOTICE **自述**为“upstream LVGL 8.4.1 unmodified rebuild”，但该说法与本包
头文件的 8.4.0 版本号、厂商差异不一致；没有据此把二进制标成上游 8.4.1。
原始二进制对应的精确源码 commit、全部补丁和构建命令未记录。

| 库 | SHA-256 |
| --- | --- |
| AArch64 | `8354745c500487e5151c2a674fc0dc95219d7921502e8e7672f2c96312cd8164` |
| x86-64 | `950be0dd7a1dc2b9b566462cd1bc48f338e11c16296929c8bad3b74252848070` |

ELF 中可见 FreeType/libc 依赖，AArch64 另依赖 libm。符号检查没有发现 Rockchip
端口符号；这不能替代原始源码审计，也不能证明库中所有代码均未修改。

## 与上游的差异及 ABI

比较基准是上游 `v8.4.0`，commit
`4495f428630cc1741bd8bfd977f080e8460e8e8d`，仅作为对照及候选重建输入，
**不是对现有二进制源码版本的认定**。

厂商头文件包含上游没有的 `inputbox`、`stiker_matrix`，以及 `lv_deinit`、
`lv_indev_drv_unregister`、`lv_fs_drv_unregister` 等声明差异。
`lv_img_header_t` 的宽高位域从上游 11 位改为 16 位，涉及结构布局；
`lv_table_cell_t` 的尾部数组也有差异。完整差异文件及散列见
[`upstream-differences.json`](upstream-differences.json)。

因此，上游重编库即使符号表相近，也不能证明与本包 ABI 或行为等价。
不要把上游库与本包厂商头文件混用。

现有 `.so` 的 `lv_tick_get()` 反汇编直接使用 `clock_gettime(CLOCK_MONOTONIC)`，
未导入 `lv_systick`。构建时采用了可内联 tick adapter 是一种推断，原始输入无法确认。
本次将只声明的 `lv_systick.h` 补成单调时钟 inline 实现，使新构建不再遗留
`lv_systick` 未定义符号；它是当前明确记录的实现，不冒充原始构建输入。

## 许可范围

LVGL 上游部分适用 [`LICENCE.txt`](LICENCE.txt)，项目配置与 tick adapter 的本次修改
适用仓库根目录 MIT 许可。厂商分支新增/修改部分的归属仍属于原权利人；
不能仅凭上游 MIT 文件替未明确许可的厂商新增代码作授权保证。

内嵌组件需分别保留许可，不因它们被编入 `.so` 而免除相应条件：

| 组件 | 原许可及位置 |
| --- | --- |
| TLSF / Matthew Conte | BSD-3-Clause，全文位于 `include/lvgl/src/misc/lv_tlsf.h` |
| LodePNG / Lode Vandevenne | zlib，全文位于 `include/lvgl/src/extra/libs/png/lodepng.h` |
| QR Code / Project Nayuki | MIT，全文位于 `include/lvgl/src/extra/libs/qrcode/qrcodegen.h` |
| stb / Sean Barrett | 头文件提供 MIT 或 public-domain 双选项，全文位于 `include/lvgl/src/extra/libs/tiny_ttf/stb_*.h`；两种文本均保留 |
| GIF decoder / lecram | 上游项目自述 public domain，[来源说明](https://github.com/lecram/gifdec)；LVGL 集成见上述固定上游 commit 的 `docs/libs/gif.md` |
| TJpgDec / ChaN | 宽松自定义许可，头文件保留 `(C)ChaN, 2021`；[原条款](https://github.com/lvgl/lvgl/blob/4495f428630cc1741bd8bfd977f080e8460e8e8d/src/extra/libs/sjpg/tjpgd.c) |

当前库导出 GIF/TJpgDec 解码符号，并非只分发组件头文件。FreeType 为系统提供的
动态依赖，不打包进本目录。整个项目的 MIT 文件不覆盖这些组件各自的条件。

## 隔离重建候选 SDK

现有二进制的原始输入缺失，不能提供逐字节复现命令。
`rebuild-candidate.sh` 使用固定上游 commit、当前配置和 tick adapter，输出独立 SDK，
用于后续迁移验证；不会替换本目录的库，也不宣称 ABI/行为等价。
源代码通过 `git archive` 从固定 commit 提取，本地工作区改动不会混入。

```bash
git clone https://github.com/lvgl/lvgl.git /tmp/lvgl-source
bash ui/third_party/lvgl/rebuild-candidate.sh native /tmp/lvgl-source
bash ui/third_party/lvgl/rebuild-candidate.sh aarch64 /tmp/lvgl-source
```

主机需要 C 编译器、Git、Python 3、ripgrep、pkg-config 与 FreeType 开发文件。
AArch64 使用 `XR_TOOLCHAIN_ROOT` 指定的 Buildroot 工具链，默认路径与 UI 构建脚本相同。
候选 SDK 写入 `ui/build/lvgl-candidate/{native,aarch64}`，包含自己的上游头文件、
库和 `BUILD.txt`（源码 commit、编译器、配置/tick 散列及命令记录）。

只在后续独立迁移中配套使用候选 SDK，例如：

```bash
XR_LVGL_ROOT="$PWD/ui/build/lvgl-candidate/native" bash ui/test.sh
```

主机回归通过也不代表板上部署通过。若以后替换 AArch64 库，必须使用其配套头文件
重编 UI，并重新验证字体、触摸、动画和显示；本次不进行该替换。
