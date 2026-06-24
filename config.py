"""插件常量与默认值配置。"""

# ========== 存储 Key ==========
KV_SESSION_PREFIX = "session"
KV_HOMEWORKS_PREFIX = "homeworks"
KV_SCHEDULE_PREFIX = "schedule"
KV_ROLLCALL_STATES = "rollcall_states"
KV_LOGIN_STATE_PREFIX = "login_state"

# ========== 登录相关 ==========
LOGIN_STATE_TTL_SECONDS = 300          # 登录状态超时（5 分钟）
MAX_LOGIN_ATTEMPTS_PER_HOUR = 3        # 每小时最大登录尝试次数

# ========== 默认值 ==========
DEFAULT_BASE_URL = "https://courses.cuc.edu.cn"
DEFAULT_SCHOOL_NAME = "中国传媒大学"
DEFAULT_HOMEWORK_CHECK_INTERVAL = 30   # 分钟
DEFAULT_ROLLCALL_DEFAULT_INTERVAL = 5  # 分钟
DEFAULT_ROLLCALL_PRECHECK_MINUTES = 5  # 分钟
DEFAULT_HOMEWORK_DUE_WARN_HOURS = 24   # 小时

# ========== API 端点 ==========
ENDPOINT_TODOS = "/api/todos"
ENDPOINT_ROLLCALLS = "/api/radar/rollcalls"

# ========== ICS 相关 ==========
ICS_DAYS_MAP = {
    "MO": 1, "TU": 2, "WE": 3, "TH": 4,
    "FR": 5, "SA": 6, "SU": 7,
}
