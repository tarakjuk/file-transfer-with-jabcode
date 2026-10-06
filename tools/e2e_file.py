import asyncio, sys, os, time
from playwright.async_api import async_playwright
VID, EXPECT = sys.argv[1], sys.argv[2]
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(accept_downloads=True, viewport={'width': 1280, 'height': 800})
        page = await ctx.new_page()
        page.on('pageerror', lambda e: print('[pageerror]', e))
        await page.goto('http://localhost:8765/index.html')
        await page.wait_for_timeout(800)
        t0 = time.time()
        async with page.expect_download(timeout=240000) as dli:
            await page.set_input_files('#fileInput', VID)
        dl = await dli.value
        path = '/tmp/dlf_' + dl.suggested_filename; await dl.save_as(path)
        print('DOWNLOADED', dl.suggested_filename, 'in %.1fs' % (time.time() - t0), 'MATCH', open(path,'rb').read() == open(EXPECT,'rb').read())
        await page.wait_for_timeout(500)
        print(await page.evaluate("document.getElementById('status').textContent")); await page.screenshot(path='/tmp/shot_desktop.png')
        await b.close()
asyncio.run(main())
