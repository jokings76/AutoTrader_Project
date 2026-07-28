import os


def merge_code_files(output_filename="combined_strategy_code.txt"):
    target_extensions = (".py",)
    exclude_dirs = {"__pycache__", ".git", ".vscode", "venv", "env"}

    with open(output_filename, "w", encoding="utf-8") as outfile:
        for root, dirs, files in os.walk("."):
            # 제외할 디렉토리 필터링
            dirs[:] = [d for d in dirs if d not in exclude_dirs]

            for file in files:
                if file.endswith(target_extensions):
                    # 자기 자신(merge_code.py 또는 split_code.py)은 합치기에서 제외
                    if file in ["merge_code.py", "split_code.py"]:
                        continue

                    file_path = os.path.join(root, file)
                    # 상대 경로 계산 (예: utils/logger.py)
                    rel_path = os.path.relpath(file_path, ".")
                    rel_path = rel_path.replace("\\", "/")  # 윈도우 역슬래시 통일

                    outfile.write(f"\n\n# === file: {rel_path} ===\n")
                    try:
                        with open(file_path, "r", encoding="utf-8") as infile:
                            outfile.write(infile.read())
                        print(f"➕ 병합 중: {rel_path}")
                    except Exception as e:
                        print(f"⚠️ 읽기 실패 ({rel_path}): {e}")

    print(f"\n🎉 모든 코드가 '{output_filename}' 파일로 성공적으로 병합되었습니다!")


if __name__ == "__main__":
    merge_code_files()
