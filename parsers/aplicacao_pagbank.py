"""Parser do layout PagBank/BancoSeguro para "EXTRATO DAS APLICAÇÕES EM
TÍTULOS DE RENDA FIXA" (CDB com liquidação financeira em conta).

A tabela de movimentação tem 6 colunas:
    Data | Histórico | Movto Bruto | Movto Líquido | Saldo Bruto | Saldo Líquido

Mesma estratégia do parser Sicoob v2: nada de faixas X fixas. Cada linha é
lida do texto já ordenado (`page.extract_text()`) e uma única regex extrai
os campos pelo CONTEÚDO:

    DD/MM/AAAA <histórico...> <bruto> <líquido> <saldo bruto> <saldo líquido>

Particularidades deste extrato:
- Valores já vêm com sinal ("-42.004,00" no Resgate), sem D/C.
- O cabeçalho da tabela se repete em cada página e a primeira página tem
  o título "Movimentação em Papéis de Emissão Própria" — ambos ignorados
  porque não casam com a regex (não começam com data + 4 números).
- "Saldo Inicial" e "Saldo Final" são saldos, não transações.
- "Bloqueio Renda Fixa" é um bloqueio da posição recém-aplicada (repete o
  valor da "Aplicacao"), não é movimentação de caixa — ignorado por padrão.
- A tabela "Resumo das Aplicações" no fim não começa com data, então
  também não casa.
- "valor" = Movto BRUTO (rendimento antes de IR/IOF). O líquido, e os
  saldos, vão em campos extras para conciliação.
"""

import re
from datetime import date

import pdfplumber

NOME = "PagBank/BancoSeguro (extrato de aplicações renda fixa)"

_NUM = r"-?[\d.]+,\d{2}"

RE_LINHA = re.compile(
    r"^(?P<data>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<hist>.+?)\s+"
    rf"(?P<bruto>{_NUM})\s+"
    rf"(?P<liquido>{_NUM})\s+"
    rf"(?P<saldo_bruto>{_NUM})\s+"
    rf"(?P<saldo_liquido>{_NUM})\s*$"
)

# Históricos que são saldo ou bloqueio, e não movimentação.
HISTORICOS_IGNORADOS = ("SALDO INICIAL", "SALDO FINAL", "BLOQUEIO RENDA FIXA")


def _num(txt: str) -> float:
    return float(txt.replace(".", "").replace(",", "."))


def _data(dd_mm_aaaa: str) -> date:
    dia, mes, ano = dd_mm_aaaa.split("/")
    return date(int(ano), int(mes), int(dia))


def parse(pdf_path, incluir_bloqueio: bool = False) -> list:
    """Extrai as movimentações do extrato de aplicações PagBank.

    Retorna lista de dicionários:
        data, valor (bruto), descricao,
        valor_liquido, saldo_bruto, saldo_liquido
    """
    ignorados = HISTORICOS_IGNORADOS
    if incluir_bloqueio:
        ignorados = tuple(h for h in ignorados if h != "BLOQUEIO RENDA FIXA")

    transacoes = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            texto = pagina.extract_text() or ""
            for linha in texto.split("\n"):
                m = RE_LINHA.match(linha.strip())
                if not m:
                    continue

                hist = m.group("hist").strip()
                if hist.upper() in ignorados:
                    continue

                transacoes.append({
                    "data": _data(m.group("data")),
                    "valor": _num(m.group("bruto")),
                    "descricao": hist,
                    "valor_liquido": _num(m.group("liquido")),
                    "saldo_bruto": _num(m.group("saldo_bruto")),
                    "saldo_liquido": _num(m.group("saldo_liquido")),
                })
    return transacoes


if __name__ == "__main__":
    import sys

    caminho = sys.argv[1] if len(sys.argv) > 1 else None
    if not caminho:
        print("Uso: python parser_aplicacao_pagbank.py <caminho_pdf>")
        raise SystemExit(1)

    for t in parse(caminho):
        print(t["data"], "|", t["descricao"], "|", t["valor"], "|", t["valor_liquido"])