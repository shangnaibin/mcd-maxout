"""命令行入口。

    python -m maxout --city 天津 --keyword 华盛广场 --budget 100 --target-name 板烧
    python -m maxout --store 1980241 --budget 100 --target-name 双层吉士 --coupons preview
"""
from __future__ import annotations

import argparse
import json
import sys

from .catalog import build_bundles, find_store, fmt, load_menu, money
from .client import MCDClient, MCPError
from .coupons import (
    collect_api_coupons,
    coupon_bundles,
    coupons_from_records,
    load_rule_file,
    preview_coupons,
)
from .membership import detect_cards, member_gain
from .solver import (
    alternatives,
    pick_better,
    render_plan,
    solve,
    solve_card_choice,
    solve_without_coupons,
)
from .verify import render_verification, verify

BURGER_HINTS = ("堡", "汉堡")


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _resolve_target(meals, code: str | None, name: str | None):
    if code:
        if code not in meals:
            raise LookupError(f"菜单里没有编码 {code}")
        return code
    if not name:
        raise ValueError("请用 --target-name 或 --target 指定要囤的商品")
    hits = [m for m in meals.values() if name in m.name]
    if not hits:
        raise LookupError(f"菜单里没有名字含「{name}」的餐品")
    singles = [m for m in hits if m.price > 0 and "单品" in m.tags]
    pool = singles or hits
    pool.sort(key=lambda m: m.price)
    return pool[0].code


def _load_coupon_pool(client, meals, args, store_code):
    """按模式装载券池，返回 (coupons, 标题, 被跳过的券说明)。"""
    mode = args.coupons
    coupons, skipped = [], []

    if mode == "off":
        return [], "已关闭（--coupons off）", []

    if mode in ("auto", "wallet"):
        records, source = collect_api_coupons(
            client, store_code, args.order_type, args.be_type
        )
        if records:
            coupons, skipped = coupons_from_records(meals, records, "store")
            if coupons:
                return coupons, source, skipped
            _log(f"  券包里的券无法建模：{'; '.join(skipped[:3])}")
        else:
            _log(f"  当前门店没有可用券（{source}）")
        if mode == "wallet":
            return [], "卡包 / 门店券（没有可用券）", skipped

    if mode in ("auto", "preview"):
        fetched, skipped_pv, errors = preview_coupons(client, meals)
        coupons = fetched
        skipped = skipped + skipped_pv
        for error in errors:
            _log(f"  {error}")
        return coupons, "可领券预演（未领到、无券码，仅供上限参考）", skipped

    if mode == "file":
        if not args.coupon_file:
            raise ValueError("--coupons file 需要同时给 --coupon-file 路径")
        coupons, skipped = load_rule_file(meals, args.coupon_file)
        return coupons, f"本地规则文件 {args.coupon_file}", skipped

    return [], "未加载", []


def _render_pool(coupons, title: str, meals) -> None:
    print()
    print(f"优惠券池：{len(coupons)} 张可用")
    print(f"来源：{title}")
    for coupon in coupons:
        meal = meals.get(coupon.target_code)
        target = meal.name if meal else coupon.target_code
        mark = "券" if coupon.usable else ("预演?" if coupon.confidence == "fuzzy" else "预演")
        if coupon.usable:
            times = "1 次（券码唯一）"
        elif coupon.uses == 0:
            times = "不限次"
        else:
            times = f"{coupon.uses} 次"
        print(
            f"  [{mark}] {coupon.name} → {target}×{coupon.quantity}"
            f"　单点 {fmt(coupon.retail)} → 用券 {fmt(coupon.price)}"
            f"（省 {fmt(coupon.saving)}，可用 {times}）"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="maxout", description="给定预算，把某种麦当劳餐品的份数顶到最多"
    )
    parser.add_argument("--city", help="城市，配合 --keyword 搜门店")
    parser.add_argument("--keyword", help="位置关键词，例如 华盛广场")
    parser.add_argument("--store", help="直接指定 storeCode，跳过门店搜索")
    parser.add_argument("--budget", type=float, default=100, help="预算（元），默认 100")
    parser.add_argument("--target-name", help="想囤的商品名，例如 板烧 / 麦香鸡 / 巨无霸")
    parser.add_argument("--target", help="想囤的商品编码")
    parser.add_argument("--no-combos", action="store_true", help="只考虑单品，不拆套餐")
    parser.add_argument(
        "--card-items",
        "--include-card-items",
        dest="card_items",
        action="store_true",
        help="（已默认开启）把「随单购麦金卡」会员价商品纳入求解，由 --cards 控制开关",
    )
    parser.add_argument(
        "--cards",
        choices=("auto", "off"),
        default="auto",
        help="auto=发现需要开卡的商品并比较「开卡 vs 不开卡」；off=只按原价算",
    )
    parser.add_argument(
        "--card-price",
        type=float,
        default=19.0,
        help="开卡费（元）。接口不返回卡费，默认 19 元/30 天，按实际页面填",
    )
    parser.add_argument(
        "--card-uses",
        type=int,
        default=2,
        help="会员价一个周期内可用几次，默认 2（官方权益页：超值四件套每日最高共 2 次）",
    )
    parser.add_argument(
        "--coupons",
        choices=("auto", "wallet", "preview", "file", "off"),
        default="auto",
        help="auto=先用真实券、没有则用可领券预演；wallet=只认真实券；"
        "preview=只用可领券预演；file=读本地规则；off=不算券",
    )
    parser.add_argument("--coupon-file", help="本地券 / 活动规则 JSON 路径")
    parser.add_argument("--verify", action="store_true", help="用官方 calculate-price 复核总价")
    parser.add_argument(
        "--html",
        nargs="?",
        const="",
        default=None,
        help="生成可分享的 HTML 报告；不给路径则写到 out/ 下",
    )
    parser.add_argument(
        "--web",
        nargs="?",
        const="",
        default=None,
        help="导出可点击的离线网页（index.html + data.js）；不给路径则写到 out/web",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--list", action="store_true", help="只列菜单里的汉堡类商品")
    parser.add_argument("--order-type", type=int, default=1, help="1 到店自取，2 外送")
    parser.add_argument("--be-type", type=int, default=1, help="1 到店取餐，2 麦乐送，5 得来速")
    args = parser.parse_args(argv)

    try:
        client = MCDClient()
    except MCPError as exc:
        _log(str(exc))
        return 2

    try:
        if args.store:
            store_code, store_name = args.store, args.store
        else:
            if not (args.city and args.keyword):
                _log("请给 --city + --keyword，或直接给 --store")
                return 2
            _log(f"正在搜 {args.city} {args.keyword} 附近门店 …")
            store = find_store(client, args.city, args.keyword, args.be_type)
            store_code = store["storeCode"]
            store_name = f"{store['storeName']}（{store['address']}）"
            _log(f"门店：{store_name}")

        _log("正在拉取菜单 …")
        meals = load_menu(client, store_code, args.order_type, args.be_type)
        _log(f"菜单共 {len(meals)} 个 SKU")

        if args.list:
            burgers = [m for m in meals.values() if any(h in m.name for h in BURGER_HINTS)]
            for meal in sorted(burgers, key=lambda m: m.price):
                kind = "套餐" if any(w in meal.name for w in ("套餐", "件套", "随心配")) else "单品"
                print(f"{meal.code:>12}  {fmt(meal.price):>8}  [{kind}] {meal.name}")
            return 0

        target_code = _resolve_target(meals, args.target, args.target_name)
        target = meals[target_code]
        budget = money(args.budget)
        _log(f"目标：{target.name} 单价 {fmt(target.price)}，预算 {fmt(budget)}")

        cards, card_of = ({}, {})
        if args.cards != "off":
            cards, card_of = detect_cards(meals, money(args.card_price), args.card_uses)
            if cards:
                first = next(iter(cards.values()))
                _log(
                    f"发现 {len(card_of)} 个「随单购{first.name}」会员价商品"
                    f"（卡费按 {fmt(first.fee)}、会员价限 {first.uses} 次算）"
                )
            else:
                _log("菜单里没有需要开卡的会员价商品，只按原价求解")

        _log("正在展开套餐 / 随心配组合 …")
        bundles, stats = build_bundles(
            client,
            store_code,
            meals,
            target_code,
            args.order_type,
            args.be_type,
            combo_codes=None if not args.no_combos else [],
            allow_card=bool(cards),
            card_of=card_of,
            progress=lambda i, n, name, count: _log(f"  [{i}/{n}] {name} → {count} 种选法")
            if i % 5 == 0 or i == n
            else None,
        )
        _log(
            f"可组合单元 {stats['unique']} 个（单品 {stats['singles']}，"
            f"套餐变体 {stats['combo_variants']}，成功展开套餐 {stats['combos_expanded']} 个，"
            f"其中会员价单元 {stats['member_units']} 个）"
        )
        if stats["failed"]:
            _log(f"  有 {len(stats['failed'])} 个套餐解析失败（不影响结果）")

        _log("正在装载优惠券 …")
        coupons, pool_source, pool_skipped = _load_coupon_pool(client, meals, args, store_code)
        coupon_units, coupon_skipped = coupon_bundles(coupons, meals)
        if coupons:
            _log(f"  可用券 {len(coupons)} 张，转成购买单元 {len(coupon_units)} 个")
        for note in coupon_skipped:
            _log(f"  跳过：{note}")

        all_units = bundles + coupon_units
        _log("正在求解 …")
        plan = solve(all_units, budget, target_code, meals)
        if plan is None:
            _log("没解出来，可能预算太低")
            return 1
        card = next(iter(cards.values())) if cards else None
        card_plan, plain_plan = (None, plan)
        if card:
            card_plan, plain_plan = solve_card_choice(all_units, budget, target_code, meals, card)
            plan = pick_better(card_plan, plain_plan)
        no_coupon = solve_without_coupons(all_units, budget, target_code, meals)

        baseline = budget // target.price if target.price else 0

        def out(text: str = "") -> None:
            """--json 时只输出纯 JSON，人类可读报告全部让路。"""
            if not args.json:
                print(text)

        out()
        out(f"门店：{store_name}")
        out(f"预算 {fmt(budget)}　目标：最多能买几个{target.name}")
        if coupons and not args.json:
            _render_pool(coupons, pool_source, meals)
        out()
        out("─" * 60)
        out(render_plan(plan, meals, target_code,
                        "最优方案（含开卡）" if plan.card_fee else "最优方案"))
        out("─" * 60)
        out(f"对照①：只单买{target.name} → {baseline} 个，花 {fmt(baseline * target.price)}")
        if no_coupon and no_coupon.target_count != plan.target_count:
            out(
                f"对照②：不用券、只算套餐 → {no_coupon.target_count} 个，"
                f"实付 {fmt(no_coupon.spend)}"
            )
        if card and plain_plan is not None:
            if card_plan is None:
                out()
                out(f"对照③：开{card.name} — 卡费 {fmt(card.fee)} 已超过预算，开了不划算")
            elif card_plan.card_uses == 0:
                out()
                out(
                    f"对照③：开{card.name} — 会员价商品里没有能多囤「{target.name}」的划算组合"
                    f"（会员价多为指定汉堡的四件套），这张卡对囤 {target.name} 没帮助"
                )
            else:
                gap_total, gap_once = member_gain(all_units, card_plan)
                out()
                out(f"对照③：开{card.name} vs 不开卡（会员价商品 {len(card_of)} 个）")
                out(
                    f"  开卡  {card_plan.target_count} 个{target.name}，"
                    f"实付 {fmt(card_plan.spend)}（卡费 {fmt(card.fee)} + 商品 {fmt(card_plan.goods_spend)}），"
                    f"顺带 {card_plan.extras_count} 件其他"
                )
                out(
                    f"  不开  {plain_plan.target_count} 个{target.name}，实付 {fmt(plain_plan.spend)}，"
                    f"顺带 {plain_plan.extras_count} 件其他"
                )
                diff = card_plan.target_count - plain_plan.target_count
                extra_gap = card_plan.extras_count - plain_plan.extras_count
                money_gap = card_plan.spend - plain_plan.spend
                if diff > 0:
                    out(f"  → 开卡多 {diff} 个{target.name}，卡费换来 {extra_gap} 件额外餐品")
                elif diff == 0 and extra_gap > 0:
                    out(f"  → 份数一样，但开卡多 {extra_gap} 件其他餐品，多花 {fmt(money_gap)}")
                elif diff == 0 and extra_gap == 0:
                    out(f"  → 东西一样，开卡纯多花 {fmt(card.fee)}，不值")
                else:
                    parts = []
                    if diff < 0:
                        parts.append(f"少 {-diff} 个{target.name}")
                    if extra_gap > 0:
                        parts.append(f"但多 {extra_gap} 件其他餐品")
                    elif extra_gap < 0:
                        parts.append(f"少 {-extra_gap} 件其他餐品")
                    out("  → 开卡" + "、".join(parts) + "，为「多囤」开卡不划算")
                if gap_total:
                    out(
                        f"  回本线：会员价共省 {fmt(gap_total)}（单次最多省 {fmt(gap_once)}），"
                        f"卡费 {fmt(card.fee)} → "
                        + ("一次下单就回本" if gap_total >= card.fee else f"还差 {fmt(card.fee - gap_total)} 才回本")
                    )
        if plan.target_count > baseline:
            out(f"       ↑ 用对组合，同样预算多拿 {plan.target_count - baseline} 个")
        if plan.coupons_used:
            out()
            out(f"这一单用到的券（共 {plan.coupon_count} 张）：")
            for note in plan.coupons_used:
                out(f"  · {note}")
            if plan.coupon_count > 1:
                out(f"  若每单只能用 1 张券，需要拆成 {plan.orders} 单下单：")
                out("  到店自取拆单没有额外成本；外送拆单会多付配送费，未必划算。")
            if not plan.exact:
                out("  ⚠ 券的可选块过多已做截断，结果可能不是全局最优")

        alts = alternatives(
            all_units, budget, target_code, meals, plan,
            card=card.key if (card and plan.card_fee) else None,
            quota=card.uses if card else 0,
            card_fee=plan.card_fee,
            card_name=plan.card_name,
        )
        if alts:
            out()
            out("同样份数，换个买法：")
            for index, alt in enumerate(alts, 1):
                detail = "；".join(
                    f"{b.coupon.name}×{b.uses * q} 次" if b.coupon else f"{b.label}×{q}"
                    for b, q in alt.items
                )
                out(f"  {index}. {detail} → 实付 {fmt(alt.spend)}，余 {fmt(alt.leftover)}")

        result = None
        if args.verify:
            out()
            _log("正在调官方计价接口复核 …")
            result = verify(client, plan, store_code, args.order_type, args.be_type)
            out(render_verification(result, plan))

        html_path = None
        if args.html is not None:
            from .report import write as write_report

            html_path = args.html or (
                f"out/{store_code}-{target_code}-{plan.target_count}个-{budget // 100}元.html"
            )
            write_report(
                html_path,
                plan=plan,
                meals=meals,
                target_code=target_code,
                target_name=target.name,
                store_name=store_name,
                store_code=store_code,
                budget=budget,
                coupons=coupons,
                pool_source=pool_source,
                baseline=baseline,
                no_coupon=no_coupon,
                verification=result,
                card=card,
                card_plan=card_plan,
                plain_plan=plain_plan,
                card_member_skus=len(card_of),
            )
            html_path = str(html_path)
            _log(f"报告已生成：{html_path}")

        web_dir = None
        if args.web is not None:
            from .export import build_snapshot, write as write_web

            web_dir = args.web or "out/web"
            snapshot = build_snapshot(
                meals,
                [b for b in bundles if b.kind == "single"],
                [b for b in bundles if b.kind == "combo"],
                coupons,
                coupon_units,
                store_code=store_code,
                store_name=store_name,
                order_type=args.order_type,
                be_type=args.be_type,
                stats=stats,
                coupon_source=pool_source,
                cards=cards,
            )
            page, data = write_web(web_dir, snapshot)
            _log(f"网页已生成：{page}")
            _log(f"数据快照：{data}（{data.stat().st_size // 1024} KB，{snapshot['meta']['units']} 个购买单元）")

        if args.json:
            print(
                json.dumps(
                    {
                        "store": store_code,
                        "target": target.name,
                        "budget": budget,
                        "coupon_pool": pool_source,
                        "plan": {
                            "target_count": plan.target_count,
                            "spend": plan.spend,
                            "leftover": plan.leftover,
                            "retail": plan.retail,
                            "coupon_savings": plan.coupon_savings,
                            "coupons_used": plan.coupons_used,
                            "coupon_count": plan.coupon_count,
                            "orders": plan.orders,
                            "exact": plan.exact,
                            "items": [
                                {
                                    "label": b.label,
                                    "price": b.price,
                                    "qty": q,
                                    "uses": b.uses,
                                    "coupon": b.coupon.name if b.coupon else None,
                                    "usable": b.coupon.usable if b.coupon else None,
                                }
                                for b, q in plan.items
                            ],
                        },
                        "without_coupons": {
                            "target_count": no_coupon.target_count if no_coupon else None,
                            "spend": no_coupon.spend if no_coupon else None,
                        },
                        "card": {
                            "detected": [c.as_dict() for c in cards.values()],
                            "member_skus": sorted(card_of),
                            "used": bool(plan.card_fee),
                            "fee": plan.card_fee,
                            "name": plan.card_name,
                            "uses": plan.card_uses,
                            "with_card": {
                                "target_count": card_plan.target_count,
                                "spend": card_plan.spend,
                                "goods_spend": card_plan.goods_spend,
                                "extras": card_plan.extras_count,
                            } if card_plan else None,
                            "without_card": {
                                "target_count": plain_plan.target_count,
                                "spend": plain_plan.spend,
                                "extras": plain_plan.extras_count,
                            } if plain_plan else None,
                        },
                        "stats": stats,
                        "verify": result,
                        "html": html_path,
                        "web": web_dir,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0

    except (MCPError, LookupError, ValueError) as exc:
        _log(f"出错了：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
