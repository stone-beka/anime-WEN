// 拖拽回传 —— 浏览器内逻辑回归（**不调 DeepSeek，不花额度**，约 10 秒跑完）。
//
// 为什么要它：拖拽/待发送列表的逻辑全靠 DOM 事件，Python 单测覆盖不到；
// 而 node --check 只看语法，抓不到 TDZ 这类运行时错误（本功能就踩过一次：
// INPUT_PLACEHOLDER 在 inputEl 声明之前取用，会让整个脚本挂掉）。
// 这个脚本用真 Chrome 把页面跑起来，直接驱动 dragstart/drop/send，并 stub 掉
// streamChat 拦下请求体，检查 images 装对了没。
//
// 前置：Anima Studio 已在 7861 运行。跑法：
//     node tests/e2e_dragdrop_browser.mjs
//   指定 Chrome：CHROME="C:\path\to\chrome.exe" node tests/e2e_dragdrop_browser.mjs
//
// 动了 index.html 里的 拖拽 / pendingImages / send() / loadStrip() 就重跑它。

// 在真实 Chrome 页面里验证「拖拽回传」的逻辑（不调 DeepSeek，靠 stub streamChat 拦请求体）
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const URL_APP = 'http://127.0.0.1:7861/';
const PORT = 9333;

const chrome = spawn(CHROME, [
  '--headless=new', '--disable-gpu', '--no-sandbox',
  `--remote-debugging-port=${PORT}`, '--user-data-dir=C:\\Users\\23077\\_cdp_profile',
  URL_APP,
], { stdio: 'ignore' });

let ws, msgId = 0;
const pending = new Map();
const consoleErrors = [];

function send(method, params = {}) {
  const id = ++msgId;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((res, rej) => pending.set(id, { res, rej }));
}

async function evaluate(expr) {
  const r = await send('Runtime.evaluate', {
    expression: expr, awaitPromise: true, returnByValue: true,
  });
  if (r.exceptionDetails) {
    throw new Error('页面里抛错: ' + JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  }
  return r.result.value;
}

const results = [];
function chk(label, cond, extra = '') {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
  results.push(!!cond);
}

try {
  // 等 Chrome 起来
  let targets;
  for (let i = 0; i < 40; i++) {
    try {
      targets = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
      if (targets.some(t => t.type === 'page' && t.url.includes('7861'))) break;
    } catch (e) {}
    await sleep(250);
  }
  const page = targets.find(t => t.type === 'page' && t.url.includes('7861'));
  if (!page) throw new Error('找不到页面 target');

  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open', r));
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { res, rej } = pending.get(m.id); pending.delete(m.id);
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
    } else if (m.method === 'Runtime.exceptionThrown') {
      consoleErrors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    } else if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      consoleErrors.push(m.params.args.map(a => a.value ?? a.description).join(' '));
    }
  });
  await send('Runtime.enable');
  await sleep(3500);   // 让页面 init / loadStrip 跑完

  console.log('① 页面加载');
  chk('无未捕获异常', consoleErrors.length === 0, consoleErrors.join(' | ').slice(0, 300));
  chk('缩略图带已填充', await evaluate(`document.querySelectorAll('#stripItems .s-item').length > 0`),
      await evaluate(`document.querySelectorAll('#stripItems .s-item').length + ' 张'`));
  chk('旧按钮已移除', await evaluate(`!document.getElementById('visionBtn') && !document.getElementById('visionNote')`));
  chk('投放区已挂上（main 有 drag 监听无法直查，改查函数存在）', await evaluate(`typeof initDropZone === 'function' && typeof handleDrop === 'function'`));

  console.log('\n② 拖图库缩略图进聊天区');
  const first = await evaluate(`document.querySelector('#stripItems .s-item img').getAttribute('src')`);
  await evaluate(`
    (() => {
      const img = document.querySelector('#stripItems .s-item img');
      const dt = new DataTransfer();
      img.dispatchEvent(new DragEvent('dragstart', {dataTransfer: dt, bubbles: true}));
      window.__dt = dt;
      return dt.getData('application/x-anima-image');
    })()
  `);
  chk('dragstart 写入了内部载荷', await evaluate(`JSON.parse(window.__dt.getData('application/x-anima-image')).url === ${JSON.stringify(first)}`),
      first);
  await evaluate(`handleDrop(window.__dt)`);
  chk('落入待发送列表', await evaluate(`pendingImages.length === 1`), await evaluate(`JSON.stringify(pendingImages.map(p=>p.name))`));
  chk('待发送行显示了', await evaluate(`document.getElementById('pendingRow').classList.contains('on')`));
  chk('缩略图渲染出来了（1 张）', await evaluate(`document.querySelectorAll('#pendingRow .p-item').length === 1`));
  chk('输入框提示改成「写关注点」', await evaluate(`inputEl.placeholder.includes('注意什么')`));

  console.log('\n③ 重复拖同一张 → 去重');
  await evaluate(`handleDrop(window.__dt)`);
  chk('还是 1 张（不重复附带）', await evaluate(`pendingImages.length === 1`));

  console.log('\n④ 再拖第 2、3 张');
  await evaluate(`
    (() => {
      const imgs = document.querySelectorAll('#stripItems .s-item img');
      for (const i of [1, 2]) {
        const dt = new DataTransfer();
        imgs[i].dispatchEvent(new DragEvent('dragstart', {dataTransfer: dt, bubbles: true}));
        handleDropSync(dt);
      }
      function handleDropSync(dt) {
        const raw = dt.getData('application/x-anima-image');
        const o = JSON.parse(raw); addPending(o.name, o.url, false);
      }
    })()
  `);
  chk('共 3 张', await evaluate(`pendingImages.length === 3`));
  chk('渲染 3 个缩略图', await evaluate(`document.querySelectorAll('#pendingRow .p-item').length === 3`));

  console.log('\n⑤ 移除一张 / 全部清除');
  await evaluate(`document.querySelector('#pendingRow .p-x').click()`);
  chk('移除后剩 2 张', await evaluate(`pendingImages.length === 2`));
  chk('DOM 同步为 2', await evaluate(`document.querySelectorAll('#pendingRow .p-item').length === 2`));
  await evaluate(`document.querySelector('#pendingRow .p-clear').click()`);
  chk('全部清除后为 0', await evaluate(`pendingImages.length === 0`));
  chk('待发送行收起了', await evaluate(`!document.getElementById('pendingRow').classList.contains('on')`));
  chk('placeholder 还原', await evaluate(`inputEl.placeholder === INPUT_PLACEHOLDER`));

  console.log('\n⑥ 发送：拦下请求体看 images 装对了没');
  await evaluate(`
    (() => {
      const imgs = document.querySelectorAll('#stripItems .s-item img');
      for (const i of [0, 1]) {
        const dt = new DataTransfer();
        imgs[i].dispatchEvent(new DragEvent('dragstart', {dataTransfer: dt, bubbles: true}));
        const o = JSON.parse(dt.getData('application/x-anima-image'));
        addPending(o.name, o.url, false);
      }
      window.__captured = null;
      window.__origStream = streamChat;
      streamChat = async body => { window.__captured = body; };
      inputEl.value = '看视角和神态';
    })()
  `);
  await evaluate(`send()`, true);
  await sleep(1500);
  const cap = await evaluate(`JSON.stringify({
    n: (window.__captured?.images || []).length,
    names: (window.__captured?.images || []).map(i => i.name),
    isJpeg: (window.__captured?.images || []).every(i => i.data.startsWith('data:image/jpeg;base64,')),
    bytes: (window.__captured?.images || []).map(i => i.data.length),
    msg: window.__captured?.message,
    sid: !!window.__captured?.session_id
  })`);
  const c = JSON.parse(cap);
  chk('带了 2 张图', c.n === 2, JSON.stringify(c.names));
  chk('都是真 JPEG data URL', c.isJpeg, '字节数 ' + JSON.stringify(c.bytes));
  chk('文本原样带上', c.msg === '看视角和神态', c.msg);
  chk('session_id 在', c.sid);
  chk('发完 pending 清空', await evaluate(`pendingImages.length === 0`));
  chk('发完输入框清空', await evaluate(`inputEl.value === ''`));

  console.log('\n⑦ 只带图、不写字也能发');
  await evaluate(`
    (() => {
      const dt = new DataTransfer();
      document.querySelector('#stripItems .s-item img').dispatchEvent(new DragEvent('dragstart', {dataTransfer: dt, bubbles: true}));
      const o = JSON.parse(dt.getData('application/x-anima-image'));
      addPending(o.name, o.url, false);
      window.__captured = null; inputEl.value = '';
    })()
  `);
  await evaluate(`send()`);
  await sleep(1200);
  chk('发出去了（message 为空、图 1 张）', await evaluate(`!!window.__captured && window.__captured.message === '' && window.__captured.images.length === 1`));

  console.log('\n⑧ 从系统拖图片文件');
  await evaluate(`
    (() => {
      const c = document.createElement('canvas'); c.width = c.height = 8;
      c.getContext('2d').fillRect(0, 0, 8, 8);
      return new Promise(res => c.toBlob(b => {
        const f = new File([b], 'my_photo.png', {type: 'image/png'});
        const dt = new DataTransfer(); dt.items.add(f);
        res(handleDrop(dt).then(() => 1));
      }, 'image/png'));
    })()
  `);
  await sleep(400);
  chk('文件被接收', await evaluate(`pendingImages.length === 1 && pendingImages[0].name === 'my_photo.png'`),
      await evaluate(`JSON.stringify(pendingImages.map(p=>p.name))`));
  chk('标记为 blob（会在清空时回收）', await evaluate(`pendingImages[0].blob === true`));
  await evaluate(`clearPending()`);

  console.log('\n⑨ 非图片拖入 → 提示而不是静默');
  await evaluate(`window.__alerted = ''; window.alert = m => { window.__alerted = m; };`);
  await evaluate(`
    (() => {
      const dt = new DataTransfer();
      dt.setData('text/plain', 'https://example.com/not-an-image');
      return handleDrop(dt);
    })()
  `);
  chk('给了提示', await evaluate(`window.__alerted.includes('只认图片')`), await evaluate(`window.__alerted`));
  chk('没被误加进列表', await evaluate(`pendingImages.length === 0`));

  console.log('\n⑩ 图片文件之外：正常文字消息不受影响');
  await evaluate(`streamChat = window.__origStream;`);
  chk('sendMessage 不带 attach 仍是纯文本', await evaluate(`
    (() => { window.__captured = null; streamChat = async b => { window.__captured = b; };
      return sendMessage('你好').then(() => !window.__captured.images && window.__captured.message === '你好'); })()
  `));

  console.log('\n' + '='.repeat(60));
  console.log(results.every(Boolean) ? '全部通过 ✅' : `失败 ${results.filter(x => !x).length} 项 ❌`);
  console.log('页面异常日志:', consoleErrors.length ? consoleErrors.slice(0, 3) : '无 ✅');
  process.exit(results.every(Boolean) ? 0 : 1);
} catch (e) {
  console.error('测试脚本出错:', e.message);
  process.exit(2);
} finally {
  try { ws?.close(); } catch (e) {}
  chrome.kill();
}
