#!/usr/bin/env python3
"""Playwright拿抖音视频直链+章节(aweme_id=7682625444235054390)"""
from playwright.sync_api import sync_playwright

AWEME_ID = "7682625444235054390"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"

p = sync_playwright().start()
browser = p.chromium.launch(headless=True)
ctx = browser.new_context(user_agent=UA, viewport={"width": 390, "height": 844})
page = ctx.new_page()
try:
    page.goto(f"https://www.douyin.com/video/{AWEME_ID}", timeout=45000, wait_until="domcontentloaded")
    page.wait_for_timeout(9000)
    title = page.evaluate('() => document.title')
    print("TITLE:", title[:130])
    chapters = page.evaluate('''() => {
        const body = document.body.innerText;
        const i = body.indexOf("章节要点");
        return i > -1 ? body.slice(i, i+900) : null;
    }''')
    print("CHAPTERS:", chapters if chapters else "无")
    vids = page.evaluate('() => JSON.stringify([...document.querySelectorAll("video")].map(v => v.src || v.currentSrc || v.querySelector("source")?.src).filter(Boolean))')
    print("VIDS:", vids[:400])
finally:
    browser.close()
    p.stop()
