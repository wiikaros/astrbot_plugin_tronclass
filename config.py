"""插件常量与默认值配置。"""

# 插件标识（与 metadata.yaml name 一致，StarTools.get_data_dir 必须显式传入，
# 否则框架通过调用栈推断插件名，在 services/ 子模块中会解析失败）
PLUGIN_NAME = "astrbot_plugin_tronclass"

# ========== 存储 Key ==========
KV_SESSION_PREFIX = "session"
KV_SESSION_ORIGIN_PREFIX = "session_origin"
KV_HOMEWORKS_PREFIX = "homeworks"
KV_SCHEDULE_PREFIX = "schedule"
KV_ROLLCALL_SEEN_PREFIX = "rollcall_seen"
KV_LOGIN_STATE_PREFIX = "login_state"
KV_LOGIN_STATE_INDEX = "_login_state_index"      # 进行中登录索引（启动清扫用）
KV_LAST_ROLLCALL_CHECK_PREFIX = "_last_rollcall_check"
KV_ALL_LOGGED_IN_USERS = "_all_logged_in_users"   # 已登录用户注册表（定时任务遍历用）
KV_LOGIN_ATTEMPTS_PREFIX = "_login_attempts"      # 登录频率限制

# ========== 登录相关 ==========
LOGIN_STATE_TTL_SECONDS = 300          # 登录状态超时（5 分钟）
MAX_LOGIN_ATTEMPTS_PER_HOUR = 3        # 每小时最大登录尝试次数
SSO_HOST = "https://sso.cuc.edu.cn"    # CAS 单点登录服务器（兜底默认，运行时从 cas_url 解析优先）

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

# ========== WeChat 登录 ==========
WECHAT_POLL_URL = "https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid={uuid}"
WECHAT_POLL_INTERVAL = 2       # 轮询间隔（秒）
WECHAT_POLL_TIMEOUT = 180      # 轮询超时（秒）

# ========== ICS 相关 ==========
ICS_DAYS_MAP = {
    "MO": 1, "TU": 2, "WE": 3, "TH": 4,
    "FR": 5, "SA": 6, "SU": 7,
}
