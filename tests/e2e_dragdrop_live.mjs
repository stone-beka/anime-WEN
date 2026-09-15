// 拖拽回传 —— 浏览器内**真链路**（会真发给 DeepSeek，**消耗 API 额度**，约 1~2 分钟）。
//
// 和 e2e_dragdrop_browser.mjs 的分工：那个 stub 掉 streamChat 只验逻辑、不花钱；
// 这个走完整用户路径（拖图 → 点发送 → 真请求 → 看回复），验的是「AI 确实看得见图」
// 和「带图这一轮不出提示词、下一轮说改才出」这条边界。
// 和 e2e_vision_live.py 的分工：那个从 HTTP 层打后端（不需要浏览器），这个从浏览器打全链路。
//
// ⚠️ 断言别用 innerText 找 '<prompt>' —— renderContent() 会把标签转成
//    <span class="prompt-block">，字面量根本不在文本里，那样断言会永远「通过」。
//    可靠信号是 done 事件有没有把 promptArea 填上。
//
// 前置：Anima Studio 已在 7861 运行。跑法：
//     node tests/e2e_dragdrop_live.mjs
//
// 它会自建一个会话、跑完删掉，不污染你的历史记录。

// 真链路：在页面里拖图 → 点发送 → 真发给 DeepSeek → 校验回复（会花 API 额度）
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9334;
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
  `--remote-debugging-port=${PORT}`, '--user-data-dir=C:\\Users\\23077\\_cdp_profile2',
  'http://127.0.0.1:7861/'], { stdio: 'ignore' });

let ws, msgId = 0;
const pending = new Map();
const errs = [];
function send(method, params = {}) {
  const id = ++msgId; ws.send(JSON.stringify({ id, method, params }));
  return new Promise((res, rej) => pending.set(id, { res, rej }));
}
async function ev(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
const R = [];
const chk = (l, c, x = '') => { console.log(`  ${c ? 'PASS' : 'FAIL'}  ${l}${x ? '  ' + x : ''}`); R.push(!!c); };

try {
  let ts;
  for (let i = 0; i < 40; i++) {
    try { ts = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
      if (ts.some(t => t.type === 'page' && t.url.includes('7861'))) break; } catch (e) {}
    await sleep(250);
  }
  const page = ts.find(t => t.type === 'page' && t.url.includes('7861'));
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open', r));
  ws.addEventListener('message', e => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); }
    else if (m.method === 'Runtime.exceptionThrown') errs.push(m.params.exceptionDetails.exception?.description);
  });
  await send('Runtime.enable');
  await sleep(3500);

  // 建个干净会话，避免污染用户的真实会话
  const sid = await ev(`newChat().then(() => currentSession)`);
  console.log('测试会话', sid);
  // ⚠️ 别用 innerText 里的 '<prompt>' 判断有没有出提示词——renderContent() 会把标签
  //    转成 <span class="prompt-block">，字面量根本不在文本里。真正可靠的信号是
  //    done 事件有没有把 promptArea 填上（后端 extract_prompt 的产物）。
  const basePrompt = await ev(`promptArea.value`);
  console.log('基线 promptArea 长度:', basePrompt.length);

  console.log('\n① 拖图库里的图进聊天区');
  await ev(`
    (() => {
      const img = document.querySelector('#stripItems .s-item img');
      const dt = new DataTransfer();
      img.dispatchEvent(new DragEvent('dragstart', {dataTransfer: dt, bubbles: true}));
      const o = JSON.parse(dt.getData('application/x-anima-image'));
      addPending(o.name, o.url, false);
      inputEl.value = '看看这张图的视角和人物神态';
      document.getElementById('send').click();     // 走真实按钮，不是直接调函数
      return pendingImages.length;
    })()
  `);
  chk('点发送后 pending 立即清空（UI 不卡）', await ev(`pendingImages.length === 0`));

  // 等流式回复结束
  for (let i = 0; i < 90; i++) {
    await sleep(1000);
    const done = await ev(`!document.querySelector('#messages .msg.assistant .loading') && document.querySelectorAll('#messages .msg.assistant').length > 0 && !window.__streaming`);
    const txt = await ev(`(document.querySelector('#messages .msg.assistant')?.innerText || '').length`);
    if (txt > 100 && await ev(`document.querySelector('#input').value === ''`)) {
      // 等回复稳定（长度不再变）
      const a = txt; await sleep(2500);
      if (await ev(`(document.querySelector('#messages .msg.assistant')?.innerText || '').length`) === a) break;
    }
  }

  const reply = await ev(`(() => { const ns = [...document.querySelectorAll('#messages .msg.assistant')]; return ns[ns.length-1].innerText; })()`);
  const replyHtml = await ev(`(() => { const ns = [...document.querySelectorAll('#messages .msg.assistant')]; return ns[ns.length-1].innerHTML; })()`);
  const userBubble = await ev(`(() => { const ns = [...document.querySelectorAll('#messages .msg.user')]; return ns[ns.length-1].innerHTML; })()`);
  const promptAfterVision = await ev(`promptArea.value`);

  console.log('\n② 校验');
  chk('发送后回了内容', reply.length > 100, reply.length + ' 字');
  chk('这一轮没产出提示词（promptArea 仍为空）', promptAfterVision === basePrompt && !promptAfterVision,
      `"${promptAfterVision.slice(0, 60)}"`);
  chk('回复里没有高亮的提示词块', !replyHtml.includes('prompt-block'));
  chk('回复里没有负向词块', !replyHtml.includes('negative-block'));
  const hits = ['视角', '神态', '镜头', '构图', '服装', '配色', '背景'].filter(k => reply.includes(k));
  chk('谈到构图要素', hits.length >= 2, '命中: ' + hits.join('、'));
  chk('用户气泡里带回了缩略图', userBubble.includes('attach-thumb') && userBubble.includes('.vision.jpg'));
  chk('用户气泡里有我写的关注点', userBubble.includes('看看这张图的视角和人物神态'));
  console.log('\n回复节选:\n' + reply.slice(0, 420));

  // ③ 下一句要求修改 → 这时才出提示词
  console.log('\n③ 接着说「把背景改成夜晚」');
  await ev(`inputEl.value = '把背景改成夜晚'; document.getElementById('send').click();`);
  let prev = 0;
  for (let i = 0; i < 90; i++) {
    await sleep(1200);
    const n = await ev(`(() => { const ns=[...document.querySelectorAll('#messages .msg.assistant')]; return ns.length ? ns[ns.length-1].innerText.length : 0; })()`);
    if (n > 100 && n === prev) break;
    prev = n;
  }
  const reply2Html = await ev(`(() => { const ns=[...document.querySelectorAll('#messages .msg.assistant')]; return ns[ns.length-1].innerHTML; })()`);
  const prompt2 = await ev(`promptArea.value`);
  const neg2 = await ev(`negativeArea.value`);
  chk('这一轮产出了提示词', prompt2.length > 50 && prompt2 !== basePrompt, prompt2.length + ' 字符');
  chk('回复里有高亮的提示词块', reply2Html.includes('prompt-block'));
  chk('提示词被自动填进提示词框', prompt2.length > 50);
  chk('提示词是英文', /^[\x00-\x7F\s,()]+$/.test(prompt2.slice(0, 200)), prompt2.slice(0, 100));
  console.log('  负向词长度:', neg2.length);

  // 清理测试会话
  await ev(`api('/api/session/' + currentSession, {method:'DELETE'})`);
  console.log('\n（已删除测试会话）');

  console.log('\n' + '='.repeat(60));
  console.log(R.every(Boolean) ? '全部通过 ✅' : `失败 ${R.filter(x => !x).length} 项 ❌`);
  console.log('页面异常:', errs.length ? errs.slice(0, 3) : '无 ✅');
  process.exit(R.every(Boolean) ? 0 : 1);
} catch (e) {
  console.error('出错:', e.message); process.exit(2);
} finally { try { ws?.close(); } catch (e) {} chrome.kill(); }
