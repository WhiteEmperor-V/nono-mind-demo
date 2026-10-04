#!/usr/bin/env python3
"""分段转写44分钟长音频: 切8段, 每段独立transcribe(内存峰值=单段), 顺序拼接"""
import subprocess, os

WAV = "/tmp/v.wav"
DUR = 2680  # 秒
SEG = 340   # 每段340秒, 8段
model = None

def get_model():
    global model
    if model is None:
        from faster_whisper import WhisperModel
        model = WhisperModel('base', device='cpu', compute_type='int8', cpu_threads=2)
    return model

out = open('/tmp/v_text.txt', 'w')
for i in range(8):
    start = i * SEG
    seg_wav = f"/tmp/v_seg{i}.wav"
    if not os.path.exists(seg_wav):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(SEG),
                        "-i", WAV, "-vn", "-ar", "16000", "-ac", "1", seg_wav], check=True)
    m = get_model()
    segs = m.transcribe(seg_wav, language='zh', vad_filter=False)[0]
    for s in segs:
        out.write(f"[{int(start + s.start)}s] {s.text}\n")
    out.flush()
    os.remove(seg_wav)  # 立刻释放磁盘
    print(f"段{i+1}/8 完成", flush=True)
out.close()
print("ALL DONE")
