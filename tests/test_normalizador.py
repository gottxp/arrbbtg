"""Testes da normalizacao de pares (prefixos, cotacao e preco por unidade).

Funcoes puras: nenhum teste acessa a internet.
"""

import pytest

from scanner.normalizador import normalizar, preco_unitario_usdt


def test_prefixo_1000():
    p = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 1})
    assert p.ativo == "PEPE" and p.multiplicador == 1000

def test_prefixo_k_hyperliquid():
    p = normalizar("hyperliquid", {"base": "kPEPE", "quote": "USDC", "contractSize": 1})
    assert p.ativo == "PEPE" and p.multiplicador == 1000 and p.cotacao == "USDC"

def test_prefixo_1m():
    p = normalizar("mexc", {"base": "1MBABYDOGE", "quote": "USDT", "contractSize": 1})
    assert p.ativo == "BABYDOGE" and p.multiplicador == 1_000_000

def test_k_maiusculo_nao_e_prefixo():
    assert normalizar("gate", {"base": "KAS", "quote": "USDT", "contractSize": 1}).ativo == "KAS"
    assert normalizar("gate", {"base": "KAVA", "quote": "USDT", "contractSize": 1}).multiplicador == 1

def test_mesmo_ativo_mesmo_preco():  # Foco de revisao 1
    a = normalizar("gate", {"base": "PEPE", "quote": "USDT", "contractSize": 1})
    b = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 1})
    pa, _ = preco_unitario_usdt(0.00001, a, {})
    pb, _ = preco_unitario_usdt(0.01, b, {})
    assert pa == pytest.approx(pb)

def test_conversao_usdc():
    p = normalizar("hyperliquid", {"base": "BTC", "quote": "USDC", "contractSize": 1})
    assert preco_unitario_usdt(100000.0, p, {"USDC": 0.9998}) == (pytest.approx(99980.0), False)

def test_sem_cotacao_usa_1_e_avisa():
    p = normalizar("lighter_rh", {"base": "BTC", "quote": "USDG", "contractSize": 1})
    assert preco_unitario_usdt(100000.0, p, {}) == (100000.0, True)


# Casos de borda alem do brief: prefixos longos, contractSize ausente e USDT.

def test_prefixos_10000_e_1000000():
    p = normalizar("mexc", {"base": "10000SHIB", "quote": "USDT", "contractSize": 1})
    assert p.ativo == "SHIB" and p.multiplicador == 10_000
    p = normalizar("mexc", {"base": "1000000BONK", "quote": "USDT", "contractSize": 1})
    assert p.ativo == "BONK" and p.multiplicador == 1_000_000

def test_k_minusculo_seguido_de_minuscula_nao_e_prefixo():
    assert normalizar("hyperliquid", {"base": "kaspa", "quote": "USDC", "contractSize": 1}).ativo == "kaspa"

def test_contract_size_none_ou_ausente_vira_1():
    assert normalizar("gate", {"base": "BTC", "quote": "USDT", "contractSize": None}).tamanho_contrato == 1
    assert normalizar("gate", {"base": "BTC", "quote": "USDT"}).tamanho_contrato == 1

def test_contract_size_guardado_no_par():
    p = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 0.1})
    assert p.tamanho_contrato == 0.1 and p.multiplicador == 1000

def test_contract_size_nao_muda_preco_unitario():
    # Contrato afeta a quantidade (Task 4), nao o preco de 1 unidade.
    p = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 0.1})
    assert preco_unitario_usdt(0.01, p, {}) == (pytest.approx(0.00001), False)

def test_usdt_ignora_cotacoes_informadas():
    p = normalizar("gate", {"base": "BTC", "quote": "USDT", "contractSize": 1})
    assert preco_unitario_usdt(100000.0, p, {"USDT": 0.5}) == (100000.0, False)
