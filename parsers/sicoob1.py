"""Parser do layout Credibom / Sicoob (extrato SISBR, conta corrente).

Versão 5 — reescrita da separação de colunas
--------------------------------------------
Causas das lançamentos incompletos na v4:

1. O ícone de seta (« / 7) / <! ...) que antecede o Histórico é extraído como
   uma "palavra" em x0≈138. Ele entrava na descrição e, nas linhas de
   documento numérico, era usado como "início do Histórico" na calibração.
2. A calibração dependia de documento só com dígitos; "000.473" (cheque),
   "CASH BACK", "CTBC TELEC", "CONSÓRCIOS", "Pix" etc. não entravam.
3. Valor era procurado só dentro de `resto` e dependia de o token "R$"
   vir separado; qualquer variação deixava o lançamento "sem valor".

Agora as colunas são fixas e descobertas pelo cabeçalho de cada página
(Data | Documento | Histórico | Valor), e cada linha é separada por posição X:

    x <  x_doc            -> Data
    x_doc <= x < x_hist   -> Documento (numérico ou textual, qualquer nº de palavras)
    x >= x_hist           -> Histórico (ícone descartado)
    x >= x_valor          -> Valor ("R$ 1.234,56C/D")
"""

import re
from datetime import date

import pdfplumber

NOME = "Sicoob (extrato SISBR)"

BANCO_NOME = "Banco Cooperativo do Brasil"
BANCO_ID = "756"

# ---- PARÂMETROS AJUSTÁVEIS (altere aqui se o layout variar) ----------------
TOL_LINHA_PRINCIPAL = 5   # pt abaixo do topo da data ainda é a linha principal
TOL_VALOR_COLUNA = 4      # folga (pt) à esquerda da coluna Valor
LARGURA_MAX_VALOR = 60    # limita o X da coluna Valor ao padrão + este valor
ZERO_VIRA_D = True        # "59,000" (D lido como 0) -> débito
# Fallbacks (pt) caso o cabeçalho da página não seja encontrado.
X_DOC_PADRAO = 65
X_HIST_PADRAO = 135
X_VALOR_PADRAO = 440
TOL_COLUNA = 3
LARGURA_ICONE = 12  # ícone de seta tem < 12pt de largura

RE_DATA = re.compile(r"^\d{2}/\d{2}$")
RE_VALOR = re.compile(r"R?\$?\s*(\d{1,3}(?:\.\d{3})*,\d{2})\s*([DC0])(?![\w])\*?")
RE_COOPERATIVA = re.compile(r"Cooperativa:\s*([\d.\-]+\d)")
RE_CONTA = re.compile(r"Conta:\s*([\d.\-]+\d)")
RE_PERIODO = re.compile(
    r"Periodo:\s*(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})"
)

JUNK = (
    "http", "histórico de movimenta", "historico de movimenta",
    "sistema de cooperativas", "plataforma de serviços", "extrato de conta corrente",
    "cooperativa:", "conta:", "periodo:", "sicoob | internet banking",
    "data documento", "resumo", "saldo em conta", "cheque especial",
    "juros vencidos", "tarifas vencidas", "saldo disponível", "saldo bloqueado (",
    "encargos", "previsão", "outras informações", "vencimento de cheque",
    "taxa cheque", "custo efetivo", "informações", "sac:",
)

PREFIXOS_SALDO = ("SALDO DO DIA", "SALDO ANTERIOR", "SALDO BLOQUEADO")

PREFIXOS_NOME = (
    "PAGAMENTO PIX", "RECEBIMENTO PIX", "REM.:", "REM:", "FAV.:", "FAV:",
    "CASH BACK", "FAVORECIDO:", "ORIGEM:",
)

HISTORICOS_COM_ESPACO_FINAL = {
    "TARIFA RENOVAÇÃO LIMITE CONTA GARANTIDA",
}


# ----------------------------------------------------------------- utilidades
def _agrupar_linhas(words, tol=3):
    linhas, atual, topo = [], [], None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if topo is None or w["top"] - topo <= tol:
            atual.append(w)
            topo = w["top"] if topo is None else topo
        else:
            linhas.append(atual)
            atual, topo = [w], w["top"]
    if atual:
        linhas.append(atual)
    return linhas


def _texto(ws):
    return " ".join(w["text"] for w in sorted(ws, key=lambda w: w["x0"]))


def _junk(texto: str) -> bool:
    t = texto.lower().strip()
    return any(t.startswith(j) for j in JUNK)


def _is_saldo(texto: str) -> bool:
    return texto.upper().strip().startswith(PREFIXOS_SALDO)


def _colunas(linhas):
    """x_doc, x_hist, x_valor e índice da linha de cabeçalho (ou None)."""
    for i, linha in enumerate(linhas):
        lo = sorted(linha, key=lambda w: w["x0"])
        t = [w["text"].lower() for w in lo]
        if "data" in t and "documento" in t and any(x.startswith("hist") for x in t):
            x_doc = lo[t.index("documento")]["x0"]
            x_hist = next(w["x0"] for w in lo if w["text"].lower().startswith("hist"))
            x_val = lo[t.index("valor")]["x0"] if "valor" in t else None
            return x_doc, x_hist, x_val, i
    return None, None, None, None


def _celulas(linha, x_doc, x_hist, x_valor):
    lo = sorted(linha, key=lambda w: w["x0"])

    # Valor: tudo a partir do token "R$" (por conteúdo), senão por posição.
    idx_rs = next((i for i, w in enumerate(lo) if w["text"] in ("R$", "R$*", "$")), None)
    if idx_rs is None:
        idx_rs = next((i for i, w in enumerate(lo) if w["x0"] >= x_valor), None)
    valor = None
    corpo = lo
    if idx_rs is not None:
        m = RE_VALOR.search(" ".join(w["text"] for w in lo[idx_rs:]))
        if m:
            valor = float(m.group(1).replace(".", "").replace(",", "."))
            if m.group(2) == "D" or (ZERO_VIRA_D and m.group(2) == "0"):
                valor = -valor
            corpo = lo[:idx_rs]

    data_w = [w for w in corpo if w["x0"] < x_doc - TOL_COLUNA]
    data = " ".join(w["text"] for w in data_w).strip()
    resto = [w for w in corpo if w["x0"] >= x_doc - TOL_COLUNA]

    corte = x_hist - TOL_COLUNA
    doc_w = [w for w in resto if w["x0"] < corte]
    hist_w = [w for w in resto if w["x0"] >= corte]

    # Descarta o ícone de seta (palavra curta logo no início do Histórico),
    # apenas em linhas com data (linhas de detalhe não têm ícone).
    if RE_DATA.match(data) and hist_w:
        p = hist_w[0]
        if (p["x1"] - p["x0"]) < LARGURA_ICONE and p["x0"] <= x_hist + 12:
            hist_w = hist_w[1:]

    return {
        "data": data,
        "doc": " ".join(w["text"] for w in doc_w).strip(),
        "hist": " ".join(w["text"] for w in hist_w).strip(),
        "valor": valor,
        "x_min": lo[0]["x0"] if lo else 0,
    }


def _data_com_ano(dd_mm: str, ini: date, fim: date) -> date:
    dia, mes = int(dd_mm[:2]), int(dd_mm[3:5])
    ano = fim.year if mes <= fim.month else ini.year
    if ini.year != fim.year and mes > fim.month:
        ano = ini.year
    return date(ano, mes, dia)


# --------------------------------------------------------------------- leitura
def _classificar_pagina(pagina):
    """Devolve lista de "registros" da página, ancorados pela data.

    Cada registro = {"ancora": bool, "palavras": [...]} em ordem vertical.
    Não agrupa por tolerância de `top` (o valor R$ fica até ~4pt fora do
    alinhamento da data e era separado da linha); em vez disso:
      * cada data na coluna Data (x < x_doc) é uma âncora;
      * palavras de valor (x >= x_valor) vão para a âncora de centro vertical mais próximo;
      * demais palavras: até 5pt abaixo do topo da âncora = linha principal;
        mais abaixo = linha de detalhe da âncora.
    """
    words = pagina.extract_words()
    linhas = _agrupar_linhas(words)
    x_doc, x_hist, x_val, i_cab = _colunas(linhas)
    x_doc = x_doc or X_DOC_PADRAO
    x_hist = x_hist or X_HIST_PADRAO
    x_val = x_val or X_VALOR_PADRAO
    x_val = min(x_val, X_VALOR_PADRAO + LARGURA_MAX_VALOR)
    top_cab = min(w["top"] for w in linhas[i_cab]) if i_cab is not None else -1

    corpo = [w for w in words if w["top"] > top_cab + 2]
    # corta rodapé de resumo e URL
    tops_resumo = [w["top"] for w in corpo if w["text"].upper() == "RESUMO" and w["x0"] < x_doc + 40]
    if tops_resumo:
        corpo = [w for w in corpo if w["top"] < min(tops_resumo) - 2]
    corpo = [w for w in corpo if not w["text"].startswith("http")]

    ancoras = sorted(
        [w for w in corpo if w["x0"] < x_doc - TOL_COLUNA and RE_DATA.match(w["text"])],
        key=lambda w: w["top"],
    )
    centro = lambda w: (w["top"] + w["bottom"]) / 2

    reg = [{"ancora": a, "main": [a], "detalhes": {}} for a in ancoras]
    pre = {}  # detalhes antes da primeira âncora (continuação da página anterior)
    for w in corpo:
        if w in ancoras:
            continue
        if not reg:
            pre.setdefault(round(w["top"]), []).append(w)
            continue
        if w["x0"] >= x_val - TOL_VALOR_COLUNA and re.match(r"^(R\$|\$|[\d.]+,\d{2}[DC0]?\*?)", w["text"]):
            r = min(reg, key=lambda r: abs(centro(r["ancora"]) - centro(w)))
            r["main"].append(w)
            continue
        # última âncora cujo topo está acima do topo da palavra (+5pt de folga)
        cand = [r for r in reg if r["ancora"]["top"] <= w["top"] + TOL_LINHA_PRINCIPAL]
        if not cand:
            pre.setdefault(round(w["top"]), []).append(w)
            continue
        r = cand[-1]
        if w["top"] - r["ancora"]["top"] <= TOL_LINHA_PRINCIPAL:
            r["main"].append(w)
        else:
            r["detalhes"].setdefault(round(w["top"] / 4), []).append(w)
    return x_doc, x_hist, x_val, pre, reg


def _varrer(pdf_path):
    """Gera (cel, tipo) em ordem: tipo = "linha" (com data) ou "detalhe"."""
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            x_doc, x_hist, x_val, pre, reg = _classificar_pagina(pagina)
            for k in sorted(pre):
                yield _celulas(pre[k], x_doc, x_hist, x_val), "detalhe"
            for r in reg:
                yield _celulas(r["main"], x_doc, x_hist, x_val), "linha"
                for k in sorted(r["detalhes"]):
                    yield _celulas(r["detalhes"][k], x_doc, x_hist, x_val), "detalhe"


def parse(pdf_path) -> list:
    cab = _ler_cabecalho(pdf_path)
    ini, fim = cab["periodo_inicio"], cab["periodo_fim"]

    transacoes = []
    detalhe_ok = False  # persiste entre páginas (detalhe pode abrir a página)

    for cel, tipo in _varrer(pdf_path):
        if tipo == "linha":
            if _is_saldo(f'{cel["doc"]} {cel["hist"]}'):
                detalhe_ok = False
                continue
            if not (cel["hist"] or cel["doc"]):
                continue
            descricao = cel["hist"]
            if descricao in HISTORICOS_COM_ESPACO_FINAL:
                descricao += " "
            t = {
                "data": _data_com_ano(cel["data"], ini, fim),
                "valor": cel["valor"],
                "descricao": descricao,
                "doc": cel["doc"],
                "nome": None,
            }
            if cel["valor"] is None:
                t["incompleto"] = True
                t["motivo"] = "Valor não identificado"
            transacoes.append(t)
            detalhe_ok = True

        elif detalhe_ok and transacoes:
            detalhe = f'{cel["doc"]} {cel["hist"]}'.strip()
            if not detalhe or _junk(detalhe) or _is_saldo(detalhe):
                continue
            ultima = transacoes[-1]
            if ultima["nome"] is not None:
                ultima["nome"] = f'{ultima["nome"]} {detalhe}'.strip()
            elif detalhe.upper().startswith(PREFIXOS_NOME):
                ultima["nome"] = detalhe
            else:
                ultima["descricao"] = f'{ultima["descricao"]} {detalhe}'.strip()

    return transacoes


def info(pdf_path) -> dict:
    return _ler_cabecalho(pdf_path)


def _ler_cabecalho(pdf_path) -> dict:
    with pdfplumber.open(str(pdf_path)) as pdf:
        texto = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])

    cooperativa = RE_COOPERATIVA.search(texto)
    conta = RE_CONTA.search(texto)
    periodo = RE_PERIODO.search(texto)
    if not periodo:
        raise ValueError(
            "Periodo não encontrado no PDF — isto é um extrato Credibom/Sicoob?"
        )
    d1, d2 = (date(int(p[6:]), int(p[3:5]), int(p[:2])) for p in periodo.groups())
    return {
        "banco_nome": BANCO_NOME,
        "banco_id": BANCO_ID,
        "agencia": cooperativa.group(1) if cooperativa else "",
        "conta": conta.group(1).replace(".", "") if conta else "",
        "moeda": "BRL",
        "periodo_inicio": d1,
        "periodo_fim": d2,
        "saldo_final": _saldo_final(pdf_path, d2),
    }


def _saldo_final(pdf_path, dia_final):
    saldos = []
    for cel, tipo in _varrer(pdf_path):
        if tipo != "linha":
            continue
        junto = f'{cel["doc"]} {cel["hist"]}'.upper().strip()
        if cel["valor"] is not None and junto == "SALDO DO DIA":
            saldos.append((cel["data"], cel["valor"]))
    for dia, valor in saldos:
        if dia == dia_final.strftime("%d/%m"):
            return valor
    return saldos[0][1] if saldos else None


def validar(pdf_path) -> dict:
    """Confere o resultado contra os saldos impressos no próprio extrato.

    - total: saldo anterior + soma dos lançamentos == saldo do último dia
    - dias: para cada "SALDO DO DIA", saldo do dia anterior + movimento do dia
    Retorna {"ok": bool, "total_ok": bool, "divergencias": [...], "incompletos": n}.
    """
    cab = _ler_cabecalho(pdf_path)
    ini, fim = cab["periodo_inicio"], cab["periodo_fim"]
    saldo_ant, saldos_dia, mov = None, {}, {}
    dia_atual = None
    for cel, tipo in _varrer(pdf_path):
        if tipo != "linha":
            continue
        junto = f'{cel["doc"]} {cel["hist"]}'.upper().strip()
        if junto.startswith("SALDO ANTERIOR"):
            saldo_ant = cel["valor"]
        elif junto.startswith("SALDO DO DIA") and cel["valor"] is not None:
            saldos_dia[_data_com_ano(cel["data"], ini, fim)] = cel["valor"]
        elif junto.startswith("SALDO"):
            continue
        elif RE_DATA.match(cel["data"]) and cel["valor"] is not None:
            d = _data_com_ano(cel["data"], ini, fim)
            mov[d] = round(mov.get(d, 0) + cel["valor"], 2)
    trans = parse(pdf_path)
    inc = sum(1 for t in trans if t.get("incompleto"))
    div, anterior = [], saldo_ant
    for d in sorted(saldos_dia):
        if anterior is not None:
            esperado = round(anterior + mov.get(d, 0), 2)
            if abs(esperado - saldos_dia[d]) > 0.005:
                div.append({"dia": d, "esperado": esperado, "extrato": saldos_dia[d]})
        anterior = saldos_dia[d]
    total_ok = (saldo_ant is not None and cab["saldo_final"] is not None and
                abs(saldo_ant + sum(t["valor"] or 0 for t in trans) - cab["saldo_final"]) < 0.005)
    return {"ok": total_ok and not div and inc == 0, "total_ok": total_ok,
            "divergencias": div, "incompletos": inc}