// extract_design.js — runs inside a rendered page and returns the page's
// measured design system as a JSON string: type scale, colour usage,
// spacing, containers, grids, radii, shadows, breakpoints, fonts and a
// section outline.
(() => {
  const MAX_SAMPLE = 60;
  const vw = window.innerWidth;
  const px = (v) => Math.round(parseFloat(v) * 100) / 100;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0;
  };
  const ownText = (el) =>
    Array.from(el.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent).join(' ').replace(/\s+/g, ' ').trim();
  const sample = (s) => (s.length > MAX_SAMPLE ? s.slice(0, MAX_SAMPLE) + '…' : s);
  const tally = (map, key, weight = 1, extra) => {
    if (!map[key]) map[key] = { count: 0, weight: 0, ...(extra || {}) };
    map[key].count += 1;
    map[key].weight += weight;
    return map[key];
  };
  const topN = (map, n, by = 'weight') =>
    Object.entries(map).sort((a, b) => b[1][by] - a[1][by]).slice(0, n).map(([k, v]) => ({ value: k, ...v }));

  const all = Array.from(document.body.querySelectorAll('*')).filter(visible);

  // ---------- Typography ----------
  const styles = {};
  const roles = {};
  for (const el of all) {
    const text = ownText(el);
    if (!text) continue;
    const cs = getComputedStyle(el);
    const key = [
      cs.fontFamily.split(',')[0].replace(/["']/g, '').trim(),
      px(cs.fontSize), cs.fontWeight,
      cs.lineHeight === 'normal' ? 'normal' : px(cs.lineHeight),
      cs.letterSpacing === 'normal' ? '0' : px(cs.letterSpacing),
      cs.textTransform, cs.fontStyle,
    ].join(' | ');
    const entry = tally(styles, key, text.length, { tags: {}, examples: [], colors: {} });
    entry.tags[el.tagName.toLowerCase()] = (entry.tags[el.tagName.toLowerCase()] || 0) + 1;
    entry.colors[cs.color] = (entry.colors[cs.color] || 0) + 1;
    if (entry.examples.length < 3) entry.examples.push(sample(text));
    const tag = el.tagName.toLowerCase();
    const role = /^h[1-6]$/.test(tag) ? tag
      : tag === 'p' ? 'p'
      : tag === 'a' ? 'link'
      : tag === 'button' || el.getAttribute('role') === 'button' ? 'button'
      : tag === 'li' ? 'li'
      : tag === 'label' || tag === 'small' || tag === 'figcaption' ? 'caption'
      : null;
    if (role && !roles[role]) {
      roles[role] = {
        fontFamily: cs.fontFamily, fontSize: px(cs.fontSize), fontWeight: cs.fontWeight,
        lineHeight: cs.lineHeight, letterSpacing: cs.letterSpacing, textTransform: cs.textTransform,
        color: cs.color, marginTop: cs.marginTop, marginBottom: cs.marginBottom, example: sample(text),
      };
    }
  }
  const typeStyles = topN(styles, 25).map((s) => {
    const [family, size, weight, lineHeight, letterSpacing, transform, style] = s.value.split(' | ');
    return { family, size: +size, weight, lineHeight, letterSpacing, transform, style, count: s.count, chars: s.weight, tags: s.tags, colors: s.colors, examples: s.examples };
  });
  const sizes = [...new Set(typeStyles.map((t) => t.size))].sort((a, b) => a - b);
  const ratios = sizes.slice(1).map((s, i) => Math.round((s / sizes[i]) * 1000) / 1000);

  // ---------- Colour ----------
  const textColors = {}, bgColors = {}, borderColors = {};
  for (const el of all) {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const area = r.width * r.height;
    if (ownText(el)) tally(textColors, cs.color, ownText(el).length);
    if (cs.backgroundColor !== 'rgba(0, 0, 0, 0)' && cs.backgroundColor !== 'transparent') tally(bgColors, cs.backgroundColor, area);
    if (parseFloat(cs.borderTopWidth) > 0 && cs.borderTopStyle !== 'none') tally(borderColors, cs.borderTopColor, r.width);
    if (cs.backgroundImage && cs.backgroundImage.includes('gradient')) tally(bgColors, cs.backgroundImage.slice(0, 160), area);
  }
  const bodyCs = getComputedStyle(document.body);
  const htmlCs = getComputedStyle(document.documentElement);

  // ---------- Spacing / radii / shadows ----------
  const spacing = {}, radii = {}, shadows = {}, gaps = {};
  for (const el of all) {
    const cs = getComputedStyle(el);
    for (const p of ['paddingTop', 'paddingBottom', 'paddingLeft', 'paddingRight', 'marginTop', 'marginBottom']) {
      const v = px(cs[p]);
      if (v > 0 && v < 400) tally(spacing, v);
    }
    if (cs.rowGap && cs.rowGap !== 'normal' && px(cs.rowGap) > 0) tally(gaps, px(cs.rowGap));
    if (cs.columnGap && cs.columnGap !== 'normal' && px(cs.columnGap) > 0) tally(gaps, px(cs.columnGap));
    if (cs.borderTopLeftRadius !== '0px') tally(radii, cs.borderTopLeftRadius);
    if (cs.boxShadow !== 'none') tally(shadows, cs.boxShadow.slice(0, 200));
  }
  const spacingValues = Object.keys(spacing).map(Number);
  const baseUnit = [8, 4, 6, 5].find((u) => {
    const fit = spacingValues.filter((v) => v % u === 0).reduce((s, v) => s + spacing[v].count, 0);
    const total = spacingValues.reduce((s, v) => s + spacing[v].count, 0);
    return total && fit / total >= 0.7;
  }) || null;

  // ---------- Layout ----------
  const containers = {};
  const grids = [];
  for (const el of all) {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (cs.maxWidth !== 'none' && r.width > 300 && r.width < vw && Math.abs((vw - r.right) - r.left) < 4) {
      tally(containers, `${cs.maxWidth} (rendered ${Math.round(r.width)}px, side padding ${cs.paddingLeft})`);
    }
    if ((cs.display === 'grid' || cs.display === 'inline-grid') && grids.length < 20) {
      grids.push({ tag: el.tagName.toLowerCase(), cls: sample(el.className && el.className.baseVal === undefined ? el.className : ''), columns: cs.gridTemplateColumns, rows: cs.gridTemplateRows.slice(0, 80), gap: `${cs.rowGap} / ${cs.columnGap}`, width: Math.round(r.width), children: el.children.length });
    }
  }
  const flexRows = all.filter((el) => {
    const cs = getComputedStyle(el);
    return cs.display.includes('flex') && cs.flexDirection.startsWith('row') && el.children.length >= 3 && el.getBoundingClientRect().width > vw * 0.5;
  }).slice(0, 15).map((el) => {
    const cs = getComputedStyle(el);
    return { tag: el.tagName.toLowerCase(), children: el.children.length, gap: cs.columnGap, justify: cs.justifyContent, align: cs.alignItems, wrap: cs.flexWrap, width: Math.round(el.getBoundingClientRect().width), childWidths: Array.from(el.children).slice(0, 6).map((c) => Math.round(c.getBoundingClientRect().width)) };
  });

  // ---------- Section outline (visual hierarchy) ----------
  const sectionCandidates = Array.from(document.querySelectorAll('header, nav, main > *, section, footer, body > div > *'))
    .filter((el) => visible(el) && el.getBoundingClientRect().width >= vw * 0.9 && el.getBoundingClientRect().height > 40);
  const seen = new Set();
  const outline = [];
  for (const el of sectionCandidates) {
    if ([...seen].some((s) => s.contains(el))) continue;
    seen.add(el);
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const headings = Array.from(el.querySelectorAll('h1,h2,h3,h4')).filter(visible).slice(0, 6).map((h) => `${h.tagName.toLowerCase()}: ${sample(h.textContent.replace(/\s+/g, ' ').trim())}`);
    outline.push({
      tag: el.tagName.toLowerCase(), top: Math.round(r.top + window.scrollY), height: Math.round(r.height),
      background: cs.backgroundColor, paddingY: `${cs.paddingTop} / ${cs.paddingBottom}`,
      headings, buttons: el.querySelectorAll('button, a[class*="btn"], a[class*="button"]').length,
      images: el.querySelectorAll('img, svg, picture, video').length, links: el.querySelectorAll('a').length,
    });
    if (outline.length >= 30) break;
  }
  outline.sort((a, b) => a.top - b.top);

  // ---------- Stylesheet-level info (same-origin sheets only) ----------
  const breakpoints = {}, fontFaces = [], cssVars = {};
  for (const sheet of Array.from(document.styleSheets)) {
    let rules;
    try { rules = sheet.cssRules; } catch (e) { continue; }
    const walk = (list) => {
      for (const rule of Array.from(list || [])) {
        if (rule.media && rule.conditionText !== undefined) {
          (rule.conditionText.match(/(min|max)-width:\s*[\d.]+(px|em|rem)/g) || []).forEach((m) => tally(breakpoints, m.replace(/\s+/g, '')));
          walk(rule.cssRules);
        } else if (rule.type === 5 && fontFaces.length < 20) {
          fontFaces.push({ family: rule.style.getPropertyValue('font-family'), weight: rule.style.getPropertyValue('font-weight'), style: rule.style.getPropertyValue('font-style') });
        } else if (rule.selectorText === ':root' && rule.style) {
          for (const prop of Array.from(rule.style)) if (prop.startsWith('--') && Object.keys(cssVars).length < 120) cssVars[prop] = rule.style.getPropertyValue(prop).trim().slice(0, 120);
        } else if (rule.cssRules) walk(rule.cssRules);
      }
    };
    walk(rules);
  }
  const loadedFonts = [];
  try { document.fonts.forEach((f) => { if (f.status === 'loaded') loadedFonts.push(`${f.family} ${f.weight} ${f.style}`); }); } catch (e) {}

  return JSON.stringify({
    url: location.href, title: document.title, viewport: { width: vw, height: window.innerHeight }, pageHeight: document.documentElement.scrollHeight,
    base: { fontFamily: bodyCs.fontFamily, fontSize: bodyCs.fontSize, lineHeight: bodyCs.lineHeight, color: bodyCs.color, background: bodyCs.backgroundColor !== 'rgba(0, 0, 0, 0)' ? bodyCs.backgroundColor : htmlCs.backgroundColor, rootFontSize: htmlCs.fontSize },
    typography: { roles, styles: typeStyles, sizes, sizeRatios: ratios },
    colors: { text: topN(textColors, 12), background: topN(bgColors, 12), border: topN(borderColors, 8) },
    spacing: { values: topN(spacing, 20, 'count').map((s) => ({ px: +s.value, count: s.count })), gaps: topN(gaps, 10, 'count').map((s) => ({ px: +s.value, count: s.count })), likelyBaseUnit: baseUnit },
    radii: topN(radii, 8, 'count').map((r) => ({ value: r.value, count: r.count })),
    shadows: topN(shadows, 6, 'count').map((s) => ({ value: s.value, count: s.count })),
    layout: { containers: topN(containers, 6, 'count').map((c) => ({ value: c.value, count: c.count })), grids, flexRows },
    outline, breakpoints: topN(breakpoints, 15, 'count').map((b) => ({ value: b.value, count: b.count })),
    fonts: { fontFaces, loaded: [...new Set(loadedFonts)].slice(0, 20) }, cssVariables: cssVars,
  }, null, 1);
})()
