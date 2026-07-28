import os
import glob

output = 'combined_log.txt'

def safe_read(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except UnicodeDecodeError:
        with open(filepath, 'r', encoding='cp949') as f:
            return f.read()

log_files = sorted(glob.glob("*.log"))

if not log_files:
    print("현재 폴더에 .log 파일이 없습니다.")
else:
    with open(output, 'w', encoding='utf-8') as out:
        for f in log_files:
            out.write(f"\n{'='*50}\nFILE: {f}\n{'='*50}\n\n")
            out.write(safe_read(f))
            out.write("\n")
    print(f"{len(log_files)}개의 로그 파일이 [{output}]로 합쳐졌습니다.")