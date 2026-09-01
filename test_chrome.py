import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright
chrome_exe = Path(r'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe')
extract_dir = Path(tempfile.mkdtemp(prefix='chrome_test_'))
print(f'Profile: {extract_dir}')
with sync_playwright() as p:
    try:
        ctx = p.chromium.launch_persistent_context(user_data_dir=str(extract_dir),executable_path=str(chrome_exe),headless=True,args=['--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'])
        print(f'ctx type: {type(ctx).__name__}')
        page = ctx.new_page()
        page.goto('https://example.com')
        print(f'OK url: {page.url}')
        ctx.close()
    except Exception as e:
        print(f'FAIL: {e}')
