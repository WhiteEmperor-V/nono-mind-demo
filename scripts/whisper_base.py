from faster_whisper import WhisperModel
import sys
m = WhisperModel('base', device='cpu', compute_type='int8', cpu_threads=2)
segs = m.transcribe('/tmp/v.wav', language='zh', vad_filter=False)[0]
with open('/tmp/v_text.txt', 'w') as f:
    for s in segs:
        f.write(f'[{int(s.start)}s] {s.text}\n')
print('DONE')
