from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
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
