"""Drive gmail OAuth via Playwright persistent context (user's Chrome profile)."""
import os
import re
import time
import urllib.parse
from pathlib import Path
from playwright.sync_api import sync_playwright

with open(r"C:\Dev\justdumpit\gen_url.out") as f:
    text = f.read()
m = re.search(r"URL:\s*(\S+)", text)
url = m.group(1)
m = re.search(r"CODE_VERIFIER \(.*?:\s*\n(.+)", text)
code_verifier = m.group(1).strip()

print(f"URL: {url[:120]}...")

chrome_profile = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"
chrome_exe = Path(r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe")
print(f"Profile exists: {chrome_profile.exists()}")

state = {"redirect_url": None, "code": None}

with sync_playwright() as p:
    launch_kwargs = {
        "user_data_dir": str(chrome_profile),
        "executable_path": str(chrome_exe),
        "headless": True,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    }
    print(f"Launching persistent context with user_data_dir={chrome_profile}")
    try:
        context = p.chromium.launch_persistent_context(**launch_kwargs)
    except Exception as e:
        print(f"launch_persistent_context failed: {e}")
        print("Falling back to non-persistent bundled chromium")
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
    page = context.new_page()

    def on_request(request):
        if "127.0.0.1:18099" in request.url or "localhost:18099" in request.url:
            state["redirect_url"] = request.url
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(request.url).query)
            if "code" in qs:
                state["code"] = qs["code"][0]
            request.abort()

    page.on("request", on_request)
    print("Navigating...")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"goto exception (expected for abort): {e}")

    for i in range(60):
        time.sleep(1)
        cur = page.url
        if "127.0.0.1:18099" in cur or "localhost:18099" in cur:
            print(f"Got redirect after {i}s: {cur[:120]}")
            break
        if i % 5 == 0:
            print(f"  waiting ({i}s) url={cur[:80]} title={page.title()[:50]}")
    else:
        print(f"Timeout. Final url: {page.url[:120]}")
        print(f"Title: {page.title()}")

    if not state["code"]:
        cur = page.url
        if "code=" in cur:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(cur).query)
            if "code" in qs:
                state["code"] = qs["code"][0]
                state["redirect_url"] = cur

    try:
        context.close()
    except Exception:
        pass

print(f"\nRedirect URL: {(state['redirect_url'] or '')[:120]}")
print(f"Code: {(state['code'] or '')[:50]}...")

if state["code"]:
    with open(r"C:\Users\bmurt\AppData\Local\Temp\gmail_code.txt", "w") as f:
        f.write(state["code"])
    print(f"Saved code to file.")
else:
    print("No code captured.")
