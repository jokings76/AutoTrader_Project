import os
import traceback

# 현재 파일 위치 무조건 잡기
try:
    script_dir = os.path.dirname(os.path.abspath(__file__))
except:
    script_dir = os.getcwd()

output_file = os.path.join(script_dir, 'combined_code.txt')

ignore_dirs = {'.git', '__pycache__', '.venv', 'venv', 'env', '.vscode'}
ignore_files = {'config.ini', '.env'}

def safe_read(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except UnicodeDecodeError:
        try:
            with open(filepath, 'r', encoding='cp949') as f:
                return f.read()
        except:
            return "[한글 인코딩 에러]"
    except Exception as e:
        return f"[파일 읽기 불가: {e}]"

print(f"시작 위치: {script_dir}")
print("파일을 합치는 중...")

with open(output_file, 'w', encoding='utf-8') as out:
    try:
        for root, dirs, files in os.walk(script_dir):
            # 무시할 폴더 제거
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            
            for f in files:
                if f.endswith('.py') and f not in ignore_files:
                    try:
                        path = os.path.join(root, f)
                        rel_path = os.path.relpath(path, script_dir)
                        
                        out.write(f'\n{"="*60}\n')
                        out.write(f'FILE: {rel_path}\n')
                        out.write(f'{"="*60}\n\n')
                        out.write(safe_read(path))
                        out.write('\n')
                    except Exception as e:
                        # 에러가 나도 멈추지 않고 다음 파일로 넘어감
                        print(f"건너뜀: {f} (이유: {e})")
    except Exception as e:
        print(f"탐색 에러 발생: {e}")
        traceback.print_exc()

print(f"\n완료되었습니다! 파일 위치: {output_file}")