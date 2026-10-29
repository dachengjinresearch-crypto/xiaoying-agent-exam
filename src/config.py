"""路径与执行预算常量。

预算上限是题目硬性要求：超出时必须返回结构化未完成状态，而不是抛异常或静默截断。
"""

from pathlib import Path

# 项目根目录（src 的上一级）
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
EXAMPLES_DIR = BASE_DIR / "examples"
CASES_DIR = EXAMPLES_DIR / "cases"

# 执行预算（docs/02-solution.md §4.7）
#
# 注意：单素材的完整链路约需 4-5 步（detail → rules → slots → save → next），
# 7 个素材实测约 32 步 / 25 次工具调用。因此预算必须是"全局 + 单素材"双层：
# 全局上限防止死循环，单素材上限防止某个素材反复试探吃掉全部额度。
MAX_STEPS = 80  # 全局硬上限
MAX_STEPS_PER_ASSET = 8  # 单素材步数上限
MAX_TOOL_CALLS = 60  # 全局工具调用上限
PER_TOOL_TIMEOUT_S = 5.0
TOTAL_WALL_S = 120.0

# 数据文件名，避免各层散落字符串
DATA_FILES = (
    "assets",
    "asset_extra",
    "rules",
    "schedule",
    "failure_config",
)
