"""Parser do extrato mensal consolidado do Itaú (Itaú Empresas / PJ).

A seção "Conta Corrente | Movimentação" do PDF é uma tabela com as colunas
data | descrição | entradas R$ (créditos) | saídas R$ (débitos) | saldo R$.
Diferente do Santander, não há coluna de nº de documento e cada lançamento
ocupa UMA linha só. A data só é impressa no primeiro lançamento de cada dia
e o saldo só aparece na última linha do dia.

Estratégia: percorre as linhas do PDF em ordem de leitura (atravessando as
quebras de página) entre a linha "Saldo anterior" e a linha "Saldo em C/C";
cada linha com valor na coluna Entradas/Saídas é uma transação. O sinal vem
da coluna (Saídas tem o "-" no final: "8.000,00-").

Tudo o que não é movimentação (resumo, gráfico, aplicações automáticas,
débitos automáticos, cheque especial, notas explicativas) é ignorado, pois
só interessam os lançamentos de crédito/débito da conta corrente.

Linhas informativas sem movimento de caixa (ex.: "SALDO APLIC AUT MAIS",
que só tem valor na coluna Saldo) são descartadas.

O OFX gerado segue o mesmo formato do parser do Santander: sem indentação,
FITID = CHECKNUM = AAAAMMDD + sequência do dia (2 dígitos), DTPOSTED só com
a data e LEDGERBAL com o saldo final do extrato.
"""

import re
import unicodedata
from datetime import date, timedelta

import pdfplumber

NOME = "Itaú (extrato mensal consolidado PJ)"
BANCO_ID = "341"
BANCO_ORG = "Banco Itaú Unibanco S.A."

RE_DATA = re.compile(r"^(\d{2})/(\d{2})$")
RE_VALOR = re.compile(r"^\d[\d.]*,\d{2}-?$")
RE_CABECALHO = re.compile(
    r"ag\s+(\d+)\s+cc\s+([\d.]+-\d)\s+([a-zç]{3})\s+(\d{4})", re.I)
RE_SALDO_DATAS = re.compile(r"saldo em (\d{2})/(\d{2})/(\d{2})", re.I)

MESES = ["jan", "fev", "mar", "abr", "mai", "jun",
         "jul", "ago", "set", "out", "nov", "dez"]

# Colunas identificadas pela posição das palavras (x0 = início, x1 = fim),
# em pontos (página A4 = 595 x 842).
X_DATA = (140, 200)      # coluna data
X_DESC = (200, 345)      # coluna descrição
X_VALOR_MIN = 345        # a partir daqui só entram valores numéricos
X_CRED_MAX = 420         # valor com x1 até aqui = Entradas (crédito)
X_DEB_MAX = 500          # valor com x1 entre 420 e 500 = Saídas (débito)
                         # x1 > 500 = Saldo
TOL_LINHA = 2            # tolerância (pt) para agrupar palavras na mesma linha

JUNK = (
    "extrato mensal",
    "este material",
    "data descrição",
    "(créditos)",
)
RE_RODAPE = re.compile(r"^\d+\s+B\d+A\s+\d{2}/\d{2}/\d{4}")


def _norm(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _num(t):
    """'8.000,00-' -> -8000.0 ; '4.029,22' -> 4029.22"""
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
    dia = mes = credito = debito = saldo = None
    desc = []
    for w in linha:
        t, x0, x1 = w["text"], w["x0"], w["x1"]
        if X_DATA[0] <= x0 < X_DATA[1]:
            m = RE_DATA.match(t)
            if m:
                dia, mes = int(m.group(1)), int(m.group(2))
            continue
        if X_DESC[0] <= x0 < X_DESC[1]:
            desc.append(t)
            continue
        if x0 >= X_VALOR_MIN and RE_VALOR.match(t):
            v = _num(t)
            if x1 <= X_CRED_MAX:
                credito = abs(v)
            elif x1 <= X_DEB_MAX:
                # Débito vem com "-" no fim; garante o sinal pela coluna
                # mesmo que o "-" tenha sido perdido na extração.
                debito = -abs(v)
            else:
                saldo = v
    valor = credito if credito is not None else debito
    return {"dia": dia, "mes": mes, "valor": valor, "saldo": saldo,
            "desc": " ".join(desc).strip()}


def _junk(texto):
    t = texto.lower()
    return any(t.startswith(p) for p in JUNK) or bool(RE_RODAPE.match(texto))


def _cabecalho(texto):
    m = RE_CABECALHO.search(_norm(texto))
    if not m:
        raise ValueError("Não encontrei agência/conta/mês de referência no "
                         "PDF — isto é um extrato mensal do Itaú?")
    mes = MESES.index(m.group(3).lower()) + 1
    return m.group(1), m.group(2), mes, int(m.group(4))


def _ler_pdf(pdf_path):
    with pdfplumber.open(str(pdf_path)) as pdf:
        texto = "\n".join((p.extract_text() or "") for p in pdf.pages)
        paginas = [p.extract_words() for p in pdf.pages]
    return texto, paginas


def _extrair(pdf_path):
    """Devolve (transações, data_saldo_anterior, saldo_inicial,
    data_saldo_final, saldo_final)."""
    texto, paginas = _ler_pdf(pdf_path)
    _, _, mes_ref, ano = _cabecalho(texto)

    transacoes = []
    data_atual = None
    dentro = False
    ini = fim = saldo_ini = saldo_fim = None

    for words in paginas:
        for linha in _agrupar_linhas(words):
            if _junk(" ".join(w["text"] for w in linha)):
                continue
            cel = _classificar(linha)
            desc_n = _norm(cel["desc"]).lower()

            if desc_n.startswith("descricao"):       # cabeçalho da tabela
                continue

            if not dentro:
                # Abertura: "31/10  Saldo anterior  ...  500,00-"
                if desc_n.startswith("saldo anterior") and cel["dia"]:
                    ano_ini = ano - 1 if cel["mes"] > mes_ref else ano
                    ini = date(ano_ini, cel["mes"], cel["dia"])
                    saldo_ini = cel["saldo"]
                    dentro = True
                continue

            # Fechamento: "Saldo em C/C  ...  480,46-"
            if desc_n.startswith("saldo em c/c"):
                saldo_fim = cel["saldo"]
                dentro = False
                continue

            if cel["dia"] is not None:
                data_atual = date(ano, cel["mes"], cel["dia"])

            if cel["valor"] is None:
                if cel["dia"] is not None and cel["desc"] and not desc_n.startswith("saldo") and not _aplic_aut(cel):
                    transacoes.append({
                        "data": data_atual,
                        "valor": None,
                        "descricao": cel["desc"],
                        "doc": "",
                        "nome": None,
                        "incompleto": True,
                        "motivo": "Valor não identificado",
                    })
                continue

            incompleto = (data_atual is None)
            item = {
                "data": data_atual,
                "valor": cel["valor"],
                "descricao": cel["desc"],
                "doc": "",
                "nome": None,
            }
            if incompleto:
                item["incompleto"] = True
                item["motivo"] = "Data não identificada"
            transacoes.append(item)

    if ini is None or not transacoes:
        raise ValueError("Nenhuma transação encontrada — isto é um extrato "
                         "mensal do Itaú?")

    # Data do saldo final: "saldo em 29/11/24" no resumo (2ª ocorrência);
    # se não achar, usa a data da última transação.
    datas = RE_SALDO_DATAS.findall(texto)
    if len(datas) >= 2:
        d, m, a = datas[1]
        fim = date(2000 + int(a), int(m), int(d))
    else:
        fim = max(t["data"] for t in transacoes)
    return transacoes, ini, saldo_ini, fim, saldo_fim


def parse(pdf_path) -> list:
    """Extrai as transações do extrato mensal Itaú.

    Retorna lista de dicionários: data, valor, descricao, doc, nome.
    """
    return _extrair(pdf_path)[0]


def info(pdf_path) -> dict:
    """Dados da conta e do período lidos do cabeçalho/resumo do extrato."""
    texto, _ = _ler_pdf(pdf_path)
    agencia, conta, _, _ = _cabecalho(texto)
    _, ini, saldo_ini, fim, saldo_final = _extrair(pdf_path)

    def _total(rotulo):
        mt = re.search(rotulo + r".*?total\s+([\d.]+,\d{2})", texto, re.S)
        return _num(mt.group(1)) if mt else None

    return {
        "banco_id": BANCO_ID,
        "agencia": agencia,
        "conta": conta,
        "acctid": conta,
        "periodo_inicio": ini + timedelta(days=1),   # dia após "Saldo anterior"
        "periodo_fim": fim,
        "saldo_inicial": saldo_ini,
        "saldo_final": saldo_final,
        "total_creditos": _total(r"entradas \(créditos\)"),
        "total_debitos": _total(r"saídas \(débitos\)"),
    }


def _aplic_aut(t):
    """Movimentos do CDB automático ("Apl/Res Aplic Aut Mais"): o próprio
    Itaú não os soma no Resumo de entradas/saídas."""
    return "aplic aut" in t["descricao"].lower()


def validar(transacoes, dados) -> list:
    """Confere as somas com o resumo do extrato e o saldo; devolve avisos."""
    avisos = []
    # O Resumo do Itaú não inclui as aplicações automáticas; o saldo sim.
    sem_aplic = [t for t in transacoes if not _aplic_aut(t)]
    cred = round(sum(t["valor"] for t in sem_aplic if t["valor"] > 0), 2)
    deb = round(-sum(t["valor"] for t in sem_aplic if t["valor"] < 0), 2)
    cred_tot = round(sum(t["valor"] for t in transacoes if t["valor"] > 0), 2)
    deb_tot = round(-sum(t["valor"] for t in transacoes if t["valor"] < 0), 2)
    if dados.get("total_creditos") is not None \
            and abs(cred - dados["total_creditos"]) > 0.005:
        avisos.append(f"Créditos: soma {cred:.2f} != resumo "
                      f"{dados['total_creditos']:.2f}")
    if dados.get("total_debitos") is not None \
            and abs(deb - dados["total_debitos"]) > 0.005:
        avisos.append(f"Débitos: soma {deb:.2f} != resumo "
                      f"{dados['total_debitos']:.2f}")
    si, sf = dados.get("saldo_inicial"), dados.get("saldo_final")
    if si is not None and sf is not None:
        esperado = round(si + cred_tot - deb_tot, 2)
        if abs(esperado - sf) > 0.005:
            avisos.append(f"Saldo: inicial {si:.2f} + movimento = "
                          f"{esperado:.2f} != saldo final {sf:.2f}")
    return avisos


if __name__ == '__main__':
    import sys
    from ofx_writer import gerar_ofx

    if len(sys.argv) < 3:
        sys.exit('uso: python parser_itau.py extrato.pdf saida.ofx')
    trans = parse(sys.argv[1])
    dados = info(sys.argv[1])
    for aviso in validar(trans, dados):
        print('AVISO:', aviso)
    gerar_ofx(trans, dados, sys.argv[2])
    print(str(len(trans)) + ' transações -> ' + sys.argv[2])
