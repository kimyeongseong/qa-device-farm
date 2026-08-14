"""맥·윈도우용 배포본을 만듭니다.

    python build/build.py

받는 사람이 파이썬도 Node도 설치하지 않고 압축만 풀어 실행할 수 있는 폴더를
만드는 게 목표입니다. 그래서 세 가지를 한 폴더에 넣습니다.

1. PyInstaller로 묶은 팜 서버 (파이썬 런타임 포함)
2. 빌드한 ws-scrcpy와 그 의존성
3. 그걸 돌릴 Node 런타임 (nodejs.org 공식 빌드에서 실행 파일만)

**크로스 컴파일은 안 됩니다.** PyInstaller는 자기가 돌고 있는 OS용 실행 파일만
만들고, Node 런타임도 플랫폼별로 다릅니다. 맥용은 맥에서, 윈도우용은 윈도우에서
돌려야 합니다 — 그래서 이 스크립트를 GitHub Actions의 macOS·Windows 러너에서
돌립니다 (.github/workflows/build.yml).

adb와 scrcpy는 넣지 않습니다. 각각 Android SDK 약관과 GPL이 재배포를 제한하고,
저장소가 이미 같은 이유로 빼 놓고 있습니다. 대신 배포본에 빈 scrcpy_bin/과
어디서 받아 어디에 두는지 적은 안내를 같이 넣습니다.
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_DIR = os.path.join(ROOT, "build")
OUT_DIR = os.path.join(BUILD_DIR, "out")
APP_NAME = "qa-device-farm"

# 번들할 Node. LTS 계열에서 고정합니다 — 여기서 올려도 되지만, 팜이 실제로
# 검증된 버전을 배포본에 박아두는 편이 낫습니다.
NODE_VERSION = "20.18.1"


def log(message):
    print(f"[build] {message}", flush=True)


def run(cmd, cwd=None, env=None):
    log(f"$ {' '.join(cmd)}" + (f"   (cwd={cwd})" if cwd else ""))
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def platform_tag():
    """배포본 이름에 붙일 꼬리표. 맥은 칩까지 구분해야 합니다."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x64"
    if system == "Windows":
        return "windows-x64" if machine.endswith("64") else "windows-x86"
    return f"{system.lower()}-{machine}"


# --- 1. 팜 서버 ---

def build_server(dist_root):
    """PyInstaller로 server.py를 묶습니다.

    onefile이 아니라 onedir입니다. onefile은 실행할 때마다 임시 폴더에 통째로
    풀어서 기동이 느리고, 무엇보다 백신이 자주 걸고 넘어집니다. 폴더째 주면
    안에 뭐가 들었는지 눈으로 볼 수도 있습니다.
    """
    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--distpath", dist_root,
        "--workpath", os.path.join(BUILD_DIR, "work"),
        "--specpath", os.path.join(BUILD_DIR, "spec"),
        # 읽기 전용 자산만 번들에 넣습니다. 쓰는 파일(lease·macro·log)은
        # server.py가 실행 파일 옆에 만듭니다.
        "--add-data", f"{os.path.join(ROOT, 'static')}{sep}static",
        "--add-data", f"{os.path.join(ROOT, 'ws-scrcpy.config.json')}{sep}.",
        # uvicorn과 websockets는 문자열로 늦게 import돼서 PyInstaller가
        # 정적 분석으로 못 찾습니다. 빠뜨리면 실행할 때 서버가 못 뜹니다.
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "websockets.legacy",
        # 반대로 이건 빼야 합니다. urllib3의 선택적 pyOpenSSL 경로 때문에 의존성
        # 그래프에 딸려 들어오는데, 팜은 어디서도 부르지 않습니다(HTTPS는 표준
        # ssl 모듈이 처리하고, adb 인증은 adb 서버가 합니다). 두면 배포본만
        # 커지고, 파이썬이 두 벌 깔린 빌드 머신에서는 훅이 터집니다.
        "--exclude-module", "cryptography",
        "--console",
        os.path.join(ROOT, "server.py"),
    ]
    run(cmd)


# --- 2. Node 런타임 ---

def node_asset():
    system, machine = platform.system(), platform.machine().lower()
    if system == "Darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
        return f"node-v{NODE_VERSION}-darwin-{arch}.tar.gz", "tar"
    if system == "Windows":
        return f"node-v{NODE_VERSION}-win-x64.zip", "zip"
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return f"node-v{NODE_VERSION}-linux-{arch}.tar.xz", "tar"


def fetch_node(target_dir):
    """공식 배포본에서 node 실행 파일만 꺼내 옵니다.

    전체 배포본에는 npm과 헤더까지 들어 있는데, 배포본에서 하는 일은
    `node index.js` 하나뿐이라 실행 파일만 있으면 됩니다 (수십 MB 절약).
    """
    asset, kind = node_asset()
    url = f"https://nodejs.org/dist/v{NODE_VERSION}/{asset}"
    cache = os.path.join(BUILD_DIR, "cache")
    os.makedirs(cache, exist_ok=True)
    archive = os.path.join(cache, asset)

    if not os.path.exists(archive):
        log(f"Node {NODE_VERSION} 내려받는 중: {url}")
        urllib.request.urlretrieve(url, archive)

    os.makedirs(target_dir, exist_ok=True)
    if kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            member = next(n for n in zf.namelist() if n.endswith("/node.exe"))
            with zf.open(member) as src, open(os.path.join(target_dir, "node.exe"), "wb") as dst:
                shutil.copyfileobj(src, dst)
        # 윈도우 Node는 이 DLL이 옆에 있어야 도는 빌드가 있습니다.
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if name.endswith(".dll") and "/" in name and name.count("/") == 1:
                    with zf.open(name) as src, \
                         open(os.path.join(target_dir, os.path.basename(name)), "wb") as dst:
                        shutil.copyfileobj(src, dst)
        node_path = os.path.join(target_dir, "node.exe")
    else:
        with tarfile.open(archive) as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith("/bin/node"))
            src = tf.extractfile(member)
            node_path = os.path.join(target_dir, "node")
            with open(node_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        os.chmod(node_path, 0o755)

        # LICENSE도 같이 (Node는 MIT이고, 재배포하면 고지 의무가 있습니다).
        with tarfile.open(archive) as tf:
            lic = next((m for m in tf.getmembers() if m.name.endswith("/LICENSE")), None)
            if lic:
                with tf.extractfile(lic) as src, \
                     open(os.path.join(target_dir, "LICENSE-node.txt"), "wb") as dst:
                    shutil.copyfileobj(src, dst)

    log(f"Node 준비 완료: {node_path}")
    return node_path


# --- 3. ws-scrcpy ---

def build_ws_scrcpy(app_dir):
    """ws-scrcpy를 빌드하고, 돌아가는 데 필요한 것만 배포본에 넣습니다."""
    source = os.path.join(ROOT, "ws-scrcpy")
    npm = "npm.cmd" if os.name == "nt" else "npm"

    run([npm, "install", "--no-audit", "--no-fund"], cwd=source)
    run([npm, "run", "dist"], cwd=source)

    dist = os.path.join(source, "dist")
    if not os.path.exists(dist):
        raise SystemExit("ws-scrcpy 빌드 결과(dist/)가 없습니다")

    # dist/package.json은 런타임 의존성만 담고 있습니다(웹팩이 만들어 줍니다).
    # 전부 순수 자바스크립트라 네이티브 빌드가 필요 없습니다.
    run([npm, "install", "--omit=dev", "--no-audit", "--no-fund"], cwd=dist)

    target = os.path.join(app_dir, "ws-scrcpy")
    if os.path.exists(target):
        shutil.rmtree(target)
    shutil.copytree(dist, target)
    log(f"ws-scrcpy 복사 완료: {target}")


# --- 4. 실행 스크립트와 안내 ---

LAUNCHER_SH = """#!/bin/bash
# QA Device Farm — 맥에서 더블클릭으로 실행합니다.
cd "$(dirname "$0")"

# 팜은 adb로 기기를 조작합니다. 여기 있는 것을 먼저 쓰고, 없으면 PATH에서 찾습니다.
# ws-scrcpy는 adb를 PATH에서 직접 실행하므로 두 서버가 같은 것을 보도록 맞춥니다.
if [ -x "./scrcpy_bin/adb" ]; then
  export PATH="$PWD/scrcpy_bin:$PATH"
fi

# 스트림 포트는 이 파일 한 곳에서만 정의합니다.
export WS_SCRCPY_CONFIG="$PWD/ws-scrcpy.config.json"

# 점유·매크로·로그를 이 폴더에 둡니다. 지정하지 않으면 실행 파일 옆(한 단계
# 안쪽)에 만들어져서 찾기 어렵습니다.
export DEVICE_FARM_HOME="$PWD"

# 팜을 사내망 밖으로 열 때는 아래 주석을 풀고 긴 임의 문자열을 넣으세요.
# export DEVICE_FARM_TOKEN="여기에-긴-임의-문자열"

echo "미러링 서버를 시작합니다..."
./node/node ./ws-scrcpy/index.js &
STREAM_PID=$!
# 창을 닫으면 미러링 서버도 같이 내려가야 합니다. 남으면 포트를 쥔 채 떠돕니다.
trap 'kill $STREAM_PID 2>/dev/null' EXIT

echo "대시보드를 시작합니다..."
echo "==================================================="
echo "  대시보드   http://localhost:8001/"
echo "  API 문서   http://localhost:8001/docs"
echo "==================================================="
./{app_name}/{app_name}
"""

LAUNCHER_BAT = """@echo off
chcp 65001 >nul
title QA Device Farm
cd /d "%~dp0"

:: 팜은 adb로 기기를 조작합니다. 여기 있는 것을 먼저 쓰고, 없으면 PATH에서 찾습니다.
:: ws-scrcpy는 adb를 PATH에서 직접 실행하므로 두 서버가 같은 것을 보도록 맞춥니다.
if exist "%~dp0scrcpy_bin\\adb.exe" set "PATH=%~dp0scrcpy_bin;%PATH%"

:: 스트림 포트는 이 파일 한 곳에서만 정의합니다.
set "WS_SCRCPY_CONFIG=%~dp0ws-scrcpy.config.json"

:: 점유·매크로·로그를 이 폴더에 둡니다. 지정하지 않으면 실행 파일 옆(한 단계
:: 안쪽)에 만들어져서 찾기 어렵습니다.
set "DEVICE_FARM_HOME=%~dp0"

:: 팜을 사내망 밖으로 열 때는 아래 주석을 풀고 긴 임의 문자열을 넣으세요.
:: set "DEVICE_FARM_TOKEN=여기에-긴-임의-문자열"

echo 미러링 서버를 시작합니다...
start "QA Device Farm - 미러링" /min "%~dp0node\\node.exe" "%~dp0ws-scrcpy\\index.js"

echo ===================================================
echo   대시보드   http://localhost:8001/
echo   API 문서   http://localhost:8001/docs
echo ===================================================
"%~dp0{app_name}\\{app_name}.exe"
"""

READ_ME = """# QA Device Farm ({tag})

압축을 풀고 아래 파일을 실행하면 됩니다. 파이썬이나 Node를 따로 설치할 필요는
없습니다 — 둘 다 안에 들어 있습니다.

- 맥: `시작하기.command` (더블클릭)
- 윈도우: `시작하기.bat` (더블클릭)

그다음 브라우저에서 http://localhost:8001/ 을 엽니다.

## adb는 직접 넣어야 합니다

기기를 조작하려면 Android platform-tools의 `adb`가 필요합니다. 라이선스상 여기
포함할 수 없어서 빠져 있습니다.

1. https://developer.android.com/tools/releases/platform-tools 에서 받습니다
2. 압축을 풀고 `adb`(윈도우는 `adb.exe`와 같이 들어 있는 dll 전부)를
   이 폴더의 `scrcpy_bin/` 안에 넣습니다

이미 PATH에 adb가 있으면 그것을 씁니다. 지금 무엇을 쓰고 있는지는
http://localhost:8001/api/health 의 `adb_path`로 확인할 수 있습니다.

## 소리도 받으려면 (선택)

scrcpy 2.7 이상 바이너리를 같은 `scrcpy_bin/`에 넣으세요. GPL이라 역시 포함하지
않았습니다. 없으면 오디오 버튼만 동작하지 않고 나머지는 정상입니다.

https://github.com/Genymobile/scrcpy/releases

## 맥에서 "확인되지 않은 개발자" 경고가 뜬다면

서명하지 않은 빌드라 처음 한 번은 macOS가 막습니다. 둘 중 하나로 넘어갑니다.

- `시작하기.command`를 **우클릭 → 열기** 후 한 번 더 열기
- 또는 터미널에서: `xattr -dr com.apple.quarantine "이 폴더 경로"`

## 만들어지는 파일

실행하면 이 폴더에 다음이 생깁니다. 지워도 되지만 지우면 그 내용은 사라집니다.

- `device_leases.json` — 누가 어느 기기를 쓰는 중인지
- `device_aliases.json` — 기기 별칭
- `macros/` — 녹화한 매크로
- `logs/` — 파일로 저장한 logcat

## 팜을 밖으로 열 때

기본은 인증이 없습니다. 접근할 수 있는 누구나 기기를 조작하고 APK를 설치할 수
있습니다. 사내망 전용이면 그대로 두고, 밖으로 노출한다면 실행 스크립트 안의
`DEVICE_FARM_TOKEN` 줄의 주석을 풀고 긴 임의 문자열을 넣으세요.

자세한 내용은 프로젝트 README를 보세요.
https://github.com/kimyeongseong/qa-device-farm
"""


def write_extras(app_dir, tag):
    name = "시작하기.bat" if os.name == "nt" else "시작하기.command"
    body = (LAUNCHER_BAT if os.name == "nt" else LAUNCHER_SH).replace("{app_name}", APP_NAME)
    path = os.path.join(app_dir, name)
    # 윈도우 배치 파일은 CRLF여야 안전합니다.
    with open(path, "w", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n") as f:
        f.write(body)
    if os.name != "nt":
        os.chmod(path, 0o755)

    with open(os.path.join(app_dir, "먼저-읽어주세요.txt"), "w", encoding="utf-8") as f:
        f.write(READ_ME.replace("{tag}", tag))

    # 사용자가 adb를 넣을 자리. 빈 폴더는 zip에 안 담기므로 안내문을 하나 둡니다.
    tools = os.path.join(app_dir, "scrcpy_bin")
    os.makedirs(tools, exist_ok=True)
    with open(os.path.join(tools, "여기에-adb를-넣으세요.txt"), "w", encoding="utf-8") as f:
        f.write("Android platform-tools의 adb를 이 폴더에 넣으세요.\n"
                "https://developer.android.com/tools/releases/platform-tools\n\n"
                "소리까지 받으려면 scrcpy 2.7 이상도 같은 폴더에 넣으면 됩니다.\n"
                "https://github.com/Genymobile/scrcpy/releases\n")

    # 스트림 포트 설정은 실행 파일 옆에도 둡니다 — 배포본을 받은 사람이
    # 포트를 바꾸려면 만질 수 있어야 합니다.
    shutil.copy2(os.path.join(ROOT, "ws-scrcpy.config.json"), app_dir)

    for doc in ("LICENSE", "NOTICE.md", "README.md"):
        source = os.path.join(ROOT, doc)
        if os.path.exists(source):
            shutil.copy2(source, app_dir)


def main():
    parser = argparse.ArgumentParser(description="맥·윈도우 배포본 빌드")
    parser.add_argument("--skip-ws-scrcpy", action="store_true",
                        help="미러링 서버 빌드를 건너뜁니다 (빠른 확인용)")
    parser.add_argument("--skip-node", action="store_true",
                        help="Node 런타임 내려받기를 건너뜁니다 (빠른 확인용)")
    args = parser.parse_args()

    tag = platform_tag()
    app_dir = os.path.join(OUT_DIR, f"{APP_NAME}-{tag}")
    if os.path.exists(app_dir):
        shutil.rmtree(app_dir)
    os.makedirs(app_dir, exist_ok=True)

    log(f"대상: {tag}")
    build_server(app_dir)
    if not args.skip_node:
        fetch_node(os.path.join(app_dir, "node"))
    if not args.skip_ws_scrcpy:
        build_ws_scrcpy(app_dir)
    write_extras(app_dir, tag)

    archive = shutil.make_archive(app_dir, "zip", root_dir=OUT_DIR,
                                  base_dir=os.path.basename(app_dir))
    size = os.path.getsize(archive) / (1024 * 1024)
    log(f"완료: {archive} ({size:.0f}MB)")


if __name__ == "__main__":
    main()
