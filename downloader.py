# -*- coding: utf-8 -*-
"""百度文库下载核心模块。

策略：
  1. 优先尝试点击下载按钮获取原文件（适用于 VIP 免费文档）
  2. 若下载按钮不存在或被拦截，自动切换为「文字提取 + 页面截图」模式：
     - 从文档的 JSONP 渲染数据提取纯文字
     - 对每页截图保存
     - 合并为 PDF
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from playwright.async_api import Download, TimeoutError as PWTimeout, async_playwright

BASE_DIR = Path(__file__).resolve().parent
COOKIE_FILE = BASE_DIR / "cookies.txt"
OUTPUT_DIR = BASE_DIR / "downloads"
OUTPUT_DIR.mkdir(exist_ok=True)

NAV_TIMEOUT = 60_000
DOWNLOAD_TIMEOUT = 120_000

KEY_COOKIE_NAMES = {
    "BDUSS": "登录令牌（最关键）",
    "BAIDUID": "浏览器标识",
    "BDUSS_BFESS": "跨域登录令牌",
    "BIDUPSID": "用户标识",
    "PTOKEN": "通行令牌",
    "STOKEN": "安全令牌",
}


def default_logger(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_cookie_string(raw: str) -> list[dict]:
    cookies: list[dict] = []
    for part in raw.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        cookies.append({"name": name, "value": value, "domain": ".baidu.com", "path": "/"})
    return cookies


def load_cookie_text() -> str:
    if not COOKIE_FILE.exists():
        raise FileNotFoundError(f"找不到 Cookie 文件: {COOKIE_FILE}")
    raw = COOKIE_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError("cookies.txt 是空的")
    return raw


def load_cookies() -> list[dict]:
    return parse_cookie_string(load_cookie_text())


def cookie_summary() -> dict:
    info = {"total": 0, "keys": {}, "empty": False, "error": None}
    try:
        raw = load_cookie_text()
    except Exception as e:
        info["error"] = str(e)
        info["empty"] = True
        return info
    cookies = parse_cookie_string(raw)
    info["total"] = len(cookies)
    names = {c["name"] for c in cookies}
    info["keys"] = {
        name: {"label": label, "present": name in names}
        for name, label in KEY_COOKIE_NAMES.items()
    }
    info["ready"] = "BDUSS" in names
    return info


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", name).strip()
    return name[:120] if name else "wenku_doc"


async def _detect_login(page) -> bool:
    try:
        html = await page.content()
    except Exception:
        return False
    if "passport.baidu.com/v2/?login" in html and "退出" not in html:
        return False
    return True


def _extract_text_from_jsonp(body: str) -> str:
    """从文库的 JSONP 渲染数据中提取纯文字。"""
    m = re.match(r"^\w+\((.+)\)$", body.strip(), re.S)
    if not m:
        return ""
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return ""

    parts: list[str] = []
    for item in data.get("body", []):
        # body 里每个元素有 "c" (content/text) 字段
        text = item.get("c") or item.get("t") or ""
        if isinstance(text, str) and text.strip():
            # 过滤渲染标记
            if text in ("word", "\n") or text.startswith("9f54"):
                continue
            parts.append(text)
    return "".join(parts)


async def download_document(url: str, log=None, output_dir: Path | None = None) -> dict:
    """下载文档。返回 {success, file?, files?, error?, method?, pages?, total_pages?}。"""
    log = log or (lambda m: None)
    out_dir = Path(output_dir) if output_dir else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"开始处理: {url}")
    cookies = load_cookies()
    log(f"已加载 {len(cookies)} 条 Cookie")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
            accept_downloads=True,
            locale="zh-CN",
        )
        init = await context.new_page()
        await init.goto("https://www.baidu.com/", timeout=NAV_TIMEOUT)
        await context.add_cookies(cookies)
        await init.close()
        log("Cookie 注入完成")

        page = await context.new_page()
        try:
            await page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            await asyncio.sleep(3)

            if not await _detect_login(page):
                log("Cookie 已失效")
                return {"success": False, "error": "未登录或 Cookie 已失效，请更新 Cookie"}

            # 提取文档基本信息
            doc_info = await page.evaluate("""() => {
                try {
                    const ri = window.pageData.readerInfo;
                    const di = window.pageData.viewBiz.docInfo;
                    return {
                        title: di.title || window.pageData.title.replace(/ - 百度文库$/, ''),
                        docId: di.docId,
                        totalPages: ri.page,
                        showPage: ri.showPage,
                        freePage: ri.freePage,
                        isVipFree: di.isVipFree,
                        jsonUrls: (ri.htmlUrls.json || []).map(x => x.pageLoadUrl),
                        pageWidth: ri.pageInfo.pageWidth,
                        pageHeight: ri.pageInfo.pageHeight,
                    };
                } catch(e) { return null; }
            }""")

            if not doc_info:
                return {"success": False, "error": "无法解析文档信息，可能不是标准文库页面"}

            title = doc_info["title"]
            total = doc_info.get("totalPages") or 0
            log(f"文档: {title}  总页: {total}  VIP免费: {doc_info.get('isVipFree')}")

            # ========== 策略1: 尝试原文件下载 ==========
            log("尝试原文件下载…")
            # 新版文库页面的真实下载按钮选择器（2025版）
            # 优先"重新下载"=单篇原文件下载；其次"批量下载"=文档集ZIP打包
            download_selectors = [
                "div.btn-normal.optimization-experiment",  # 重新下载（单篇）
                ".btn-normal:has-text('重新下载')",
                ".btn-batch-down",                          # 批量下载（文档集ZIP）
                ".btn-download", "a.download-btn",
                "#fileDownloadBtn",
            ]
            clicked = False
            for sel in download_selectors:
                try:
                    el = await page.wait_for_selector(sel, timeout=3_000)
                    if el and await el.is_visible():
                        log(f"找到下载按钮: {sel}")
                        try:
                            async with page.expect_download(timeout=30_000) as dl_info:
                                await el.scroll_into_view_if_needed(timeout=3_000)
                                await asyncio.sleep(0.3)
                                await el.click()
                                clicked = True
                                log(f"已点击: {sel}，等待下载…")
                            download = await dl_info.value
                            suggested = download.suggested_filename or "wenku_file"
                            dest = out_dir / safe_filename(suggested)
                            await download.save_as(str(dest))
                            sz = dest.stat().st_size
                            log(f"✅ 原文件下载成功: {dest.name} ({sz/1024:.0f} KB)")
                            return {"success": True, "file": dest.name, "method": "original",
                                    "pages": total, "total_pages": total}
                        except PWTimeout:
                            log(f"点击 {sel} 后未触发下载，继续尝试下一个按钮")
                            clicked = False
                            continue
                except (PWTimeout, Exception):
                    continue

            if not clicked:
                log("未找到可用的下载按钮")

            # ========== 策略2: 文字提取 + 截图 ==========
            log("启动文字提取 + 截图模式")

            # 隐藏干扰元素
            await page.add_style_tag(content="""
                .reader-topbar, .header-wrapper, .bottom-vip-notice,
                .text-bottom, .pc-client-wrap, [class*="recommend"],
                [class*="sidebar"], [class*="footer"], [class*="ad-"],
                [class*="dialog"], [class*="popover"], [class*="popup"],
                [class*="modal"], #doc-related-search, #doc-bottom-banner,
                .copy-limit-dialog-v2, .retain-dialog-wrap, .detain-dialog-wrap,
                .tip-popover, .vip-entry-outer, .send-to-phone-pop-container,
                .client-modal {
                    display: none !important;
                }
                .reader-wrap, .reader-container, .creader-reader, .creader-root {
                    margin: 0 !important; padding: 0 !important;
                }
                body { margin: 0 !important; background: white !important; }
            """)

            # 2a. 从 JSONP 提取文字
            all_text = ""
            json_urls = doc_info.get("jsonUrls") or []
            if json_urls:
                log(f"获取 {len(json_urls)} 批渲染数据…")
                for ju in json_urls:
                    try:
                        r = await context.request.get(ju, timeout=15_000)
                        body = await r.text()
                        text = _extract_text_from_jsonp(body)
                        all_text += text + "\n"
                    except Exception as e:
                        log(f"  渲染数据获取失败: {e}")
                all_text = all_text.replace("word", "").strip()
                log(f"提取文字: {len(all_text)} 字符")

            # 2b. 逐页截图
            screenshots = []
            for target in range(1, total + 1):
                canvas_id = f"original-creader-canvas-{target}"
                canvas = await page.query_selector(f"#{canvas_id}")
                if canvas and await canvas.is_visible():
                    shot = out_dir / f"_screenshot_{target}.png"
                    await canvas.screenshot(path=str(shot))
                    if shot.stat().st_size > 1000:
                        screenshots.append((target, shot))
                        log(f"  截图第 {target} 页 ✓")

            # 清理旧截图文件
            for f in out_dir.glob("_screenshot_*.png"):
                if (f.stem.split("_")[-1]).isdigit():
                    pageno = int(f.stem.split("_")[-1])
                    if pageno > len(screenshots):
                        f.unlink(missing_ok=True)

            log(f"截图完成: {len(screenshots)}/{total} 页")

            if not all_text and not screenshots:
                return {"success": False, "error": "无法提取任何内容（既无文字也无截图）"}

            # 2c. 保存文字
            saved_files = []
            if all_text:
                txt_name = safe_filename(title) + ".txt"
                txt_path = out_dir / txt_name
                txt_path.write_text(all_text, encoding="utf-8")
                saved_files.append(txt_name)
                log(f"文字已保存: {txt_name}")

            # 2d. 截图合并为 PDF
            if screenshots:
                try:
                    from PIL import Image
                    pdf_name = safe_filename(title) + ".pdf"
                    pdf_path = out_dir / pdf_name
                    images = [Image.open(str(s[1])).convert("RGB") for s in screenshots]
                    if len(images) == 1:
                        images[0].save(str(pdf_path))
                    else:
                        first = images[0]
                        first.save(str(pdf_path), save_all=True, append_images=images[1:])
                    saved_files.append(pdf_name)
                    log(f"截图 PDF 已保存: {pdf_name}")
                    # 清理单页截图
                    for _, s in screenshots:
                        s.unlink(missing_ok=True)
                except ImportError:
                    log("Pillow 未安装，截图保存为单独 PNG 文件")
                    for pageno, shot in screenshots:
                        png_name = f"{safe_filename(title)}_第{pageno}页.png"
                        shot.rename(out_dir / png_name)
                        saved_files.append(png_name)

            return {
                "success": True,
                "files": saved_files,
                "method": "extract",
                "pages": len(screenshots) if screenshots else 0,
                "total_pages": total,
                "text_chars": len(all_text),
                "partial": len(screenshots) < total if total else False,
            }

        except Exception as e:
            log(f"异常: {e}")
            return {"success": False, "error": str(e)}
        finally:
            await page.close()
            await context.close()
            await browser.close()


async def check_login_live() -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(locale="zh-CN")
        pg = await ctx.new_page()
        try:
            await pg.goto("https://www.baidu.com/", timeout=NAV_TIMEOUT)
            await ctx.add_cookies(load_cookies())
            await pg.goto("https://wenku.baidu.com/", timeout=45_000, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            html = await pg.content()
            logged_in = any(k in html for k in ["退出", "个人中心", "我的文库", "userName"])
            vip = "VIP" in html or "vip" in html
            forced_login = "passport.baidu.com/v2/?login" in html and "退出" not in html
            return {"logged_in": logged_in and not forced_login, "vip": vip}
        finally:
            await browser.close()
