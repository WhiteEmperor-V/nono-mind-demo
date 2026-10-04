import subprocess, os
DUR = 356
SEG = 180  # 2段
subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", "/tmp/v.mp4", "-vn", "-ar", "16000", "-ac", "1", "/tmp/v_full.wav"], check=True)
from faster_whisper import WhisperModel
m = WhisperModel('base', device='cpu', compute_type='int8', cpu_threads=2)
with open('/tmp/v_text.txt', 'w') as f:
    done = 0
    while done < DUR:
        seg = f"/tmp/v_seg.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(done), "-t", str(SEG),
                        "-i", "/tmp/v_full.wav", seg], check=True)
        segs = m.transcribe(seg, language='zh', vad_filter=False)[0]
        for s in segs:
            f.write(f"[{int(done + s.start)}s] {s.text}\n")
        f.flush()
        os.remove(seg)
        done += SEG
        print(f"{done}/{DUR}", flush=True)
print("DONE")
