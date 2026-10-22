"""
Parser para extrato Nubank (conta PJ/PF - PDF).

Layout do extrato: a movimentação vem agrupada por dia, com 3 colunas
(tipo | descrição | valor), seções de entradas/saídas e o saldo do dia:

    01 SET 2026  Total de entradas                                   + 635,24
                 Transferência recebida pelo Pix  PIPO BRASIL ... LTDA -      77,18
                                                  46.683.038/0001-07 - ...
                 Total de saídas                                     - 615,00
                 Pagamento de boleto efetuado     CONTABILIDADE SANTOS LTDA  615,00
                 Saldo do dia                                       30.602,38

Particularidades tratadas:
  * os valores das transações NÃO têm sinal: o sinal vem da seção
    ("Total de entradas" = crédito, "Total de saídas" = débito);
  * a descrição ocupa várias linhas e pode quebrar entre páginas;
  * cabeçalho/rodapé se repetem em todas as páginas (são ignorados);
  * o "Rendimento líquido" do resumo NÃO aparece como transação, mas está
    embutido no "Saldo do dia". Por padrão ele é reconstruído como uma
    transação diária (diferença entre o saldo do dia e o saldo esperado),
    para o OFX fechar com o saldo final. Desligue com GERAR_RENDIMENTO = False.

As colunas são separadas pela distância X entre as palavras (sem limites
fixos em pontos), então funciona para qualquer conta Nubank com este layout.

Gera dados no mesmo formato do parser Inter (parse() / info()), para uso com
ofx_writer.gerar_ofx.
"""

import copy
import re
import unicodedata
from datetime import date
from pathlib import Path

import pdfplumber

# ── Nome e banco ────────────────────────────────────────────────────────────
NOME = "Nubank (extrato conta - PDF)"
BANCO_NOME = "Nu Pagamentos S.A."
BANCO_ID = "260"

# ── Opções ──────────────────────────────────────────────────────────────────
GERAR_RENDIMENTO = False   # cria transação "Rendimento líquido" por dia

# ── Parâmetros de layout (pontos) ───────────────────────────────────────────
TOL_LINHA = 3.0        # variação de Y aceita para agrupar palavras na mesma linha
GAP_COLUNA = 12.0      # vazio mínimo em X para separar colunas
GAP_PALAVRA = 1.0      # vazio mínimo em X para inserir espaço entre palavras
TOL_COL_TIPO = 6.0     # tolerância em X para reconhecer a coluna "tipo"
FRAC_VALOR = 0.70      # coluna de valor começa após 70% da largura da página
FRAC_DATA = 0.20       # coluna de data fica antes de 20% da largura

# ── Regex ───────────────────────────────────────────────────────────────────
NEGATIVOS = ("-", "−", "–")
RE_VALOR_CHEIO = re.compile(r"^([+\-−–])?\s*(?:R\$\s*)?([\d.]+,\d{2})$")
RE_VALOR_FIM = re.compile(r"([+\-−–])?\s*(?:R\$\s*)?([\d.]+,\d{2})\s*$")
RE_PERIODO = re.compile(
    r"(?:a\s+)?(\d{1,2})\s+DE\s+([A-Za-zÀ-ÿ]+)\s+DE\s+(\d{4})\s*(?:a\s*)?"
    r"(\d{1,2})\s+DE\s+([A-Za-zÀ-ÿ]+)\s+DE\s+(\d{4})",
    re.IGNORECASE,
)
RE_CNPJ = re.compile(r"(?:CNPJ|CPF)\s+([\d./\-]+)")
RE_AGENCIA = re.compile(r"Ag[êe]ncia\s+([\d.\-]+)")
RE_CONTA = re.compile(r"Conta\s+([\d.\-]+)")

# CNPJ completo (46.683.038/0001-07) ou CPF parcialmente mascarado (•••.927.656-••)
_DOC = (
    r"(?:[\d•*]{2}\.[\d•*]{3}\.[\d•*]{3}/[\d•*]{4}-[\d•*]{2}"
    r"|[\d•*]{3}\.[\d•*]{3}\.[\d•*]{3}-[\d•*]{2})"
)
RE_NOME_DOC = re.compile(rf"^(.+?)\s+-\s+({_DOC})(?:\s+-\s+(.*))?$")

MESES = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11,
    "dezembro": 12,
}
MESES_ABREV = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}
# Tipos de operação que são crédito (usado só se a seção não for identificada).
_TIPOS_CREDITO = ("recebid", "deposito", "estorno", "reembolso", "resgate", "credito")


# ── Auxiliares de texto ─────────────────────────────────────────────────────

def _sem_acento(txt: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", txt)
        if unicodedata.category(c) != "Mn"
    )


def _para_float(numero: str) -> float:
    return float(numero.replace(".", "").replace(",", "."))


def _valor_assinado(texto: str):
    """'+ 635,24' -> 635.24 | '- 1.542,69' -> -1542.69 | '77,18' -> 77.18."""
    m = RE_VALOR_FIM.search(texto or "")
    if not m:
        return None
    v = _para_float(m.group(2))
    return -v if m.group(1) in NEGATIVOS else v


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


def _texto_linha(blocos) -> str:
    return " ".join(_texto(b) for b in blocos).strip()


def _data_da_linha(linha, largura):
    """
    Se a linha começa com 'DD MMM AAAA' na coluna de data, devolve
    (date, palavras_restantes); senão (None, palavras_ordenadas).
    """
    ws = sorted(linha, key=lambda w: w["x0"])
    if (
        len(ws) >= 3
        and ws[0]["x0"] < largura * FRAC_DATA
        and re.fullmatch(r"\d{1,2}", ws[0]["text"])
        and re.fullmatch(r"\d{4}", ws[2]["text"])
    ):
        mes = MESES_ABREV.get(_sem_acento(ws[1]["text"]).lower())
        if mes:
            try:
                return date(int(ws[2]["text"]), mes, int(ws[0]["text"])), ws[3:]
            except ValueError:
                pass
    return None, ws


# ── Nome / documento do favorecido ──────────────────────────────────────────

def _normalizar_nome(texto: str):
    """'PIPO BRASIL INSTITUICAO DE PAGAMENTO LTDA' -> 'Pipo Brasil Instituicao De Pagamento Ltda'."""
    t = (texto or "").strip()
    if not t:
        return None
    return " ".join(p[:1].upper() + p[1:].lower() for p in t.split(" ") if p)


def _nome_doc(detalhe: str):
    """
    Separa (nome, doc, resto) do detalhe da transação.

    'Edina Aparecida Ferreira - •••.927.656-•• - NU PAGAMENTOS ...'
        -> ('Edina Aparecida Ferreira', None, 'NU PAGAMENTOS ...')
    'PIPO BRASIL ... LTDA - 46.683.038/0001-07 - BANCO BTG ...'
        -> ('PIPO BRASIL ... LTDA', '46683038000107', 'BANCO BTG ...')
    'CONTABILIDADE SANTOS LTDA' -> ('CONTABILIDADE SANTOS LTDA', None, None)
    """
    if not detalhe:
        return None, None, None
    m = RE_NOME_DOC.match(detalhe)
    if not m:
        return detalhe, None, None
    doc = m.group(2)
    doc = None if re.search(r"[•*]", doc) else re.sub(r"\D", "", doc)
    return m.group(1).strip(), doc, m.group(3)


def _sinal_por_tipo(tipo: str) -> int:
    t = _sem_acento(tipo).lower()
    return 1 if any(k in t for k in _TIPOS_CREDITO) else -1


def _montar(raw: dict) -> dict:
    """Converte a transação bruta (linhas acumuladas) no dicionário final."""
    detalhe = re.sub(r"\s+", " ", " ".join(t for t in raw["linhas"] if t)).strip()
    nome_orig, doc, _ = _nome_doc(detalhe)

    descricao = raw["tipo"]
    if nome_orig:
        descricao = f"{raw['tipo']} - {nome_orig}"

    sinal = raw["secao"] or _sinal_por_tipo(raw["tipo"])
    valor = None if raw["valor"] is None else sinal * raw["valor"]

    tx = {
        "data": raw["data"],
        "valor": valor,
        "descricao": descricao,
        "doc": doc,
        "nome": _normalizar_nome(nome_orig),
        "saldo": None,
        "detalhe": detalhe,
    }
    if valor is None:
        tx["incompleto"] = True
        tx["motivo"] = "Valor não identificado"
    return tx


# ── Resumo (página 1) ───────────────────────────────────────────────────────

def _ler_resumo(pagina) -> dict:
    """Lê o quadro-resumo à direita: saldo inicial, rendimento, totais, saldo final."""
    palavras = pagina.extract_words()
    topo_mov = min(
        (w["top"] for w in palavras if _sem_acento(w["text"]).lower().startswith("movimentacoes")),
        default=pagina.height,
    )
    direita = [
        w for w in palavras
        if w["x0"] > pagina.width * 0.45 and w["top"] < topo_mov
    ]
    resumo = {}
    for linha in _agrupar_linhas(direita):
        texto = _texto_linha(_quebrar_em_blocos(linha))
        v = _valor_assinado(texto)
        if v is None:
            continue
        chave = _sem_acento(texto).lower()
        if chave.startswith("saldo inicial"):
            resumo["saldo_inicial"] = v
        elif chave.startswith("rendimento"):
            resumo["rendimento"] = v
        elif chave.startswith("total de entradas"):
            resumo["total_entradas"] = abs(v)
        elif chave.startswith("total de saidas"):
            resumo["total_saidas"] = abs(v)
        elif chave.startswith("saldo final"):
            resumo["saldo_final"] = v
    return resumo


# ── Extração ────────────────────────────────────────────────────────────────

def _ler(pdf_path) -> dict:
    """Lê o PDF inteiro. Devolve transações, saldos, totais e avisos."""
    brutas = []        # transações já montadas (sem rendimento)
    saldos_dia = []    # (data, saldo, posição em `brutas` no momento do saldo)
    totais = {}        # (data, sinal) -> valor do "Total de entradas/saídas"
    resumo = {}

    data_atual = None
    secao = None       # +1 entradas | -1 saídas
    atual = None       # transação em construção
    col_tipo_x = None  # X da coluna "tipo" (calibrado pelos rótulos Total/Saldo)

    def fechar():
        nonlocal atual
        if atual is not None:
            brutas.append(_montar(atual))
            atual = None

    with pdfplumber.open(str(pdf_path)) as pdf:
        for ip, pagina in enumerate(pdf.pages):
            largura = pagina.width
            if ip == 0:
                resumo = _ler_resumo(pagina)

            ativo = False
            for linha in _agrupar_linhas(pagina.extract_words()):
                blocos = _quebrar_em_blocos(linha)
                texto = _texto_linha(blocos)
                tl = _sem_acento(texto).lower()

                # Ignora cabeçalho: na pág. 1 até "Movimentações"; nas demais até a
                # linha do período ("... VALORES EM R$").
                if not ativo:
                    if tl.startswith("movimentacoes") or (ip > 0 and "valores em r$" in tl):
                        ativo = True
                    continue
                # Rodapé: encerra a página.
                if tl.startswith("tem alguma duvida") or tl.startswith("o saldo liquido"):
                    break

                nova_data, palavras = _data_da_linha(linha, largura)
                if nova_data:
                    fechar()
                    data_atual, secao = nova_data, None
                if not palavras or data_atual is None:
                    continue

                blocos = _quebrar_em_blocos(palavras)

                # Último bloco à direita com apenas um valor monetário.
                val_txt, corpo = None, blocos
                if blocos[-1][0]["x0"] > largura * FRAC_VALOR:
                    cand = _texto(blocos[-1])
                    if RE_VALOR_CHEIO.match(cand):
                        val_txt, corpo = cand, blocos[:-1]
                v = _valor_assinado(val_txt) if val_txt else None

                # Linha só com valor (valor deslocado da 1ª linha da transação).
                if not corpo:
                    if atual is not None and atual["valor"] is None and v is not None:
                        atual["valor"] = abs(v)
                    continue

                corpo_txt = _texto_linha(corpo)
                ct = _sem_acento(corpo_txt).lower()
                x_ini = corpo[0][0]["x0"]

                if ct.startswith("total de entradas") or ct.startswith("total de saidas"):
                    fechar()
                    secao = 1 if "entradas" in ct else -1
                    col_tipo_x = x_ini
                    if v is not None:
                        totais[(data_atual, secao)] = abs(v)
                    continue

                if ct.startswith("saldo do dia"):
                    fechar()
                    col_tipo_x = x_ini
                    if v is not None:
                        saldos_dia.append((data_atual, v, len(brutas)))
                    continue

                ref = col_tipo_x if col_tipo_x is not None else largura * 0.20
                if x_ini <= ref + TOL_COL_TIPO:
                    # Nova transação: [tipo] [descrição] [valor]
                    fechar()
                    atual = {
                        "data": data_atual,
                        "secao": secao,
                        "tipo": _texto(corpo[0]),
                        "linhas": [_texto_linha(corpo[1:])],
                        "valor": abs(v) if v is not None else None,
                    }
                elif atual is not None:
                    # Continuação da descrição (inclusive após quebra de página).
                    atual["linhas"].append(corpo_txt)
                    if atual["valor"] is None and v is not None:
                        atual["valor"] = abs(v)

    fechar()
    return _consolidar(brutas, saldos_dia, totais, resumo)


def _consolidar(brutas, saldos_dia, totais, resumo) -> dict:
    """Associa saldos do dia, reconstrói o rendimento e confere os totais."""
    avisos = []

    # 1) Diferença entre o saldo do dia e o saldo esperado (= rendimento).
    ajustes = []  # (posição, data, diferença, saldo_do_dia)
    saldo_ant = resumo.get("saldo_inicial")
    if saldo_ant is not None:
        ini = 0
        for data, saldo, pos in saldos_dia:
            soma = sum(t["valor"] or 0.0 for t in brutas[ini:pos])
            diff = round(saldo - (saldo_ant + soma), 2)
            ajustes.append((pos, data, diff, saldo))
            saldo_ant, ini = saldo, pos

    gerar = False
    if GERAR_RENDIMENTO and ajustes:
        total_diff = round(sum(a[2] for a in ajustes), 2)
        esperado = resumo.get("rendimento", 0.0)
        if abs(total_diff - esperado) <= 0.05:
            gerar = True
        else:
            avisos.append(
                f"Rendimento reconstruído (R$ {total_diff:.2f}) difere do resumo "
                f"(R$ {esperado:.2f}): rendimento NÃO gerado; possível transação "
                f"perdida ou lida errada."
            )

    # 2) Lista final: transações do dia + rendimento + saldo do dia.
    final, ini = [], 0
    eventos = ajustes if ajustes else [(p, d, 0.0, s) for d, s, p in saldos_dia]
    for pos, data, diff, saldo in eventos:
        final.extend(brutas[ini:pos])
        ini = pos
        if gerar and abs(diff) >= 0.01:
            final.append({
                "data": data,
                "valor": diff,
                "descricao": "Rendimento líquido",
                "doc": None,
                "nome": None,
                "saldo": saldo,
                "detalhe": "",
                "rendimento": True,
            })
        elif final and final[-1]["data"] == data:
            final[-1]["saldo"] = saldo
    final.extend(brutas[ini:])

    # 3) Conferências.
    for (data, sinal), total in sorted(totais.items()):
        soma = round(sum(
            abs(t["valor"]) for t in final
            if t["data"] == data and t["valor"] is not None
            and not t.get("rendimento") and (t["valor"] > 0) == (sinal > 0)
        ), 2)
        if abs(soma - total) > 0.005:
            nome = "entradas" if sinal > 0 else "saídas"
            avisos.append(
                f"{data:%d/%m/%Y}: total de {nome} do extrato R$ {total:.2f} "
                f"≠ soma lida R$ {soma:.2f}"
            )

    ent = round(sum(t["valor"] for t in final if t["valor"] and t["valor"] > 0 and not t.get("rendimento")), 2)
    sai = round(sum(-t["valor"] for t in final if t["valor"] and t["valor"] < 0), 2)
    if "total_entradas" in resumo and abs(ent - resumo["total_entradas"]) > 0.005:
        avisos.append(f"Total de entradas do resumo R$ {resumo['total_entradas']:.2f} ≠ lido R$ {ent:.2f}")
    if "total_saidas" in resumo and abs(sai - resumo["total_saidas"]) > 0.005:
        avisos.append(f"Total de saídas do resumo R$ {resumo['total_saidas']:.2f} ≠ lido R$ {sai:.2f}")
    if saldos_dia and "saldo_final" in resumo and abs(saldos_dia[-1][1] - resumo["saldo_final"]) > 0.005:
        avisos.append("Último 'Saldo do dia' difere do saldo final do resumo")
    for t in final:
        if t.get("incompleto"):
            avisos.append(f"{t['data']:%d/%m/%Y}: transação sem valor — {t['descricao']}")

    return {
        "transacoes": final,
        "saldos_dia": saldos_dia,
        "resumo": resumo,
        "avisos": avisos,
    }


# ── API pública (mesma do parser Inter) ─────────────────────────────────────

def parse(pdf_path) -> list:
    """
    Extrai as transações do extrato Nubank.

    Retorna lista de dicionários:
        data, valor, descricao, doc, nome, saldo
    (+ 'detalhe' com o texto completo da descrição).
    """
    return copy.deepcopy(_ler(pdf_path)["transacoes"])


def avisos(pdf_path) -> list:
    """Divergências encontradas ao conferir totais e saldos do extrato."""
    return _ler(pdf_path)["avisos"]


def info(pdf_path) -> dict:
    """Dados da conta e do período, usados no cabeçalho do OFX."""
    return _ler_cabecalho(pdf_path)


def _data_extenso(dia: str, mes: str, ano: str) -> date:
    return date(int(ano), MESES[_sem_acento(mes).lower()], int(dia))


def _titular(cabecalho: str) -> str:
    linhas = [l.strip() for l in cabecalho.splitlines()]
    for i, l in enumerate(linhas):
        if RE_CNPJ.search(l):
            for j in range(i - 1, -1, -1):
                if linhas[j]:
                    return linhas[j]
    return ""


def _ler_cabecalho(pdf_path) -> dict:
    with pdfplumber.open(str(pdf_path)) as pdf:
        texto = pdf.pages[0].extract_text() or ""

    periodo = RE_PERIODO.search(texto)
    if not periodo:
        raise ValueError(
            "Período não encontrado no PDF — isto é um extrato do Nubank?"
        )
    g = periodo.groups()
    d1 = _data_extenso(g[0], g[1], g[2])
    d2 = _data_extenso(g[3], g[4], g[5])

    cab = texto[:periodo.start()]   # só o cabeçalho (evita "Conta:" das transações)
    agencia = RE_AGENCIA.search(cab)
    conta = RE_CONTA.search(cab)
    cnpj = RE_CNPJ.search(cab)

    dados = _ler(pdf_path)
    resumo = dados["resumo"]
    saldo_final = resumo.get("saldo_final")
    if saldo_final is None and dados["saldos_dia"]:
        saldo_final = dados["saldos_dia"][-1][1]

    return {
        "banco_nome": BANCO_NOME,
        "banco_id": BANCO_ID,
        "agencia": agencia.group(1) if agencia else "",
        "conta": conta.group(1).replace(".", "").replace("-", "") if conta else "",
        "cnpj": cnpj.group(1) if cnpj else "",
        "titular": _titular(cab),
        "moeda": "BRL",
        "periodo_inicio": d1,
        "periodo_fim": d2,
        "saldo_final": saldo_final,
        "saldo_inicial": resumo.get("saldo_inicial"),
        "total_entradas": resumo.get("total_entradas"),
        "total_saidas": resumo.get("total_saidas"),
        "rendimento": resumo.get("rendimento"),
    }


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
    for a in avisos(pdf_path):
        print('AVISO:', a)
    gerar_ofx(txs, inf, out_path)
    print('OFX gerado:', out_path)