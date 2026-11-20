import pytest
from gold_trader.core.strategy_engine import StrategyConfig


def test_strategy_config_enable_ml_filter():
    cfg = StrategyConfig(symbol="XAUUSD")
    assert cfg.enable_ml_filter is False

    cfg_ml = StrategyConfig(symbol="XAUUSD", enable_ml_filter=True)
    assert cfg_ml.enable_ml_filter is True
    cfg_ml.validate()
