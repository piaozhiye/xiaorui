import os
#!/usr/bin/env python3
"""Generate reproducible guard phrases with PC Matcha; private output, no board writes."""
import argparse
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
VENV = os.path.expanduser("~/rk/xiaozhi-esp32-server/main/xiaozhi-server/.venv/bin/python")
CASES = [("prefix-thanks", "谢谢小瑞小瑞现在几点了", True),
         ("prefix-wei", "喂，小瑞小瑞，现在几点了", True),
         ("negative-not", "不是小度小度", False),
         ("negative-narrative", "他说小度小度", False)]
SYNTH = r'''
import glob,json,pathlib,sys,wave,hashlib
import numpy as np
from sherpa_onnx import OfflineTts,OfflineTtsConfig,OfflineTtsMatchaModelConfig,OfflineTtsModelConfig
d=pathlib.Path(sys.argv[1]); out=pathlib.Path(sys.argv[2]); cases=json.loads(sys.argv[3])
m=OfflineTtsMatchaModelConfig(acoustic_model=sorted(glob.glob(str(d/'model*.onnx')))[0],
 vocoder=(sorted(glob.glob(str(d/'hifigan*.onnx'))) or sorted(glob.glob(str(d/'vocoder*.onnx'))))[0],
 lexicon=str(d/'lexicon.txt'),tokens=str(d/'tokens.txt'),dict_dir=str(d/'dict'))
tts=OfflineTts(OfflineTtsConfig(model=OfflineTtsModelConfig(matcha=m,num_threads=4)))
items=[]
for name,text,positive in cases:
 a=tts.generate(text,sid=0,speed=1.0); x=np.asarray(a.samples,dtype=np.float64)
 rms=float(np.sqrt(np.mean(x*x))); peak=float(np.max(np.abs(x)))
 assert rms>0 and peak>0
 gain=min(4558/rms,32000/peak); samples=(x*gain).astype('<i2')
 p=out/(name+'.wav')
 with wave.open(str(p),'wb') as w:
  w.setnchannels(1); w.setsampwidth(2); w.setframerate(a.sample_rate); w.writeframes(samples.tobytes())
 items.append(dict(id=name,text=text,positive=positive,prefix=('谢谢' if name=='prefix-thanks' else text[0]) if positive else '',wav=str(p),sample_rate=a.sample_rate,
  accepted_prefixes=['喂','为'] if name=='prefix-wei' else (['谢谢'] if positive else []),
  sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
(out/'manifest.json').write_text(json.dumps(dict(cases=items),ensure_ascii=False,indent=2))
print(out/'manifest.json')
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, default=Path("/tmp/wake-guard-" + str(time.time_ns())))
    args = ap.parse_args()
    args.output.mkdir(mode=0o700)
    subprocess.run([VENV, "-c", SYNTH,
                    str(ROOT / "tts-matcha/matcha-icefall-zh-baker"),
                    str(args.output.resolve()), json.dumps(CASES, ensure_ascii=False)],
                   check=True, timeout=180)


if __name__ == "__main__":
    main()
