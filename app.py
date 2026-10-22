#!/usr/bin/env python3
"""ConvertHub (pywebview) - unificação de:
  • PDF -> OFX            (antigo app.py, Tkinter)
  • Extrato -> TXT        (antigo ofx_pdf_para_txt.py, Tkinter, Alterdata)

Requisitos na mesma pasta deste arquivo (projeto existente):
  - ofx_writer.py  (gerar_ofx)
  - parsers/       (LAYOUTS)
Dependências: pip install pywebview pdfplumber
"""

import base64
import html as _html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
from datetime import date, datetime
from pathlib import Path

# ── Caminhos ────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", sys.executable)).resolve()
else:
    BASE_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = BASE_DIR

for _p in (str(BASE_DIR), str(BUNDLE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import webview  # noqa: E402

IMPORT_ERRORS: dict = {}
try:
    from ofx_writer import gerar_ofx
except Exception as _e:  # pragma: no cover
    gerar_ofx = None
    IMPORT_ERRORS["ofx_writer"] = f"{type(_e).__name__}: {_e}"
try:
    from parsers import LAYOUTS
except Exception as _e:  # pragma: no cover
    LAYOUTS = {}
    IMPORT_ERRORS["parsers"] = f"{type(_e).__name__}: {_e}"

JSON_PATH = BASE_DIR / "layouts_imagens.json"
IMG_DIR = BASE_DIR / "imagens_layouts"
PERFIS_DIR = BASE_DIR / "perfis"
CONFIG_FILE = BASE_DIR / "perfis_config.json"
OLD_MEMORY = BASE_DIR / "lancamentos_memoria.json"
STATS_FILE = BASE_DIR / "converthub_stats.json"


# ══════════════════════════════════════════════════════════════════════
#  JSON helpers
# ══════════════════════════════════════════════════════════════════════
def _load_json(path, default=None):
    path = Path(path)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {} if default is None else default


def _save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════════
#  Imagem / observação por layout  (compatível com layouts_imagens.json)
# ══════════════════════════════════════════════════════════════════════
class ImagemStore:
    def __init__(self, json_path: Path, img_dir: Path):
        self.json_path = json_path
        self.img_dir = img_dir
        self.data: dict = {}
        self.load()

    def load(self):
        for p in (self.json_path, BUNDLE_DIR / "layouts_imagens.json"):
            if p.is_file():
                try:
                    self.data = json.loads(p.read_text(encoding="utf-8"))
                    return
                except Exception:
                    pass
        self.data = {}

    def save(self):
        self.json_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8")

    @staticmethod
    def _slug(key: str) -> str:
        return re.sub(r"[^\w\-]+", "_", key).strip("_")

    def _chave(self, key: str) -> str:
        alvo = key.strip().casefold()
        for k in self.data:
            if k.strip().casefold() == alvo:
                return k
        return key

    def get(self, key: str) -> dict:
        return self.data.get(self._chave(key), {})

    def resolve_imagem(self, key: str):
        entry = self.get(key)
        candidatos = []
        for campo in ("imagem", "imagem_original"):
            valor = entry.get(campo)
            if valor:
                p = Path(valor)
                if p.is_absolute():
                    candidatos.append(p)
                else:
                    candidatos.append(BASE_DIR / p)
                    candidatos.append(BUNDLE_DIR / p)
        nomes = {key.strip().casefold(), self._slug(key).casefold()}
        for pasta in (self.img_dir, BUNDLE_DIR / "imagens_layouts"):
            if pasta.is_dir():
                for f in sorted(pasta.iterdir()):
                    if f.is_file() and f.stem.casefold() in nomes:
                        candidatos.append(f)
        for c in candidatos:
            if c.is_file():
                return c
        return None

    def data_uri(self, key: str):
        p = self.resolve_imagem(key)
        if not p:
            return None
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        try:
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        except Exception:
            return None
        return f"data:{mime};base64,{b64}"

    def set_imagem(self, key: str, nome: str, origem: Path):
        self.img_dir.mkdir(parents=True, exist_ok=True)
        key = self._chave(key)
        destino = self.img_dir / f"{self._slug(key)}{origem.suffix.lower()}"
        if origem.resolve() != destino.resolve():
            shutil.copy2(origem, destino)
        entry = self.data.setdefault(key, {})
        try:
            rel = destino.relative_to(BASE_DIR).as_posix()
        except ValueError:
            rel = str(destino)
        entry.update({
            "nome": nome,
            "imagem": rel,
            "imagem_original": str(origem),
            "atualizado_em": datetime.now().isoformat(timespec="seconds"),
        })
        self.save()

    def set_descricao(self, key: str, nome: str, texto: str):
        key = self._chave(key)
        entry = self.data.setdefault(key, {})
        entry["nome"] = nome
        entry["descricao"] = texto.strip()
        entry["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
        self.save()

    def remove_imagem(self, key: str):
        entry = self.data.get(self._chave(key))
        if not entry:
            return
        path = self.resolve_imagem(key)
        if path and path.is_file() and self.img_dir in path.parents:
            try:
                path.unlink()
            except OSError:
                pass
        entry.pop("imagem", None)
        entry.pop("imagem_original", None)
        entry["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
        self.save()


# ══════════════════════════════════════════════════════════════════════
#  Entrada manual: datas e valores
# ══════════════════════════════════════════════════════════════════════
def _parse_data_usuario(texto, ano_padrao=None):
    if not texto:
        return None
    texto = texto.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto, fmt).date()
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", texto)
    if m:
        dia, mes = int(m.group(1)), int(m.group(2))
        try:
            return date(ano_padrao or datetime.now().year, mes, dia)
        except ValueError:
            return None
    m = re.match(r"^(\d{2})(\d{2})(\d{4})$", texto)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def _parse_valor_usuario(texto, tipo_debito=False):
    if not texto:
        return None
    t = texto.strip().replace("R$", "").replace(" ", "")
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    sinal = -1 if tipo_debito else 1
    if t.upper().endswith("D"):
        sinal, t = -1, t[:-1]
    elif t.upper().endswith("C"):
        sinal, t = 1, t[:-1]
    elif t.startswith("-"):
        sinal, t = -1, t[1:]
    elif t.startswith("+"):
        sinal, t = 1, t[1:]
    try:
        val = float(t)
    except ValueError:
        return None
    return -abs(val) if sinal == -1 else abs(val)


def _brl(v):
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ══════════════════════════════════════════════════════════════════════
#  Normalização / OFX / TXT (módulo Extrato -> TXT)
# ══════════════════════════════════════════════════════════════════════
def normalize_desc(desc: str) -> str:
    cleaned = re.sub(r'\s*DOC\.\s*:\s*\S+.*$', '', desc.strip(),
                     flags=re.IGNORECASE)
    cleaned = re.sub(r'\s{2,}[\d/\s]+$', '', cleaned.strip())
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    return cleaned if cleaned else desc.strip()


def group_key_desc(desc: str, considerar_numeros: bool = False) -> str:
    base = normalize_desc(desc)
    if considerar_numeros:
        return "NUM|" + re.sub(r"\s+", " ", base).strip().upper()
    key = base.replace("|", " ").replace(".", " ")
    key = re.sub(r'\d+', ' ', key)
    key = re.sub(r'[\-/_:]+', ' ', key)
    key = re.sub(r'\s{2,}', ' ', key).strip().upper()
    return key if key else base.upper()


def extrair_doc(memo: str) -> str:
    m = re.search(r'DOC\.\s*:\s*(\S+)', memo, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _decode_ofx_bytes(raw: bytes) -> str:
    encs = []
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        encs.append("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        encs.append("utf-8-sig")
    try:
        head_txt = raw[:400].decode("ascii", errors="ignore")
        cs = re.search(r'CHARSET\s*:\s*([\w-]+)', head_txt, re.IGNORECASE)
        if cs:
            val = cs.group(1).upper()
            if "1252" in val:
                encs.append("cp1252")
            elif "8859" in val:
                encs.append("latin-1")
            elif "UTF-8" in val or "UTF8" in val:
                encs.append("utf-8")
    except Exception:
        pass
    encs += ["utf-8", "utf-8-sig", "utf-16", "cp1252", "latin-1"]
    for enc in encs:
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
    val = _html.unescape(m.group(1).strip())
    return re.sub(r'\s*\n\s*', ' ', val).strip()


def parse_ofx(filepath: str) -> list:
    with open(filepath, "rb") as f:
        raw = f.read()
    content = _decode_ofx_bytes(raw).replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r'<STMTTRN>', content, flags=re.IGNORECASE)
    if len(blocks) <= 1:
        raise ValueError("Nenhum lançamento (<STMTTRN>) encontrado. "
                         "Verifique se o arquivo é um OFX válido.")

    credit_types = {"CREDIT", "DEP", "DIRECTDEP", "INT", "DIV", "REPEATPMT"}
    debit_types = {"DEBIT", "ATM", "POS", "XFER", "CHECK", "PAYMENT", "CASH",
                   "FEE", "SRVCHG", "DIRECTDEBIT"}
    out = []
    for block in blocks[1:]:
        end = re.search(r'</STMTTRN>', block, re.IGNORECASE)
        if end:
            block = block[:end.start()]
        trntype = _get_tag(block, "TRNTYPE").upper()
        dtposted = _get_tag(block, "DTPOSTED")
        trnamt = _get_tag(block, "TRNAMT")
        fitid = _get_tag(block, "FITID")
        memo = _get_tag(block, "MEMO")
        name = _get_tag(block, "NAME")
        checknum = _get_tag(block, "CHECKNUM")

        date_str = ""
        dm = re.match(r'(\d{4})(\d{2})(\d{2})', dtposted)
        if dm:
            date_str = f"{dm.group(3)}/{dm.group(2)}/{dm.group(1)}"

        vr = trnamt.replace(" ", "")
        if "," in vr and "." in vr:
            vr = vr.replace(".", "").replace(",", ".")
        elif "," in vr:
            vr = vr.replace(",", ".")
        try:
            vnum = float(vr)
        except Exception:
            vnum = 0.0

        if trntype in credit_types:
            tipo = "C"
        elif trntype in debit_types:
            tipo = "D"
        else:
            tipo = "C" if vnum >= 0 else "D"

        out.append({
            "tipo": tipo, "data": date_str, "valor": abs(vnum),
            "descricao": memo or name or checknum or "(sem descricao)",
            "fitid": fitid,
            "num_doc": extrair_doc(memo) or checknum,
            "uid": None,
        })

    seen = {}
    for i, lan in enumerate(out):
        base = lan["fitid"] if lan["fitid"] else f"NOFITID-{i}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        lan["uid"] = base if n == 0 else f"{base}#{n}"
    return out


def transacoes_para_lancamentos(transacoes: list) -> list:
    """Converte transações dos parsers PDF para o formato do módulo TXT."""
    out = []
    for i, t in enumerate(transacoes):
        v = t.get("valor")
        d = t.get("data")
        if v is None or d is None:
            continue
        ds = d.strftime("%d/%m/%Y") if hasattr(d, "strftime") else str(d)
        desc = str(t.get("descricao") or t.get("memo") or "").strip()
        nome = str(t.get("nome") or "").strip()
        if nome and nome.upper() not in desc.upper():
            desc = f"{desc} {nome}".strip()
        out.append({
            "tipo": "C" if v >= 0 else "D",
            "data": ds,
            "valor": abs(float(v)),
            "descricao": desc or "(sem descricao)",
            "fitid": "",
            "num_doc": str(t.get("doc") or "").strip(),
            "uid": f"PDF-{i}",
        })
    return out


def gerar_txt(lancamentos, codigo_por_uid, out_path) -> int:
    linhas = []
    for lan in lancamentos:
        codigo = codigo_por_uid.get(lan["uid"], "").strip()
        if not codigo:
            continue
        doc = lan.get("num_doc", "")
        linhas.append(
            f'"{codigo}","","","{lan["data"]}","{lan["valor"]:.2f}","","{doc}","{doc}"')
    with open(out_path, "w", encoding="latin-1", errors="replace") as f:
        f.write("\n".join(linhas))
    return len(linhas)


# ── perfis ─────────────────────────────────────────────────────────
def _pdir(name):
    return PERFIS_DIR / name


def create_profile(name):
    d = _pdir(name)
    d.mkdir(parents=True, exist_ok=True)
    for fname in ("memoria.json", "regras.json"):
        p = d / fname
        if not p.exists():
            _save_json(p, {})


def delete_profile(name):
    d = _pdir(name)
    if d.is_dir():
        shutil.rmtree(d)


# ══════════════════════════════════════════════════════════════════════
#  Smart Layout (testa todos os parsers)
# ══════════════════════════════════════════════════════════════════════
def _norm_txt(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


_TOKENS_GENERICOS = {
    "banco", "bank", "extrato", "extratos", "conta", "corrente", "layout",
    "sistema", "cooperativo", "cooperativa", "cooperativas", "credito",
    "brasil", "financeiros", "financeira", "servicos", "plataforma",
    "saldo", "para", "movimentacao", "historico", "pagamentos", "digital",
}


def _amostra_pdf(pdf, paginas=2):
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf)) as doc:
            return "\n".join((pg.extract_text() or "") for pg in doc.pages[:paginas])
    except Exception:
        return ""


def ranquear_layouts(pdf):
    """Pré-análise: lê as primeiras páginas e pontua cada layout pelo nome do banco."""
    texto = _norm_txt(_amostra_pdf(pdf))
    if not texto.strip():
        return []
    ranking = []
    for key, mod in LAYOUTS.items():
        score = 0
        vistos = set()
        fontes = [key, getattr(mod, "NOME", ""), getattr(mod, "BANCO_NOME", ""),
                  getattr(mod, "BANCO_ORG", "")]
        for f in fontes:
            fn = _norm_txt(f).strip()
            if len(fn) >= 6 and fn in texto:
                score += 10
            for tok in re.findall(r"[a-z]{4,}", fn):
                if tok in _TOKENS_GENERICOS or tok in vistos:
                    continue
                vistos.add(tok)
                if re.search(r"(?<![a-z])" + re.escape(tok) + r"(?![a-z])", texto):
                    score += 3
        banco_id = str(getattr(mod, "BANCO_ID", "") or "").strip()
        if banco_id and banco_id != "000" and re.search(
                r"(?<!\d)" + re.escape(banco_id) + r"(?!\d)", texto):
            score += 1
        padrao = getattr(mod, "DETECTAR", None)
        if padrao:
            try:
                if re.search(padrao, texto, re.IGNORECASE):
                    score += 20
            except Exception:
                pass
        ranking.append((score, key))
    ranking.sort(key=lambda x: (-x[0], x[1]))
    return ranking


def smart_layout(pdf, log=lambda *a, **k: None):
    ranking = ranquear_layouts(pdf)
    pontos = {k: s for s, k in ranking}
    if ranking and ranking[0][0] > 0:
        ordem = [k for s, k in ranking if s > 0] + [k for s, k in ranking if s <= 0]
        log(f"Pré-análise: banco provável -> {ranking[0][1]} "
            f"(pontuação {ranking[0][0]})")
    else:
        ordem = sorted(LAYOUTS)
        log("Pré-análise: banco não identificado no texto; testando todos os layouts.")

    candidatos = []
    for key in ordem:
        mod = LAYOUTS[key]
        try:
            trans = mod.parse(pdf)
            if not trans:
                raise ValueError("nenhuma transação")
            incomp = sum(1 for t in trans
                         if t.get("incompleto") or t.get("data") is None
                         or t.get("valor") is None)
            avisos = 0
            try:
                info = mod.info(pdf)
                validar = getattr(mod, "validar", None) or \
                    getattr(mod, "avisos", None)
                if validar:
                    avisos = len(validar(trans, info))
            except Exception:
                avisos = 5
            candidatos.append(((incomp, avisos, -len(trans)), key, trans))
            log(f"Smart Layout: {key} -> {len(trans)} transações, "
                f"{incomp} incompletas, {avisos} avisos")
            if pontos.get(key, 0) > 0 and incomp == 0 and avisos == 0:
                log(f"Smart Layout: layout escolhido = {key} (pré-análise)", "ok")
                return key, trans
        except Exception as exc:
            log(f"Smart Layout: {key} descartado ({str(exc)[:80]})")
    if not candidatos:
        raise ValueError("Nenhum layout conseguiu ler este PDF.")
    candidatos.sort(key=lambda c: c[0])
    _, key, trans = candidatos[0]
    log(f"Smart Layout: layout escolhido = {key}", "ok")
    return key, trans


def info_fallback(layout, transacoes):
    datas = [t["data"] for t in transacoes if t.get("data")]
    agora = datetime.now().date()
    parser = LAYOUTS.get(layout)
    return {
        "banco_nome": getattr(parser, "BANCO_ORG",
                              getattr(parser, "BANCO_NOME", layout)),
        "banco_id": getattr(parser, "BANCO_ID", "000"),
        "agencia": "0000",
        "conta": "00000",
        "moeda": "BRL",
        "periodo_inicio": min(datas) if datas else agora,
        "periodo_fim": max(datas) if datas else agora,
        "saldo_final": None,
    }


def _item_view(t):
    d, v = t.get("data"), t.get("valor")
    desc = str(t.get("descricao") or t.get("memo") or "").strip()
    doc = str(t.get("doc") or "").strip()
    incomp = bool(t.get("incompleto") or d is None or v is None or not desc)
    motivo = t.get("motivo", "")
    if not motivo and incomp:
        f = []
        if d is None:
            f.append("Data")
        if not desc:
            f.append("Descrição")
        if v is None:
            f.append("Valor")
        motivo = f"Falta: {', '.join(f)}"
    return {
        "data": d.strftime("%d/%m/%Y") if d else "",
        "descricao": desc,
        "doc": doc,
        "valor": f"{abs(v):.2f}".replace(".", ",") if v is not None else "",
        "tipo": "C" if (v is not None and v > 0) else "D",
        "nome": t.get("nome") or t.get("name") or "",
        "incompleto": incomp,
        "motivo": motivo,
    }


# ══════════════════════════════════════════════════════════════════════
#  API exposta ao JavaScript
# ══════════════════════════════════════════════════════════════════════
class Api:
    def __init__(self):
        self._window = None
        self._store = ImagemStore(JSON_PATH, IMG_DIR)
        self._lock = threading.RLock()
        self._sessao = {}

        # TXT
        self._cfg = _load_json(CONFIG_FILE) or {"profiles": [], "active": ""}
        self._perfil = self._cfg.get("active", "")
        self._memoria = {}
        self._regras = {}
        self._lancs = []
        self._codigos = {}
        self._num = False
        self._txt_migrate()
        self._txt_load_perfil()

    # ── utilidades internas ─────────────────────────────────────────
    def _log(self, texto, tag=""):
        if not self._window:
            return
        try:
            self._window.evaluate_js(
                f"Hub.log({json.dumps(texto)},{json.dumps(tag)})")
        except Exception:
            pass

    def _dialog(self, kind, **kw):
        if not self._window:
            return None
        FD = getattr(webview, "FileDialog", None)
        mode = (getattr(FD, kind) if FD else
                getattr(webview, f"{kind}_DIALOG"))
        try:
            return self._window.create_file_dialog(mode, **kw)
        except Exception:
            return None

    def _bump(self, ok: bool):
        st = _load_json(STATS_FILE, {"days": {}})
        d = st.setdefault("days", {}).setdefault(
            date.today().isoformat(), {"ok": 0, "fail": 0})
        d["ok" if ok else "fail"] += 1
        _save_json(STATS_FILE, st)

    # ── sistema ─────────────────────────────────────────────────────
    def _log_txt(self, texto, tag=""):
        self._log(texto, tag)
        if self._window:
            try:
                self._window.evaluate_js(f"Hub.txtMsg({json.dumps(texto)})")
            except Exception:
                pass

    def init_info(self):
        layouts = [{"key": k, "nome": getattr(m, "NOME", k)}
                   for k, m in sorted(LAYOUTS.items())]
        return {
            "layouts": layouts,
            "errors": IMPORT_ERRORS,
            "base_dir": str(BASE_DIR),
            "ofx_writer": gerar_ofx is not None,
        }

    def get_tema(self):
        return _load_json(BASE_DIR / "converthub_ui.json", {}).get("tema", "claro")

    def set_tema(self, tema):
        _save_json(BASE_DIR / "converthub_ui.json",
                   {"tema": "black" if tema == "black" else "claro"})
        return True

    def get_stats(self):
        st = _load_json(STATS_FILE, {"days": {}}).get("days", {})
        hoje = date.today().isoformat()
        mes = hoje[:7]
        ok_hoje = st.get(hoje, {}).get("ok", 0)
        ok_mes = sum(v.get("ok", 0) for k, v in st.items() if k.startswith(mes))
        fail_mes = sum(v.get("fail", 0) for k, v in st.items() if k.startswith(mes))
        taxa = None
        if ok_mes + fail_mes:
            taxa = round(100 * ok_mes / (ok_mes + fail_mes))
        return {"hoje": ok_hoje, "mes": ok_mes, "taxa": taxa}

    def pick_pdf(self):
        r = self._dialog("OPEN", file_types=("PDF (*.pdf)", "Todos (*.*)"))
        return r[0] if r else None

    def pick_extrato(self):
        r = self._dialog("OPEN", file_types=(
            "Extratos (*.pdf;*.ofx)", "PDF (*.pdf)", "OFX (*.ofx)",
            "Todos (*.*)"))
        return r[0] if r else None

    def pick_image(self):
        r = self._dialog("OPEN", file_types=(
            "Imagens (*.png;*.jpg;*.jpeg;*.gif;*.bmp;*.webp)",
            "Todos (*.*)"))
        return r[0] if r else None

    def pick_save(self, initial="", ext="ofx"):
        filtro = {"ofx": "OFX (*.ofx)", "txt": "Texto (*.txt)"}.get(
            ext, "Todos (*.*)")
        r = self._dialog("SAVE", save_filename=initial,
                         file_types=(filtro, "Todos (*.*)"))
        if not r:
            return None
        p = r if isinstance(r, str) else r[0]
        if p and not Path(p).suffix:
            p += "." + ext
        return p

    def abrir_pasta(self, caminho):
        try:
            p = Path(caminho)
            pasta = p if p.is_dir() else p.parent
            if sys.platform.startswith("win"):
                if p.is_file():
                    subprocess.Popen(["explorer", "/select,", str(p)])
                else:
                    os.startfile(str(pasta))  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(pasta)])
            else:
                subprocess.Popen(["xdg-open", str(pasta)])
            return True
        except Exception:
            return False

    # ══════════════════════════════════════════════════════════════
    #  PDF -> OFX
    # ══════════════════════════════════════════════════════════════
    def layout_info(self, key):
        if not key:
            return {"nome": "", "descricao": "", "imagem": None}
        mod = LAYOUTS.get(key)
        return {
            "nome": getattr(mod, "NOME", key) if mod else key,
            "descricao": self._store.get(key).get("descricao", ""),
            "imagem": self._store.data_uri(key),
        }

    def salvar_descricao(self, key, texto):
        if not key:
            return False
        nome = getattr(LAYOUTS.get(key), "NOME", key)
        self._store.set_descricao(key, nome, texto or "")
        return True

    def cadastrar_imagem(self, key):
        if not key:
            return {"ok": False, "erro": "Selecione um layout."}
        p = self.pick_image()
        if not p:
            return {"ok": False, "cancelado": True}
        try:
            nome = getattr(LAYOUTS.get(key), "NOME", key)
            self._store.set_imagem(key, nome, Path(p))
        except Exception as exc:
            return {"ok": False, "erro": f"Falha ao cadastrar imagem: {exc}"}
        return {"ok": True}

    def remover_imagem(self, key):
        if key:
            self._store.remove_imagem(key)
        return True

    def converter(self, opts):
        """opts: pdf, layout, auto, saida, revisar"""
        try:
            return self._converter(opts)
        except Exception as exc:
            self._bump(False)
            return {"status": "erro", "erro": f"{type(exc).__name__}: {exc}",
                    "trace": traceback.format_exc()}

    def _converter(self, opts):
        if gerar_ofx is None:
            return {"status": "erro",
                    "erro": "ofx_writer não encontrado: "
                            + IMPORT_ERRORS.get("ofx_writer", "")}
        if not LAYOUTS:
            return {"status": "erro",
                    "erro": "Nenhum layout carregado de parsers/: "
                            + IMPORT_ERRORS.get("parsers", "")}
        pdf = Path(opts.get("pdf") or "")
        if not pdf.is_file():
            return {"status": "erro", "erro": "Selecione um arquivo PDF válido."}
        auto = bool(opts.get("auto"))
        layout = opts.get("layout") or ""
        if not auto and layout not in LAYOUTS:
            return {"status": "erro", "erro": "Selecione um layout de banco."}
        saida = Path(opts["saida"]) if opts.get("saida") else None
        revisar = bool(opts.get("revisar"))

        def saida_padrao():
            s = pdf.with_suffix(".ofx")
            if s.exists():
                s = pdf.with_name(pdf.stem + "_convertido.ofx")
            return s

        self._log("Iniciando conversão (Smart Layout)." if auto
                  else f"Iniciando conversão (layout: {layout}).")
        try:
            if auto:
                layout, transacoes = smart_layout(pdf, self._log)
            else:
                transacoes = LAYOUTS[layout].parse(pdf)
            if not transacoes:
                raise ValueError("Nenhuma transação foi encontrada no PDF.")
        except Exception as exc:
            return self._review_payload(
                [], info_fallback(layout, []) if layout in LAYOUTS else {},
                layout, pdf, saida or saida_padrao(),
                f"Falha na leitura automática: {exc}", True)

        parser = LAYOUTS[layout]
        try:
            info = parser.info(pdf)
        except Exception:
            info = info_fallback(layout, transacoes)
        saida = saida or saida_padrao()

        incompletos = [
            t for t in transacoes
            if t.get("incompleto") or t.get("data") is None
            or t.get("valor") is None
            or not str(t.get("descricao") or t.get("memo") or "").strip()]

        if incompletos or revisar:
            motivo = (f"Foram encontrados {len(incompletos)} lançamento(s) com "
                      "informações faltantes (data, descrição, valor ou "
                      "documento não identificados no PDF)."
                      if incompletos else
                      "Conferência manual de lançamentos ativada.")
            return self._review_payload(transacoes, info, layout, pdf, saida,
                                        motivo, bool(incompletos))

        gerar_ofx(transacoes, info, saida)
        self._bump(True)
        return self._ok_payload(saida, len(transacoes), info, layout)

    def _ok_payload(self, saida, n, info, layout):
        msg = f"{n} transações exportadas"
        saldo = info.get("saldo_final") if info else None
        if saldo is not None:
            msg += f"  |  Saldo final: R$ {_brl(saldo)}"
        self._log(f"Arquivo gerado: {saida}", "ok")
        self._log(msg, "ok")
        return {"status": "ok", "saida": str(saida), "msg": msg,
                "layout": layout}

    def _review_payload(self, transacoes, info, layout, pdf, saida, motivo,
                        tem_incompletos):
        with self._lock:
            self._sessao = {"info": dict(info or {}), "layout": layout,
                            "pdf": str(pdf), "saida": str(saida)}
        self._log("Abrindo revisão dos lançamentos.")
        return {"status": "review", "motivo": motivo,
                "incompletos": tem_incompletos,
                "saida": str(saida), "layout": layout,
                "itens": [_item_view(t) for t in transacoes]}

    def confirmar_revisao(self, itens):
        if gerar_ofx is None:
            return {"ok": False, "erro": "ofx_writer não encontrado."}
        with self._lock:
            sess = dict(self._sessao)
        if not sess:
            return {"ok": False, "erro": "Sessão de revisão expirada."}
        info = sess["info"]
        pi = info.get("periodo_inicio")
        ano = pi.year if hasattr(pi, "year") else datetime.now().year

        if not itens:
            return {"ok": False, "erro": "Não há lançamentos para exportar."}
        finais = []
        for i, it in enumerate(itens):
            d = _parse_data_usuario(it.get("data", ""), ano)
            desc = (it.get("descricao") or "").strip()
            doc = (it.get("doc") or "").strip()
            v = _parse_valor_usuario(it.get("valor", ""),
                                     it.get("tipo") == "D")
            if not d:
                return {"ok": False, "idx": i,
                        "erro": f"Lançamento {i + 1}: data inválida (ex: 15/08/2026)."}
            if not desc:
                return {"ok": False, "idx": i,
                        "erro": f"Lançamento {i + 1}: informe a descrição."}
            if v is None:
                return {"ok": False, "idx": i,
                        "erro": f"Lançamento {i + 1}: valor inválido (ex: 150,00)."}
            nome = it.get("nome") or None
            finais.append({"data": d, "valor": v, "descricao": desc,
                           "memo": desc, "doc": doc, "nome": nome,
                           "name": nome or ""})

        datas = [t["data"] for t in finais]
        if not info.get("periodo_inicio"):
            info["periodo_inicio"] = min(datas)
        if not info.get("periodo_fim"):
            info["periodo_fim"] = max(datas)
        try:
            gerar_ofx(finais, info, Path(sess["saida"]))
        except Exception as exc:
            self._bump(False)
            return {"ok": False, "erro": f"Erro ao gerar OFX: {exc}"}
        self._bump(True)
        res = self._ok_payload(sess["saida"], len(finais), info,
                               sess.get("layout", ""))
        res["ok"] = True
        return res

    # ══════════════════════════════════════════════════════════════
    #  Extrato -> TXT
    # ══════════════════════════════════════════════════════════════
    def _txt_migrate(self):
        if not OLD_MEMORY.exists():
            return
        if "Padrao" in self._cfg.get("profiles", []):
            return
        self._cfg.setdefault("profiles", []).append("Padrao")
        self._cfg["active"] = "Padrao"
        _save_json(CONFIG_FILE, self._cfg)
        create_profile("Padrao")
        try:
            _save_json(_pdir("Padrao") / "memoria.json",
                       _load_json(OLD_MEMORY))
        except Exception:
            pass
        self._perfil = "Padrao"

    def _txt_load_perfil(self):
        if not self._perfil:
            self._memoria, self._regras = {}, {}
            return
        self._memoria = _load_json(_pdir(self._perfil) / "memoria.json")
        self._regras = _load_json(_pdir(self._perfil) / "regras.json")

    def _match_keyword(self, descricao):
        dn = normalize_desc(descricao).upper()
        for kw, code in self._regras.items():
            if kw.upper() in dn:
                return code
        return None

    def _rebuild_codigos(self):
        self._codigos = {}
        for lan in self._lancs:
            gk = group_key_desc(lan["descricao"], self._num)
            dn = normalize_desc(lan["descricao"])
            codigo = None
            if str(self._memoria.get(gk, "")).strip():
                codigo = self._memoria[gk]
            elif str(self._memoria.get(dn, "")).strip():
                codigo = self._memoria[dn]
            if codigo:
                self._codigos[lan["uid"]] = codigo
        for lan in self._lancs:
            if lan["uid"] in self._codigos:
                continue
            code = self._match_keyword(lan["descricao"])
            if code:
                self._codigos[lan["uid"]] = code

    def _txt_snapshot(self):
        return {
            "perfil": self._perfil,
            "considerar_numeros": self._num,
            "perfis": self._cfg.get("profiles", []),
            "regras": len(self._regras),
            "memoria": len(self._memoria),
            "lancamentos": [
                {"uid": l["uid"], "tipo": l["tipo"], "data": l["data"],
                 "valor": l["valor"], "descricao": l["descricao"],
                 "codigo": self._codigos.get(l["uid"], "")}
                for l in self._lancs],
        }

    def txt_state(self):
        with self._lock:
            return self._txt_snapshot()

    def txt_considerar_numeros(self, flag):
        with self._lock:
            self._num = bool(flag)
            antigos = dict(self._codigos)
            self._rebuild_codigos()
            for uid, c in antigos.items():
                self._codigos.setdefault(uid, c)
            return {"ok": True, **self._txt_snapshot()}

    def txt_novo_perfil(self, name):
        name = (name or "").strip()
        if not name:
            return {"ok": False, "erro": "Digite um nome."}
        if re.search(r'[\\/:*?"<>|]', name):
            return {"ok": False, "erro": "Nome contém caracteres inválidos."}
        with self._lock:
            if name in self._cfg.get("profiles", []):
                return {"ok": False, "erro": "Esse perfil já existe."}
            self._cfg.setdefault("profiles", []).append(name)
            self._cfg["active"] = name
            _save_json(CONFIG_FILE, self._cfg)
            create_profile(name)
            self._perfil = name
            self._txt_load_perfil()
            self._rebuild_codigos()
            return {"ok": True, **self._txt_snapshot()}

    def txt_trocar_perfil(self, name):
        with self._lock:
            if name not in self._cfg.get("profiles", []):
                return {"ok": False, "erro": "Perfil inexistente."}
            self._perfil = name
            self._cfg["active"] = name
            _save_json(CONFIG_FILE, self._cfg)
            self._txt_load_perfil()
            self._rebuild_codigos()
            return {"ok": True, **self._txt_snapshot()}

    def txt_excluir_perfil(self, name):
        with self._lock:
            if name not in self._cfg.get("profiles", []):
                return {"ok": False, "erro": "Perfil inexistente."}
            delete_profile(name)
            self._cfg["profiles"].remove(name)
            if self._cfg.get("active") == name:
                self._cfg["active"] = ""
            if self._cfg["profiles"]:
                self._perfil = self._cfg["profiles"][0]
                self._cfg["active"] = self._perfil
            else:
                self._perfil = ""
            _save_json(CONFIG_FILE, self._cfg)
            self._txt_load_perfil()
            self._rebuild_codigos()
            return {"ok": True, **self._txt_snapshot()}

    # regras
    def txt_regras(self):
        with self._lock:
            return sorted(({"kw": k, "code": v}
                           for k, v in self._regras.items()),
                          key=lambda r: r["kw"].lower())

    def txt_regra_add(self, kw, code):
        kw, code = (kw or "").strip(), (code or "").strip()
        if not self._perfil:
            return {"ok": False, "erro": "Selecione ou crie um perfil."}
        if not kw or not code:
            return {"ok": False, "erro": "Preencha trecho e código."}
        with self._lock:
            self._regras[kw] = code
            _save_json(_pdir(self._perfil) / "regras.json", self._regras)
        return {"ok": True, "regras": self.txt_regras()}

    def txt_regra_del(self, kws):
        with self._lock:
            for kw in kws:
                self._regras.pop(kw, None)
            if self._perfil:
                _save_json(_pdir(self._perfil) / "regras.json", self._regras)
        return {"ok": True, "regras": self.txt_regras()}

    def txt_regras_aplicar(self):
        with self._lock:
            if not self._lancs:
                return {"ok": False, "erro": "Nenhum extrato carregado."}
            n = 0
            updates = {}
            for lan in self._lancs:
                if self._codigos.get(lan["uid"], "").strip():
                    continue
                code = self._match_keyword(lan["descricao"])
                if code:
                    self._codigos[lan["uid"]] = code
                    updates[lan["uid"]] = code
                    n += 1
            return {"ok": True, "n": n, "updates": updates}

    # extrato
    def txt_abrir(self, path):
        if not self._perfil:
            return {"ok": False, "erro": "Selecione ou crie um perfil primeiro."}
        p = Path(path or "")
        if not p.is_file():
            return {"ok": False, "erro": "Arquivo não encontrado."}
        layout = None
        try:
            if p.suffix.lower() == ".pdf":
                if not LAYOUTS:
                    return {"ok": False,
                            "erro": "Nenhum layout carregado de parsers/: "
                                    + IMPORT_ERRORS.get("parsers", "")}
                self._log_txt(f"Analisando {p.name} e identificando o banco…")
                layout, trans = smart_layout(p, self._log_txt)
                lancs = transacoes_para_lancamentos(trans)
            else:
                lancs = parse_ofx(str(p))
        except Exception as exc:
            return {"ok": False, "erro": str(exc)}
        if not lancs:
            return {"ok": False, "erro": "Nenhum lançamento encontrado."}
        with self._lock:
            self._lancs = lancs
            self._rebuild_codigos()
            snap = self._txt_snapshot()
        return {"ok": True, "arquivo": p.name, "layout": layout, **snap}

    def txt_set_codigo(self, uid, codigo):
        codigo = (codigo or "").strip()
        with self._lock:
            lan = next((l for l in self._lancs if l["uid"] == uid), None)
            if lan is None:
                return {"ok": False, "updates": {}}
            updates = {}
            if codigo:
                gk = group_key_desc(lan["descricao"], self._num)
                self._memoria[gk] = codigo
                if self._perfil:
                    _save_json(_pdir(self._perfil) / "memoria.json",
                               self._memoria)
                for outro in self._lancs:
                    if group_key_desc(outro["descricao"], self._num) == gk:
                        self._codigos[outro["uid"]] = codigo
                        updates[outro["uid"]] = codigo
            else:
                self._codigos.pop(uid, None)
                updates[uid] = ""
            return {"ok": True, "updates": updates,
                    "memoria": len(self._memoria)}

    def txt_desvincular(self, uid):
        with self._lock:
            self._codigos.pop(uid, None)
        return {"ok": True, "updates": {uid: ""}}

    def txt_gerar(self, out):
        with self._lock:
            if not self._lancs:
                return {"ok": False, "erro": "Nenhum extrato carregado."}
            if not out:
                return {"ok": False, "erro": "Caminho de saída inválido."}
            try:
                n = gerar_txt(self._lancs, self._codigos, out)
            except Exception as exc:
                return {"ok": False, "erro": str(exc)}
        return {"ok": True, "n": n, "saida": out}


# ══════════════════════════════════════════════════════════════════════
#  Interface (HTML)
# ══════════════════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ConvertHub</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
  tailwind.config = { theme: { extend: { colors: { primary: {
    50:'#f0f9ff',100:'#e0f2fe',200:'#bae6fd',300:'#7dd3fc',400:'#38bdf8',
    500:'#0ea5e9',600:'#0284c7',700:'#0369a1',800:'#075985',900:'#0c4a6e' } } } } }
</script>
<style>
  .sidebar-item.active { background-color:#e0f2fe; color:#0369a1; border-right:3px solid #0ea5e9; }
  .dz.drag { border-color:#0ea5e9; background:#f0f9ff; }
  .prog { height:4px; background:#e0f2fe; overflow:hidden; border-radius:9999px; }
  .prog > i { display:block; height:100%; width:35%; background:#0284c7; border-radius:9999px; animation:slide 1s infinite ease-in-out; }
  @keyframes slide { 0%{margin-left:-35%} 100%{margin-left:100%} }
  .code-in { font-family:Consolas,monospace; font-weight:600; }
  .flash { background:#dcfce7 !important; transition:background .5s; }
  th.sortable { cursor:pointer; user-select:none; }
  #log { font-family:Consolas,monospace; font-size:12px; }
  #log .erro { color:#b3261e; } #log .ok { color:#2e7d32; }
  .tbl-wrap { max-height:calc(100vh - 290px); overflow:auto; }
  body, main { background:#d5dce8; }
  main .bg-white.rounded-xl { border-color:#b8c2d3; box-shadow:0 1px 4px rgba(15,23,42,.14); }
  header.bg-white { border-color:#b8c2d3; }
  #layout-img-box { background:#f4f8fe; }
  #layout-img-box img { cursor:zoom-in; }
  .tbl-wrap thead th { position:sticky; top:0; z-index:1; }

/* ===== v2: correções e tema ===== */
::selection { background:#0284c7; color:#ffffff; }
#txt-body td.desc-cell, #txt-body td.desc-cell * {
  -webkit-user-select:text !important; user-select:text !important; cursor:text;
}
#txt-body td.desc-cell {
  white-space:normal; word-break:break-word; overflow-wrap:anywhere;
  line-height:1.3; min-width:0; overflow:hidden;
}
#txt-body td { vertical-align:middle; }
#txt-body tr:hover td { background:#e6f2fd; }
.tbl-wrap { max-height:calc(100vh - 300px); border-radius:12px; }
.tbl-wrap table { table-layout:fixed; border-collapse:separate; border-spacing:0; }
.tbl-wrap thead th {
  position:sticky; top:0; z-index:5;
  background:#ffffff !important; color:#334155 !important; opacity:1 !important;
  box-shadow:inset 0 -2px 0 #0284c7; padding-top:12px; padding-bottom:12px;
}
.btn-blue {
  background:#0284c7 !important; color:#ffffff !important;
  border:1px solid #0284c7 !important; border-radius:8px;
  font-weight:500; transition:background .15s, transform .05s;
}
.btn-blue:hover { background:#0369a1 !important; border-color:#0369a1 !important; }
.btn-blue:active { transform:translateY(1px); }
.btn-blue:disabled { background:#7dd3fc !important; border-color:#7dd3fc !important; opacity:.7; cursor:not-allowed; }
aside { box-shadow:2px 0 8px rgba(15,23,42,.06); }
.sidebar-item { border-radius:10px; }
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-thumb { background:#0284c7; border-radius:8px; }
::-webkit-scrollbar-track { background:transparent; }

/* ===== barra de loading (TXT) ===== */
#txt-prog { height:6px; }
button:disabled, select:disabled { opacity:.6; cursor:not-allowed; }

/* ===== tema Black ===== */
html.dark body, html.dark main { background:#000000 !important; color:#e5e5e5; }
html.dark .bg-white, html.dark aside.bg-white { background:#0d0d0d !important; }
html.dark .bg-slate-50, html.dark .bg-slate-100 { background:#151515 !important; }
html.dark .border-slate-100, html.dark .border-slate-200, html.dark .border-slate-300,
html.dark header.bg-white, html.dark main .bg-white.rounded-xl { border-color:#262626 !important; }
html.dark main .bg-white.rounded-xl { box-shadow:none; }
html.dark aside { box-shadow:none; border-color:#262626 !important; }
html.dark .text-slate-900, html.dark .text-slate-800, html.dark .text-slate-700 { color:#f2f2f2 !important; }
html.dark .text-slate-600, html.dark .text-slate-500 { color:#b8b8b8 !important; }
html.dark .text-slate-400 { color:#8c8c8c !important; }
html.dark input, html.dark select, html.dark textarea {
  background:#000000 !important; color:#7dd3fc !important; border-color:#2a2a2a !important;
}
html.dark input::placeholder { color:#666; }
html.dark #layout-img-box { background:#0a0a0a; border-color:#262626; }
html.dark #log { background:#0a0a0a !important; }
html.dark .sidebar-item.active { background:#0c2a40; color:#7dd3fc; }
html.dark .hover\:bg-slate-50:hover { background:#1a1a1a !important; }
html.dark .tbl-wrap thead th { background:#111111 !important; color:#cfe8fa !important; }
html.dark #txt-body tr:hover td { background:#10202c; }
html.dark .bg-primary-50, html.dark .bg-blue-50 { background:#0c2233 !important; }
html.dark .bg-primary-100, html.dark .bg-blue-100 { background:#0e3350 !important; }
html.dark .bg-emerald-50 { background:#052e1a !important; }
html.dark .border-emerald-200 { border-color:#14532d !important; }
html.dark .text-emerald-800, html.dark .text-emerald-700 { color:#86efac !important; }
html.dark .bg-red-50, html.dark .bg-red-100 { background:#2a0f0f !important; }
html.dark .text-red-700, html.dark .text-red-500 { color:#fca5a5 !important; }
html.dark .text-emerald-600 { color:#4ade80 !important; }
html.dark .dz.drag { background:#0c2233; }
html.dark ::-webkit-scrollbar-track { background:#000; }
</style>
</head>
<body class="text-slate-800 antialiased">
<div class="flex h-screen overflow-hidden">

  <!-- Sidebar -->
  <aside class="w-64 bg-white border-r border-slate-200 flex flex-col shrink-0">
    <div class="px-5 py-5 border-b border-slate-100">
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-lg bg-primary-600 flex items-center justify-center">
          <svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"/></svg>
        </div>
        <span class="font-semibold text-lg tracking-tight">Convert<span class="text-primary-600">Hub</span></span>
      </div>
    </div>
    <nav class="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
      <button data-module="home" class="sidebar-item active w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors text-left">
        <svg class="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-4 0a1 1 0 01-1-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 01-1 1h-2z"/></svg>
        Home
      </button>
      <p class="px-3 text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2 mt-4">Módulos</p>
      <button data-module="pdf-ofx" class="sidebar-item w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-slate-500 hover:bg-slate-50 hover:text-slate-700 transition-colors text-left">
        <svg class="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
        PDF → OFX
      </button>
      <button data-module="txt" class="sidebar-item w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-slate-500 hover:bg-slate-50 hover:text-slate-700 transition-colors text-left">
        <svg class="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M3 10h18M3 14h18m-9-4v8m-7 0h14a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
        Extrato → TXT
      </button>
      <div class="pt-4 mt-2 border-t border-slate-100">
        <p class="px-3 text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">Ferramentas</p>
        <button data-module="settings" class="sidebar-item w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-slate-500 hover:bg-slate-50 hover:text-slate-700 transition-colors text-left">
          <svg class="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
          Sistema
        </button>
      </div>
    </nav>
    <div class="px-4 py-3 border-t border-slate-100">
      <button id="theme-btn" class="w-full px-3 py-2 text-sm">Tema: Claro</button>
    </div>
    <div class="px-4 py-4 border-t border-slate-100 text-xs text-slate-400">
      Perfil TXT: <span id="side-perfil" class="font-medium text-slate-600">—</span>
    </div>
  </aside>

  <main class="flex-1 overflow-y-auto">

    <!-- ========== HOME ========== -->
    <div id="module-home" class="module-content">
      <header class="bg-white border-b border-slate-200 px-8 py-5 sticky top-0 z-10">
        <h1 class="text-xl font-semibold text-slate-900">Home</h1>
        <p class="text-sm text-slate-500 mt-0.5">Bem-vindo ao ConvertHub</p>
      </header>
      <div class="p-8 max-w-5xl mx-auto">
        <div class="bg-gradient-to-br from-primary-600 to-primary-800 rounded-2xl p-8 text-white mb-8 shadow-lg">
          <h2 class="text-2xl font-semibold mb-2">Converta extratos com facilidade</h2>
          <p class="text-primary-100 text-sm max-w-lg leading-relaxed">Transforme PDFs de extratos bancários em OFX e gere o arquivo TXT de lançamentos para o Alterdata a partir de PDF ou OFX.</p>
        </div>
        <h3 class="text-sm font-semibold text-slate-500 uppercase tracking-wider mb-4">Módulos disponíveis</h3>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-10">
          <button data-goto="pdf-ofx" class="goto-btn group bg-white rounded-xl border border-slate-200 p-5 shadow-sm hover:border-primary-300 hover:shadow-md transition-all text-left">
            <div class="w-10 h-10 rounded-lg bg-primary-50 text-primary-600 flex items-center justify-center mb-3 group-hover:bg-primary-100 transition-colors">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
            </div>
            <p class="font-medium text-slate-800">PDF → OFX</p>
            <p class="text-xs text-slate-400 mt-1">Extratos em PDF com layout por banco, Smart Layout e revisão manual</p>
          </button>
          <button data-goto="txt" class="goto-btn group bg-white rounded-xl border border-slate-200 p-5 shadow-sm hover:border-primary-300 hover:shadow-md transition-all text-left">
            <div class="w-10 h-10 rounded-lg bg-blue-50 text-blue-500 flex items-center justify-center mb-3 group-hover:bg-blue-100 transition-colors">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M3 10h18M3 14h18m-9-4v8m-7 0h14a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
            </div>
            <p class="font-medium text-slate-800">Extrato → TXT (Alterdata)</p>
            <p class="text-xs text-slate-400 mt-1">PDF ou OFX, perfis, memória de códigos e regras de palavras-chave</p>
          </button>
        </div>
        <h3 class="text-sm font-semibold text-slate-500 uppercase tracking-wider mb-4">Resumo (PDF → OFX)</h3>
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div class="bg-white rounded-xl border border-slate-200 p-5 shadow-sm"><p class="text-xs text-slate-400 mb-1">Conversões hoje</p><p id="st-hoje" class="text-2xl font-semibold text-slate-800">0</p></div>
          <div class="bg-white rounded-xl border border-slate-200 p-5 shadow-sm"><p class="text-xs text-slate-400 mb-1">Total este mês</p><p id="st-mes" class="text-2xl font-semibold text-slate-800">0</p></div>
          <div class="bg-white rounded-xl border border-slate-200 p-5 shadow-sm"><p class="text-xs text-slate-400 mb-1">Taxa de sucesso</p><p id="st-taxa" class="text-2xl font-semibold text-slate-800">—</p></div>
        </div>
      </div>
    </div>

    <!-- ========== PDF → OFX ========== -->
    <div id="module-pdf-ofx" class="module-content hidden">
      <header class="bg-white border-b border-slate-200 px-8 py-5 sticky top-0 z-10">
        <div class="flex items-center justify-between">
          <div>
            <h1 class="text-xl font-semibold text-slate-900">PDF → OFX</h1>
            <p class="text-sm text-slate-500 mt-0.5">Converta extratos bancários em PDF para o formato OFX</p>
          </div>
          <button id="btn-convert" class="inline-flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-700 disabled:bg-primary-300 text-white text-sm font-medium rounded-lg transition-colors shadow-sm">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
            Converter para OFX
          </button>
        </div>
        <div id="prog" class="prog mt-4 hidden"><i></i></div>
      </header>
      <div class="p-8 max-w-6xl mx-auto">
        <div class="grid grid-cols-1 lg:grid-cols-5 gap-6">
          <div class="lg:col-span-2 space-y-5">
            <div class="bg-white rounded-xl border border-slate-200 p-5 shadow-sm">
              <label class="block text-sm font-medium text-slate-700 mb-3">1. Extrato em PDF</label>
              <div id="pdf-dz" class="dz flex flex-col items-center justify-center w-full h-32 border-2 border-dashed border-slate-200 rounded-lg cursor-pointer hover:border-primary-400 hover:bg-primary-50/40 transition-colors">
                <svg class="w-9 h-9 text-slate-300 mb-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/></svg>
                <span class="text-sm text-slate-500">Clique ou arraste o PDF</span>
              </div>
              <p id="pdf-name" class="mt-2 text-xs text-slate-500 truncate hidden"></p>
            </div>
            <div class="bg-white rounded-xl border border-slate-200 p-5 shadow-sm">
              <label class="block text-sm font-medium text-slate-700 mb-2">2. Layout do banco</label>
              <select id="layout-sel" class="w-full rounded-lg border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-700 focus:outline-none focus:ring-2 focus:ring-primary-500 disabled:bg-slate-100 disabled:text-slate-400"></select>
              <label class="flex items-start gap-2 mt-3 text-sm text-slate-600 cursor-pointer">
                <input id="auto-chk" type="checkbox" class="mt-1 accent-sky-600">
                <span>Automático (Smart Layout): testar todos os layouts e usar o que funcionar</span>
              </label>
            </div>
            <div class="bg-white rounded-xl border border-slate-200 p-5 shadow-sm">
              <label class="block text-sm font-medium text-slate-700 mb-1">3. Arquivo de saída <span class="text-slate-400 font-normal">(opcional)</span></label>
              <p class="text-xs text-slate-400 mb-2">Se não informado, o OFX será salvo ao lado do PDF.</p>
              <div class="flex gap-2">
                <input id="out-path" readonly class="flex-1 min-w-0 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600" placeholder="—">
                <button id="out-pick" class="px-3 py-2 text-sm rounded-lg border border-primary-600 text-primary-700 hover:bg-primary-50">Escolher…</button>
              </div>
            </div>
            <label class="flex items-center gap-2 px-1 text-sm text-slate-600 cursor-pointer">
              <input id="revisar-chk" type="checkbox" class="accent-sky-600">
              Sempre revisar lançamentos antes de salvar
            </label>
            <div id="result-card" class="hidden bg-emerald-50 border border-emerald-200 rounded-xl p-4">
              <p class="text-sm font-semibold text-emerald-800">Conversão concluída</p>
              <p id="result-msg" class="text-xs text-emerald-700 mt-1"></p>
              <p id="result-path" class="text-xs text-emerald-700 mt-1 break-all"></p>
              <div class="flex flex-wrap gap-2 mt-3">
                <button id="res-folder" class="px-3 py-1.5 text-xs rounded-lg border border-emerald-600 text-emerald-800 hover:bg-emerald-100">Abrir pasta</button>
                <button id="res-txt" class="px-3 py-1.5 text-xs rounded-lg bg-emerald-600 text-white hover:bg-emerald-700">Usar no módulo TXT</button>
              </div>
            </div>
          </div>
          <div class="lg:col-span-3 space-y-5">
            <div class="bg-white rounded-xl border border-slate-200 shadow-sm">
              <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
                <h2 id="layout-title" class="text-sm font-medium text-slate-700">Modelo do extrato</h2>
                <span class="text-xs text-slate-400 bg-slate-50 px-2 py-1 rounded">Pré-visualização do layout</span>
              </div>
              <div class="p-5">
                <div id="layout-img-box" class="w-full h-44 rounded-lg border border-sky-100 flex items-center justify-center overflow-hidden">
                  <p class="text-sm text-slate-400 text-center px-6">Nenhuma imagem cadastrada<br>para este layout.</p>
                </div>
                <label class="block text-xs text-slate-500 mt-4 mb-1">Observação</label>
                <div class="flex gap-2">
                  <input id="layout-desc" class="flex-1 min-w-0 rounded-lg border border-slate-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500">
                  <button id="desc-save" class="px-3 py-2 text-sm rounded-lg border border-primary-600 text-primary-700 hover:bg-primary-50">Salvar</button>
                </div>
                <div class="flex gap-2 mt-4">
                  <button id="img-add" class="px-3 py-2 text-sm rounded-lg border border-primary-600 text-primary-700 hover:bg-primary-50">Cadastrar imagem…</button>
                  <button id="img-del" class="px-3 py-2 text-sm rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50 disabled:opacity-40">Remover imagem</button>
                </div>
              </div>
            </div>
            <div class="bg-white rounded-xl border border-slate-200 shadow-sm">
              <div class="px-5 py-3 border-b border-slate-100 flex items-center justify-between">
                <h2 class="text-sm font-medium text-slate-700">Registro de atividades</h2>
                <button id="log-clear" class="text-xs text-slate-400 hover:text-slate-600">Limpar</button>
              </div>
              <div id="log" class="p-4 h-32 overflow-y-auto bg-slate-50 rounded-b-xl whitespace-pre-wrap"></div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ========== EXTRATO → TXT ========== -->
    <div id="module-txt" class="module-content hidden">
      <header class="bg-white border-b border-slate-200 px-8 py-4 sticky top-0 z-10">
        <div class="flex items-center justify-between gap-4 flex-wrap">
          <div>
            <h1 class="text-xl font-semibold text-slate-900">Extrato → TXT <span class="text-slate-400 font-normal text-base">Alterdata</span></h1>
            <p id="txt-file" class="text-sm text-slate-500 mt-0.5">Nenhum arquivo selecionado</p>
          </div>
          <div class="flex gap-2">
            <button id="txt-open" class="px-4 py-2 bg-amber-500 hover:bg-amber-600 text-white text-sm font-medium rounded-lg shadow-sm">Abrir PDF / OFX</button>
            <button id="txt-gen" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-medium rounded-lg shadow-sm">Gerar TXT</button>
          </div>
        </div>
        <div class="flex items-center gap-2 mt-4 flex-wrap">
          <span class="text-sm font-medium text-slate-600">Perfil:</span>
          <select id="perfil-sel" class="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm min-w-[180px] focus:outline-none focus:ring-2 focus:ring-primary-500"></select>
          <button id="perfil-new" class="px-3 py-1.5 text-sm rounded-lg bg-primary-600 hover:bg-primary-700 text-white">+ Novo perfil</button>
          <button id="perfil-del" class="px-3 py-1.5 text-sm rounded-lg border border-red-300 text-red-700 hover:bg-red-50">Excluir perfil</button>
          <input id="txt-filter" placeholder="Filtrar descrição…" class="ml-4 rounded-lg border border-slate-200 px-3 py-1.5 text-sm w-56 focus:outline-none focus:ring-2 focus:ring-primary-500">
          <label class="flex items-center gap-1.5 text-sm text-slate-600 cursor-pointer"><input id="txt-missing" type="checkbox" class="accent-sky-600"> só sem código</label>
          <label class="flex items-center gap-1.5 text-sm text-slate-600 cursor-pointer" title="Marcado: só agrupa descrições com letras E números iguais"><input id="txt-num" type="checkbox" class="accent-sky-600"> considerar números</label>
          <button id="rules-open" class="ml-auto px-3 py-1.5 text-sm rounded-lg bg-violet-700 hover:bg-violet-800 text-white">Regras de palavras-chave</button>
        </div>
        <p id="txt-status" class="text-xs text-emerald-700 mt-3">Selecione ou crie um perfil para começar.</p>
        <div id="txt-prog" class="prog mt-3 hidden"><i></i></div>
      </header>
      <div class="p-6">
        <div class="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
          <div class="tbl-wrap">
            <table class="w-full text-sm table-fixed"> <thead class="bg-white text-slate-500 text-xs uppercase">
                <tr>
                  <th class="sortable px-3 py-2 text-left w-14" data-sort="tipo">Tp</th>
                  <th class="sortable px-3 py-2 text-left w-28" data-sort="data">Data</th>
                  <th class="sortable px-3 py-2 text-right w-36" data-sort="valor">Valor (R$)</th>
                  <th class="sortable px-3 py-2 text-left" data-sort="descricao">Descrição / Nome</th>
                  <th class="sortable px-3 py-2 text-left w-48" data-sort="codigo">Lanc. Automático</th>
                  <th class="px-3 py-2 text-left w-40">Ação</th>
                </tr>
              </thead>
              <tbody id="txt-body"></tbody>
            </table>
            <div id="txt-empty" class="p-10 text-center text-sm text-slate-400">Abra um extrato em PDF ou OFX para listar os lançamentos.</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ========== SISTEMA ========== -->
    <div id="module-settings" class="module-content hidden">
      <header class="bg-white border-b border-slate-200 px-8 py-5 sticky top-0 z-10">
        <h1 class="text-xl font-semibold text-slate-900">Sistema</h1>
        <p class="text-sm text-slate-500 mt-0.5">Estado dos componentes usados pelos conversores</p>
      </header>
      <div class="p-8 max-w-2xl mx-auto space-y-4" id="sys-box"></div>
    </div>

  </main>
</div>

<!-- Toast -->
<div id="toast" class="fixed bottom-6 right-6 z-50 hidden max-w-sm px-4 py-3 rounded-lg shadow-lg text-sm text-white"></div>

<!-- Modal genérico -->
<div id="modal" class="fixed inset-0 z-40 hidden bg-slate-900/40 flex items-center justify-center p-6">
  <div id="modal-card" class="bg-white rounded-xl shadow-xl w-full max-w-md"></div>
</div>

<script>
const $  = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const brl = v => Number(v).toLocaleString('pt-BR', {minimumFractionDigits:2, maximumFractionDigits:2});
const py = async (fn, ...a) => {
  try { return await window.pywebview.api[fn](...a); }
  catch (e) { toast('Erro interno: ' + (e && e.message || e), 'err'); throw e; }
};

/* ---------- toast / modais ---------- */
let toastT = null;
function toast(msg, kind='ok') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'fixed bottom-6 right-6 z-50 max-w-sm px-4 py-3 rounded-lg shadow-lg text-sm text-white ' +
    (kind === 'err' ? 'bg-red-600' : kind === 'warn' ? 'bg-amber-600' : 'bg-emerald-600');
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.add('hidden'), 4500);
}
function openModal(html, wide=false) {
  $('#modal-card').className = 'bg-white rounded-xl shadow-xl w-full ' + (wide ? 'max-w-5xl' : 'max-w-md') + ' max-h-[90vh] flex flex-col';
  $('#modal-card').innerHTML = html; $('#modal').classList.remove('hidden');
}
function closeModal() { $('#modal').classList.add('hidden'); $('#modal-card').innerHTML = ''; }
function confirmDialog(title, text, okLabel='Confirmar') {
  return new Promise(res => {
    openModal(`<div class="p-6"><h3 class="font-semibold text-slate-900 mb-2">${esc(title)}</h3>
      <p class="text-sm text-slate-600 whitespace-pre-line">${esc(text)}</p>
      <div class="flex justify-end gap-2 mt-6">
        <button id="m-no" class="px-4 py-2 text-sm rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50">Cancelar</button>
        <button id="m-yes" class="px-4 py-2 text-sm rounded-lg bg-primary-600 text-white hover:bg-primary-700">${esc(okLabel)}</button>
      </div></div>`);
    $('#m-no').onclick = () => { closeModal(); res(false); };
    $('#m-yes').onclick = () => { closeModal(); res(true); };
  });
}
function promptDialog(title, label) {
  return new Promise(res => {
    openModal(`<div class="p-6"><h3 class="font-semibold text-slate-900 mb-3">${esc(title)}</h3>
      <label class="text-xs text-slate-500">${esc(label)}</label>
      <input id="m-in" class="w-full mt-1 rounded-lg border border-slate-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500">
      <div class="flex justify-end gap-2 mt-6">
        <button id="m-no" class="px-4 py-2 text-sm rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50">Cancelar</button>
        <button id="m-yes" class="px-4 py-2 text-sm rounded-lg bg-emerald-600 text-white hover:bg-emerald-700">Criar</button>
      </div></div>`);
    const inp = $('#m-in'); inp.focus();
    inp.onkeydown = e => { if (e.key === 'Enter') $('#m-yes').click(); };
    $('#m-no').onclick = () => { closeModal(); res(null); };
    $('#m-yes').onclick = () => { const v = inp.value; closeModal(); res(v); };
  });
}

/* ---------- navegação ---------- */
function switchModule(m) {
  $$('.sidebar-item').forEach(b => { b.classList.remove('active'); b.classList.add('text-slate-500'); });
  const btn = $(`.sidebar-item[data-module="${m}"]`);
  if (btn) { btn.classList.add('active'); btn.classList.remove('text-slate-500'); }
  $$('.module-content').forEach(el => el.classList.add('hidden'));
  $('#module-' + m).classList.remove('hidden');
  if (m === 'home') loadStats();
  if (m === 'settings') renderSys();
}
$$('.sidebar-item').forEach(b => b.addEventListener('click', () => switchModule(b.dataset.module)));
$$('.goto-btn').forEach(b => b.addEventListener('click', () => switchModule(b.dataset.goto)));

/* ---------- log (chamado pelo Python) ---------- */
const Hub = {
  log(texto, tag) {
    const d = new Date(), p = n => String(n).padStart(2, '0');
    const el = $('#log'), line = document.createElement('div');
    if (tag) line.className = tag;
    line.textContent = `[${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}]  ${texto}`;
    el.appendChild(line); el.scrollTop = el.scrollHeight;
  }
};
Hub.txtMsg = t => {
  const p = $('#txt-prog'), el = $('#txt-file');
  if (p && el && !p.classList.contains('hidden')) el.textContent = t;
};
$('#log-clear').onclick = () => { $('#log').innerHTML = ''; };

/* =================================================================
   PDF → OFX
================================================================= */
const P = { pdf: null, saida: null, busy: false, lastOfx: null, sess: null };

function setPdf(path) {
  if (!path) return;
  P.pdf = path; P.saida = null; $('#out-path').value = '';
  const n = $('#pdf-name'); n.textContent = path; n.classList.remove('hidden');
  $('#result-card').classList.add('hidden');
  Hub.log('PDF selecionado: ' + path.split(/[\\/]/).pop());
}
$('#pdf-dz').onclick = async () => setPdf(await py('pick_pdf'));
['dragenter','dragover'].forEach(ev => $('#pdf-dz').addEventListener(ev, e => { e.preventDefault(); $('#pdf-dz').classList.add('drag'); }));
['dragleave','drop'].forEach(ev => $('#pdf-dz').addEventListener(ev, e => { e.preventDefault(); $('#pdf-dz').classList.remove('drag'); }));
$('#pdf-dz').addEventListener('drop', e => {
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) return;
  const full = f.pywebviewFullPath || f.path;
  if (full && /\.pdf$/i.test(full)) setPdf(full);
  else toast('Não foi possível ler o caminho do arquivo arrastado. Clique para selecionar.', 'warn');
});

$('#out-pick').onclick = async () => {
  const ini = P.pdf ? P.pdf.split(/[\\/]/).pop().replace(/\.pdf$/i, '') + '.ofx' : 'extrato.ofx';
  const p = await py('pick_save', ini, 'ofx');
  if (p) { P.saida = p; $('#out-path').value = p; }
};

$('#auto-chk').onchange = () => { $('#layout-sel').disabled = $('#auto-chk').checked; };
$('#layout-sel').onchange = loadLayoutInfo;

async function loadLayoutInfo() {
  const key = $('#layout-sel').value;
  const info = await py('layout_info', key);
  $('#layout-title').textContent = info.nome || 'Modelo do extrato';
  $('#layout-desc').value = info.descricao || '';
  const box = $('#layout-img-box');
  if (info.imagem) box.innerHTML = `<img src="${info.imagem}" title="Clique para ampliar" class="max-w-full max-h-full object-contain">`;
  if (info.imagem) box.querySelector('img').onclick = () => zoomImg(info.imagem);
  else box.innerHTML = '<p class="text-sm text-slate-400 text-center px-6">Nenhuma imagem cadastrada<br>para este layout.</p>';
  $('#img-del').disabled = !info.imagem;
}
function zoomImg(src) {
  openModal(`<div class="p-3 overflow-auto"><img src="${src}" class="max-w-full mx-auto"></div>
    <div class="px-4 py-3 border-t border-slate-100 text-right"><button id="zm-close" class="px-4 py-2 text-sm rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50">Fechar</button></div>`, true);
  $('#zm-close').onclick = closeModal;
}
$('#desc-save').onclick = async () => {
  const k = $('#layout-sel').value; if (!k) return;
  await py('salvar_descricao', k, $('#layout-desc').value);
  Hub.log(`Observação salva para o layout '${k}'.`, 'ok');
};
$('#layout-desc').onkeydown = e => { if (e.key === 'Enter') $('#desc-save').click(); };
$('#img-add').onclick = async () => {
  const k = $('#layout-sel').value;
  if (!k) return toast('Selecione um layout de banco.', 'warn');
  const r = await py('cadastrar_imagem', k);
  if (r.ok) { Hub.log(`Imagem cadastrada para o layout '${k}'.`, 'ok'); loadLayoutInfo(); }
  else if (!r.cancelado) toast(r.erro || 'Falha ao cadastrar imagem.', 'err');
};
$('#img-del').onclick = async () => {
  const k = $('#layout-sel').value; if (!k) return;
  if (await confirmDialog('Remover imagem', 'Remover a imagem deste layout?', 'Remover')) {
    await py('remover_imagem', k); Hub.log(`Imagem removida do layout '${k}'.`); loadLayoutInfo();
  }
};

function setBusy(b) {
  P.busy = b;
  $('#btn-convert').disabled = b;
  $('#prog').classList.toggle('hidden', !b);
}

$('#btn-convert').onclick = async () => {
  if (P.busy) return;
  if (!P.pdf) return toast('Selecione um arquivo PDF.', 'warn');
  const auto = $('#auto-chk').checked, layout = $('#layout-sel').value;
  if (!auto && !layout) return toast('Selecione um layout de banco.', 'warn');
  $('#result-card').classList.add('hidden');
  setBusy(true);
  let r;
  try {
    r = await py('converter', { pdf: P.pdf, layout, auto, saida: P.saida, revisar: $('#revisar-chk').checked });
  } finally { setBusy(false); }
  handleConvertResult(r);
};

function handleConvertResult(r) {
  if (r.status === 'ok') return showResult(r);
  if (r.status === 'review') {
    if (r.layout) selectLayout(r.layout);
    return openReview(r);
  }
  Hub.log('Erro: ' + r.erro, 'erro');
  toast(r.erro, 'err');
  loadStats();
}
function selectLayout(key) {
  const sel = $('#layout-sel');
  if ([...sel.options].some(o => o.value === key)) { sel.value = key; loadLayoutInfo(); }
}
function showResult(r) {
  P.lastOfx = r.saida;
  $('#result-msg').textContent = r.msg;
  $('#result-path').textContent = r.saida;
  $('#result-card').classList.remove('hidden');
  if (r.layout) selectLayout(r.layout);
  toast('Arquivo OFX gerado com sucesso.');
  loadStats();
}
$('#res-folder').onclick = () => py('abrir_pasta', P.lastOfx);
$('#res-txt').onclick = async () => { switchModule('txt'); await txtLoad(P.lastOfx); };

/* ----- revisão manual ----- */
const R = { itens: [] };
function openReview(r) {
  R.itens = r.itens.map(x => ({ ...x }));
  const head = r.incompletos ? 'bg-red-800' : 'bg-primary-700';
  openModal(`
    <div class="${head} text-white px-6 py-4 rounded-t-xl">
      <h3 class="font-semibold">${r.incompletos ? '⚠️ Lançamentos incompletos encontrados' : 'Conferência de lançamentos do extrato'}</h3>
      <p class="text-xs opacity-90 mt-1">${esc(r.motivo || '')}</p>
    </div>
    <div class="p-5 overflow-auto flex-1">
      <table class="w-full text-sm">
        <thead class="text-xs text-slate-500 uppercase"><tr>
          <th class="px-1 py-1 text-left w-8">#</th><th class="px-1 text-left w-28">Data *</th>
          <th class="px-1 text-left">Descrição *</th><th class="px-1 text-left w-28">Documento</th>
          <th class="px-1 text-left w-28">Valor *</th><th class="px-1 text-left w-24">Tipo</th>
          <th class="px-1 text-left w-40">Situação</th><th class="w-8"></th></tr></thead>
        <tbody id="rv-body"></tbody>
      </table>
      <button id="rv-add" class="mt-3 px-3 py-1.5 text-sm rounded-lg border border-primary-600 text-primary-700 hover:bg-primary-50">+ Adicionar lançamento</button>
    </div>
    <div class="px-5 py-3 border-t border-slate-100 flex items-center justify-between gap-4">
      <p id="rv-sum" class="text-xs font-semibold"></p>
      <div class="flex gap-2">
        <button id="rv-cancel" class="px-4 py-2 text-sm rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50">Cancelar</button>
        <button id="rv-ok" class="px-4 py-2 text-sm rounded-lg bg-primary-600 text-white hover:bg-primary-700">Confirmar e gerar OFX</button>
      </div>
    </div>`, true);
  renderReview();
  $('#rv-add').onclick = () => {
    R.itens.push({ data:'', descricao:'', doc:'', valor:'', tipo:'D', nome:'', incompleto:true, motivo:'Novo lançamento' });
    renderReview(); const rows = $$('#rv-body tr'); rows[rows.length-1].querySelector('input').focus();
  };
  $('#rv-cancel').onclick = async () => {
    if (await confirmDialog('Cancelar', 'Deseja fechar sem gerar o arquivo OFX?', 'Fechar sem gerar')) {
      closeModal(); Hub.log('Revisão de lançamentos cancelada pelo usuário.');
    } else { /* a confirmação sobrescreve o modal; reabre a revisão */ reopenReview(r); }
  };
  $('#rv-ok').onclick = async () => {
    const res = await py('confirmar_revisao', R.itens.map(({data,descricao,doc,valor,tipo,nome}) => ({data,descricao,doc,valor,tipo,nome})));
    if (res.ok) { closeModal(); showResult(res); }
    else {
      toast(res.erro, 'err');
      if (res.idx !== undefined) {
        const tr = $$('#rv-body tr')[res.idx];
        if (tr) { tr.scrollIntoView({block:'center'}); tr.classList.add('bg-red-100'); }
      }
    }
  };
}
function reopenReview(r) {
  const keep = R.itens.map(x => ({ ...x }));
  openReview({ ...r, itens: keep });
}
function parseBR(s) {
  if (!s) return null; let t = String(s).replace(/R\$|\s/g, '');
  if (t.includes(',') && t.includes('.')) t = t.replace(/\./g, '').replace(',', '.'); else if (t.includes(',')) t = t.replace(',', '.');
  t = t.replace(/^[-+]/, '').replace(/[CDcd]$/, ''); const n = parseFloat(t); return isNaN(n) ? null : Math.abs(n);
}
function renderReview() {
  const body = $('#rv-body');
  body.innerHTML = R.itens.map((it, i) => {
    const inc = !it.data || !it.descricao || !it.valor;
    const inp = 'w-full rounded border border-slate-200 px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-primary-500';
    return `<tr data-i="${i}" class="${inc ? 'bg-red-50' : ''}">
      <td class="px-1 text-slate-400">${i+1}</td>
      <td class="px-1 py-1"><input data-f="data" class="${inp}" value="${esc(it.data)}" placeholder="dd/mm/aaaa"></td>
      <td class="px-1"><input data-f="descricao" class="${inp}" value="${esc(it.descricao)}"></td>
      <td class="px-1"><input data-f="doc" class="${inp}" value="${esc(it.doc)}"></td>
      <td class="px-1"><input data-f="valor" class="${inp}" value="${esc(it.valor)}" placeholder="0,00"></td>
      <td class="px-1"><select data-f="tipo" class="${inp}"><option value="D" ${it.tipo==='D'?'selected':''}>Débito (-)</option><option value="C" ${it.tipo==='C'?'selected':''}>Crédito (+)</option></select></td>
      <td class="px-1 text-xs ${inc ? 'text-red-700' : 'text-emerald-700'}">${inc ? '⚠️ ' + esc(it.motivo || 'Incompleto') : '✓ OK'}</td>
      <td class="px-1"><button data-del="${i}" class="text-red-500 hover:text-red-700" title="Excluir">✕</button></td></tr>`;
  }).join('');
  body.oninput = e => {
    const tr = e.target.closest('tr'); if (!tr || !e.target.dataset.f) return;
    R.itens[+tr.dataset.i][e.target.dataset.f] = e.target.value; updateReviewSummary();
  };
  body.onchange = e => { if (e.target.dataset.f === 'tipo') updateReviewSummary(); };
  body.onclick = e => {
    const d = e.target.closest('[data-del]'); if (!d) return;
    R.itens.splice(+d.dataset.del, 1); renderReview();
  };
  updateReviewSummary();
}
function updateReviewSummary() {
  let deb = 0, cred = 0, pend = 0;
  R.itens.forEach(it => {
    const v = parseBR(it.valor);
    if (!it.data || !it.descricao || v === null) pend++;
    if (v !== null) { if (it.tipo === 'D') deb += v; else cred += v; }
  });
  const el = $('#rv-sum'); if (!el) return;
  el.textContent = `Total: ${R.itens.length} lançamentos   |   Débitos: R$ ${brl(deb)}   |   Créditos: R$ ${brl(cred)}   |   ` +
    (pend ? `⚠️ Pendentes: ${pend}` : '✓ Todos preenchidos');
  el.className = 'text-xs font-semibold ' + (pend ? 'text-red-700' : 'text-emerald-700');
}

/* =================================================================
   EXTRATO → TXT
================================================================= */
const T = { lan: [], perfil: '', perfis: [], sortKey: null, asc: true, regras: 0, memoria: 0 };

function txtApply(snap) {
  T.perfil = snap.perfil; T.perfis = snap.perfis; T.regras = snap.regras; T.memoria = snap.memoria;
  T.lan = snap.lancamentos || T.lan;
  { const cn = $('#txt-num'); if (cn) cn.checked = !!snap.considerar_numeros; }
  const sel = $('#perfil-sel');
  sel.innerHTML = T.perfis.map(p => `<option ${p === T.perfil ? 'selected' : ''}>${esc(p)}</option>`).join('');
  $('#side-perfil').textContent = T.perfil || '—';
  txtRender();
}
function txtStatus() {
  const total = T.lan.length, ok = T.lan.filter(l => (l.codigo || '').trim()).length;
  $('#txt-status').textContent = `Perfil: ${T.perfil || '(nenhum)'}   |   Total: ${total}   OK: ${ok}   Sem código: ${total - ok}   |   Regras: ${T.regras}   |   Memória: ${T.memoria}`;
}
function txtRender() {
  txtStatus();
  const body = $('#txt-body'), empty = $('#txt-empty');
  let rows = T.lan.map((l, i) => ({ l, i }));
  const q = $('#txt-filter').value.trim().toUpperCase();
  if (q) rows = rows.filter(r => r.l.descricao.toUpperCase().includes(q));
  if ($('#txt-missing').checked) rows = rows.filter(r => !(r.l.codigo || '').trim());
  if (T.sortKey) {
    const k = T.sortKey, dkey = s => s.split('/').reverse().join('');
    rows.sort((a, b) => {
      let x, y;
      if (k === 'data') { x = dkey(a.l.data); y = dkey(b.l.data); }
      else if (k === 'valor') { x = a.l.valor; y = b.l.valor; }
      else if (k === 'codigo') { x = [(a.l.codigo||'').trim() ? 0 : 1, a.l.descricao.toLowerCase()]; y = [(b.l.codigo||'').trim() ? 0 : 1, b.l.descricao.toLowerCase()]; }
      else { x = String(a.l[k]).toLowerCase(); y = String(b.l[k]).toLowerCase(); }
      const c = Array.isArray(x) ? (x[0] - y[0] || (x[1] < y[1] ? -1 : x[1] > y[1] ? 1 : 0)) : (x < y ? -1 : x > y ? 1 : 0);
      return T.asc ? c : -c;
    });
  }
  $$('th.sortable').forEach(th => {
    const base = th.textContent.replace(/ [▲▼]$/, '');
    th.textContent = base + (th.dataset.sort === T.sortKey ? (T.asc ? ' ▲' : ' ▼') : '');
  });
  empty.classList.toggle('hidden', T.lan.length > 0);
  body.innerHTML = rows.map(({ l }, n) => `
    <tr class="${n % 2 ? 'bg-slate-50' : 'bg-white'} border-t border-slate-100" data-uid="${esc(l.uid)}">
      <td class="px-3 py-1.5 font-bold ${l.tipo === 'C' ? 'text-emerald-600' : 'text-red-500'}">${l.tipo}</td>
      <td class="px-3 py-1.5 text-slate-600">${esc(l.data)}</td>
      <td class="px-3 py-1.5 text-right tabular-nums">${brl(l.valor)}</td>
      <td class="desc-cell px-3 py-1.5 text-slate-700">${esc(l.descricao)}</td>
      <td class="px-3 py-1"><input class="code-in w-full rounded border border-slate-200 px-2 py-1 text-teal-700 focus:outline-none focus:ring-2 focus:ring-primary-500" value="${esc(l.codigo)}"></td>
      <td class="px-3 py-1"><button class="unlink text-xs px-2 py-1 rounded border border-red-300 text-red-700 hover:bg-red-50">✕ Desvincular</button></td>
    </tr>`).join('');
}
$$('th.sortable').forEach(th => th.onclick = () => {
  const k = th.dataset.sort;
  if (T.sortKey === k) T.asc = !T.asc; else { T.sortKey = k; T.asc = true; }
  txtRender();
});
$('#txt-filter').oninput = txtRender;
$('#txt-missing').onchange = txtRender;
$('#txt-num').onchange = async () => {
  const r = await py('txt_considerar_numeros', $('#txt-num').checked);
  if (r.ok) { T.lan = r.lancamentos; T.memoria = r.memoria; txtRender(); }
};

function txtUpdates(updates, origemUid) {
  Object.entries(updates || {}).forEach(([uid, code]) => {
    const l = T.lan.find(x => x.uid === uid); if (l) l.codigo = code;
    const tr = $$('#txt-body tr').find(r => r.dataset.uid === uid);
    if (tr) {
      const inp = tr.querySelector('input'); inp.value = code;
      if (uid !== origemUid && code) { inp.classList.add('flash'); setTimeout(() => inp.classList.remove('flash'), 600); }
    }
  });
  txtStatus();
}
$('#txt-body').addEventListener('change', async e => {
  if (!e.target.classList.contains('code-in')) return;
  const uid = e.target.closest('tr').dataset.uid;
  const r = await py('txt_set_codigo', uid, e.target.value.trim());
  if (r.ok) { if (r.memoria !== undefined) T.memoria = r.memoria; txtUpdates(r.updates, uid); }
});
$('#txt-body').addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.classList.contains('code-in')) e.target.blur(); });
$('#txt-body').addEventListener('click', async e => {
  const b = e.target.closest('.unlink'); if (!b) return;
  const uid = b.closest('tr').dataset.uid;
  const r = await py('txt_desvincular', uid); txtUpdates(r.updates, uid);
});

function txtBusy(b) {
  $('#txt-prog').classList.toggle('hidden', !b);
  ['#txt-open', '#txt-gen', '#perfil-new', '#perfil-del', '#rules-open', '#perfil-sel'].forEach(s => {
    const el = $(s); if (el) el.disabled = b;
  });
  $('#txt-file').classList.toggle('animate-pulse', b);
}
async function txtLoad(path) {
  if (!path) return;
  const name = path.split(/[\\/]/).pop();
  $('#txt-file').textContent = 'Carregando ' + name + '…';
  txtBusy(true);
  let r;
  try { r = await py('txt_abrir', path); }
  catch (e) { txtBusy(false); $('#txt-file').textContent = 'Nenhum arquivo selecionado'; return; }
  txtBusy(false);
  if (!r.ok) { $('#txt-file').textContent = 'Nenhum arquivo selecionado'; return toast(r.erro, 'err'); }
  $('#txt-file').textContent = r.arquivo + (r.layout ? `  •  layout detectado: ${r.layout}` : '');
  T.sortKey = null; T.asc = true;
  txtApply(r);
}
$('#txt-open').onclick = async () => txtLoad(await py('pick_extrato'));
$('#txt-gen').onclick = async () => {
  if (!T.lan.length) return toast('Nenhum extrato carregado.', 'warn');
  const sem = T.lan.filter(l => !(l.codigo || '').trim()).length;
  if (sem && !(await confirmDialog('Confirmar exportação', `${sem} lançamento(s) sem código NÃO serão exportados.\nDeseja continuar mesmo assim?`, 'Continuar'))) return;
  const out = await py('pick_save', 'lancamentos.txt', 'txt');
  if (!out) return;
  const r = await py('txt_gerar', out);
  if (r.ok) toast(`${r.n} lançamentos exportados para ${r.saida}`); else toast(r.erro, 'err');
};

$('#perfil-sel').onchange = async () => { const r = await py('txt_trocar_perfil', $('#perfil-sel').value); if (r.ok) txtApply(r); };
$('#perfil-new').onclick = async () => {
  const name = await promptDialog('Novo perfil', 'Nome do perfil:'); if (name === null) return;
  const r = await py('txt_novo_perfil', name); if (r.ok) txtApply(r); else toast(r.erro, 'err');
};
$('#perfil-del').onclick = async () => {
  const name = $('#perfil-sel').value; if (!name) return;
  if (!(await confirmDialog('Excluir perfil', `Excluir o perfil '${name}' e todos os dados salvos?`, 'Excluir'))) return;
  const r = await py('txt_excluir_perfil', name); if (r.ok) txtApply(r);
};

/* ----- regras ----- */
async function openRules() {
  if (!T.perfil) return toast('Selecione ou crie um perfil primeiro.', 'warn');
  let regras = await py('txt_regras'); let sel = new Set();
  const render = () => {
    $('#rl-body').innerHTML = regras.map(r => `<tr data-kw="${esc(r.kw)}" class="border-t border-slate-100 cursor-pointer ${sel.has(r.kw) ? 'bg-primary-100' : ''}">
      <td class="px-3 py-1.5">${esc(r.kw)}</td><td class="px-3 py-1.5 font-mono text-teal-700">${esc(r.code)}</td></tr>`).join('') ||
      '<tr><td colspan="2" class="p-4 text-center text-slate-400 text-sm">Nenhuma regra cadastrada.</td></tr>';
  };
  openModal(`<div class="p-6 flex flex-col min-h-0">
    <h3 class="font-semibold text-violet-800">Regras do perfil: ${esc(T.perfil)}</h3>
    <p class="text-xs text-slate-500 mt-1 mb-3">Cadastre um trecho (palavra ou frase) e um código. Ao abrir um extrato, todo lançamento que contenha o trecho será preenchido automaticamente.</p>
    <div class="border border-slate-200 rounded-lg overflow-auto max-h-64"><table class="w-full text-sm"><thead class="bg-slate-100 text-xs text-slate-500 uppercase"><tr><th class="px-3 py-1.5 text-left">Palavra-chave / trecho</th><th class="px-3 py-1.5 text-left w-28">Código</th></tr></thead><tbody id="rl-body"></tbody></table></div>
    <div class="flex gap-2 mt-4">
      <input id="rl-kw" placeholder="Trecho" class="flex-1 min-w-0 rounded-lg border border-slate-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500">
      <input id="rl-code" placeholder="Código" class="w-28 rounded-lg border border-slate-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500">
    </div>
    <div class="flex gap-2 mt-3 flex-wrap">
      <button id="rl-add" class="px-3 py-1.5 text-sm rounded-lg bg-emerald-600 text-white hover:bg-emerald-700">Adicionar regra</button>
      <button id="rl-del" class="px-3 py-1.5 text-sm rounded-lg border border-red-300 text-red-700 hover:bg-red-50">Excluir selecionadas</button>
    </div>
    <div class="flex justify-between mt-5">
      <button id="rl-apply" class="px-4 py-2 text-sm rounded-lg bg-amber-500 text-white hover:bg-amber-600">Aplicar regras ao extrato atual</button>
      <button id="rl-close" class="px-4 py-2 text-sm rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50">Fechar</button>
    </div></div>`);
  render();
  $('#rl-body').onclick = e => {
    const tr = e.target.closest('tr[data-kw]'); if (!tr) return;
    const k = tr.dataset.kw; sel.has(k) ? sel.delete(k) : sel.add(k); render();
  };
  const add = async () => {
    const r = await py('txt_regra_add', $('#rl-kw').value, $('#rl-code').value);
    if (!r.ok) return toast(r.erro, 'err');
    regras = r.regras; T.regras = regras.length; txtStatus(); render();
    $('#rl-kw').value = ''; $('#rl-code').value = ''; $('#rl-kw').focus();
  };
  $('#rl-add').onclick = add;
  $('#rl-kw').onkeydown = $('#rl-code').onkeydown = e => { if (e.key === 'Enter') add(); };
  $('#rl-del').onclick = async () => {
    if (!sel.size) return;
    const r = await py('txt_regra_del', [...sel]); regras = r.regras; sel = new Set(); T.regras = regras.length; txtStatus(); render();
  };
  $('#rl-apply').onclick = async () => {
    const r = await py('txt_regras_aplicar');
    if (!r.ok) return toast(r.erro, 'warn');
    txtUpdates(r.updates); toast(`Regras aplicadas. ${r.n} lançamento(s) atualizado(s).`);
  };
  $('#rl-close').onclick = closeModal;
}
$('#rules-open').onclick = openRules;

/* ---------- home / sistema ---------- */
async function loadStats() {
  const s = await py('get_stats');
  $('#st-hoje').textContent = s.hoje; $('#st-mes').textContent = s.mes;
  $('#st-taxa').textContent = s.taxa === null ? '—' : s.taxa + '%';
}
let SYS = null;
function renderSys() {
  if (!SYS) return;
  const row = (t, ok, d) => `<div class="bg-white rounded-xl border border-slate-200 p-5 shadow-sm flex items-start gap-3">
    <span class="mt-0.5 w-2.5 h-2.5 rounded-full ${ok ? 'bg-emerald-500' : 'bg-red-500'}"></span>
    <div><p class="text-sm font-medium text-slate-700">${esc(t)}</p><p class="text-xs text-slate-500 mt-0.5 break-all">${esc(d)}</p></div></div>`;
  $('#sys-box').innerHTML =
    row('parsers (layouts)', SYS.layouts.length > 0, SYS.layouts.length ? SYS.layouts.length + ' layouts carregados' : (SYS.errors.parsers || 'nenhum layout')) +
    row('ofx_writer', SYS.ofx_writer, SYS.ofx_writer ? 'gerar_ofx disponível' : (SYS.errors.ofx_writer || 'não encontrado')) +
    row('Pasta do aplicativo', true, SYS.base_dir);
}

/* ---------- inicialização ---------- */
async function init() {
  SYS = await py('init_info');
  const sel = $('#layout-sel');
  sel.innerHTML = SYS.layouts.map(l => `<option value="${esc(l.key)}">${esc(l.key)} — ${esc(l.nome)}</option>`).join('');
  if (!SYS.layouts.length) sel.innerHTML = '<option value="">(nenhum layout carregado)</option>';
  await loadLayoutInfo();
  txtApply(await py('txt_state'));
  loadStats(); renderSys();
  if (Object.keys(SYS.errors).length) toast('Atenção: ' + Object.entries(SYS.errors).map(([k, v]) => k + ' (' + v + ')').join(' | '), 'warn');
}
window.addEventListener('pywebviewready', init);
</script>

<script>
(function () {
  var cur = 'claro';
  try { cur = localStorage.getItem('hub-theme') || 'claro'; } catch (e) {}
  function apply(t) {
    document.documentElement.classList.toggle('dark', t === 'black');
    var b = document.getElementById('theme-btn');
    if (b) b.textContent = t === 'black' ? 'Tema: Black' : 'Tema: Claro';
  }
  function setTheme(t) {
    cur = t; apply(t);
    try { localStorage.setItem('hub-theme', t); } catch (e) {}
    try { window.pywebview.api.set_tema(t); } catch (e) {}
  }
  apply(cur);
  var tb = document.getElementById('theme-btn');
  if (tb) tb.addEventListener('click', function () {
    setTheme(cur === 'black' ? 'claro' : 'black');
  });
  window.addEventListener('pywebviewready', async function () {
    try {
      var t = await window.pywebview.api.get_tema();
      if (t && t !== cur) { cur = t; apply(t); }
    } catch (e) {}
  });

  function paint() {
    document.querySelectorAll('button').forEach(function (b) {
      if (b.classList.contains('btn-blue')) return;
      if (b.classList.contains('sidebar-item') || b.classList.contains('goto-btn')) return;
      if (b.hasAttribute('data-del') || b.id === 'log-clear') return;
      b.classList.add('btn-blue');
    });
  }
  var pending = false;
  new MutationObserver(function () {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () { pending = false; paint(); });
  }).observe(document.body, { childList: true, subtree: true });
  paint();
})();
</script>
</body>
</html>
"""


def main():
    api = Api()
    window = webview.create_window(
        "ConvertHub  |  PDF → OFX  •  Extrato → TXT",
        html=HTML, js_api=api, width=1320, height=840,
        min_size=(1050, 680))
    api._window = window
    webview.start()


if __name__ == "__main__":
    main()