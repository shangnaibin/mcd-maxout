"""把一次真实运行的结果导出成「可点击的网页」所需的离线快照。

为什么要离线快照，而不是让网页直接调 MCP：
1. MCP 需要 Bearer token。把 token 放进前端等于公开你的会员账号。
2. MCP 端点不允许浏览器跨域直接调用。
3. 求解只需要「菜单 + 套餐结构 + 券规则」这三样，它们在一天内几乎不变。

所以网页版是：**一次导出，永久可点**。快照里带生成时间与门店，
页面上也会明确标注「离线快照」，不会假装是实时价格。

快照极简编码（体积约 200KB，GitHub Pages 上 gzip 后 < 100KB）：
    meals        [[code, name, price, original, minUnitCost, isSingle], ...]
    singles      [[mealIdx, price], ...]
    combos       [[skuIdx, price, [[mealIdx, qty], ...]], ...]
    coupons      [{n,m,q,p,r,s,src,conf,u,kind}, ...]
    coupon_units [[skuIdx, price, [[mealIdx, qty], ...], couponIdx], ...]

商品名不存进购买单元，网页端用 meals 里的名字重建标签，
这样 9000 多个单元不会各自背着一段中文，体积能小一个数量级。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .catalog import KNOWN_NAMES

ASSET_DIR = Path(__file__).with_name("assets")
CST = timezone(timedelta(hours=8))


def _indexer():
    order: list[str] = []
    at: dict[str, int] = {}

    def take(code: str) -> int:
        if code not in at:
            at[code] = len(order)
            order.append(code)
        return at[code]

    return order, take


def build_snapshot(meals, singles, combos, coupons, coupon_units, *,
                   store_code: str, store_name: str, order_type: int, be_type: int,
                   stats: dict | None = None, coupon_source: str = "",
                   cards: dict | None = None) -> dict:
    """组装快照。singles / combos / coupon_units 都是 Bundle 列表。

    cards 是 {key: Card}；带卡的购买单元会多存一个「卡下标」，
    网页端据此把会员价单元单独归到开卡分支里，并按卡的次数额度求解。
    """
    order, take = _indexer()
    card_keys = list((cards or {}).keys())
    card_index = {key: index for index, key in enumerate(card_keys)}

    def card_of(bundle) -> int:
        return card_index.get(bundle.card, -1) if bundle.card else -1

    # 先给真实菜单编号，保证 meals 的顺序稳定（分类顺序）
    for meal in meals.values():
        take(meal.code)

    # 套餐里出现的选项可能不在菜单字典里（例如某些饮料），补一个占位条目
    unknown = 0
    for bundle in list(combos) + list(coupon_units):
        for code, _qty in bundle.contents:
            if code not in order:
                take(code)
                unknown += 1

    by_code = {meal.code: meal for meal in meals.values()}

    def enc(bundle):
        return [[take(code), qty] for code, qty in bundle.contents]

    # 注意：take() 会往 order 里追加新编码，所以必须先把所有编码登记完，
    # 最后再生成 rows，否则索引会和 rows 的物理位置错开。
    single_rows = [[take(b.contents[0][0]), b.price, card_of(b)] for b in singles]
    combo_rows = [[take(b.sku), b.price, enc(b), card_of(b)] for b in combos]

    coupon_rows = []
    for coupon in coupons:
        meal = by_code.get(coupon.target_code)
        coupon_rows.append({
            "n": coupon.name,
            "m": take(coupon.target_code),
            "mname": meal.name if meal else KNOWN_NAMES.get(coupon.target_code, coupon.target_code),
            "q": coupon.quantity,
            "p": coupon.price,
            "r": coupon.retail,
            "s": coupon.saving,
            "src": coupon.source,
            "conf": coupon.confidence,
            "u": bool(coupon.usable),
            "uses": coupon.uses,
            "kind": coupon.kind,
        })
    # 按对象身份对回券下标：一条券规则可能被二进制分解成多个块（1/2/4…），
    # 所以 coupon_units 和 coupons 早就不再等长、同序了，不能用 enumerate 的位置。
    coupon_index = {id(coupon): index for index, coupon in enumerate(coupons)}
    coupon_unit_rows = []
    for bundle in coupon_units:
        if not bundle.coupon:
            continue
        idx = coupon_index.get(id(bundle.coupon), -1)
        if idx < 0:
            continue
        coupon_unit_rows.append(
            [take(bundle.sku), bundle.price, enc(bundle), idx, bundle.uses, 1 if bundle.one_shot else 0]
        )

    # 到这里所有编码都登记完了，可以安全地按 order 生成 rows
    rows = []
    for code in order:
        meal = by_code.get(code)
        rows.append([
            code,
            meal.name if meal else KNOWN_NAMES.get(code, code),
            meal.price if meal else 0,
            meal.original if meal else 0,
            0,                       # minUnitCost 稍后填
            0,                       # isSingle 稍后填
        ])
    for row in single_rows:
        rows[row[0]][5] = 1

    # 每个商品的最低「到手单价」：单买价 vs 在任何套餐里的分摊价。
    # 会员价单元要排除——它要求先开卡，拿来当「走套餐能到多少」会误导。
    best = {}
    for row in single_rows:
        if row[2] >= 0:
            continue
        idx, price = row[0], row[1]
        best[idx] = min(best.get(idx, price), price)
    for bundle in combos:
        if bundle.card:
            continue
        for code, qty in bundle.contents:
            if qty <= 0:
                continue
            unit = bundle.price // qty
            idx = take(code)
            best[idx] = min(best.get(idx, unit), unit)
    for idx, value in best.items():
        rows[idx][4] = value

    stats = stats or {}
    return {
        "schema": 2,
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        "store": {"code": store_code, "name": store_name},
        "meta": {
            "sku": len(meals),
            "units": len(single_rows) + len(combo_rows) + len(coupon_unit_rows),
            "singles": len(single_rows),
            "combo_variants": len(combo_rows),
            "combos": stats.get("combos_expanded", 0),
            "order_type": order_type,
            "be_type": be_type,
            "coupon_source": coupon_source,
            "coupon_count": len(coupon_rows),
            "unknown_codes": unknown,
            "member_units": sum(1 for row in single_rows + combo_rows if row[-1] >= 0),
        },
        "cards": [
            {"n": cards[key].name, "fee": cards[key].fee, "uses": cards[key].uses}
            for key in card_keys
        ],
        "meals": rows,
        "singles": single_rows,
        "combos": combo_rows,
        "coupons": coupon_rows,
        "coupon_units": coupon_unit_rows,
    }


def write(out_dir: str | Path, snapshot: dict) -> tuple[Path, Path]:
    """写出 index.html + data.js，目录本身即可作为静态站点部署。"""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    html = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
    page = target / "index.html"
    page.write_text(html, encoding="utf-8")

    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    data = target / "data.js"
    data.write_text(
        "// 由 `python -m maxout --web` 生成，请勿手改\n"
        f"window.MCD_SNAPSHOT = {payload};\n",
        encoding="utf-8",
    )
    return page, data


def copy_assets(out_dir: str | Path) -> None:
    """把 assets 下除 index.html 之外的资源一起复制过去（目前没有，留作扩展）。"""
    target = Path(out_dir)
    for item in ASSET_DIR.iterdir():
        if item.is_file() and item.name != "index.html":
            shutil.copy2(item, target / item.name)
