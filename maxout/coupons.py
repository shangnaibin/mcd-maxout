"""优惠券与活动层：把「券」和「活动」翻译成可以参与求解的购买单元。

关键事实（来自真实接口，不是猜的）：
- `calculate-price` / `create-order` 的 **每个商品行** 都可以带 `couponCode` + `couponId`，
  也就是说券是绑定在具体商品上的，而不是订单级的满减。
- 计价返回里每行都有 `originalSubtotal` / `subtotal`，所以「这张券到底省了多少」
  可以逐行核对，不用相信券名。

因此一张券在求解器眼里就是：**同样内容、更便宜、但消耗一张券**的购买单元。
真正卡住数量的不是「下几单」，而是「你手里有几张同款券」——
券是绑在商品行上的，同一笔订单里多行各带一张券在接口层面完全成立，
所以「分开下单」在价格上并不会更便宜，它只是让人误以为绕过了限制。

于是模型里的关键参数变成 `Coupon.uses`：这张券（这套活动规则）能用几次。
- `uses=1`（默认）：一张真券，用一次
- `uses=n`：能拿到 n 张同款券 / 活动可参与 n 次
- `uses=0`：不限次（只有纯活动规则才该这么设，真券不可能无限用）

一次可用 n 次，在求解器里是**有界背包**，用二进制分解成 1/2/4/… 个 0/1 单元，
既保证最优，又不会让状态数爆炸。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Bundle, Meal, fmt, is_combo, money

FIXED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元\s*(.+)")
FREE_RE = re.compile(r"免费\s*(.+)")
BOGO_RE = re.compile(r"(.+?)\s*买一送一")
HALF_RE = re.compile(r"(.+?)\s*第二份半价")
PICK_RE = re.compile(r"任选\s*(\d+)")
THRESHOLD_RE = re.compile(r"满\s*(\d+(?:\.\d+)?)\s*减\s*(\d+(?:\.\d+)?)")

# 券名里常见、但对「匹配哪个商品」没有意义的前后缀
NOISE_TOKENS = (
    "中杯", "大杯", "小杯", "超大杯", "单人", "双人",
    "人气", "王牌", "经典", "精选", "限定", "新品", "超值", "特价", "会员",
)
SIZE_TOKENS = ("中杯", "大杯", "小杯", "超大杯")


@dataclass
class Coupon:
    """一张可参与计算的券 / 活动规则。"""

    name: str
    kind: str                      # fixed | free | bogo | half_second
    target_code: str               # 落到菜单里的具体商品
    quantity: int                  # 这张券覆盖几件
    price: int                     # 覆盖这些件数要付多少（分）
    retail: int                    # 同样内容按单点要花多少（分）
    code: str = ""                 # 真实 couponCode，空表示只有规则没有券
    coupon_id: str = ""            # 真实 couponId
    confidence: str = "name-parse"  # exact（接口给了商品绑定）| name（券名精确）| fuzzy（模糊匹配）
    source: str = "preview"        # store | wallet | preview | file
    note: str = ""
    uses: int = 1                  # 这套券/活动能用几次；0 表示不限次

    @property
    def saving(self) -> int:
        return self.retail - self.price

    @property
    def usable(self) -> bool:
        """有真实券码才算能真下单核价。"""
        return bool(self.code or self.coupon_id)


def _strip_noise(text: str) -> str:
    cleaned = text.replace(" ", "").replace("　", "")
    for token in NOISE_TOKENS:
        cleaned = cleaned.replace(token, "")
    return cleaned


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（用于「麦辣鸡腿堡」↔「麦辣鸡腿汉堡」这类差异）。"""
    if not a or not b:
        return 0
    best = 0
    previous = [0] * (len(b) + 1)
    for ca in a:
        current = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                current[j] = previous[j - 1] + 1
                if current[j] > best:
                    best = current[j]
        previous = current
    return best


def resolve_target(meals: dict[str, Meal], phrase: str) -> tuple[str | None, str, str]:
    """把「券名里的商品描述」落到菜单商品上。

    门店菜单的叫法常和券名不一致（券：麦辣鸡腿堡 / 菜单：麦辣鸡腿汉堡），
    所以先精确包含，再用最长公共子串兜底，并把匹配可信度回报给调用方。
    只接受单品：套餐的券结构不同，不在这里处理。
    """
    cleaned = _strip_noise(phrase)
    cleaned = PICK_RE.sub("", cleaned).strip("（）()·-—™")
    if not cleaned:
        return None, "券名里没有可识别的商品名"

    singles = [m for m in meals.values() if m.price > 0 and not is_combo(m)]
    exact = [m for m in singles if cleaned in _strip_noise(m.name)]
    if not exact:
        exact = [m for m in singles if _strip_noise(m.name) and _strip_noise(m.name) in cleaned]
    if exact:
        exact.sort(key=lambda m: m.price)
        return exact[0].code, f"按券名精确匹配到「{exact[0].name}」", "name"

    need = max(2, int(len(cleaned) * 0.5 + 0.5))
    fuzzy = []
    for meal in singles:
        score = _lcs_len(cleaned, _strip_noise(meal.name).strip("™"))
        if score >= need:
            fuzzy.append((score, meal))
    if fuzzy:
        fuzzy.sort(key=lambda pair: (-pair[0], pair[1].price))
        score, meal = fuzzy[0]
        return meal.code, f"按相似度匹配到「{meal.name}」（公共片段 {score} 字，需人工确认）", "fuzzy"

    return None, f"菜单里找不到与「{cleaned}」对应的单品", "none"


def _retail_for(meals: dict[str, Meal], code: str, qty: int) -> int:
    meal = meals.get(code)
    return (meal.price if meal else 0) * qty


def parse_coupon(meals: dict[str, Meal], name: str) -> tuple[Coupon | None, str]:
    """把券名解析成结构化规则。返回 (Coupon, 说明)；无法建模时 Coupon 为 None。"""
    raw = (name or "").strip()
    if not raw:
        return None, "券名为空"

    if THRESHOLD_RE.search(raw):
        return None, "满减券属于订单级优惠，本版只支持商品级券"

    if FREE_RE.search(raw):
        phrase = FREE_RE.search(raw).group(1)
        code, note, conf = resolve_target(meals, phrase)
        if not code:
            return None, note
        return (
            Coupon(name=raw, kind="free", target_code=code, quantity=1, price=0,
                   retail=_retail_for(meals, code, 1), confidence=conf, note=note),
            note,
        )

    if BOGO_RE.search(raw):
        phrase = BOGO_RE.search(raw).group(1)
        code, note, conf = resolve_target(meals, phrase)
        if not code:
            return None, note
        unit = _retail_for(meals, code, 1)
        return (
            Coupon(name=raw, kind="bogo", target_code=code, quantity=2, price=unit,
                   retail=unit * 2, confidence=conf, note=note),
            note,
        )

    if HALF_RE.search(raw):
        phrase = HALF_RE.search(raw).group(1)
        code, note, conf = resolve_target(meals, phrase)
        if not code:
            return None, note
        unit = _retail_for(meals, code, 1)
        return (
            Coupon(name=raw, kind="half_second", target_code=code, quantity=2,
                   price=unit + unit // 2, retail=unit * 2, confidence=conf, note=note),
            note,
        )

    fixed = FIXED_RE.search(raw)
    if fixed:
        price = money(fixed.group(1))
        phrase = fixed.group(2)
        pick = PICK_RE.search(phrase)
        qty = int(pick.group(1)) if pick else 1
        code, note, conf = resolve_target(meals, phrase)
        if not code:
            return None, note
        return (
            Coupon(name=raw, kind="fixed", target_code=code, quantity=max(qty, 1),
                   price=price, retail=_retail_for(meals, code, max(qty, 1)),
                   confidence=conf, note=note),
            note,
        )

    return None, "无法从券名识别出优惠形式"


# ---------------------------------------------------------------- 数据来源


def _walk(node, hit: list[dict]) -> None:
    """递归找出所有「像优惠券」的字典对象。"""
    if isinstance(node, dict):
        keys = {k.lower() for k in node}
        if {"couponcode", "couponid", "couponno"} & keys:
            hit.append(node)
            return
        for value in node.values():
            _walk(value, hit)
    elif isinstance(node, list):
        for value in node:
            _walk(value, hit)


def _pick(record: dict, *names: str) -> str:
    lowered = {k.lower(): v for k, v in record.items()}
    for name in names:
        value = lowered.get(name.lower())
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def collect_api_coupons(client, store_code: str, order_type: int = 1,
                        be_type: int = 1) -> tuple[list[dict], str]:
    """拉取「当前门店可用券」，失败时退回卡包。返回 (原始记录, 来源说明)。"""
    try:
        data = client.call("query-store-coupons", storeCode=store_code,
                           orderType=order_type, beType=be_type)
        records: list[dict] = []
        _walk(data, records)
        if records:
            return records, "query-store-coupons（当前门店可用）"
    except Exception as exc:  # 门店券不可用时不该中断求解
        last_error = str(exc)
    else:
        last_error = ""

    try:
        data = client.call("query-my-coupons", page="1", pageSize="200")
        records = []
        _walk(data, records)
        if records:
            return records, "query-my-coupons（卡包，未校验门店可用性）"
    except Exception as exc:
        last_error = str(exc)

    return [], last_error or "券包为空"


def coupons_from_records(meals: dict[str, Meal], records: list[dict],
                         source: str) -> tuple[list[Coupon], list[str]]:
    """把接口返回的券记录翻译成 Coupon。券名负责语义，券码负责核销。"""
    coupons: list[Coupon] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for record in records:
        name = _pick(record, "couponName", "couponTitle", "couponDesc", "name", "title")
        if not name or name in seen:
            continue
        seen.add(name)
        coupon, note = parse_coupon(meals, name)
        if coupon is None:
            skipped.append(f"{name}：{note}")
            continue
        coupon.code = _pick(record, "couponCode", "couponNo")
        coupon.coupon_id = _pick(record, "couponId")
        coupon.source = source
        # 接口若直接给了商品绑定，就以接口为准，可信度更高
        bound = _pick(record, "productCode", "mealCode", "goodsCode")
        if bound and bound in meals:
            coupon.target_code = bound
            coupon.retail = _retail_for(meals, bound, coupon.quantity)
            coupon.confidence = "exact"
        coupons.append(coupon)
    return coupons, skipped


def preview_coupons(client, meals: dict[str, Meal]) -> tuple[list[Coupon], list[str], list[str]]:
    """把「可领取但还没领」的券按券名折算成假设规则，用于预演。

    这类券没有券码，结果只能当上限参考，不能直接下单。
    """
    try:
        data = client.call("available-coupons")
    except Exception as exc:
        return [], [], [f"查询可领券失败：{exc}"]

    names: list[str] = []
    for item in data or []:
        if isinstance(item, dict):
            name = _pick(item, "couponName", "name", "title")
            status = _pick(item, "couponStatus", "status")
            if name and status in ("", "CAN_GET", "AVAILABLE"):
                names.append(name)
        elif isinstance(item, str):
            names.append(item)

    coupons, skipped = coupons_from_records(meals, [{"couponName": n} for n in names], "preview")
    return coupons, skipped, []


UNLIMITED_WORDS = ("unlimited", "inf", "infinity", "no-limit", "nolimit", "不限", "不限次", "无限", "无限次")


def _parse_uses(value, name: str = "") -> tuple[int, str]:
    """解析「这套券能用几次」。返回 (次数, 提示)；0 表示不限次。"""
    if value is None:
        return 1, ""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in UNLIMITED_WORDS or text == "0":
            return 0, f"{name}：按不限次计算，需自行确认活动没有单账号上限"
        try:
            value = float(text)
        except ValueError:
            return 1, f"{name}：uses「{value}」无法识别，按 1 次计算"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1, f"{name}：uses「{value}」无法识别，按 1 次计算"
    if number < 0:
        return 1, f"{name}：uses 不能为负，按 1 次计算"
    return number, ""


def load_rule_file(meals: dict[str, Meal], path: str) -> tuple[list[Coupon], list[str]]:
    """读取本地规则文件：既支持券，也支持「不是券」的活动（买一送一、第二份半价）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = data.get("coupons", data) if isinstance(data, dict) else data
    coupons: list[Coupon] = []
    skipped: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("couponName") or ""
        kind = (entry.get("kind") or "fixed").strip()
        target = entry.get("target") or ""
        qty = int(entry.get("quantity") or 1)
        uses, uses_note = _parse_uses(entry.get("uses", entry.get("times")), name)
        if uses_note:
            skipped.append(uses_note)

        if target:
            code, note, conf = resolve_target(meals, target)
        else:
            parsed, note = parse_coupon(meals, name)
            if parsed is not None:
                parsed.code = str(entry.get("couponCode") or "")
                parsed.coupon_id = str(entry.get("couponId") or "")
                parsed.source = "file"
                parsed.uses = uses
                parsed.confidence = "exact" if parsed.code else parsed.confidence
                coupons.append(parsed)
                continue
            code, note, conf = None, note, "none"
        if not code:
            skipped.append(f"{name or target}：{note}")
            continue

        unit = _retail_for(meals, code, 1)
        if kind == "fixed":
            price = money(entry.get("price"))
            retail = unit * qty
        elif kind == "free":
            price, qty = 0, max(qty, 1)
            retail = unit * qty
        elif kind == "bogo":
            qty = max(qty, 2)
            price, retail = unit, unit * qty
        elif kind == "half_second":
            qty = max(qty, 2)
            price, retail = unit + unit // 2, unit * qty
        else:
            skipped.append(f"{name}：不支持的 kind「{kind}」")
            continue
        coupons.append(
            Coupon(
                name=name or f"{target}{qty}件",
                kind=kind,
                target_code=code,
                quantity=qty,
                price=price,
                retail=retail,
                code=str(entry.get("couponCode") or ""),
                coupon_id=str(entry.get("couponId") or ""),
                confidence="exact" if (entry.get("couponCode") or entry.get("couponId")) else conf,
                source="file",
                note=note,
                uses=uses,
            )
        )
    return coupons, skipped


# ---------------------------------------------------------------- 转成购买单元


def coupon_bundles(coupons: list[Coupon], meals: dict[str, Meal]) -> tuple[list[Bundle], list[str]]:
    """券 → 购买单元。只有真比单点便宜才纳入。

    `uses > 1` 时用二进制分解成 1/2/4/… 个 0/1 块：既能表达「最多用 n 次」，
    又能让求解器继续用 0/1 子集枚举，不会把状态数乘成指数。
    `uses == 0` 表示不限次，直接当成普通可重复单元丢进无限背包。
    """
    bundles: list[Bundle] = []
    skipped: list[str] = []
    for coupon in coupons:
        if coupon.price <= 0 and coupon.kind != "free":
            skipped.append(f"{coupon.name}：券价异常")
            continue
        if coupon.retail - coupon.price <= 0:
            skipped.append(f"{coupon.name}：用券不比单点便宜（{fmt(coupon.retail)} → {fmt(coupon.price)}）")
            continue
        meal = meals.get(coupon.target_code)
        base_label = coupon.name
        if meal and coupon.quantity > 1:
            base_label += f"｜{meal.name}×{coupon.quantity}"

        # 真券的券码是唯一的：一个 code 只能核销一次，不能被当成多次
        uses = 1 if coupon.usable else coupon.uses
        if uses < 0:
            uses = 0

        def make(block: int, unlimited: bool) -> Bundle:
            label = base_label if block == 1 else f"{base_label}（连用 {block} 次）"
            if unlimited:
                label += "（不限次）"
            return Bundle(
                kind="coupon",
                sku=coupon.target_code,
                label=label,
                price=coupon.price * block,
                contents=((coupon.target_code, coupon.quantity * block),),
                coupon=coupon,
                one_shot=not unlimited,
                uses=block,
            )

        if uses == 0:
            bundles.append(make(1, True))
            continue

        remaining, block = uses, 1
        while remaining > 0:
            take = min(block, remaining)
            bundles.append(make(take, False))
            remaining -= take
            block *= 2
    return bundles, skipped
