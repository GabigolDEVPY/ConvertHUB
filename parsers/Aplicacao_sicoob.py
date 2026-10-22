"""Parser do layout Sicoob para Extrato de Apropriação Diária (RDC Flexível
e aplicações similares).

Baseado no extrato "EXTRATO DE APROPRIAÇÃO DIÁRIA" exportado do Internet
Banking Sicoob, cuja tabela de movimentação tem as colunas fixas:
Data | Histórico | Valor.

Diferente do extrato de conta corrente, aqui não há coluna "Documento" nem
linhas de detalhe (nome do favorecido) abaixo da transação — cada linha de
movimentação é autocontida. A data já vem no formato DD/MM/AAAA (ano cheio),
então não é necessário inferir o ano a partir do período.

Versão 2 — robusta a variações de calibração entre extratos
-------------------------------------------------------------
A versão anterior separava Data/Histórico/Valor por FAIXAS X FIXAS
(X_DATA/X_HIST/X_VALOR), assumindo que a coluna Valor sempre começa em
x0>=500. Isso quebra entre extratos emitidos por cooperativas/contas
diferentes: por exemplo, no arquivo "APLI_7.pdf" a coluna Valor começa em
x0≈457 (abaixo de 500), então o número "123,50C" caía dentro da faixa do
Histórico em vez da faixa do Valor, e a transação era descartada (regex de
valor nunca casava).

A nova estratégia não usa nenhuma faixa X: cada linha da tabela é extraída
do texto já ordenado (`page.extract_text()`), que o pdfplumber monta
respeitando a ordem visual esquerda->direita independente de qualquer
calibração de coluna. Uma única regex por linha extrai Data, Histórico e
Valor pelo CONTEÚDO (não pela posição):

    DD/MM/AAAA <histórico...> <valor>,<centavos><D|C>

Isso funciona igualmente bem nos dois layouts observados (com a coluna
Valor começando em x0≈399, 457 ou 536 — ou qualquer outra posição).
"""

import re
from datetime import date

import pdfplumber

NOME = "Sicoob (extrato de apropriação diária)"

# Regex de uma linha de transação completa, extraída de extract_text():
#   "01/07/2026 APROPRIAÇÃO DE CM 0,48C"
#   "30/06/2026 SALDO ANTERIOR 1.010,08C"
# Histórico é "guloso-mínimo" (.+?) e o valor é ancorado ao FINAL da linha,
# então funciona independente de quantas palavras tem o histórico.
RE_LINHA = re.compile(
    r"^(?P<data>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<hist>.+?)\s+"
    r"(?P<valor>[\d.]+,\d{2})\s*(?P<dc>[DC])\s*$"
)

# Históricos que representam saldo (abertura) e não são transações.
HISTORICOS_SALDO = ("SALDO ANTERIOR", "SALDO DO DIA")

# Linhas de rodapé/resumo que não fazem parte da tabela de movimentação
# (comparação sempre em minúsculas / case-insensitive).
JUNK = (
    "resumo",
    "saldo bruto",
    "saldo disponível",
    "ouvidoria",
    "http",
    "sicoob | internet banking",
    "data histórico",
)


def _junk(texto: str) -> bool:
    t = texto.lower().strip()
    return any(t.startswith(j) for j in JUNK)


def parse(pdf_path) -> list:
    """Extrai as transações do extrato de apropriação diária Sicoob.

    Retorna lista de dicionários: data, valor, descricao.
    """
    transacoes = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            texto = pagina.extract_text() or ""
            for linha_texto in texto.split("\n"):
                linha_texto = linha_texto.strip()
                if not linha_texto or _junk(linha_texto):
                    continue

                m = RE_LINHA.match(linha_texto)
                if not m:
                    continue

                hist = m.group("hist").strip()
                if hist.upper() in HISTORICOS_SALDO:
                    continue

                numero, dc = m.group("valor"), m.group("dc")
                valor = float(numero.replace(".", "").replace(",", "."))
                if dc == "D":
                    valor = -valor

                transacoes.append({
                    "data": _data(m.group("data")),
                    "valor": valor,
                    "descricao": hist,
                })
    return transacoes


def _data(dd_mm_aaaa: str) -> date:
    dia, mes, ano = dd_mm_aaaa.split("/")
    return date(int(ano), int(mes), int(dia))


if __name__ == "__main__":
    import sys

    caminho = sys.argv[1] if len(sys.argv) > 1 else None
    if not caminho:
        print("Uso: python parser_aplicacao_diaria_sicoob.py <caminho_pdf>")
        raise SystemExit(1)

    for t in parse(caminho):
        print(t["data"], "|", t["descricao"], "|", t["valor"])