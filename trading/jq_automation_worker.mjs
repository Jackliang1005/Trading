#!/usr/bin/env node
/**
 * JoinQuant 回测自动化 Worker
 *
 * 用法:
 *   node jq_automation_worker.mjs <strategy_file> [--cookie-file <path>] [--cookie <str>]
 *
 * 环境变量:
 *   JOINQUANT_COOKIE — Cookie: 头字符串 (与 --cookie 二选一)
 *   JOINQUANT_ALGORITHM_ID — 策略 ID (可选, 默认创建新策略)
 *
 * 输出: JSON 到 stdout, 含 { state, metrics, error, debug }
 */

import { chromium } from 'playwright';
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { resolve, dirname, basename } from 'path';
import { Buffer } from 'buffer';

// ─── 参数解析 ────────────────────────────────────────────
const args = process.argv.slice(2);
if (args.length < 1) {
  console.error('Usage: node jq_automation_worker.mjs <strategy_file> [--cookie-file <path>] [--cookie <str>]');
  process.exit(1);
}

const strategyFile = resolve(args[0]);
if (!existsSync(strategyFile)) {
  console.error(`Strategy file not found: ${strategyFile}`);
  process.exit(1);
}
const strategyCode = readFileSync(strategyFile, 'utf-8');

let cookieStr = process.env.JOINQUANT_COOKIE || '';
let cookieFilePath = '';
for (let i = 1; i < args.length; i++) {
  if (args[i] === '--cookie-file' && args[i + 1]) {
    cookieFilePath = resolve(args[++i]);
    if (existsSync(cookieFilePath)) {
      cookieStr = readFileSync(cookieFilePath, 'utf-8').trim();
    }
  } else if (args[i] === '--cookie' && args[i + 1]) {
    cookieStr = args[++i];
  }
}

const algorithmId = process.env.JOINQUANT_ALGORITHM_ID || '';

// Backtest params
const START_TIME = process.env.JQ_START_TIME || '2024-01-01 00:00:00';
const END_TIME = process.env.JQ_END_TIME || '2026-04-30 23:59:59';
const CAPITAL_BASE = process.env.JQ_CAPITAL_BASE || '150000';
const BACKTEST_TIMEOUT_SEC = parseInt(process.env.JQ_TIMEOUT_SEC || '600');

// Output paths
const SESSION_ID = Date.now().toString(36);
const OUTPUT_DIR = resolve('.jq_backtests');
const STATUS_FILE = resolve(OUTPUT_DIR, `${SESSION_ID}.json`);

if (!existsSync(OUTPUT_DIR)) mkdirSync(OUTPUT_DIR, { recursive: true });

// ─── Cookie 解析 ─────────────────────────────────────────
function parseCookies(cookieHeader) {
  if (!cookieHeader) return [];
  return cookieHeader.split(';').map(pair => {
    const [name, ...rest] = pair.trim().split('=');
    return {
      name: name.trim(),
      value: rest.join('=').trim(),
      domain: '.joinquant.com',
      path: '/',
      httpOnly: false,
      secure: true,
      sameSite: 'Lax',
    };
  }).filter(c => c.name && c.value);
}

// ─── 状态写入 ────────────────────────────────────────────
function writeStatus(update) {
  const current = existsSync(STATUS_FILE) ? JSON.parse(readFileSync(STATUS_FILE, 'utf-8')) : {};
  const status = { ...current, ...update, updated_at: new Date().toISOString() };
  writeFileSync(STATUS_FILE, JSON.stringify(status, null, 2));
  return status;
}

// ─── 策略代码预检 ────────────────────────────────────────
function precheckCode(code) {
  if (code.length < 80) return { ok: false, reason: 'code too short' };
  if (!code.includes('def initialize(context)')) return { ok: false, reason: 'missing initialize' };
  if (!code.includes('run_daily') && !code.includes('handle_data') && !code.includes('run_weekly'))
    return { ok: false, reason: 'missing run_daily/handle_data/run_weekly' };
  return { ok: true };
}

// ─── 主流程 ──────────────────────────────────────────────
async function main() {
  writeStatus({ state: 'starting', stage: 'init', session_id: SESSION_ID });

  // 预检
  const precheck = precheckCode(strategyCode);
  if (!precheck.ok) {
    writeStatus({ state: 'failed', error: `Precheck failed: ${precheck.reason}` });
    console.error(`Precheck failed: ${precheck.reason}`);
    process.exit(1);
  }
  console.log(`Strategy code: ${strategyCode.length} chars, precheck OK`);

  // 检查 cookie
  if (!cookieStr) {
    writeStatus({ state: 'failed', error: 'No cookie provided. Use --cookie-file, --cookie, or JOINQUANT_COOKIE env.' });
    console.error('No cookie provided.');
    console.error('To get a cookie:');
    console.error('  1. Open Chrome, go to https://www.joinquant.com, log in');
    console.error('  2. F12 → Application → Cookies → www.joinquant.com');
    console.error('  3. Copy the "Cookie" header value (right-click request → Copy → Copy request headers)');
    console.error('  4. Or paste the cookie string directly with --cookie "key1=val1; key2=val2; ..."');
    process.exit(1);
  }
  console.log(`Cookie: ${cookieStr.length} chars`);

  const cookies = parseCookies(cookieStr);
  if (cookies.length === 0) {
    writeStatus({ state: 'failed', error: 'Failed to parse cookie string' });
    console.error('Failed to parse cookie string');
    process.exit(1);
  }
  console.log(`Parsed ${cookies.length} cookies`);

  // 启动浏览器
  console.log('Launching browser...');
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
  });
  await context.addCookies(cookies);
  const page = await context.newPage();

  try {
    // ─── Step 1: 打开策略编辑页 ───
    writeStatus({ stage: 'open_editor' });
    const editorUrl = algorithmId
      ? `https://www.joinquant.com/algorithm/index/edit?algorithmId=${algorithmId}`
      : 'https://www.joinquant.com/algorithm/index/edit';
    console.log(`Navigating to: ${editorUrl}`);
    await page.goto(editorUrl, { waitUntil: 'networkidle', timeout: 30000 });

    // 检查是否被重定向到登录页
    if (page.url().includes('login') || page.url().includes('passport')) {
      writeStatus({ state: 'failed', error: 'Redirected to login page — cookie may be expired', url: page.url() });
      console.error('Cookie expired or invalid — redirected to login page');
      await page.screenshot({ path: resolve(OUTPUT_DIR, `${SESSION_ID}_login.png`) });
      process.exit(1);
    }
    console.log(`Current URL: ${page.url()}`);

    // 等待 Ace 编辑器加载
    writeStatus({ stage: 'wait_editor' });
    try {
      await page.waitForSelector('#ide-container', { timeout: 15000 });
      const hasAce = await page.evaluate(() => !!window.ace);
      if (!hasAce) {
        console.log('Ace not found, waiting...');
        await page.waitForFunction(() => !!window.ace, { timeout: 10000 });
      }
      console.log('Ace editor ready');
    } catch (e) {
      writeStatus({ state: 'failed', error: 'Ace editor not found', url: page.url() });
      await page.screenshot({ path: resolve(OUTPUT_DIR, `${SESSION_ID}_no_ace.png`) });
      throw new Error('Ace editor not found — may need to create a new algorithm first');
    }

    // ─── Step 2: 关闭弹窗 ───
    await dismissModals(page);
    // 等一会儿让页面稳定
    await page.waitForTimeout(1000);

    // ─── Step 3: 注入策略代码 ───
    writeStatus({ stage: 'inject_code' });
    const byteLength = Buffer.byteLength(strategyCode, 'utf-8');
    console.log(`Code size: ${byteLength} bytes`);

    if (byteLength < 1200) {
      // 小文件: 直接注入
      await page.evaluate((code) => {
        const editor = window.ace.edit('ide-container');
        editor.setValue(code, -1);
      }, strategyCode);
    } else {
      // 大文件: base64 分块注入
      const b64 = Buffer.from(strategyCode, 'utf-8').toString('base64');
      const chunkSize = 1500;
      const chunks = [];
      for (let i = 0; i < b64.length; i += chunkSize) {
        chunks.push(b64.slice(i, i + chunkSize));
      }
      console.log(`Injecting ${chunks.length} chunks...`);

      // Init chunks array
      await page.evaluate(() => { window._b64Chunks = []; });

      // Send chunks
      for (const chunk of chunks) {
        await page.evaluate((c) => { window._b64Chunks.push(c); }, chunk);
      }

      // Decode and set
      await page.evaluate(() => {
        const b64 = window._b64Chunks.join('');
        const binary = atob(b64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
          bytes[i] = binary.charCodeAt(i);
        }
        const decoded = new TextDecoder('utf-8').decode(bytes);
        const editor = window.ace.edit('ide-container');
        editor.setValue(decoded, -1);
        delete window._b64Chunks;
      });
    }
    console.log('Code injected');

    // ─── Step 4: 保存代码 ───
    writeStatus({ stage: 'save_code' });
    await saveCode(page);
    console.log('Code saved');

    // ─── Step 5: 设置回测参数 ───
    writeStatus({ stage: 'set_params' });
    await page.evaluate(({ start, end, capital }) => {
      const setVal = (id, val) => {
        const el = document.getElementById(id);
        if (el) { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }
      };
      setVal('startTime', start);
      setVal('endTime', end);
      setVal('daily_backtest_capital_base_box', capital);
    }, { start: START_TIME, end: END_TIME, capital: CAPITAL_BASE });
    console.log(`Backtest params: ${START_TIME} → ${END_TIME}, capital=${CAPITAL_BASE}`);

    // 再次保存确保参数落盘
    await saveCode(page);
    await page.waitForTimeout(1000);

    // ─── Step 6: 触发回测 ───
    writeStatus({ stage: 'trigger_backtest' });

    // 多重触发: jQuery click + DOM click + 等待导航
    async function triggerBacktest() {
      // 方法1: jQuery click
      const hasJQuery = await page.evaluate(() => !!window.jQuery);
      if (hasJQuery) {
        console.log('Triggering via jQuery...');
        await page.evaluate(() => {
          window.jQuery('#daily-new-backtest-button').click();
        });
      } else {
        console.log('jQuery not available, using DOM click');
      }

      // 方法2: 如果jQuery不存在或不行, 用DOM click
      await page.waitForTimeout(1000);
      try {
        await page.click('#daily-new-backtest-button', { timeout: 3000 });
        console.log('DOM click succeeded');
      } catch (e) {
        console.log('DOM click on #daily-new-backtest-button failed, trying button text...');
        // 方法3: 找含"新建回测"的按钮
        await page.evaluate(() => {
          const buttons = document.querySelectorAll('button, a, .btn');
          for (const btn of buttons) {
            if (btn.innerText && (btn.innerText.includes('新建回测') || btn.innerText.includes('开始回测') || btn.innerText.includes('运行回测'))) {
              btn.click();
              return;
            }
          }
        });
      }
    }

    const beforeUrl = page.url();
    await triggerBacktest();
    console.log('Backtest trigger sent');

    // 等待页面响应: 导航到 detail 页 或 弹窗 或 错误
    await page.waitForTimeout(3000);
    const afterUrl = page.url();
    console.log(`URL after trigger: ${afterUrl}`);

    // 截图看看状态
    await page.screenshot({ path: resolve(OUTPUT_DIR, `${SESSION_ID}_after_trigger.png`) });

    // 处理「继续运行」弹窗 (可能在触发后延迟出现)
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const found = await page.evaluate(() => {
          const modals = document.querySelectorAll('.modal, .ant-modal-wrap, .ant-modal');
          for (const m of modals) {
            const text = m.innerText || m.textContent || '';
            if (text.includes('继续运行')) return true;
          }
          return false;
        });
        if (found) {
          console.log('Found "继续运行" modal');
          await page.evaluate(() => {
            const modals = document.querySelectorAll('.modal, .ant-modal-wrap, .ant-modal');
            for (const m of modals) {
              const text = m.innerText || m.textContent || '';
              if (text.includes('继续运行')) {
                const buttons = m.querySelectorAll('button, .ant-btn');
                for (const btn of buttons) {
                  if ((btn.innerText || btn.textContent || '').includes('继续运行')) {
                    btn.click();
                    return;
                  }
                }
              }
            }
          });
          console.log('Clicked "继续运行"');
          await page.waitForTimeout(2000);
          break;
        }
      } catch (e) { /* retry */ }
      await page.waitForTimeout(2000);
    }

    // 如果还在编辑页且 URL 没变成 backtest/detail，重新触发
    const currentUrl = page.url();
    if (!currentUrl.includes('backtest/detail') && currentUrl === beforeUrl) {
      console.log('Still on editor page — backtest may not have started. Trying alternative trigger...');
      // 尝试直接导航到回测列表
      await page.goto('https://www.joinquant.com/algorithm/backtest/list', { waitUntil: 'networkidle', timeout: 15000 });
      await page.waitForTimeout(2000);
      // 看有没有正在运行的回测
      const hasRunning = await page.evaluate(() => {
        const text = document.body ? document.body.innerText || '' : '';
        return text.includes('运行中') || text.includes('排队中');
      });
      console.log(`Backtest list page — has running: ${hasRunning}`);
    }

    // 关闭可能残留的弹窗
    await dismissModals(page);

    // ─── Step 7: 轮询回测结果 ───
    writeStatus({ stage: 'polling', state: 'running' });
    const startTime = Date.now();
    const timeoutMs = BACKTEST_TIMEOUT_SEC * 1000;
    let lastMetrics = null;
    let pollCount = 0;

    while (Date.now() - startTime < timeoutMs) {
      pollCount++;
      await page.waitForTimeout(9000);

      const currentUrl = page.url();
      // 如果在编辑页，尝试去回测列表页
      if (!currentUrl.includes('backtest') || currentUrl.includes('/algorithm/index')) {
        console.log(`  Poll #${pollCount}: navigating to backtest list...`);
        try {
          await page.goto('https://www.joinquant.com/algorithm/backtest/list', {
            waitUntil: 'networkidle', timeout: 15000
          });
          await page.waitForTimeout(1000);
          await dismissModals(page);
        } catch (e) {
          console.log('  Navigation to list failed, reloading...');
          try { await page.reload({ waitUntil: 'networkidle', timeout: 15000 }); } catch (e2) {}
        }
      } else {
        // Already on a backtest page, just reload
        try {
          await page.reload({ waitUntil: 'networkidle', timeout: 15000 });
          await page.waitForTimeout(1000);
          await dismissModals(page);
        } catch (e) {
          console.log('  Reload failed, continuing...');
        }
      }

      // 如果在列表页，点击第一个回测结果进入详情
      const onListPage = page.url().includes('backtest/list');
      if (onListPage) {
        // Try clicking the first backtest result to get to detail page
        try {
          await page.evaluate(() => {
            const links = document.querySelectorAll('a[href*="backtest/detail"]');
            if (links.length > 0) {
              links[0].click();
            }
          });
          await page.waitForTimeout(3000);
        } catch (e) {}
      }

      // 探针
      const probe = await page.evaluate(() => {
        const body = document.body ? document.body.innerText || document.body.textContent || '' : '';
        const head8000 = body.slice(0, 8000);

        const hasCancel = [...document.querySelectorAll('button')].some(
          b => (b.innerText || b.textContent || '').includes('取消')
        );

        const hasRunningHint = /运行中|排队中|提交中|正在回测|等待执行/.test(head8000);
        const hasCompleteHint = /回测完成|运行完成/.test(head8000);
        const hasFailedHint = /回测失败|编译失败|Traceback|运行失败|策略失败/.test(head8000);
        const hasRuntimeHint = /实际耗时/.test(head8000);

        // 提取指标
        const metrics = {};
        const patterns = {
          strategy_return: /策略收益[：:\s]*([-\d.]+%)/,
          annual_return: /年化收益[率]?[：:\s]*([-\d.]+%)/,
          sharpe: /夏普比率[：:\s]*([-\d.]+)/,
          max_drawdown: /最大回撤[：:\s]*([-\d.]+%)/,
          excess_return: /超额收益[：:\s]*([-\d.]+%)/,
          benchmark_return: /基准收益[：:\s]*([-\d.]+%)/,
          run_time: /实际耗时[：:\s]*(.+)/,
          status_line: /状态[：:\s]*(.+)/,
        };
        for (const [key, regex] of Object.entries(patterns)) {
          const m = body.match(regex);
          if (m) metrics[key] = m[1].trim();
        }

        return { hasCancel, hasRunningHint, hasCompleteHint, hasFailedHint, hasRuntimeHint, metrics, body_excerpt: head8000.slice(0, 2000), url: window.location.href };
      });

      writeStatus({ metrics: probe.metrics, running_elapsed_sec: Math.round((Date.now() - startTime) / 1000), url: probe.url });

      console.log(`  Poll #${pollCount}: ${Math.round((Date.now() - startTime) / 1000)}s, running=${probe.hasRunningHint}, complete=${probe.hasCompleteHint}, failed=${probe.hasFailedHint}, runtime=${probe.hasRuntimeHint}, url=${probe.url?.slice(0, 80)}`);

      // Screenshot on every poll for debugging
      try {
        await page.screenshot({ path: resolve(OUTPUT_DIR, `${SESSION_ID}_poll_${String(pollCount).padStart(3, '0')}.png`) });
      } catch (e) {
        console.log(`  Screenshot failed: ${e.message}`);
      }

      // Always try to scrape log content (even while running, may have partial output)
      try {
        const partialLog = await scrapeLog(page);
        if (partialLog && partialLog.length > 10) {
          console.log(`  --- LOG (poll #${pollCount}) ---`);
          console.log(partialLog.slice(0, 4000));
          console.log(`  --- END LOG ---`);
          writeStatus({ [`log_poll_${pollCount}`]: partialLog.slice(0, 8000) });
        }
      } catch (e) {
        console.log(`  Log scrape error: ${e.message}`);
      }

      // If runtime hint (backtest finished or failed), scrape the run log
      if (probe.hasRuntimeHint) {
        try {
          // Dismiss modals first (Escape key)
          await page.keyboard.press('Escape');
          await page.waitForTimeout(1000);

          // Click "日志输出" tab using Playwright locator (handles React events)
          const logTab = page.locator('text=日志输出').first();
          try {
            await logTab.click({ timeout: 5000 });
          } catch (e) {
            console.log('  Log tab click via locator failed: ' + e.message);
          }
          await page.waitForTimeout(4000);

          // Use shared scrapeLog
          const completeLog = await scrapeLog(page);
          if (completeLog && completeLog.length > 10) {
            console.log('  --- COMPLETE LOG ---');
            console.log(completeLog.slice(0, 8000));
            console.log('  --- END COMPLETE LOG ---');
            writeStatus({ complete_log: completeLog.slice(0, 8000) });
          }
        } catch (e) {
          console.log('  Complete log scrape error: ' + e.message);
        }
      }

      // 判断失败
      if (probe.hasFailedHint) {
        writeStatus({ state: 'failed', stage: 'completed_failed', error: 'Backtest failed', metrics: probe.metrics, body_excerpt: probe.body_excerpt });
        await page.screenshot({ path: resolve(OUTPUT_DIR, `${SESSION_ID}_failed.png`) });
        console.error('Backtest FAILED');
        console.error(JSON.stringify(probe.metrics, null, 2));
        process.exit(1);
      }

      // 判断成功
      const hasGoodMetrics = probe.metrics.strategy_return && probe.metrics.annual_return;
      const done = (!probe.hasCancel && probe.hasCompleteHint && !probe.hasRunningHint) || hasGoodMetrics;

      if (done) {
        lastMetrics = probe.metrics;

        // Final log scrape using shared helper
        try {
          await page.waitForTimeout(2000);
          const finalLog = await scrapeLog(page);
          if (finalLog && finalLog.length > 50) {
            console.log('  === FINAL LOG ===');
            console.log(finalLog.slice(0, 8000));
            writeStatus({ final_log: finalLog.slice(0, 8000) });
          }
        } catch (e) {
          console.log('  Final log error: ' + e.message);
        }
        break;
      }
    }

    // ─── Step 8: 输出结果 ───
    if (lastMetrics && lastMetrics.strategy_return) {
      writeStatus({ state: 'success', stage: 'completed', metrics: lastMetrics });
      console.log('\n=== BACKTEST COMPLETE ===');
      console.log(JSON.stringify(lastMetrics, null, 2));
      process.exit(0);
    } else {
      writeStatus({ state: 'timeout', stage: 'polling_timeout', metrics: lastMetrics });
      console.error('Backtest timed out');
      await page.screenshot({ path: resolve(OUTPUT_DIR, `${SESSION_ID}_timeout.png`) });
      process.exit(2);
    }

  } catch (e) {
    writeStatus({ state: 'failed', error: e.message, stack: e.stack });
    console.error(`Error: ${e.message}`);
    await page.screenshot({ path: resolve(OUTPUT_DIR, `${SESSION_ID}_error.png`) }).catch(() => {});
    process.exit(1);
  } finally {
    await browser.close();
  }
}

// ─── 辅助函数 ────────────────────────────────────────────

/**
 * Scrape log content from the backtest detail page.
 * Tries clicking pre elements and log containers to reveal content,
 * then extracts textContent after a short wait.
 */
async function scrapeLog(page) {
  return await page.evaluate(() => {
    // Helper to get text from elements
    const getTexts = (selector) => {
      const els = document.querySelectorAll(selector);
      const texts = [];
      for (const el of els) {
        const t = (el.innerText || el.textContent || '').trim();
        if (t.length > 20) texts.push(t);
      }
      return texts;
    };

    // Try pre elements first (most likely log container)
    const preTexts = getTexts('pre');
    if (preTexts.length > 0) {
      // Return the longest pre content
      preTexts.sort((a, b) => b.length - a.length);
      return preTexts[0].slice(0, 10000);
    }

    // Try code blocks
    const codeTexts = getTexts('code');
    if (codeTexts.length > 0) {
      codeTexts.sort((a, b) => b.length - a.length);
      return codeTexts[0].slice(0, 10000);
    }

    // Try common log container classes
    const logSelectors = [
      '.log-content', '.log-area', '.backtest-log', '#backtest_log',
      '.CodeMirror', '.ace_editor', '.ant-tabs-content',
      '.log-panel', '.output-panel', '.strategy-log',
      '[class*="log"]', '[class*="output"]',
    ];
    for (const sel of logSelectors) {
      const texts = getTexts(sel);
      if (texts.length > 0) {
        texts.sort((a, b) => b.length - a.length);
        return texts[0].slice(0, 10000);
      }
    }

    // Search full page for debug markers
    const body = document.body ? (document.body.innerText || document.body.textContent || '') : '';
    const idx = body.search(/\[DEBUG\]|\[INIT\]|\[WARN\]|\[ERROR\]|Traceback|运行日志|策略日志/);
    if (idx >= 0) {
      return body.slice(Math.max(0, idx - 200), idx + 8000);
    }

    return body.slice(0, 5000);
  });
}

/** 关闭各类弹窗 */
async function dismissModals(page) {
  // 新手指引: 「使用Python语言编辑策略」
  try {
    await page.waitForFunction(() => {
      const modals = document.querySelectorAll('.modal, .ant-modal-wrap');
      for (const m of modals) {
        if (m.innerText && m.innerText.includes('使用Python语言编辑策略')) return true;
      }
      return false;
    }, { timeout: 3000 });
    await page.evaluate(() => {
      const modals = document.querySelectorAll('.modal, .ant-modal-wrap');
      for (const m of modals) {
        if (m.innerText && m.innerText.includes('使用Python语言编辑策略')) {
          const buttons = m.querySelectorAll('button');
          // 优先点「跳过」或「不再提示」
          for (const btn of buttons) {
            if (btn.innerText && (btn.innerText.includes('跳过') || btn.innerText.includes('不再提示'))) {
              btn.click(); return;
            }
          }
          // 再点「确定」
          for (const btn of buttons) {
            if (btn.innerText && btn.innerText.includes('确定')) {
              btn.click(); return;
            }
          }
        }
      }
    });
    await page.waitForTimeout(500);
  } catch (e) { /* no intro modal */ }

  // 资源/积分提示
  try {
    await page.evaluate(() => {
      const resourceKeywords = ['积分', 'CPU', '资源', '提示', '说明', '注意'];
      const modals = document.querySelectorAll('.modal, .ant-modal-wrap');
      for (const m of modals) {
        const text = m.innerText || '';
        if (resourceKeywords.some(k => text.includes(k)) && !text.includes('继续运行') && !text.includes('使用Python语言编辑策略')) {
          const buttons = m.querySelectorAll('button');
          for (const btn of buttons) {
            if (btn.innerText && (btn.innerText.includes('不再提示') || btn.innerText.includes('知道了') || btn.innerText.includes('确定'))) {
              btn.click(); return;
            }
          }
        }
      }
    });
  } catch (e) { /* no resource modal */ }

  // 模拟交易弹窗 (backtest detail page)
  try {
    await page.evaluate(() => {
      const modals = document.querySelectorAll('.modal, .ant-modal-wrap, .ant-modal');
      for (const m of modals) {
        const text = m.innerText || m.textContent || '';
        if (text.includes('模拟交易') || text.includes('新建模拟交易') || text.includes('无可用模拟交易位')) {
          const buttons = m.querySelectorAll('button');
          for (const btn of buttons) {
            const txt = btn.innerText || btn.textContent || '';
            if (txt.includes('取消')) {
              btn.click(); return;
            }
          }
          // Also try clicking the X close button
          const closeBtns = m.querySelectorAll('.ant-modal-close, .close, [aria-label="Close"]');
          for (const cb of closeBtns) {
            try { cb.click(); return; } catch(e) {}
          }
        }
      }
    });
  } catch (e) { /* no paper trading modal */ }

  // Generic modal dismiss: press Escape
  try {
    // Click mask/overlay to dismiss
    await page.evaluate(() => {
      const overlays = document.querySelectorAll('.ant-modal-mask');
      for (const o of overlays) {
        try { o.click(); } catch(e) {}
      }
    });
  } catch (e) {}
}

/** 保存代码 */
async function saveCode(page) {
  // Meta+S (mac) / Ctrl+S
  await page.keyboard.press('Meta+s');
  await page.waitForTimeout(1500);

  // Check save status
  const saved = await page.evaluate(() => {
    const text = document.body ? document.body.innerText || '' : '';
    return text.includes('已保存') || text.includes('保存成功') || text.includes('保存完成');
  });

  if (!saved) {
    // Retry
    await page.keyboard.press('Meta+s');
    await page.waitForTimeout(2000);
  }
}

main().catch(e => {
  console.error(e);
  process.exit(1);
});
