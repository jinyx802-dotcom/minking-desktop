from minking_desktop.app import _model_choices_from


def test_model_choices_keep_rate_official_and_sell_price():
    choices = _model_choices_from({
        "models": ["gpt-6-astra"],
        "catalog": {"models": [{
            "slug": "gpt-6-astra",
            "display_name": "Astra",
            "pricing": {
                "multiplier": "0.12",
                "official": {"input_usd_per_1m": "10.0000", "output_usd_per_1m": "50.0000"},
                "sell": {"input_usd_per_1m": "1.2000", "output_usd_per_1m": "6.0000"},
            },
        }]},
    })
    pricing = choices[0]["pricing"]
    assert pricing["multiplier"] == "0.12"
    assert pricing["official"]["input_usd_per_1m"] == "10.0000"
    assert pricing["sell"]["output_usd_per_1m"] == "6.0000"
