"""Parser do extrato de conta corrente do Banco do Brasil (PDF "Extrato de Conta Corrente").

Tabela: Dia | Lote | Documento | Histórico | Valor ("108,84 (-)").
As colunas são localizadas pelo cabeçalho de cada página (não depende de
posições fixas) e cada transação é delimitada pelas linhas separadoras da
tabela. Ignora "Saldo do dia"; usa "Saldo Anterior" e o "S A L D O" final.
Trata a linha quebrada entre páginas (data truncada + fragmento no topo
da página seguinte).
"""

import bisect
import re
import unicodedata
from datetime import date, timedelta

import pdfplumber

NOME = "Banco do Brasil (extrato de conta corrente)"
BANCO_ID = "001"
BANCO_ORG = "Banco do Brasil S.A."

RE_DATA = re.compile(r"^(\d{2})/(\d{2})/(\d{2,4})$")
RE_CONT = re.compile(r"^\d{1,2}$")
RE_VALOR = re.compile(r"(\d[\d.]*,\d{2})\s*\(([+-])\)")
RE_VAL_TOKEN = re.compile(r"^(\d[\d.]*,\d{2}(\([+-]\))?|\([+-]\))$")
RE_AGCONTA = re.compile(r"Ag[eê]ncia:?\s*([\dXx.\-]+)\s+Conta:?\s*([\dXx.\-]+)")
TOL = 2  # tolerância (pt) para juntar palavras na mesma linha


def _norm(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _num(txt, sinal):
    v = float(txt.replace(".", "").replace(",", "."))
    return -v if sinal == "-" else v


def _colunas(words, largura):
    """x0 das colunas Lote/Documento/Histórico e topo do cabeçalho."""
    cab = {}
    for w in words:
        k = _norm(w["text"]).lower()
        if k in ("lote", "documento", "historico") and k not in cab:
            cab[k] = w
    if len(cab) == 3 and all(abs(cab[k]["top"] - cab["documento"]["top"]) < 3
                             for k in cab):
        return (cab["lote"]["x0"], cab["documento"]["x0"],
                cab["historico"]["x0"], cab["documento"]["top"])
    return largura * 0.152, largura * 0.258, largura * 0.449, None


def _separadores(page):
    tops = sorted(e["top"] for e in page.edges
                  if e["orientation"] == "h"
                  and (e["x1"] - e["x0"]) >= 0.6 * page.width)
    out = []
    for t in tops:
        if not out or t - out[-1] > 1.5:
            out.append(t)
    return out


def _seps_por_valor(words, x_val):
    """Plano B (sem linhas na tabela): pontos médios entre valores."""
    tops = sorted(w["top"] for w in words
                  if w["x0"] >= x_val and re.match(r"^\d[\d.]*,\d{2}", w["text"]))
    uniq = []
    for t in tops:
        if not uniq or t - uniq[-1] > 3:
            uniq.append(t)
    return [(a + b) / 2 for a, b in zip(uniq, uniq[1:])]


def _texto(ws):
    ws = sorted(ws, key=lambda w: (w["top"], w["x0"]))
    linhas, atual, topo = [], [], None
    for w in ws:
        if topo is None or abs(w["top"] - topo) <= TOL:
            topo = w["top"] if topo is None else topo
            atual.append(w)
        else:
            linhas.append(atual)
            atual, topo = [w], w["top"]
    if atual:
        linhas.append(atual)
    return " ".join(" ".join(w["text"] for w in sorted(l, key=lambda w: w["x0"]))
                    for l in linhas).strip()


def _linha(ws, x_lote, x_doc, x_hist, x_val, m):
    dia, lote, doc, hist, val = [], [], [], [], []
    for w in ws:
        x = w["x0"]
        if x < x_lote - m:
            dia.append(w)
        elif x < x_doc - m:
            lote.append(w)
        elif x < x_hist - m:
            doc.append(w)
        elif x >= x_val and RE_VAL_TOKEN.match(w["text"]):
            val.append(w)
        else:
            hist.append(w)

    data, cont = None, False
    for w in dia:
        mt = RE_DATA.match(w["text"])
        if mt:
            data = mt.groups()
    mv = RE_VALOR.search("".join(w["text"] for w in sorted(val, key=lambda w: w["x0"])))
    valor = _num(mv.group(1), mv.group(2)) if mv else None
    if data is None and valor is None and any(RE_CONT.match(w["text"]) for w in dia):
        cont = True   # fragmento da linha quebrada entre páginas
    return {"data": data, "cont": cont, "valor": valor,
            "lote": _texto(lote), "doc": _texto(doc), "desc": _texto(hist)}


def _linhas_pagina(page):
    words = page.extract_words()
    largura = float(page.width)
    x_lote, x_doc, x_hist, topo = _colunas(words, largura)
    m = largura * 0.006
    x_val = x_hist + largura * 0.25

    if topo is not None:
        words = [w for w in words if w["top"] >= topo - 1]
    corte = [w["top"] for w in words
             if w["x0"] < x_lote - m and w["text"].lower().startswith("total")]
    if corte:
        words = [w for w in words if w["top"] < min(corte) - 1]

    seps = [s for s in _separadores(page) if topo is None or s > topo]
    if len(seps) < 2:
        seps = _seps_por_valor(words, x_val)

    bandas = {}
    for w in words:
        cy = (w["top"] + w["bottom"]) / 2
        bandas.setdefault(bisect.bisect(seps, cy), []).append(w)
    return [_linha(bandas[i], x_lote, x_doc, x_hist, x_val, m)
            for i in sorted(bandas)]


def _extrair(pdf_path):
    linhas, textos = [], []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            textos.append(page.extract_text() or "")
            linhas.extend(_linhas_pagina(page))
    texto = "\n".join(textos)

    trans, ultima, ano = [], None, None
    ini = fim = saldo_ini = saldo_fim = None

    for l in linhas:
        if l["cont"]:
            if ultima is not None and l["desc"]:
                ultima["descricao"] = (ultima["descricao"] + " " + l["desc"]).strip()
            continue
        if l["data"] is None and l["valor"] is None:
            continue

        d = mth = None
        if l["data"]:
            dd, mm, aa = l["data"]
            if len(aa) == 4 and int(aa) > 0:
                ano = int(aa)
            elif len(aa) < 4 and ano is not None:
                pass                      # ano truncado: usa o último conhecido
            if int(dd) and int(mm) and ano:
                try:
                    d = date(ano, int(mm), int(dd))
                except ValueError:
                    d = None

        h = re.sub(r"\s+", "", _norm(l["desc"])).lower()
        if h.startswith("saldo"):
            if "anterior" in h:
                ini, saldo_ini = d, l["valor"]
            elif "dodia" not in h:
                fim, saldo_fim = d, l["valor"]
            ultima = None
            continue

        t = {"data": d, "valor": l["valor"], "descricao": l["desc"],
             "doc": l["doc"], "lote": l["lote"], "nome": None}
        if d is None:
            t["incompleto"], t["motivo"] = True, "Data não identificada"
        elif l["valor"] is None:
            t["incompleto"], t["motivo"] = True, "Valor não identificado"
        trans.append(t)
        ultima = t

    if not trans:
        raise ValueError("Nenhuma transação encontrada — isto é um extrato "
                         "de conta corrente do Banco do Brasil?")
    return trans, texto, ini, fim, saldo_ini, saldo_fim


def parse(pdf_path) -> list:
    """Lista de dicts: data, valor, descricao, doc, lote, nome."""
    return _extrair(pdf_path)[0]


def info(pdf_path) -> dict:
    trans, texto, ini, fim, saldo_ini, saldo_fim = _extrair(pdf_path)
    m = RE_AGCONTA.search(texto)
    agencia, conta = (m.group(1), m.group(2)) if m else ("", "")
    datas = [t["data"] for t in trans if t["data"]]
    vals = [t["valor"] for t in trans if t["valor"] is not None]
    if saldo_fim is None and saldo_ini is not None:
        saldo_fim = round(saldo_ini + sum(vals), 2)
    return {
        "banco_id": BANCO_ID,
        "agencia": agencia,
        "conta": conta,
        "acctid": conta,
        "periodo_inicio": (ini + timedelta(days=1)) if ini else min(datas),
        "periodo_fim": fim or max(datas),
        "saldo_inicial": saldo_ini,
        "saldo_final": saldo_fim,
        "total_creditos": round(sum(v for v in vals if v > 0), 2),
        "total_debitos": round(-sum(v for v in vals if v < 0), 2),
    }


def validar(transacoes, dados) -> list:
    avisos = []
    for t in transacoes:
        if t.get("incompleto"):
            avisos.append(f"{t.get('motivo')}: {t['descricao']}")
    vals = [t["valor"] for t in transacoes if t["valor"] is not None]
    ini, fim = dados.get("saldo_inicial"), dados.get("saldo_final")
    if ini is not None and fim is not None \
            and abs(ini + sum(vals) - fim) > 0.005:
        avisos.append(f"Saldo: anterior {ini:.2f} + movimento {sum(vals):.2f} "
                      f"!= final {fim:.2f}")
    return avisos


avisos = validar

if __name__ == '__main__':
    import sys
    from ofx_writer import gerar_ofx

    if len(sys.argv) < 3:
        sys.exit('uso: python parser_bb.py extrato.pdf saida.ofx')
    trans = parse(sys.argv[1])
    dados = info(sys.argv[1])
    for aviso in validar(trans, dados):
        print('AVISO:', aviso)
    gerar_ofx(trans, dados, sys.argv[2])
    print(str(len(trans)) + ' transações -> ' + sys.argv[2])