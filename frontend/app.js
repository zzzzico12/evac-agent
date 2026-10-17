// 避難計画エージェント（PWA）
// 家族の情報と避難計画はこの端末の localStorage にだけ保存する。サーバ側には保存しない。
const LS_HOUSEHOLDS = 'evac.households.v1';
const LS_PLANS = 'evac.plans.v1';
const LS_HAZARD = 'evac.hazard.v1';
const LS_HERE = 'evac.here.v1'; // いまいる場所の自分（場所・年齢・移動・言語・メモ）
const SELF_ID = 'SELF';

const state = {
  cfg: null,
  households: [],
  plans: {}, // 世帯ID -> { plan, alert, savedAt }（端末に保存）
  runs: {}, // 世帯ID -> { status, events }（今回の警報の実行状況。保存しない）
  shelters: {},
  cities: [],
  rivers: {}, // 河川名 -> { cities, ... }（data/alerts.json）
  area: null, // 警報を取得する地域（市町村）
  level: 4,
  hazard: 'flood', // デモ用の警報で選んでいる災害の種類（flood / landslide）
  here: null, // { lat, lng, address, city, age, mobility, language, notes }
  hereDraft: null, // いまいる場所の登録画面で編集中の値
  cityCodes: {}, // 市町村コード（5桁）→ 市町村名
  riverLines: [], // 主な河川の流路（OpenStreetMap）。洪水で川を渡るかの判定に使う
  alert: null,
  selected: null,
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
// モデルの発言に絵文字や Markdown の記号が混ざっても、そのまま表示しない
const plain = (s) => String(s ?? '')
  .replace(/[\p{Extended_Pictographic}\u{FE0F}\u{200D}]/gu, '')
  .replace(/\*\*(.+?)\*\*/g, '$1')
  .replace(/^#{1,6}\s*/gm, '')
  .replace(/^\s*\|?\s*:?-{3,}.*$/gm, '')
  .replace(/^---+$/gm, '')
  .replace(/\n{3,}/g, '\n\n')
  .trim();

// ---------------- 端末内の保存 ----------------
function load(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v ? JSON.parse(v) : fallback;
  } catch {
    return fallback;
  }
}
function save(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (err) {
    console.warn('保存できませんでした', err);
  }
}
const saveHouseholds = () => save(LS_HOUSEHOLDS, state.households);
const savePlans = () => save(LS_PLANS, state.plans);

// ---------------- 地図 ----------------
// 避難所が県内で約2,400件あるので Canvas で描く
const map = L.map('map', { zoomControl: false, preferCanvas: true }).setView([36.3, 140.25], 9);
L.control.zoom({ position: 'bottomleft' }).addTo(map);
L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png', {
  attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>',
  maxZoom: 18,
}).addTo(map);
const layers = {
  shelters: L.layerGroup(),
  nearby: L.layerGroup().addTo(map),
  homes: L.layerGroup().addTo(map),
  route: L.layerGroup().addTo(map),
};
const DEPTH_COLORS = { 0: '#0f766e', 1: '#c9a227', 2: '#e8913c', 3: '#e0603a', 4: '#c2410c', 5: '#9d174d', 6: '#6b21a8' };
const SHELTER_MIN_ZOOM = 12; // 県全体を見ているときは避難所の点を出さない

const LANDSLIDE_COLORS = { 0: '#0f766e', 1: '#d4a017', 2: '#c2410c' };
const GSI = 'https://disaportaldata.gsi.go.jp/raster';
const GSI_ATTR = '<a href="https://disaportal.gsi.go.jp/hazardmap/copyright/opendata.html">国土地理院 重ねるハザードマップ</a>';

// 災害の種類ごとの、地図の重ね方・避難所の安全の判定・表示の言葉
const HAZARDS = {
  flood: {
    label: '洪水',
    safeText: '浸水想定なし',
    nearbySub: '浸水想定がなく、川を渡らずに行ける避難所',
    isSafe: (s) => s.flood_depth_rank === 0 && s.nearby_max_depth_rank < 3,
    unsafeText: (s) => `浸水想定区域内（${s.flood_depth}）`,
    color: (s) => DEPTH_COLORS[s.flood_depth_rank],
    statusText: (s) => `想定浸水深：${s.flood_depth}`,
    layers: () => [L.tileLayer(`${GSI}/01_flood_l2_shinsuishin_data/{z}/{x}/{y}.png`, { opacity: 0.5, maxNativeZoom: 17, maxZoom: 18, attribution: `洪水浸水想定区域：${GSI_ATTR}` })],
    legend: '<b>想定浸水深</b>' + [['#F7F5A9', '0.5m未満'], ['#FFD8C0', '0.5〜3m'], ['#FFB7B7', '3〜5m'], ['#FF9191', '5〜10m'], ['#F285C9', '10〜20m'], ['#DC7ADC', '20m以上']]
      .map(([c, t]) => `<div><i style="background:${c}"></i>${t}</div>`).join(''),
  },
  landslide: {
    label: '土砂災害',
    safeText: '土砂災害警戒区域外',
    nearbySub: '土砂災害警戒区域外の避難所',
    isSafe: (s) => s.landslide_rank === 0 && s.nearby_landslide_rank === 0,
    unsafeText: (s) => (s.landslide_rank ? s.landslide_zone : '土砂災害警戒区域のすぐそば'),
    color: (s) => LANDSLIDE_COLORS[s.landslide_rank],
    statusText: (s) => `土砂災害：${s.landslide_zone}`,
    layers: () => ['05_dosekiryukeikaikuiki', '05_kyukeishakeikaikuiki', '05_jisuberikeikaikuiki'].map((id) =>
      L.tileLayer(`${GSI}/${id}/{z}/{x}/{y}.png`, { opacity: 0.6, maxNativeZoom: 17, maxZoom: 18, attribution: `土砂災害警戒区域：${GSI_ATTR}` })),
    legend: '<b>土砂災害</b>' + [['#E6C832', '警戒区域（土石流）'], ['#FAE600', '警戒区域（急傾斜地）'], ['#FF9900', '警戒区域（地すべり）'], ['#C2002F', '特別警戒区域']]
      .map(([c, t]) => `<div><i style="background:${c}"></i>${t}</div>`).join(''),
  },
};
// 地図・近くの避難所で使う災害の種類：警報を取得したらその種類、まだならデモの選択
const currentHazard = () => state.alert?.hazard || state.hazard;

const hazardView = { key: null, layers: [], legend: null };
function setHazardView(key) {
  if (hazardView.key === key) return;
  hazardView.layers.forEach((l) => l.remove());
  hazardView.layers = HAZARDS[key].layers();
  hazardView.layers.forEach((l) => l.addTo(map));
  if (!hazardView.legend) {
    hazardView.legend = L.control({ position: 'bottomright' });
    hazardView.legend.onAdd = () => L.DomUtil.create('div', 'legend');
    hazardView.legend.addTo(map);
  }
  hazardView.legend.getContainer().innerHTML = HAZARDS[key].legend;
  hazardView.key = key;
  drawShelters();
}

function drawShelters() {
  layers.shelters.clearLayers();
  const hz = HAZARDS[currentHazard()];
  for (const s of Object.values(state.shelters)) {
    const kinds = [
      s.designated_shelter === '対応' && '指定避難所', s.emergency_place === '対応' && '指定緊急避難場所', s.welfare_shelter === '対応' && '福祉避難所',
    ].filter(Boolean).join('・');
    L.circleMarker([s.lat, s.lng], {
      radius: s.designated_shelter === '対応' ? 5 : 3.5, color: '#fff', weight: 1.5,
      fillColor: hz.color(s) || '#8a96a3', fillOpacity: 1,
    }).bindTooltip(`<b>${esc(s.name)}</b><br>${esc(s.city)}・${esc(kinds || '避難施設')}<br>想定浸水深：${esc(s.flood_depth)}<br>土砂災害：${esc(s.landslide_zone)}`).addTo(layers.shelters);
  }
}

function toggleShelters() {
  if (map.getZoom() >= SHELTER_MIN_ZOOM) layers.shelters.addTo(map);
  else layers.shelters.remove();
}
map.on('zoomend', toggleShelters);

// いまいる場所の自分を、エージェントに渡す世帯と同じ形にする
function selfProfile() {
  const h = state.here;
  if (!h) return null;
  return {
    id: SELF_ID,
    name: 'いまいる場所の自分',
    situation: 'current_location',
    address: h.address,
    lat: h.lat,
    lng: h.lng,
    city: h.city,
    language: h.language || 'ja',
    home: { structure: '外出先（建物の種類や階数は不明）', floors: 1 },
    members: [{
      relation: '本人',
      age: h.age ? Number(h.age) : null,
      mobility: h.mobility || '不明',
      notes: [h.notes, !h.age && '年齢や移動のしやすさは未入力'].filter(Boolean).join('。'),
    }],
    pets: [],
    vehicles: [],
    contacts: [],
  };
}
const findEntity = (id) => (id === SELF_ID ? selfProfile() : state.households.find((x) => x.id === id));

function drawHomes() {
  layers.homes.clearLayers();
  const me = selfProfile();
  if (me) {
    L.marker([me.lat, me.lng], {
      icon: L.divIcon({ className: '', html: '<div class="mk-here"></div>', iconSize: [18, 18], iconAnchor: [9, 9] }),
      zIndexOffset: 1100,
    }).bindTooltip('いまいる場所', { direction: 'top', offset: [0, -10] }).on('click', () => select(SELF_ID)).addTo(layers.homes);
  }
  for (const h of state.households) {
    const sel = h.id === state.selected;
    L.marker([h.lat, h.lng], {
      // iconSize を null にして、名前の長さに合わせた大きさで描く
      icon: L.divIcon({ className: 'mk-home-wrap', html: `<div class="mk-home ${sel ? 'selected' : ''}">${esc(h.name)}</div>`, iconSize: null }),
      zIndexOffset: sel ? 1000 : 0,
    }).on('click', () => select(h.id)).addTo(layers.homes);
  }
}

// ---------------- 最寄りの避難所 ----------------
// エージェントを呼ばずに端末内で計算する（警報の前でも、オフラインでも見られる）
function distanceM(a, b) {
  const R = 6371000, rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad, dLng = (b.lng - a.lng) * rad;
  const x = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(x));
}
const formatDistance = (m) => (m < 1000 ? `${Math.round(m / 10) * 10}m` : `${(m / 1000).toFixed(1)}km`);
const isSafe = (s) => HAZARDS[currentHazard()].isSafe(s);

// 2地点の間で渡らなければならない川。直線が川を奇数回横切るなら反対側にある（agent/geo.py と同じ考え方）
function riversCrossed(a, b) {
  const cross = (o, p, q) => (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0]);
  const A = [a.lat, a.lng], B = [b.lat, b.lng];
  const [lo0, hi0] = [Math.min(A[0], B[0]), Math.max(A[0], B[0])];
  const [lo1, hi1] = [Math.min(A[1], B[1]), Math.max(A[1], B[1])];
  const counts = {};
  for (const r of state.riverLines) {
    const [y0, x0, y1, x1] = r.bbox;
    if (y1 < lo0 || y0 > hi0 || x1 < lo1 || x0 > hi1) continue;
    let n = 0;
    for (let i = 1; i < r.coords.length; i++) {
      const p = r.coords[i - 1], q = r.coords[i];
      if (cross(p, q, A) * cross(p, q, B) < 0 && cross(A, B, p) * cross(A, B, q) < 0) n++;
    }
    if (n) counts[r.name] = (counts[r.name] || 0) + n;
  }
  return Object.keys(counts).filter((k) => counts[k] % 2 === 1);
}

function nearbyShelters(h, count = 3) {
  // 14市町村は指定避難所かどうかがすべて「不明」なので、「対応無し」と明記されたものだけを外す
  const designated = Object.values(state.shelters)
    .filter((s) => s.designated_shelter !== '対応無し')
    .map((s) => ({ ...s, distance: distanceM(h, s) }))
    .sort((a, b) => a.distance - b.distance)
    .slice(0, 60);
  // 洪水のときは、増水した川を渡らずに行けることも「安全」の条件にする
  const flood = currentHazard() === 'flood';
  designated.forEach((s) => { s.rivers = flood ? riversCrossed(h, s) : []; });
  const ok = (s) => isSafe(s) && !s.rivers.length;
  const safe = designated.filter(ok).slice(0, count);
  const nearest = designated[0];
  // いちばん近い避難所が危険な区域にあるか川の対岸なら、あえて「使えない最寄り」として示す
  const unsafeNearest = nearest && !ok(nearest) && nearest.distance < (safe[0]?.distance ?? Infinity) ? nearest : null;
  if (unsafeNearest) unsafeNearest.reason = isSafe(unsafeNearest) ? `${unsafeNearest.rivers.join('・')}の対岸` : HAZARDS[currentHazard()].unsafeText(unsafeNearest);
  return { safe, unsafeNearest };
}

function drawNearby() {
  layers.nearby.clearLayers();
  const h = findEntity(state.selected);
  if (!h) return;
  const { safe, unsafeNearest } = nearbyShelters(h);
  safe.forEach((s, i) => {
    L.marker([s.lat, s.lng], {
      icon: L.divIcon({ className: '', html: `<div class="mk-num">${i + 1}</div>`, iconSize: [26, 26], iconAnchor: [13, 13] }),
      zIndexOffset: 500,
    }).bindTooltip(`<b>${esc(s.name)}</b><br>${esc(s.city)}・${formatDistance(s.distance)}・${HAZARDS[currentHazard()].safeText}`, { direction: 'top', offset: [0, -12] })
      .addTo(layers.nearby);
  });
  if (unsafeNearest) {
    L.marker([unsafeNearest.lat, unsafeNearest.lng], {
      icon: L.divIcon({ className: '', html: '<div class="mk-num unsafe"></div>', iconSize: [22, 22], iconAnchor: [11, 11] }),
      zIndexOffset: 400,
    }).bindTooltip(`<b>${esc(unsafeNearest.name)}</b><br>最寄りですが${esc(unsafeNearest.reason)}`, { direction: 'top', offset: [0, -10] })
      .addTo(layers.nearby);
  }
}

function renderNearby() {
  const h = findEntity(state.selected);
  $('#nearbyBlock').hidden = !h;
  if (!h) return;
  const { safe, unsafeNearest } = nearbyShelters(h);
  const hz = HAZARDS[currentHazard()];
  $('#nearbySub').textContent = hz.nearbySub;
  const items = safe.map((s, i) => `
    <li data-id="${esc(s.id)}">
      <span class="num">${i + 1}</span>
      <span class="body"><span class="name">${esc(s.name)}</span><span class="meta">${esc(s.city)}　${s.designated_shelter === '対応' ? '指定避難所' : '指定の有無は不明'}${s.welfare_shelter === '対応' ? '・福祉避難所' : ''}　${hz.safeText}</span></span>
      <span class="dist">${formatDistance(s.distance)}</span>
    </li>`).join('');
  const warn = unsafeNearest ? `
    <li class="unsafe" data-id="${esc(unsafeNearest.id)}">
      <span class="num"></span>
      <span class="body"><span class="name">${esc(unsafeNearest.name)}</span><span class="meta">最寄りですが${esc(unsafeNearest.reason)}。${hz.label}のときは使わない</span></span>
      <span class="dist">${formatDistance(unsafeNearest.distance)}</span>
    </li>` : '';
  $('#nearbyList').innerHTML = items + warn || `<li class="none">近くに${hz.nearbySub}が見つかりません</li>`;
  $('#nearbyList').querySelectorAll('li[data-id]').forEach((li) => li.addEventListener('click', () => {
    const s = state.shelters[li.dataset.id];
    map.flyTo([s.lat, s.lng], 16, { duration: 0.6 });
  }));
}

function drawRoute() {
  layers.route.clearLayers();
  const h = findEntity(state.selected);
  if (!h) return;
  const plan = state.plans[h.id]?.plan;
  if (!plan) {
    // 計画がまだないときは、自宅と近くの安全な避難所が収まる範囲を表示する
    const pts = [[h.lat, h.lng], ...nearbyShelters(h).safe.map((s) => [s.lat, s.lng])];
    if (pts.length > 1) map.flyToBounds(pts, { padding: [70, 70], duration: 0.6, maxZoom: 15 });
    else map.flyTo([h.lat, h.lng], 14, { duration: 0.6 });
    return;
  }
  const pts = [[h.lat, h.lng]];
  if (plan.backup_shelter) {
    L.polyline([[h.lat, h.lng], [plan.backup_shelter.lat, plan.backup_shelter.lng]], { color: '#0b3b5c', weight: 2.5, dashArray: '5 7', opacity: 0.7 })
      .bindTooltip(`予備：${esc(plan.backup_shelter.name)}`).addTo(layers.route);
    pts.push([plan.backup_shelter.lat, plan.backup_shelter.lng]);
  }
  if (plan.primary_shelter) {
    L.polyline([[h.lat, h.lng], [plan.primary_shelter.lat, plan.primary_shelter.lng]], { color: '#0f766e', weight: 5, opacity: 0.95 }).addTo(layers.route);
    L.circleMarker([plan.primary_shelter.lat, plan.primary_shelter.lng], { radius: 9, color: '#0f766e', weight: 3, fillColor: '#fff', fillOpacity: 1 })
      .bindTooltip(esc(plan.primary_shelter.name), { permanent: true, direction: 'top', offset: [0, -8] })
      .addTo(layers.route);
    pts.push([plan.primary_shelter.lat, plan.primary_shelter.lng]);
  }
  if (pts.length > 1) map.flyToBounds(pts, { padding: [60, 60], duration: 0.8, maxZoom: 15 });
  else map.flyTo([h.lat, h.lng], 15, { duration: 0.6 });
}

// ---------------- 家族 ----------------
const STATUS_LABEL = { idle: '', running: '計画中', done: '計画あり', error: 'エラー', skip: '対象外' };
// 地域（市町村）に警報を出す河川。複数あるときは data/alerts.json に書いた順で最初のもの
const riverFor = (city) => Object.keys(state.rivers).find((r) => state.rivers[r].cities.includes(city));

function householdStatus(h) {
  if (state.runs[h.id]) return state.runs[h.id].status;
  if (state.alert) return 'skip';
  return state.plans[h.id] ? 'done' : 'idle';
}

function renderHouseholds() {
  const el = $('#householdList');
  el.innerHTML = '';
  const me = selfProfile();
  if (!me && !state.households.length) {
    el.innerHTML = `<p class="empty">避難計画を作る場所を登録してください。外出先なら「いまいる場所」、自宅などは「住所を登録」から登録します。住所では家族構成も登録できます。</p>`;
    return;
  }
  const rows = [];
  if (me) {
    const m = me.members[0];
    rows.push({ id: me.id, name: 'いまいる場所', meta: `${me.address}${m.age ? `　${m.age}歳・${m.mobility}` : ''}`, edit: () => openHereDialog() });
  }
  for (const h of state.households) {
    rows.push({ id: h.id, name: h.name, meta: `${h.city}　${h.members.map((x) => `${x.relation}${x.age}`).join('・')}`, edit: () => openEditor(h.id) });
  }
  for (const r of rows) {
    const status = householdStatus({ id: r.id });
    const row = document.createElement('div');
    row.className = 'family' + (r.id === state.selected ? ' selected' : '') + (status === 'skip' ? ' dim' : '');
    row.innerHTML = `
      <span class="name">${esc(r.name)}</span>
      <span class="meta">${esc(r.meta)}</span>
      <span class="side">${STATUS_LABEL[status] ? `<span class="status ${status}">${STATUS_LABEL[status]}</span>` : ''}<button class="edit">編集</button></span>`;
    row.addEventListener('click', () => select(r.id));
    row.querySelector('.edit').addEventListener('click', (ev) => { ev.stopPropagation(); r.edit(); });
    el.appendChild(row);
  }
}

function select(id) {
  state.selected = id;
  // 警報を取得する地域は、選んでいる場所の市町村
  const entity = findEntity(id);
  if (entity) state.area = entity.city;
  setButtons(!Object.values(state.runs).some((r) => r.status === 'running'));
  renderHouseholds();
  drawHomes();
  drawNearby();
  renderNearby();
  renderSteps();
  renderPlan();
  drawRoute();
}

// ---------------- エージェントの進み具合 ----------------
// ストリームのイベントを「手順」の一覧にまとめる（推論の要約は表示しない）
function buildSteps(run) {
  const steps = [];
  for (const e of run.events) {
    const body = e.body || {};
    if (e.kind === 'tool_call') {
      steps.push({ cls: 'active', tool: body.tool, label: e.title, detail: '' });
    } else if (e.kind === 'tool_result' || e.kind === 'tool_error') {
      const st = steps.find((x) => x.tool === body.tool && x.cls === 'active') || steps.findLast((x) => x.cls === 'active');
      if (st) {
        st.cls = e.kind === 'tool_result' ? 'done' : 'error';
        st.detail = e.kind === 'tool_result' ? plain(e.title) : `うまくいかなかったため、やり直します（${plain(body.error)}）`;
      }
    } else if (e.kind === 'say') {
      steps.push({ cls: 'note', label: '', detail: plain(body) });
    } else if (e.kind === 'status' && body.state === 'error') {
      steps.push({ cls: 'error', label: 'エラー', detail: plain(body.error || e.title) });
    }
  }
  if (run.status === 'running' && !steps.some((x) => x.cls === 'active')) {
    steps.push({ cls: 'active', label: steps.length ? '考えています' : 'エージェントを起動しています', detail: '' });
  }
  if (run.status === 'done') steps.push({ cls: 'done', label: '避難計画ができました', detail: '' });
  return steps;
}

function renderSteps() {
  const run = state.runs[state.selected];
  $('#progressBlock').hidden = !run;
  if (!run) return;
  const h = findEntity(state.selected);
  $('#progressTarget').textContent = h?.name || '';
  const steps = buildSteps(run);
  // 計画ができたら手順は1行に折りたたみ、すぐ下の避難計画を見やすくする
  if (run.status === 'done' && !run.expanded) {
    const n = steps.filter((st) => st.cls === 'done' && st.label !== '避難計画ができました').length;
    $('#steps').innerHTML = `<li class="done"><span class="label">${n}つの手順で避難計画を作りました</span><button class="link" id="expandSteps">手順を見る</button></li>`;
    $('#expandSteps').addEventListener('click', () => { run.expanded = true; renderSteps(); });
    return;
  }
  $('#steps').innerHTML = steps.map((st) => `
    <li class="${st.cls}">${st.label ? `<span class="label">${esc(st.label)}</span>` : ''}${st.detail ? `<span class="detail">${esc(st.detail)}</span>` : ''}</li>`).join('')
    + (run.status === 'done' ? '<li class="collapse"><button class="link" id="collapseSteps">手順を閉じる</button></li>' : '');
  $('#collapseSteps')?.addEventListener('click', () => { run.expanded = false; renderSteps(); });
}

// ---------------- 避難計画 ----------------
function renderPlan() {
  const el = $('#plan');
  const saved = state.plans[state.selected];
  const run = state.runs[state.selected];
  // 計画があるときだけ表示する（作成中は「エージェント」の欄で進み具合を見せる）
  $('#planBlock').hidden = !saved || run?.status === 'running';
  if ($('#planBlock').hidden) return;
  $('#planTarget').textContent = findEntity(state.selected)?.name || '';
  const p = saved.plan;
  const t = (v) => esc(plain(v));
  const shelter = (s) => (s ? `${esc(s.name)}<br><span class="muted">${esc(s.city || '')}</span>` : 'なし');
  el.innerHTML = `
    <div class="plan">
      <div class="plan-head">
        <span class="pill decision">${t(p.decision)}</span>
        <span class="pill ${p.urgency === '今すぐ' ? 'urgent' : ''}">${t(p.urgency)}</span>
      </div>
      <h3>${t(p.headline)}</h3>
      <p class="lead">${t(p.risk_assessment)}</p>
      <div class="facts">
        <div class="fact"><div class="k">避難先</div><div class="v">${shelter(p.primary_shelter)}</div></div>
        <div class="fact"><div class="k">予備の避難先</div><div class="v">${shelter(p.backup_shelter)}</div></div>
        <div class="fact wide"><div class="k">出発</div><div class="v">${t(p.depart_by)}</div></div>
        <div class="fact wide"><div class="k">移動手段</div><div class="v">${t(p.transport)}</div></div>
      </div>
      ${p.route_notes ? `<div class="section"><h4>経路の注意</h4><p>${t(p.route_notes)}</p></div>` : ''}
      ${(p.member_assignments || []).length ? `<div class="section"><h4>役割分担</h4><ul class="list">${p.member_assignments.map((m) => `<li><b>${t(m.member)}</b>${t(m.role)}</li>`).join('')}</ul></div>` : ''}
      ${(p.timeline || []).length ? `<div class="section"><h4>行動の順番</h4><ul class="list timeline">${p.timeline.map((x) => `<li><span class="when">${t(x.time)}</span><span>${t(x.action)}</span></li>`).join('')}</ul></div>` : ''}
      ${(p.checklist || []).length ? `<div class="section"><h4>持ち出し品</h4><ul class="list checks">${p.checklist.map((c) => `<li>${t(c)}</li>`).join('')}</ul></div>` : ''}
      <div class="section"><h4>家族へのメッセージ</h4>
        <div class="message">${t(p.family_message)}</div>
        ${p.translated_message ? `<div class="message">${t(p.translated_message)}</div>` : ''}
      </div>
      <p class="saved">この端末に保存済み（${esc(saved.alert?.title || '')}、${new Date(saved.savedAt).toLocaleString('ja-JP', { dateStyle: 'short', timeStyle: 'short' })}）。オフラインでも見られます。</p>
    </div>`;
}

// ---------------- エージェント呼び出し（ストリーミング） ----------------
function applyEvent(h, e) {
  if (e.kind === 'ping') return; // 接続を保つための信号（表示しない）
  const run = state.runs[h.id];
  run.events.push(e);
  if (e.kind === 'status') {
    if (e.body?.state === 'running') run.status = 'running';
    if (e.body?.state === 'done') run.status = 'done';
    if (e.body?.state === 'error') run.status = 'error';
  }
  if (e.kind === 'plan') {
    state.plans[h.id] = { plan: e.body, alert: { title: state.alert.title, alertLevel: state.alert.alertLevel }, savedAt: Date.now() };
    savePlans();
  }
  renderHouseholds();
  if (h.id === state.selected) {
    renderSteps();
    if (e.kind === 'plan' || e.kind === 'status') renderPlan();
    if (e.kind === 'status' && e.body?.state === 'done') {
      drawRoute();
      $('#planBlock').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }
}

async function runAgent(h, alert) {
  state.runs[h.id] = { status: 'running', events: [] };
  renderHouseholds();
  const fail = (msg) => applyEvent(h, { kind: 'status', title: 'エラー', body: { state: 'error', error: msg }, ts: Date.now() });
  let res;
  try {
    const body = JSON.stringify({ household: h, alert });
    // CloudFront（OAC）経由で Lambda に POST するときは、本文の SHA-256 をヘッダーに付ける必要がある
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(body));
    const sha256 = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
    res = await fetch(state.cfg.agentUrl, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-amz-content-sha256': sha256 },
      body,
    });
  } catch (err) {
    return fail(`通信できませんでした：${err.message}`);
  }
  if (!res.ok || !res.body) return fail(`HTTP ${res.status} ${await res.text().catch(() => '')}`);

  // AgentCore の SSE（"data: {...}\n\n"）を1件ずつ取り出す
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const data = chunk.split('\n').filter((l) => l.startsWith('data:')).map((l) => l.slice(5).trim()).join('');
      if (!data) continue;
      try {
        const ev = JSON.parse(data);
        if (ev.kind) applyEvent(h, ev);
        else if (ev.error) fail(ev.error);
      } catch {
        /* 途中で切れた行は無視 */
      }
    }
  }
  if (state.runs[h.id].status === 'running') fail('ストリームが途中で終了しました');
}

// ---------------- 警報の取得（デモ用の疑似警報） ----------------
const fill = (tpl, vars) => tpl.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? '');

// 選んだ災害の種類・地域・警戒レベルの疑似警報を data/alerts.json から組み立てる
async function fetchAlert() {
  const t = await fetch('data/alerts.json', { cache: 'no-store' }).then((r) => r.json());
  const hazard = state.hazard;
  const spec = t[hazard].levels[String(state.level)];
  let vars, cities, river = null;
  if (hazard === 'flood') {
    // 洪水は、いまいる地域を流れる河川の洪水予報。対象はその河川の沿川市町村
    river = Object.keys(t.rivers).find((r) => t.rivers[r].cities.includes(state.area));
    if (!river) return null;
    vars = { river, ...t.rivers[river] };
    cities = t.rivers[river].cities;
  } else {
    // 土砂災害は市町村ごとに発表される
    vars = { city: state.area };
    cities = [state.area];
  }
  const now = new Date();
  const iso = (d) => new Date(d.getTime() + 9 * 3600e3).toISOString().slice(0, 16) + '+09:00';
  return {
    alertId: crypto.randomUUID(),
    demo: true,
    hazard,
    hazardLabel: HAZARDS[hazard].label,
    infoKind: t[hazard].infoKind,
    publisher: t.publisher,
    reportDatetime: iso(now),
    area: { prefecture: '茨城県', cities },
    requestedArea: state.area,
    river,
    level: state.level,
    title: fill(spec.title, vars),
    alertLevel: spec.alertLevel,
    headline: fill(spec.headline, vars),
    expectedPeakAt: iso(new Date(now.getTime() + spec.floodInMinutes * 60e3)),
  };
}

async function getAlertAndPlan() {
  const entity = findEntity(state.selected);
  if (!entity) return;
  state.area = entity.city;
  setButtons(false);
  let alert;
  try {
    alert = await fetchAlert();
  } catch (err) {
    setButtons(true);
    window.alert(`警報を取得できませんでした：${err.message}`);
    return;
  }
  if (!alert) {
    setButtons(true);
    window.alert(`現在、${state.area}に${HAZARDS[state.hazard].label}の警報は出ていません。`);
    return;
  }
  alert.receivedAt = Date.now();
  state.alert = alert;
  state.runs = {};
  setHazardView(alert.hazard);
  renderBanner(alert);

  // 選んでいる場所について、エージェントで計画を立てる
  select(entity.id);
  setButtons(false);
  $('#fetchHint').textContent = `${entity.id === SELF_ID ? 'いまいる場所' : entity.name}の避難計画を作っています。`;
  await runAgent(entity, alert);
  setButtons(true);
}

function renderBanner(alert) {
  const el = $('#alertBanner');
  el.style.setProperty('--lvc', `var(--lv${alert.level})`);
  el.innerHTML = `
    <span class="demo-tag">デモ用の疑似警報</span>
    <span class="level">${esc(alert.alertLevel.replace(/（.*）/, ''))}</span>
    <span class="title">${esc(alert.title)}　${esc((alert.alertLevel.match(/（(.*)）/) || [])[1] || '')}</span>
    <span class="desc">${esc(alert.headline)}　${esc(alert.reportDatetime.slice(11, 16))} 発表　対象：${esc(alert.area.cities.join('・'))}</span>`;
  el.hidden = false;
}

function setButtons(enabled) {
  const ok = enabled && navigator.onLine && Boolean(findEntity(state.selected));
  document.querySelectorAll('#fetchAlert, #confirmAlert').forEach((b) => (b.disabled = !ok));
  if (enabled) {
    $('#fetchHint').textContent = !navigator.onLine
      ? 'オフラインのため、計画は作れません。保存済みの計画は見られます。'
      : !findEntity(state.selected) ? '先に、手順1で場所を登録してください。' : '';
  }
}

function resetRun() {
  Object.assign(state, { alert: null, runs: {} });
  $('#alertBanner').hidden = true;
  setHazardView(state.hazard);
  select(state.selected);
}

function renderAlertControls() {
  const entity = findEntity(state.selected);
  $('#alertTarget').textContent = entity ? `${entity.id === SELF_ID ? 'いまいる場所' : entity.name}（${entity.city}）` : '';
  const river = riverFor(state.area);
  $('#areaRiver').textContent = state.hazard === 'flood'
    ? (river ? `${river}の洪水予報が出ます（対象：${state.rivers[river].cities.join('・')}）` : '')
    : `${state.area}に土砂災害の警報が出ます（市町村ごとに発表）`;
  const hz = $('#hazards');
  hz.innerHTML = Object.entries(HAZARDS).map(([k, v]) => `<button class="hazard${k === state.hazard ? ' on' : ''}" data-value="${k}">${v.label}</button>`).join('');
  hz.querySelectorAll('button').forEach((b) => b.addEventListener('click', (ev) => {
    ev.preventDefault();
    chooseHazard(b.dataset.value);
  }));
  const el = $('#levels');
  el.innerHTML = [3, 4, 5].map((v) => `<button class="level${v === state.level ? ' on' : ''}" data-value="${v}">レベル${v}</button>`).join('');
  el.querySelectorAll('button').forEach((b) => b.addEventListener('click', (ev) => {
    ev.preventDefault();
    state.level = Number(b.dataset.value);
    renderAlertControls();
  }));
}

function openAlertDialog() {
  if (!findEntity(state.selected)) {
    window.alert('先に、手順1で場所を登録してください。');
    return;
  }
  renderAlertControls();
  $('#alertDialog').showModal();
}

function chooseHazard(key) {
  state.hazard = key;
  save(LS_HAZARD, key);
  renderAlertControls();
  if (!state.alert) {
    setHazardView(key);
    drawNearby();
    renderNearby();
    drawRoute();
  }
}

// ---------------- いまいる場所の登録 ----------------
// 保存を押すまでは下書き（hereDraft）に入れておく
function openHereDialog() {
  state.hereDraft = state.here ? { ...state.here } : null;
  const h = state.hereDraft || {};
  $('#selfAge').value = h.age ?? '';
  $('#selfMobility').value = h.mobility || '自立';
  $('#selfLanguage').value = h.language || 'ja';
  $('#selfNotes').value = h.notes || '';
  $('.more').open = Boolean(h.age || h.notes);
  $('#deleteHere').hidden = !state.here;
  renderHereText();
  if (!$('#hereDialog').open) $('#hereDialog').showModal();
}

function renderHereText(text) {
  const d = state.hereDraft;
  $('#hereText').textContent = text || (d ? `${d.address}（${d.city}）` : '場所がまだ決まっていません');
  $('#hereText').classList.toggle('unset', !text && !d);
}

async function setHere(lat, lng) {
  renderHereText('場所を調べています…');
  try {
    const r = await fetch(`https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress?lat=${lat}&lon=${lng}`).then((x) => x.json());
    const city = state.cityCodes[r.results?.muniCd];
    if (!city) throw new Error('茨城県内の場所を指定してください');
    state.hereDraft = { ...(state.hereDraft || {}), lat, lng, city, address: `${city}${r.results.lv01Nm || ''}` };
    map.flyTo([lat, lng], 15, { duration: 0.6 });
  } catch (err) {
    window.alert(`いまいる場所を決められませんでした：${err.message}`);
  }
  renderHereText();
}

function useGps() {
  if (!navigator.geolocation) { window.alert('この端末では現在地を取得できません。「地図で指定」を使ってください。'); return; }
  renderHereText('現在地を取得しています…');
  navigator.geolocation.getCurrentPosition(
    (pos) => setHere(Number(pos.coords.latitude.toFixed(5)), Number(pos.coords.longitude.toFixed(5))),
    (err) => { window.alert(`現在地を取得できませんでした（${err.message}）。「地図で指定」を使ってください。`); renderHereText(); },
    { enableHighAccuracy: true, timeout: 10000 },
  );
}

function pickOnMap() {
  $('#hereDialog').close();
  document.body.classList.add('picking');
  $('#pickHint').hidden = false;
  const done = () => { document.body.classList.remove('picking'); $('#pickHint').hidden = true; $('#hereDialog').showModal(); };
  const onClick = async (e) => { done(); await setHere(Number(e.latlng.lat.toFixed(5)), Number(e.latlng.lng.toFixed(5))); };
  map.once('click', onClick);
  $('#cancelPick').onclick = () => { map.off('click', onClick); done(); };
}

function saveHere(ev) {
  ev.preventDefault();
  if (!state.hereDraft) {
    window.alert('「現在地を使う」か「地図で指定」で、場所を決めてください。');
    return;
  }
  state.here = {
    ...state.hereDraft,
    age: Number($('#selfAge').value) || null,
    mobility: $('#selfMobility').value,
    language: $('#selfLanguage').value,
    notes: $('#selfNotes').value.trim(),
  };
  save(LS_HERE, state.here);
  $('#hereDialog').close();
  select(SELF_ID);
}

function deleteHere() {
  if (!confirm('いまいる場所と、その避難計画を端末から削除しますか？')) return;
  state.here = null;
  delete state.plans[SELF_ID];
  save(LS_HERE, null);
  savePlans();
  $('#hereDialog').close();
  select(state.households[0]?.id);
}

// ---------------- 世帯の登録・編集（端末に保存） ----------------
const LANGS = { ja: '日本語', vi: 'ベトナム語', en: '英語', zh: '中国語', ko: '韓国語', pt: 'ポルトガル語', tl: 'タガログ語', ne: 'ネパール語', id: 'インドネシア語' };
const MOBILITY = ['自立', '歩行がゆっくり', '車いす', '乳幼児', '寝たきり'];

const linesToList = (text, key1, key2) =>
  text.split('\n').map((l) => l.trim()).filter(Boolean).map((l) => {
    const [a, ...rest] = l.split(/\s+/);
    return { [key1]: a, [key2]: rest.join(' ') };
  });
const listToLines = (list, key1, key2) => (list || []).map((x) => [x[key1], x[key2]].filter(Boolean).join(' ')).join('\n');

function memberRow(m = {}) {
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td><input name="relation" placeholder="父" value="${esc(m.relation)}" required></td>
    <td><input name="age" type="number" min="0" max="120" value="${esc(m.age)}" required></td>
    <td><select name="mobility">${MOBILITY.map((v) => `<option ${m.mobility === v ? 'selected' : ''}>${v}</option>`).join('')}</select></td>
    <td><input name="notes" placeholder="持病・服薬・配慮" value="${esc(m.notes)}"></td>
    <td><button type="button" class="del" title="削除">×</button></td>`;
  tr.querySelector('.del').addEventListener('click', () => tr.remove());
  return tr;
}

function openEditor(id) {
  const h = state.households.find((x) => x.id === id) || {
    id: 'H' + Date.now().toString(36), name: '', address: '', lat: '', lng: '', city: '', language: 'ja',
    home: { structure: '木造2階建て', floors: 2 }, members: [{ relation: '本人', age: 40, mobility: '自立', notes: '' }],
    pets: [], vehicles: [], contacts: [],
  };
  const f = $('#editor form');
  f.dataset.id = h.id;
  f.hhname.value = h.name;
  f.address.value = h.address;
  f.lat.value = h.lat;
  f.lng.value = h.lng;
  f.city.value = h.city;
  f.language.value = h.language;
  f.structure.value = h.home?.structure || '';
  f.floors.value = h.home?.floors || 1;
  f.pets.value = listToLines(h.pets, 'type', 'notes');
  f.vehicles.value = listToLines(h.vehicles, 'type', 'notes');
  f.contacts.value = listToLines(h.contacts, 'name', 'relation');
  const tbody = $('#members tbody');
  tbody.innerHTML = '';
  h.members.forEach((m) => tbody.appendChild(memberRow(m)));
  $('#deleteHousehold').hidden = !state.households.some((x) => x.id === h.id);
  $('#geoMsg').textContent = '';
  $('#editor').showModal();
}

async function geocode() {
  const f = $('#editor form');
  const q = f.address.value.trim().replace(/（.*?）/g, '');
  if (!q) return;
  $('#geoMsg').textContent = '検索中…';
  try {
    const r = await fetch(`https://msearch.gsi.go.jp/address-search/AddressSearch?q=${encodeURIComponent(q.startsWith('茨城県') ? q : '茨城県' + q)}`).then((x) => x.json());
    if (!r.length) throw new Error('見つかりませんでした');
    const [lng, lat] = r[0].geometry.coordinates;
    const title = r[0].properties.title;
    f.lat.value = lat.toFixed(5);
    f.lng.value = lng.toFixed(5);
    const m = title.match(/^茨城県(?:[^市町村]+郡)?(.+?[市町村])/);
    if (m) f.city.value = m[1];
    $('#geoMsg').textContent = `${title}（${lat.toFixed(4)}, ${lng.toFixed(4)}）`;
  } catch (err) {
    $('#geoMsg').textContent = `位置を取得できませんでした：${err.message}`;
  }
}

function submitEditor(ev) {
  ev.preventDefault();
  const f = ev.target;
  if (!state.cities.includes(f.city.value)) {
    $('#geoMsg').textContent = '市町村は茨城県内の市町村名を入力してください（住所から位置を取得すると自動で入ります）';
    return;
  }
  const h = {
    id: f.dataset.id,
    name: f.hhname.value.trim(),
    address: f.address.value.trim(),
    lat: Number(f.lat.value),
    lng: Number(f.lng.value),
    city: f.city.value,
    language: f.language.value,
    home: { structure: f.structure.value.trim(), floors: Number(f.floors.value) },
    members: [...$('#members tbody').rows].map((tr) => ({
      relation: tr.querySelector('[name=relation]').value.trim(),
      age: Number(tr.querySelector('[name=age]').value),
      mobility: tr.querySelector('[name=mobility]').value,
      notes: tr.querySelector('[name=notes]').value.trim(),
    })),
    pets: linesToList(f.pets.value, 'type', 'notes'),
    vehicles: linesToList(f.vehicles.value, 'type', 'notes'),
    contacts: linesToList(f.contacts.value, 'name', 'relation'),
  };
  const i = state.households.findIndex((x) => x.id === h.id);
  if (i >= 0) state.households[i] = h; else state.households.push(h);
  saveHouseholds();
  $('#editor').close();
  select(h.id);
}

function deleteHousehold() {
  const id = $('#editor form').dataset.id;
  if (!confirm('この世帯と保存済みの避難計画を端末から削除しますか？')) return;
  state.households = state.households.filter((x) => x.id !== id);
  delete state.plans[id];
  saveHouseholds();
  savePlans();
  $('#editor').close();
  select(state.households[0]?.id);
}

// ---------------- 起動 ----------------
async function boot() {
  state.households = load(LS_HOUSEHOLDS, []);
  state.plans = load(LS_PLANS, {});

  const [cfg, shelterData, alerts, cityCodes, riverData] = await Promise.all([
    fetch('config.json').then((r) => r.json()),
    fetch('data/shelters.json').then((r) => r.json()),
    fetch('data/alerts.json').then((r) => r.json()),
    fetch('data/cities.json').then((r) => r.json()),
    fetch('data/rivers.json').then((r) => r.json()),
  ]);
  state.riverLines = riverData.rivers.map((r) => {
    const lats = r.coords.map((c) => c[0]), lngs = r.coords.map((c) => c[1]);
    return { ...r, bbox: [Math.min(...lats), Math.min(...lngs), Math.max(...lats), Math.max(...lngs)] };
  });
  state.cfg = cfg;
  state.cityCodes = cityCodes;
  state.here = load(LS_HERE, null);
  state.rivers = alerts.rivers;
  for (const s of shelterData.shelters) state.shelters[s.id] = s;
  state.cities = [...new Set(shelterData.shelters.map((s) => s.city))];
  $('#cityList').innerHTML = state.cities.map((c) => `<option value="${esc(c)}">`).join('');
  $('#editor [name=language]').innerHTML = Object.entries(LANGS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('');
  state.hazard = HAZARDS[load(LS_HAZARD, 'flood')] ? load(LS_HAZARD, 'flood') : 'flood';
  setHazardView(state.hazard);
  toggleShelters();

  // 手順1：場所の登録（いまいる場所／住所）
  $('#useHere').addEventListener('click', openHereDialog);
  $('#useGps').addEventListener('click', useGps);
  $('#pickOnMap').addEventListener('click', pickOnMap);
  $('#hereDialog form').addEventListener('submit', saveHere);
  $('#cancelHere').addEventListener('click', () => $('#hereDialog').close());
  $('#deleteHere').addEventListener('click', deleteHere);
  $('#selfMobility').innerHTML = MOBILITY.map((v) => `<option>${v}</option>`).join('');
  $('#selfLanguage').innerHTML = Object.entries(LANGS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('');
  // 手順2：警報を取得して計画を作る（デモ用の警報の種類とレベルを選ぶ）
  $('#fetchAlert').addEventListener('click', openAlertDialog);
  $('#cancelAlert').addEventListener('click', () => $('#alertDialog').close());
  $('#alertDialog form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    $('#alertDialog').close();
    getAlertAndPlan();
  });
  $('#addHousehold').addEventListener('click', () => openEditor(null));
  $('#geocodeBtn').addEventListener('click', geocode);
  $('#addMember').addEventListener('click', () => $('#members tbody').appendChild(memberRow()));
  $('#editor form').addEventListener('submit', submitEditor);
  $('#cancelEdit').addEventListener('click', () => $('#editor').close());
  $('#deleteHousehold').addEventListener('click', deleteHousehold);
  window.addEventListener('online', () => setButtons(true));
  window.addEventListener('offline', () => setButtons(false));

  // 最初は、いまいる場所があればそれ、なければ最初に登録した住所を選ぶ
  resetRun();
  select(state.here ? SELF_ID : state.households[0]?.id);
}

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch((err) => console.warn('Service Worker を登録できませんでした', err));
}

boot().catch((err) => {
  console.error(err);
  $('#plan').innerHTML = `<p class="plan-empty">初期化に失敗しました：${esc(err.message)}</p>`;
});
