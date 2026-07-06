/* ============================================================
   CONTINUITY STUDIO PRO — UI Enhancement Script
   Add to index.html before </body>:
   <script src="/static/enhancements.js"></script>
   ============================================================ */

(function() {
  'use strict';

  // === CURSOR GLOW EFFECT ===
  const glow = document.createElement('div');
  glow.className = 'cursor-glow';
  document.body.appendChild(glow);

  let glowTimeout;
  document.addEventListener('mousemove', (e) => {
    glow.style.left = e.clientX + 'px';
    glow.style.top = e.clientY + 'px';
    glow.classList.add('active');
    clearTimeout(glowTimeout);
    glowTimeout = setTimeout(() => glow.classList.remove('active'), 2000);
  });

  // === SCROLL REVEAL ===
  const observerOptions = { threshold: 0.1, rootMargin: '0px 0px -50px 0px' };
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
        observer.unobserve(entry.target);
      }
    });
  }, observerOptions);

  // Auto-add reveal class to cards and shots
  function initReveal() {
    document.querySelectorAll('.card, .shot, .scenelet, .audio-card').forEach(el => {
      if (!el.classList.contains('reveal')) {
        el.classList.add('reveal');
        observer.observe(el);
      }
    });
  }

  // Re-run on DOM changes (for dynamically added content)
  const mutationObserver = new MutationObserver(() => {
    requestAnimationFrame(initReveal);
  });
  mutationObserver.observe(document.body, { childList: true, subtree: true });

  // === RIPPLE EFFECT ON BUTTONS ===
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.btn');
    if (!btn) return;

    const ripple = document.createElement('span');
    const rect = btn.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height);
    const x = e.clientX - rect.left - size / 2;
    const y = e.clientY - rect.top - size / 2;

    ripple.style.cssText = `
      position: absolute;
      width: ${size}px;
      height: ${size}px;
      left: ${x}px;
      top: ${y}px;
      background: rgba(255,255,255,0.3);
      border-radius: 50%;
      transform: scale(0);
      animation: ripple 0.6s ease-out;
      pointer-events: none;
    `;
    btn.style.position = 'relative';
    btn.style.overflow = 'hidden';
    btn.appendChild(ripple);
    setTimeout(() => ripple.remove(), 600);
  });

  // === SMOOTH TAB TRANSITIONS ===
  const originalTabClick = () => {
    const tabs = document.querySelectorAll('.tab, .nav-tab');
    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        // Add exit animation to current view
        const activeView = document.querySelector('.view.active');
        if (activeView) {
          activeView.style.animation = 'none';
          activeView.offsetHeight; // Force reflow
          activeView.style.animation = '';
        }
      });
    });
  };
  originalTabClick();

  // === PARALLAX HEADER ===
  const header = document.querySelector('header');
  if (header) {
    window.addEventListener('scroll', () => {
      const scroll = window.scrollY;
      if (scroll > 10) {
        header.style.borderBottomColor = 'rgba(255,133,48,0.1)';
        header.style.boxShadow = '0 4px 30px -10px rgba(0,0,0,0.5)';
      } else {
        header.style.borderBottomColor = '';
        header.style.boxShadow = '';
      }
    }, { passive: true });
  }

  // === LOADING STATE ANIMATION ===
  // Override fetch to add loading indicators
  const originalFetch = window.fetch;
  let activeRequests = 0;

  window.fetch = function(...args) {
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
    if (url.startsWith('/api/')) {
      activeRequests++;
      document.body.classList.add('is-loading');
    }
    return originalFetch.apply(this, args).finally(() => {
      if (url.startsWith('/api/')) {
        activeRequests--;
        if (activeRequests <= 0) {
          activeRequests = 0;
          document.body.classList.remove('is-loading');
        }
      }
    });
  };

  // === KEYBOARD SHORTCUTS ===
  document.addEventListener('keydown', (e) => {
    // Ctrl/Cmd + number to switch tabs
    if ((e.ctrlKey || e.metaKey) && e.key >= '1' && e.key <= '9') {
      e.preventDefault();
      const tabs = document.querySelectorAll('.tab, .nav-tab');
      const idx = parseInt(e.key) - 1;
      if (tabs[idx]) tabs[idx].click();
    }
  });

  // === FRAME COUNTER ANIMATION ===
  function animateCounter(el, target) {
    let current = 0;
    const step = Math.max(1, Math.floor(target / 30));
    const interval = setInterval(() => {
      current += step;
      if (current >= target) {
        current = target;
        clearInterval(interval);
      }
      el.textContent = current;
    }, 30);
  }

  // Expose for use by the app
  window.CSEnhancements = {
    animateCounter,
    initReveal,
  };

  // Init
  document.addEventListener('DOMContentLoaded', initReveal);
  setTimeout(initReveal, 1000); // Fallback for late-loaded content

  console.log('%c✦ Continuity Studio Pro %c— Enhanced UI loaded', 
    'color: #ff8530; font-weight: bold; font-size: 14px',
    'color: #b0a8be; font-size: 12px');
})();
