from typing import (
    List,
    Tuple,
)

from hummingbot.client.config.global_config_map import global_config_map
from hummingbot.strategy.market_trading_pair_tuple import MarketTradingPairTuple
from hummingbot.strategy.liquidity_mirroring.liquidity_mirroring_market_pair import LiquidityMirroringMarketPair
from hummingbot.strategy.liquidity_mirroring.liquidity_mirroring import LiquidityMirroringStrategy
from hummingbot.strategy.liquidity_mirroring.liquidity_mirroring_config_map import liquidity_mirroring_config_map

def start(self):
    primary_market = liquidity_mirroring_config_map.get("primary_market").value.lower()
    mirrored_market = liquidity_mirroring_config_map.get("mirrored_market").value.lower()
    mirrored_trading_pair = liquidity_mirroring_config_map.get("market_trading_pair_to_mirror").value
    primary_trading_pair = liquidity_mirroring_config_map.get("primary_market_trading_pair").value
    two_sided_mirroring = liquidity_mirroring_config_map.get("two_sided_mirroring").value
    spread_percent = liquidity_mirroring_config_map.get("spread_percent").value
    max_exposure_base = liquidity_mirroring_config_map.get("max_exposure_base").value
    max_exposure_quote = liquidity_mirroring_config_map.get("max_exposure_quote").value
    max_loss = liquidity_mirroring_config_map.get("max_offset_loss").value
    max_total_loss = liquidity_mirroring_config_map.get("max_total_offset_loss").value
    equivalent_tokens = liquidity_mirroring_config_map.get("equivalent_tokens").value
    min_primary_amount = liquidity_mirroring_config_map.get("min_primary_amount").value
    min_mirroring_amount = liquidity_mirroring_config_map.get("min_mirroring_amount").value
    slack_hook = global_config_map.get("SLACK_HOOK").value
    
    bid_ratios_type = liquidity_mirroring_config_map.get("bid_amount_ratio_type").value
    if bid_ratios_type == "manual":
        bid_ratios = []
        denominations = liquidity_mirroring_config_map.get("bid_amount_ratios").value
        denominator = 0.0
        for summand in denominations:
            denominator += float(summand)
        
        if denominator == 0:
            self.logger().warning("empty bid ratio list!")
            return
        else:
            for summand in denominations:
                bid_ratios.append(float(summand)/denominator)
    else:
        bid_ratios = [float(1/55),float(2/55),float(3/55),float(4/55),float(5/55),float(6/55),
                               float(7/55),float(8/55),float(9/55),float(10/55)]

    ask_ratios_type = liquidity_mirroring_config_map.get("ask_amount_ratio_type").value                               
    if ask_ratios_type == "manual":
        ask_ratios = []
        denominations = liquidity_mirroring_config_map.get("ask_amount_ratios").value
        denominator = 0.0
        for summand in denominations:
            denominator += float(summand)
        
        if denominator == 0:
            self.logger().warning("empty ask ratio list!")
            return
        else:
            for summand in denominations:
                ask_ratios.append(float(summand)/denominator)
    else:
        ask_ratios = [float(1/55),float(2/55),float(3/55),float(4/55),float(5/55),float(6/55),
                               float(7/55),float(8/55),float(9/55),float(10/55)]

    try:
        primary_market_trading_pair: str = self._convert_to_exchange_trading_pair(primary_market, [primary_trading_pair])[0]
        mirrored_market_trading_pair: str = self._convert_to_exchange_trading_pair(mirrored_market, [mirrored_trading_pair])[0]
        primary_assets: List[Tuple[str, str]] = self._initialize_market_assets(primary_market, [primary_market_trading_pair])[0]
        secondary_assets: List[Tuple[str, str]] = self._initialize_market_assets(mirrored_market,
                                                                           [mirrored_market_trading_pair])[0]                                                                           
    except ValueError as e:
        self._notify(str(e))
        return
    
    for primary_asset in primary_assets:
        found = False
        for token_list in equivalent_tokens:
            if primary_asset.upper() in token_list:
                for mirror_asset in secondary_assets:
                    if mirror_asset.upper() in token_list:
                        found = True
        if not found:            
            self.logger().warning(f"{primary_asset} not equivalent to any mirrored asset")
            return

    market_names: List[Tuple[str, List[str]]] = [(primary_market, [primary_market_trading_pair]),
                                                 (mirrored_market, [mirrored_market_trading_pair])]
                                                 
    self._initialize_wallet(token_trading_pairs=list(set(primary_assets + secondary_assets)))
    self._initialize_markets(market_names)
    self.assets = set(primary_assets + secondary_assets)

    self.primary_market_trading_pair_tuples = [MarketTradingPairTuple(self.markets[primary_market], primary_market_trading_pair, primary_assets[0], primary_assets[1])]
    self.mirrored_market_trading_pair_tuples = [MarketTradingPairTuple(self.markets[mirrored_market], mirrored_market_trading_pair, secondary_assets[0], secondary_assets[1])]
    self.strategy = LiquidityMirroringStrategy(primary_market_pairs=self.primary_market_trading_pair_tuples,
                                               mirrored_market_pairs=self.mirrored_market_trading_pair_tuples,
                                               two_sided_mirroring=two_sided_mirroring,
                                               spread_percent=spread_percent,
                                               max_exposure_base=max_exposure_base,
                                               max_exposure_quote=max_exposure_quote,
                                               max_loss=max_loss,
                                               max_total_loss=max_total_loss,
                                               bid_amount_percents=bid_ratios,
                                               ask_amount_percents=ask_ratios,
                                               equivalent_tokens=equivalent_tokens,
                                               min_primary_amount=min_primary_amount,
                                               min_mirroring_amount=min_mirroring_amount,
                                               slack_hook=slack_hook,
                                               logging_options=LiquidityMirroringStrategy.OPTION_LOG_ALL)
