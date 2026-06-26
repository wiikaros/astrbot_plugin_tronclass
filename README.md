# AstrBot 畅课助手插件

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.13-green.svg)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/License-AGPLv3-orange.svg)](LICENSE)

为中国传媒大学畅课系统（TronClass）开发的 AstrBot 插件。支持 **微信扫码一键登录**、**作业查询与截止提醒**、**课表驱动的点名实时通知**。

## 功能

- **微信扫码登录**：无需密码和验证码，微信扫码即可绑定账号
- **账号密码登录**：支持 CAS SSO 登录（含图片验证码和短信验证码）
- **作业查询**：`/作业列表` 查看未完成作业，按截止时间排序
- **自动更新**：定时检测新作业发布，自动推送私聊通知
- **截止提醒**：作业快到期时自动提醒
- **点名通知**：ICS 课表驱动，仅在上课时检测点名，实时推送签到提醒
- **多用户**：每个成员独立绑定账号，互不干扰
- **课表导入**：上传 .ics 课表文件，智能判断上课时间
- **Session 过期检测**：自动检测登录状态，过期时通知用户重新登录

## 安装

1. 将插件目录放入 AstrBot 的 `data/plugins/` 下：

```bash
cd data/plugins
git clone https://github.com/wiikaros/astrbot_plugin_tronclass.git
```

2. 重启 AstrBot 或在 WebUI 中加载插件。

3. 插件依赖会在加载时自动安装（`aiohttp`, `icalendar`）。

## 使用

### 1. 登录畅课（推荐微信扫码）

在**私聊**中发送：

```
/微信登录
```

打开收到的二维码链接，用微信扫描并确认登录即可。无需密码，无需验证码。

> 也可用账号密码登录：`/登录畅课`

### 2. 上传课表（可选，推荐）

将自己的课表导出为 .ics 文件，发送给机器人并附带命令：

```
/上传课表
```

> 获取 .ics 课表：大部分高校教务系统支持导出课表为 .ics 格式，也可以在手机日历 App 中导出。

### 3. 查询作业

在群聊或私聊中：

```
/作业列表
/更新作业
```

### 4. 自动通知

- **作业检测**：每 30 分钟自动检测（可在 WebUI 配置）
- **点名检测**：有课表时仅在上课时间检测；无课表时每 5 分钟检测一次
- **Session 过期**：主动检测登录过期，私聊提醒用户重新登录

## 命令一览

| 命令 | 功能 | 适用场景 |
|------|------|---------|
| `/微信登录` | 微信扫码登录 | 私聊 |
| `/登录畅课` | 账号密码登录 | 私聊 |
| `/作业列表` | 查询未完成作业 | 群聊/私聊 |
| `/更新作业` | 手动刷新作业列表 | 群聊/私聊 |
| `/上传课表` | 上传 .ics 课表文件 | 私聊/群聊（带文件） |
| `/重置登录限制` | 清除登录频率限制 | 管理员 |

## 配置

在 AstrBot WebUI 的插件管理 → 畅课助手 → 配置面板中可调整：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 服务器地址 | `courses.cuc.edu.cn` | 畅课服务器地址 |
| 作业检测间隔 | 30 分钟 | 自动检测作业更新的频率 |
| 点名检测间隔 | 5 分钟 | 无课表时的点名检测频率 |
| 课前提前检测 | 5 分钟 | 上课前多久开始检测点名 |
| 快到期提醒 | 24 小时 | 距截止多久时提醒 |
| 新作业通知 | 开启 | 新作业发布时推送通知 |
| 快到期提醒 | 开启 | 作业快到期时推送提醒 |
| 点名通知 | 开启 | 新点名时推送通知 |

## 运行测试

```bash
pip install pytest
python -m pytest tests/ -v  # 29 个用例
```

## 项目结构

```
astrbot_plugin_tronclass/
├── main.py                # 插件入口，命令注册，登录状态机
├── metadata.yaml           # 插件元数据
├── _conf_schema.json       # WebUI 可视化配置面板
├── requirements.txt        # aiohttp, icalendar
├── config.py               # 常量与默认值
├── api/
│   ├── __init__.py
│   ├── _utils.py           # 内部工具函数（JWT 解码、日期解析）
│   ├── auth.py             # CAS SSO 登录、Session 管理、密码登录
│   ├── wechat_login.py     # 微信扫码登录（combinedLogin 流程）
│   ├── homework.py         # 作业 API + 比对 + 快到期检测
│   └── rollcall.py         # 点名 API + 新点名检测
├── services/
│   ├── __init__.py
│   ├── storage.py          # KV 存储统一封装
│   ├── ics_parser.py       # .ics 课表解析 + is_in_class_now()
│   ├── scheduler.py        # Cron 定时任务管理
│   └── notifier.py         # 通知消息生成器
├── tests/                  # 单元测试（29 用例）
│   ├── test_homework.py
│   ├── test_ics_parser.py
│   └── test_rollcall.py
├── debug/                  # 独立调试工具（不随插件加载）
│   └── login_debug.py
└── 示例数据包/              # API 请求/响应样例
```

## License

AGPL-3.0 © wiikaros
