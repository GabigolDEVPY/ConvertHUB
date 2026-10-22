"""Parser do extrato de conta corrente do Banco do Brasil (BB Empresa -
"Consultas - Extrato de conta corrente", PDF).

A seção "Lançamentos" é uma tabela com as colunas
Dt. balancete | Dt. movimento | Ag. origem | Lote | Histórico | Documento |
Valor R$ | Saldo. A linha de cada lançamento (a "âncora") começa com a data
do balancete (dd/mm/aaaa) e traz histórico, documento e valor seguido de
"C" (crédito) ou "D" (débito). Muitos históricos têm uma 2ª linha, sem data,
só com a continuação da descrição (ex.: favorecido do Pix, TED, boleto).
A coluna Saldo só é preenchida na última linha do dia (aqui, as linhas
"BB Rende Fácil", que zeram a conta todo dia).

Estratégia: percorre as linhas do PDF em ordem de leitura (atravessando as
quebras de página — uma continuação pode cair no topo da página seguinte)
entre a linha "Saldo Anterior" e a linha "S A L D O"; cada linha com data e
valor abre uma transação nova e as linhas sem data são anexadas à descrição
da transação anterior. O sinal vem da letra C/D ao lado do valor.

Tudo fora da movimentação (cabeçalho, limite do cheque especial, CET,
rodapé) é ignorado.

Validação: o extrato do BB não tem resumo de totais, então a conferência é
feita pelo saldo — saldo anterior + movimento tem que bater com o saldo
impresso em CADA linha que tem saldo (fim de cada dia) e com o saldo final.

O OFX gerado segue exatamente o formato dos parsers Santander/Itaú: sem
indentação, FITID = CHECKNUM = AAAAMMDD + sequência do dia (2 dígitos),
DTPOSTED só com a data e LEDGERBAL com o saldo final do extrato.

Compatibilidade de layout:
- Layout "antigo"/largo: histórico ~268, valor x1<=516, C/D valor x0>500
- Layout "novo"/compacto (ex. set/2026): histórico ~263, valor x1~488,
  C/D valor x0~490, e ocasionalmente Documento+Valor colados num único token
  (ex. "996.409.176.021.201281.066,36"). O parser detecta e separa.
"""

import re
import unicodedata
from datetime import date, timedelta

import pdfplumber

NOME = "Banco do Brasil (extrato de conta corrente PJ)"
BANCO_ID = "001"
BANCO_ORG = "Banco do Brasil S.A."

RE_DATA = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
# Valor monetário BR estrito: grupos de milhar com ponto + vírgula + 2 decimais
RE_VALOR = re.compile(r"^(\d{1,3}(?:\.\d{3})*,\d{2})([CD])?$")
# Valor com letra C/D colada na frente (ex. D100.953,97 — comum no saldo)
RE_LETRA_VALOR = re.compile(r"^([CD])(\d{1,3}(?:\.\d{3})*,\d{2})$")
# Mesmo padrão ancorado no fim (para tokens Documento+Valor colados)
RE_VALOR_FIM = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})([CD])?$")
RE_DOC = re.compile(r"^[\d.]+$")
RE_AGENCIA = re.compile(r"Ag[eê]ncia\s+(\d+-[\dXx])")
RE_CONTA = re.compile(r"Conta corrente\s+(\d[\d.]*-[\dXx])")
RE_PERIODO = re.compile(r"Per[ií]odo do extrato\s+(\d{2})\s*/\s*(\d{4})")

# Colunas — faixas amplas o suficiente para os dois layouts conhecidos.
# Valores "preferidos" (layout antigo) e limites soltos (fallback).
X_DATA_MAX = 130          # coluna Dt. balancete (Dt. movimento é ignorada)
X_DESC_MIN = 240          # início mínimo do Histórico (âncora ou continuação)
X_DESC_ANCORA = 260       # preferência âncora (era 268; layout novo usa ~263)
X_DESC_CONT = 240         # continuação (era 250)
X_DOC_X1_MIN = 360        # documento: x1 mínimo
X_DOC_X1_MAX = 490        # documento: x1 máximo (antes do valor)
X_VALOR_MAX = 520         # x1 <= isto → Valor; acima → Saldo (era 516)
X_LETRA_VALOR_MAX = 530   # letra C/D do Valor (era 524; layout novo ~496)
X_LETRA_MIN = 470         # C/D só considerados a partir daqui (evita "C" do nome)
TOL_LINHA = 3             # tolerância (pt) para agrupar palavras na mesma linha

HIST_COM_DOC = ("cheque",)   # históricos cujo nº do documento entra no MEMO


def _norm(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _num(t):
    """'8.000,00' -> 8000.0"""
    return float(t.replace(".", "").replace(",", "."))


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


def _separar_doc_valor(t):
    """Tenta separar token colado 'documento+valor' (ex. 996...201281.066,36).

    Retorna (doc, valor_str, letra) ou None.
    """
    m = RE_VALOR_FIM.search(t)
    if not m or m.start() == 0:
        return None
    doc = t[:m.start()]
    valor_str, letra = m.group(1), m.group(2)
    # Documento deve parecer número de documento BB (dígitos/pontos, não vazio)
    if not doc or not RE_DOC.match(doc):
        return None
    return doc, valor_str, letra


def _classificar(linha):
    """Separa as palavras de uma linha por coluna.

    Usa faixas amplas + fallbacks para cobrir layouts com colunas
    ligeiramente deslocadas e tokens Documento+Valor colados pelo pdfplumber.

    Retorna também data_mov (Dt. movimento) quando a linha traz as duas datas.
    A data principal (data) continua sendo a Dt. balancete; o chamador pode
    preferir data_mov para DTPOSTED do OFX.
    """
    data = data_mov = None
    for w in linha:
        m = RE_DATA.match(w["text"])
        if not m:
            continue
        d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        # Dt. balancete: coluna esquerda (x0 < ~120)
        # Dt. movimento: coluna seguinte (~120–180)
        if w["x0"] < 120:
            data = d
        elif w["x0"] < 180 and data is not None:
            data_mov = d
        elif data is None and w["x0"] < X_DATA_MAX:
            data = d
    x_desc = X_DESC_ANCORA if data else X_DESC_CONT

    valor = saldo = None
    letra_valor = letra_saldo = None
    doc = None
    desc = []
    for w in linha:
        t, x0, x1 = w["text"], w["x0"], w["x1"]
        if x0 < X_DATA_MAX:
            continue

        # Token colado documento+valor (layout compacto / pdfplumber)
        if x0 > 300 and valor is None:
            sep = _separar_doc_valor(t)
            if sep:
                doc_s, valor_s, letra_s = sep
                if not doc:
                    doc = doc_s
                valor = _num(valor_s)
                if letra_s:
                    letra_valor = letra_s
                continue

        m = RE_VALOR.match(t)
        if m and x0 > 350:
            v = _num(m.group(1))
            if x1 <= X_VALOR_MAX:
                valor, letra_valor = v, m.group(2) or letra_valor
            else:
                saldo, letra_saldo = v, m.group(2) or letra_saldo
            continue

        # Letra C/D colada ao número (ex. "D100.953,97" no saldo)
        m = RE_LETRA_VALOR.match(t)
        if m and x0 > 350:
            letra, v = m.group(1), _num(m.group(2))
            # Se ainda não temos valor e x1 está na zona de valor, é o valor;
            # senão é saldo (caso típico: D colado ao saldo).
            if valor is None and x1 <= X_VALOR_MAX:
                valor, letra_valor = v, letra
            else:
                # A letra colada pertence ao número; a letra do valor (se
                # houver e estiver faltando) pode estar "absorvida".
                # Heurística: se valor existe sem letra e este token começa
                # logo após a zona de valor, a 1ª letra é do valor.
                if valor is not None and letra_valor is None and x0 < 510:
                    letra_valor = letra
                    # O número em si é o saldo (sem letra própria neste token)
                    saldo = v
                else:
                    saldo, letra_saldo = v, letra
            continue

        # Letra C/D isolada (layout novo: valor ~490; saldo ~540)
        if t in ("C", "D") and x0 > X_LETRA_MIN:
            if x1 <= X_LETRA_VALOR_MAX:
                letra_valor = t
            else:
                letra_saldo = t
            continue

        # Documento (somente dígitos/pontos, alinhado à coluna Documento)
        if (data and X_DOC_X1_MIN <= x1 <= X_DOC_X1_MAX
                and RE_DOC.match(t) and x0 > 300 and not doc):
            # Evitar confudir lote curto (ex. 9.903 do Rende Fácil) —
            # 9.903 é documento legítimo do BB Rende Fácil; aceitar.
            doc = t
            continue

        # Histórico: tudo entre desc e documento (com folga)
        if x0 >= x_desc and x1 <= X_DOC_X1_MAX + 10:
            # Não engolir número que é claramente documento
            if RE_DOC.match(t) and x0 > 350:
                if not doc:
                    doc = t
                continue
            desc.append(t)
            continue

        # Fallback: se ainda não pegamos desc e a palavra está na zona de
        # histórico (layout com x0 um pouco à esquerda de X_DESC_ANCORA)
        if (data and valor is None and not desc
                and X_DESC_MIN <= x0 < x_desc
                and not RE_DOC.match(t) and t not in ("C", "D")):
            desc.append(t)

    if valor is not None:
        valor = -valor if letra_valor == "D" else valor
    if saldo is not None:
        saldo = -saldo if letra_saldo == "D" else saldo
    return {"data": data, "data_mov": data_mov, "valor": valor,
            "saldo": saldo, "doc": doc or "",
            "desc": " ".join(desc).strip()}


def _ler_pdf(pdf_path):
    with pdfplumber.open(str(pdf_path)) as pdf:
        texto = "\n".join((p.extract_text() or "") for p in pdf.pages)
        paginas = [p.extract_words() for p in pdf.pages]
    return texto, paginas


def _eh_saldo_anterior(desc_n, cel):
    """Detecta a linha de abertura 'Saldo Anterior' (várias grafias/layouts)."""
    if not cel.get("data"):
        return False
    # "saldoanterior", "saldo anterior", só "anterior" com saldo preenchido
    if "saldoanterior" in desc_n or desc_n == "anterior":
        return True
    if desc_n.startswith("saldo") and "anterior" in desc_n:
        return True
    return False


def _eh_saldo_final(desc_n, cel):
    """Detecta a linha de fechamento 'S A L D O' / 'SALDO'."""
    if not cel.get("data"):
        return False
    # "saldo", "s a l d o" normalizado vira "saldo"
    limpo = desc_n.replace(" ", "")
    return limpo in ("saldo", "saldofinal") or limpo == "saldo"



def _limpar_descricao(partes):
    """Junta partes da descrição e remove ruído típico do extrato BB.

    - "Rende Facil" / "Rende Fácil" repetido após "BB Rende Fácil"
    - espaços duplicados
    """
    limpas = []
    visto_rende = False
    for p in partes:
        if not p:
            continue
        n = _norm(p).lower().replace(" ", "")
        if n in ("rendefacil", "rendefacil"):
            if visto_rende:
                continue
        if "rendefacil" in n or n.startswith("bbrende"):
            visto_rende = True
        limpas.append(p)
    # Dedup consecutivo
    out = []
    for p in limpas:
        if out and out[-1] == p:
            continue
        out.append(p)
    return " ".join(out).strip()


def _extrair(pdf_path):
    """Devolve (transações, data_saldo_anterior, saldo_inicial,
    data_saldo_final, saldo_final, checkpoints)."""
    texto, paginas = _ler_pdf(pdf_path)

    transacoes = []
    checkpoints = []          # (data, saldo calculado, saldo impresso)
    atual = None
    dentro = False
    ini = fim = saldo_ini = saldo_fim = None
    corrente = 0.0

    for words in paginas:
        for linha in _agrupar_linhas(words):
            cel = _classificar(linha)
            desc_n = _norm(cel["desc"]).lower().replace(" ", "")

            if not dentro:
                # Abertura: "30/04/2026 ... Saldo Anterior ... 0,00 C"
                if _eh_saldo_anterior(desc_n, cel):
                    ini = cel["data"]
                    saldo_ini = cel["saldo"] if cel["saldo"] is not None \
                        else 0.0
                    corrente = saldo_ini
                    dentro = True
                continue

            # Fechamento: "31/05/2026 ... S A L D O ... 0,00 C"
            if _eh_saldo_final(desc_n, cel) and cel["valor"] is None:
                fim = cel["data"]
                saldo_fim = cel["saldo"]
                dentro = False
                atual = None
                continue

            if cel["data"] is not None:            # linha âncora
                if cel["valor"] is None:           # informativa, sem valor
                    if not desc_n.startswith("saldo") and cel["desc"]:
                        data_tx = cel.get("data_mov") or cel["data"]
                        atual = {
                            "data": data_tx,
                            "valor": None,
                            "hist": cel["desc"],
                            "partes": [],
                            "doc": cel["doc"],
                            "nome": None,
                            "incompleto": True,
                            "motivo": "Valor não identificado",
                        }
                        transacoes.append(atual)
                    else:
                        atual = None
                    continue
                # Preferir Dt. movimento quando presente (data real do lançamento)
                data_tx = cel.get("data_mov") or cel["data"]
                atual = {
                    "data": data_tx,
                    "valor": cel["valor"],
                    "hist": cel["desc"],
                    "partes": [],
                    "doc": cel["doc"],
                    "nome": None,
                }
                transacoes.append(atual)
                corrente = round(corrente + cel["valor"], 2)
                if cel["saldo"] is not None:
                    checkpoints.append((data_tx, corrente, cel["saldo"]))
            elif cel["desc"] and atual is not None:    # continuação
                # Ignorar linhas "Tar. agrupadas - ocorrencia dd/mm/aaaa"
                if desc_n.startswith("tar.agrupadas") or desc_n.startswith(
                        "taragrupadas"):
                    continue
                atual["partes"].append(cel["desc"])

    if ini is None or not transacoes:
        raise ValueError("Nenhuma transação encontrada — isto é um extrato "
                         "de conta corrente do Banco do Brasil?")

    for t in transacoes:
        hist_txt = t.pop("hist", t.get("descricao", ""))
        partes = [hist_txt] + t.pop("partes", [])
        # Nota: o doc já é gravado em CHECKNUM/REFNUM pelo ofx_writer,
        # então não precisa incluir no descricao/memo.
        t["descricao"] = _limpar_descricao(partes)

    if fim is None:                        # sem a linha "S A L D O"
        fim = max(t["data"] for t in transacoes)
    return transacoes, ini, saldo_ini, fim, saldo_fim, checkpoints


def parse(pdf_path) -> list:
    """Extrai as transações do extrato de conta corrente BB.

    Retorna lista de dicionários: data, valor, descricao, doc, nome.
    """
    return _extrair(pdf_path)[0]


def info(pdf_path) -> dict:
    """Dados da conta e do período lidos do cabeçalho/saldos do extrato."""
    texto, _ = _ler_pdf(pdf_path)
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
        "periodo_inicio": ini + timedelta(days=1),   # dia após "Saldo Anterior"
        "periodo_fim": fim,
        "saldo_inicial": saldo_ini,
        "saldo_final": saldo_final,
        "checkpoints": checkpoints,
        # O extrato do BB não traz totais de créditos/débitos.
        "total_creditos": None,
        "total_debitos": None,
    }


def validar(transacoes, dados) -> list:
    """Confere saldo diário e saldo final; devolve lista de avisos."""
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
    from ofx_writer import gerar_ofx

    if len(sys.argv) < 3:
        sys.exit('uso: python parser_bb.py extrato.pdf saida.ofx')
    trans = parse(sys.argv[1])
    dados = info(sys.argv[1])
    for aviso in validar(trans, dados):
        print('AVISO:', aviso)
    gerar_ofx(trans, dados, sys.argv[2])
    print(str(len(trans)) + ' transações -> ' + sys.argv[2])