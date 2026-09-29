/* SteamOS Companion landing page.
   Dependency-free. Handles: latest-release lookup, the pairing walkthrough,
   install tabs, copy buttons, and reveal-on-scroll. */
(() => {
  'use strict';

  const REPO = 'tuthan/steamos-companion-decky';
  const FALLBACK_TAG = 'v0.5.18';
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ------------------------------------------------------------ release */
  const applyVersion = (tag, zipUrl) => {
    if (!/^v\d+\.\d+\.\d+$/.test(tag)) return;
    document.querySelectorAll('[data-version]').forEach((el) => { el.textContent = tag; });
    document.querySelectorAll('[data-version-bare]').forEach((el) => { el.textContent = tag.slice(1); });
    if (zipUrl) document.querySelectorAll('[data-zip]').forEach((a) => { a.href = zipUrl; });
  };
  applyVersion(FALLBACK_TAG);
  fetch(`https://api.github.com/repos/${REPO}/releases/latest`, { headers: { Accept: 'application/vnd.github+json' } })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
    .then((rel) => {
      const zip = (rel.assets || []).find((a) => /^steamos-companion-decky-.*\.zip$/.test(a.name));
      applyVersion(rel.tag_name, zip && zip.browser_download_url);
    })
    .catch(() => { /* keep the static fallback */ });

  /* ------------------------------------------------------------ pairing */
  const STEPS = [
    {
      title: 'Enable the server',
      text: 'On the target device, a Steam Deck or a SteamOS living-room PC, choose Server or Both. The host creates its identity and TLS certificate on first use and listens on TCP 18443 on all IPv4 interfaces.',
      wire: { dir: 'none', text: 'Server listening on 0.0.0.0:18443 · TLS identity ready' },
      packets: [],
      dur: 3800,
    },
    {
      title: 'Discover on the LAN',
      text: 'The client runs an explicit, bounded scan of the local subnet and asks each candidate for its service marker and certificate identity. Nothing is trusted yet.',
      wire: { dir: 'both', text: 'GET /v1/discovery → { service:"steamos-companion", host_id, certificate_fingerprint }' },
      packets: [
        { dir: 'to-server', cls: 'probe', delay: 0 },
        { dir: 'to-server', cls: 'probe', delay: 220 },
        { dir: 'to-server', cls: '', delay: 480 },
        { dir: 'to-client', cls: '', delay: 1750 },
      ],
      dur: 4200,
    },
    {
      title: 'Pin the certificate',
      text: 'The client stores the host’s sha256 fingerprint. From now on every connection to that address must present exactly this certificate, or it is refused before any request is sent.',
      wire: { dir: 'none', text: 'pinned sha256:9f3a27c1b8e0…41e4d2 · no traffic' },
      packets: [],
      dur: 3600,
    },
    {
      title: 'Request pairing',
      text: 'The client generates a random 16-byte nonce and sends it, with its requested scopes, over the pinned TLS 1.2 channel. The request also carries a binding derived from that exact TLS session.',
      wire: { dir: 'to-server', text: 'POST /v1/pair/request { verification_nonce, client_id, client_name, scopes } + X-SteamOS-Companion-TLS-Binding' },
      packets: [{ dir: 'to-server', cls: 'secure', delay: 200 }],
      dur: 4000,
    },
    {
      title: 'Both derive the code',
      text: 'Client and host each run scrypt over the nonce, salted with the certificate fingerprint, and show the same eight digits. The code itself never crosses the network, so nothing in the middle can read or replace it.',
      wire: { dir: 'blocked', text: 'code = scrypt(nonce, "steamos-companion:v1:pairing-sas:" + fingerprint) mod 10^8 · derived on both sides, never transmitted' },
      packets: [],
      code: true,
      dur: 4600,
    },
    {
      title: 'Compare and approve',
      text: 'The owner checks that both screens show the same digits, then taps Approve on the host. Reject always comes first in the card, and an unapproved request expires after 120 seconds.',
      wire: { dir: 'none', text: 'owner compares 7533 2470 on both screens → Approve' },
      packets: [],
      dur: 4000,
    },
    {
      title: 'Token issued',
      text: 'The host mints a bearer token, stores only its hash, and returns it with the wake target. The client polls with a pairing-session handle, so a new TLS connection never has to re-derive the code.',
      wire: { dir: 'to-client', text: '200 { status:"approved", token, granted_scopes, wake_target:{ mac, interface } }' },
      packets: [
        { dir: 'to-server', cls: 'probe', delay: 0 },
        { dir: 'to-client', cls: 'secure', delay: 700 },
      ],
      dur: 4000,
    },
    {
      title: 'Authenticated control',
      text: 'Every later call carries the token and a scope. Mutations use a client-generated request_id and return 202 Accepted; after a disconnect the client reconciles the operation journal instead of sending the command again.',
      wire: { dir: 'both', text: 'GET /v1/status · POST /v1/power { request_id, action } → 202 Accepted · GET /v1/operations/{id}' },
      packets: [
        { dir: 'to-server', cls: 'secure', delay: 0 },
        { dir: 'to-client', cls: 'secure', delay: 800 },
        { dir: 'to-server', cls: 'secure', delay: 1900 },
        { dir: 'to-client', cls: 'secure', delay: 2700 },
      ],
      dur: 4600,
    },
  ];
  const DIR_GLYPH = { none: '·', both: '⇄', 'to-server': '→', 'to-client': '←', blocked: '⊘' };

  const stage = document.getElementById('stage');
  const ctl = document.getElementById('stage-ctl');
  if (stage && ctl) {
    const link = stage.querySelector('.link');
    const states = Array.from(stage.querySelectorAll('.state'));
    const codes = Array.from(stage.querySelectorAll('.q-code'));
    const stepButtons = Array.from(document.querySelectorAll('.step'));
    const desc = document.getElementById('flow-desc');
    const wireDir = document.querySelector('.wire .dir');
    const wireMsg = document.getElementById('wire-msg');
    const playBtn = document.getElementById('btn-play');

    let index = 0;
    let timer = null;
    let playing = !reduceMotion;
    let visible = true;
    let rollTimers = [];

    const restart = (el) => {
      el.style.animation = 'none';
      void el.offsetWidth; // reflow so the animation restarts
      el.style.animation = '';
    };

    const spawnPackets = (list) => {
      link.querySelectorAll('.packet').forEach((p) => p.remove());
      if (reduceMotion) return;
      list.forEach((p) => {
        const el = document.createElement('i');
        el.className = `packet ${p.dir} ${p.cls || ''}`.trim();
        el.style.setProperty('--delay', `${p.delay || 0}ms`);
        el.addEventListener('animationend', () => el.remove());
        link.appendChild(el);
      });
    };

    const settleCodes = () => {
      rollTimers.forEach(clearTimeout);
      rollTimers = [];
      codes.forEach((el) => {
        const digits = el.dataset.code.split('');
        const spans = Array.from(el.querySelectorAll('span:not(.gap)'));
        spans.forEach((s, k) => { s.textContent = digits[k] || ''; });
        el.classList.remove('rolling');
      });
    };

    const rollCodes = () => {
      settleCodes();
      if (reduceMotion) return;
      codes.forEach((el) => {
        const digits = el.dataset.code.split('');
        const spans = Array.from(el.querySelectorAll('span:not(.gap)'));
        el.classList.add('rolling');
        let ticks = 0;
        const tick = () => {
          ticks += 1;
          spans.forEach((s, k) => {
            // Each digit settles a little later than the previous one.
            if (ticks > 8 + k * 2) s.textContent = digits[k];
            else s.textContent = String(Math.floor(Math.random() * 10));
          });
          if (ticks < 8 + spans.length * 2) rollTimers.push(setTimeout(tick, 70));
          else el.classList.remove('rolling');
        };
        rollTimers.push(setTimeout(tick, 350));
      });
    };

    const schedule = () => {
      clearTimeout(timer);
      if (!playing || !visible) return;
      timer = setTimeout(() => show(index + 1), STEPS[index].dur);
    };

    const show = (n) => {
      index = (n + STEPS.length) % STEPS.length;
      const step = STEPS[index];
      stage.dataset.step = String(index);
      stage.dataset.secure = index >= 2 ? '1' : '0';

      states.forEach((st) => {
        const on = st.dataset.steps.split(' ').includes(String(index));
        st.classList.toggle('is-on', on);
        if (on) st.querySelectorAll('.q-prog i, .q-btn.armed').forEach(restart);
      });

      stepButtons.forEach((b, k) => {
        b.classList.toggle('is-active', k === index);
        b.classList.toggle('is-done', k < index);
        b.setAttribute('aria-current', k === index ? 'step' : 'false');
        b.style.setProperty('--step-dur', `${step.dur}ms`);
      });

      desc.innerHTML = '';
      const strong = document.createElement('strong');
      strong.textContent = `${step.title}. `;
      desc.append(strong, step.text);

      wireDir.dataset.dir = step.wire.dir;
      wireDir.textContent = DIR_GLYPH[step.wire.dir];
      wireMsg.textContent = step.wire.text;
      restart(wireMsg);

      spawnPackets(step.packets);
      if (step.code) rollCodes(); else settleCodes();
      schedule();
    };

    const setPlaying = (value) => {
      playing = value;
      ctl.classList.toggle('is-paused', !playing);
      playBtn.setAttribute('aria-label', playing ? 'Pause' : 'Play');
      schedule();
    };

    playBtn.addEventListener('click', () => setPlaying(!playing));
    document.getElementById('btn-prev').addEventListener('click', () => { setPlaying(false); show(index - 1); });
    document.getElementById('btn-next').addEventListener('click', () => { setPlaying(false); show(index + 1); });
    document.getElementById('btn-replay').addEventListener('click', () => { show(0); setPlaying(true); });
    stepButtons.forEach((b) => b.addEventListener('click', () => { setPlaying(false); show(Number(b.dataset.step)); }));

    // Pause the loop while the stage is off screen or the tab is hidden.
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((entries) => {
        visible = entries.some((e) => e.isIntersecting);
        schedule();
      }, { threshold: 0.15 }).observe(stage);
    }
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) clearTimeout(timer); else schedule();
    });

    ctl.classList.toggle('is-paused', !playing);
    playBtn.setAttribute('aria-label', playing ? 'Pause' : 'Play');
    show(0);
  }

  /* --------------------------------------------------------------- tabs */
  const tabs = Array.from(document.querySelectorAll('.tab[role="tab"]'));
  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      tabs.forEach((t) => {
        const on = t === tab;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        const panel = document.getElementById(t.getAttribute('aria-controls'));
        if (panel) { panel.classList.toggle('is-on', on); panel.hidden = !on; }
      });
    });
    tab.addEventListener('keydown', (e) => {
      const i = tabs.indexOf(tab);
      if (e.key === 'ArrowRight') tabs[(i + 1) % tabs.length].focus();
      if (e.key === 'ArrowLeft') tabs[(i - 1 + tabs.length) % tabs.length].focus();
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') document.activeElement.click();
    });
  });

  /* --------------------------------------------------------------- copy */
  document.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const pre = btn.closest('pre');
      const text = Array.from(pre.childNodes)
        .filter((n) => n !== btn)
        .map((n) => n.textContent)
        .join('')
        .trim();
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = 'Copied';
        btn.classList.add('done');
      } catch {
        btn.textContent = 'Select and copy';
      }
      setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('done'); }, 1800);
    });
  });

  /* ------------------------------------------------------------- reveal */
  const revealables = document.querySelectorAll('.reveal');
  if (reduceMotion || !('IntersectionObserver' in window)) {
    revealables.forEach((el) => el.classList.add('in'));
  } else {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
    revealables.forEach((el) => io.observe(el));
  }

})();
