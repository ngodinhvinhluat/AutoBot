const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let SCHEMA = [], VALUES = {};
let toastTimer = null;

function toast(msg, isErr) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = isErr ? 'err' : ''; }, isErr ? 6000 : 2500);
}

function fmt(spec, v) {
  if (spec.type !== 'range') return v;
  const n = Number(v);
  const shown = Number.isInteger(spec.step) ? n.toFixed(0) : n.toFixed(2).replace(/0$/, '');
  return shown + (spec.unit ? ' ' + spec.unit : '');
}

function field(s) {
  const v = VALUES[s.key];
  const help = s.help ? `<div class="help">${esc(s.help)}</div>` : '';
  let ctl = '';
  if (s.type === 'bool') {
    ctl = `<label class="switch"><input type="checkbox" data-key="${s.key}" ${v ? 'checked' : ''}><span></span></label>`;
  } else if (s.type === 'range') {
    ctl = `<div class="range"><input type="range" data-key="${s.key}" min="${s.min}" max="${s.max}" step="${s.step}" value="${v}"><b id="v_${s.key}">${esc(fmt(s, v))}</b></div>`;
  } else if (s.type === 'select') {
    ctl = `<select data-key="${s.key}">${s.options.map(o => `<option value="${esc(o[0])}" ${o[0] === v ? 'selected' : ''}>${esc(o[1])}</option>`).join('')}</select>`;
  } else if (s.type === 'textarea') {
    ctl = `<textarea data-key="${s.key}" rows="4">${esc(v)}</textarea>`;
  } else {
    ctl = `<input type="text" data-key="${s.key}" value="${esc(v)}">`;
  }
  return `<div class="field${s.type === 'textarea' ? ' wide' : ''}"><div class="lbl">${esc(s.label)}</div>${ctl}${help}</div>`;
}

function renderSettings() {
  const groups = {};
  SCHEMA.forEach(s => { (groups[s.group] = groups[s.group] || []).push(s); });
  document.getElementById('settings').innerHTML = Object.entries(groups).map(([g, items]) =>
    `<details open class="group"><summary>${esc(g)}</summary><div class="fields">${items.map(field).join('')}</div></details>`
  ).join('');
  document.getElementById('settings').classList.remove('muted');
  document.querySelectorAll('#settings [data-key]').forEach(el => {
    const key = el.dataset.key;
    const spec = SCHEMA.find(s => s.key === key);
    if (spec.type === 'range') {
      el.addEventListener('input', () => { document.getElementById('v_' + key).textContent = fmt(spec, el.value); });
      el.addEventListener('change', () => save(key, Number(el.value)));
    } else if (spec.type === 'bool') {
      el.addEventListener('change', () => save(key, el.checked));
    } else {
      el.addEventListener('change', () => save(key, el.value));
    }
  });
}

async function loadSettings() {
  const res = await fetch('api/settings');
  const data = await res.json();
  SCHEMA = data.schema; VALUES = data.values;
  document.getElementById('cfgfile').textContent = data.file;
  renderSettings();
}

async function save(key, value) {
  try {
    const res = await fetch('api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ [key]: value }) });
    const data = await res.json();
    VALUES = data.values;
    const errs = Object.values(data.errors || {});
    if (errs.length) { toast('Không lưu được: ' + errs.join(', '), true); renderSettings(); }
    else {
      const spec = SCHEMA.find(s => s.key === key);
      toast('Đã lưu: ' + spec.label + (spec.type === 'textarea' ? '' : ' = ' + (spec.type === 'bool' ? (value ? 'BẬT' : 'TẮT') : fmt(spec, VALUES[key]))));
    }
  } catch (e) { toast('Lỗi kết nối server: ' + e, true); }
}

async function action(name, body) {
  try {
    const res = await fetch('api/action/' + name, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    const data = await res.json();
    toast(data.message || (data.ok ? 'Xong' : 'Lỗi'), !data.ok);
    if (data.values) { VALUES = data.values; renderSettings(); }
    if (name === 'clear_history') document.getElementById('logs').innerHTML = '<span class="muted">Đã xóa lịch sử.</span>';
    return data;
  } catch (e) { toast('Lỗi kết nối server: ' + e, true); }
}

async function testTts() {
  const btn = document.getElementById('btnTest');
  btn.disabled = true; btn.textContent = '⏳ Đang tổng hợp...';
  await action('test_tts', { text: document.getElementById('testText').value });
  btn.disabled = false; btn.textContent = '🔈 Phát thử ra loa';
}

function renderDevices(devs) {
  if (!devs.length) return '<span class="muted">Chưa có thiết bị nào kết nối (ESP32 tự kết nối lại mỗi 3 giây).</span>';
  return devs.map(d => {
    const scale = Math.max(d.level, d.start_thr * 1.4, 1);
    const p = x => Math.min(100, x / scale * 100).toFixed(1);
    return `<div class="dev">
      <div class="devhead"><b>${esc(d.addr)}</b>
        <span class="badge">${esc(d.state)}</span>
        <span class="badge ${d.tts ? 'on' : 'off'}">Loa ${d.tts ? 'BẬT' : 'TẮT'}${d.tts_forced ? ' (theo URL)' : ''}</span>
        <span class="small muted">kết nối từ ${esc(d.since)}</span></div>
      <div class="meter"><div class="fill" style="width:${p(d.level)}%"></div>
        <i class="mk floor" style="left:${p(d.floor)}%"></i><i class="mk cont" style="left:${p(d.cont_thr)}%"></i><i class="mk start" style="left:${p(d.start_thr)}%"></i></div>
      <div class="small muted">Mức mic ${d.level} · Nền ồn ${d.floor} · Ngưỡng duy trì ${d.cont_thr} · Ngưỡng bắt đầu ${d.start_thr}</div>
    </div>`;
  }).join('');
}

async function refresh() {
  try {
    const res = await fetch('api/state');
    const data = await res.json();
    document.getElementById('mode').textContent = data.ai_mode;
    document.getElementById('logfile').textContent = data.log_file;
    document.getElementById('devices').innerHTML = renderDevices(data.devices || []);
    if (data.logs.length > 0) {
      document.getElementById('logs').innerHTML = data.logs.slice().reverse().map(l =>
        `<div class="msg">
          <div class="small muted">[${esc(l.time)}] Lượt #${esc(l.turn || '1')} (${esc(l.latency)})</div>
          <div>🧑 <b>Bạn:</b> ${esc(l.user)}</div>
          <div style="color:var(--accent);margin-top:4px">🤖 <b>AI:</b> ${esc(l.ai)}</div>
        </div>`
      ).join('');
    }
    if (data.events.length > 0) {
      document.getElementById('events').innerHTML = data.events.slice().reverse().map(e =>
        `<div class="evt"><span class="muted">[${esc(e.time)}]</span> <span class="badge">${esc(e.stage)}</span> ${esc(e.message)}</div>`
      ).join('');
    }
  } catch (err) {}
}

loadSettings().catch(e => toast('Không tải được cài đặt: ' + e, true));
refresh();
setInterval(refresh, 700);
