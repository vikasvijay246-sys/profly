/* ── Modal helpers ────────────────────────────────────── */
function openModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('hidden');
  // Trap focus on first input for accessibility
  const first = el.querySelector('input:not([type=hidden]), select, textarea');
  if (first) setTimeout(() => first.focus(), 80);
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('hidden');
}

// Close on backdrop click
document.addEventListener('click', function(e) {
  if (e.target.classList.contains('modal-backdrop')) {
    e.target.classList.add('hidden');
  }
});

// Close on Escape
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-backdrop:not(.hidden)').forEach(el => {
      el.classList.add('hidden');
    });
  }
});

/* ── Sidebar (mobile) ─────────────────────────────────── */
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebarOverlay').classList.toggle('open');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebarOverlay').classList.remove('open');
}

/* ── Dark mode ────────────────────────────────────────── */
function toggleDark() {
  const html   = document.documentElement;
  const isDark = html.dataset.theme === 'dark';
  html.dataset.theme = isDark ? 'light' : 'dark';
  localStorage.setItem('pf_theme', html.dataset.theme);
  const btn = document.getElementById('darkToggle');
  if (btn) btn.textContent = isDark ? '🌙' : '☀️';
}

// Apply saved theme immediately (called from base.html inline script too)
(function() {
  const saved = localStorage.getItem('pf_theme');
  if (saved) {
    document.documentElement.dataset.theme = saved;
    const btn = document.getElementById('darkToggle');
    if (btn) btn.textContent = saved === 'dark' ? '☀️' : '🌙';
  }
})();

/* ── Auto-dismiss flash messages ──────────────────────── */
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('.flash').forEach(function(el) {
    setTimeout(function() {
      el.style.transition = 'opacity .4s';
      el.style.opacity    = '0';
      setTimeout(() => el.remove(), 400);
    }, 5000);
  });
});

/* ── Join user-specific SocketIO room ─────────────────── */
// Called from base.html after socket is initialised
if (typeof socket !== 'undefined') {
  socket.on('connect', function() {
    socket.emit('join_user_room');
  });
}
