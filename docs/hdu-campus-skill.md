# 杭电校园助手 · 接口与运维手册

写给未来的自己：出问题时先查这张表，别从头读代码。

---

## 一、数据从哪来

| 来源 | 提供什么 | 怎么调 | 要不要登录 |
|---|---|---|---|
| hduhelp-cli | 课表、成绩、余额、电费、空教室、图书馆、长跑、活动、失物 | 命令行，见下节 | 要（OAuth 设备码） |
| 学院官网 | 通知、专业、师资、领导、联系方式、科研 | 直接抓 HTML | 不要 |
| 教务处 / 后勤 / 科研院 | 转专业、报修电话、政策原文 | 直接抓 HTML | 不要 |
| 杭电邮箱 | 未读邮件、最近邮件 | IMAP | 要（网易授权码） |
| 智谱 / DeepSeek | 措辞、摘要、联网搜索 | HTTP API | 要（Key） |

学院官网的坑：主站 `www.hdu.edu.cn` 上那些学院链接**全是空壳页**，
真站在独立子域（`ee.` `cs.` `me.` …），属性还可能用单引号。
完整域名表在 `app/tools/college.py` 的 `COLLEGES`。

---

## 二、hduhelp-cli 命令

```bash
# 授权状态（不消耗额度）
hduhelp-cli auth status --json
hduhelp-cli auth scopes --json

# 重新授权（会打印设备码，浏览器里确认）
hduhelp-cli auth login --scope S1,S2
hduhelp-cli auth login --all-scopes

# 课表 / 余额 / 电费
hduhelp-cli academic schedule ...
hduhelp-cli campuslife card balance --json
hduhelp-cli campuslife electric balance --json

# 空教室（必须现查，别用本地缓存算）
hduhelp-cli campus ... --status unused

# 二课活动
hduhelp-cli campuslife activities list --status 8 --json      # 8 = 报名中
hduhelp-cli campuslife activities registration apply --activity-id X --yes
```

当前已授权 **11 个只读 scope**，没有 `campuslife:activity:write`。
所以**能查活动、不能报名**——报名会报权限不足。
重新授权时必须把 11 个原有 scope 一起写上，只申请新的会把旧的覆盖掉。

### `API_40920` 有两种含义，都会报成这一个码

| # | 实际原因 | 在哪解决 |
|---|---|---|
| ① | **OAuth scope 没开**（命令行授权范围里没有这项权限） | 网页授权：`hduhelp-cli auth login --scope ...`，浏览器里点同意 |
| ② | **杭电助手 App 内的服务授权没开** | 杭电助手 App / 小程序 → 我的 · 设置 → 服务授权 → 找到对应项（如「二课活动」）→ 同意 |

**两者都要开才能用**：
① 决定这串令牌有没有资格访问这个接口，② 决定你在杭电助手那边授不授权这个服务。
只开一个，表现都是 40920，容易互相甩锅。

其他错误码：

| 码 | 含义 | 怎么解决 |
|---|---|---|
| `MISSING_SCOPE` / 权限不足 | CLI 的 OAuth scope 里确实没有这项 | `hduhelp-cli auth login --scope ...` 重新授权，注意把原有 scope 一起写上 |
| `HTTP_401` | 令牌过期或被撤销 | 同上 |

### 查活动 vs 报名：两件事，别混

| 场景 | 需要什么 | 报错时的表现 |
|---|---|---|
| 查活动列表 | `campuslife:activity:read` + App 服务授权 | 「查不到活动列表」 |
| 提交报名 | 上面这些 **再加** `campuslife:activity:write` | 「报名没提交成功」 |

代码里这两类错误走不同提示，不要合并成一句泛泛的「去授权」。

---

## 三、只读 / 写操作规范

**只读**（随便调）：课表、余额、电费、空教室、图书馆、长跑、失物、学院官网、邮箱已读。

**写操作**（必须用户确认）：

| 操作 | 风险 | 规则 |
|---|---|---|
| 二课报名 | 会占名额，可能报错活动 | 先出确认卡片，点「确认报名」才提交 |
| 取消报名 | 影响签到记录 | 同上 |
| 发邮件 | 发出去收不回 | **默认关闭**，`.env` 里 `MAIL_SMTP_ENABLED=1` 才启用 |
| 删除记忆 / 提醒 | 数据丢失 | 用户明说才做 |

写操作一律走 `--yes` 前先确认，代码里不出现"自动重试写操作"。

---

## 四、诊断脚本

| 脚本 | 用途 |
|---|---|
| `py -3 scripts/env_check.py` | 环境自检：依赖、配置项、CLI、数据库、备份目录、人设文件 |
| `scripts/run_bot.bat --check` | 同上，给双击用 |
| `py -3 scripts/test_local.py` | 56 条回归 + 5 条红线断言，GBK 环境直接跑 |
| `py -3 scripts/chat.py` | 命令行聊天，不连飞书 |
| `py -3 scripts/reauth.py` | CLI 重新授权引导 |
| 机器人里发「自检」 | 三项在线检查：CLI 授权 / 数据库完整性 / 最近一次成功回答 |

自检**不会 ping 模型**（那个花钱）。想确认模型活着，问一句真问题。

---

## 五、故障对照表

| 现象 | 原因 | 解决 |
|---|---|---|
| 飞书报 **21001** | 应用权限改了但**没发布版本** | 开放平台 → 版本管理与发布 → 创建版本并发布 |
| 报 **99991672** | 缺 `im:resource`（传图）或 `im:message:readonly`（下语音） | 补权限 → 发版本 |
| 占位卡片不变成结果 | 卡片缺 `config.update_multi = true`，飞书不允许 PATCH | 构造卡片时带上 |
| 卡片更新连续失败 | `_PATCH_STATE["fails"]` 累计到 3，自动停发占位卡 | 查权限，修好后重启 |
| 按钮点了没反应 | `card.action.trigger` 配在了「事件配置」，应该在「回调配置」 | 挪到回调配置，长连接要在跑才能保存 |
| 消息时有时无 | 同一个飞书 app 被本机和云端**同时长连接** | 只保留一处连接 |
| 控制台报 GBK 编码错 | 中文 Windows 默认 GBK，打印 `•` `💡` 会崩 | 入口第一行 `from app.console import setup; setup()` |
| CLI 报未登录 / 401 | OAuth 过期或被清 | `hduhelp-cli auth login --scope ...` |
| 学院页抓到 0 条 | 站点列表是 JS 渲染 | 自动回落到首页抓带日期的条目，回复里标「首页最新」 |
| 通知日期错位 | 页面日期列和标题不在一行 | 已改用 URL 里的 `/年/月日/` 取日期 |
| `you 13 要去上课` | 时间字段被字符串硬切 | 已改用 `_hhmm()` 按 `HH:MM` 提取 |

---

## 六、目录速查

```
app/
  agent/         路由、处理器、Agent、分页
  tools/         校园数据、学院抓取、FAQ、邮件、图表
  feishu/        长连接、卡片、调度器
  ops/           备份、审计、限流、自检
  llm/           双模型客户端
scripts/         回归、聊天、环境检查、启动入口
data/            campus.db、备份、生成的图片
logs/            bot-YYYYMMDD.log
docs/            本文档
```
