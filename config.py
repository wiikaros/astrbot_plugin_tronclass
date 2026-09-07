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
KV_PUSH_FAIL_PREFIX = "_push_fail"                # 推送失败计数（P0-4）
KV_DUE_NOTIFIED_PREFIX = "_due_notified"          # 快到期已通知记录（P0-1）
KV_SCHEDULE_EXPIRED_PREFIX = "_schedule_expired"  # 课表过期已提醒（P1-1）

# ========== 推送与去重（P0-1/P0-4） ==========
PUSH_FAIL_THRESHOLD = 3                 # 连续推送失败阈值
PUSH_FAIL_NOTIFY_COOLDOWN = 3600        # 失败提示冷却（秒），防每轮轰炸
DUE_WARN_INNER_LEVELS_HOURS = (6, 1)    # 快到期内部分级（小时），最外层用 homework_due_warn_hours
DUE_NOTIFIED_MAX_ENTRIES = 200          # 去重记录容量上限（防 KV 膨胀）

# ========== 拉取失败退避（P1-5） ==========
# 序列：1min → 2min → 4min → 8min → 16min → 30min（封顶）
FETCH_FAIL_BACKOFF_BASE = 60            # 基础退避（秒）
FETCH_FAIL_BACKOFF_MAX = 1800           # 最大退避（秒）= 30 分钟
FETCH_FAIL_ALERT_THRESHOLD = 5          # 连续失败达此数升级为 error 日志

# ========== 登录相关 ==========
LOGIN_STATE_TTL_SECONDS = 300          # 登录状态超时（5 分钟）
MAX_LOGIN_ATTEMPTS_PER_HOUR = 3        # 每小时最大登录尝试次数
SSO_HOST = "https://sso.cuc.edu.cn"    # CAS 单点登录服务器（兜底默认，运行时从 cas_url 解析优先）

# ========== 固定值（插件仅接入中国传媒大学畅课） ==========
BASE_URL = "https://courses.cuc.edu.cn"

# ========== 默认值 ==========
DEFAULT_HOMEWORK_CHECK_INTERVAL = 30   # 分钟
DEFAULT_ROLLCALL_DEFAULT_INTERVAL = 5  # 分钟
DEFAULT_ROLLCALL_PRECHECK_MINUTES = 5  # 分钟
DEFAULT_HOMEWORK_DUE_WARN_HOURS = 24   # 小时

# ========== 免打扰时段（P1-4） ==========
DEFAULT_QUIET_HOURS_ENABLED = True
DEFAULT_QUIET_HOURS_START = "23:00"
DEFAULT_QUIET_HOURS_END = "07:00"

# ========== API 端点 ==========
ENDPOINT_TODOS = "/api/todos"
ENDPOINT_ROLLCALLS = "/api/radar/rollcalls"

# ========== 点名（P1-6） ==========
# 黑名单策略：只剔除已签到，未知 status 一律推送（漏通知 = 缺勤，代价高于多推）
ROLLCALL_STATUS_ATTENDED = "on_call_fine"    # 已签到状态

# ========== WeChat 登录 ==========
WECHAT_POLL_URL = "https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid={uuid}"
WECHAT_POLL_INTERVAL = 2       # 轮询间隔（秒）
WECHAT_POLL_TIMEOUT = 180      # 轮询超时（秒）

# ========== ICS 相关 ==========
ICS_DAYS_MAP = {
    "MO": 1, "TU": 2, "WE": 3, "TH": 4,
    "FR": 5, "SA": 6, "SU": 7,
}

# ========== 课表过期（P1-1） ==========
SCHEDULE_EXPIRED_NOTIFY_COOLDOWN = 86400   # 过期提醒冷却（秒）= 24h，防每轮轰炸
