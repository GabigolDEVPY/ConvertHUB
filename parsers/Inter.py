"""
Parser para extrato Banco Inter (Internet Banking - PDF).

Baseado no extrato exportado do Internet Banking do Banco Inter, em que a
movimentação vem agrupada por dia:

    1 de Junho de 2026 Saldo do dia: R$ 2.106,97
    Pix recebido: "Cp :60701190-TUNA PAGAMENTOS LTDA"      R$ 423,03   R$ 594,51
                                                          Valor   Saldo por transação

Colunas da movimentação: descrição | Valor | Saldo por transação.
A separação das colunas usa a distância X entre as palavras no PDF
(vazios grandes = virada de coluna), dispensando limites fixos de pontos.

Gera saída em formato OFX 1.02 (SGML), no mesmo padrão do parser
Credibom / Sicoob.
"""

import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

import pdfplumber

# ── Nome e banco ────────────────────────────────────────────────────────────
NOME = "Banco Inter (extrato Internet Banking)"
BANCO_NOME = "Banco Intermedium S/A"
BANCO_ID = "077"

# ── Parâmetros de layout (pontos) ───────────────────────────────────────────
TOL_LINHA = 3.0     # variação de Y aceita para agrupar palavras na mesma linha
GAP_COLUNA = 12.0   # vazio mínimo em X para separar colunas
GAP_PALAVRA = 1.0   # vazio mínimo em X para inserir espaço entre palavras

# ── Regex ───────────────────────────────────────────────────────────────────
# "1 de Junho de 2026 Saldo do dia: R$ 2.106,97"
RE_GRUPO_DIA = re.compile(
    r"(\d{1,2})\s+de\s+([A-Za-zÀ-ÿ]+)\s+de\s+(\d{4})\s+Saldo do dia:\s*"
    r"(-?)\s*R\$\s*([\d.]+,\d{2})",
    re.IGNORECASE,
)
RE_VALOR = re.compile(r"(-?)\s*R\$\s*(-?)\s*([\d.]+,\d{2})")
RE_ASPAS = re.compile(r'"([^"]+)"')
RE_CODIGO_INICIAL = re.compile(
    r"^(?:Cp[\s.]*:?\s*)?[\d\s.\-]+(?=[A-Za-zÀ-ÿ])", re.IGNORECASE
)
RE_CNPJ = re.compile(r"CNPJ:\s*([\d./\-]+)")
RE_AGENCIA = re.compile(r"Ag[êe]ncia:\s*([\d.\-]+)")
RE_CONTA = re.compile(r"Conta:\s*([\d.\-]+)")
RE_PERIODO = re.compile(r"Per[íi]odo:\s*(\d{2}/\d{2}/\d{4})\s*a\s*(\d{2}/\d{2}/\d{4})")

MESES = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11,
    "dezembro": 12,
}

# Cabeçalho/rodapé que não é movimentação.
JUNK_PREFIXO = (
    "fale com a gente",
    "sac:",
    "ouvidoria:",
    "deficiência",
    "deficiencia",
    "solicitado em",
    "saldo total",
    "saldo disponível",
    "saldo disponivel",
    "saldo bloqueado",
    "saldo do dia",
    "cnpj:",
    "instituição:",
    "instituicao:",
    "período:",
    "periodo:",
    "valor",
    "saldo por transação",
    "saldo por transacao",
    "página",
    "pagina",
)
JUNK_EXATO = {"", "inter"}


# ── Auxiliares de texto ─────────────────────────────────────────────────────

def _sem_acento(txt: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", txt)
        if unicodedata.category(c) != "Mn"
    )


def _para_float(numero: str) -> float:
    return float(numero.replace(".", "").replace(",", "."))


def _valor(texto: str):
    """Extrai valor monetário; aceita 'R$ 423,03' e '-R$ 2.000,00'."""
    m = RE_VALOR.search(texto)
    if not m:
        return None
    sinal = -1 if (m.group(1) == "-" or m.group(2) == "-") else 1
    return sinal * _para_float(m.group(3))


def _junk(texto: str) -> bool:
    t = _sem_acento(texto).lower().strip()
    return t in {_sem_acento(j).lower() for j in JUNK_EXATO} or any(
        _sem_acento(texto).lower().startswith(j) for j in JUNK_PREFIXO
    )


# ── Auxiliares de linha ─────────────────────────────────────────────────────

def _agrupar_linhas(words, tol=TOL_LINHA):
    """Agrupa palavras em linhas pela coordenada Y (top)."""
    linhas, atual, topo = [], [], None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if topo is None or abs(w["top"] - topo) <= tol:
            atual.append(w)
            topo = w["top"] if topo is None else topo
        else:
            linhas.append(atual)
            atual, topo = [w], w["top"]
    if atual:
        linhas.append(atual)
    return linhas


def _quebrar_em_blocos(linha, gap=GAP_COLUNA):
    """Quebra uma linha em blocos (colunas) usando os vazios em X."""
    blocos, atual, x_fim = [], [], None
    for w in sorted(linha, key=lambda w: w["x0"]):
        if x_fim is not None and w["x0"] - x_fim > gap:
            blocos.append(atual)
            atual = []
        atual.append(w)
        x_fim = w["x1"]
    if atual:
        blocos.append(atual)
    return blocos


def _texto(bloco) -> str:
    """Reconstrói o texto de um bloco respeitando o espaçamento original."""
    partes, x_fim = [], None
    for w in sorted(bloco, key=lambda w: w["x0"]):
        if x_fim is not None and w["x0"] - x_fim > GAP_PALAVRA:
            partes.append(" ")
        partes.append(w["text"])
        x_fim = w["x1"]
    return "".join(partes).strip()


# ── Nome do favorecido/origem ───────────────────────────────────────────────

def _normalizar_nome(texto: str) -> str:
    """'Cp :60701190-TUNA PAGAMENTOS LTDA' -> 'Tuna Pagamentos Ltda'."""
    t = RE_CODIGO_INICIAL.sub("", texto.strip()).strip()
    if not t:
        return None
    return " ".join(p[:1].upper() + p[1:].lower() for p in t.split(" ") if p)


def _extrair_nome(descricao: str):
    m = RE_ASPAS.search(descricao)
    return _normalizar_nome(m.group(1)) if m else None


# ── Extração ────────────────────────────────────────────────────────────────

def parse(pdf_path) -> list:
    """
    Extrai as transações do extrato Banco Inter.

    Retorna lista de dicionários:
        data, valor, descricao, doc, nome, saldo
    """
    transacoes = []
    data_atual = None

    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            for linha in _agrupar_linhas(pagina.extract_words()):
                blocos = _quebrar_em_blocos(linha)
                texto = " ".join(_texto(b) for b in blocos).strip()
                if not texto:
                    continue

                m_grupo = RE_GRUPO_DIA.search(texto)
                if m_grupo:
                    data_atual = _data_do_grupo(m_grupo)
                    continue

                if _junk(texto):
                    continue

                valores = []
                for i, bloco in enumerate(blocos):
                    v = _valor(_texto(bloco))
                    if v is not None:
                        valores.append((i, v))

                if data_atual and valores:
                    # Penúltimo valor = Valor da transação; último = saldo por transação.
                    if len(valores) >= 2:
                        idx, valor = valores[-2]
                        saldo_trn = valores[-1][1]
                    else:
                        idx, valor = valores[-1]
                        saldo_trn = None

                    descricao = " ".join(
                        t for t in (_texto(b) for b in blocos[:idx]) if t
                    ).strip()

                    transacoes.append({
                        "data": data_atual,
                        "valor": valor,
                        "descricao": descricao,
                        "doc": None,
                        "nome": _extrair_nome(descricao),
                        "saldo": saldo_trn,
                    })

                elif data_atual:
                    palavras_op = (
                        "pix ", "ted ", "doc ", "transfer", "pagamento", "compra",
                        "boleto", "tarifa", "saque", "deposito", "depósito", "estorno"
                    )
                    texto_lower = texto.lower()
                    if (not transacoes) or any(texto_lower.startswith(p) for p in palavras_op):
                        transacoes.append({
                            "data": data_atual,
                            "valor": None,
                            "descricao": texto,
                            "doc": None,
                            "nome": _extrair_nome(texto),
                            "saldo": None,
                            "incompleto": True,
                            "motivo": "Valor não identificado",
                        })
                    elif transacoes:
                        # Linha de continuação da descrição.
                        transacoes[-1]["descricao"] += " " + texto
                        if transacoes[-1]["nome"] is None:
                            transacoes[-1]["nome"] = _extrair_nome(
                                transacoes[-1]["descricao"]
                            )

    return transacoes


def info(pdf_path) -> dict:
    """Dados da conta e do período, usados no cabeçalho do OFX."""
    return _ler_cabecalho(pdf_path)


def _data_do_grupo(m) -> date:
    dia, nome_mes, ano = int(m.group(1)), m.group(2), int(m.group(3))
    mes = MESES[_sem_acento(nome_mes).lower()]
    return date(ano, mes, dia)


def _data_br(txt: str) -> date:
    d, m, a = txt.split("/")
    return date(int(a), int(m), int(d))


def _ler_cabecalho(pdf_path) -> dict:
    with pdfplumber.open(str(pdf_path)) as pdf:
        texto = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])

    periodo = RE_PERIODO.search(texto)
    if not periodo:
        raise ValueError(
            "Período não encontrado no PDF — isto é um extrato do Banco Inter?"
        )

    d1, d2 = _data_br(periodo.group(1)), _data_br(periodo.group(2))
    agencia = RE_AGENCIA.search(texto)
    conta = RE_CONTA.search(texto)
    cnpj = RE_CNPJ.search(texto)

    saldos = _saldos_do_dia(pdf_path)
    saldo_final = None
    for dia, valor in saldos:
        if dia == d2:
            saldo_final = valor
    if saldo_final is None and saldos:
        saldo_final = saldos[-1][1]

    return {
        "banco_nome": BANCO_NOME,
        "banco_id": BANCO_ID,
        "agencia": agencia.group(1) if agencia else "",
        "conta": conta.group(1).replace(".", "").replace("-", "") if conta else "",
        "cnpj": cnpj.group(1) if cnpj else "",
        "titular": _titular(texto),
        "moeda": "BRL",
        "periodo_inicio": d1,
        "periodo_fim": d2,
        "saldo_final": saldo_final,
    }


def _titular(texto: str) -> str:
    linhas = [l.strip() for l in texto.splitlines()]
    for i, l in enumerate(linhas):
        if RE_CNPJ.search(l):
            for j in range(i - 1, -1, -1):
                if linhas[j]:
                    return linhas[j]
    return ""


def _saldos_do_dia(pdf_path):
    """Lista de (data, saldo) extraída dos cabeçalhos de dia do extrato."""
    saldos = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            for linha in _agrupar_linhas(pagina.extract_words()):
                texto = " ".join(_texto(b) for b in _quebrar_em_blocos(linha)).strip()
                m = RE_GRUPO_DIA.search(texto)
                if m:
                    sinal = -1 if m.group(4) == "-" else 1
                    saldos.append((_data_do_grupo(m), sinal * _para_float(m.group(5))))
    return saldos


if __name__ == '__main__':
    import sys
    from ofx_writer import gerar_ofx

    if len(sys.argv) < 2:
        print('Uso: python ' + sys.argv[0] + ' extrato.pdf [saida.ofx]')
        sys.exit(1)
    pdf_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else Path(pdf_path).with_suffix('.ofx')
    txs = parse(pdf_path)
    inf = info(pdf_path)
    gerar_ofx(txs, inf, out_path)
    print('OFX gerado:', out_path)
