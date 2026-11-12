#!/usr/bin/env python3
"""把 HuggingFace 模型转成 RK3562 专用 .rkllm (RKLLM-Toolkit 1.3.1)。

RK3562 的 NPU 只有 1 核 / 2GB 内存，所以用 Qwen2.5-0.5B-Instruct +
w8a8 量化。转换在 PC 上跑(CPU), 产物推到板子由 RKLLM runtime 在 NPU 上推理。

用法: convert_rk3562.py <hf_model_dir> <out_rkllm>
"""
import os
import sys
import time

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from rkllm.api import RKLLM  # noqa: E402

# RK3562 的 rknpu(0.9.8, 单核) 不支持 w4a16 —— toolkit 明确报
# "target_platform: rk3562 not support quantized_dtype: w4a16"。
# 逐个试出来的可用档是 w8a8(0.5B 模型体积 ~500MB, 板子 2GB 内存放得下)。
QUANT_DTYPE = os.environ.get("XR_RKLLM_QUANT", "w8a8")


def main():
    model = sys.argv[1]
    out = sys.argv[2]
    t0 = time.time()
    llm = RKLLM()
    print(f"[conv] load_huggingface {model} (cpu/fp32)...", flush=True)
    llm.load_huggingface(model=model, model_lora=None, device="cpu",
                         dtype="float32", custom_config=None, load_weight=True)
    print(f"[conv] load done {time.time()-t0:.0f}s; build({QUANT_DTYPE}, rk3562)...", flush=True)
    llm.build(do_quantization=True,
              optimization_level=1,
              quantized_dtype=QUANT_DTYPE,
              quantized_algorithm="normal",
              target_platform="rk3562",
              num_npu_core=1)
    print(f"[conv] build done {time.time()-t0:.0f}s; export {out} ...", flush=True)
    llm.export_rkllm(out)
    print(f"[conv] exported {out} in {time.time()-t0:.0f}s", flush=True)

if __name__ == "__main__":
    main()
