# LinkedIn 动态追踪器

定时监控指定 LinkedIn 用户的最新动态，发现新帖子时自动发送邮件通知。

---

## 功能特点

- 使用真实浏览器（Chrome）登录 LinkedIn，绕过基础反爬措施
- 保存已见帖子 ID，仅在出现**真正的新内容**时发送通知
- 邮件包含帖子正文、发布时间和原贴链接（HTML + 纯文本双格式）
- 支持按分钟级别自定义检查频率

---

## 环境要求

| 软件 | 版本要求 |
|------|----------|
| Python | 3.9 + |
| Google Chrome | 最新稳定版 |

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

将 `.env.example` 复制为 `.env`，然后填写各项配置：

```bash
copy .env.example .env
```

`.env` 文件各字段说明：

| 字段 | 说明 |
|------|------|
| `LINKEDIN_EMAIL` | 用于登录 LinkedIn 的账号（建议使用小号） |
| `LINKEDIN_PASSWORD` | 对应密码 |
| `TARGET_PROFILE_URL` | 要监控的用户主页链接，例如 `https://www.linkedin.com/in/someone/` |
| `NOTIFY_EMAIL_SENDER` | 发件用的 Gmail 地址 |
| `NOTIFY_EMAIL_APP_PASSWORD` | Gmail **应用专用密码**（见下方说明） |
| `NOTIFY_EMAIL_RECIPIENT` | 接收通知的邮箱地址 |
| `CHECK_INTERVAL_MINUTES` | 检查间隔（分钟），默认 `60` |
| `HEADLESS` | 是否后台运行浏览器，`true`/`false`，默认 `false` |

### 3. 获取 Gmail 应用专用密码

1. 访问 [Google 账号安全设置](https://myaccount.google.com/security)
2. 确保已开启**两步验证**
3. 搜索"应用专用密码" → 选择应用类型"邮件" → 生成
4. 将生成的 16 位密码填入 `NOTIFY_EMAIL_APP_PASSWORD`

### 4. 运行

```bash
python main.py
```

**首次运行**：程序会将当前已有帖子全部标记为"已读"，不发送通知。  
后续每次检查时，若检测到新帖子，将立即发送邮件。

---

## 文件结构

```
linkedInInformationCollection/
├── main.py           # 程序入口，定时调度
├── scraper.py        # LinkedIn 浏览器登录与帖子抓取
├── notifier.py       # Gmail 邮件发送
├── storage.py        # 已见帖子 ID 持久化
├── config.py         # 配置读取与校验
├── requirements.txt  # Python 依赖
├── .env.example      # 配置模板
├── .env              # 你的实际配置（不要提交到 Git！）
├── seen_posts.json   # 运行后自动生成，记录已见帖子
└── tracker.log       # 运行日志
```

---

## 注意事项

1. **LinkedIn 使用条款**：自动化抓取可能违反 LinkedIn 服务条款，请自行评估风险，建议仅用于个人学习目的。
2. **隐私设置**：若目标用户将动态设为仅好友可见，需要你的监控账号与该用户是好友。
3. **安全验证**：LinkedIn 有时会触发滑块或短信验证，设置 `HEADLESS=false` 可手动通过验证。
4. **运行频率**：过于频繁的请求（小于 30 分钟）可能增加账号被限制的风险。
5. **`.env` 文件安全**：`.env` 包含账号和密码，切勿上传到任何公开代码仓库。

---

## 常见问题

**Q: 提示"登录失败"？**  
A: 确认账号密码正确；首次在新环境登录 LinkedIn 可能触发验证，建议先设置 `HEADLESS=false` 手动完成。

**Q: 抓取到 0 条帖子？**  
A: 该用户可能设置了隐私限制；或 LinkedIn 页面结构已更新，可提 issue 反馈。

**Q: 邮件发送失败（535 错误）？**  
A: 确认使用的是 Gmail **应用专用密码**，而不是登录密码。
