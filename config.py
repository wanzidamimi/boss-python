import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def load_env(path: Path | None = None) -> None:
    """加载项目根目录的 .env（不覆盖已有环境变量）。

    平时由 启动BOSS服务.bat 注入；直接运行脚本时靠这里补齐，
    避免 db.py 连库时拿到空密码。
    """
    env_path = path or (BASE_DIR / ".env")
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


load_env()
CAPTURE_DIR = BASE_DIR / "captures"
DATA_DIR = BASE_DIR / "data"

CDP_DEBUG_PORT = 9223
# 必须使用正常的职位列表页：带 _security_check 的地址会让页面处于安全校验态，
# 导致 security-js 产出的 stoken 与服务端期望不一致（接口返回 code=37）。
TARGET_URL = "https://www.zhipin.com/web/geek/jobs"

INTERESTING_URL_PARTS = (
    "search/joblist.json",
    "recommend/job/list.json",
    "detail.json",
    "set/zpToken",
    "getUserInfo.json",
    "smsSwitch",
)

CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
