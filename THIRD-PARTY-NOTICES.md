# 第三方组件与许可证声明（THIRD-PARTY NOTICES）

XiaoRui 项目自研代码按根目录 [LICENSE](LICENSE)（MIT）授权。
以下第三方组件保留其原始许可证与本声明；本文件不含任何针对第三方的
版权转让——各组件版权归其原作者所有。

## 直接派生/捆绑的组件

> xiaozhi-esp32-server 上游 LICENSE 版权行（2026-10-19 核对）：`Copyright (c) 2025 xinnan-tech`。

| 组件 | 仓库位置 | 许可证 | 来源 | 义务 |
| --- | --- | --- | --- | --- |
| board-server 服务端 | `board-server/` | **MIT** | 派生自 [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)（xinnan-tech，MIT） | 保留其 MIT 许可证文本与版权声明（见下） |
| pymicro-vad | 交叉编译 wheel（`tools/ha-crossbuild/`，装于板上 HA） | **Apache-2.0** | [rhasspy/pymicro-vad](https://github.com/rhasspy/pymicro-vad) | 随分发附 Apache-2.0 文本（sdist 内 LICENSE 原文），保留版权与 NOTICE |
| pyspeex-noise | 交叉编译 wheel（同上） | **MIT** | rhasspy/pyspeex-noise（Michael Hansen，2024） | 保留 MIT 文本与版权声明 |

## 板上运行时依赖（不随本仓库分发，随整机/镜像分发时适用）

| 组件 | 许可证 | 说明 |
| --- | --- | --- |
| Home Assistant Core | Apache-2.0 | 作为独立服务运行（REST 通信，无代码链接）；随镜像分发时须附 Apache-2.0 文本、NOTICE 并声明修改 |
| sherpa-onnx | Apache-2.0 | 板载 ASR/VAD/KWS 推理 |
| libopus (Xiph) | Xiph.Org BSD 变体 | 静态链入前端 |
| Linux 内核 / Buildroot / busybox | GPL-2.0（及各组件自有许可） | 分发相关源码或二进制时按具体组件许可履行义务，不限于整机镜像；源码提供方式须符合适用条款 |
| RKLLM / RKNN 运行时 | 以所用版本附带许可证为准 | 仓库级 LICENSE 不自动代表所有预编译运行库的授权范围；再分发前单独核对 |
| VQE 算法库 / ALSA 插件 / wopt 权重 | 以对应 SDK / 发布包条款为准 | **不随本仓分发**（向瑞芯微申请 .so）；若随整机再分发，须按对应条款单独核对，不适用本项目 MIT 授权 |

## xiaozhi-esp32-server 的 MIT 许可证文本（保留义务）

```
MIT License

Copyright (c) 2025 xinnan-tech

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 许可证兼容性说明

本项目自研代码采用 MIT；第三方代码、运行库与模型保留各自许可，本仓随源码
分发的第三方组件均为 MIT / Apache-2.0 等宽松许可。分发整机镜像或再分发
板端运行库前，应分别核对所用版本的许可文本与义务。此清单不构成商用授权保证。
