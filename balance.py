"""余额：直连 DeepSeek Platform 查询（运行时 + 命令行，零第三方依赖）。

dsh 的登录凭据就明文存在 `$DSH_HOME/.credentials.yaml` 的
`records."deepseek-account-platform/default".payload` 里，带上其中的 token 直接请求
Platform 的 `get_user_summary` 即可 —— 不需要启动 dsh、不需要 web GUI、不需要 token/cookie 交换。

对事件系统只暴露三个函数：

    available(cfg)  凭据文件在不在（一次 stat，菜单可以随手判断）
    query(cfg)      真去查一次，返回 balance state（`shape: "money"`），失败也返回带 reason 的 state
    credentials_file(cfg)   凭据路径（调试用）

为什么不直接复用 DSH 的 `POST /balance`：那条路要 DSH 在跑。桌宠很多时候是「DSH 关着、
只想养只宠物」，可余额数据是现成的——登录凭据就躺在磁盘上，没必要为此拉起一个服务。
所以余额**只有这一条通道**，不看 DSH 在不在；DSH 在线时也不改走它的 `/balance`，
免得同一个数字有两种口径。

配置（`config.jsonc` 的 `dsh` 段）：

    balanceFull: 100      参考额度（¥）：余额到这个数算「钱袋满溢」，0 = 不折算百分比
    balanceHome: ""       dsh home 目录，空 = $DSH_HOME 或 ~/.dsh
    balanceTimeout: 30    单次请求超时（秒）
    balanceProxy: "direct"  代理策略，见下面 `_proxy_urls`：
                            direct  直连（默认；这个接口国内直连就通，别绕代理）
                            auto    跟随系统代理；万一代理是本地回环且没在监听，自动改直连再试
                            http://127.0.0.1:7890   指定代理（简写 127.0.0.1:7890 也行）

开关只有两个：`events.balanceEnabled`（要不要这个功能）、凭据文件在不在。

命令行（原来的 `balance_watch.py`，现在同一个文件）：

    python balance.py                    # 打印一行余额
    python balance.py --json             # 输出原始 biz_data
    python balance.py --threshold 10     # 低于 10 元时退出码 3
    python balance.py --watch 300        # 每 300 秒轮询
    python balance.py --token <TOKEN>    # 跳过凭据文件，直接给 token

退出码：0 正常 / 2 未登录（读不到 grant）/ 3 低于阈值 / 4 请求或响应失败

⚠ 凭据等同密码：这里只读 token 用于请求，不打印、不写日志、不落盘、不进仓库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, getproxies, urlopen

DEFAULT_ORIGIN = "https://platform.deepseek.com"
GRANT_KEY = "deepseek-account-platform/default"
CREDENTIALS_FILENAME = ".credentials.yaml"

# Platform 的 WAF 会拦非浏览器 UA（curl 默认 UA 会拿到 HTML 的 "Request Blocked"）。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
# 与宿主 details.ts 一致的十进制文法（big.js 风格，允许 0E-16、1e+3）。
DECIMAL_RE = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")

CURRENCY_SYMBOL = {"CNY": "¥", "USD": "$"}


class NotSignedIn(RuntimeError):
    """凭据文件里没有可用的 grant。"""


class BalanceQueryError(RuntimeError):
    """请求失败、被拒绝，或响应不符合约定结构。"""


# ---------------------------------------------------------------- 底层：凭据


def credentials_path(home: str | Path | None = None) -> Path:
    """定位凭据文件；优先级：显式 home > $DSH_HOME > ~/.dsh。"""
    if home is not None:
        return Path(home).expanduser() / CREDENTIALS_FILENAME
    env_home = os.environ.get("DSH_HOME")
    base = Path(env_home).expanduser() if env_home else Path.home() / ".dsh"
    return base / CREDENTIALS_FILENAME


def read_grant(home: str | Path | None = None, key: str = GRANT_KEY) -> dict:
    """从凭据文档中取出一个 grant 记录。

    只做定点解析，不引入 PyYAML，避免为了读两个字段装依赖。
    文档结构形如：
        version: 1
        records:
            deepseek-account-platform/default:
                kind: grant
                payload:
                    version: 1
                    token: xxx
                    issuer: https://platform.deepseek.com

    :param home: 覆盖 dsh home 目录。
    :param key: 记录键，默认取 Platform 账号 grant。
    :returns: {"token": str, "issuer": str}
    :raises NotSignedIn: 文件不存在或不含该记录。
    """
    path = credentials_path(home)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise NotSignedIn(f"读不到凭据文件 {path}：{error}") from error

    lines = text.splitlines()
    head = re.compile(rf"^\s{{2}}{re.escape(key)}:\s*$")
    start = next((i for i, line in enumerate(lines) if head.match(line)), None)
    if start is None:
        raise NotSignedIn(f"凭据文件里没有记录 {key}（可能尚未登录）")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.startswith("   "):
            end = i
            break
    block = lines[start + 1:end]

    def field(name: str) -> str | None:
        pattern = re.compile(rf"^\s*{name}:\s*(.+)$")
        for line in block:
            found = pattern.match(line)
            if found:
                return found.group(1).strip().strip("'\"")
        return None

    kind = field("kind")
    if kind is not None and kind != "grant":
        raise NotSignedIn(f"记录 {key} 不是 grant（kind={kind}）")
    token = field("token")
    if not token:
        raise NotSignedIn(f"记录 {key} 里没有 token")
    return {"token": token, "issuer": field("issuer") or DEFAULT_ORIGIN}


# ---------------------------------------------------------------- 底层：请求

# 「系统代理」指向这些地址 = 本机代理软件（Clash / v2ray 之类）的入口。
# 代理没开时端口没人监听，连接会被立刻 RST，Windows 报 WinError 10061
# 「由于目标计算机积极拒绝，无法连接」—— 看着像目标服务器炸了，其实是本地代理死了：
# `urllib` 默认跟随系统代理（环境变量 HTTP(S)_PROXY > 注册表 Internet Settings），
# 浏览器可能走的 PAC / 自己的规则，所以只有这个脚本会先崩。
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _proxy_urls(proxy: str) -> dict[str, str] | None:
    """配置值 → ProxyHandler 要的映射；None = 用 urllib 默认（跟随系统设置）。

    默认 `direct`：这个接口（platform.deepseek.com）国内直连就通，走代理纯属自找麻烦——
    代理软件一关、系统代理还留着，请求就先撞上 10061「积极拒绝」。
    """
    mode = (proxy or "direct").strip()
    low = mode.lower()
    if low in ("", "auto", "system"):
        return None
    if low in ("direct", "none", "off", "no", "false"):
        return {}
    if "://" not in mode:
        mode = "http://" + mode
    return {"http": mode, "https": mode}


def _effective_proxy() -> str:
    """「系统代理」此刻会用的地址（和 urlopen 的判断口径一致，环境变量优先）。"""
    proxies = getproxies()
    for key in ("https", "http", "all"):
        url = proxies.get(key)
        if url:
            return str(url)
    return ""


def _is_loopback(url: str) -> bool:
    """代理地址是不是本机回环（127.0.0.1 / localhost …）。"""
    host = url.split("://", 1)[-1].split("/", 1)[0]
    if "@" in host:  # user:pass@host:port
        host = host.rsplit("@", 1)[1]
    host = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return host.strip("[]").lower() in LOOPBACK_HOSTS


def _refused(error: Exception) -> bool:
    """是不是「连接被拒绝」（本地代理端口没人监听）。"""
    reason = getattr(error, "reason", error)
    return (
        isinstance(reason, ConnectionRefusedError)
        or "10061" in str(reason)
        or "积极拒绝" in str(reason)
    )


def _proxy_error(error: Exception, timeout: float) -> BalanceQueryError:
    """把底层异常翻成一句人话（别让用户对着一串英文猜）。"""
    if isinstance(error, HTTPError):
        hint = "（token 可能已失效，请重新登录）" if error.code == 401 else ""
        return BalanceQueryError(f"HTTP {error.code}{hint}")
    if isinstance(error, TimeoutError):
        return BalanceQueryError(f"请求超时（{timeout}s）")
    reason = getattr(error, "reason", error)
    proxy = _effective_proxy()
    if _refused(error) and proxy and _is_loopback(proxy):
        return BalanceQueryError(
            f"连不上本地代理 {proxy}（代理软件没开 / 系统代理没关？）"
            f"—— 原始错误：{reason}"
        )
    return BalanceQueryError(f"网络错误：{reason}")


def _build_request(origin: str, token: str) -> Request:
    return Request(
        f"{origin}/api/v0/users/get_user_summary",
        headers={
            "x-dsh-auth-token": token,
            "x-client-bundle-id": "",
            "x-client-platform": "web",
            "x-client-version": "balance-watch-py",
            "x-client-locale": "zh_CN",
            "x-client-timezone-offset": str(-time.timezone if not time.daylight else -time.altzone),
            "User-Agent": BROWSER_UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{origin}/usage",
        },
        method="GET",
    )


def _fetch_once(origin: str, token: str, timeout: float, proxy: str) -> bytes:
    """按代理策略发一次请求，返回原始响应体（最多再读 1 字节用来判超长）。

    **每次都要新建 Request**：走代理时 `Request.set_proxy()` 会把 host 改写成代理地址，
    复用同一个对象重试会继续连那个死代理（这就是「改了直连还是 10061」的坑）。
    """
    request = _build_request(origin, token)
    urls = _proxy_urls(proxy)
    if urls is None:
        with urlopen(request, timeout=timeout) as response:
            return response.read(65_536 + 1)
    opener = build_opener(ProxyHandler(urls))
    with opener.open(request, timeout=timeout) as response:
        return response.read(65_536 + 1)


def fetch_balance(
    token: str,
    issuer: str = DEFAULT_ORIGIN,
    timeout: float = 30.0,
    proxy: str = "direct",
) -> dict:
    """请求 get_user_summary 并返回校验过的 biz_data。

    :param token: 凭据文件中的 grant token。
    :param issuer: 签发该 token 的 Platform origin，必须是同一 origin。
    :param timeout: 单次请求超时（秒）。
    :param proxy: `direct`（默认，直连）/ `auto`（跟随系统，回环代理死掉时自动改直连）/ 代理 URL。
    :returns: {"normal_wallets": [...], "bonus_wallets": [...], "total_costs": [...]}
    :raises BalanceQueryError: HTTP 失败、被 WAF 拦截、业务码非 0 或结构不符。
    """
    origin = (issuer or DEFAULT_ORIGIN).rstrip("/")

    try:
        body = _fetch_once(origin, token, timeout, proxy)
    except (HTTPError, URLError, TimeoutError) as error:
        # auto 模式专治「代理软件关了、系统代理还指着 127.0.0.1:7890」：
        # 本地端口没人听 → 10061 积极拒绝。这时直连一次往往就通了，
        # 用户既不用关代理设置，也不用配任何东西。
        if _proxy_urls(proxy) is None and _refused(error) and _is_loopback(_effective_proxy()):
            print(
                f"[余额] 本地代理 {_effective_proxy()} 没在监听，改直连重试一次",
                flush=True,
            )
            try:
                body = _fetch_once(origin, token, timeout, "direct")
            except (HTTPError, URLError, TimeoutError) as retry_error:
                raise _proxy_error(retry_error, timeout) from retry_error
        else:
            raise _proxy_error(error, timeout) from error

    if len(body) > 65_536:
        raise BalanceQueryError("响应体超过 64KiB，不符合预期")
    text = body.decode("utf-8", "replace")
    if text.lstrip().startswith("<"):
        raise BalanceQueryError("返回了 HTML 而不是 JSON（通常是被 WAF 拦截，检查 User-Agent）")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise BalanceQueryError(f"响应不是合法 JSON：{error}") from error

    if payload.get("code") != 0:
        code = payload.get("code")
        # 40003 与宿主等价于 AccountUnauthorizedError：宿主此时会直接删掉本地凭据。
        if code == 40003:
            raise BalanceQueryError("token 已失效（code=40003），请在 dsh 里重新登录")
        raise BalanceQueryError(f"业务失败 code={code} msg={payload.get('msg')!r}")
    data = payload.get("data") or {}
    if data.get("biz_code") != 0:
        raise BalanceQueryError(f"业务失败 biz_code={data.get('biz_code')}")
    biz = data.get("biz_data") or {}
    for field_name in ("normal_wallets", "bonus_wallets"):
        wallets = biz.get(field_name, [])
        if not isinstance(wallets, list):
            raise BalanceQueryError(f"{field_name} 不是数组")
        for wallet in wallets:
            if not isinstance(wallet, dict) or "currency" not in wallet or "balance" not in wallet:
                raise BalanceQueryError(f"{field_name} 条目缺少 currency/balance")
            if not DECIMAL_RE.match(str(wallet["balance"])):
                raise BalanceQueryError(f"余额字符串不合法：{wallet['balance']!r}")
    return biz


def _sum_wallets(wallets) -> dict[str, Decimal]:
    """按币种求和；用 Decimal 保持字符串精度（0E-16 也不会变成 NaN）。"""
    totals: dict[str, Decimal] = {}
    for wallet in wallets or []:
        try:
            amount = Decimal(str(wallet["balance"]))
        except (InvalidOperation, KeyError) as error:
            raise BalanceQueryError(f"余额无法解析：{wallet!r}") from error
        currency = str(wallet.get("currency", "CNY"))
        totals[currency] = totals.get(currency, Decimal(0)) + amount
    return totals


def summarize(biz_data: dict) -> dict:
    """把 biz_data 归约成 {recharge, bonus, cost} 三组按币种的 Decimal 金额。"""
    costs: dict[str, Decimal] = {}
    for row in biz_data.get("total_costs", []) or []:
        currency = str(row.get("currency", "CNY"))
        try:
            amount = Decimal(str(row.get("amount", "0")))
        except InvalidOperation as error:
            raise BalanceQueryError(f"消费金额无法解析：{row!r}") from error
        costs[currency] = costs.get(currency, Decimal(0)) + amount
    return {
        "recharge": _sum_wallets(biz_data.get("normal_wallets")),
        "bonus": _sum_wallets(biz_data.get("bonus_wallets")),
        "cost": costs,
    }


# ---------------------------------------------------------------- 显示


def _pretty(amount: Decimal) -> str:
    """去掉尾随零，0 统一显示为 0。"""
    if amount == 0:
        return "0"
    try:
        text = format(amount, "f")
    except (ValueError, InvalidOperation):
        return str(amount)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _money(amount: Decimal) -> str:
    """金额显示：保留两位小数（接口给的 0.010888 这种尾数没必要摆在脸上）。"""
    try:
        rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return _pretty(amount)
    return _pretty(rounded)


def format_line(biz_data: dict, stamp: str | None = None) -> str:
    """渲染成一行人类可读的余额摘要（命令行用）。"""
    totals = summarize(biz_data)
    parts = []
    for label, group in (("充值余额", totals["recharge"]), ("赠金", totals["bonus"]), ("累计消费", totals["cost"])):
        for currency, amount in group.items():
            parts.append(f"{label} {CURRENCY_SYMBOL.get(currency, '')}{_pretty(amount)}")
    body = "  ".join(parts) if parts else "余额数据为空"
    if stamp is None:
        return body
    return f"[{stamp}] {body}"


# ---------------------------------------------------------------- 事件系统接口


def _fail(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "text": f"余额查不到：{reason}"}


def credentials_file(cfg) -> Path | None:
    """凭据文件路径；文件不存在返回 None。"""
    try:
        path = credentials_path(getattr(cfg.dsh, "balance_home", "") or None)
    except (OSError, ValueError):
        return None
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def available(cfg) -> bool:
    """能不能查余额（只判断凭据在不在，不发请求）。"""
    return credentials_file(cfg) is not None


def query(cfg) -> dict[str, Any]:
    """查一次余额，返回 balance state。

    成功：`{"ok": True, "shape": "money", "data": {"wallets": [...]}, "text": "..."}`
    失败：`{"ok": False, "reason": "...", "text": "余额查不到：..."}`

    `data.wallets` 的 `total` 是**可用余额**（充值 + 赠金），`fullBalance` 是配置的参考额度；
    事件系统就按 `1 - total/fullBalance` 折算「已用百分比」，映射到六档钱袋动画。
    `fullBalance` 为 0 时不折算（只弹文字，不动画）。
    """
    home = getattr(cfg.dsh, "balance_home", "") or None
    timeout = float(getattr(cfg.dsh, "balance_timeout", 30.0) or 30.0)
    proxy = getattr(cfg.dsh, "balance_proxy", "direct") or "direct"

    if not available(cfg):
        return _fail("找不到 dsh 的登录凭据（先 dsh 登录一次）")

    try:
        grant = read_grant(home)
    except NotSignedIn as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001  凭据文件格式意外，别让桌宠崩
        return _fail(f"读凭据出错（{type(exc).__name__}: {exc}）")

    try:
        biz = fetch_balance(grant["token"], grant["issuer"], timeout=timeout, proxy=proxy)
        totals = summarize(biz)
    except BalanceQueryError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001  网络/解析的任何意外都只影响这一次查询
        return _fail(f"查询出错（{type(exc).__name__}: {exc}）")

    full = max(0.0, float(getattr(cfg.dsh, "balance_full", 100.0) or 0.0))
    full_text = _pretty(_decimal(full))

    wallets: list[dict[str, Any]] = []
    pieces: list[str] = []
    empty = True
    for currency in sorted(set(totals["recharge"]) | set(totals["bonus"])):
        recharge = totals["recharge"].get(currency, Decimal(0))
        bonus = totals["bonus"].get(currency, Decimal(0))
        total = recharge + bonus
        if total > 0:
            empty = False
        wallets.append(
            {"currency": currency, "total": _money(total), "fullBalance": full_text}
        )
        symbol = CURRENCY_SYMBOL.get(currency, "")
        piece = f"{symbol}{_money(total)}"
        if bonus > 0:
            piece += f"（含赠金 {symbol}{_money(bonus)}）"
        pieces.append(piece)

    if pieces:
        text = "余额 " + " / ".join(pieces)
        spent = [
            f"{CURRENCY_SYMBOL.get(c, '')}{_money(v)}"
            for c, v in totals["cost"].items()
            if v
        ]
        if spent:
            text += " · 累计花了 " + " / ".join(spent)
        if empty:
            text += "，该充点钱了"
    else:
        text = "余额是空的，该充点钱了"

    return {
        "ok": True,
        "shape": "money",
        "source": "direct",
        "data": {"wallets": wallets},
        "text": text,
    }


# ---------------------------------------------------------------- 命令行


def _once(args: argparse.Namespace) -> int:
    if args.token:
        token, issuer = args.token, args.issuer or DEFAULT_ORIGIN
    else:
        try:
            grant = read_grant(args.home)
        except NotSignedIn as error:
            print(f"未登录：{error}", file=sys.stderr)
            return 2
        token, issuer = grant["token"], grant["issuer"]

    try:
        biz = fetch_balance(token, issuer, args.timeout, args.proxy)
    except BalanceQueryError as error:
        print(f"余额查询失败：{error}", file=sys.stderr)
        return 4

    if args.json:
        print(json.dumps(biz, ensure_ascii=False, indent=2))
    else:
        print(format_line(biz, time.strftime("%H:%M:%S")))

    if args.threshold is not None:
        recharge = summarize(biz)["recharge"]
        total = recharge.get("CNY", Decimal(0))
        if total < Decimal(str(args.threshold)):
            print(f"余额 {_pretty(total)} 低于阈值 {args.threshold}", file=sys.stderr)
            return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="查询 dsh 当前登录账号的 DeepSeek Platform 余额（零依赖）。",
        epilog="退出码：0 正常 / 2 未登录 / 3 低于阈值 / 4 请求失败",
    )
    parser.add_argument("--json", action="store_true", help="输出原始 biz_data JSON")
    parser.add_argument("--threshold", type=float, metavar="N", help="充值余额（CNY）低于 N 时退出码 3")
    parser.add_argument("--watch", type=float, metavar="SEC", default=0, help="每 SEC 秒轮询一次")
    parser.add_argument("--home", metavar="DIR", help="dsh home 目录（默认 $DSH_HOME 或 ~/.dsh）")
    parser.add_argument("--token", metavar="TOKEN", help="直接用给定 token，跳过凭据文件")
    parser.add_argument("--issuer", metavar="URL", default=None, help="配合 --token 指定 Platform origin")
    parser.add_argument("--timeout", type=float, metavar="SEC", default=30.0, help="单次请求超时（默认 30）")
    parser.add_argument(
        "--proxy",
        metavar="MODE",
        default="direct",
        help="direct（默认，直连）/ auto（跟随系统、代理死了自动直连）/ 127.0.0.1:7890",
    )
    args = parser.parse_args(argv)

    if args.watch and args.watch > 0:
        try:
            while True:
                _once(args)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0
    return _once(args)


if __name__ == "__main__":
    sys.exit(main())
