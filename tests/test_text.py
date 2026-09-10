from pipeline import text


def test_token_set_ratio_order_independent():
    assert text.token_set_ratio("ABC Constructions", "Constructions ABC") >= 90
    assert text.token_set_ratio("ABC Constructions", "XYZ Infra") < 60


def test_normalize_name_folds_suffixes_and_honorifics():
    a = text.normalize_name("M/s ABC Constructions Pvt. Ltd.")
    b = text.normalize_name("ABC Constructions")
    assert a == b == "abc constructions"


def test_normalize_org():
    assert text.normalize_org("RDD-CEO-JALGAON || Minor Irri.") == \
        "rdd ceo jalgaon minor irri"


def test_funding_scheme_and_level():
    assert text.funding_scheme("Water supply under Jal Jeevan Mission") == \
        "Jal Jeevan Mission"
    assert text.funding_scheme("DPDC road work") == "DPDC / District Planning"
    assert text.funding_scheme("ordinary repair") == ""
    assert text.funding_level("NHAI national highway work") == "central"
    assert text.funding_level("Zilla Parishad school repair") == "local"
    assert text.funding_level("state PWD building") == "state"


def test_funding_source():
    assert text.funding_source("World Bank assisted project") == "world_bank"
    assert text.funding_source("AMRUT sewerage") == "css"
    assert text.funding_source("routine work") == "unknown"


def test_clean_strips_em_dash():
    assert "—" not in text.clean("a — b")
