#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百度文库 VIP 下载器 - 命令行版。

用法:
    python wenku_download.py <文档URL> [文档URL2 ...]
    python wenku_download.py urls.txt            # 一行一个链接
"""
import asyncio
import os
import sys

from downloader import download_document, default_logger


async def main(urls: list[str]) -> None:
    results = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        res = await download_document(url, log=default_logger)
        results.append((url, res))
    print("\n========== 汇总 ==========")
    for u, r in results:
        print(f"{u}\n   {'成功 -> ' + r.get('file', '') if r.get('success') else '失败: ' + r.get('error', '?')}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    expanded = []
    for a in args:
        if a.endswith(".txt") and os.path.isfile(a):
            expanded += [ln.strip() for ln in open(a, encoding="utf-8") if ln.strip()]
        else:
            expanded.append(a)
    asyncio.run(main(expanded))
