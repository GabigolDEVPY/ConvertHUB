# ──────────────────────────────────────────────────────────────────────────────
#  Conversor Extrato (OFX / PDF) → TXT  |  Alterdata  |  v4
#  • Aceita OFX (parser interno) e PDF (parsers/ -> LAYOUTS)
#  • Perfis, memoria e regras por perfil (perfis/<nome>/...)
# ──────────────────────────────────────────────────────────────────────────────

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import re
import json
import os
import shutil
import html
import datetime
os.environ["TK_SILENCE_DEPRECATION"] = "1"
import sys

BASE_DIR    = os.path.dirname(os.path.abspath(sys.argv[0]))
PERFIS_DIR  = os.path.join(BASE_DIR, "perfis")
CONFIG_FILE = os.path.join(BASE_DIR, "perfis_config.json")
OLD_MEMORY  = os.path.join(BASE_DIR, "lancamentos_memoria.json")

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

ROW_H    = 28
OVERSCAN = 5


# ══════════════════════════════════════════════════════════════════════════════
#  Layouts de PDF (pasta parsers/)
# ══════════════════════════════════════════════════════════════════════════════

def carregar_layouts():
    """Retorna (LAYOUTS dict, erro str|None)."""
    try:
        import importlib
        mod = importlib.import_module("parsers")
        mod = importlib.reload(mod)
        layouts = getattr(mod, "LAYOUTS", None)
        if not isinstance(layouts, dict):
            return {}, "parsers/__init__.py nao define LAYOUTS."
        return layouts, None
    except Exception as e:
        return {}, f"{type(e).__name__}: {e}"


def _converter_brutas(brutas: list) -> list:
    transactions = []
    for i, t in enumerate(brutas):
        valor_num = t.get("valor")
        if valor_num is None:
            continue
        d = t.get("data")
        if isinstance(d, (datetime.date, datetime.datetime)):
            date_str = d.strftime("%d/%m/%Y")
        else:
            date_str = str(d or "")
        descricao = (t.get("descricao") or "").strip()
        nome = (t.get("nome") or "").strip()
        if nome and nome.upper() not in descricao.upper():
            descricao = f"{descricao} {nome}".strip()
        if not descricao:
            descricao = "(sem descricao)"
        doc = str(t.get("doc") or "").strip()
        transactions.append({
            "tipo":      "C" if valor_num >= 0 else "D",
            "data":      date_str,
            "valor":     abs(float(valor_num)),
            "descricao": descricao,
            "fitid":     "",
            "num_doc":   doc,
            "uid":       f"PDF-{i}",
        })
    return transactions


def _pontuar_layout(mod, filepath, brutas):
    """Pontua o quanto o resultado de um parser 'bate' com o PDF."""
    validas = [t for t in brutas
               if t.get("valor") is not None and t.get("data")]
    if not validas:
        return -1.0, "sem lancamentos"

    n = len(brutas)
    score = float(len(validas))
    notas = [f"{len(validas)} lanc."]

    # qualidade dos dados
    sem_desc = sum(1 for t in validas
                   if not (t.get("descricao") or "").strip())
    zerados = sum(1 for t in validas if abs(t["valor"]) < 0.005)
    score -= sem_desc * 2 + zerados
    score -= (n - len(validas)) * 2

    # conferencia de saldo/totais do proprio parser, quando existir
    if hasattr(mod, "info"):
        try:
            dados = mod.info(filepath)
            score += 1000
            notas.append("cabecalho ok")
            if hasattr(mod, "validar"):
                try:
                    avisos = mod.validar(brutas, dados)
                    if not avisos:
                        score += 5000
                        notas.append("saldos conferem")
                    else:
                        score -= 200 * len(avisos)
                        notas.append(f"{len(avisos)} aviso(s) de saldo")
                except Exception:
                    score -= 100
        except Exception:
            score -= 500
            notas.append("cabecalho nao reconhecido")
    return score, ", ".join(notas)


def detectar_e_parse_pdf(filepath: str):
    """Testa todos os layouts de parsers/ e devolve
    (nome_layout, lancamentos, relatorio) do que melhor bater."""
    layouts, err = carregar_layouts()
    if err:
        messagebox.showerror(
            "Erro", f"Nao foi possivel carregar a pasta 'parsers':\n{err}")
        return None, [], []
    if not layouts:
        messagebox.showerror("Erro", "Nenhum layout em parsers/LAYOUTS.")
        return None, [], []

    relatorio = []
    melhor = None
    for nome, mod in layouts.items():
        try:
            brutas = mod.parse(filepath)
        except Exception as e:
            relatorio.append((nome, -1.0, f"erro: {type(e).__name__}"))
            continue
        try:
            score, nota = _pontuar_layout(mod, filepath, brutas or [])
        except Exception as e:
            score, nota = -1.0, f"erro ao pontuar: {e}"
        relatorio.append((nome, score, nota))
        if score > 0 and (melhor is None or score > melhor[0]):
            melhor = (score, nome, brutas)

    relatorio.sort(key=lambda r: r[1], reverse=True)
    if melhor is None:
        detalhes = "\n".join(f"- {n}: {nt}" for n, _, nt in relatorio)
        messagebox.showerror(
            "Erro",
            "Nenhum layout reconheceu este PDF.\n\nResultado dos testes:\n"
            + detalhes)
        return None, [], relatorio

    _, nome, brutas = melhor
    return nome, _converter_brutas(brutas), relatorio


# ══════════════════════════════════════════════════════════════════════════════
#  JSON helpers
# ══════════════════════════════════════════════════════════════════════════════

def _load_json(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config():
    c = _load_json(CONFIG_FILE)
    if not c:
        return {"profiles": [], "active": ""}
    return c


def save_config(cfg):
    _save_json(CONFIG_FILE, cfg)


def _pdir(name):
    return os.path.join(PERFIS_DIR, name)


def create_profile(name):
    d = _pdir(name)
    os.makedirs(d, exist_ok=True)
    for fname in ("memoria.json", "regras.json"):
        p = os.path.join(d, fname)
        if not os.path.exists(p):
            _save_json(p, {})


def delete_profile(name):
    d = _pdir(name)
    if os.path.isdir(d):
        shutil.rmtree(d)


def load_memoria(name):
    return _load_json(os.path.join(_pdir(name), "memoria.json"))


def save_memoria(name, data):
    _save_json(os.path.join(_pdir(name), "memoria.json"), data)


def load_regras(name):
    return _load_json(os.path.join(_pdir(name), "regras.json"))


def save_regras(name, data):
    _save_json(os.path.join(_pdir(name), "regras.json"), data)


# ══════════════════════════════════════════════════════════════════════════════
#  Normalizacao
# ══════════════════════════════════════════════════════════════════════════════

def normalize_desc(desc: str) -> str:
    cleaned = re.sub(r'\s*DOC\.\s*:\s*\S+.*$', '', desc.strip(),
                     flags=re.IGNORECASE)
    cleaned = re.sub(r'\s{2,}[\d/\s]+$', '', cleaned.strip())
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    return cleaned if cleaned else desc.strip()


def group_key_desc(desc: str) -> str:
    base = normalize_desc(desc)
    key = base.replace("|", " ")
    key = key.replace(".", " ")
    key = re.sub(r'\d+', ' ', key)
    key = re.sub(r'[\-/_:]+', ' ', key)
    key = re.sub(r'\s{2,}', ' ', key).strip().upper()
    return key if key else base.upper()


def extrair_doc(memo: str) -> str:
    m = re.search(r'DOC\.\s*:\s*(\S+)', memo, re.IGNORECASE)
    return m.group(1).strip() if m else ""


# ══════════════════════════════════════════════════════════════════════════════
#  Parser OFX
# ══════════════════════════════════════════════════════════════════════════════

def _decode_ofx_bytes(raw: bytes) -> str:
    encodings_to_try = []

    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        encodings_to_try.append("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        encodings_to_try.append("utf-8-sig")

    head = raw[:400]
    try:
        head_txt = head.decode("ascii", errors="ignore")
        cs = re.search(r'CHARSET\s*:\s*([\w-]+)', head_txt, re.IGNORECASE)
        if cs:
            val = cs.group(1).upper()
            if "1252" in val:
                encodings_to_try.append("cp1252")
            elif "8859" in val:
                encodings_to_try.append("latin-1")
            elif "UTF-8" in val or "UTF8" in val:
                encodings_to_try.append("utf-8")
    except Exception:
        pass

    encodings_to_try += ["utf-8", "utf-8-sig", "utf-16", "cp1252", "latin-1"]

    for enc in encodings_to_try:
        try:
            text = raw.decode(enc)
            if enc != "utf-16" and text.count("\x00") > len(text) * 0.1:
                continue
            return text
        except Exception:
            continue

    return raw.decode("latin-1", errors="replace")


def _get_tag(block: str, tag: str) -> str:
    m = re.search(
        rf'<{tag}[^>]*>\s*(.*?)\s*(?=<{tag}[^>]*/?>|</{tag}>|<[A-Z0-9.]+[>\s]|\Z)',
        block, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    val = m.group(1).strip()
    val = html.unescape(val)
    val = re.sub(r'\s*\n\s*', ' ', val).strip()
    return val


def parse_ofx(filepath: str) -> list:
    try:
        with open(filepath, "rb") as f:
            raw = f.read()
    except Exception as e:
        messagebox.showerror("Erro", f"Nao foi possivel abrir o arquivo:\n{e}")
        return []

    content = _decode_ofx_bytes(raw)
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    transactions = []
    blocks = re.split(r'<STMTTRN>', content, flags=re.IGNORECASE)

    if len(blocks) <= 1:
        messagebox.showwarning(
            "Atencao",
            "Nao foram encontrados lancamentos (<STMTTRN>) no arquivo.\n"
            "Verifique se o arquivo e realmente um OFX valido.")
        return []

    for block in blocks[1:]:
        end = re.search(r'</STMTTRN>', block, re.IGNORECASE)
        if end:
            block = block[:end.start()]

        trntype  = _get_tag(block, "TRNTYPE")
        dtposted = _get_tag(block, "DTPOSTED")
        trnamt   = _get_tag(block, "TRNAMT")
        fitid    = _get_tag(block, "FITID")
        memo     = _get_tag(block, "MEMO")
        name     = _get_tag(block, "NAME")
        checknum = _get_tag(block, "CHECKNUM")

        date_str = ""
        dm = re.match(r'(\d{4})(\d{2})(\d{2})', dtposted)
        if dm:
            date_str = f"{dm.group(3)}/{dm.group(2)}/{dm.group(1)}"

        trntype_up = trntype.upper()
        credit_types = {"CREDIT", "DEP", "DIRECTDEP", "INT", "DIV", "REPEATPMT"}
        debit_types = {"DEBIT", "ATM", "POS", "XFER", "CHECK", "PAYMENT",
                       "CASH", "FEE", "SRVCHG", "DIRECTDEBIT"}

        valor_raw = trnamt.replace(" ", "")
        if "," in valor_raw and "." in valor_raw:
            valor_raw = valor_raw.replace(".", "").replace(",", ".")
        elif "," in valor_raw:
            valor_raw = valor_raw.replace(",", ".")

        try:
            valor_num = float(valor_raw)
        except Exception:
            valor_num = 0.0

        if trntype_up in credit_types:
            tipo = "C"
        elif trntype_up in debit_types:
            tipo = "D"
        else:
            tipo = "C" if valor_num >= 0 else "D"

        valor = abs(valor_num)

        descricao = memo if memo else (name if name else checknum)
        if not descricao:
            descricao = "(sem descricao)"

        num_doc = extrair_doc(memo) or checknum

        transactions.append({
            "tipo":      tipo,
            "data":      date_str,
            "valor":     valor,
            "descricao": descricao,
            "fitid":     fitid,
            "num_doc":   num_doc,
            "uid":       None,
        })

    seen_fitid = {}
    for i, lan in enumerate(transactions):
        base = lan["fitid"] if lan["fitid"] else f"NOFITID-{i}"
        n = seen_fitid.get(base, 0)
        seen_fitid[base] = n + 1
        lan["uid"] = base if n == 0 else f"{base}#{n}"

    return transactions


# ══════════════════════════════════════════════════════════════════════════════
#  Gerar TXT
# ══════════════════════════════════════════════════════════════════════════════

def gerar_txt(lancamentos: list, codigo_por_uid: dict, out_path: str) -> int:
    linhas = []
    for lan in lancamentos:
        codigo = codigo_por_uid.get(lan["uid"], "").strip()
        if not codigo:
            continue
        data    = lan["data"]
        valor   = f"{lan['valor']:.2f}"
        num_doc = lan.get("num_doc", "")
        linhas.append(
            f'"{codigo}","","","{data}","{valor}","","{num_doc}","{num_doc}"')

    with open(out_path, "w", encoding="latin-1", errors="replace") as f:
        f.write("\n".join(linhas))
    return len(linhas)


COLUMNS = [
    ("tipo",      "Tp",                3),
    ("data",      "Data",             10),
    ("valor",     "Valor (R$)",       12),
    ("descricao", "Descricao / Nome", 52),
    ("codigo",    "Lanc. Automatico", 18),
    (None,        "Acao",             12),
]


# ══════════════════════════════════════════════════════════════════════════════
#  App
# ══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Conversor Extrato (OFX/PDF) -> TXT  |  Alterdata  |  v4")
        self.geometry("1200x720")
        self.minsize(950, 500)
        self.configure(bg="#1a2236")

        self.cfg: dict           = load_config()
        self.perfil: str         = self.cfg.get("active", "")
        self.memoria: dict       = {}
        self.regras: dict        = {}
        self.codigo_por_uid: dict = {}
        self.lancamentos: list   = []
        self.view_order: list    = []

        self.ofx_path_var = tk.StringVar(value="Nenhum arquivo selecionado")
        self._sort_key    = None
        self._sort_asc    = True
        self._col_buttons: dict = {}
        self._vis: dict         = {}
        self._vp_after          = None

        self._migrate()
        self._load_perfil_data()
        self._build_ui()
        if self.perfil:
            self._pvar.set(self.perfil)
        self._refresh_profile_ui()

    def _migrate(self):
        if not os.path.exists(OLD_MEMORY):
            return
        if "Padrao" in self.cfg.get("profiles", []):
            return
        self.cfg.setdefault("profiles", []).append("Padrao")
        self.cfg["active"] = "Padrao"
        save_config(self.cfg)
        create_profile("Padrao")
        try:
            with open(OLD_MEMORY, "r", encoding="utf-8") as f:
                old = json.load(f)
            save_memoria("Padrao", old)
        except Exception:
            pass
        self.perfil = "Padrao"

    def _load_perfil_data(self):
        if not self.perfil:
            self.memoria = {}
            self.regras  = {}
            return
        self.memoria = load_memoria(self.perfil)
        self.regras  = load_regras(self.perfil)

    # ═══════════════════════════════════════════════════════════════════════
    #  UI
    # ═══════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        hdr = tk.Frame(self, bg="#0d1526", pady=8)
        hdr.pack(fill="x")

        tk.Label(hdr, text="Conversor Extrato -> TXT  |  Alterdata",
                 font=("Consolas", 15, "bold"), fg="#4fc3f7",
                 bg="#0d1526").pack(side="left", padx=18)
        tk.Label(hdr, textvariable=self.ofx_path_var,
                 font=("Consolas", 9), fg="#607d8b",
                 bg="#0d1526").pack(side="left", padx=8)

        tk.Button(hdr, text="Abrir PDF / OFX",
                  font=("Consolas", 10, "bold"),
                  bg="#f57c00", fg="white", relief="flat",
                  padx=14, pady=5, cursor="hand2",
                  command=self._abrir_extrato).pack(side="right", padx=10)
        tk.Button(hdr, text="Gerar TXT",
                  font=("Consolas", 10, "bold"),
                  bg="#2e7d32", fg="white", relief="flat",
                  padx=14, pady=5, cursor="hand2",
                  command=self._gerar_txt).pack(side="right", padx=4)

        pbar = tk.Frame(self, bg="#12203d", pady=6)
        pbar.pack(fill="x")

        tk.Label(pbar, text="Perfil:",
                 font=("Consolas", 10, "bold"), fg="#90caf9",
                 bg="#12203d").pack(side="left", padx=(16, 4))

        self._pvar = tk.StringVar()
        self._pcombo = ttk.Combobox(pbar, textvariable=self._pvar,
                                    state="readonly", width=22,
                                    font=("Consolas", 10))
        self._pcombo.pack(side="left", padx=4)
        self._pcombo.bind("<<ComboboxSelected>>", self._on_profile_sel)

        tk.Button(pbar, text="+ Novo Perfil",
                  font=("Consolas", 9, "bold"),
                  bg="#1565c0", fg="white", relief="flat",
                  padx=8, pady=2, cursor="hand2",
                  command=self._novo_perfil).pack(side="left", padx=4)
        tk.Button(pbar, text="Excluir Perfil",
                  font=("Consolas", 9),
                  bg="#7f0000", fg="#ffcdd2", relief="flat",
                  padx=8, pady=2, cursor="hand2",
                  command=self._excluir_perfil).pack(side="left", padx=4)

        tk.Button(pbar, text="Regras de Palavras-chave",
                  font=("Consolas", 10, "bold"),
                  bg="#4a148c", fg="#e1bee7", relief="flat",
                  padx=12, pady=4, cursor="hand2",
                  command=self._abrir_regras).pack(side="right", padx=16)

        self.status_var = tk.StringVar(
            value="Selecione ou crie um perfil para comecar.")
        tk.Label(self, textvariable=self.status_var,
                 font=("Consolas", 8), fg="#aed581",
                 bg="#1a2236", anchor="w").pack(fill="x", padx=16, pady=(4, 0))

        col_hdr = tk.Frame(self, bg="#263350", pady=4)
        col_hdr.pack(fill="x", padx=10, pady=(4, 0))

        for sort_key, label, width in COLUMNS:
            if sort_key is None:
                tk.Label(col_hdr, text=label, font=("Consolas", 9, "bold"),
                         fg="#90a4ae", bg="#263350", width=width,
                         anchor="w").pack(side="left", padx=4)
            else:
                btn = tk.Button(
                    col_hdr, text=label,
                    font=("Consolas", 9, "bold"),
                    fg="#90a4ae", bg="#263350",
                    activeforeground="#4fc3f7", activebackground="#1e3050",
                    relief="flat", bd=0, padx=4, pady=0,
                    width=width, anchor="w", cursor="hand2",
                    command=lambda k=sort_key: self._sort_by(k))
                btn.pack(side="left", padx=4)
                self._col_buttons[sort_key] = btn

        wrap = tk.Frame(self, bg="#1a2236")
        wrap.pack(fill="both", expand=True, padx=10, pady=(2, 8))

        self.canvas = tk.Canvas(wrap, bg="#1a2236", highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical",
                            command=self._scroll_cmd)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg="#1a2236")
        self._cwin = self.canvas.create_window(
            (0, 0), window=self.inner, anchor="nw")

        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    # ═══════════════════════════════════════════════════════════════════════
    #  Perfis
    # ═══════════════════════════════════════════════════════════════════════

    def _refresh_profile_ui(self):
        profiles = self.cfg.get("profiles", [])
        self._pcombo["values"] = profiles
        if self.perfil in profiles:
            self._pvar.set(self.perfil)
        elif profiles:
            self.perfil = profiles[0]
            self._pvar.set(profiles[0])
            self.cfg["active"] = self.perfil
            save_config(self.cfg)
            self._load_perfil_data()
        self._update_status()

    def _on_profile_sel(self, _=None):
        name = self._pvar.get()
        if name and name != self.perfil:
            self._switch_profile(name)

    def _switch_profile(self, name):
        self.perfil = name
        self.cfg["active"] = name
        save_config(self.cfg)
        self._load_perfil_data()
        if self.lancamentos:
            self._rebuild_codigo_map()
            self._reset_virtual_canvas()
        self._update_status()

    def _novo_perfil(self):
        win = tk.Toplevel(self)
        win.title("Novo Perfil")
        win.geometry("360x145")
        win.configure(bg="#1a2236")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        tk.Label(win, text="Nome do perfil:",
                 font=("Consolas", 11), fg="#e0e0e0",
                 bg="#1a2236").pack(pady=(18, 4))

        var = tk.StringVar()
        ent = tk.Entry(win, textvariable=var, font=("Consolas", 11),
                       bg="#0d1b2a", fg="#80cbc4",
                       insertbackground="#80cbc4",
                       relief="flat", bd=4, width=28)
        ent.pack(pady=4)
        ent.focus_set()

        def ok():
            name = var.get().strip()
            if not name:
                messagebox.showwarning("Atencao", "Digite um nome.",
                                       parent=win)
                return
            if name in self.cfg.get("profiles", []):
                messagebox.showwarning("Atencao", "Esse perfil ja existe.",
                                       parent=win)
                return
            self.cfg.setdefault("profiles", []).append(name)
            self.cfg["active"] = name
            save_config(self.cfg)
            create_profile(name)
            self.perfil = name
            self._load_perfil_data()
            self._refresh_profile_ui()
            if self.lancamentos:
                self._rebuild_codigo_map()
                self._reset_virtual_canvas()
            win.destroy()

        tk.Button(win, text="Criar", font=("Consolas", 10, "bold"),
                  bg="#2e7d32", fg="white", relief="flat",
                  padx=20, pady=4, cursor="hand2",
                  command=ok).pack(pady=8)
        ent.bind("<Return>", lambda e: ok())

    def _excluir_perfil(self):
        name = self._pvar.get()
        if not name:
            return
        if not messagebox.askyesno(
                "Confirmar",
                f"Excluir o perfil '{name}' e todos os dados salvos?"):
            return
        delete_profile(name)
        self.cfg["profiles"].remove(name)
        if self.cfg["active"] == name:
            self.cfg["active"] = ""
        save_config(self.cfg)

        if self.cfg["profiles"]:
            self._switch_profile(self.cfg["profiles"][0])
        else:
            self.perfil         = ""
            self.memoria        = {}
            self.regras         = {}
            self.codigo_por_uid = {}
            self.lancamentos    = []
            self.view_order     = []
            for info in self._vis.values():
                info["frame"].destroy()
            self._vis.clear()
        self._refresh_profile_ui()

    # ═══════════════════════════════════════════════════════════════════════
    #  Regras
    # ═══════════════════════════════════════════════════════════════════════

    def _abrir_regras(self):
        if not self.perfil:
            messagebox.showwarning(
                "Atencao", "Selecione ou crie um perfil primeiro.")
            return

        win = tk.Toplevel(self)
        win.title(f"Regras de Palavras-chave  —  {self.perfil}")
        win.geometry("660x540")
        win.configure(bg="#1a2236")
        win.resizable(True, True)
        win.transient(self)
        win.grab_set()

        tk.Label(win, text=f"Regras do perfil:  {self.perfil}",
                 font=("Consolas", 13, "bold"), fg="#ce93d8",
                 bg="#1a2236").pack(pady=(14, 2))
        tk.Label(win,
                 text="Cadastre um trecho (palavra ou frase) e um codigo.\n"
                      "Ao abrir um extrato, todo lancamento que contenha\n"
                      "o trecho sera preenchido automaticamente.",
                 font=("Consolas", 8), fg="#78909c",
                 bg="#1a2236", justify="left").pack(padx=20, pady=(0, 10))

        tf = tk.Frame(win, bg="#1a2236")
        tf.pack(fill="both", expand=True, padx=16, pady=(0, 6))

        cols = ("kw", "code")
        tree = ttk.Treeview(tf, columns=cols, show="headings", height=10)
        tree.heading("kw",   text="Palavra-chave / Trecho")
        tree.heading("code", text="Codigo")
        tree.column("kw",   width=440, minwidth=200)
        tree.column("code", width=110, minwidth=80)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Rules.Treeview",
                        background="#0d1b2a", foreground="#eceff1",
                        fieldbackground="#0d1b2a", rowheight=26,
                        font=("Consolas", 9))
        style.configure("Rules.Treeview.Heading",
                        background="#263350", foreground="#90caf9",
                        font=("Consolas", 9, "bold"))
        tree.configure(style="Rules.Treeview")

        vsb = ttk.Scrollbar(tf, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        def refresh_tree():
            for i in tree.get_children():
                tree.delete(i)
            for kw, code in sorted(self.regras.items(),
                                   key=lambda x: x[0].lower()):
                tree.insert("", "end", values=(kw, code))
        refresh_tree()

        form = tk.Frame(win, bg="#1a2236")
        form.pack(fill="x", padx=16, pady=4)

        tk.Label(form, text="Trecho:",
                 font=("Consolas", 10), fg="#b0bec5",
                 bg="#1a2236").grid(row=0, column=0, sticky="w", padx=(0, 6))
        kw_var = tk.StringVar()
        kw_ent = tk.Entry(form, textvariable=kw_var,
                          font=("Consolas", 10),
                          bg="#0d1b2a", fg="#80cbc4",
                          insertbackground="#80cbc4",
                          relief="flat", bd=3, width=40)
        kw_ent.grid(row=0, column=1, padx=4, pady=3, columnspan=3,
                    sticky="ew")

        tk.Label(form, text="Codigo:",
                 font=("Consolas", 10), fg="#b0bec5",
                 bg="#1a2236").grid(row=1, column=0, sticky="w", padx=(0, 6))
        code_var = tk.StringVar()
        code_ent = tk.Entry(form, textvariable=code_var,
                            font=("Consolas", 10),
                            bg="#0d1b2a", fg="#80cbc4",
                            insertbackground="#80cbc4",
                            relief="flat", bd=3, width=14)
        code_ent.grid(row=1, column=1, sticky="w", padx=4, pady=3)

        form.columnconfigure(1, weight=1)

        def add_rule():
            kw   = kw_var.get().strip()
            code = code_var.get().strip()
            if not kw or not code:
                messagebox.showwarning(
                    "Atencao", "Preencha trecho e codigo.", parent=win)
                return
            self.regras[kw] = code
            save_regras(self.perfil, self.regras)
            refresh_tree()
            kw_var.set("")
            code_var.set("")
            kw_ent.focus_set()

        def delete_sel():
            for item in tree.selection():
                kw = str(tree.item(item)["values"][0])
                self.regras.pop(kw, None)
            save_regras(self.perfil, self.regras)
            refresh_tree()

        def apply_rules():
            if not self.lancamentos:
                messagebox.showinfo(
                    "Info", "Nenhum extrato carregado.", parent=win)
                return
            n = self._apply_keyword_rules()
            self._reset_virtual_canvas()
            self._update_status()
            messagebox.showinfo(
                "Aplicado",
                f"Regras aplicadas. {n} lancamento(s) atualizado(s).",
                parent=win)

        bf = tk.Frame(form, bg="#1a2236")
        bf.grid(row=2, column=0, columnspan=4, pady=8)

        tk.Button(bf, text="Adicionar Regra",
                  font=("Consolas", 9, "bold"),
                  bg="#2e7d32", fg="white", relief="flat",
                  padx=10, pady=3, cursor="hand2",
                  command=add_rule).pack(side="left", padx=4)
        tk.Button(bf, text="Excluir Selecionada",
                  font=("Consolas", 9),
                  bg="#7f0000", fg="#ffcdd2", relief="flat",
                  padx=10, pady=3, cursor="hand2",
                  command=delete_sel).pack(side="left", padx=4)

        bot = tk.Frame(win, bg="#1a2236")
        bot.pack(fill="x", padx=16, pady=(2, 14))

        tk.Button(bot, text="Aplicar Regras ao Extrato Atual",
                  font=("Consolas", 10, "bold"),
                  bg="#f57c00", fg="white", relief="flat",
                  padx=14, pady=5, cursor="hand2",
                  command=apply_rules).pack(side="left", padx=4)
        tk.Button(bot, text="Fechar",
                  font=("Consolas", 10),
                  bg="#37474f", fg="#eceff1", relief="flat",
                  padx=14, pady=5, cursor="hand2",
                  command=win.destroy).pack(side="right", padx=4)

        kw_ent.bind("<Return>", lambda e: add_rule())
        code_ent.bind("<Return>", lambda e: add_rule())

    def _match_keyword(self, descricao: str):
        dn = normalize_desc(descricao)
        for kw, code in self.regras.items():
            if kw.upper() in dn.upper():
                return code
        return None

    def _apply_keyword_rules(self):
        count = 0
        for lan in self.lancamentos:
            if self.codigo_por_uid.get(lan["uid"], "").strip():
                continue
            code = self._match_keyword(lan["descricao"])
            if code:
                self.codigo_por_uid[lan["uid"]] = code
                count += 1
        return count

    def _rebuild_codigo_map(self):
        self.codigo_por_uid = {}
        for lan in self.lancamentos:
            gk = group_key_desc(lan["descricao"])
            dn = normalize_desc(lan["descricao"])
            codigo = None
            if gk in self.memoria and str(self.memoria[gk]).strip():
                codigo = self.memoria[gk]
            elif dn in self.memoria and str(self.memoria[dn]).strip():
                codigo = self.memoria[dn]
            if codigo:
                self.codigo_por_uid[lan["uid"]] = codigo

        for lan in self.lancamentos:
            if lan["uid"] in self.codigo_por_uid:
                continue
            code = self._match_keyword(lan["descricao"])
            if code:
                self.codigo_por_uid[lan["uid"]] = code

    # ═══════════════════════════════════════════════════════════════════════
    #  Scroll / Viewport
    # ═══════════════════════════════════════════════════════════════════════

    def _scroll_cmd(self, *args):
        self.canvas.yview(*args)
        self._sched_vp()

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._sched_vp()

    def _on_canvas_resize(self, event):
        self.canvas.itemconfig(self._cwin, width=event.width)
        self._sched_vp()

    def _sched_vp(self):
        if self._vp_after:
            self.after_cancel(self._vp_after)
        self._vp_after = self.after(30, self._update_viewport)

    def _reset_virtual_canvas(self):
        for info in self._vis.values():
            info["frame"].destroy()
        self._vis.clear()
        total_h = len(self.view_order) * ROW_H
        self.inner.configure(height=max(total_h, 1))
        self.canvas.configure(
            scrollregion=(0, 0, self.canvas.winfo_width(), total_h))
        self.canvas.yview_moveto(0)
        self._update_viewport()

    def _update_viewport(self):
        self._vp_after = None
        n = len(self.view_order)
        if n == 0:
            return
        canvas_h = self.canvas.winfo_height() or 500
        total_h  = n * ROW_H
        top_px   = self.canvas.yview()[0] * total_h

        first = max(0, int(top_px // ROW_H) - OVERSCAN)
        last  = min(n - 1, int((top_px + canvas_h) // ROW_H) + OVERSCAN)

        needed  = set(range(first, last + 1))
        current = set(self._vis.keys())

        for di in current - needed:
            self._vis[di]["frame"].destroy()
            del self._vis[di]
        for di in needed - current:
            lan = self.lancamentos[self.view_order[di]]
            self._make_row(di, lan)
        for di in needed & current:
            self._vis[di]["frame"].place(
                x=0, y=di * ROW_H, relwidth=1, height=ROW_H)

    def _make_row(self, di, lan):
        uid  = lan["uid"]
        code = self.codigo_por_uid.get(uid, "")
        bg   = "#1e2d45" if di % 2 == 0 else "#18263c"

        row = tk.Frame(self.inner, bg=bg, height=ROW_H)
        row.place(x=0, y=di * ROW_H, relwidth=1, height=ROW_H)
        row.pack_propagate(False)

        cor = "#4caf50" if lan["tipo"] == "C" else "#ef5350"
        tk.Label(row, text=lan["tipo"], font=("Consolas", 9, "bold"),
                 fg=cor, bg=bg, width=3).pack(side="left", padx=4)
        tk.Label(row, text=lan["data"], font=("Consolas", 9),
                 fg="#cfd8dc", bg=bg, width=11).pack(side="left")
        vf = (f"{lan['valor']:>13,.2f}"
              .replace(",", "X").replace(".", ",").replace("X", "."))
        tk.Label(row, text=vf, font=("Consolas", 9),
                 fg="#fff9c4", bg=bg, width=14,
                 anchor="e").pack(side="left", padx=(0, 6))

        d = lan["descricao"]
        desc_var = tk.StringVar(value=(d[:56] + "…") if len(d) > 56 else d)
        desc_entry = tk.Entry(row, textvariable=desc_var,
                              font=("Consolas", 9),
                              fg="#eceff1", bg=bg,
                              readonlybackground=bg,
                              selectbackground="#2a4a7f",
                              selectforeground="#ffffff",
                              relief="flat", bd=0,
                              state="readonly", width=58,
                              cursor="xterm", justify="left")
        desc_entry.pack(side="left")
        var   = tk.StringVar(value=code)
        entry = tk.Entry(row, textvariable=var,
                         font=("Consolas", 10, "bold"),
                         bg="#0d1b2a", fg="#80cbc4",
                         insertbackground="#80cbc4",
                         relief="flat", bd=2, width=18)
        entry.pack(side="left", padx=6)

        self._vis[di] = {"frame": row, "var": var,
                         "entry": entry, "uid": uid}

        def on_confirm(e, _uid=uid, _v=var, _en=entry):
            self._confirmar(_uid, _v.get().strip(), _en)
        for seq in ("<Return>", "<Tab>", "<FocusOut>"):
            entry.bind(seq, on_confirm)

        tk.Button(row, text="X Desvincular",
                  font=("Consolas", 8),
                  bg="#7f0000", fg="#ffcdd2",
                  relief="flat", padx=4, pady=1, cursor="hand2",
                  command=lambda _uid=uid, _v=var:
                      self._desvincular(_uid, _v)
                  ).pack(side="left", padx=4)

    # ═══════════════════════════════════════════════════════════════════════
    #  Confirmar / Desvincular
    # ═══════════════════════════════════════════════════════════════════════

    def _confirmar(self, uid, codigo, entry_origem):
        lan = next((l for l in self.lancamentos if l["uid"] == uid), None)
        if lan is None:
            return

        if codigo:
            gk = group_key_desc(lan["descricao"])
            self.memoria[gk] = codigo
            if self.perfil:
                save_memoria(self.perfil, self.memoria)

            for outro in self.lancamentos:
                if group_key_desc(outro["descricao"]) == gk:
                    self.codigo_por_uid[outro["uid"]] = codigo

            for info in self._vis.values():
                info_lan = next(
                    (l for l in self.lancamentos if l["uid"] == info["uid"]),
                    None)
                if info_lan is None:
                    continue
                if group_key_desc(info_lan["descricao"]) == gk:
                    info["var"].set(codigo)
                    if info["entry"] is not entry_origem:
                        info["entry"].configure(bg="#163320")
                        self.after(
                            500,
                            lambda e=info["entry"]: e.configure(bg="#0d1b2a"))
        else:
            self.codigo_por_uid.pop(uid, None)

        self._update_status()

    def _desvincular(self, uid, var):
        self.codigo_por_uid.pop(uid, None)
        var.set("")
        self._update_status()

    # ═══════════════════════════════════════════════════════════════════════
    #  Abrir extrato (PDF ou OFX)
    # ═══════════════════════════════════════════════════════════════════════

    def _abrir_extrato(self):
        if not self.perfil:
            messagebox.showwarning(
                "Atencao", "Selecione ou crie um perfil primeiro.")
            return

        path = filedialog.askopenfilename(
            title="Selecionar extrato",
            filetypes=[("Extratos (PDF/OFX)", "*.pdf *.ofx"),
                       ("PDF", "*.pdf"), ("OFX", "*.ofx"),
                       ("Todos", "*.*")])
        if not path:
            return

        ext = os.path.splitext(path)[1].lower()
        layout_usado = None
        if ext == ".pdf":
            self.status_var.set("Detectando layout do PDF...")
            self.config(cursor="watch")
            self.update_idletasks()
            try:
                layout_usado, lancs, _ = detectar_e_parse_pdf(path)
            finally:
                self.config(cursor="")
        else:
            lancs = parse_ofx(path)

        if not lancs:
            self._update_status()
            return

        self.ofx_path_var.set(os.path.basename(path))
        self.lancamentos = lancs
        self._rebuild_codigo_map()
        self.view_order = list(range(len(self.lancamentos)))
        self._sort_key  = None
        self._sort_asc  = True
        for k, btn in self._col_buttons.items():
            base = btn.cget("text").replace(" ▲", "").replace(" ▼", "")
            btn.configure(text=base, fg="#90a4ae")

        self._reset_virtual_canvas()
        self._update_status()
        if layout_usado:
            self.status_var.set(
                f"Layout detectado: {layout_usado}   |   "
                + self.status_var.get())

    # ═══════════════════════════════════════════════════════════════════════
    #  Ordenacao
    # ═══════════════════════════════════════════════════════════════════════

    def _sort_by(self, key):
        if not self.lancamentos:
            return
        if self._sort_key == key:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_key = key
            self._sort_asc = True

        def key_fn(idx):
            lan = self.lancamentos[idx]
            dn  = normalize_desc(lan["descricao"])
            if key == "tipo":
                return lan["tipo"]
            if key == "data":
                p = lan["data"].split("/")
                return f"{p[2]}{p[1]}{p[0]}" if len(p) == 3 else lan["data"]
            if key == "valor":
                return lan["valor"]
            if key == "descricao":
                return dn.lower()
            if key == "codigo":
                has = 0 if self.codigo_por_uid.get(lan["uid"], "").strip() else 1
                return (has, dn.lower())
            return ""

        self.view_order.sort(key=key_fn, reverse=not self._sort_asc)

        for k, btn in self._col_buttons.items():
            base = btn.cget("text").replace(" ▲", "").replace(" ▼", "")
            if k == key:
                arrow = " ▲" if self._sort_asc else " ▼"
                btn.configure(text=base + arrow, fg="#4fc3f7")
            else:
                btn.configure(text=base, fg="#90a4ae")

        self._reset_virtual_canvas()

    def _update_status(self):
        total = len(self.lancamentos)
        com   = sum(
            1 for l in self.lancamentos
            if self.codigo_por_uid.get(l["uid"], "").strip())
        sem = total - com
        self.status_var.set(
            f"Perfil: {self.perfil or '(nenhum)'}   |   "
            f"Total: {total}   OK: {com}   Sem codigo: {sem}   |   "
            f"Regras: {len(self.regras)}   |   "
            f"Memoria: {len(self.memoria)}")

    def _gerar_txt(self):
        if not self.lancamentos:
            messagebox.showwarning("Atencao", "Nenhum extrato carregado.")
            return

        sem = sum(
            1 for l in self.lancamentos
            if not self.codigo_por_uid.get(l["uid"], "").strip())
        if sem and not messagebox.askyesno(
                "Confirmar exportacao",
                f"{sem} lancamento(s) sem codigo NAO serao exportados.\n"
                "Deseja continuar mesmo assim?"):
            return

        out = filedialog.asksaveasfilename(
            title="Salvar TXT",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("Todos", "*.*")])
        if not out:
            return

        n = gerar_txt(self.lancamentos, self.codigo_por_uid, out)
        messagebox.showinfo(
            "Exportado!", f"{n} lancamentos exportados para:\n{out}")


if __name__ == "__main__":
    app = App()
    app.mainloop()