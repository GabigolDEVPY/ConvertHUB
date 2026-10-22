"""Parser do layout Credibom / Sicoob (extrato SISBR, conta corrente).

Baseado no extrato exportado do Internet Banking Sicoob, cuja tabela de
movimentação tem as colunas fixas: Data | Documento | Histórico | Valor.

Versão 4 — Documento textual (ex.: "CTBC TELEC")
-------------------------------------------------
Problema: em linhas como
    08/09 CTBC TELEC DÉB.CONV.TELECOMUNICAÇÕES R$ 219,41D
o Documento é textual e tem mais de uma palavra. A heurística antiga
(gap < 25 e x0 <= X_DOC_MAX) engolia também "DÉB.CONV.TELECOMUNICAÇÕES"
como parte do documento, e a descrição ficava vazia.

Correção: o corte Documento -> Histórico agora é feito pela posição REAL
da coluna Histórico, descoberta dinamicamente por página:

1. Calibração por linhas com documento numérico (ex.: "30/09 1201540
   PAGAMENTO PIX ..."): o x0 da palavra logo após o número é o início
   da coluna Histórico. Usamos a mediana desses valores na página.
2. Se a página não tiver nenhuma linha assim, usamos o x0 da palavra
   "Histórico" no cabeçalho da tabela.
3. Se ainda assim não houver referência, usamos o valor da última página
   calibrada; e, por último, a heurística antiga (gap/X_DOC_MAX).

Assim funciona tanto para documento numérico quanto textual (uma ou
várias palavras), sem depender de um X fixo.
"""

import re
import statistics
from datetime import date

import pdfplumber

NOME = "Sicoob (extrato SISBR)"

BANCO_NOME = "Banco Cooperativo do Brasil"
BANCO_ID = "756"

X_DATA_MAX = 85
X_DOC_MAX = 185
TOL_COLUNA = 3  # tolerância (pt) ao comparar x0 com o início da coluna Histórico

RE_DATA = re.compile(r"^\d{2}/\d{2}$")
RE_DOC_NUM = re.compile(r"^\d+$")
RE_VALOR = re.compile(r"R?\$?\s*([\d.]{1,12},\d{2})\s*([DC])\b")
RE_COOPERATIVA = re.compile(r"Cooperativa:\s*([\d.\-]+\d)")
RE_CONTA = re.compile(r"Conta:\s*([\d.\-]+\d)")
RE_PERIODO = re.compile(
    r"Periodo:\s*(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})"
)

RE_LINHA_FALLBACK = re.compile(
    r"^(?P<data>\d{2}/\d{2})\s+"
    r"(?P<doc>\S+(?:\s\S+)?)??\s*"
    r"(?P<hist>.*?)\s*"
    r"R?\$\s*(?P<valor>[\d.]{1,12},\d{2})\s*(?P<dc>[DC])\s*$"
)

JUNK = (
    "http",
    "histórico de movimenta",
    "sistema de cooperativas",
    "plataforma de serviços",
    "extrato de conta corrente",
    "cooperativa:",
    "conta:",
    "periodo:",
    "sicoob | internet banking",
    "data documento histórico",
    "resumo",
    "saldo em conta:",
    "cheque especial contratado:",
    "juros vencidos",
    "tarifas vencidas",
    "saldo disponível:",
    "saldo bloqueado",
    "saldo em conta capital:",
    "encargos vencidos",
    "encargos a vencer",
    "previsão iof:",
    "previsão encargos:",
    "previsão tarifas:",
    "outras informações",
    "vencimento de cheque",
    "taxa cheque especial:",
    "custo efetivo total",
    "informações",
    "sac:",
)

PREFIXOS_SALDO = ("SALDO DO DIA", "SALDO ANTERIOR", "SALDO BLOQUEADO")

PREFIXOS_NOME = (
    "PAGAMENTO PIX",
    "RECEBIMENTO PIX",
    "REM.:",
    "REM:",
    "FAV.:",
    "FAV:",
    "CASH BACK",
    "FAVORECIDO:",
    "ORIGEM:",
)

HISTORICOS_COM_ESPACO_FINAL = {
    "TARIFA RENOVAÇÃO LIMITE CONTA GARANTIDA",
}


def _agrupar_linhas(words, tol=3):
    linhas = []
    atual = []
    topo = None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if topo is None or w["top"] - topo <= tol:
            atual.append(w)
            topo = w["top"] if topo is None else topo
        else:
            linhas.append(atual)
            atual = [w]
            topo = w["top"]
    if atual:
        linhas.append(atual)
    return linhas


def _texto_linha(linha):
    return " ".join(w["text"] for w in sorted(linha, key=lambda w: w["x0"]))


def _calibrar_x_hist(linhas):
    """Descobre o x0 onde começa a coluna Histórico numa página.

    1) Linhas "DD/MM <doc numérico> <histórico...>": o x0 da palavra após
       o número é o início do Histórico (mediana das ocorrências).
    2) Senão, x0 da palavra "Histórico" no cabeçalho da tabela.
    """
    candidatos = []
    x_header = None
    for linha in linhas:
        lo = sorted(linha, key=lambda w: w["x0"])
        textos = [w["text"].lower() for w in lo]

        if "documento" in textos and "histórico" in textos:
            x_header = lo[textos.index("histórico")]["x0"]
            continue

        if len(lo) >= 3 and RE_DATA.match(lo[0]["text"]) and RE_DOC_NUM.match(lo[1]["text"]):
            prox = lo[2]
            if prox["text"] not in ("R$", "R$*", "$") and prox["x0"] > lo[1]["x1"]:
                candidatos.append(prox["x0"])

    if candidatos:
        return statistics.median(candidatos)
    return x_header


def _extrair_valor_dinamico(linha_ord):
    idx_rs = None
    for i, w in enumerate(linha_ord):
        if w["text"] in ("R$", "R$*", "$"):
            idx_rs = i
            break
    if idx_rs is None:
        texto = _texto_linha(linha_ord)
        m = RE_VALOR.search(texto)
        if not m:
            return None, None, None
        numero, dc = m.groups()
        valor = float(numero.replace(".", "").replace(",", "."))
        if dc == "D":
            valor = -valor
        return valor, None, None

    texto_valor = " ".join(w["text"] for w in linha_ord[idx_rs:])
    m = RE_VALOR.search(texto_valor)
    if not m:
        return None, idx_rs, None
    numero, dc = m.groups()
    valor = float(numero.replace(".", "").replace(",", "."))
    if dc == "D":
        valor = -valor
    return valor, idx_rs, linha_ord[idx_rs]["x0"]


def _x_inicio_historico_transacao(linha, x_hist=None):
    cel = _células_dynamic(linha, x_hist)
    if not cel["hist"]:
        return None
    primeira_palavra_hist = cel["hist"].split()[0]
    for w in sorted(linha, key=lambda w: w["x0"]):
        if w["text"] == primeira_palavra_hist and w["x0"] >= X_DATA_MAX:
            return w["x0"]
    return None


def _separar_doc_hist(resto, x_hist):
    """Separa os tokens (já sem data e sem valor) em Documento e Histórico."""
    if not resto:
        return "", ""

    # Caminho preferido: coluna Histórico conhecida -> corte exato.
    if x_hist is not None:
        corte = x_hist - TOL_COLUNA
        i = 0
        while i < len(resto) and resto[i]["x0"] < corte:
            i += 1
        doc = " ".join(w["text"] for w in resto[:i]).strip()
        hist = " ".join(w["text"] for w in resto[i:]).strip()
        return doc, hist

    # Fallback legado (sem calibração disponível).
    doc_tokens = [resto[0]]
    i = 1
    prev_x1 = resto[0]["x1"]
    while i < len(resto):
        gap = resto[i]["x0"] - prev_x1
        if resto[i]["x0"] <= X_DOC_MAX and gap < 25:
            doc_tokens.append(resto[i])
            prev_x1 = resto[i]["x1"]
            i += 1
        else:
            break
    doc = " ".join(w["text"] for w in doc_tokens).strip()
    hist = " ".join(w["text"] for w in resto[i:]).strip()
    return doc, hist


def _células_dynamic(linha, x_hist=None):
    """Separa uma linha nas células Data / Documento / Histórico / Valor.

    - Data: localizada pelo token "DD/MM" (conteúdo, não posição).
    - Valor: localizado pelo token "R$" (conteúdo, não posição).
    - Documento/Histórico: corte pela coluna Histórico calibrada (x_hist),
      o que suporta documento numérico OU textual com várias palavras.
    """
    linha_ord = sorted(linha, key=lambda w: w["x0"])

    idx_fim_data = 0
    data_tokens = []
    for w in linha_ord:
        if RE_DATA.match(w["text"]):
            data_tokens.append(w)
            idx_fim_data += 1
        else:
            break
    data = " ".join(w["text"] for w in data_tokens).strip()

    if not data and linha_ord:
        texto_linha = _texto_linha(linha_ord)
        m = re.match(r"^(\d{2}/\d{2})(?![/\d])", texto_linha)
        if m:
            data = m.group(1)
            linha_ord_resto = [w for w in linha_ord if w["x0"] >= X_DATA_MAX]
        else:
            linha_ord_resto = linha_ord
    else:
        linha_ord_resto = linha_ord[idx_fim_data:]

    valor, idx_rs, x0_valor = _extrair_valor_dinamico(linha_ord_resto)

    if idx_rs is not None:
        resto = linha_ord_resto[:idx_rs]
    else:
        resto = linha_ord_resto

    doc, hist = _separar_doc_hist(resto, x_hist)

    return {
        "data": data,
        "doc": doc,
        "hist": hist,
        "valor": valor,
        "x_min": min(w["x0"] for w in linha) if linha else 0,
    }


def _junk(texto: str) -> bool:
    t = texto.lower().strip()
    return any(t.startswith(j) for j in JUNK)


def _is_saldo(*partes: str) -> bool:
    junto = " ".join(p for p in partes if p).upper().strip()
    return any(junto.startswith(p) for p in PREFIXOS_SALDO)


def parse(pdf_path) -> list:
    cab = _ler_cabecalho(pdf_path)
    ano_base = cab["periodo_inicio"].year
    mes_base = cab["periodo_inicio"].month

    transacoes = []
    linhas_nao_resolvidas = []
    detalhe_ok = False
    x_hist_anterior = None

    with pdfplumber.open(str(pdf_path)) as pdf:
        for num_pagina, pagina in enumerate(pdf.pages, start=1):
            linhas = _agrupar_linhas(pagina.extract_words())
            x_hist = _calibrar_x_hist(linhas)
            if x_hist is None:
                x_hist = x_hist_anterior
            else:
                x_hist_anterior = x_hist

            for linha in linhas:
                cel = _células_dynamic(linha, x_hist)

                if RE_DATA.match(cel["data"]):
                    if _is_saldo(cel["doc"], cel["hist"]):
                        detalhe_ok = False
                        continue

                    if cel["valor"] is not None:
                        descricao = cel["hist"]
                        if descricao in HISTORICOS_COM_ESPACO_FINAL:
                            descricao += " "
                        transacoes.append({
                            "data": _data_com_ano(cel["data"], ano_base, mes_base),
                            "valor": cel["valor"],
                            "descricao": descricao,
                            "doc": cel["doc"],
                            "nome": None,
                            "_x_hist": _x_inicio_historico_transacao(linha, x_hist),
                        })
                        detalhe_ok = True
                    elif cel["hist"] and not _junk(cel["hist"]):
                        texto_bruto = _texto_linha(linha)
                        linhas_nao_resolvidas.append(
                            (texto_bruto, num_pagina, cel, ano_base, mes_base)
                        )
                        transacoes.append({
                            "data": _data_com_ano(cel["data"], ano_base, mes_base),
                            "valor": None,
                            "descricao": cel["hist"],
                            "doc": cel["doc"],
                            "nome": None,
                            "incompleto": True,
                            "motivo": "Valor não identificado",
                            "_idx_fallback": len(linhas_nao_resolvidas) - 1,
                        })
                        detalhe_ok = True

                elif detalhe_ok and cel["x_min"] >= X_DATA_MAX:
                    # Linha de detalhe (sem data): texto corrido único,
                    # então juntamos doc+hist preservando a ordem X.
                    if cel["doc"] and cel["hist"]:
                        detalhe = f'{cel["doc"]} {cel["hist"]}'.strip()
                    else:
                        detalhe = cel["hist"] or cel["doc"]
                    if not detalhe or _junk(detalhe) or _is_saldo(cel["doc"], cel["hist"]):
                        continue
                    if not transacoes:
                        continue
                    ultima = transacoes[-1]
                    if ultima["nome"] is not None:
                        ultima["nome"] = f'{ultima["nome"]} {detalhe}'.strip()
                    elif detalhe.upper().startswith(PREFIXOS_NOME):
                        ultima["nome"] = detalhe
                    else:
                        ultima["descricao"] = f'{ultima["descricao"]} {detalhe}'.strip()

    if linhas_nao_resolvidas:
        _aplicar_fallback_texto(pdf_path, transacoes, linhas_nao_resolvidas)

    for t in transacoes:
        t.pop("_x_hist", None)

    return transacoes


def _aplicar_fallback_texto(pdf_path, transacoes, linhas_nao_resolvidas):
    with pdfplumber.open(str(pdf_path)) as pdf:
        textos_por_pagina = {}
        for i, pagina in enumerate(pdf.pages, start=1):
            textos_por_pagina[i] = pagina.extract_text() or ""

    for texto_bruto, num_pagina, cel, ano_base, mes_base in linhas_nao_resolvidas:
        texto_pagina = textos_por_pagina.get(num_pagina, "")
        padrao = re.compile(
            re.escape(cel["data"]) + r".*?R?\$\s*([\d.]{1,12},\d{2})\s*([DC])"
        )
        m = padrao.search(texto_pagina)
        if not m:
            continue
        numero, dc = m.groups()
        valor = float(numero.replace(".", "").replace(",", "."))
        if dc == "D":
            valor = -valor
        for t in transacoes:
            if (
                t.get("incompleto")
                and t["data"] == _data_com_ano(cel["data"], ano_base, mes_base)
                and t["descricao"] == cel["hist"]
                and t["doc"] == cel["doc"]
            ):
                t["valor"] = valor
                t.pop("incompleto", None)
                t.pop("motivo", None)
                break


def info(pdf_path) -> dict:
    return _ler_cabecalho(pdf_path)


def _ler_cabecalho(pdf_path) -> dict:
    with pdfplumber.open(str(pdf_path)) as pdf:
        texto = "\n".join(
            (p.extract_text() or "") for p in pdf.pages[:2]
        )

    cooperativa = RE_COOPERATIVA.search(texto)
    conta = RE_CONTA.search(texto)
    periodo = RE_PERIODO.search(texto)
    if not periodo:
        raise ValueError(
            "Periodo não encontrado no PDF — isto é um extrato Credibom/Sicoob?"
        )
    d1, d2 = (
        date(int(p[6:]), int(p[3:5]), int(p[:2])) for p in periodo.groups()
    )
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
    x_hist_anterior = None
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            linhas = _agrupar_linhas(pagina.extract_words())
            x_hist = _calibrar_x_hist(linhas) or x_hist_anterior
            x_hist_anterior = x_hist
            for linha in linhas:
                cel = _células_dynamic(linha, x_hist)
                junto = f'{cel["doc"]} {cel["hist"]}'.upper().strip()
                if cel["valor"] is not None and junto == "SALDO DO DIA":
                    saldos.append((cel["data"], cel["valor"]))
    for dia, valor in saldos:
        if dia == dia_final.strftime("%d/%m"):
            return valor
    return saldos[0][1] if saldos else None


def _data_com_ano(dd_mm: str, ano_base: int, mes_base: int) -> date:
    dia, mes = int(dd_mm[:2]), int(dd_mm[3:5])
    ano = ano_base + 1 if mes < mes_base else ano_base
    return date(ano, mes, dia)