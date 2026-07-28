import os


def split_combined_code(source_file):
    if not os.path.exists(source_file):
        print(f"⚠️ {source_file} 파일을 찾을 수 없습니다.")
        return

    with open(source_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    print(f"📄 파일 읽기 성공 (총 {len(lines)} 줄)")

    current_filename = None
    current_content = []
    file_count = 0

    for line in lines:
        stripped = line.strip()
        # 주석 기호로 시작하면서 .py 가 포함된 줄을 파일명 매커로 인식
        if (
            stripped.startswith("#")
            or stripped.startswith("//")
            or stripped.startswith("###")
        ) and ".py" in stripped:
            # 이전 파일 저장
            if current_filename and current_content:
                save_file(current_filename, current_content)
                file_count += 1

            # 앞쪽 주석 기호와 등호만 정리하고, 경로 분리 기호(/, \)는 그대로 유지
            clean_line = stripped.lstrip("#").lstrip("/").replace("=", "").strip()
            parts = clean_line.split()

            for part in parts:
                if part.endswith(".py"):
                    current_filename = part
                    break
            else:
                current_filename = parts[-1] if parts else None

            current_content = []
        else:
            if current_filename:
                current_content.append(line)

    # 마지막 파일 저장
    if current_filename and current_content:
        save_file(current_filename, current_content)
        file_count += 1

    print(f"🎉 총 {file_count}개의 파일이 성공적으로 분할 생성되었습니다!")


def save_file(filename, content_lines):
    filename = filename.strip()
    # 경로가 포함되어 있다면 해당 폴더를 자동으로 먼저 생성
    if "/" in filename or "\\" in filename:
        os.makedirs(os.path.dirname(filename), exist_ok=True)

    with open(filename, "w", encoding="utf-8") as out_f:
        out_f.writelines(content_lines)
    print(f"✅ 생성 완료: {filename}")


if __name__ == "__main__":
    target_source = "combined_strategy_code.txt"
    split_combined_code(target_source)
