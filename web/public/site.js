// The landing page's behaviour: a full height mobile menu, sections that rise into place once, the current section in the navigation, and the tagline whose words light up in reading order.
// Nothing here runs on every scroll event: it is all IntersectionObserver. Without script, or with reduced motion asked for, the page is simply shown.
(() => {
  const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const toggle = document.querySelector('.menu-toggle'), menu = document.querySelector('.mobile-menu');
  if (toggle && menu) {
    const set = (open) => { toggle.setAttribute('aria-expanded', String(open)); toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu'); menu.classList.toggle('open', open); document.body.classList.toggle('menu-open', open); };
    toggle.addEventListener('click', () => set(toggle.getAttribute('aria-expanded') !== 'true'));
    menu.querySelectorAll('a').forEach((a) => a.addEventListener('click', () => set(false)));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') set(false); });
  }
  if (!('IntersectionObserver' in window)) return;
  const reveal = new IntersectionObserver((entries) => entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add('in'); reveal.unobserve(e.target); } }), { rootMargin: '0px 0px -10% 0px' });
  document.querySelectorAll('main > section:not(.hero) > .wrap').forEach((el) => { el.classList.add('reveal'); reveal.observe(el); });
  const links = [...document.querySelectorAll('.nav ul a[href^="#"]')];
  const here = new IntersectionObserver((entries) => entries.forEach((e) => { if (e.isIntersecting) links.forEach((a) => a.toggleAttribute('aria-current', a.getAttribute('href') === '#' + e.target.id)); }), { rootMargin: '-40% 0px -55% 0px' });
  links.forEach((a) => { const s = document.querySelector(a.getAttribute('href')); if (s) here.observe(s); });
  document.querySelectorAll('.tag-text').forEach((p) => {
    const words = p.textContent.trim().split(/\s+/);
    p.setAttribute('aria-label', words.join(' '));
    p.replaceChildren(...words.flatMap((w, i) => { const s = document.createElement('span'); s.className = 'w'; s.setAttribute('aria-hidden', 'true'); s.textContent = w; return i ? [document.createTextNode(' '), s] : [s]; }));
    const spans = [...p.querySelectorAll('.w')];
    if (still) { spans.forEach((s) => s.classList.add('on')); return; }
    const io = new IntersectionObserver((entries) => entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add('on'); io.unobserve(e.target); } }), { rootMargin: '0px 0px -40% 0px' });
    spans.forEach((s) => io.observe(s));
  });
})();
