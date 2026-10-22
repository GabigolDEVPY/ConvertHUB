"""Parser do layout Sicredi (extrato conta corrente).

Este parser extrai transações de extratos do Sicredi, lidando com 
sobreposição de camadas de texto (deduplicação) e extraindo as colunas 
Data, Descrição, Documento, Valor e Saldo.
"""

import re
from datetime import datetime

import pdfplumber

NOME = "Sicredi"

BANCO_NOME = "Banco Cooperativo Sicredi"
BANCO_ID = "748"

# Limites X (em pontos) das colunas no PDF do Sicredi
X_DATA = (0, 90)
X_DESC = (90, 320)
X_DOC = (320, 420)
X_VALOR = (420, 500)
X_SALDO = (500, 10_000)

RE_DATA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
# O Sicredi usa sinal de negativo explícito e ponto como separador de milhar
RE_VALOR = re.compile(r"^(-?(?:\d{1,3}(?:\.\d{3})*|\d+),\d{2})$")

RE_ASSOCIADO = re.compile(r"Associado:\s*(.+?)(?:\s{2,}|Cooperativa:)", re.IGNORECASE)
RE_COOP = re.compile(r"Cooperativa:\s*([\d\-]+)")
RE_CONTA = re.compile(r"Conta:\s*([\d\-]+)")
RE_PERIODO = re.compile(
    r"Per[íi]odo de\s*(\d{2}/\d{2}/\d{4})\s*a\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE
)

JUNK = (
    "sicredi",
    "extrato",
    "período",
    "periodo",
    "associado",
    "cooperativa",
    "conta",
    "data",
    "descrição",
    "documento",
    "valor",
    "saldo",
    "lançamentos futuros",
    "fone",
    "sac",
    "ouvidoria",
)


def _agrupar_linhas(words):
    """Agrupa palavras em linhas pela coordenada Y (top)."""
    linhas = []
    atual = []
    topo = None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if topo is None or w["top"] - topo <= 3:
            atual.append(w)
            topo = w["top"] if topo is None else topo
        else:
            linhas.append(atual)
            atual = [w]
            topo = w["top"]
    if atual:
        linhas.append(atual)
    return linhas


def _faixa(linha, x0, x1):
    """Extrai texto de uma faixa X, deduplicando camadas sobrepostas do PDF."""
    words = sorted([w for w in linha if x0 <= w["x0"] < x1], key=lambda w: w["x0"])
    texts = [w["text"] for w in words]
    
    # Deduplicação robusta para camadas de PDF sobrepostas (ex: texto duplicado)
    if len(texts) > 1:
        for i in range(1, len(texts) // 2 + 1):
            if len(texts) % i == 0:
                chunk = texts[:i]
                if all(texts[j*i:(j+1)*i] == chunk for j in range(len(texts) // i)):
                    texts = chunk
                    break
                    
    return " ".join(texts).strip()


def _celulas(linha):
    return {
        "data": _faixa(linha, *X_DATA),
        "desc": _faixa(linha, *X_DESC),
        "doc": _faixa(linha, *X_DOC),
        "valor": _faixa(linha, *X_VALOR),
        "saldo": _faixa(linha, *X_SALDO),
    }


def _junk(texto: str) -> bool:
    t = texto.lower().strip()
    return any(t.startswith(j) for j in JUNK) or not t


def parse(pdf_path) -> list:
    """Extrai as transações do extrato Sicredi.

    Retorna lista de dicionários: data, valor, descricao, doc, nome.
    """
    transacoes = []
    parou = False
    
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            if parou:
                break
            for linha in _agrupar_linhas(pagina.extract_words()):
                linha_texto = " ".join(w["text"] for w in linha).upper()
                
                # Encerra a leitura ao atingir o bloco de Lançamentos Futuros
                if "LANÇAMENTOS FUTUROS" in linha_texto:
                    parou = True
                    break
                
                cel = _celulas(linha)
                
                # Ignora linhas de saldo (ex: SALDO ANTERIOR)
                if "SALDO" in cel["desc"].upper():
                    continue
                
                # Linha de transação: tem data no início
                if RE_DATA.match(cel["data"]):
                    data_str = cel["data"]
                    try:
                        data_obj = datetime.strptime(data_str, "%d/%m/%Y").date()
                    except Exception:
                        data_obj = None
                    
                    if RE_VALOR.match(cel["valor"]):
                        valor_str = RE_VALOR.match(cel["valor"]).group(1)
                        valor = float(valor_str.replace(".", "").replace(",", "."))
                        
                        descricao = cel["desc"]
                        doc = cel["doc"] if not _junk(cel["doc"]) else None
                        
                        transacoes.append({
                            "data": data_obj,
                            "valor": valor,
                            "descricao": descricao.strip(),
                            "doc": doc,
                            "nome": None, # No Sicredi, o nome do favorecido já vem embutido na descrição
                        })
                    elif cel["desc"] and not _junk(cel["desc"]):
                        transacoes.append({
                            "data": data_obj,
                            "valor": None,
                            "descricao": cel["desc"].strip(),
                            "doc": cel["doc"] if not _junk(cel["doc"]) else None,
                            "nome": None,
                            "incompleto": True,
                            "motivo": "Valor não identificado",
                        })
                    
    return transacoes


def info(pdf_path) -> dict:
    """Dados da conta e do período, usados no cabeçalho do OFX."""
    return _ler_cabecalho(pdf_path)


def _ler_cabecalho(pdf_path) -> dict:
    with pdfplumber.open(str(pdf_path)) as pdf:
        texto = "\n".join(
            (p.extract_text() or "") for p in pdf.pages[:2]
        )
        
    assoc = RE_ASSOCIADO.search(texto)
    coop = RE_COOP.search(texto)
    conta = RE_CONTA.search(texto)
    periodo = RE_PERIODO.search(texto)
    
    if not periodo:
        raise ValueError("Período não encontrado no PDF — isto é um extrato Sicredi?")
        
    d1 = datetime.strptime(periodo.group(1), "%d/%m/%Y").date()
    d2 = datetime.strptime(periodo.group(2), "%d/%m/%Y").date()
    
    return {
        "banco_nome": BANCO_NOME,
        "banco_id": BANCO_ID,
        "agencia": coop.group(1) if coop else "",
        "conta": conta.group(1).replace(".", "").replace("-", "") if conta else "",
        "moeda": "BRL",
        "periodo_inicio": d1,
        "periodo_fim": d2,
        "saldo_final": _saldo_final(pdf_path),
    }


def _saldo_final(pdf_path):
    """Extrai o último saldo da tabela de movimentação."""
    ultimo_saldo = None
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            for linha in _agrupar_linhas(pagina.extract_words()):
                linha_texto = " ".join(w["text"] for w in linha).upper()
                if "LANÇAMENTOS FUTUROS" in linha_texto:
                    return ultimo_saldo
                
                cel = _celulas(linha)
                if RE_DATA.match(cel["data"]) and RE_VALOR.match(cel["saldo"]):
                    saldo_str = RE_VALOR.match(cel["saldo"]).group(1)
                    ultimo_saldo = float(saldo_str.replace(".", "").replace(",", "."))
    return ultimo_saldo


if __name__ == "__main__":
    import sys

    caminho = sys.argv[1] if len(sys.argv) > 1 else "SICREDI CC AGOSTO 26.pdf"

    dados = info(caminho)
    print("=== Cabeçalho ===")
    for k, v in dados.items():
        print(f"{k}: {v}")

    print("\n=== Transações ===")
    txs = parse(caminho)
    for t in txs:
        print(t)
    print(f"\nTotal: {len(txs)} transações")