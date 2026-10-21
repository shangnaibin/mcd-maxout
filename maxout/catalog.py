"""把门店菜单变成「可组合的购买单元」（bundle）。

一个 bundle 就是一笔可以真实下单的最小购买单位：
- 单品：一个鸡腿堡
- 套餐：三件套（轮次 1 选 1 + 轮次 2 选 1 + …）
- 随心配：蓝区 1 件 + 粉区 1 件，选不同商品要加减差价

只有把套餐拆到这个粒度，才能回答「100 元最多能买几个」——
因为一个套餐里可能就藏着一个目标商品，而它的单位成本远比单买低。
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Iterable

COMBO_HINTS = ("套餐", "件套", "随心配", "随心选", "拼", "组合", "分享餐", "双人餐", "四件套", "三件套")
MAX_VARIANTS_PER_COMBO = 4000
MAX_CHOICES_PER_ROUND = 10

# 套餐轮次里的选项有时并不在 query-meals 的菜单字典里（例如部分饮料、加料），
# 它们只在 query-meal-detail 里出现过。这里顺手记下名字，
# 导出网页快照时就能显示「小杯玉米杯」而不是裸编码。
KNOWN_NAMES: dict[str, str] = {}


def money(text) -> int:
    """把 "23.5" / "+ ¥0" / "- ¥1" 一律解析成「分」。"""
    if text is None:
        return 0
    if isinstance(text, (int, float)):
        return int(round(float(text) * 100))
    raw = str(text)
    value = re.search(r"(\d+(?:\.\d+)?)", raw)
    if not value:
        return 0
    cents = int(round(float(value.group(1)) * 100))
    return -cents if "-" in raw else cents


def fmt(cents: int) -> str:
    return f"¥{cents / 100:.2f}"


@dataclass
class Meal:
    code: str
    name: str
    price: int
    original: int
    tags: tuple[str, ...] = ()
    discount: str | None = None
    can_with_order: bool = False
    with_order: dict | None = None

    @property
    def requires_card(self) -> bool:
        """菜单标价是不是「随单购卡」专属价。

        这类商品的 currentPrice 只有在下单时同时带上 withOrder（麦金卡 / 早餐卡）
        才成立，裸价往往贵得多（实测鱼牛汉堡四件套随心选：菜单 32.00，裸买 59.50）。
        """
        if not self.can_with_order or not self.with_order:
            return False
        return bool(self.discount and "随单购" in self.discount)


@dataclass
class Bundle:
    kind: str                      # single | combo | coupon
    sku: str
    label: str
    price: int                     # 分
    contents: tuple[tuple[str, int], ...]      # 内容清单
    rounds: tuple[tuple[int, str], ...] = ()   # 套餐轮次选择，用于官方核价
    note: str = ""
    with_order: dict | None = None             # 需要随单购卡时带上，否则价格不成立
    coupon: "Coupon | None" = None             # 这一单靠哪张券拿到这个价
    one_shot: bool = False                     # 有限次数的券 → 只能被选中有限的次数
    uses: int = 1                              # 这个单元一次消耗几张券（二进制分解后的块大小）
    card: str | None = None                    # 会员价单元：属于哪张卡（卡 key），None 表示原价

    @property
    def codes(self) -> set[str]:
        return {code for code, _ in self.contents}

    def count_of(self, code: str) -> int:
        return sum(qty for item, qty in self.contents if item == code)


def find_store(client, city: str, keyword: str, be_type: int = 1) -> dict:
    stores = client.call(
        "query-nearby-stores", beType=be_type, searchType=2, city=city, keyword=keyword
    )
    if not stores:
        raise LookupError(f"{city} {keyword} 附近没找到营业门店")
    return stores[0]


def load_menu(client, store_code: str, order_type: int = 1, be_type: int = 1) -> dict[str, Meal]:
    data = client.call(
        "query-meals", storeCode=store_code, orderType=order_type, beType=be_type
    )
    meals: dict[str, Meal] = {}
    for code, raw in (data or {}).get("meals", {}).items():
        meals[code] = Meal(
            code=code,
            name=raw.get("name", code),
            price=money(raw.get("currentPrice")),
            original=money(raw.get("originalPrice") or raw.get("currentPrice")),
            tags=(),
            discount=raw.get("discountType"),
            can_with_order=bool(raw.get("canWithOrder")),
            with_order=raw.get("withOrder"),
        )
    # 把分类里的标签挂回去，便于按「单品 / 套餐」筛选
    tag_map: dict[str, tuple[str, ...]] = {}
    for category in (data or {}).get("categories", []):
        for entry in category.get("meals", []):
            tag_map.setdefault(entry["code"], tuple(entry.get("tags") or ()))
    for code, tags in tag_map.items():
        if code in meals:
            meals[code].tags = tags
    return meals


def is_combo(meal: Meal) -> bool:
    if any(word in meal.name for word in COMBO_HINTS):
        return True
    return any(tag in {"套餐", "随心配", "随心选", "小食拼盘"} for tag in meal.tags)


def _pick_choices(choices: list[dict], target_code: str | None) -> list[dict]:
    """轮次内做一次保守裁剪，避免组合爆炸，同时保证不会漏掉含目标商品的选项。"""
    if len(choices) <= MAX_CHOICES_PER_ROUND:
        return choices
    keep: list[dict] = []
    for choice in choices:
        if target_code and choice.get("code") == target_code:
            keep.append(choice)
        elif choice.get("isDefault") == 1:
            keep.append(choice)
    rest = sorted(
        (c for c in choices if c not in keep),
        key=lambda c: money(c.get("diffPrice")),
    )
    for choice in rest:
        if len(keep) >= MAX_CHOICES_PER_ROUND:
            break
        keep.append(choice)
    return keep


def expand_combo(client, meal: Meal, target_code: str | None, store_code: str,
                 order_type: int, be_type: int, base_price: int | None = None,
                 card: str | None = None, detail: dict | None = None) -> list[Bundle]:
    """展开一个套餐的所有选法。

    base_price 用来生成「同一套餐的另一种价格」：
    随单购卡的商品，菜单标价是会员价，原价在 originalPrice 里。
    两者差价构成一轮选择，所以必须分别展开，才能公平比较「开卡 vs 不开卡」。
    detail 可传入已拉取的套餐详情，避免同一 SKU 为了两种价格请求两次。
    """
    if detail is None:
        detail = client.call(
            "query-meal-detail",
            storeCode=store_code,
            orderType=order_type,
            beType=be_type,
            code=meal.code,
        )
    rounds = [r for r in (detail or {}).get("rounds", []) if r.get("choices")]
    if not rounds:
        return []
    per_round = [_pick_choices(r["choices"], target_code) for r in rounds]
    total = 1
    for group in per_round:
        total *= len(group)
    if total > MAX_VARIANTS_PER_COMBO:
        per_round = [group[:4] for group in per_round]

    base = meal.price if base_price is None else base_price
    bundles: list[Bundle] = []
    for combo in itertools.product(*per_round):
        price = base + sum(money(choice.get("diffPrice")) for choice in combo)
        if price <= 0:
            continue
        counts: dict[str, int] = {}
        for choice in combo:
            KNOWN_NAMES.setdefault(choice["code"], choice["name"])
            counts[choice["code"]] = counts.get(choice["code"], 0) + 1
        label = meal.name
        if len(combo) <= 3:
            label += "（" + "/".join(c["name"] for c in combo) + "）"
        if card:
            label += "（会员价）"
        elif base_price is not None:
            label += "（原价）"
        bundles.append(
            Bundle(
                kind="combo",
                sku=meal.code,
                label=label,
                price=price,
                contents=tuple(sorted(counts.items())),
                rounds=tuple((int(r["id"]), c["code"]) for r, c in zip(rounds, combo)),
                note=meal.name,
                with_order=meal.with_order if card else None,
                card=card,
            )
        )
    return bundles


def build_bundles(client, store_code: str, meals: dict[str, Meal], target_code: str | None,
                  order_type: int = 1, be_type: int = 1,
                  combo_codes: Iterable[str] | None = None,
                  allow_card: bool = False,
                  card_of: dict[str, str] | None = None,
                  progress=None) -> tuple[list[Bundle], dict]:
    """返回 (bundles, 统计信息)。

    「随单购卡」商品会同时产出两套购买单元：
    - 原价单元（card=None，price=originalPrice）——不开卡的方案用它
    - 会员价单元（card=卡 key，price=菜单标价）——开卡的方案用它，消耗卡的次数额度

    必须两套都留着，否则「开卡到底划不划算」就没有对照组。
    allow_card=False 时只保留原价单元（旧行为：整类商品被排除，现在改成按原价纳入，
    这样不算卡也不会漏掉真实能买的东西）。
    """
    card_of = card_of or {}
    bundles: list[Bundle] = []
    card_skus: list[str] = []
    for meal in meals.values():
        if meal.price <= 0:
            continue
        key = card_of.get(meal.code) if meal.requires_card else None
        if key:
            card_skus.append(meal.name)
            # 原价：同一件商品不开卡真实要付的钱
            bundles.append(
                Bundle(
                    kind="single",
                    sku=meal.code,
                    label=f"{meal.name}（原价）",
                    price=meal.original,
                    contents=((meal.code, 1),),
                )
            )
            if allow_card:
                bundles.append(
                    Bundle(
                        kind="single",
                        sku=meal.code,
                        label=f"{meal.name}（会员价）",
                        price=meal.price,
                        contents=((meal.code, 1),),
                        with_order=meal.with_order,
                        card=key,
                    )
                )
            continue
        bundles.append(
            Bundle(
                kind="single",
                sku=meal.code,
                label=meal.name,
                price=meal.price,
                contents=((meal.code, 1),),
            )
        )

    if combo_codes is None:
        combo_codes = [m.code for m in meals.values() if is_combo(m) and m.price > 0]
    combo_codes = list(combo_codes)

    expanded = 0
    failed: list[str] = []
    for index, code in enumerate(combo_codes, 1):
        meal = meals.get(code)
        if not meal:
            continue
        key = card_of.get(meal.code) if meal.requires_card else None
        try:
            if key:
                detail = client.call(
                    "query-meal-detail",
                    storeCode=store_code,
                    orderType=order_type,
                    beType=be_type,
                    code=meal.code,
                )
                # 原价套餐：选法相同，只是基础价换成 originalPrice
                variants = expand_combo(client, meal, target_code, store_code,
                                        order_type, be_type, base_price=meal.original,
                                        detail=detail)
                if allow_card:
                    variants += expand_combo(client, meal, target_code, store_code,
                                             order_type, be_type, card=key, detail=detail)
            else:
                variants = expand_combo(client, meal, target_code, store_code,
                                        order_type, be_type)
        except Exception as exc:  # 单个套餐解析失败不该拖垮整体
            failed.append(f"{meal.name}: {exc}")
            continue
        bundles.extend(variants)
        expanded += 1
        if progress:
            progress(index, len(combo_codes), meal.name, len(variants))

    # 同价同内容去重（会员价与原价可能撞价，保留带卡的更合理）
    seen: dict[tuple, Bundle] = {}
    for bundle in bundles:
        key = (bundle.price, bundle.contents, bundle.card)
        if key not in seen:
            seen[key] = bundle
    stats = {
        "singles": sum(1 for b in bundles if b.kind == "single"),
        "combos_expanded": expanded,
        "combo_variants": sum(1 for b in bundles if b.kind == "combo"),
        "unique": len(seen),
        "failed": failed,
        "skipped_card": [],
        "card_skus": card_skus,
        "member_units": sum(1 for b in bundles if b.card),
    }
    return list(seen.values()), stats
