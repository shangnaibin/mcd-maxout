"""官方核价：把求解器给出的方案交给 calculate-price 真实算一遍。

求解器用的是菜单标价 + 券面价格，这一步用麦当劳自己的计价接口复核，
确保「100 元能买 N 个」「这张券真的省这么多」不是估出来的。

两点设计上必须坚持的原则：
1. **预演券不核价**。只有拿到真实 couponCode / couponId 的券才拿去核价，
   否则接口按原价算回来，得出一堆假差异，反而误导人。
2. **逐行比对**，不只看总额。券是绑在商品行上的，总额对上但某一行错了，
   说明券用错了位置。
"""
from __future__ import annotations

from .catalog import Bundle, fmt
from .solver import Plan


def _line_key(bundle: Bundle) -> tuple:
    coupon = (bundle.coupon.code, bundle.coupon.coupon_id) if bundle.coupon else ("", "")
    return bundle.sku, bundle.rounds, coupon


def _items_payload(plan: Plan) -> list[dict]:
    """同一个商品可能既有券行又有原价行，必须分行传，不能合并数量。"""
    merged: dict[tuple, dict] = {}
    for bundle, qty in plan.items:
        key = _line_key(bundle)
        if key in merged:
            merged[key]["quantity"] += qty
            continue
        item = {"productCode": bundle.sku, "quantity": qty}
        if bundle.with_order:
            item["withOrder"] = {
                "membershipCode": bundle.with_order.get("membershipCode"),
                "membershipSpecId": bundle.with_order.get("specId"),
            }
        if bundle.rounds:
            grouped: dict[int, list[dict]] = {}
            for round_id, code in bundle.rounds:
                grouped.setdefault(round_id, []).append({"code": code, "quantity": 1})
            item["roundList"] = [
                {"round": str(round_id), "comboItemList": codes}
                for round_id, codes in sorted(grouped.items())
            ]
        if bundle.coupon and bundle.coupon.code:
            item["couponCode"] = bundle.coupon.code
        if bundle.coupon and bundle.coupon.coupon_id:
            item["couponId"] = bundle.coupon.coupon_id
        merged[key] = item
    return list(merged.values())


def _find_amount(data) -> int | None:
    """在计价返回里找实付总额（单位：分）。"""
    preferred = ("price", "totalAmount", "payAmount", "totalPrice", "actualAmount")
    if not isinstance(data, dict):
        return None
    for key in preferred:
        value = data.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(round(value))
        if isinstance(value, str) and value.replace(".", "").isdigit():
            return int(round(float(value)))
    for value in data.values():
        if isinstance(value, dict):
            found = _find_amount(value)
            if found:
                return found
    return None


def has_unverified_coupons(plan: Plan) -> bool:
    """方案里有没有「没有券码」的预演券。"""
    return any(b.coupon and not b.coupon.usable for b, _ in plan.items)


def has_member_units(plan: Plan) -> bool:
    """方案里有没有会员价单元。

    会员价只在账号真的持卡时才生效：实测未持卡账号传 withOrder，
    calculate-price 一律按原价算（菜单价 28 的四件套返回 57.50）。
    所以带会员价的方案不能官方核价，只能以菜单标价为准并如实标注。
    """
    return plan.card_uses > 0


def verify(client, plan: Plan, store_code: str, order_type: int = 1, be_type: int = 1) -> dict:
    if has_unverified_coupons(plan):
        names = [b.coupon.name for b, _ in plan.items if b.coupon and not b.coupon.usable]
        return {
            "skipped": True,
            "reason": "方案里的优惠没有真实券码（预演券 / 本地活动规则），"
            "官方接口会以「未找到优惠券或已赠送给好友」拒绝",
            "preview_coupons": names,
        }

    if has_member_units(plan):
        return {
            "skipped": True,
            "reason": f"方案包含{plan.card_name or '会员卡'}会员价 {plan.card_uses} 次；"
            "未持卡账号调官方计价只会返回原价，核出来全是假差异，"
            "会员价部分以菜单标价为准",
        }

    payload = _items_payload(plan)
    data = client.call(
        "calculate-price",
        storeCode=store_code,
        orderType=order_type,
        beType=be_type,
        items=payload,
    )
    amount = _find_amount(data)
    lines = []
    for line in (data or {}).get("productList", []) or []:
        lines.append(
            {
                "code": line.get("productCode"),
                "name": line.get("productName"),
                "qty": line.get("quantity"),
                "original": line.get("originalSubtotal"),
                "pay": line.get("subtotal"),
            }
        )
    return {
        "skipped": False,
        "amount": amount,
        "matches_local": amount == plan.spend if amount else None,
        "official_discount": (data or {}).get("discount"),
        "lines": lines,
        "raw_keys": sorted(data.keys()) if isinstance(data, dict) else [],
        "data": data,
    }


def render_verification(result: dict, plan: Plan) -> str:
    if result.get("skipped"):
        return f"官方核价：跳过 —— {result['reason']}"
    amount = result.get("amount")
    if amount is None:
        return "官方核价：接口已返回，但没识别出金额字段，请看原始数据。"
    verdict = "一致 ✅" if result["matches_local"] else f"有差异（本地算出 {fmt(plan.spend)}）"
    text = f"官方核价：{fmt(amount)}　{verdict}"
    discount = result.get("official_discount")
    if discount:
        text += f"　（官方优惠 {fmt(discount)}）"
    return text
