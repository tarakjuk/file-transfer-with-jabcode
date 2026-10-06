# Edge(Windows 기본 설치) + 가짜 카메라 종단 테스트. pip install playwright (브라우저 다운로드 불필요)
# python e2e_edge.py <y4m 절대경로> <원본파일> <타임아웃초> <URL> <태그>   (PYTHONIOENCODING=utf-8 권장)
# 스크린샷/다운로드는 임시 폴더에 저장
import asyncio, os, sys, tempfile, time
from playwright.async_api import async_playwright

Y4M, EXPECT, TIMEOUT, URL, TAG = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
HERE = tempfile.gettempdir()
JS = """[document.getElementById('status').textContent, document.getElementById('stRank').textContent,
 document.getElementById('chipFps').textContent, document.getElementById('chipHit').textContent,
 (document.getElementById('chipRoi')||{}).textContent||'', (document.getElementById('stRate')||{}).textContent||''].join(' | ')"""


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel='msedge', args=[
            '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
            f'--use-file-for-fake-video-capture={Y4M}'])
        ctx = await b.new_context(accept_downloads=True, viewport={'width': 412, 'height': 900}, is_mobile=True, has_touch=True)
        await ctx.grant_permissions(['camera'])
        page = await ctx.new_page()
        page.on('pageerror', lambda e: print('[pageerror]', e))
        page.on('console', lambda m: print('[console]', m.text) if m.type == 'error' else None)
        await page.goto(URL)
        await page.wait_for_timeout(1500)
        await page.click('#btnStart')
        t0 = time.time()
        dl_task = asyncio.ensure_future(page.wait_for_event('download', timeout=TIMEOUT * 1000))
        last, shot = '', False
        while not dl_task.done() and time.time() - t0 < TIMEOUT:
            await asyncio.sleep(2)
            s = await page.evaluate(JS)
            if s != last:
                print(f'{time.time() - t0:5.1f}s', s); last = s
            if not shot and time.time() - t0 > 8:
                await page.screenshot(path=os.path.join(HERE, f'shot_{TAG}_mid.png')); shot = True
        if not dl_task.done():
            print('TIMEOUT', TAG, last)
            dl_task.cancel()
            await b.close(); return
        dl = await dl_task
        path = os.path.join(HERE, 'dl_' + TAG + '_' + dl.suggested_filename)
        await dl.save_as(path)
        print('DOWNLOADED', dl.suggested_filename, os.path.getsize(path), 'in %.1fs' % (time.time() - t0))
        await page.wait_for_timeout(700)
        print('FINAL', await page.evaluate(JS))
        print('CARD', await page.evaluate("document.querySelector('.file .info') && document.querySelector('.file .info').textContent"))
        await page.screenshot(path=os.path.join(HERE, f'shot_{TAG}_done.png'))
        print('MATCH', open(path, 'rb').read() == open(EXPECT, 'rb').read())
        await b.close()

asyncio.run(main())
