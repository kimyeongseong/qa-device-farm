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

# 이 스크립트는 한국어로 진행 상황을 찍는데, 윈도우 콘솔의 기본 코드페이지는
# cp1252라서 첫 줄에서 UnicodeEncodeError로 죽습니다(실제로 CI에서 그렇게
# 실패했습니다). server.py가 같은 이유로 하는 처리를 그대로 합니다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass  # 리다이렉트되어 재설정할 수 없는 스트림; 그냥 둡니다.

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

def build_server(dist_root, staging):
    """PyInstaller로 server.py를 묶습니다. 결과는 실행 파일 하나입니다.

    onedir로 시작했다가 onefile로 바꿨습니다. 이유는 두 가지입니다.

    받는 사람 입장에서 파일 하나가 훨씬 낫습니다. 폴더로 주면 `_internal`을
    열어 보고 뭔가 지우는 사람이 반드시 나오고, 그러면 원인 모를 고장으로
    돌아옵니다. 그리고 미러링 서버(Node + ws-scrcpy)까지 안에 넣어 두면 서버가
    직접 띄울 수 있어서, 배치 파일로 프로세스 두 개를 관리할 필요가 없어집니다.

    대신 두 가지를 감수합니다. 실행할 때마다 임시 폴더에 풀어서 기동이 몇 초
    걸리고, 백신이 onefile을 더 자주 걸고 넘어집니다. 안내문에 적어 둡니다.
    """
    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
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
    ]

    # 미러링 서버를 통째로 EXE 안에 넣습니다. server.py가 기동할 때 여기서
    # 꺼내 직접 띄웁니다.
    for name in ("node", "ws-scrcpy"):
        staged = os.path.join(staging, name)
        if os.path.isdir(staged):
            cmd += ["--add-data", f"{staged}{sep}{name}"]

    cmd.append(os.path.join(ROOT, "server.py"))
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

READ_ME = """# QA Device Farm ({tag})

실행 파일 하나입니다. `{exe_name}` 을 실행하면 끝입니다 — 파이썬도 Node도
따로 설치할 필요가 없고, 미러링 서버도 이 프로그램이 알아서 같이 띄웁니다.

실행하면 콘솔에 주소가 나옵니다.

    대시보드   http://localhost:8001/
               http://192.168.x.x:8001/   <- 다른 PC에서

## 다른 PC에서 접속하기

이 팜의 요점은 기기를 이 PC에만 묶어두지 않는 것입니다. 위에 나오는
`192.168.x.x` 주소를 알려주면 다른 사람도 브라우저로 씁니다. 화면 미러링까지
그대로 따라옵니다.

안 열리면 대부분 **방화벽**입니다. 관리자 권한 PowerShell에서 한 번만:

    New-NetFirewallRule -DisplayName "QA Device Farm" -Direction Inbound `
      -LocalPort 8001,8010 -Protocol TCP -Action Allow

맥은 처음 실행할 때 "네트워크 연결 허용" 창이 뜨면 허용하면 됩니다.

## adb

기기를 조작하려면 adb가 필요한데, 안에 들어 있어서 따로 준비하지 않아도 됩니다.
처음 실행할 때 실행 파일 옆 `scrcpy_bin/` 폴더로 꺼내 둡니다.

특정 버전을 쓰고 싶으면 그 폴더에 직접 넣으면 그게 우선합니다. 지금 무엇을 쓰는
중인지는 http://localhost:8001/api/health 의 `adb_path`로 확인할 수 있습니다.

소리까지 받으려면 scrcpy 2.7 이상을 같은 폴더에 넣으세요(GPL이라 포함하지
않았습니다). https://github.com/Genymobile/scrcpy/releases

## 처음 실행할 때 느리거나 경고가 뜬다면

- **기동에 몇 초 걸립니다.** 실행 파일 하나에 다 들어 있어서, 실행할 때마다
  임시 폴더에 풀고 시작합니다. 두 번째부터는 조금 빨라집니다.
- **백신이 경고할 수 있습니다.** 서명하지 않은 단일 실행 파일이라 그렇습니다.
  실행 허용으로 두시면 됩니다.
- **맥은 "확인되지 않은 개발자"** 경고가 뜹니다. 우클릭 → 열기로 한 번 넘기거나
  `xattr -dr com.apple.quarantine <파일>` 을 쓰세요.

## 만들어지는 파일

실행 파일 옆에 다음이 생깁니다.

- `scrcpy_bin/` — 꺼내 둔 adb
- `device_leases.json` — 누가 어느 기기를 쓰는 중인지
- `device_aliases.json` — 기기 별칭
- `macros/`, `logs/` — 녹화한 매크로, 저장한 logcat
- `install-id.txt`, `dist-control-cache.json` — 아래 참고

포트를 바꾸려면 `ws-scrcpy.config.json` 을 실행 파일 옆에 두면 그게 우선합니다.

## 사용 확인에 대해 (숨기지 않고 적습니다)

이 프로그램은 실행할 때와 6시간마다, 배포자가 관리하는 파일 하나를 읽어서 계속
써도 되는지 확인합니다. 확인하는 주소와 이 설치본의 ID는 실행할 때 콘솔에
표시되고, http://localhost:8001/api/health 에서도 볼 수 있습니다. 기기 정보나
사용 내역을 보내지는 않습니다.

네트워크가 안 되는 환경도 쓸 수 있게, 마지막 확인 결과로 **14일까지는** 그대로
동작합니다.

## 팜을 밖으로 열 때

기본은 인증이 없습니다. 접근할 수 있는 누구나 기기를 조작하고 APK를 설치할 수
있습니다. 사내망 전용이면 그대로 두고, 밖으로 노출한다면 실행 전에 토큰을
설정하세요.

    윈도우   set DEVICE_FARM_TOKEN=긴-임의-문자열
    맥       export DEVICE_FARM_TOKEN=긴-임의-문자열

자세한 내용은 프로젝트 README를 보세요.
https://github.com/kimyeongseong/qa-device-farm
"""


def write_extras(app_dir, tag):
    """실행 파일 옆에 둘 안내문과 라이선스.

    실행 스크립트는 없습니다 -- 실행 파일 하나가 미러링 서버까지 직접 띄웁니다.
    """
    exe_name = APP_NAME + (".exe" if os.name == "nt" else "")
    with open(os.path.join(app_dir, "먼저-읽어주세요.txt"), "w", encoding="utf-8") as f:
        f.write(READ_ME.replace("{tag}", tag).replace("{exe_name}", exe_name))

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
    staging = os.path.join(BUILD_DIR, "staging")
    for path in (app_dir, staging):
        if os.path.exists(path):
            shutil.rmtree(path)
    os.makedirs(app_dir, exist_ok=True)
    os.makedirs(staging, exist_ok=True)

    log(f"대상: {tag}")

    # 미러링 서버를 먼저 모아 둡니다 -- EXE 안에 같이 들어가야 해서 빌드보다
    # 앞서야 합니다.
    if not args.skip_node:
        fetch_node(os.path.join(staging, "node"))
    if not args.skip_ws_scrcpy:
        build_ws_scrcpy(staging)

    build_server(app_dir, staging)
    write_extras(app_dir, tag)

    exe = os.path.join(app_dir, APP_NAME + (".exe" if os.name == "nt" else ""))
    if os.path.exists(exe):
        log(f"실행 파일: {exe} ({os.path.getsize(exe)/1048576:.0f}MB)")

    archive = shutil.make_archive(app_dir, "zip", root_dir=OUT_DIR,
                                  base_dir=os.path.basename(app_dir))
    size = os.path.getsize(archive) / (1024 * 1024)
    log(f"완료: {archive} ({size:.0f}MB)")


if __name__ == "__main__":
    main()
