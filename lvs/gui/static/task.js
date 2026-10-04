/* 任务工作台：跑阶段 + 看日志 + 审片重做（票据 36）
   三条与后端一致的规矩：
   1. 阶段一律**串行** —— 忙的时候按钮直接锁掉，不让用户撞 409；
   2. 日志走 SSE，界面只是订阅者，关掉页面不影响那条子进程；
   3. 改分镜走 `lvs studio --redo`，不自己拼 ffmpeg / 不碰 ComfyUI。 */

let busy = false;          // 有阶段在跑（全局串行）
let currentJob = null;
let stream = null;
let shotCache = [];
let editing = null;

/* 实拍镜的来源策略（票 41）。它是**任务级**的，所以单独放在流水线上方，
   跑任何阶段都自动带上，不进每个阶段的「选项」弹窗（免得两处各说一套）。 */
let SOURCE_MODE = 'auto';

const $ = (id) => document.getElementById(id);

/* ---------- 阶段 ---------- */

/** 把来源策略塞进这次运行的参数里（只对认识 `source` 的阶段）。 */
function withSource(stage, options) {
  const spec = STAGE_SPEC[stage] || [];
  if (!spec.some((o) => o.key === 'source')) return options;
  return { ...options, source: SOURCE_MODE };
}

function stageOptions(stage) {
  const spec = STAGE_SPEC[stage] || [];
  const saved = JSON.parse(localStorage.getItem('lvs.opt.' + stage) || '{}');
  const out = {};
  for (const opt of spec) {
    let value = saved[opt.key];
    if (value === undefined) value = opt.kind === 'bool' ? false : (opt.default || '');
    out[opt.key] = value;
  }
  return out;
}

function openOptions(stage) {
  // `source` 由流水线上方那个选择器统一负责，这里不重复出现
  const spec = (STAGE_SPEC[stage] || []).filter((o) => o.key !== 'source');
  const values = stageOptions(stage);
  $('opt-title').textContent = '运行「' + stage + '」';
  $('opt-body').innerHTML = spec.map((opt) => {
    const v = values[opt.key];
    if (opt.kind === 'bool') {
      return `<label class="check"><input type="checkbox" data-opt="${esc(opt.key)}" ${v ? 'checked' : ''}>${esc(opt.label)}</label>`;
    }
    if (opt.kind === 'choice') {
      const opts = opt.choices.map((c) => `<option value="${esc(c)}" ${c === v ? 'selected' : ''}>${esc(c)}</option>`).join('');
      return `<div class="field"><label>${esc(opt.label)}</label><select data-opt="${esc(opt.key)}">${opts}</select>
              ${opt.hint ? `<span class="hint">${esc(opt.hint)}</span>` : ''}</div>`;
    }
    return `<div class="field"><label>${esc(opt.label)}</label>
            <input type="text" data-opt="${esc(opt.key)}" value="${esc(v)}" autocomplete="off">
            ${opt.hint ? `<span class="hint">${esc(opt.hint)}</span>` : ''}</div>`;
  }).join('') || '<div class="dim">这个阶段没有可调选项，直接开始即可。</div>';

  $('opt-go').onclick = () => {
    const picked = {};
    $('opt-body').querySelectorAll('[data-opt]').forEach((el) => {
      picked[el.dataset.opt] = el.type === 'checkbox' ? el.checked : el.value.trim();
    });
    localStorage.setItem('lvs.opt.' + stage, JSON.stringify(picked));
    $('dlg-opt').close();
    doRun(stage, picked);
  };
  $('dlg-opt').showModal();
}

function runStage(stage) {
  if (busy) { toast('已有阶段在跑，显存只够串行 —— 先等它结束或点「停止」。', true); return; }
  const spec = (STAGE_SPEC[stage] || []).filter((o) => o.key !== 'source');
  if (spec.length) openOptions(stage); else doRun(stage, {});
}

/* ---------- 来源策略 ---------- */

function initSourceMode() {
  const fromUrl = new URLSearchParams(location.search).get('source');
  const initial = fromUrl || SOURCE_MODE_SAVED || 'auto';
  $('src-mode').value = initial;
  SOURCE_MODE = $('src-mode').value;
  if (fromUrl) persistSourceMode(fromUrl);      // 新建任务时选的那个，落地它
}

async function onSourceChange() {
  const mode = $('src-mode').value;
  SOURCE_MODE = mode;
  const saved = await persistSourceMode(mode);
  toast(saved
    ? '来源策略已记为「' + $('src-mode').selectedOptions[0].textContent + '」，重跑素材才会重取。'
    : '还没拆镜，策略会在第一次运行「拆镜」时生效。');
}

/** 记进 shots.json（没拆镜就没得记，返回 false）。 */
async function persistSourceMode(mode) {
  try {
    const r = await fetch(`/api/task/${encodeURIComponent(TASK)}/source-mode`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { toast(data.error || '策略没记上', true); return false; }
    return !!data.persisted;
  } catch (e) {
    toast(e.message, true);
    return false;
  }
}

async function doRun(stage, options) {
  if (busy) { toast('已有阶段在跑。', true); return; }
  const payload = withSource(stage, options);
  if (payload.source) appendLog('[gui] 来源策略：' + payload.source);
  appendLog('[gui] 启动 ' + stage + ' …');
  try {
    const data = await postJSON(`/api/task/${encodeURIComponent(TASK)}/run`, { stage, options: payload });
    if (data.note) appendLog('[gui] ' + data.note);   // 例如"已把 pexels 镜改成本地生图"
    subscribe(data.job.id, stage);
  } catch (e) {
    appendLog('[gui] ' + e.message);
    toast(e.message, true);
  }
}

async function runAll() {
  if (busy) { toast('已有阶段在跑。', true); return; }
  const options = withSource('run', {});
  appendLog('[gui] 一键到底（parse → shots → assets → voice → build）…'
            + (options.source ? '［来源 ' + options.source + '］' : ''));
  try {
    const data = await postJSON(`/api/task/${encodeURIComponent(TASK)}/run`, { stage: 'run', options });
    if (data.note) appendLog('[gui] ' + data.note);
    subscribe(data.job.id, 'run');
  } catch (e) { toast(e.message, true); }
}

/* ---------- 日志流 ---------- */

function appendLog(line, cls) {
  const box = $('log');
  if (box.dataset.fresh !== '1') { box.textContent = ''; box.dataset.fresh = '1'; }
  const span = document.createElement('div');
  if (cls) span.className = cls;
  else if (/^\[gui\] 阶段结束（成功）/.test(line)) span.className = 'l-ok';
  else if (/失败|错误|Error|Traceback/.test(line)) span.className = 'l-err';
  else if (/^\[gui\]/.test(line)) span.className = 'l-gui';
  span.textContent = line;
  box.appendChild(span);
  box.scrollTop = box.scrollHeight;
}

function subscribe(jobId, stage) {
  currentJob = jobId;
  busy = true;
  setBusyUI(true, stage);
  if (stream) stream.close();
  stream = new EventSource(`/api/job/${jobId}/stream`);
  stream.onmessage = (ev) => {
    try { appendLog(JSON.parse(ev.data)); } catch (_) { appendLog(ev.data); }
  };
  stream.addEventListener('end', () => {
    stream.close(); stream = null;
    busy = false; currentJob = null;
    setBusyUI(false);
    refresh();                     // 阶段落地的产物变了，重新拉一遍
  });
  stream.onerror = () => { /* SSE 断了就等下一次刷新兜底 */ };
}

function setBusyUI(on, stage) {
  $('btn-stop').hidden = !on;
  $('job-label').textContent = on ? `运行中：${stage || ''}` : '空闲';
  $('pipe').classList.toggle('busy', on);
  document.querySelectorAll('#pipe button').forEach((b) => { b.disabled = on; });
}

async function stopJob() {
  try {
    const r = await postJSON('/api/job/stop', {});
    toast(r.ok ? '已发送停止请求' : '当前没有在跑的阶段');
  } catch (e) { toast(e.message, true); }
}

/* ---------- 刷新 ---------- */

async function refresh() {
  try {
    const [task, shots] = await Promise.all([
      api(`/api/task/${encodeURIComponent(TASK)}`),
      api(`/api/task/${encodeURIComponent(TASK)}/shots`),
    ]);
    paintPipeline(task);
    shotCache = shots.shots || [];
    paintShots();
  } catch (e) { /* 刷新失败不打扰用户 */ }
}

function paintPipeline(task) {
  for (const st of task.stages) {
    const el = $(`step-${st.name}`);
    if (!el) continue;
    el.classList.toggle('done', ['done', 'skipped'].includes(st.status));
    el.querySelector('.st').textContent = st.status;
    const prog = el.querySelector('.prog');
    if (['assets', 'voice', 'build'].includes(st.name)) {
      // total 为 0 = 总数未知（还没拆镜）：显示「—」，绝不拿 done 凑成 100%
      if (!st.total) {
        prog.innerHTML = `${st.done}<small> / —</small>`;
        el.querySelector('.bar i').style.width = '0%';
      } else {
        prog.innerHTML = `${st.done}<small> / ${st.total}</small>`;
        el.querySelector('.bar i').style.width = Math.floor((100 * st.done) / st.total) + '%';
      }
    } else {
      prog.textContent = st.done ? '就绪' : '—';
      el.querySelector('.bar i').style.width = (st.done ? 100 : 0) + '%';
    }
  }
}

/* ---------- 分镜栅格 ---------- */

function paintShots() {
  const q = ($('q').value || '').trim().toLowerCase();
  const kind = $('f-kind').value;
  const source = $('f-source').value;
  const failedOnly = $('f-failed').checked;

  const list = shotCache.filter((s) => {
    if (kind && s.kind !== kind) return false;
    if (source && s.source !== source) return false;
    if (failedOnly && s.status !== 'failed') return false;
    if (!q) return true;
    return (`${s.id}`.includes(q) || (s.narration || '').toLowerCase().includes(q)
            || (s.prompt || '').toLowerCase().includes(q) || (s.visual || '').toLowerCase().includes(q));
  });

  $('shot-count').textContent = `（${list.length} / ${shotCache.length}）`;
  if (!list.length) {
    $('grid').innerHTML = '<div class="empty">没有匹配的分镜。</div>';
    return;
  }

  $('grid').innerHTML = list.map((s) => {
    const badge = s.status === 'failed' ? '<span class="tag" style="border-color:rgba(212,103,78,.5);color:#d4674e">失败</span>'
      : (s.kind === 'graphic' ? '<span class="tag acc">卡片</span>' : '<span class="tag">实拍</span>');
    const img = s.has_asset
      ? `<img loading="lazy" src="${s.thumb}" alt="镜 ${s.id}" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'读不到图'}))">`
      : '<div class="ph">还没有素材</div>';
    return `<div class="shot">
      ${img}
      <div class="meta">
        <div class="row"><span class="id">#${String(s.id).padStart(3, '0')}</span>${badge}</div>
        <div class="narr">${esc(s.narration || s.visual || '（无旁白）')}</div>
        <div class="ops">
          <button class="btn tiny" onclick="openShot(${s.id})">查看 / 改</button>
          <button class="btn tiny" onclick="quickAgain(${s.id})">换一张</button>
        </div>
      </div>
    </div>`;
  }).join('');
}

/* ---------- 单镜编辑 ---------- */

function findShot(sid) { return shotCache.find((s) => s.id === sid); }

function openShot(sid) {
  const s = findShot(sid);
  if (!s) return;
  editing = sid;
  $('shot-title').textContent = `分镜 #${String(sid).padStart(3, '0')}`;
  $('shot-body').innerHTML = `
    <div class="field">
      <label>旁白（改它会连带把 voice / build 标成需重跑）</label>
      <textarea id="ed-narration" style="min-height:70px">${esc(s.narration)}</textarea>
    </div>
    <div class="field">
      <label>生图提示词</label>
      <textarea id="ed-prompt" style="min-height:80px">${esc(s.prompt)}</textarea>
    </div>
    <div class="field">
      <label>画面位（原始编辑设计）</label>
      <textarea id="ed-visual" style="min-height:60px;color:var(--fg-mute)" readonly>${esc(s.visual)}</textarea>
    </div>
    <div class="field" style="max-width:220px">
      <label>seed</label>
      <input type="number" id="ed-seed" value="${s.seed ?? ''}">
      <span class="hint">留空 = 沿用；「换一张」会自动 +1</span>
    </div>
    <div class="field" style="max-width:260px">
      <label>来源</label>
      <select id="ed-source">
        ${['local', 'graphic', 'pexels', 'library'].map((v) => `<option value="${v}" ${v === s.source ? 'selected' : ''}>${v}</option>`).join('')}
      </select>
    </div>
    ${s.error ? `<div class="dim" style="color:#d4674e">上次失败：${esc(s.error)}</div>` : ''}
    <div class="field" style="border-top:1px solid var(--line-soft);padding-top:14px;margin-top:6px">
      <label>自己选一张图</label>
      <input type="file" id="ed-pick" accept="image/png,image/jpeg,image/webp,image/bmp">
      <span class="hint">从文件夹里挑一张钉死给这一镜用 —— 之后重跑素材也不会被覆盖
        （${s.pinned ? '当前已钉死：' + esc(s.pinned) : '还没钉死过'}）。</span>
      <button class="btn" style="align-self:flex-start;margin-top:6px" onclick="pickImage()">用它替换本镜画面</button>
    </div>
    <div style="margin-top:12px">${s.has_asset ? `<img src="${s.thumb}" style="width:100%;border-radius:6px">` : '<div class="dim">还没有素材</div>'}</div>`;
  $('dlg-shot').showModal();
}

async function pickImage() {
  if (busy) { toast('已有阶段在跑，先等它结束。', true); return; }
  const file = $('ed-pick').files[0];
  if (!file) { toast('先在文件夹里选一张图', true); return; }
  const fd = new FormData();
  fd.append('image', file);
  try {
    const res = await fetch(`/api/task/${encodeURIComponent(TASK)}/shot/${editing}/pick`, { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || '换图失败');
    $('dlg-shot').close();
    appendLog(`[gui] 镜 ${editing} 已改用你选的图：${data.picked}`);
    subscribe(data.job.id, 'studio');
  } catch (e) { toast(e.message, true); }
}

async function saveShot() {
  const source = $('ed-source').value;
  const patch = {
    narration: $('ed-narration').value,
    prompt: $('ed-prompt').value,
    seed: $('ed-seed').value === '' ? '' : Number($('ed-seed').value),
  };
  // source 也允许改；但要和补跑逻辑对齐（改来源后要重出这张）
  patch.source = source;
  await postJSON(`/api/task/${encodeURIComponent(TASK)}/shots/${editing}`, patch);
}

async function redoShot(mode) {
  if (busy) { toast('已有阶段在跑，先等它结束。', true); return; }
  try {
    await saveShot();
    const body = { seed: null, prompt: null };
    if (mode === 'save') {
      body.prompt = $('ed-prompt').value;
      const seed = $('ed-seed').value;
      if (seed !== '') body.seed = Number(seed);
    }
    $('dlg-shot').close();
    const data = await postJSON(`/api/task/${encodeURIComponent(TASK)}/shot/${editing}/redo`, body);
    appendLog(`[gui] 重做镜 ${editing}（${mode === 'save' ? '按设定' : 'seed+1'}）`);
    subscribe(data.job.id, 'studio');
  } catch (e) { toast(e.message, true); }
}

async function quickAgain(sid) {
  if (busy) { toast('已有阶段在跑，先等它结束。', true); return; }
  try {
    const data = await postJSON(`/api/task/${encodeURIComponent(TASK)}/shot/${sid}/redo`, {});
    appendLog(`[gui] 重做镜 ${sid}（seed+1）`);
    subscribe(data.job.id, 'studio');
  } catch (e) { toast(e.message, true); }
}

/* ---------- 启动 ---------- */

['q', 'f-kind', 'f-source', 'f-failed'].forEach((id) => {
  const el = $(id);
  el.addEventListener('input', paintShots);
  el.addEventListener('change', paintShots);
});

(async function init() {
  initSourceMode();
  await refresh();
  // 打开页面时若已有阶段在跑，自动接上日志
  try {
    const { job } = await api('/api/job/current');
    if (job && job.running) subscribe(job.id, job.stage);
  } catch (_) { /* 没在跑就算了 */ }
})();
