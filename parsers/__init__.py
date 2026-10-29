"""Registro de layouts de extrato.

Para adicionar um novo banco:
  1. Crie ``parsers/<banco>.py`` expondo ``parse(pdf_path) -> list[dict]``
     (e opcionalmente ``info(pdf_path) -> dict`` com dados da conta).
  2. Importe o módulo e adicione uma entrada em ``LAYOUTS`` abaixo.

Cada transação retornada por ``parse`` é um dicionário com as chaves:
  data      -> datetime.date
  valor     -> float (negativo = débito, positivo = crédito)
  descricao -> str (histórico da transação)
  doc       -> str (número do documento, pode ser vazio)
  nome      -> str | None (nome do favorecido/origem, quando houver)
"""

from . import bradesco, sicoob1, sicoob2, sicredi, Inter, santander, itau, banco_brasil, Aplicacao_sicoob, Caixa_economica, nubank, itau2, banco_brasil2, C6bank, Pagbank, Stone, banco_brasil3, mercado_pago, aplicacao_pagbank


LAYOUTS = {
    "Bradesco": bradesco,
    "Sicoob - SISBR - SETINHA": sicoob1,
    "SICOOB - SISBR - BASICO": sicoob2,
    "Sicredi": sicredi,
    "Inter": Inter,
    "Santander": santander,
    "Itau": itau,
    "Itau2": itau2,
    "Banco do Brasil": banco_brasil,
    "Aplicacao_sicoob": Aplicacao_sicoob,
    "Caixa economica": Caixa_economica,
    "Nubank": nubank,
    "banco_brasil2": banco_brasil2,
    "c6_bank": C6bank,
    "Pagbank": Pagbank,
    "Stone": Stone,
    "banco_brasil3": banco_brasil3,
    "Mercado Pago": mercado_pago,
    "aplicacao_pagbank": aplicacao_pagbank,
}
