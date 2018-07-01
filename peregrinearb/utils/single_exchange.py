import asyncio
import math
import networkx as nx
from ccxt import async as ccxt
import datetime
import logging
from peregrinearb.settings import LOGGING_PATH
from peregrinearb.utils import format_for_log


class LoadExchangeGraphAdapter(logging.LoggerAdapter):

    def __init__(self, logger, extra):
        super(LoadExchangeGraphAdapter, self).__init__(logger, extra)

    def process(self, msg, kwargs):
        return 'Invocation#{} - Exchange#{} - {}'.format(self.extra['count'], self.extra['exchange'], msg), kwargs


class FeesNotAvailable(Exception):
    pass


file_logger = logging.getLogger(LOGGING_PATH + __name__)


def create_exchange_graph(exchange: ccxt.Exchange):
    """
    Returns a simple graph representing exchange. Each edge represents a market.

    exchange.load_markets() must have been called. Will throw a ccxt error if it has not.
    """
    graph = nx.Graph()
    for market_name in exchange.symbols:
        try:
            base_currency, quote_currency = market_name.split('/')
        # if ccxt returns a market in incorrect format (e.g FX_BTC_JPY on BitFlyer)
        except ValueError:
            continue

        graph.add_edge(base_currency, quote_currency, market_name=market_name)

    return graph


async def load_exchange_graph(exchange, name=True, fees=False, suppress=None, depth=False, tickers=None,
                              invocation_id=0) -> nx.DiGraph:
    """
    Returns a Networkx DiGraph populated with the current ask and bid prices for each market in graph (represented by
    edges). If depth, also adds an attribute 'depth' to each edge which represents the current volume of orders
    available at the price represented by the 'weight' attribute of each edge.
    """
    if suppress is None:
        suppress = ['markets']
    if name:
        adapter = LoadExchangeGraphAdapter(file_logger, {'count': invocation_id, 'exchange': exchange})
        exchange = getattr(ccxt, exchange)()
    else:
        adapter = LoadExchangeGraphAdapter(file_logger, {'count': invocation_id, 'exchange': exchange.id})

    await exchange.load_markets()

    adapter.info('Loading exchange graph')

    if tickers is None:
        adapter.info('Fetching tickers')
        tickers = await exchange.fetch_tickers()
        adapter.info('Fetched tickers')

    adapter.debug('Initializing empty graph with exchange_name and timestamp attributes')
    graph = nx.DiGraph()

    # todo: get exchange's server time?
    graph.graph['exchange_name'] = exchange.id
    graph.graph['timestamp'] = datetime.datetime.now()
    adapter.debug('Initialized empty graph with exchange_name and timestamp attributes')

    adapter.info('Adding market data to graph')
    tasks = [_add_weighted_edge_to_graph(exchange, market_name, graph, log=True, fees=fees, suppress=suppress,
                                         ticker=ticker, depth=depth, invocation_id=invocation_id)
             for market_name, ticker in tickers.items()]
    await asyncio.wait(tasks)
    adapter.info('Added data to graph')

    adapter.info('Closing connection')
    await exchange.close()
    adapter.info('Closed connection')

    file_logger.info('Loaded exchange graph')
    return graph


async def populate_exchange_graph(graph: nx.Graph, exchange: ccxt.Exchange, log=True, fees=False, suppress=None,
                                  depth=False) -> nx.DiGraph:
    """
    Returns a Networkx DiGraph populated with the current ask and bid prices for each market in graph (represented by
    edges)
    """
    if suppress is None:
        suppress = ['markets']
    result = nx.DiGraph()

    tasks = [_add_weighted_edge_to_graph(exchange, edge[2]['market_name'], result, log, fees=fees, suppress=suppress,
                                         depth=depth)
             for edge in graph.edges(data=True)]
    await asyncio.wait(tasks)
    await exchange.close()

    return result


async def _add_weighted_edge_to_graph(exchange: ccxt.Exchange, market_name: str, graph: nx.DiGraph, log=True, fees=False,
                                      suppress=None, ticker=None, depth=False, invocation_id=0):
    """
    todo: add global variable to bid_volume/ ask_volume to see if all tickers (for a given exchange) have value == None
    Returns a Networkx DiGraph populated with the current ask and bid prices for each market in graph (represented by
    edges).
    :param exchange: A ccxt Exchange object
    :param market_name: A string representing a cryptocurrency market formatted like so:
    '{base_currency}/{quote_currency}'
    :param graph: A Networkx DiGraph upon
    :param log: If the edge weights given to the graph should be the negative logarithm of the ask and bid prices. This
    is necessary to calculate arbitrage opportunities.
    :param fee: The fee applied to the base currency represented as a decimal.
    :param suppress: A list or set which tells which types of warnings to not throw. Accepted elements are 'markets'.
    :param ticker: A dictionary representing a market as returned by ccxt's Exchange's fetch_ticker method
    :param depth: If True, also adds an attribute 'depth' to each edge which represents the current volume of orders
    available at the price represented by the 'weight' attribute of each edge.
    """
    adapter = LoadExchangeGraphAdapter(file_logger, {'count': invocation_id, 'exchange': exchange})
    adapter.debug(format_for_log('Adding edge to graph', market=market_name))
    if ticker is None:
        try:
            ticker = await exchange.fetch_ticker(market_name)
        # any error is solely because of fetch_ticker
        except:
            if 'markets' not in suppress:
                file_logger.warning(format_for_log('Market is unavailable at this time. It will not be included '
                                                   'in the graph.', market=market_name))
            return

    if fees:
        if 'taker' in exchange.markets[market_name]:
            # we always take the taker side because arbitrage depends on filling orders
            fee = exchange.fees['trading']['taker']
        else:
            if 'fees' not in suppress:
                adapter.warning("The fees for {} have not yet been implemented into ccxt's uniform API."
                                .format(exchange))
                raise FeesNotAvailable('Fees are not available for {} on {}'.format(market_name, exchange.id))
            else:
                fee = 0.002
    else:
        fee = 0

    fee_scalar = 1 - fee

    try:
        ticker_bid = ticker['bid']
        ticker_ask = ticker['ask']
        if depth:
            bid_volume = ticker['bidVolume']
            ask_volume = ticker['askVolume']
            if bid_volume is None:
                file_logger.warning(format_for_log('Market is unavailable because its bid volume was given as None. '
                                                   'It will not be included in the graph.', market=market_name))
                return
            if ask_volume is None:
                file_logger.warning(format_for_log('Market is unavailable because its ask volume was given as None. '
                                                   'It will not be included in the graph.', market=market_name))
                return
    # ask and bid == None if this market is non existent.
    except TypeError:
        file_logger.warning(format_for_log('Market is unavailable at this time. It will not be included in the graph.',
                                           market=market_name))
        return

    # Exchanges give asks and bids as either 0 or None when they do not exist.
    # todo: should we account for exchanges upon which an ask exists but a bid does not (and vice versa)? Would this
    # cause bugs?
    if ticker_ask == 0 or ticker_bid == 0 or ticker_ask is None or ticker_bid is None:
        file_logger.warning(format_for_log('Market is unavailable at this time. It will not be included in the graph.',
                                           market=market_name))
        return
    try:
        base_currency, quote_currency = market_name.split('/')
    # if ccxt returns a market in incorrect format (e.g FX_BTC_JPY on BitFlyer)
    except ValueError:
        if 'markets' not in suppress:
            file_logger.warning(format_for_log('Market is unavailable at this time due to incorrect formatting. '
                                               'It will not be included in the graph.', market=market_name))
        return

    if log:
        if depth:
            graph.add_edge(base_currency, quote_currency, weight=-math.log(fee_scalar * ticker_bid),
                           depth=-math.log(bid_volume))
            graph.add_edge(quote_currency, base_currency, weight=-math.log(fee_scalar * 1 / ticker_ask),
                           depth=-math.log(ask_volume))
        else:
            graph.add_edge(base_currency, quote_currency, weight=-math.log(fee_scalar * ticker_bid))
            graph.add_edge(quote_currency, base_currency, weight=-math.log(fee_scalar * 1 / ticker_ask))
    else:
        if depth:
            graph.add_edge(base_currency, quote_currency, weight=fee_scalar * ticker_bid, depth=bid_volume)
            graph.add_edge(quote_currency, base_currency, weight=fee_scalar * 1 / ticker_ask, depth=ask_volume)
        else:
            graph.add_edge(base_currency, quote_currency, weight=fee_scalar * ticker_bid)
            graph.add_edge(quote_currency, base_currency, weight=fee_scalar * 1 / ticker_ask)

    adapter.debug(format_for_log('Added edge to graph', market=market_name))
