# 슬라이드 사진 보정·PDF 제작 도구

`01_원본사진`에 발표 화면 사진을 넣으면, 브라우저에서 원근·색을 보정한 고해상도 PDF가 `03_결과물`에 만들어집니다.

## 📥 내려받기

### 방법 1 — ZIP (git 없어도 됨, 가장 쉬움)

[**여기를 눌러 ZIP 내려받기**](https://github.com/CNI-KaeSoon/presentation-photo/archive/refs/heads/main.zip)
→ 압축을 풀고, 나온 폴더(`presentation-photo-main`)를 원하는 위치로 옮깁니다.

명령으로 받으려면 (Windows 10 이상은 `curl`·`tar`가 기본 내장):

```cmd
curl -L -o slidetool.zip https://github.com/CNI-KaeSoon/presentation-photo/archive/refs/heads/main.zip
tar -xf slidetool.zip
cd presentation-photo-main
```

### 방법 2 — git clone

```bash
git clone https://github.com/CNI-KaeSoon/presentation-photo.git
cd presentation-photo
```

> 폴더는 **어디에 두어도 됩니다.** 경로에 한글·공백이 있어도 동작합니다.
> 나중에 폴더를 옮겨도 그대로 동작합니다.

---

## 🚀 시작하기 — 사람용

- Windows: 패키지 루트의 `시작하기.bat`을 더블클릭합니다.
- macOS: 패키지 루트의 `시작하기.command`를 더블클릭합니다.

기존 `00_시작` 폴더 안의 같은 이름 파일도 계속 사용할 수 있습니다.

첫 실행에는 필요한 환경을 자동으로 점검·설치한 뒤 브라우저가 열립니다. 그 뒤에는 화면의 `📋 작업 순서`만 따라가면 됩니다.

1. **사진 넣기** — 사진을 브라우저 화면에 끌어놓거나 `사진 폴더 열기`로 원본 폴더에 넣습니다.
2. **사진 준비** — 촬영 간격으로 그룹을 나누고 작업용 축소본과 목록을 만듭니다.
3. **모서리·색 보정** — 왼쪽 목록에서 사진을 고르고 네 모서리와 색을 맞춥니다.
4. **PDF 만들기** — `PDF 만들기(고해상도)`를 눌러 `03_결과물`에 저장합니다.

새 작업 순서 패널의 고해상도 PDF는 로컬 Python으로 만들기 때문에 인터넷이 필요 없습니다. 기존 툴바의 `PDF 내보내기`를 사용할 때만 jsPDF를 `cdn.jsdelivr.net`에서 받습니다. 파일은 SRI 해시로 무결성을 검증하며 사진은 외부로 전송하지 않습니다. 기존 `백업 내보내기` 다운로드도 오프라인 백업 수단으로 그대로 사용할 수 있습니다.

### 번호 매긴 폴더 지도

```text
공개배포폴더/
├── 시작하기.bat   Windows 시작 파일
├── 시작하기.command macOS 시작 파일
├── 00_시작/       시작 파일과 환경 점검
├── 01_원본사진/   ← 여기에 사진을 넣습니다. 원본은 수정하지 않습니다.
├── 02_작업장/     작업용 축소본·그룹·브라우저 도구
├── 03_결과물/     ← 완성된 PDF가 여기에 생깁니다.
├── 04_설명서/     단계별 설명과 문제 해결
└── 05_스크립트/   변환·내보내기·검증 프로그램
```

폴더 전체를 원하는 위치에 복사해 사용해도 됩니다. 경로 깊이, 공백, 한글은 상관없습니다.

## 자주 막히는 것

- **Windows에서 시작 창이 바로 닫힘** — Python 설치 때 `Add python.exe to PATH`를 체크했는지 확인합니다.
- **HEIC 사진을 못 읽음** — CLI 메뉴의 환경 점검을 다시 실행해 `pillow-heif`를 설치합니다.
- **업로드가 거부됨** — 파일명의 `#`, `?`, `%`, 경로 문자와 Windows 예약 이름을 제거합니다.
- **“보안 확인 실패”가 보임** — 주소가 `http://localhost:8770/slide_tool/`인지 확인하고 시작 파일로 다시 엽니다.
- **사진이 목록에 없음** — 브라우저의 `사진 준비`를 다시 실행해 목록을 갱신합니다.
- **그룹 이름을 바꾸고 싶음** — 왼쪽 그룹 헤더를 우클릭해 `이름 변경`을 사용합니다. 탐색기에서 폴더나 파일 이름을 직접 바꾸면 저장 키가 어긋납니다.
- **시작 화면이나 API를 우회해 복구해야 함** — 패키지 루트에서 `python3 00_시작/launch.py`를 실행하면 기존 CLI 메뉴가 열립니다.

자세한 진단은 [문제해결](04_설명서/06_문제해결.md)을 보세요.

## 문서 목록

| 문서 | 내용 |
|---|---|
| [01_설치](04_설명서/01_설치.md) | 복사 후 첫 실행, 요구 환경 |
| [02_워크트리_만들기](04_설명서/02_워크트리_만들기.md) | 사진을 세션·발표별 그룹으로 나누기 |
| [03_사진_넣기](04_설명서/03_사진_넣기.md) | 원본을 보존하며 작업용 축소본 만들기 |
| [04_툴_사용법](04_설명서/04_툴_사용법.md) | 원근·색 보정, 저장과 백업 |
| [05_PDF_내보내기](04_설명서/05_PDF_내보내기.md) | 원본 기반 고해상도 PDF 생성 |
| [06_문제해결](04_설명서/06_문제해결.md) | 흔한 오류와 복구 순서 |
| [07_개발자_노트](04_설명서/07_개발자_노트.md) | 코드 계약과 검증 원칙 |
| [08_Windows_설치](04_설명서/08_Windows_설치.md) | Windows 설치와 함정 |

---

## 🤖 AI 에이전트(코덱스·클로드)용 안내

### 에이전트용 설치 절차 (비대화형)

`시작하기.bat` / `시작하기.command`는 `launch.py --auto`로 사람용 브라우저 흐름을 연다.
인자 없는 `launch.py`의 기존 CLI 메뉴도 유지되지만 대화형이므로, 에이전트는 아래 스크립트를 직접 호출하라. 전부 비대화형이고 종료코드로 성공/실패를 판정할 수 있다.

**Windows (cmd) 기준. macOS·Linux 는 `python` → `python3`, `\` → `/`, `.venv\Scripts\` → `.venv/bin/`.**

```cmd
REM 1) 내려받기 — git 이 없으면 curl+tar (Windows 10 이상 내장)
curl -L -o slidetool.zip https://github.com/CNI-KaeSoon/presentation-photo/archive/refs/heads/main.zip
tar -xf slidetool.zip
cd presentation-photo-main

REM 2) 파이썬 확인 — 없으면 https://www.python.org/downloads/ (Add python.exe to PATH 체크)
python --version

REM 3) 환경 자동 셋업 (venv 생성 + 패키지 설치, 질문 없음)
python 00_시작\envcheck.py --yes --pkg-root .

REM 4) 셋업 검증 — ok:true 와 workers 수가 나오면 성공
python 00_시작\envcheck.py --json --pkg-root .
```

3)의 종료코드가 `0` 이면 성공이다. 이후 무거운 작업은 **venv 파이썬**으로 호출한다:
`.venv\Scripts\python.exe` (Windows) / `.venv/bin/python3` (그 외).

```cmd
REM 5) 사진을 01_원본사진\ 에 넣은 뒤 — 그룹 나누기 → 축소본 → 목록
.venv\Scripts\python.exe 05_스크립트\init_worktree.py --src 01_원본사진 --out 02_작업장 --by-gap 20
.venv\Scripts\python.exe 05_스크립트\prepare_photos.py --plan 02_작업장\worktree.json --workers 0
python 02_작업장\slide_tool\gen_manifest.py

REM 6) 브라우저 도구 서버 (127.0.0.1 전용, 창 닫으면 자동 종료)
python 00_시작\serve_tool.py
```

`serve_tool.py` 는 브라우저를 자동으로 연다. 열지 않으려면 `--no-open`,
포트를 바꾸려면 `--port N`, 자동 종료를 끄려면 `--no-watchdog`.
사람이 한 번에 시작할 때는 `python 00_시작\launch.py --auto`를 사용할 수 있다.

서버에는 `/api/token`으로 시작하는 브라우저 워크플로 API가 있으며 업로드·그룹 이름 변경·준비·PDF 생성 같은 변경 요청에는 기동별 토큰이 필요하다. 에이전트 자동화는 브라우저 토큰을 흉내 내지 말고 위의 CLI 스크립트를 직접 사용한다.

사람이 브라우저에서 보정하고 `백업 내보내기`를 누른 뒤:

```cmd
REM 7) 백업 JSON → 03_결과물 에 고해상도 PDF
.venv\Scripts\python.exe 05_스크립트\export_pdf.py ^
    --backup "%USERPROFILE%\Downloads\slide_tool_backup_....json" ^
    --root 02_작업장 --src-dir 01_원본사진 --out 03_결과물 --workers 0
```

#### 설치 성공 판정

| 확인 | 명령 | 성공 기준 |
|---|---|---|
| 환경 | `python 00_시작\envcheck.py --json --pkg-root .` | 종료코드 0, JSON 에 `"ok":true` |
| 경로 독립성 | `python 05_스크립트\check_portable.py` | `PORTABLE: PASS` |
| 보안 회귀 | `.venv\Scripts\python.exe 05_스크립트\test_security.py` | `SECURITY: ALL PASS` |
| 서버 수명 | `python 05_스크립트\test_serve_tool.py` | `SERVE: ALL PASS` |
| 워크플로 API | `.venv\Scripts\python.exe 05_스크립트\test_workflow_api.py` | `WORKFLOW: ALL PASS` |
| HTML 무결성 | `python 05_스크립트\check_index_integrity.py` | `INDEX: PASS` |
| 색연산 일치 | `.venv\Scripts\python.exe 05_스크립트\test_color_parity.py` | `RESULT: ALL PASS` |

#### 에이전트가 흔히 막히는 지점

- **`index.html` 을 직접 열면 안 된다.** `file://` 에서는 캔버스가 tainted 되어 보정이
  `SecurityError` 로 막힌다. 반드시 `serve_tool.py` 로 띄워라.
- **갓 내려받은 상태에는 `data.js` 가 없다**(생성물이라 저장소에 없음). 도구는 빈 화면으로 뜬다.
  사진을 넣고 5)까지 실행해야 슬라이드가 보인다.
- `envcheck.py`·`launch.py`·`serve_tool.py` 는 **표준 라이브러리 전용**이라 venv 없이도 돈다.
  반대로 `05_스크립트/` 의 이미지 처리 스크립트는 **반드시 venv 파이썬**으로 불러야 한다.
- HEIC(아이폰) 사진을 쓰는데 `pillow-heif` 가 없으면 명시적으로 오류를 낸다.
  `--yes` 셋업이 자동 설치하지만, 실패하면 `pip install pillow-heif` 를 따로 실행하라.
- Windows 콘솔에서 한글이 깨지면 `chcp 65001` 후 재실행.
- **샌드박스가 포트를 막을 수 있다.** 에이전트(특히 Codex CLI)가 기본 샌드박스로 돌면
  `serve_tool.py` 가 `PermissionError: [Errno 1] Operation not permitted` 로 죽는다.
  소켓 바인딩이 차단된 것이지 코드 문제가 아니다. 셋 중 하나로 해결한다:
  ① 에이전트를 네트워크 허용 모드로 실행 ② 서버 실행만 사람이 직접 ③ `--port` 를 바꿔 재시도.
  설치·사진준비·PDF 생성은 소켓이 필요 없어 샌드박스에서도 정상 동작한다.
- **에이전트가 대신 할 수 없는 단계가 있다.** 네 모서리를 끌어 맞추고 색을 보는 것은
  사람이 브라우저에서 해야 한다. 에이전트의 역할은 "설치 → 사진 준비 → 서버 기동" 까지와,
  사람이 `백업 내보내기` 한 뒤의 "PDF 생성" 이다.

### 불변 계약

- `02_작업장/slide_tool/index.html`의 `COLOR_CORE`와 `05_스크립트/slide_color.py`는 허용 오차 안에서 픽셀 단위로 일치해야 합니다.
- `--workers 1` 직렬 경로는 기준 구현입니다. 병렬화 수정 뒤에도 결과 바이트·순서가 같아야 합니다.
- localStorage 이미지 키 `../<그룹>/img/<파일>` 형식은 바꾸지 않습니다.
- `01_원본사진`의 파일은 읽기만 하며 변형·이동·삭제하지 않습니다.
- 패키지 내부 경로는 `Path(__file__)` 또는 전달된 계획 파일 위치를 기준으로 계산합니다. 현재 작업 디렉터리나 개발자 절대경로에 의존하지 않습니다.

### 게이트 명령

패키지 루트에서 실행합니다.

```bash
python3 05_스크립트/test_color_parity.py
python3 05_스크립트/test_parallel_parity.py
python3 05_스크립트/test_security.py
python3 05_스크립트/test_serve_tool.py
python3 05_스크립트/test_workflow_api.py
python3 05_스크립트/check_index_integrity.py
python3 05_스크립트/lint_bat.py 시작하기.bat 00_시작/시작하기.bat
python3 05_스크립트/check_portable.py
python3 05_스크립트/check_docs_paths.py
```

### 수정 시 주의

- `index.html`의 색 수식 또는 파라미터를 바꾸면 Python 색 코어와 파리티 케이스를 함께 갱신해야 합니다.
- 파일명 stem이나 `data.js` 경로를 바꾸면 원본 매칭과 브라우저 저장 키가 동시에 깨집니다.
- `export_pdf.py`의 warp → inset → 색보정 순서를 바꾸면 화면과 PDF 결과가 달라집니다.
- 병렬 작업의 수집 순서를 바꾸면 페이지 순서와 직렬 기준 결과가 달라질 수 있습니다.
- `worktree.json` 경로는 계획 파일 기준 상대경로가 기본입니다. v1 절대경로 하위호환을 제거하지 않습니다.

### 파일별 역할

| 파일 | 역할 |
|---|---|
| `00_시작/envcheck.py` | 실행 환경·가상환경·필수 패키지 점검 |
| `00_시작/launch.py` | 사람용 자동 브라우저 흐름과 기존 CLI 메뉴 |
| `00_시작/serve_tool.py` | 로컬 브라우저 도구 서버 |
| `02_작업장/slide_tool/index.html` | 원근·색 보정 UI와 브라우저 저장 |
| `02_작업장/slide_tool/workflow.js` | 사진 업로드·준비·고해상도 PDF 작업 순서 패널 |
| `02_작업장/slide_tool/gen_manifest.py` | 그룹 사진 목록을 `data.js`로 생성 |
| `05_스크립트/init_worktree.py` | 그룹 계획과 이동 가능한 `worktree.json` 생성 |
| `05_스크립트/prepare_photos.py` | 원본을 읽어 작업용 축소본 생성 |
| `05_스크립트/export_pdf.py` | 백업값을 원본에 적용해 PDF 생성 |
| `05_스크립트/slide_color.py` | 브라우저와 짝을 이루는 Python 색 코어 |
| `05_스크립트/check_portable.py` | 절대경로·개발 환경 문자열·삭제 기능 잔존 검사 |
| `05_스크립트/check_docs_paths.py` | 설명서 구경로와 깨진 로컬 링크 검사 |
| `05_스크립트/check_index_integrity.py` | 색 코어 해시와 워크플로 스크립트 삽입 무결성 검사 |

## 출처

이 도구는 실제 컨퍼런스 발표사진 수백 장을 정리하며 만들고 검증했습니다.

## 라이선스

MIT 라이선스입니다. 자세한 내용은 [LICENSE](LICENSE)를 확인하세요.
