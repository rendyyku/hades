import pytest

from hades.config import ChainCfg
from hades.identity import dedupe_key, normalize_address, project_key, registered_domain

CHAINS = {"monad": ChainCfg(evm=True), "solana": ChainCfg(evm=False)}


def test_evm_address_case_same_key():
    a = project_key(chain="monad", address="0xAbC", chains=CHAINS)
    b = project_key(chain="monad", address="0xabc", chains=CHAINS)
    assert a == b == "monad:0xabc"


def test_solana_address_case_sensitive():
    assert project_key(chain="solana", address="AbC", chains=CHAINS) != project_key(chain="solana", address="abc", chains=CHAINS)


def test_unknown_chain_0x_lowercased():
    assert normalize_address(None, "0xABC") == "0xabc"
    assert normalize_address(None, "AbC") == "AbC"


def test_project_key_fallbacks():
    assert project_key(symbol="CAT") == "sym:cat"
    assert project_key(domain="claim.cat.xyz") == "dom:cat.xyz"
    with pytest.raises(ValueError):
        project_key()


def test_registered_domain_multi_suffix():
    assert registered_domain("claim.cat.co.uk") == "cat.co.uk"
    assert registered_domain("WWW.Cat.XYZ") == "cat.xyz"


def test_dedupe_key_lowercases_and_skips_none():
    assert dedupe_key("repo_diff", "a/B", "monad", None, "0xAbC") == "repo_diff:a/b:monad:0xabc"
