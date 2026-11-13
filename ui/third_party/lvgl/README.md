# LVGL 8.4（vendored 构建依赖）

`ui/build.sh` 默认用这里的头文件和预编译库，不需要额外准备 SDK；设置
`XR_LVGL_ROOT` 可以覆盖为别的同版本 SDK。

| 路径 | 内容 |
| --- | --- |
| `include/lvgl/` | LVGL 8.4.0 与 lv_drivers 的公开头文件（未改动） |
| `include/lv_conf.h` | 本项目的 LVGL 配置：32-bit 色深、FreeType 字体、`LV_TICK_CUSTOM` |
| `lib/aarch64/liblvgl.so` | 板端（aarch64，`-march=armv8-a`，无 LSE 原子指令） |
| `lib/x86/liblvgl.so` | 主机模拟构建，供 `ui/build.sh native` 的截图测试 |
| `LICENCE.txt` | 上游 LVGL 8.4.0 的 MIT 许可证原文 |

两个 `.so` 是上游 LVGL 8.4 源码配上面这份 `lv_conf.h` 重编的，动态依赖只有
`libfreetype` / `libm` / `libc`，不含 Rockchip SDK 的端口代码；Rockchip demo 里的
`lv_port_*`、`evdev`、`key` 等文件不在本目录，也不在本仓。

许可证：LVGL 与 lv_drivers 均为 MIT（见 `LICENCE.txt`）；`lv_conf.h` 以上游 MIT
模板为基础修改。LVGL 内嵌的第三方组件（qrcodegen、lodepng、stb、lv_tlsf）各保留
其原始许可——本目录只含头文件，不含这些组件的独立分发物。
