# 08. Windows 설치와 함정

Windows에서도 모든 처리는 현재 PC의 CPU에서 실행됩니다. GPU는 필요하지 않고 사진은 외부로 전송되지 않습니다.

## 권장 시작

1. [Python 다운로드 페이지](https://www.python.org/downloads/)에서 Python 3.9 이상을 설치합니다.
2. 설치 첫 화면의 `Add python.exe to PATH`를 체크합니다.
3. 받은 공개배포폴더를 원하는 위치에 복사합니다. 한글과 공백이 있는 경로도 사용할 수 있습니다.
4. `00_시작\시작하기.bat`을 더블클릭합니다.
5. 환경 점검·필요 패키지 설치 뒤 브라우저가 자동으로 열립니다. 메뉴 선택은 필요 없습니다.
6. 브라우저에서 `① 사진 넣기 → ② 사진 준비 → ③ 보정 → ④ PDF` 순서로 진행합니다.

## 수동 설치

명령 프롬프트나 Windows Terminal에서 공개배포폴더로 이동한 뒤 실행합니다.

```cmd
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m pip install pillow-heif
```

HEIC·HEIF 사진이 없으면 마지막 줄은 생략할 수 있습니다.

## 명령으로 전체 흐름 실행

```cmd
python 05_스크립트\init_worktree.py --src 01_원본사진 --out 02_작업장 --by-gap 20
python 05_스크립트\prepare_photos.py --plan 02_작업장\worktree.json
python 02_작업장\slide_tool\gen_manifest.py
python 00_시작\serve_tool.py --root 02_작업장
```

도구 작업 뒤 PDF를 만듭니다.

```cmd
python 05_스크립트\export_pdf.py ^
  --backup C:\Users\나\Downloads\slide_tool_backup_날짜시각.json ^
  --root 02_작업장 --src-dir 01_원본사진 --out 03_결과물
```

`^`는 명령 프롬프트의 줄바꿈 기호입니다. PowerShell에서는 한 줄로 실행하거나 PowerShell 줄바꿈 문자를 사용합니다.

## 자주 막히는 것

### `python`을 찾을 수 없음

Python을 다시 설치하며 `Add python.exe to PATH`를 체크하거나 `py` 명령을 사용합니다.

### 시작 창이 바로 닫힘

명령 프롬프트에서 다음을 실행해 오류를 확인합니다.

```cmd
00_시작\시작하기.bat
```

자동 흐름 대신 기존 CLI 메뉴로 문제를 진단하려면 다음을 실행합니다.

```cmd
python 00_시작\launch.py
```

### 한글이 깨짐

Windows Terminal 또는 PowerShell을 권장합니다. 기존 명령 프롬프트에서는 먼저 `chcp 65001`을 실행합니다.

### HEIC 미지원

`.venv\Scripts\activate` 뒤 `python -m pip install pillow-heif`를 실행합니다.

### 방화벽 경고

서버는 `127.0.0.1`의 로컬 브라우저만 사용합니다. 공용 네트워크 허용은 필요하지 않습니다.

### 통합 PDF 실패

Windows에는 `pdfunite`가 기본 설치되지 않습니다. 그룹별 PDF는 정상 결과이며, 통합본이 필요하면 poppler를 별도로 설치하거나 PDF 프로그램에서 합칩니다.

전체 사용 순서는 [README](../README.md), 일반 오류는 [06_문제해결](06_문제해결.md)을 보세요.
