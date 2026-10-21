"""会员卡层：把「开卡」变成一个有成本、有次数额度的可选分支。

为什么需要单独一层
------------------
麦当劳菜单里有 17 个 SKU 的 `currentPrice` 是「随单购麦金卡优惠」价，
只有开卡（或下单时加购卡）才成立，`originalPrice` 才是不开卡的真实价。
所以「100 元能买几个」有两个完全不同的答案：

    不开卡：按 originalPrice 算，可无限买
    开卡：  先付卡费，再按会员价买，但会员价有**次数上限**

接口里能拿到什么、拿不到什么
----------------------------
能拿到：
- `withOrder = {membershipCode, specId}`：卡的身份（实测门店全是 106 / 880）
- `discountType = "随单购麦金卡优惠"`：卡的名字
- 会员价与原价的差（菜单里两个字段都有）
拿不到：
- **卡费**。`query-meals` / `calculate-price` 都不返回卡的价格，
  也没有任何会员卡查询工具。所以卡费必须由用户给（`--card-price`），
  默认值只是公开资料里的常见档位，页面与报告都会标明「可改」。
- 会员价在 `calculate-price` 里对未持卡账号不生效，因此会员价方案
  **无法官方核价**，只能以菜单标价为准，这一点必须如实标注。

次数上限
--------
官方权益页写明「超值四件套每日最高共可享 2 次，每次限一份」。
所以会员价不是无限用，本工具默认按 `uses_per_period = 2` 建模（`--card-uses` 可改）。
这条约束决定了「开卡是否划算」的答案，忽略它会把结果吹得很高。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CARD_NAME_RE = re.compile(r"随单购(.+?)(?:优惠|权益)?$")


@dataclass
class Card:
    """一张需要花钱开的会员卡。"""

    key: str                 # 内部标识，形如 "106:880"
    name: str                # 麦金卡
    membership_code: str
    spec_id: str
    fee: int                 # 开卡费（分），用户给
    uses: int = 2            # 会员价在一个周期内可用几次（0 = 不限）
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}（卡费 ¥{self.fee / 100:.2f}，会员价限 {self.uses} 次）"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "membership_code": self.membership_code,
            "spec_id": self.spec_id,
            "fee": self.fee,
            "uses": self.uses,
        }


def _card_name(discount: str | None) -> str:
    if not discount:
        return "会员卡"
    hit = CARD_NAME_RE.search(discount)
    return hit.group(1) if hit else discount


def detect_cards(meals, fee: int = 1900, uses: int = 2) -> tuple[dict[str, Card], dict[str, str]]:
    """从菜单里发现「需要开卡才成立」的商品。

    返回 (cards, card_of)，其中 card_of 是 mealCode → card.key。
    卡的身份完全来自接口，不靠猜；只有卡费是外部输入。
    """
    cards: dict[str, Card] = {}
    card_of: dict[str, str] = {}
    for meal in meals.values():
        if not meal.requires_card or not meal.with_order:
            continue
        code = str(meal.with_order.get("membershipCode") or "")
        spec = str(meal.with_order.get("specId") or "")
        if not code:
            continue
        key = f"{code}:{spec}"
        if key not in cards:
            cards[key] = Card(
                key=key,
                name=_card_name(meal.discount),
                membership_code=code,
                spec_id=spec,
                fee=fee,
                uses=uses,
            )
        card_of[meal.code] = key
    return cards, card_of


def member_gain(bundles, plan) -> tuple[int, int]:
    """这条方案里，会员价相比「同内容的最低原价」省了多少。

    必须按同内容比，不能拿全店最大的差价来吹：
    分享餐省 68 元，但它里面没有你要囤的板烧鸡腿堡。
    返回 (合计省下, 单次最大省下)，用来算卡费回本线。
    """
    plain_min: dict[tuple, int] = {}
    for bundle in bundles:
        if bundle.card or bundle.coupon or bundle.one_shot:
            continue
        current = plain_min.get(bundle.contents)
        if current is None or bundle.price < current:
            plain_min[bundle.contents] = bundle.price

    total = 0
    best = 0
    for bundle, qty in plan.items:
        if not bundle.card:
            continue
        base = plain_min.get(bundle.contents)
        if base is None or base <= bundle.price:
            continue
        gain = base - bundle.price
        total += gain * qty
        best = max(best, gain)
    return total, best
