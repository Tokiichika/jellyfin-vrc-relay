/* Layout enhancements only. Authentication and media behavior live in each page. */
(() => {
  'use strict';
  const icons = {
    overview: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    add: '<path d="M12 5v14M5 12h14"/>',
    library: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m10 8 6 4-6 4Z"/>',
    settings: '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>'
  };
  if (document.body.classList.contains('dashboard-page')) {
    const nav = document.createElement('nav');
    nav.className = 'app-nav'; nav.setAttribute('aria-label', '管理导航');
    for (const [icon, label, href] of [['overview','概览','#stats'],['add','添加视频','#create-section'],['library','放映列表','#library-heading'],['settings','设置','#settings']]) {
      const a = document.createElement('a'); a.href = href;
      a.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">${icons[icon]}</svg>`;
      a.append(document.createTextNode(label));
      nav.append(a);
    }
    document.querySelector('#message').after(nav);
  }
  const fields = document.getElementById('fields');
  if (fields) {
    // Group labels with inputs after the authenticated schema has rendered.
    // No polling or perpetual animation is used.
    const decorate = () => {
      const nav = document.getElementById('settings-nav'); nav.replaceChildren();
      let n = 0;
      for (const section of fields.children) {
        if (!section.dataset.decorated) {
          section.dataset.decorated = 'true';
          const grid = document.createElement('div'); grid.className = 'setting-grid';
          let field = null;
          for (const child of Array.from(section.children)) {
            if (child.tagName === 'H2') continue;
            if (child.tagName === 'LABEL') {
              field = document.createElement('div'); field.className = 'setting-field';
              if (['FOOTER_NOTICE','NAS_ORIGIN','NAS_API_KEY','NAS_DECODING_CODECS','CACHE_MAX_BYTES'].includes(child.htmlFor)) field.classList.add('wide');
              grid.append(field);
            }
            if (field) field.append(child);
          }
          section.append(grid);
        }
        section.id = `setting-group-${n++}`;
        const a = document.createElement('a'); a.href = '#' + section.id;
        a.textContent = section.querySelector('h2').textContent; nav.append(a);
      }
    };
    new MutationObserver(decorate).observe(fields, {childList:true});
    decorate();
  }
  // One hovered surface, one queued frame; never an idle render loop.
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const pointer = matchMedia('(hover: hover) and (pointer: fine)');
  let frame = 0, point = null, active = null;
  const clear = () => {
    if (active) { active.style.removeProperty('--light-x'); active.style.removeProperty('--light-y'); }
    active = null;
  };
  document.addEventListener('pointermove', event => {
    if (reduced.matches || !pointer.matches) return;
    point = {target:event.target, x:event.clientX, y:event.clientY};
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      const surface = point.target.closest('button:not(:disabled),.metric');
      if (active !== surface) clear();
      if (!surface) return;
      active = surface;
      const rect = surface.getBoundingClientRect();
      surface.style.setProperty('--light-x', `${Math.round(point.x-rect.left)}px`);
      surface.style.setProperty('--light-y', `${Math.round(point.y-rect.top)}px`);
    });
  }, {passive:true});
  document.addEventListener('pointerleave', clear);
  reduced.addEventListener('change', clear);
})();
