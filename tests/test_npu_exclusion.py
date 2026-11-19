"""RKLLM 与 Rock helper 的跨进程锁必须真正共享，不能只锁 Python 线程。"""
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]


def test_llm_and_rock_helpers_share_cross_process_lock(tmp_path):
    holder = '''import importlib.util,sys,time
s=importlib.util.spec_from_file_location('engine',sys.argv[1])
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
with m.npu_exclusive():
 print('locked',flush=True)
 time.sleep(.4)
'''
    contender = '''import importlib.util,sys
s=importlib.util.spec_from_file_location('engine',sys.argv[1])
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
with m.npu_exclusive():print('acquired',flush=True)
'''
    env=dict(os.environ,XR_NPU_LOCK_FILE=str(tmp_path/'npu.lock'))
    llm=ROOT/'board-server/llm_rkllm.py'
    for module in ('tts_helper.py','asr_helper.py'):
        a=subprocess.Popen([sys.executable,'-c',holder,str(llm)],env=env,
                           stdout=subprocess.PIPE,text=True)
        try:
            assert a.stdout.readline().strip()=='locked'
            start=time.monotonic()
            result=subprocess.run([sys.executable,'-c',contender,
                str(ROOT/'board-server/pkg/core/providers/rock'/module)],
                env=env,capture_output=True,text=True,timeout=5)
            assert result.returncode==0,result.stderr
            assert result.stdout.strip()=='acquired'
            assert time.monotonic()-start>=.3
        finally:
            a.wait(timeout=5)
            a.stdout.close()
