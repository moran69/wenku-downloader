#!/usr/bin/env bash
# 甲骨文云服务器（Ubuntu/Debian）一键部署脚本
# 用法：在服务器上 bash setup.sh
set -e

echo "==== 1. 安装系统依赖 ===="
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv fonts-wqy-zenhei \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2

echo "==== 2. 创建虚拟环境 ===="
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate

echo "==== 3. 安装 Python 依赖 ===="
pip install --upgrade pip
pip install -r requirements.txt

echo "==== 4. 安装 Playwright 浏览器内核 ===="
playwright install chromium
# ARM 架构（Ampere A1）需要额外装系统库
playwright install-deps chromium || true

echo "==== 5. 校验 ===="
python3 -c "import playwright; print('playwright OK')"
echo ""
echo "部署完成！用法："
echo "  source .venv/bin/activate"
echo "  python wenku_download.py <文档URL>"
echo "下载的文件在 ./downloads 目录。"
