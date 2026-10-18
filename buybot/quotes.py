"""Quote assets the bot can value in dollars, and their rates.

A pool pairs a token with a quote asset. Dollars are shown only for quotes on this
list, matched by address: anyone can deploy a token called "USDC" or "NVDA" (several
fake USDCs trade on the chain), and a fake quote must not produce fake dollars.

- ETH and WETH: the ETH/USD ticker (prices.EthUsd);
- USDG: one dollar;
- Robinhood stock tokens: their Chainlink feed on the chain itself. The addresses come
  from Robinhood's asset registry (https://api.robinhood.com/rhj/assets), the feeds
  from Chainlink's list for Robinhood Chain; `tools/preflight.py` re-checks each pair
  live. A feed's price already includes the token's share multiplier, so it is the
  price of one token. Stock feeds pause when the market is closed, so a price up to
  four days old (a long weekend) still counts; an older one does not.
"""

import logging
import time

from . import abi

log = logging.getLogger(__name__)

NATIVE = abi.ZERO_ADDRESS
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"

SEL_LATEST_ROUND = "0xfeaf968c"  # latestRoundData() on a Chainlink feed
FEED_DECIMALS = 8                 # every USD feed on the list; preflight checks it
FEED_MAX_AGE = 4 * 86400

# Robinhood stock tokens (18 decimals) that have a Chainlink USD feed: token -> (symbol, feed)
STOCKS = {
    "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9": ("AAPL", "0x6b22a786baa607d76728168703a39ea9c99f2cd0"),
    "0x86923f96303d656e4aa86d9d42d1e57ad2023fdc": ("AMD", "0x943a29e7ae51a4798823ca9eed2ed533b2a22c72"),
    "0x12f190a9f9d7d37a250758b26824b97ce941bf54": ("AMZN", "0xd5a1508ced74c084ebf3cbe853e2c968fb2a651c"),
    "0x47f93d52cbec7c6d2cfc080e154002370a60daea": ("ASML", "0xb4106147e8cce40b7d46124090d373a71b70f87d"),
    "0xad25ac6c84d497db898fa1e8387bf6af3532a1c4": ("BABA", "0x62cc8f9b5f56a33c9c8a60c8b92779f523c4e984"),
    "0xcbb95bbf36099d34da091dc6fa6f49efa257cee3": ("CLSK", "0x810c12d3a554bc47fd39597fe3b3aac4941f50ef"),
    "0x6330d8c3178a418788df01a47479c0ce7ccf450b": ("COIN", "0xa3a468a452940b7d6b69991207b508c609a98ef2"),
    "0xdf0992e440dd0be65bd8439b609d6d4366bf1cb5": ("CRCL", "0x6652edf64ba3731c4f2d3ce821a0fb1f1f6b482a"),
    "0x5f10a1c971b69e47e059e1dc91901b59b3fb49c3": ("CRWV", "0xe1b3aabcafad1c94708dc1367dcff8aa4407487c"),
    "0x941ae714ec6d8130c7b75d67160ca08f1e7d11dd": ("DELL", "0x1c6c8cadbe02e19129c39ddb92281ce4c0bf206b"),
    "0x7f0abef0c07280f82c6a08ead09ded6bae2c13fc": ("EWY", "0xefdf54610b62a7753ec30bdc380847c12d32e1d1"),
    "0x1b0e319c6a659f002271b69db8a7df2f911c153e": ("GME", "0x27c71df6a64fb476468edf256cf72c038bab5b67"),
    "0x2e0847e8910a9732eb3fb1bb4b70a580adad4fe3": ("GOOGL", "0xf6f373a037c30f0e5010d854385ca89185ae638b"),
    "0xc72b96e0e48ecd4dc75e1e45396e26300bc39681": ("INTC", "0x3f390c5c24628ac7c489515402235fead71d1913"),
    "0x558378e000d634a36593e338ebacdd6207640efe": ("IONQ", "0x22efec4919baf55f360e0edee4abeb26de4971eb"),
    "0xc0d6457c16cc70d6790dd43521c899c87ce02f35": ("META", "0x7c38c00c30bee9378381e7b6135d7283356d71b1"),
    "0xe93237c50d904957cf27e7b1133b510c669c2e74": ("MSFT", "0x45c3c877c15e6ba2ebb19ea114ea508d14c1af2e"),
    "0xec262a75e413fafd0df80480274532c79d42da09": ("MSTR", "0x396118bdfb181e6240e74d243f266b061c0edc3d"),
    "0xff080c8ce2e5feadaca0da81314ae59d232d4afd": ("MU", "0x425eefdcf05ed6526c3ce61af99429a228a6d596"),
    "0x9d9c6684f596f66a64c030b93a886d51fd4d7931": ("NBIS", "0xe1d87b116ba0fe898998f1d140339d1fa1e09705"),
    "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec": ("NVDA", "0x379ec4f7c378f34a1b47e4f3cbebcbac3e8e9f15"),
    "0xb0992820e760d836549ba69bc7598b4af75dee03": ("ORCL", "0x0e6a64a2b58a6693a531e6c555f3a5d042eea844"),
    "0x894e1ec2d74ffe5aef8dc8a9e84686accb964f2a": ("PLTR", "0x820abedff239034956b7a9d2f0a331f9f075eb4c"),
    "0xd5f3879160bc7c32ebb4dc785f8a4f505888de68": ("QQQ", "0x80901d846d5d7b030f26b480776ee3b29374c2ae"),
    "0x284358abc07f9359f19f4b5b4ac91901be2597ba": ("RGTI", "0x2a045cf1c49c61c166c036d2f06fa2d2d984f765"),
    "0x3b14c39e89d60d627b42a1a4ca45b5bb45fc12e2": ("RKLB", "0x045477bf65aef6f4f2386ad0164579e48381cc74"),
    "0x92fd66527192e3e61d4ddd13322aa222de86f9b5": ("SGOV", "0xa0df4ee0fff975306345875e3548fcc519577a11"),
    "0x411efb0e7f985935daec3d4c3ebaea0d0ad7d89f": ("SLV", "0x209b73908e92ae021826ed79609845451ecba2ce"),
    "0xb90a19ff0af67f7779aff50a882a9cff42446400": ("SNDK", "0xfb133fa4b7b385802b693a293606682df47109a3"),
    "0x4a0e65a3eccec6dbe60ae065f2e7bb85fae35eea": ("SPCX", "0xb265810950ba6c5c0ff821c9963014a56fd8bffb"),
    "0x117cc2133c37b721f49de2a7a74833232b3b4c0c": ("SPY", "0x319724394d3a0e3669269846abe664cd621f9f6a"),
    "0x322f0929c4625ed5bad873c95208d54e1c003b2d": ("TSLA", "0x4a1166a659a55625345e9515b32adecea5547c38"),
    "0x58ffe4a942d3885baa22d7520691f611ef09e7aa": ("TSM", "0x874cf94aa8ec88fd9560094dd065f2fb3e41fc2f"),
    "0xd917b029c761d264c6a312bbbcda868658ef86a6": ("USAR", "0xa994d3684e8400a6c8078226925779fdee682dd9"),
    "0xa30fa36db767ad9ed3f7a60fc79526fb4d56d344": ("USO", "0x75a9c76ef439e2c7c2e5a34ab105ecfe3766431c"),
}

# address -> (symbol, decimals, kind); kind is "eth", "usd" or "feed:<feed address>"
QUOTES = {
    NATIVE: ("ETH", 18, "eth"),
    WETH: ("WETH", 18, "eth"),
    USDG: ("USDG", 6, "usd"),
    **{addr: (sym, 18, f"feed:{feed}") for addr, (sym, feed) in STOCKS.items()},
}


def quote_info(address: str) -> tuple[str, int, str] | None:
    return QUOTES.get(address.lower())


def decode_round(ret_hex: str) -> tuple[float, int] | None:
    """(answer in dollars, updatedAt) from latestRoundData, or None for a bad answer."""
    try:
        data = abi.to_bytes(ret_hex)
    except ValueError:
        return None
    answer, updated = abi.word_int(data, 1), abi.word_uint(data, 3)
    if answer is None or updated is None or answer <= 0 or updated == 0:
        return None
    return answer / 10 ** FEED_DECIMALS, updated


class Rates:
    """Dollars per unit of each quote kind: "eth", "usd", "feed:<address>".
    "other" (a quote not on the list) has no rate. Feed reads are cached for `ttl`."""

    def __init__(self, rpc, eth_usd, ttl: float = 60.0, max_age: float = FEED_MAX_AGE):
        self.rpc = rpc
        self.eth_usd = eth_usd
        self.ttl = ttl
        self.max_age = max_age
        self._feeds: dict[str, tuple[float, float, int]] = {}  # feed -> (usd, read at, updatedAt)

    def current(self, kind: str) -> float | None:
        """The last known rate, without touching the network."""
        if kind == "usd":
            return 1.0
        if kind == "eth":
            return self.eth_usd.current
        if kind.startswith("feed:"):
            hit = self._feeds.get(kind[5:])
            if hit and time.time() - hit[2] <= self.max_age:
                return hit[0]
        return None

    async def get(self, kind: str) -> float | None:
        if kind == "usd":
            return 1.0
        if kind == "eth":
            return await self.eth_usd.get()
        if not kind.startswith("feed:"):
            return None
        feed = kind[5:]
        hit = self._feeds.get(feed)
        if hit is None or time.time() - hit[1] > self.ttl:
            try:
                got = decode_round(await self.rpc.eth_call(feed, SEL_LATEST_ROUND))
            except Exception as e:  # a failed read keeps the last value until it is too old
                log.info("feed %s unreadable: %s", feed, e)
                got = None
            if got:
                self._feeds[feed] = (got[0], time.time(), got[1])
        return self.current(kind)

    async def table(self, kinds) -> dict[str, float | None]:
        return {k: await self.get(k) for k in set(kinds)}


def usd_value(amount: float | None, kind: str, rates: dict) -> float | None:
    if amount is None:
        return None
    rate = rates.get(kind)
    return amount * rate if rate else None
