"""Parser do extrato de conta corrente do Banco do Brasil (BB Empresa -
"Consultas - Extrato de conta corrente", PDF).

Layout (set/2026 em diante): a seção "Lançamentos" tem as colunas
    Dt. balancete | Dt. movimento | Ag. origem | Lote | Histórico |
    Documento | Valor R$ | Saldo
mas no PDF cada lançamento sai como UMA linha de texto e a segunda linha do
registro (Dt. movimento + hora e o favorecido/descrição extra) vem APÊNDICE
no fim da mesma linha, depois do Valor/Saldo:

  01/09/2026 0000 13105 Pix- Enviado 90.101 6.000,00 D 17.885,83 C 01/09 12:12 CONTABILIDADE SANTOS
  14/09/2026 0000 14397 Pix- Recebido 141.358.588.013.071 25.000,00 C 14/09 13:58 57545772000126 ETHEREAL PA
  28/09/2026 0000 13105 Pagamento de Boleto 92.801 2.163,00 D A E L SOLUCOES METROLOGICAS LT

Repare: a coluna Saldo só aparece no ÚLTIMO lançamento do dia (não há mais
"BB Rende Fácil" zerando a conta). A continuação também pode vir em linha
separada ou na página seguinte, depois do rodapé "05/10/2026, 11:49 Banco do
Brasil" + URL — o parser atravessa: rodapé é descartado e a linha sem data é
anexada à transação anterior.

Estratégia: linhas de texto entre "Saldo Anterior" e "S A L D O". Linha que
começa com dd/mm/aaaa abre transação; o 1º token monetário BR (###.###,##) é
o Valor (o seguinte, se houver, é o Saldo); antes = Histórico+Documento,
depois = continuação (com o "dd/mm hh:mm" descartado). Sinal vem do C/D.
Linhas sem data são anexadas à transação anterior.

Validação: o extrato do BB não traz totais, então a conferência é pelo
saldo — saldo anterior + movimento tem que bater com o saldo impresso em
cada linha que tem saldo e com o saldo final.

O OFX gerado segue o formato Santander/Itaú: sem indentação,
FITID = CHECKNUM = AAAAMMDD + sequência do dia (2 dígitos), DTPOSTED só com
a data e LEDGERBAL com o saldo final do extrato.
"""

import re
import unicodedata
from datetime import date, timedelta

import pdfplumber

NOME = "Banco do Brasil (extrato de conta corrente PJ)"
BANCO_ID = "001"
BANCO_ORG = "Banco do Brasil S.A."

HIST_COM_DOC = ("cheque",)

# dd/mm/aaaa no INÍCIO da linha. O lookahead exclui o rodapé "05/10/2026, 11:49"
RE_DATA = re.compile(r"^(\d{2})/(\d{2})/(\d{4})(?=\s|$)")
RE_DATA_TOK = re.compile(r"^\d{2}/\d{2}/\d{4}$")
# Valor monetário BR: grupos de milhar com ponto + vírgula + 2 decimais
RE_VALOR = re.compile(r"^(\d{1,3}(?:\.\d{3})*,\d{2})([CD])?$")
RE_VALOR_LETRA = re.compile(r"^([CD])(\d{1,3}(?:\.\d{3})*,\d{2})$")
RE_DOC = re.compile(r"^\d[\d.]{2,}$")          # documento: só dígitos/pontos
RE_AG_LOTE = re.compile(r"^\d{4,5}$")           # Ag. origem (4) + Lote (5)
RE_MMDD = re.compile(r"^\d{2}/\d{2}$")          # Dt. movimento (sem ano)
RE_HHMM = re.compile(r"^\d{2}:\d{2}$")
RE_MOV = re.compile(r"^\d{2}/\d{2}\s+\d{2}:\d{2}\s+")
RE_AGENCIA = re.compile(r"Ag[eê]ncia\s+(\d+-[\dXx])")
RE_CONTA = re.compile(r"Conta\s+corrente\s+(\d[\d.]*-[\dXx])")
RE_RODAPE = re.compile(r"^\d{2}/\d{2}/\d{4},")
RE_HORA = re.compile(r"\d{2}:\d{2}")


def _norm(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _num(t):
    """'8.000,00' -> 8000.0"""
    return float(t.replace(".", "").replace(",", "."))


def _valor_de(token):
    """'6.000,00' | '6.000,00D' | 'D6.000,00' -> (6000.0, 'D'|None) | None"""
    m = RE_VALOR.match(token)
    if m:
        return _num(m.group(1)), m.group(2)
    m = RE_VALOR_LETRA.match(token)
    if m:
        return _num(m.group(2)), m.group(1)
    return None


def _lixo(linha):
    """Cabeçalho/rodapé do PDF que não é lançamento."""
    baixo = linha.lower()
    if "http" in baixo:
        return True
    if RE_RODAPE.match(linha):                       # "05/10/2026, 11:49 Banco do Brasil"
        return True
    if "banco do brasil" in baixo and RE_HORA.search(linha):
        return True
    if re.match(r"^g\d{8,}", baixo):                 # "G333051144065624004 05/10/2026 ..."
        return True
    if re.match(r"^\d+\s*/\s*\d+$", linha):          # paginação "1/2"
        return True
    for pref in ("lançamentos", "visualizar pix", "dt. balancete", "cliente-"):
        if baixo.startswith(pref):
            return True
    return False


def _ler_pdf(pdf_path):
    with pdfplumber.open(str(pdf_path)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def _linhas(texto):
    for bruta in texto.split("\n"):
        linha = " ".join(bruta.split())
        if linha and not _lixo(linha):
            yield linha


def _classificar(linha):
    """Quebra uma linha de lançamento em data / histórico / doc / valor /
    saldo / continuação. Devolve None se a linha não começa com data
    (ou seja: é continuação da transação anterior)."""
    m = RE_DATA.match(linha)
    if not m:
        return None
    data = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    tok = linha.split()[1:]

    if tok and RE_DATA_TOK.match(tok[0]):    # Dt. movimento repetida na linha
        tok = tok[1:]

    i = 0                                    # Ag. origem + Lote
    while i < 2 and i < len(tok) and RE_AG_LOTE.match(tok[i]):
        i += 1

    j = next((k for k in range(i, len(tok)) if _valor_de(tok[k])), None)
    if j is None:                            # linha informativa, sem valor
        return {"data": data, "valor": None, "saldo": None, "doc": "",
                "desc": " ".join(tok[i:]).strip(), "cont": ""}

    valor, letra = _valor_de(tok[j])
    k = j + 1
    if letra is None and k < len(tok) and tok[k] in ("C", "D"):
        letra, k = tok[k], k + 1

    saldo = letra_s = None                   # Saldo: só no último lançamento do dia
    if k < len(tok):
        achou = _valor_de(tok[k])
        if achou:
            saldo, letra_s = achou
            k += 1
            if letra_s is None and k < len(tok) and tok[k] in ("C", "D"):
                letra_s, k = tok[k], k + 1

    corpo = tok[i:j]
    doc = ""
    if corpo and RE_DOC.match(corpo[-1]):
        doc = corpo.pop()

    resto = tok[k:]                          # continuação (favorecido, etc.)
    if resto and RE_MMDD.match(resto[0]):
        resto = resto[1:]
        if resto and RE_HHMM.match(resto[0]):
            resto = resto[1:]
    elif resto and RE_HHMM.match(resto[0]):
        resto = resto[1:]

    return {
        "data": data,
        "valor": -valor if letra == "D" else valor,
        "saldo": None if saldo is None else (-saldo if letra_s == "D" else saldo),
        "doc": doc,
        "desc": " ".join(corpo).strip(),
        "cont": " ".join(resto).strip(),
    }


def _limpar_descricao(partes):
    limpas, visto_rende = [], False
    for p in partes:
        if not p:
            continue
        n = _norm(p).lower().replace(" ", "")
        if n == "rendefacil" and visto_rende:
            continue
        if "rendefacil" in n or n.startswith("bbrende"):
            visto_rende = True
        limpas.append(p)
    out = []
    for p in limpas:
        if out and out[-1] == p:
            continue
        out.append(p)
    return " ".join(out).strip()


def _extrair(pdf_path):
    """(transações, data_saldo_anterior, saldo_inicial, data_saldo_final,
    saldo_final, checkpoints)."""
    texto = _ler_pdf(pdf_path)

    transacoes, checkpoints = [], []
    atual = None
    dentro = False
    ini = fim = saldo_ini = saldo_fim = None
    corrente = 0.0

    for linha in _linhas(texto):
        cel = _classificar(linha)

        if cel is None:                      # continuação (mesmo após quebra de página)
            if dentro and atual is not None:
                extra = RE_MOV.sub("", linha, count=1).strip()
                if RE_MMDD.match(extra):
                    extra = extra[5:].strip()
                n = _norm(extra).lower().replace(" ", "")
                if extra and not n.startswith("taragrupadas"):
                    atual["partes"].append(extra)
            continue

        desc_n = _norm(cel["desc"]).lower().replace(" ", "")

        if not dentro:                       # abertura: "... Saldo Anterior ..."
            if "saldoanterior" in desc_n:
                ini = cel["data"]
                saldo_ini = cel["saldo"] if cel["saldo"] is not None \
                    else (cel["valor"] or 0.0)
                corrente = saldo_ini
                dentro = True
            continue

        if (desc_n.startswith("saldo") and "anterior" not in desc_n
                and not cel["doc"]):         # fechamento: "... S A L D O ..."
            fim = cel["data"]
            saldo_fim = cel["saldo"] if cel["saldo"] is not None else cel["valor"]
            dentro = False
            atual = None
            continue

        if cel["valor"] is None:             # informativa: gruda na anterior
            if atual is not None and cel["desc"]:
                atual["partes"].append(cel["desc"])
            continue

        atual = {"data": cel["data"], "valor": cel["valor"], "hist": cel["desc"],
                 "partes": [], "doc": cel["doc"], "nome": None}
        if cel["cont"]:
            atual["partes"].append(cel["cont"])
        transacoes.append(atual)
        corrente = round(corrente + cel["valor"], 2)
        if cel["saldo"] is not None:
            checkpoints.append((cel["data"], corrente, cel["saldo"]))

    if ini is None or not transacoes:
        raise ValueError("Nenhuma transação encontrada — isto é um extrato "
                         "de conta corrente do Banco do Brasil?")

    for t in transacoes:
        partes = [t.pop("hist")] + t.pop("partes")
        t["descricao"] = _limpar_descricao(partes)

    if fim is None:
        fim = max(t["data"] for t in transacoes)
    if saldo_fim is None:
        saldo_fim = corrente
    return transacoes, ini, saldo_ini, fim, saldo_fim, checkpoints


def parse(pdf_path) -> list:
    """Lista de dicionários: data, valor, descricao, doc, nome."""
    return _extrair(pdf_path)[0]


def info(pdf_path) -> dict:
    """Dados da conta e do período lidos do cabeçalho/saldos do extrato."""
    texto = _ler_pdf(pdf_path)
    _, ini, saldo_ini, fim, saldo_final, checkpoints = _extrair(pdf_path)

    ma = RE_AGENCIA.search(texto)
    mc = RE_CONTA.search(texto)
    if not (ma and mc):
        raise ValueError("Não encontrei agência/conta no PDF — isto é um "
                         "extrato do Banco do Brasil?")
    return {
        "banco_id": BANCO_ID,
        "agencia": ma.group(1),
        "conta": mc.group(1),
        "acctid": mc.group(1),
        "periodo_inicio": ini + timedelta(days=1),
        "periodo_fim": fim,
        "saldo_inicial": saldo_ini,
        "saldo_final": saldo_final,
        "checkpoints": checkpoints,
        "total_creditos": None,
        "total_debitos": None,
    }


def validar(transacoes, dados) -> list:
    avisos = []
    for d, calc, impresso in dados.get("checkpoints", []):
        if abs(calc - impresso) > 0.005:
            avisos.append(f"Saldo de {d:%d/%m/%Y}: calculado {calc:.2f} != "
                          f"extrato {impresso:.2f}")
    si, sf = dados.get("saldo_inicial"), dados.get("saldo_final")
    if si is not None and sf is not None:
        esperado = round(si + sum(t["valor"] for t in transacoes
                                  if t.get("valor") is not None), 2)
        if abs(esperado - sf) > 0.005:
            avisos.append(f"Saldo final: inicial {si:.2f} + movimento = "
                          f"{esperado:.2f} != extrato {sf:.2f}")
    return avisos


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        sys.exit('uso: python parser_bb.py extrato.pdf [saida.ofx]')
    trans = parse(sys.argv[1])
    dados = info(sys.argv[1])
    for aviso in validar(trans, dados):
        print('AVISO:', aviso)
    if len(sys.argv) < 3:
        for t in trans:
            print(f"{t['data']:%d/%m/%Y} {t['valor']:>12.2f}  {t['descricao']}")
        print(f"{len(trans)} transações | {dados['saldo_inicial']:.2f} -> "
              f"{dados['saldo_final']:.2f}")
        sys.exit(0)
    from ofx_writer import gerar_ofx
    gerar_ofx(trans, dados, sys.argv[2])
    print(str(len(trans)) + ' transações -> ' + sys.argv[2])