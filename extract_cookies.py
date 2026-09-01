import json
import sys
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

CHROME_PATHS = [Path(r'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe'), Path(r'C:/Program Files/Google/Chrome/Application/chrome.exe')]
COOKIE_TARGET = Path(r'C:/Users/bmurt/AppData/Local/Temp/youtube_cookies_autofetch.txt')

def extract_cookies() -> dict:
    chrome_exe = next((p for p in CHROME_PATHS if p.exists()), None)
    if not chrome_exe: return {'ok': False, 'error': 'Chrome not found'}
    extract_dir = Path(tempfile.mkdtemp(prefix='chrome_yt_'))
    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(user_data_dir=str(extract_dir),executable_path=str(chrome_exe),headless=True,args=['--headless=new','--no-sandbox','--disable-dev-shm-usage','--disable-gpu'])
        except Exception as e: return {'ok': False, 'error': f'launch failed: {e}'}
        try:
            page = ctx.new_page()
            page.goto('https://www.youtube.com', wait_until='domcontentloaded', timeout=30000)
            cookies = ctx.cookies()
            yt = [c for c in cookies if 'youtube' in c.get('domain','') or 'google' in c.get('domain','')]
            if not yt: return {'ok': False, 'error': 'no YouTube/Google cookies. Sign into YouTube in Chrome first, then re-run.'}
            netscape = ['# Netscape HTTP Cookie File']
            for c in cookies:
                domain=c.get('domain',''); host_only='+' if c.get('sameSite') in ('Lax','Strict') else 'FALSE'
                secure='TRUE' if c.get('secure') else 'FALSE'; expires=int(c.get('expires',0))
                name=c.get('name',''); value=c.get('value',''); path=c.get('path','/')
                netscape.append('\t'.join([domain,host_only,str(domain.startswith('.')).upper(),secure,str(expires),name,value,path]))
            COOKIE_TARGET.write_text('
'.join(netscape)+'
', encoding='utf-8')
            return {'ok': True, 'path': str(COOKIE_TARGET), 'count': len(cookies), 'yt_count': len(yt), 'size': COOKIE_TARGET.stat().st_size}
        except Exception as e: return {'ok': False, 'error': f'navigate failed: {e}'}
        finally:
            try: ctx.close()
            except: pass

if __name__ == '__main__':
    result = extract_cookies()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result['ok'] else 1)
