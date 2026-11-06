#!/usr/bin/env python3
import ast
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARCADOR = "[PATCH titlebar v1]"

# ───────────────────────── Python (classe Api) ─────────────────────────
API_OLD = "    def init_info(self):"
API_NEW = r'''    # [PATCH titlebar v1]
    def _hwnd(self):
        if not sys.platform.startswith("win") or not self._window:
            return None
        try:
            return int(self._window.native.Handle.ToInt64())
        except Exception:
            try:
                return int(self._window.native.Handle.ToInt32())
            except Exception:
                return None

    def win_minimize(self):
        try:
            self._window.minimize()
        except Exception:
            pass
        return True

    def win_close(self):
        try:
            self._window.destroy()
        except Exception:
            pass
        return True

    def win_is_max(self):
        return bool(getattr(self, "_maxed", False))

    def win_toggle_max(self):
        w = self._window
        if not w:
            return False
        hwnd = self._hwnd()
        if hwnd:
            try:
                import ctypes
                from ctypes import wintypes
                u = ctypes.windll.user32
                flags = 0x0004 | 0x0010
                if not getattr(self, "_maxed", False):
                    r = wintypes.RECT()
                    u.GetWindowRect(hwnd, ctypes.byref(r))
                    self._restore_rect = (r.left, r.top,
                                          r.right - r.left, r.bottom - r.top)
                    wa = wintypes.RECT()
                    u.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)
                    u.SetWindowPos(hwnd, 0, wa.left, wa.top,
                                   wa.right - wa.left, wa.bottom - wa.top,
                                   flags)
                    self._maxed = True
                else:
                    x, y, ww, hh = getattr(self, "_restore_rect",
                                           (100, 100, 1320, 840))
                    u.SetWindowPos(hwnd, 0, x, y, ww, hh, flags)
                    self._maxed = False
                return self._maxed
            except Exception:
                pass
        try:
            if not getattr(self, "_maxed", False):
                w.maximize()
                self._maxed = True
            else:
                w.restore()
                self._maxed = False
        except Exception:
            pass
        return bool(getattr(self, "_maxed", False))

    def win_resize_begin(self):
        w = self._window
        if not w:
            return False
        hwnd = self._hwnd()
        if hwnd:
            try:
                import ctypes
                from ctypes import wintypes
                r = wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
                self._rs = ("win", hwnd, r.left, r.top,
                            r.right - r.left, r.bottom - r.top)
                return True
            except Exception:
                pass
        try:
            self._rs = ("pw", None, 0, 0, int(w.width), int(w.height))
        except Exception:
            self._rs = None
        return True

    def win_resize_drag(self, edge, dx, dy, dpr):
        rs = getattr(self, "_rs", None)
        if not rs or getattr(self, "_maxed", False):
            return False
        kind, hwnd, left, top, w, h = rs
        try:
            dpr = float(dpr or 1)
            dx, dy = float(dx), float(dy)
            if kind == "win":
                import ctypes
                nw = w + dx * dpr if "r" in edge else w
                nh = h + dy * dpr if "b" in edge else h
                nw = max(int(nw), int(1050 * dpr))
                nh = max(int(nh), int(680 * dpr))
                ctypes.windll.user32.SetWindowPos(
                    hwnd, 0, left, top, nw, nh, 0x0004 | 0x0010)
            else:
                nw = max(int(w + dx) if "r" in edge else w, 1050)
                nh = max(int(h + dy) if "b" in edge else h, 680)
                self._window.resize(nw, nh)
        except Exception:
            return False
        return True

    def init_info(self):'''

# ───────────────────────── main() ─────────────────────────
MAIN_OLD = "        min_size=(1050, 680))"
MAIN_NEW = "        min_size=(1050, 680), frameless=True, easy_drag=False)"

# ───────────────────────── HTML: barra + container ─────────────────────────
BODY_OLD = '<div class="flex h-screen overflow-hidden">'
BODY_NEW = r'''<!-- [PATCH titlebar v1] -->
<div id="titlebar">
  <div id="tb-drag" class="pywebview-drag-region tb-drag">
    <span class="tb-logo"><svg viewBox="0 0 24 24" fill="none" stroke="#0369a1" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M7 21h10a2 2 0 002-2V9.4a1 1 0 00-.3-.7l-5.4-5.4a1 1 0 00-.7-.3H7a2 2 0 00-2 2v14a2 2 0 002 2z"/></svg></span>
    <span class="tb-brand">Convert<b>Hub</b></span>
    <span id="tb-title" class="tb-title">Home</span>
  </div>
  <div class="tb-ctrl">
    <button id="wc-min" class="wc-btn" title="Minimizar"><svg viewBox="0 0 12 12"><rect x="2" y="5.3" width="8" height="1.5" rx=".75" fill="currentColor"/></svg></button>
    <button id="wc-max" class="wc-btn" title="Maximizar"><svg viewBox="0 0 12 12"><rect x="2.5" y="2.5" width="7" height="7" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.4"/></svg></button>
    <button id="wc-close" class="wc-btn wc-close" title="Fechar"><svg viewBox="0 0 12 12"><path d="M2.8 2.8l6.4 6.4M9.2 2.8L2.8 9.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" fill="none"/></svg></button>
  </div>
</div>
<div class="rz" data-edge="r"></div>
<div class="rz" data-edge="b"></div>
<div class="rz" data-edge="rb"></div>
<div class="flex overflow-hidden" style="height:calc(100vh - 38px)">'''

CONC_OLD = '<div class="flex flex-col" style="height:100vh">'
CONC_NEW = '<div class="flex flex-col" style="height:calc(100vh - 38px)">'

CSS_OLD = "</style>"
CSS_NEW = r'''
/* [PATCH titlebar v1] */
html, body { overflow:hidden; height:100%; }
#titlebar {
  height:38px; display:flex; align-items:center; justify-content:space-between;
  position:relative; z-index:60; color:#ffffff; user-select:none; -webkit-user-select:none;
  background:linear-gradient(90deg,#0c4a6e 0%,#0369a1 55%,#0284c7 100%);
  box-shadow:0 1px 6px rgba(15,23,42,.35);
}
.tb-drag { flex:1; height:100%; min-width:0; display:flex; align-items:center; gap:10px; padding-left:14px; cursor:default; }
.tb-drag > * { pointer-events:none; }
.tb-logo { width:20px; height:20px; border-radius:7px; background:#ffffff; display:flex; align-items:center; justify-content:center; flex-shrink:0; }
.tb-logo svg { width:12px; height:12px; }
.tb-brand { font-weight:600; font-size:13px; letter-spacing:.2px; }
.tb-brand b { color:#7dd3fc; font-weight:600; }
.tb-title { font-size:12px; opacity:.8; padding-left:10px; border-left:1px solid rgba(255,255,255,.35); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tb-ctrl { display:flex; align-items:center; gap:8px; padding:0 14px; }
.wc-btn {
  width:26px; height:26px; border-radius:9999px; border:0; padding:0; cursor:pointer;
  background:rgba(255,255,255,.16); color:#ffffff;
  display:flex; align-items:center; justify-content:center;
  transition:background .15s, transform .08s;
}
.wc-btn svg { width:12px; height:12px; }
.wc-btn:hover { background:rgba(255,255,255,.32); }
.wc-btn:active { transform:scale(.92); }
#wc-min:hover { background:#f59e0b; }
#wc-max:hover { background:#10b981; }
.wc-close:hover { background:#ef4444; }
html.dark #titlebar { background:linear-gradient(90deg,#000000 0%,#08121b 60%,#0c2a40 100%); border-bottom:1px solid #262626; box-shadow:none; }
.rz { position:fixed; z-index:70; }
.rz[data-edge="r"]  { top:38px; right:0; bottom:10px; width:4px; cursor:ew-resize; }
.rz[data-edge="b"]  { left:0; right:10px; bottom:0; height:4px; cursor:ns-resize; }
.rz[data-edge="rb"] { right:0; bottom:0; width:10px; height:10px; cursor:nwse-resize; }
html.is-max .rz { display:none; }
</style>'''

PAINT_OLD = "if (b.hasAttribute('data-del') || b.id === 'log-clear') return;"
PAINT_NEW = ("if (b.hasAttribute('data-del') || b.id === 'log-clear' || "
             "b.classList.contains('wc-btn')) return;")

SCRIPT_OLD = "</body>"
SCRIPT_NEW = r'''<script>
/* [PATCH titlebar v1] */
(function () {
  var ICON_MAX = '<svg viewBox="0 0 12 12"><rect x="2.5" y="2.5" width="7" height="7" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>';
  var ICON_RES = '<svg viewBox="0 0 12 12"><rect x="2" y="4" width="6" height="6" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M4.5 3.9V3.4a1.2 1.2 0 011.2-1.2h2.9a1.2 1.2 0 011.2 1.2v2.9a1.2 1.2 0 01-1.2 1.2h-.5" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';
  var maxed = false;
  function api(fn) {
    var a = Array.prototype.slice.call(arguments, 1);
    try { return Promise.resolve(window.pywebview.api[fn].apply(null, a)); }
    catch (e) { return Promise.resolve(null); }
  }
  var bMax = document.getElementById('wc-max');
  function setMax(m) {
    maxed = !!m;
    document.documentElement.classList.toggle('is-max', maxed);
    bMax.innerHTML = maxed ? ICON_RES : ICON_MAX;
    bMax.title = maxed ? 'Restaurar' : 'Maximizar';
  }
  async function toggleMax() { setMax(await api('win_toggle_max')); }

  document.getElementById('wc-min').addEventListener('click', function () { api('win_minimize'); });
  bMax.addEventListener('click', toggleMax);
  document.getElementById('wc-close').addEventListener('click', function () { api('win_close'); });
  document.getElementById('tb-drag').addEventListener('dblclick', toggleMax);

  Array.prototype.forEach.call(document.querySelectorAll('.rz'), function (h) {
    h.addEventListener('pointerdown', function (e) {
      if (maxed) return;
      e.preventDefault();
      var edge = h.dataset.edge, sx = e.screenX, sy = e.screenY;
      var ready = api('win_resize_begin');
      var busy = false, last = null;
      try { h.setPointerCapture(e.pointerId); } catch (_) {}
      async function pump() {
        busy = true;
        await ready;
        while (last) {
          var d = last; last = null;
          await api('win_resize_drag', edge, d[0], d[1], window.devicePixelRatio || 1);
        }
        busy = false;
      }
      function move(ev) {
        last = [ev.screenX - sx, ev.screenY - sy];
        if (!busy) pump();
      }
      function up(ev) {
        h.removeEventListener('pointermove', move);
        h.removeEventListener('pointerup', up);
        h.removeEventListener('pointercancel', up);
        try { h.releasePointerCapture(ev.pointerId); } catch (_) {}
      }
      h.addEventListener('pointermove', move);
      h.addEventListener('pointerup', up);
      h.addEventListener('pointercancel', up);
    });
  });

  var TITULOS = { 'home': 'Home', 'pdf-ofx': 'PDF \u2192 OFX', 'txt': 'Extrato \u2192 TXT',
                  'conciliacao': 'Concilia\u00e7\u00e3o Banc\u00e1ria', 'settings': 'Sistema' };
  function titulo(m) {
    var t = document.getElementById('tb-title');
    if (t && TITULOS[m]) t.textContent = TITULOS[m];
  }
  Array.prototype.forEach.call(document.querySelectorAll('.sidebar-item'), function (b) {
    b.addEventListener('click', function () { titulo(b.dataset.module); });
  });
  Array.prototype.forEach.call(document.querySelectorAll('.goto-btn'), function (b) {
    b.addEventListener('click', function () { titulo(b.dataset.goto); });
  });
  window.addEventListener('pywebviewready', async function () {
    var m = await api('win_is_max');
    setMax(m);
  });
})();
</script>
</body>'''

SUBSTITUICOES = [
    ("main()", MAIN_OLD, MAIN_NEW),
    ("Api", API_OLD, API_NEW),
    ("corpo HTML", BODY_OLD, BODY_NEW),
    ("conciliacao", CONC_OLD, CONC_NEW),
    ("CSS", CSS_OLD, CSS_NEW),
    ("paint", PAINT_OLD, PAINT_NEW),
    ("script", SCRIPT_OLD, SCRIPT_NEW),
]


def proximo_livre(base: Path) -> Path:
    if not base.exists():
        return base
    i = 2
    while True:
        cand = base.with_name(f"{base.stem}_{i}{base.suffix}")
        if not cand.exists():
            return cand
        i += 1


def main():
    if len(sys.argv) != 2:
        print("Uso: python corrigir_titlebar.py <arquivo.py>")
        sys.exit(1)

    origem = Path(sys.argv[1]).expanduser().resolve()
    if not origem.is_file():
        print(f"Erro: arquivo não encontrado: {origem}")
        sys.exit(1)

    try:
        texto = origem.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print("Erro: o arquivo não está em UTF-8.")
        sys.exit(1)
    except OSError as exc:
        print(f"Erro ao ler o arquivo: {exc}")
        sys.exit(1)

    if MARCADOR in texto:
        print("Este arquivo já contém a barra de título personalizada. Nada a fazer.")
        sys.exit(0)

    novo = texto
    for nome, antigo, replacement in SUBSTITUICOES:
        qtd = novo.count(antigo)
        if qtd != 1:
            print(f"Erro: trecho '{nome}' encontrado {qtd} vez(es); esperado 1. "
                  "O arquivo não corresponde à versão esperada.")
            sys.exit(1)
        novo = novo.replace(antigo, replacement, 1)

    try:
        ast.parse(novo)
    except SyntaxError as exc:
        print(f"Erro: o resultado gerado tem erro de sintaxe: {exc}")
        sys.exit(1)

    for essencial in ("def win_toggle_max(", "frameless=True", 'id="titlebar"'):
        if essencial not in novo:
            print(f"Erro: elemento esperado ausente no resultado: {essencial}")
            sys.exit(1)

    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = origem.with_name(f"{origem.stem}.backup_{carimbo}{origem.suffix}")
    destino = proximo_livre(origem.with_name(f"{origem.stem}_corrigido{origem.suffix}"))

    try:
        shutil.copy2(origem, backup)
        destino.write_text(novo, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"Erro ao gravar arquivos: {exc}")
        sys.exit(1)

    print(f"Backup do original : {backup}")
    print(f"Arquivo corrigido  : {destino}")


if __name__ == "__main__":
    main()
