'use strict';
/* Mobil fiş okuyucu: çek -> küçült -> yükle (sıralı kuyruk) -> kanıtlı sonuç -> düzelt.
 * Düz JS, bağımlılık yok. Sunucudan gelen her metin textContent ile yazılır (innerHTML yok). */
(() => {
  const API = '/api/v1';
  // Sunucu girdiyi 3200 px'e, düzeltilmiş fişi 2400 px'e küçültür. Fiş fotoğrafta dar bir şerit
  // olabileceğinden istemci 1600 px'e inmez; 2400 hem okunaklı kalır hem yüklemeyi küçültür.
  const MAX_SIDE = 2400;
  const JPEG_QUALITY = 0.85;
  const SMALL_FILE = 1.5 * 1024 * 1024; // küçük JPEG'i yeniden kodlamadan gönder
  const UPLOAD_TIMEOUT_MS = 180000; // CPU'da OCR saniyeler sürer
  const QUEUE_KEEP = 20; // ekranda tutulan en fazla satır
  // Eşikler yalnızca sentetik bozulmayla ayarlandı (örnek fiş + canvas bulanıklığı: varyans 13'te OCR hâlâ
  // doğru, 5'te toplam yanlış okundu). Gerçek telefon fotoğraflarıyla denenmedi; uyarı yalnızca öneridir.
  const BLUR_MIN = 10; // Laplace varyansı (en uzun kenarı 800 px'e indirilmiş kopyada) bunun altı: bulanık
  const DARK_P95_MAX = 110; // en parlak %5'lik dilim (kağıt) bunun altı: karanlık

  const CATEGORY_TR = {
    grocery: 'Market', food_drink: 'Yeme-içme', stationery: 'Kırtasiye', transport: 'Ulaşım',
    health: 'Sağlık', clothing: 'Giyim', electronics: 'Elektronik', other: 'Diğer',
  };
  const VERIFIED = 'verified';

  const $ = (id) => document.getElementById(id);

  /** Küçük DOM kurucu: h('div', {class:'a', text:'x', onclick: fn}, ...çocuklar). */
  function h(tag, props = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value == null || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key === 'text') el.textContent = value;
      else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? '' : value);
    }
    for (const kid of kids) if (kid != null) el.append(kid);
    return el;
  }

  class ApiError extends Error {}

  /** Sunucu çağrısı; hatalarda kullanıcıya gösterilebilir Türkçe ApiError fırlatır. */
  async function api(method, path, body, timeoutMs = 30000) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const isForm = body instanceof FormData;
      const res = await fetch(API + path, {
        method,
        headers: body && !isForm ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? (isForm ? body : JSON.stringify(body)) : undefined,
        signal: ctl.signal,
      });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        const detail = data && typeof data.detail === 'string' ? data.detail : 'İstek reddedildi (girdiyi kontrol edin).';
        throw new ApiError(detail);
      }
      return res.status === 204 ? null : await res.json();
    } catch (err) {
      if (err instanceof ApiError) throw err;
      throw new ApiError(err.name === 'AbortError' ? 'İstek zaman aşımına uğradı.' : 'Sunucuya ulaşılamadı. Bağlantıyı kontrol edin.');
    } finally {
      clearTimeout(timer);
    }
  }

  // ---------------------------------------------------------------- biçim
  function money(value, currency) {
    if (value == null) return '—';
    try {
      return new Intl.NumberFormat('tr-TR', { style: 'currency', currency: currency || 'TRY' }).format(Number(value));
    } catch (_) {
      return String(value);
    }
  }

  /** "1.234,50", "1234.5", "₺12,3" -> "1234.50". Boş -> null, anlaşılmaz -> false. */
  function parseMoney(text) {
    const s = text.replace(/[^\d.,]/g, '');
    if (!s) return null;
    if (/^\d+$/.test(s)) return s;
    let m = s.match(/^(\d*)[.,](\d{1,2})$/); // 12,50  .5  1234.5
    if (m) return `${m[1] || '0'}.${m[2]}`;
    // binlik gruplu: 1.234  1.234,50  1,234.50  1.234.567 (her grup tam 3 hane, ayraçlar aynı)
    m = s.match(/^(\d{1,3}(?:[.,]\d{3})+)(?:([.,])(\d{1,2}))?$/);
    if (!m) return false;
    const groupSeps = new Set(m[1].replace(/\d/g, ''));
    if (groupSeps.size !== 1 || (m[2] && groupSeps.has(m[2]))) return false;
    const digits = m[1].replace(/\D/g, '');
    return m[2] ? `${digits}.${m[3]}` : digits;
  }

  const toInput = (value) => (value == null ? '' : String(Number(value).toFixed(2)).replace('.', ','));
  const same2 = (a, b) => (a == null && b == null) || (a != null && b != null && Number(a).toFixed(2) === Number(b).toFixed(2));

  // ---------------------------------------------------------------- görüntü hazırlama
  /** Gri tonlamada bulanıklık (Laplace varyansı) ve parlaklık (p95) ölçer; uyarı metinleri döner. */
  function assess(canvas) {
    const scale = Math.min(1, 800 / Math.max(canvas.width, canvas.height));
    const w = Math.max(3, Math.round(canvas.width * scale));
    const hh = Math.max(3, Math.round(canvas.height * scale));
    const small = document.createElement('canvas');
    small.width = w;
    small.height = hh;
    const ctx = small.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(canvas, 0, 0, w, hh);
    const px = ctx.getImageData(0, 0, w, hh).data;
    const gray = new Float32Array(w * hh);
    const hist = new Uint32Array(256);
    for (let i = 0, j = 0; i < px.length; i += 4, j++) {
      const g = 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2];
      gray[j] = g;
      hist[g | 0]++;
    }
    let acc = 0;
    let p95 = 255;
    for (let v = 0; v < 256; v++) {
      acc += hist[v];
      if (acc >= gray.length * 0.95) { p95 = v; break; }
    }
    let sum = 0;
    let sumSq = 0;
    let n = 0;
    for (let y = 1; y < hh - 1; y++) {
      for (let x = 1; x < w - 1; x++) {
        const i = y * w + x;
        const lap = gray[i - 1] + gray[i + 1] + gray[i - w] + gray[i + w] - 4 * gray[i];
        sum += lap;
        sumSq += lap * lap;
        n++;
      }
    }
    const variance = n ? sumSq / n - (sum / n) ** 2 : 0;
    const notes = [];
    if (variance < BLUR_MIN) notes.push('Fotoğraf bulanık görünüyor; sonuç şüpheliyse tekrar çekin.');
    if (p95 < DARK_P95_MAX) notes.push('Fotoğraf karanlık görünüyor; ışığı artırıp tekrar çekmeyi deneyin.');
    return notes;
  }

  /** Fotoğrafı küçültüp JPEG'e çevirir; başarısız olursa özgün dosyayı gönderir. */
  async function prepare(file) {
    let bitmap;
    try {
      bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
    } catch (_) {
      return { blob: file, notes: [] };
    }
    try {
      const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(bitmap.width * scale);
      canvas.height = Math.round(bitmap.height * scale);
      canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      let notes = [];
      try { notes = assess(canvas); } catch (_) { /* ölçüm başarısızsa sessizce geç */ }
      if (scale === 1 && file.type === 'image/jpeg' && file.size <= SMALL_FILE) return { blob: file, notes };
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', JPEG_QUALITY));
      return { blob: blob || file, notes };
    } finally {
      bitmap.close();
    }
  }

  // ---------------------------------------------------------------- durum
  /** @type {{id:number, file:File, thumb:string, state:'waiting'|'reading'|'done'|'error', blob?:Blob, notes:string[], warnings:string[], result?:object, error?:string}[]} */
  let queue = [];
  let review = [];
  let reviewTotal = 0;
  let seq = 0;
  let current = null; // açık ayrıntı: {id, receipt, reconciliation, fromReview, notes}

  function toast(text) {
    const el = $('toast');
    el.textContent = text;
    el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { el.hidden = true; }, 3500);
  }

  const pill = (kind, text) => h('span', { class: `pill ${kind}`, text });
  const statusPill = (status) => (status === VERIFIED ? pill('ok', '✓ Mutabık') : pill('warn', '⚠ Şüpheli'));

  // ---------------------------------------------------------------- kuyruk
  function renderQueue() {
    const list = $('queue');
    list.replaceChildren(...queue.slice().reverse().map((item) => {
      const rec = item.result && item.result.receipt;
      const thumb = h('img', { src: item.thumb, alt: '', loading: 'lazy' });
      let title = 'Fiş';
      let sub = '';
      let right = null;
      if (item.state === 'waiting') { sub = 'Sırada'; right = pill('busy', 'Bekliyor'); }
      else if (item.state === 'reading') { sub = 'Okunuyor…'; right = pill('busy', 'Okunuyor'); }
      else if (item.state === 'error') {
        title = 'Okunamadı';
        sub = item.error || 'Hata';
        right = h('span', { class: 'retry', role: 'button', tabindex: '0', text: 'Tekrar dene',
          onclick: (e) => { e.stopPropagation(); retry(item); } });
      } else if (rec) {
        title = rec.merchant_name || 'İşletme okunamadı';
        sub = item.notes[0] || rec.date || 'Tarih okunamadı';
        right = h('div', { class: 'right' },
          h('div', { class: 'amt', text: money(rec.total_amount, rec.currency) }),
          statusPill(rec.reconciliation_status));
      }
      const row = h('button', { class: 'item', type: 'button' },
        thumb, h('div', {}, h('div', { class: 't', text: title }), h('div', { class: 's', text: sub })), right);
      if (item.state === 'done' && rec) row.addEventListener('click', () => openDetail(rec.id, { notes: [...item.notes, ...item.warnings] }));
      return h('li', {}, row);
    }));
  }

  function enqueue(files) {
    for (const file of files) {
      if (!file.type.startsWith('image/')) { toast('Yalnızca fotoğraf seçin.'); continue; }
      queue.push({ id: ++seq, file, thumb: URL.createObjectURL(file), state: 'waiting', notes: [], warnings: [] });
    }
    trimQueue();
    renderQueue();
    pump();
  }

  function trimQueue() {
    while (queue.length > QUEUE_KEEP) {
      const idx = queue.findIndex((i) => i.state === 'done' || i.state === 'error');
      if (idx === -1) break;
      URL.revokeObjectURL(queue[idx].thumb);
      queue.splice(idx, 1);
    }
  }

  function retry(item) {
    item.state = 'waiting';
    item.error = undefined;
    renderQueue();
    pump();
  }

  let busy = false;
  /** Fişleri tek tek yollar: sunucu tek OCR motoruyla çalışır, art arda istek yarar sağlamaz. */
  async function pump() {
    if (busy) return;
    busy = true;
    try {
      for (let item = queue.find((i) => i.state === 'waiting'); item; item = queue.find((i) => i.state === 'waiting')) {
        await processItem(item);
      }
    } finally {
      busy = false;
    }
  }

  async function processItem(item) {
    item.state = 'reading';
    renderQueue();
    try {
      if (!item.blob) {
        const prepared = await prepare(item.file);
        item.blob = prepared.blob;
        item.notes = prepared.notes;
      }
      const form = new FormData();
      form.append('file', item.blob, 'fis.jpg');
      item.result = await api('POST', '/extract-receipt', form, UPLOAD_TIMEOUT_MS);
      item.warnings = item.result.warnings || [];
      item.state = 'done';
      loadReview();
    } catch (err) {
      item.state = 'error';
      item.error = err.message;
    }
    renderQueue();
  }

  // ---------------------------------------------------------------- inceleme listesi
  async function loadReview() {
    try {
      const data = await api('GET', '/expenses?status=flagged_for_review&page_size=100');
      review = data.items;
      reviewTotal = data.total;
    } catch (err) {
      toast(err.message);
      return;
    }
    const badge = $('badge');
    badge.textContent = String(reviewTotal);
    badge.hidden = reviewTotal === 0;
    renderReview();
  }

  function renderReview() {
    $('review-empty').hidden = review.length > 0;
    $('review').replaceChildren(...review.map((rec) => {
      const row = h('button', { class: 'item', type: 'button' },
        h('img', { src: `${API}/expenses/${rec.id}/image`, alt: '', loading: 'lazy' }),
        h('div', {}, h('div', { class: 't', text: rec.merchant_name || 'İşletme okunamadı' }),
          h('div', { class: 's', text: rec.date || 'Tarih okunamadı' })),
        h('div', { class: 'right' }, h('div', { class: 'amt', text: money(rec.total_amount, rec.currency) }), statusPill(rec.reconciliation_status)));
      row.addEventListener('click', () => openDetail(rec.id, { fromReview: true }));
      return h('li', {}, row);
    }));
  }

  // ---------------------------------------------------------------- ayrıntı / düzeltme
  const form = $('form');
  const sheet = $('sheet');

  async function openDetail(id, { fromReview = false, notes = [] } = {}) {
    try {
      const data = await api('GET', `/expenses/${id}`);
      current = { id, receipt: data.receipt, reconciliation: data.reconciliation, fromReview, notes };
    } catch (err) {
      toast(err.message);
      return;
    }
    fillSheet();
    $('photo').open = false;
    $('photo-img').removeAttribute('src');
    if (!sheet.open) sheet.showModal();
  }

  function fillSheet() {
    const { receipt: r, reconciliation: rec, notes, fromReview } = current;
    const ok = r.reconciliation_status === VERIFIED;
    const verdict = $('verdict');
    verdict.className = `verdict ${ok ? 'ok' : 'warn'}`;
    verdict.replaceChildren(ok ? '✓ Mutabık' : '⚠ Şüpheli', h('small', { text: rec.message }));
    $('notes').replaceChildren(...notes.map((n) => h('li', { text: n })));
    form.merchant_name.value = r.merchant_name || '';
    form.date.value = r.date || '';
    form.subtotal_amount.value = toInput(r.subtotal_amount);
    form.tax_amount.value = toInput(r.tax_amount);
    form.total_amount.value = toInput(r.total_amount);
    form.currency.value = r.currency;
    form.category.value = r.category || '';
    $('form-error').hidden = true;
    $('skip').hidden = !(fromReview && review.length > 1);
    $('save').disabled = false;
    $('delete').disabled = false;
  }

  /** Yalnızca değişen alanları döndürür: matrahı olduğu gibi göndermek onu "elle" saymasın. */
  function collectChanges() {
    const r = current.receipt;
    const changes = {};
    const text = (name, orig) => {
      const value = form[name].value.trim();
      if (value !== (orig || '')) changes[name] = value || null;
    };
    text('merchant_name', r.merchant_name);
    text('date', r.date);
    for (const name of ['subtotal_amount', 'tax_amount', 'total_amount']) {
      const parsed = parseMoney(form[name].value);
      if (parsed === false) throw new ApiError('Tutar anlaşılamadı. Örnek: 1.234,50');
      if (!same2(parsed, r[name])) changes[name] = parsed;
    }
    if (form.currency.value !== r.currency) changes.currency = form.currency.value;
    if (form.category.value !== (r.category || '')) changes.category = form.category.value || null;
    return changes;
  }

  function showFormError(message) {
    const el = $('form-error');
    el.textContent = message;
    el.hidden = false;
  }

  async function nextReview(afterId) {
    await loadReview();
    const next = review.find((rec) => rec.id !== afterId);
    if (next) openDetail(next.id, { fromReview: true });
    else sheet.close();
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    $('form-error').hidden = true;
    let changes;
    try {
      changes = collectChanges();
    } catch (err) {
      showFormError(err.message);
      return;
    }
    if (Object.keys(changes).length === 0) {
      showFormError('Değişiklik yok. Yanlış alanı düzeltip kaydedin.');
      return;
    }
    const { id, fromReview, notes } = current;
    $('save').disabled = true;
    try {
      await api('PATCH', `/expenses/${id}`, changes);
      const data = await api('GET', `/expenses/${id}`);
      current = { id, receipt: data.receipt, reconciliation: data.reconciliation, fromReview, notes };
    } catch (err) {
      showFormError(err.message);
      $('save').disabled = false;
      return;
    }
    for (const item of queue) if (item.result && item.result.receipt.id === id) item.result.receipt = current.receipt;
    renderQueue();
    if (current.receipt.reconciliation_status === VERIFIED) {
      toast('Kaydedildi: mutabık ✓');
      if (fromReview) await nextReview(id);
      else { sheet.close(); loadReview(); }
    } else {
      toast('Kaydedildi, ancak hâlâ şüpheli.');
      fillSheet();
      loadReview();
    }
  });

  $('skip').addEventListener('click', () => {
    const idx = review.findIndex((rec) => rec.id === current.id);
    const next = review[(idx + 1) % review.length];
    if (next && next.id !== current.id) openDetail(next.id, { fromReview: true });
  });

  $('delete').addEventListener('click', async () => {
    if (!window.confirm('Bu fiş silinsin mi? Geri alınamaz.')) return;
    const { id, fromReview } = current;
    $('delete').disabled = true;
    try {
      await api('DELETE', `/expenses/${id}`);
    } catch (err) {
      showFormError(err.message);
      $('delete').disabled = false;
      return;
    }
    for (const item of queue.filter((i) => i.result && i.result.receipt.id === id)) URL.revokeObjectURL(item.thumb);
    queue = queue.filter((item) => !(item.result && item.result.receipt.id === id));
    renderQueue();
    toast('Fiş silindi.');
    if (fromReview) await nextReview(id);
    else { sheet.close(); loadReview(); }
  });

  $('photo').addEventListener('toggle', () => {
    if ($('photo').open && current) $('photo-img').src = `${API}/expenses/${current.id}/image`;
  });
  $('close').addEventListener('click', () => sheet.close());
  sheet.addEventListener('click', (event) => { if (event.target === sheet) sheet.close(); });

  // ---------------------------------------------------------------- girdiler, sekmeler, kurulum
  for (const id of ['cam', 'gallery']) {
    $(id).addEventListener('change', (event) => {
      enqueue([...event.target.files]);
      event.target.value = ''; // aynı dosya yeniden seçilebilsin
    });
  }

  function showView(name) {
    $('view-capture').hidden = name !== 'capture';
    $('view-review').hidden = name !== 'review';
    for (const btn of document.querySelectorAll('.tabs button')) {
      if (btn.dataset.view === name) btn.setAttribute('aria-current', 'page');
      else btn.removeAttribute('aria-current');
    }
    if (name === 'review') loadReview();
  }
  for (const btn of document.querySelectorAll('.tabs button')) btn.addEventListener('click', () => showView(btn.dataset.view));

  let installEvent = null;
  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    installEvent = event;
    $('install').hidden = false;
  });
  $('install').addEventListener('click', async () => {
    if (!installEvent) return;
    installEvent.prompt();
    await installEvent.userChoice;
    installEvent = null;
    $('install').hidden = true;
  });

  // Service worker yalnızca güvenli bağlamda (HTTPS ya da localhost) vardır; yoksa sayfa yine çalışır.
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});

  loadReview();
})();
