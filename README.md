# 衡选 · Hengxuan

一个可自行部署的多人协作评分与选拔工具，适用于社团招新、部门面试、校队选拔、小型比赛和奖项评审。

候选人、评分任务、进度和结果汇总在同一个评审房间中。后端使用 Flask 和 SQLite，前端使用 Jinja 模板与原生 JavaScript，无需 Node.js 构建。

## 功能

- 账号注册、登录、密码修改、恢复码和密码找回。
- 独立评审房间，通过房间码与密码或邀请链接加入。
- 房主、协管员、普通成员权限；支持不参与评分的观察成员。
- 候选人新增、修改、归档和恢复。
- 1–5、1–10、0–100 及自定义整数分制，点选或提交后保存，支持修改与撤销。
- 个人进度、整体进度、评委漏评检查和评分页定期同步。
- 平均分排名、同分并列、结果可见范围和 CSV 结果导出。
- 用户中心、成员移出通知、帮助中心和上下文帮助。
- SQLite WAL、事务保护，以及旧版数据库兼容迁移。

当前评分模型为单项整数评分；暂不支持多指标加权评分、候选人批量导入或原生 Excel 导出。CSV 可用 Excel/WPS 打开。

## 快速开始

建议使用 Python 3.12。首次启动会自动初始化数据库，不包含预置账号或评审数据。

```bash
git clone https://github.com/schrieeeffer/hengxuan.git
cd hengxuan
python -m venv .venv
```

Linux / macOS：

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m flask --app wsgi run --host 127.0.0.1 --port 8000
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m flask --app wsgi run --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>，注册账号并保存一次性展示的恢复码，然后创建房间、添加候选人、邀请评委并开始评分。开发服务器仅用于本地调试。

## 配置

应用读取进程环境变量，不会自动加载 `.env` 文件。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | 项目目录中的 `data/scores.db` | SQLite 文件位置；父目录自动创建 |
| `SECRET_KEY` | 自动生成并保存到数据库目录的 `.secret_key` | Flask 会话签名密钥；同一部署需要保持稳定 |
| `PLATFORM_ADMIN_USER_IDS` | 空 | 可签发账号临时重置码的已注册用户 ID，以逗号分隔；与房主身份独立 |

例如在 PowerShell 中设置数据库位置：

```powershell
$env:DATABASE_PATH = 'D:\hengxuan-data\scores.db'
```

密码找回使用恢复码或平台协助员签发的临时重置码，不依赖手机号或邮箱。不要通过可自行注册的账号名授予平台权限。

## 部署

### Docker

```bash
docker build -t hengxuan .
docker run -d --name hengxuan --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v hengxuan-data:/app/data \
  hengxuan
```

容器以非 root 用户运行，数据库和自动生成的会话密钥保存在持久化卷中。健康检查路径为 `/healthz`。

### Linux / systemd

可使用 [deploy/hengxuan.service](deploy/hengxuan.service) 作为模板：

1. 创建专用服务用户 `hengxuan`，将代码和虚拟环境放到 `/opt/hengxuan`，安装运行依赖。
2. 将服务文件安装到 `/etc/systemd/system/`，按实际目录调整配置。systemd 会为服务创建 `/var/lib/hengxuan` 数据目录。
3. 可在 `/etc/hengxuan.env` 设置环境变量，限制该文件的读取权限。
4. 执行 `sudo systemctl daemon-reload` 和 `sudo systemctl enable --now hengxuan`。

服务默认只监听 `127.0.0.1:8000`。公网部署应配置 HTTPS 反向代理。应用按一层代理处理转发头，请由可信代理覆盖这些头，并限制后端仅能被代理访问。

当前请求频率限制保存在进程内，部署示例使用一个 Gunicorn worker、多线程。多进程或多实例部署前需另行考虑共享限流与 SQLite 写入并发。

### 数据与升级

数据库内包含账号凭据的哈希、成员关系、候选人、评分和操作记录，应与会话密钥一同妥善保管。升级前请使用 SQLite 备份接口或停服后完整备份数据库，保留可回滚的程序版本。不要只在运行时复制 WAL 模式下的主数据库文件。

启动时会执行兼容迁移；当前模式版本为 4。旧单房间数据导入时使用通用名称“历史评审房间”，已存在的房间和评分继续保留。

## 权限与结果口径

房主拥有协管员的管理能力，并可管理成员身份、邀请、加入控制和房主转让。协管员可管理候选人、房间状态和常规评审设置；普通成员按分配任务评分。

房主和协管员始终可见结果。其他成员根据房间配置，在评分过程中、完成本人任务后或房间结束后查看结果；CSV 导出遵守同一规则。无评分权限的观察成员在“完成本人评分后可见”模式下需等房间结束。

归档候选人会将其移出当前结果，但保留历史评分。移出成员会免除其待评任务，已提交评分默认继续计入；房主可另行作废该成员的历史评分。平均分按当前有效评分计算，同分并列，未完成结果仍可能变化。完整结果页需要手动刷新。

“暂停新成员加入”限制房间码与密码入口；有效邀请链接及房主按账号添加仍可使用。更多操作说明见登录后的帮助中心。

## 开发与测试

激活虚拟环境后：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
```

测试使用临时数据库和虚构账号，不需要真实数据。GitHub Actions 在 Windows 和 Linux 上运行测试及静态检查，并验证容器可启动。

```text
app.py                 路由、权限、评分与数据库迁移
help_content.py        帮助内容
wsgi.py                服务入口
templates/             Jinja 页面
static/                样式、脚本与品牌 SVG
tests/                 功能及回归测试
deploy/                通用部署示例
```

提交改动前运行测试；修改数据库结构时提供兼容迁移，并保持既有评分与排名数据完整。提交问题时使用虚构账号与候选人复现，不要附上真实数据库、密码、恢复码或个人信息。

## 许可证

[MIT](LICENSE) · Copyright © 2026 schrieeeffer
