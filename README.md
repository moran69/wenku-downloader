# 百度文库下载器

部署在云服务器上的百度文库自动下载工具。网页操作、队列下载、Cookie 在线管理，VIP 会员可直接下载原始格式文档（doc/docx/ppt/pdf 等）。

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![Playwright](https://img.shields.io/badge/Playwright-Chromium-green) ![License](https://img.shields.io/badge/License-MIT-yellow)

## 功能

- **网页界面**：粘贴文档链接一键下载，实时日志、任务队列、文件管理
- **原格式下载**：注入会员 Cookie 后自动点击页面下载按钮，保留 doc/docx/ppt/pdf 原始格式
- **降级提取**：若原文件下载不可用，自动切换为「文字提取 + 页面截图合并 PDF」模式
- **Cookie 管理**：网页上直接粘贴更新 Cookie，支持联网检测登录态和关键令牌（BDUSS 等）状态
- **访问保护**：密码登录、Session 持久化、并发下载限制（防止 Chromium 打爆内存）
- **开机自启**：systemd 服务托管，崩溃自动重启

## 部署

适用于任何 Ubuntu/Debian 云服务器（ARM/x86 均可，已在 Oracle Cloud Ampere A1 上验证）。

```bash
# 1. 克隆并进入目录
git clone https://github.com/moran69/wenku-downloader.git
cd wenku-downloader

# 2. 一键部署（装系统依赖 + Python 虚拟环境 + Playwright Chromium）
bash setup.sh

# 3. 放入你的 Cookie（见下文获取方法）
echo "你的百度文库Cookie" > cookies.txt

# 4. 启动
source .venv/bin/activate
python app.py
```

访问 `http://<服务器IP>:18900`。首次启动会自动生成访问密码，打印在日志里，也保存在 `password.txt`。

### systemd 服务（推荐）

```bash
sudo cp wenku-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wenku-web
```

### 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `WENKU_PORT` | `18900` | 网页服务端口 |
| `WENKU_MAX_CONCURRENT` | `2` | 同时下载的最大文档数 |

注意：云服务器还需在平台防火墙/安全组放行对应端口（如 Oracle Cloud 的 VCN Security List）。

## 获取 Cookie

1. 电脑浏览器登录 [百度文库](https://wenku.baidu.com)
2. `F12` 打开开发者工具 → `Network` → 刷新页面
3. 点击任意请求 → `Headers` → 复制 `Cookie` 整条内容
4. 粘贴到网页「Cookie 管理」页保存，或写入 `cookies.txt`

Cookie 中 `BDUSS` 是登录令牌（必需），过期后重新复制即可。

## 使用

```bash
# 网页方式（推荐）
浏览器打开 http://<IP>:18900 → 输入密码 → 粘贴链接 → 下载

# 命令行方式
python wenku_download.py https://wenku.baidu.com/view/xxxx.html
python wenku_download.py urls.txt    # 批量，一行一个链接
```

下载的文件在 `downloads/` 目录，网页上可直接点击下载到本地。

## 项目结构

```
wenku-downloader/
├── app.py               # Flask 网页应用
├── downloader.py        # 核心下载逻辑（Playwright）
├── wenku_download.py    # 命令行入口
├── templates/           # 前端页面
├── setup.sh             # 一键部署脚本
├── wenku-web.service    # systemd 服务文件
├── cookies.txt          # 百度文库 Cookie（自行创建，git 忽略）
├── password.txt         # 访问密码（自动生成，git 忽略）
└── downloads/           # 下载文件存放（git 忽略）
```

## 已知限制

- 百度文库部分文档标记为「单独购买」或受版权保护，不在 VIP 免费范围，无法下载原文件（会自动降级为文字提取）
- 免费账户只能预览文档的部分页面，降级模式下只能提取已预览的内容
- 文库页面结构可能随时调整，若按钮选择器失效需更新 `downloader.py` 中的 `download_selectors`
- 下载文档请遵守版权法规，仅限个人学习使用

## License

MIT
