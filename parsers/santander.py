"""Parser do extrato consolidado do Santander (Extrato Consolidado Inteligente PJ).

A seção "Conta Corrente > Movimentação" do PDF é uma tabela com as colunas
Data | Descrição | Nº Documento | Créditos | Débitos | Saldo. Cada transação
é um bloco de 1 ou 2 linhas de descrição: a primeira linha traz o documento
e o valor (a "âncora"), a segunda linha (se houver) é só continuação da
descrição. A data só é impressa na primeira transação de cada dia, e a
linha de saldo (0,00) só aparece no fim do dia.

Estratégia: percorre as linhas do PDF em ordem de leitura (atravessando as
quebras de página) entre "SALDO EM 30/06" e "SALDO EM 31/07"; cada linha com
valor na coluna Crédito/Débito abre uma transação nova e as linhas de
descrição sem valor são anexadas à transação anterior. O sinal do valor
vem da coluna (Débito tem o "-" no final: "70.000,00-").

O OFX gerado segue exatamente o formato do conversor usado hoje para o
Santander: sem indentação, FITID = CHECKNUM = AAAAMMDD + sequência do dia
(2 dígitos), DTPOSTED só com a data e LEDGERBAL com o saldo final do extrato.
"""

import re
import unicodedata
from datetime import date, datetime, timedelta

import pdfplumber

NOME = "Santander (extrato consolidado inteligente PJ)"
BANCO_ID = "033"
BANCO_ORG = "Banco Santander (Brasil) S.A."

RE_DATA = re.compile(r"^(\d{2})/(\d{2})$")
RE_VALOR = re.compile(r"^\d[\d.]*,\d{2}-?$")
RE_SALDO_EM = re.compile(r"^SALDO EM (\d{2})/(\d{2})\b")
RE_REF = re.compile(
    r"\b(janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|"
    r"outubro|novembro|dezembro)/(\d{4})\b", re.I)
RE_AGENCIA_CONTA = re.compile(
    r"Ag[eê]ncia\s+Conta\s+Corrente\s+(\d+)\s+(\d[\d.]*-\d)", re.S)

MESES = ["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]

# Colunas identificadas pela posição das palavras (x0 = início, x1 = fim).
X_DATA_MAX = 60          # coluna Data
X_DESC = (60, 290)       # coluna Descrição
X_DOC = (290, 350)       # coluna Nº Documento
X_CRED_MAX = 430         # valor com x1 até aqui = Crédito
X_DEB_MAX = 500          # valor com x1 entre 430 e 500 = Débito
                         # x1 > 500 = Saldo (ignorado)
TOL_LINHA = 2            # tolerância (pt) para agrupar palavras na mesma linha

JUNK = (
    "extrato consolidado inteligente",
    "data descrição",
    "créditos débitos",
    "extrato_pj",
    "balp_",
    "pagina:",
)


def _norm(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _num(t):
    """'70.000,00-' -> -70000.0 ; '11.384,91' -> 11384.91"""
    neg = t.endswith("-")
    v = float(t.rstrip("-").replace(".", "").replace(",", "."))
    return -v if neg else v


def _agrupar_linhas(words):
    """Agrupa palavras em linhas de texto pela coordenada Y (top)."""
    linhas, atual, topo = [], [], None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if topo is None or w["top"] - topo <= TOL_LINHA:
            if topo is None:
                topo = w["top"]
            atual.append(w)
        else:
            linhas.append(sorted(atual, key=lambda w: w["x0"]))
            atual, topo = [w], w["top"]
    if atual:
        linhas.append(sorted(atual, key=lambda w: w["x0"]))
    return linhas


def _classificar(linha):
    """Separa as palavras de uma linha por coluna."""
    dia = mes = doc = valor = None
    desc = []
    for w in linha:
        t = w["text"]
        if w["x0"] < X_DATA_MAX:
            m = RE_DATA.match(t)
            if m:
                dia, mes = int(m.group(1)), int(m.group(2))
            continue
        if X_DESC[0] <= w["x0"] < X_DESC[1]:
            desc.append(t)
            continue
        if X_DOC[0] <= w["x0"] < X_DOC[1]:
            doc = t
            continue
        if RE_VALOR.match(t) and w["x1"] <= X_DEB_MAX:
            valor = _num(t)
            # Débito vem com "-" no fim; Crédito não. Garante o sinal pela
            # coluna mesmo que o "-" tenha sido perdido na extração.
            if w["x1"] > X_CRED_MAX and valor > 0:
                valor = -valor
    return {"dia": dia, "mes": mes, "doc": doc, "valor": valor,
            "desc": " ".join(desc).strip()}


def _junk(texto):
    t = texto.lower()
    return any(t.startswith(p) for p in JUNK) or bool(
        re.fullmatch(r"[a-zç]+/\d{4}", t))


def _referencia(texto):
    m = RE_REF.search(_norm(texto))
    if not m:
        raise ValueError("Não encontrei o mês/ano de referência no PDF — "
                         "isto é um extrato consolidado do Santander?")
    return MESES.index(m.group(1).lower()) + 1, int(m.group(2))


def _ler_pdf(pdf_path):
    with pdfplumber.open(str(pdf_path)) as pdf:
        texto = "\n".join((p.extract_text() or "") for p in pdf.pages)
        paginas = [pdf.pages[i].extract_words() for i in range(len(pdf.pages))]
    return texto, paginas


def _extrair(pdf_path):
    """Devolve (transações, saldo_inicial_data, saldo_final_data, saldo_final)."""
    texto, paginas = _ler_pdf(pdf_path)
    mes_ref, ano = _referencia(texto)

    transacoes = []
    atual = None
    data_atual = None
    dentro = False
    ini = fim = None
    saldo_final = None

    for words in paginas:
        for linha in _agrupar_linhas(words):
            texto_linha = " ".join(w["text"] for w in linha)
            if _junk(texto_linha):
                continue
            cel = _classificar(linha)

            m = RE_SALDO_EM.match(cel["desc"])
            if m:
                d = date(ano, int(m.group(2)), int(m.group(1)))
                if not dentro:
                    dentro, ini = True, d
                    continue
                if dentro:
                    fim = d
                    saldos = [w["text"] for w in linha
                              if RE_VALOR.match(w["text"]) and w["x1"] > X_DEB_MAX]
                    saldo_final = _num(saldos[0]) if saldos else None
                    dentro = False
                    atual = None
                    continue
            if not dentro:
                continue

            if cel["dia"] is not None:
                data_atual = date(ano, cel["mes"], cel["dia"])

            if cel["valor"] is not None:        # âncora: nova transação
                incompleto = (data_atual is None)
                atual = {
                    "data": data_atual,
                    "valor": cel["valor"],
                    "partes": [cel["desc"]] if cel["desc"] else [],
                    "doc": "" if cel["doc"] in (None, "-") else cel["doc"],
                    "nome": None,
                }
                if incompleto:
                    atual["incompleto"] = True
                    atual["motivo"] = "Data não identificada"
                transacoes.append(atual)
            elif cel["dia"] is not None and cel["desc"] and not cel["desc"].upper().startswith("SALDO"):
                atual = {
                    "data": data_atual,
                    "valor": None,
                    "partes": [cel["desc"]],
                    "doc": "" if cel["doc"] in (None, "-") else cel["doc"],
                    "nome": None,
                    "incompleto": True,
                    "motivo": "Valor não identificado",
                }
                transacoes.append(atual)
            elif cel["desc"] and atual is not None:   # continuação
                atual["partes"].append(cel["desc"])

    if ini is None or not transacoes:
        raise ValueError("Nenhuma transação encontrada — isto é um extrato "
                         "consolidado do Santander?")
    for t in transacoes:
        t["descricao"] = " ".join(t.pop("partes")).strip()
    return transacoes, ini, fim, saldo_final


def parse(pdf_path) -> list:
    """Extrai as transações do extrato consolidado Santander.

    Retorna lista de dicionários: data, valor, descricao, doc, nome.
    """
    return _extrair(pdf_path)[0]


def info(pdf_path) -> dict:
    """Dados da conta e do período lidos do cabeçalho/resumo do extrato."""
    texto, _ = _ler_pdf(pdf_path)
    _, ini, fim, saldo_final = _extrair(pdf_path)

    m = RE_AGENCIA_CONTA.search(texto)
    agencia, conta = (m.group(1), m.group(2)) if m else ("", "")

    def _total(rotulo):
        mt = re.search(rotulo + r"\s+([\d.]+,\d{2})", texto)
        return _num(mt.group(1)) if mt else None

    return {
        "banco_id": BANCO_ID,
        "agencia": agencia,
        "conta": conta,
        "acctid": conta,            # o OFX do Santander mantém pontos e dígito
        "periodo_inicio": ini + timedelta(days=1),   # dia após o "SALDO EM"
        "periodo_fim": fim,
        "saldo_final": saldo_final,
        "total_creditos": _total(r"Total de Créditos"),
        "total_debitos": _total(r"Total de Débitos"),
    }


def validar(transacoes, dados) -> list:
    """Confere as somas com o Resumo do extrato; devolve lista de avisos."""
    avisos = []
    cred = round(sum(t["valor"] for t in transacoes if t["valor"] > 0), 2)
    deb = round(-sum(t["valor"] for t in transacoes if t["valor"] < 0), 2)
    if dados.get("total_creditos") is not None \
            and abs(cred - dados["total_creditos"]) > 0.005:
        avisos.append(f"Créditos: soma {cred:.2f} != resumo "
                      f"{dados['total_creditos']:.2f}")
    if dados.get("total_debitos") is not None \
            and abs(deb - dados["total_debitos"]) > 0.005:
        avisos.append(f"Débitos: soma {deb:.2f} != resumo "
                      f"{dados['total_debitos']:.2f}")
    return avisos


if __name__ == '__main__':
    import sys
    from ofx_writer import gerar_ofx

    if len(sys.argv) < 3:
        sys.exit('uso: python parser_santander.py extrato.pdf saida.ofx')
    trans = parse(sys.argv[1])
    dados = info(sys.argv[1])
    for aviso in validar(trans, dados):
        print('AVISO:', aviso)
    gerar_ofx(trans, dados, sys.argv[2])
    print(str(len(trans)) + ' transações -> ' + sys.argv[2])
