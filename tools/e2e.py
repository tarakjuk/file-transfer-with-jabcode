import asyncio, sys, os, time, subprocess
from playwright.async_api import async_playwright
Y4M = sys.argv[1]; EXPECT = sys.argv[2]; TIMEOUT = int(sys.argv[3]) if len(sys.argv) > 3 else 180
URL = sys.argv[4] if len(sys.argv) > 4 else 'http://localhost:8765/index.html'
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=None, args=[
            '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
            f'--use-file-for-fake-video-capture={Y4M}'])
        ctx = await b.new_context(accept_downloads=True, viewport={'width': 412, 'height': 900}, is_mobile=True, has_touch=True,
                                  user_agent='Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36')
        await ctx.grant_permissions(['camera'])
        page = await ctx.new_page()
        page.on('console', lambda m: print('[console]', m.text) if m.type in ('error', 'warning') else None)
        page.on('pageerror', lambda e: print('[pageerror]', e))
        await page.goto(URL)
        await page.click('#btnStart')
        t0 = time.time()
        dl_task = asyncio.ensure_future(page.wait_for_event('download', timeout=TIMEOUT * 1000))
        last = ''
        while not dl_task.done() and time.time() - t0 < TIMEOUT:
            await asyncio.sleep(3)
            s = await page.evaluate("[document.getElementById('status').textContent, document.getElementById('stRank').textContent, document.getElementById('chipFps').textContent, document.getElementById('chipHit').textContent].join(' | ')")
            if s != last: print(f'{time.time()-t0:5.1f}s', s); last = s
            if time.time() - t0 > 12 and not os.path.exists('/tmp/shot_mid.png'):
                await page.screenshot(path='/tmp/shot_mid.png')
        dl = await dl_task
        path = '/tmp/dl_' + dl.suggested_filename
        await dl.save_as(path)
        print('DOWNLOADED', dl.suggested_filename, os.path.getsize(path), 'in %.1fs' % (time.time() - t0))
        await asyncio.sleep(0.5)
        await page.screenshot(path='/tmp/shot_done.png')
        print('MATCH', open(path, 'rb').read() == open(EXPECT, 'rb').read())
        await b.close()
asyncio.run(main())
