"""JavaScript executado num mundo isolado da página (Page.createIsolatedWorld).

O mundo isolado compartilha o DOM com a página, mas não as variáveis globais: scripts da página
não enxergam nem alteram o mapa de referências (ref_N) mantido aqui.
"""

INSTALL = r"""
(() => {
  if (globalThis.__ag) return;
  const INTERACTIVE_ROLES = new Set(['button','link','textbox','searchbox','checkbox','radio','combobox',
    'listbox','option','menuitem','menuitemcheckbox','menuitemradio','tab','switch','slider','spinbutton','treeitem']);
  const SKIP_TAGS = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','META','LINK','HEAD','SVG','PATH','BR']);
  const byRef = new Map();
  const refOf = new WeakMap();
  let nextId = 1;

  const clean = (s, n = 100) => {
    s = (s || '').replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  };
  const refFor = (el) => {
    let r = refOf.get(el);
    if (!r) { r = 'ref_' + (nextId++); refOf.set(el, r); byRef.set(r, new WeakRef(el)); }
    return r;
  };
  const get = (ref) => {
    const w = byRef.get(ref);
    const el = w && w.deref();
    if (!el || !el.isConnected) return null;
    return el;
  };
  const role = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.split(' ')[0];
    const tag = el.tagName;
    switch (tag) {
      case 'A': return el.hasAttribute('href') ? 'link' : '';
      case 'BUTTON': case 'SUMMARY': return 'button';
      case 'SELECT': return el.multiple ? 'listbox' : 'combobox';
      case 'TEXTAREA': return 'textbox';
      case 'OPTION': return 'option';
      case 'IMG': return 'img';
      case 'NAV': return 'navigation';
      case 'MAIN': return 'main';
      case 'HEADER': return 'banner';
      case 'FOOTER': return 'contentinfo';
      case 'ASIDE': return 'complementary';
      case 'FORM': return 'form';
      case 'DIALOG': return 'dialog';
      case 'UL': case 'OL': return 'list';
      case 'LI': return 'listitem';
      case 'TABLE': return 'table';
      case 'TR': return 'row';
      case 'TD': return 'cell';
      case 'TH': return 'columnheader';
      case 'LABEL': return 'label';
      case 'IFRAME': return 'iframe';
      case 'H1': case 'H2': case 'H3': case 'H4': case 'H5': case 'H6': return 'heading';
      case 'INPUT': {
        const t = (el.getAttribute('type') || 'text').toLowerCase();
        if (t === 'hidden') return '';
        if (['button','submit','reset','image'].includes(t)) return 'button';
        if (t === 'checkbox') return 'checkbox';
        if (t === 'radio') return 'radio';
        if (t === 'range') return 'slider';
        if (t === 'number') return 'spinbutton';
        if (t === 'search') return 'searchbox';
        if (t === 'file') return 'button';
        return 'textbox';
      }
    }
    if (el.isContentEditable && (!el.parentElement || !el.parentElement.isContentEditable)) return 'textbox';
    return '';
  };
  const isInteractive = (el, r) => INTERACTIVE_ROLES.has(r) || el.hasAttribute('onclick') ||
    (el.tabIndex >= 0 && el.hasAttribute('tabindex'));
  const labelText = (el) => {
    if (el.labels && el.labels.length) return Array.from(el.labels).map(l => l.innerText).join(' ');
    return '';
  };
  const name = (el, r) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map(id => { const n = document.getElementById(id); return n ? n.innerText : ''; }).join(' ');
      if (clean(t)) return clean(t);
    }
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') {
      const t = (el.getAttribute('type') || '').toLowerCase();
      if (['button','submit','reset'].includes(t)) return clean(el.value);
      return clean(labelText(el) || el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '');
    }
    if (el.tagName === 'IMG') return clean(el.getAttribute('alt') || el.getAttribute('title') || '');
    if (['link','button','heading','option','tab','menuitem','label','cell','columnheader','listitem','checkbox','radio'].includes(r))
      return clean(el.innerText || el.textContent || el.getAttribute('title') || '');
    return clean(el.getAttribute('title') || '');
  };
  const ownText = (el) => {
    let s = '';
    for (const n of el.childNodes) if (n.nodeType === 3) s += n.textContent;
    return clean(s, 200);
  };
  const rendered = (el) => {
    if (!el.getClientRects().length) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity || '1') > 0;
  };
  const inViewport = (el) => {
    const b = el.getBoundingClientRect();
    return b.bottom > 0 && b.right > 0 && b.top < innerHeight && b.left < innerWidth;
  };
  const extras = (el, r) => {
    const out = [];
    if (r === 'link' && el.href) out.push('href="' + clean(el.href, 200) + '"');
    if (r === 'heading') out.push('level=' + el.tagName.slice(1));
    if (['textbox','searchbox','spinbutton','combobox','slider'].includes(r)) {
      const v = el.isContentEditable ? el.innerText : el.value;
      if (v) out.push('value="' + clean(v, 80) + '"');
      const t = el.getAttribute && el.getAttribute('type');
      if (t && t !== 'text') out.push('type=' + t);
    }
    if (r === 'checkbox' || r === 'radio' || r === 'switch') out.push(el.checked || el.getAttribute('aria-checked') === 'true' ? 'checked' : 'unchecked');
    if (el.disabled) out.push('disabled');
    if (el.getAttribute('aria-expanded')) out.push('expanded=' + el.getAttribute('aria-expanded'));
    return out.length ? ' ' + out.join(' ') : '';
  };
  const lineFor = (el, r, indent) => {
    const label = r || el.tagName.toLowerCase();
    const n = name(el, r) || (r ? '' : ownText(el));
    return '  '.repeat(indent) + '- ' + label + (n ? ' "' + n.replace(/"/g, '\\"') + '"' : '') +
      ' [' + refFor(el) + ']' + extras(el, r);
  };
  const children = (el) => {
    const list = Array.from(el.children);
    if (el.shadowRoot) list.unshift(...el.shadowRoot.children);
    return list;
  };

  const readPage = ({ filter, depth, ref, limit }) => {
    const maxDepth = depth == null ? 1e9 : depth;
    const lines = [];
    let size = 0, truncated = false;
    const root = ref ? get(ref) : document.body || document.documentElement;
    if (!root) return { error: 'stale_ref' };
    const walk = (el, d) => {
      if (truncated || SKIP_TAGS.has(el.tagName.toUpperCase())) return;
      if (!rendered(el) && el.tagName !== 'OPTION') {
        // contêineres invisíveis com filhos fixos/visíveis ainda são percorridos
        if (getComputedStyle(el).display === 'none') return;
      }
      const r = role(el);
      const interactive = isInteractive(el, r);
      let notable = false;
      if (rendered(el)) {
        if (filter === 'interactive') notable = interactive;
        else notable = (r && r !== 'list' && r !== 'listitem' && r !== 'row' && r !== 'cell') || interactive || !!ownText(el);
        if (notable && filter !== 'all' && filter !== 'interactive' && !inViewport(el)) notable = false;
      }
      let nextDepth = d;
      if (notable) {
        if (d > maxDepth) return;
        const line = lineFor(el, r, filter === 'interactive' ? 0 : d);
        size += line.length + 1;
        if (size > limit) { truncated = true; return; }
        lines.push(line);
        nextDepth = d + 1;
        if (interactive && ['link','button','textbox','searchbox','combobox','option','checkbox','radio'].includes(r)) return;
      }
      for (const c of children(el)) walk(c, nextDepth);
    };
    walk(root, 0);
    return { text: lines.join('\n'), truncated, scrollY: Math.round(scrollY), height: document.documentElement.scrollHeight, viewport: [innerWidth, innerHeight] };
  };

  const find = ({ query, max }) => {
    const words = clean(query, 200).toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(w => w.length > 1);
    const results = [];
    const all = document.querySelectorAll('*');
    for (const el of all) {
      if (SKIP_TAGS.has(el.tagName.toUpperCase()) || !rendered(el)) continue;
      const r = role(el);
      const interactive = isInteractive(el, r);
      if (!interactive && !r && !ownText(el)) continue;
      const hay = [name(el, r), ownText(el), r, el.tagName, el.getAttribute('placeholder'), el.getAttribute('type'),
        el.getAttribute('name'), el.id, el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('alt')]
        .filter(Boolean).join(' ').toLowerCase();
      let score = 0;
      for (const w of words) if (hay.includes(w)) score += 2;
      if (!score) continue;
      if (interactive) score += 1;
      if (inViewport(el)) score += 0.5;
      results.push({ el, r, score });
    }
    results.sort((a, b) => b.score - a.score);
    const seen = new Set();
    const out = [];
    for (const { el, r } of results) {
      if (out.length >= max) break;
      if (seen.has(el.parentElement) && !isInteractive(el, r)) continue;
      seen.add(el);
      const b = el.getBoundingClientRect();
      out.push(lineFor(el, r, 0) + ' at (' + Math.round(b.left + b.width / 2) + ', ' + Math.round(b.top + b.height / 2) + ')' +
        (inViewport(el) ? '' : ' off-screen'));
    }
    return { text: out.join('\n') };
  };

  const pageText = ({ limit }) => {
    const candidates = ['article', 'main', '[role=main]'].map(s => document.querySelector(s)).filter(Boolean);
    const best = candidates.find(c => (c.innerText || '').trim().length > 200);
    const text = ((best || document.body || document.documentElement).innerText || '').replace(/\n{3,}/g, '\n\n').trim();
    return { text: text.length > limit ? text.slice(0, limit) + '\n[…texto truncado]' : text, truncated: text.length > limit };
  };

  const center = ({ ref }) => {
    const el = get(ref);
    if (!el) return { error: 'stale_ref' };
    let b = el.getBoundingClientRect();
    if (b.top < 0 || b.left < 0 || b.bottom > innerHeight || b.right > innerWidth) {
      el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
      b = el.getBoundingClientRect();
    }
    if (b.width === 0 && b.height === 0) return { error: 'not_visible' };
    return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) };
  };

  const scrollTo = ({ ref }) => {
    const el = get(ref);
    if (!el) return { error: 'stale_ref' };
    el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
    return { ok: true };
  };

  const formInput = ({ ref, value }) => {
    const el = get(ref);
    if (!el) return { error: 'stale_ref' };
    const fire = () => {
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    };
    if (el.tagName === 'SELECT') {
      const want = String(value);
      const opt = Array.from(el.options).find(o => o.value === want) ||
        Array.from(el.options).find(o => clean(o.text).toLowerCase() === want.toLowerCase());
      if (!opt) return { error: 'option_not_found', options: Array.from(el.options).map(o => clean(o.text, 60)).slice(0, 30) };
      el.value = opt.value; fire();
      return { ok: true, set: clean(opt.text) };
    }
    if (el.tagName === 'INPUT' && ['checkbox', 'radio'].includes((el.type || '').toLowerCase())) {
      const want = typeof value === 'boolean' ? value : ['true', '1', 'on', 'yes', 'sim'].includes(String(value).toLowerCase());
      if (el.checked !== want) el.click();
      return { ok: true, set: el.checked };
    }
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      const proto = el.tagName === 'INPUT' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
      el.focus();
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, String(value));
      fire();
      return { ok: true, set: clean(el.value, 80) };
    }
    if (el.isContentEditable) {
      el.focus(); el.textContent = String(value);
      el.dispatchEvent(new InputEvent('input', { bubbles: true }));
      return { ok: true };
    }
    return { error: 'not_a_form_field' };
  };

  globalThis.__ag = { readPage, find, pageText, center, scrollTo, formInput, get,
    setNext: (n) => { if (n > nextId) nextId = n; }, next: () => nextId };
})();
"""
