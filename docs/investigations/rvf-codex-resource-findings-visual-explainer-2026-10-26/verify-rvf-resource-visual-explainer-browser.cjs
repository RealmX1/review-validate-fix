#!/usr/bin/env node
// 验证两种交付页面的离线交互、数据一致性和窄屏布局；只启动自己的浏览器。
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const playwrightModulePath = process.argv[2];
const sessionPagePath = process.argv[3];
if (!playwrightModulePath || !sessionPagePath) throw new Error('需要 Playwright 模块路径和会话图解页面路径。');
const { chromium } = require(playwrightModulePath);
const investigationDirectory = __dirname;
const standalonePagePath = path.join(investigationDirectory, 'rvf-resource-findings-and-workflow-proposals-offline.html');
const historicalResourceSummary = JSON.parse(fs.readFileSync(path.join(investigationDirectory, '../rvf-codex-stage-duration-token-share-2026-10-18/rvf-codex-resource-summary.json'), 'utf8'));

(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const verificationChecks = [];
  const browserErrors = [];
  const remoteRequests = [];
  const assertObserved = (description, condition) => {
    verificationChecks.push({ description, passed: Boolean(condition) });
    if (!condition) throw new Error(description);
  };
  try {
    const context = await browser.newContext({ viewport: { width: 1360, height: 1050 } });
    await context.setOffline(true);
    const page = await context.newPage();
    page.on('pageerror', error => browserErrors.push(String(error)));
    page.on('request', request => { if (/^https?:/.test(request.url())) remoteRequests.push(request.url()); });
    for (const [pageName, pagePath] of [['独立离线图解', standalonePagePath], ['会话可视化页', sessionPagePath]]) {
      await page.goto(pathToFileURL(pagePath).href, { waitUntil: 'domcontentloaded' });
      const explainer = page.locator('#rvf-resource-findings-explainer');
      await explainer.locator('#rvf-hundred-token-dots .rvf-token-dot').first().waitFor();
      assertObserved(`${pageName}：积木数等于 100`, await explainer.locator('.rvf-token-dot').count() === 100);
      assertObserved(`${pageName}：默认比例为 11.88%`, (await explainer.locator('#rvf-analysis-share-value').textContent()) === '11.88%');
      await explainer.locator('[data-token-metric="total_tokens"]').click();
      assertObserved(`${pageName}：全部 Token 切换为 3.47%`, (await explainer.locator('#rvf-analysis-share-value').textContent()) === '3.47%');
      assertObserved(`${pageName}：切换后积木仍为 100`, await explainer.locator('.rvf-token-dot').count() === 100);
      await explainer.locator('[data-token-metric="noncached_input_plus_output_tokens"]').click();
      assertObserved(`${pageName}：新读写复盘约 12 份`, await explainer.locator('.rvf-token-dot[data-group="analysis"]').count() === 12);
      assertObserved(`${pageName}：历史归档为 18 份与 1 份通过`, await explainer.locator('.rvf-document').count() === 18 && await explainer.locator('.rvf-document[data-archived="true"]').count() === 1);
      assertObserved(`${pageName}：提案卡片三张`, await explainer.locator('.rvf-proposal').count() === 3);
      const analysisTimeWidth = await explainer.locator('.rvf-time-bar span').last().evaluate(element => parseFloat(element.style.width));
      assertObserved(`${pageName}：时长比例直接来自历史统计`, Math.abs(analysisTimeWidth-historicalResourceSummary.primary_7_days.analyze_elapsed_share*100)<0.00001);
      assertObserved(`${pageName}：标注了新版修复与历史窗口`, (await explainer.textContent()).includes('新版已补这道检查') && (await explainer.textContent()).includes('截止 2026-10-18'));
      assertObserved(`${pageName}：区分旧缓存和实际声明的 Hook 入口`, (await explainer.textContent()).includes('旧缓存插件的三个 Hook 入口在配置中已禁用'));
      for (const viewportWidth of [1360, 390]) {
        await page.setViewportSize({ width: viewportWidth, height: 1050 });
        const overflow = await explainer.evaluate(element => ({ clientWidth: element.clientWidth, scrollWidth: element.scrollWidth, rectWidth: element.getBoundingClientRect().width }));
        assertObserved(`${pageName}：${viewportWidth}px 无图解横向溢出`, overflow.scrollWidth <= overflow.clientWidth + 1 && overflow.rectWidth <= viewportWidth);
      }
      await page.setViewportSize({ width: 1360, height: 1050 });
      if (pageName === '独立离线图解') {
        await explainer.locator('#rvf-historical-cost-panel').screenshot({ path: path.join(investigationDirectory, 'rvf-resource-findings-desktop-browser-verification.png') });
        await explainer.locator('#rvf-proposed-workflow-panel').screenshot({ path: path.join(investigationDirectory, 'rvf-workflow-proposals-desktop-browser-verification.png') });
        await page.setViewportSize({ width: 390, height: 1050 });
        await explainer.locator('#rvf-historical-cost-panel').screenshot({ path: path.join(investigationDirectory, 'rvf-resource-findings-mobile-browser-verification.png') });
      }
    }
    assertObserved('页面脚本无运行错误', browserErrors.length === 0);
    const standaloneHtml = fs.readFileSync(standalonePagePath, 'utf8');
    assertObserved('独立页面无远程脚本或外部资产依赖', !/(?:src|href)\s*=\s*["']https?:/i.test(standaloneHtml));
    const receipt = { verifiedAtUtc: new Date().toISOString(), browser: '隔离 Chrome / Playwright', offline: true,
      initialCliNavigationFailure: 'CLI 默认禁止 file://；使用本地 Playwright API 验证相同离线文件，未修改 CLI 或生产配置。',
      status: 'passed', checks: verificationChecks, browserErrors, remoteRequests, standalonePagePath, sessionPagePath };
    fs.writeFileSync(path.join(investigationDirectory, 'rvf-visual-explainer-browser-verification.json'), JSON.stringify(receipt, null, 2) + '\n');
    console.log(`${verificationChecks.length} 项离线浏览器核验通过，已保存桌面与手机截图。`);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
