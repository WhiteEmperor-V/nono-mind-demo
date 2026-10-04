#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wechat_loop: 诺诺的微信器官(主动插件 / continuous organ)

把腾讯 iLink Bot API 变成 nono-mind 的一个器官:
  * 扫码登录: 二维码 ASCII 打到 stdout + PNG 存 /root/nono-mind/weixin/qr.png
  * 长轮询 getupdates(35s) 收消息: 文本消息 -> Router.route('master_message', ...)
    走标准信号进细胞外液(与 immune/router 白名单规则一致, 缺 text 自动挂起)
  * 发送能力: 轮询 State kv key ``wechat_outbox``, 有新条目就 sendmessage 发出
    (发送时 echo 该联系人最新的 context_token)
  * 主动说话: 任何进程/器官/意识核往 ``wechat_outbox`` 做 kv_append 即可,
    本器官负责投递 -> 与"回复出口"同一通道
  * 断线重连/会话失效参照 Hermes weixin 适配器:
    MAX_CONSECUTIVE_FAILURES=3, 常规2s / 连续失败后 30s backoff,
    session expired = errcode -14 (及 -2 + "unknown error" 旧会话信号)
  * 去重: MessageDeduplicator(message_id + 文本指纹, TTL 300s)

插件规格(四件套 + manifest, 供免疫系统/手动 systemd 使用):
    init(state)/tick(state)/on_event(event)/health() + 模块常量 MANIFEST
    loop_type: continuous —— 直接 ``python3 organs/wechat_loop.py`` 常驻即可,
    或参照 plugins/heartbeat 布局把本文件放进 plugins/<name>/ 由免疫层子进程托管.

outbox 条目契约(写入方):
    kv_append('wechat_outbox', {"text": "...", "to": "可选目标wxid/room"})
    - text 必填; to 缺省时发给主人(State kv 'wechat/owner', 未设则最近联系人)

依赖: aiohttp(HTTP) + qrcode(登录渲染). 系统 python(/usr/bin/python3)缺省没有:
    /usr/local/lib/hermes-agent/venv/bin/pip install aiohttp qrcode certifi
运行环境(与 deploy/*.service 一致)建议用 Hermes venv:
    /usr/local/lib/hermes-agent/venv/bin/python3 organs/wechat_loop.py

实现自写, 只参考 Hermes 的 HTTP 协议/wire 格式, 不依赖 Hermes 任何代码.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import struct
import sys
import time
import uuid

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# --------------------------------------------------------------------------
# 运行依赖(可选加载, 缺省给明确报错/安装提示)
# --------------------------------------------------------------------------
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except Exception:  # pragma: no cover - 环境缺依赖
    aiohttp = None
    AIOHTTP_AVAILABLE = False

try:
    import qrcode
    QRCODE_AVAILABLE = True
except Exception:  # pragma: no cover - 环境缺依赖
    qrcode = None
    QRCODE_AVAILABLE = False


def check_requirements() -> list:
    """返回缺失依赖清单(空列表 = 全部就绪)."""
    missing = []
    if not AIOHTTP_AVAILABLE:
        missing.append("aiohttp")
    if not QRCODE_AVAILABLE:
        missing.append("qrcode")
    return missing


def install_hint() -> str:
    return ("缺少微信器官依赖, 安装命令:\n"
            "  /usr/local/lib/hermes-agent/venv/bin/pip install aiohttp qrcode certifi\n"
            "或系统 python: pip3 install aiohttp qrcode certifi")


class DependencyError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 常量 / wire 格式(与腾讯 iLink Bot 官方接口对齐)
# --------------------------------------------------------------------------
MANIFEST = {
    "name": "wechat_loop",
    "version": "0.1",
    "loop_type": "continuous",
    "subscribes": ["master_message", "wechat_outbox"],
    "publishes": ["master_message"],
    "entry": "organs/wechat_loop.py",
}

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
QR_TIMEOUT_MS = 35_000
QR_LOGIN_TIMEOUT_SECONDS = 300
QR_MAX_REFRESH = 3

MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
SESSION_EXPIRED_PAUSE_SECONDS = 600
RATE_LIMIT_ERRCODE = -2
SESSION_EXPIRED_ERRCODE = -14
MESSAGE_DEDUP_TTL_SECONDS = 300
OUTBOX_POLL_SECONDS = 1.0
MAX_OUTBOX_RETRIES = 8        # P1-3: 单条outbox失败重试上限(超过即放弃, 防无限重试)
                              # 2026-09-21主人消息被丢事故: iLink限流窗口远超3次重试周期,
                              # 值得发的消息宁可口慢也不能丢, 上限放宽到8次(~35分钟)
OUTBOX_RETRY_COOLDOWN = 240   # 2026-09-22凌晨限流风暴: 整轮失败后冷却4分钟再碰sendmessage,
                              # 高频重试本身会续期风控窗口(8轮x5次=40次/40分钟触发每日上限)

MAX_MESSAGE_LENGTH = 1900          # 超长文本分片发送
SEND_RETRIES = 4                   # 每片发送重试次数
SEND_RATE_LIMIT_BACKOFF_SECONDS = 9  # 2026-09-21: 限流时改为逐轮翻倍退避(9→18→36…上限120s), 见_send_text

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5
MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

STATE_OUTBOX_KEY = "wechat_outbox"
STATE_OWNER_KEY = "wechat/owner"

def _should_send_outbox_item(item: dict) -> bool:
    """outbox闸(9/29主人拍板): 只发'对话口/大成果报喜'(显式标from_organ=dialogue),
    后台独白/反思日志(无from_organ标记 或 标reflection/coder等后台器官的)不发主人微信.
    纯函数, 可单测. 无标记当后台独白(拦), 只有显式标dialogue的才放行.
    2026-10-02加: 带debug标记的(诺诺调试时塞的测试)一律不发主人微信(主人嫌测试刷屏),
    只有真人主人在跟诺诺聊天的才发."""
    if item.get("debug"):
        return False
    return item.get("from_organ") == "dialogue"


DEFAULT_DATA_DIR = os.environ.get("NONO_WECHAT_DIR") or os.path.join(_REPO_ROOT, "weixin")
ACCOUNT_FILE = "account.json"
SYNC_FILE = "sync.json"
TOKENS_FILE = "context_tokens.json"
QR_FILE = "qr.png"


def log(msg: str) -> None:
    """器官日志走 stderr(把 stdout 留给二维码 ASCII 输出)."""
    print(f"[{time.strftime('%H:%M:%S')}][wechat] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------
def _json_dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def _atomic_write_json(path: str, obj, mode: int = 0o600) -> None:
    """原子写 JSON(临时文件+rename), 默认 0600(账号/同步/token 都是敏感信息)."""
    _ensure_dir(os.path.dirname(path))
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _load_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log(f"读取 {path} 失败({exc}), 按空处理")
        return default


def _is_stale_session_ret(ret, errcode, errmsg) -> bool:
    """errcode/ret = -2 且 errmsg='unknown error' 是老会话信号(与 -14 同级)."""
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    return str(errmsg or "").lower() == "unknown error"


def _is_session_expired(ret, errcode, errmsg) -> bool:
    if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE:
        return True
    return _is_stale_session_ret(ret, errcode, errmsg)


def _split_text(content: str, limit: int = MAX_MESSAGE_LENGTH) -> list:
    """微信单条长度上限内尽量整段发; 超长按行/字符切开."""
    if not content:
        return []
    if len(content) <= limit:
        return [content]
    chunks, buf = [], ""
    for line in content.split("\n"):
        if len(line) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(line[i:i + limit] for i in range(0, len(line), limit))
            continue
        if len(buf) + len(line) + 1 > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c and c.strip()]


class MessageDeduplicator:
    """TTL 去重缓存(思路同 Hermes helpers.MessageDeduplicator)."""

    def __init__(self, ttl_seconds: float = MESSAGE_DEDUP_TTL_SECONDS, max_size: int = 2000):
        self._seen: dict = {}
        self._ttl = ttl_seconds
        self._max_size = max_size

    def is_duplicate(self, key: str) -> bool:
        if not key:
            return False
        now = time.time()
        if key in self._seen:
            if now - self._seen[key] < self._ttl:
                return True
            del self._seen[key]
        self._seen[key] = now
        if len(self._seen) > self._max_size:
            cutoff = now - self._ttl
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
            if len(self._seen) > self._max_size:
                newest = sorted(self._seen.items(), key=lambda kv: kv[1])[-self._max_size:]
                self._seen = dict(newest)
        return False

    def contains(self, key: str) -> bool:
        if not key:
            return False
        ts = self._seen.get(key)
        if ts is None:
            return False
        if time.time() - ts < self._ttl:
            return True
        del self._seen[key]
        return False

    def discard(self, key: str) -> None:
        self._seen.pop(key, None)


# --------------------------------------------------------------------------
# HTTP transport: 默认 aiohttp, 测试可注入任意带 post_json/get_json 的桩
# --------------------------------------------------------------------------
class AiohttpTransport:
    """基于 aiohttp 的默认 transport(带 certifi CA + wait_for 超时)."""

    def __init__(self):
        if not AIOHTTP_AVAILABLE:
            raise DependencyError(install_hint())
        self._session = None
        self._connector = None

    def _ssl_connector(self):
        """certifi 可用时用它的 CA; 否则回退 aiohttp 默认(trust_env 尊重 SSL_CERT_FILE)."""
        if self._connector is not None:
            return self._connector
        import ssl
        ssl_ctx = None
        try:
            import certifi
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ssl_ctx = None
        if ssl_ctx is not None:
            self._connector = aiohttp.TCPConnector(ssl=ssl_ctx, keepalive_timeout=2,
                                                   enable_cleanup_closed=True)
        return self._connector

    async def _session_get(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=True, connector=self._ssl_connector())
        return self._session

    async def post_json(self, url: str, *, headers: dict, payload: dict, timeout_ms: int):
        body = _json_dumps(payload)
        headers = dict(headers)
        headers.setdefault("Content-Length", str(len(body.encode("utf-8"))))
        session = await self._session_get()

        async def _do():
            async with session.post(url, data=body, headers=headers) as resp:
                raw = await resp.text()
                if not resp.ok:
                    raise RuntimeError(f"iLink POST HTTP {resp.status}: {raw[:300]}")
                return json.loads(raw)

        return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)

    async def get_json(self, url: str, *, headers: dict, timeout_ms: int):
        session = await self._session_get()

        async def _do():
            async with session.get(url, headers=headers) as resp:
                raw = await resp.text()
                if not resp.ok:
                    raise RuntimeError(f"iLink GET HTTP {resp.status}: {raw[:300]}")
                return json.loads(raw)

        return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None


# --------------------------------------------------------------------------
# iLink 协议客户端(薄封装: URL 拼接 + 认证头 + 端点调用)
# --------------------------------------------------------------------------
class ILinkError(RuntimeError):
    """iLink 返回的业务错误(ret/errcode 非0)."""
    def __init__(self, message: str, ret=None, errcode=None, session_expired: bool = False):
        super().__init__(message)
        self.ret = ret
        self.errcode = errcode
        self.session_expired = session_expired


class ILinkClient:
    def __init__(self, transport, base_url: str = ILINK_BASE_URL, token: str = ""):
        self.transport = transport
        self.base_url = (base_url or ILINK_BASE_URL).rstrip("/")
        self.token = token or ""

    def _url(self, endpoint: str) -> str:
        return f"{self.base_url}/{endpoint}"

    def _headers(self, body: str = "") -> dict:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _random_wechat_uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body:
            headers["Content-Length"] = str(len(body.encode("utf-8")))
        return headers

    # ---- 端点 ----
    async def get_bot_qrcode(self, bot_type: str = "3") -> dict:
        url = self._url(f"{EP_GET_BOT_QR}?bot_type={bot_type}")
        return await self.transport.get_json(url, headers=self._headers(), timeout_ms=QR_TIMEOUT_MS)

    async def get_qrcode_status(self, qrcode_value: str) -> dict:
        url = self._url(f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}")
        return await self.transport.get_json(url, headers=self._headers(), timeout_ms=QR_TIMEOUT_MS)

    async def get_updates(self, sync_buf: str, timeout_ms: int = LONG_POLL_TIMEOUT_MS) -> dict:
        payload = {"get_updates_buf": sync_buf}
        try:
            return await self.transport.post_json(
                self._url(EP_GET_UPDATES), headers=self._headers(),
                payload=payload, timeout_ms=timeout_ms)
        except asyncio.TimeoutError:
            # 长轮询 35s 到期无消息 = 正常空批(不视为失败)
            return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}

    async def send_message(self, to_user_id: str, text: str, context_token=None,
                           client_id: str = "") -> dict:
        message = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id or f"nono-wechat-{uuid.uuid4().hex}",
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token
        payload = {"msg": message}
        return await self.transport.post_json(
            self._url(EP_SEND_MESSAGE), headers=self._headers(),
            payload=payload, timeout_ms=API_TIMEOUT_MS)

    def with_token(self, token: str) -> "ILinkClient":
        self.token = token
        return self


# --------------------------------------------------------------------------
# 微信器官本体
# --------------------------------------------------------------------------
def _extract_text(item_list: list) -> str:
    """从 iLink item_list 提取文本(仅文字消息; 媒体留给后续版本)."""
    if not isinstance(item_list, list):
        return ""
    for item in item_list:
        if not isinstance(item, dict):
            continue
        if item.get("type") == ITEM_TEXT:
            return str((item.get("text_item") or {}).get("text") or "")
        if item.get("type") == ITEM_VOICE:
            # 无原始音频时腾讯自带 STT 可用(带来源标记)
            voice = item.get("voice_item") or {}
            if not (voice.get("media") or {}):
                vt = str(voice.get("text") or "")
                if vt:
                    return f"[微信语音转写]\n{vt}"
            continue
    return ""


class WeChatLoopOrgan:
    """微信器官: 扫码登录 + getupdates 长轮询收 + wechat_outbox 轮询发.

    四件套:
      init(state) -> organ
      tick(state) -> dict   (单轮 outbox 排空/状态)
      on_event(event) -> None
      health() -> bool
    """

    def __init__(self, client=None, data_dir: str = DEFAULT_DATA_DIR, transport=None,
                 bot_type: str = "3", owner: str = ""):
        self.data_dir = _ensure_dir(data_dir)
        self.bot_type = str(bot_type or "3")
        self.client = client
        self.transport = transport or (AiohttpTransport() if AIOHTTP_AVAILABLE else None)
        self.account = self.load_account()
        token = str((self.account or {}).get("bot_token")
                    or (self.account or {}).get("token") or "")
        base_url = (self.account or {}).get("base_url") or ILINK_BASE_URL
        self.ilink = (ILinkClient(self.transport, base_url=base_url, token=token)
                       if self.transport is not None else None)

        self._tokens = _load_json(self._path(TOKENS_FILE), {}) or {}
        self._sync_buf = str((_load_json(self._path(SYNC_FILE), {}) or {}).get("get_updates_buf", ""))
        self._dedup = MessageDeduplicator(ttl_seconds=MESSAGE_DEDUP_TTL_SECONDS)
        self._owner = owner or ""
        self._last_peer = None          # 最近来信联系人(兜底目标)
        self._long_poll_timeout_ms = LONG_POLL_TIMEOUT_MS
        self._failure_streak = 0
        self._running = False
        self._started_at = None
        self._last_poll_ok = 0.0        # 0 = 尚未成功 poll 过
        self._qr_status_interval = 1.0  # 登录轮询间隔(测试可调小)
        self._outbox_poll_seconds = OUTBOX_POLL_SECONDS
        self._tasks = []

    # ---------- 路径/持久化 ----------
    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, name)

    @property
    def account_id(self) -> str:
        return str((self.account or {}).get("ilink_bot_id") or (self.account or {}).get("account_id") or "")

    @property
    def token(self) -> str:
        acct = self.account or {}
        return str(acct.get("bot_token") or acct.get("token") or "")

    def load_account(self) -> dict:
        return _load_json(self._path(ACCOUNT_FILE)) or {}

    def save_account(self, creds: dict) -> None:
        """保存登录凭据(0600). creds 字段: ilink_bot_id/bot_token/base_url/ilink_user_id."""
        payload = {
            "ilink_bot_id": str(creds.get("ilink_bot_id") or creds.get("account_id") or ""),
            "bot_token": str(creds.get("bot_token") or creds.get("token") or ""),
            "base_url": str(creds.get("base_url") or creds.get("baseurl") or ILINK_BASE_URL),
            "ilink_user_id": str(creds.get("ilink_user_id") or creds.get("user_id") or ""),
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        _atomic_write_json(self._path(ACCOUNT_FILE), payload, mode=0o600)
        self.account = payload
        log(f"登录凭据已保存: {self._path(ACCOUNT_FILE)}")
        return self.account

    def _save_tokens(self) -> None:
        try:
            _atomic_write_json(self._path(TOKENS_FILE), self._tokens, mode=0o600)
        except Exception as exc:
            log(f"context_token 持久化失败: {exc}")

    def _save_sync_buf(self) -> None:
        try:
            _atomic_write_json(self._path(SYNC_FILE), {"get_updates_buf": self._sync_buf}, mode=0o600)
        except Exception as exc:
            log(f"sync_buf 持久化失败: {exc}")

    # ---------- State ----------
    def _state_client(self):
        if self.client is None:
            from state.server import StateClient
            self.client = StateClient()
        return self.client

    def _router(self):
        from immune.router import Router
        return Router(self._state_client())

    # ---------- 二维码渲染 ----------
    def _render_qr(self, scan_data: str) -> str:
        """ASCII 打 stdout + PNG 存 weixin/qr.png. 返回 PNG 绝对路径或 ''."""
        print("\n请使用微信扫描以下二维码登录诺诺: ")
        print(scan_data)
        png_path = ""
        try:
            if QRCODE_AVAILABLE:
                qr = qrcode.QRCode(border=1)
                qr.add_data(scan_data)
                qr.make(fit=True)
                qr.print_ascii(invert=True)
                png = qrcode.QRCode(border=2)
                png.add_data(scan_data)
                png.make(fit=True)
                png_path = self._path(QR_FILE)
                png.make_image(fill_color="black", back_color="white").save(png_path)
                print(f"二维码图片(可发给主人): {png_path}")
            else:
                print("(提示: 未安装 qrcode 库, 无法输出ASCII二维码, 请直接打开上方链接)")
        except Exception as exc:
            print(f"(二维码渲染失败: {exc}, 请直接打开上方链接)", file=sys.stderr)
        return png_path

    # ---------- 扫码登录 ----------
    async def _login_async(self, bot_type: str = "", timeout_seconds: int = QR_LOGIN_TIMEOUT_SECONDS) -> dict:
        if self.transport is None:
            raise DependencyError(install_hint())
        if self.ilink is None:
            self.ilink = ILinkClient(self.transport, base_url=ILINK_BASE_URL, token="")
        bot_type = bot_type or self.bot_type
        refresh_count = 0
        deadline = time.monotonic() + timeout_seconds
        qrcode_value, current_base_url = "", ILINK_BASE_URL

        while time.monotonic() < deadline:
            qr_resp = await self.ilink.get_bot_qrcode(bot_type=bot_type)
            qrcode_value = str(qr_resp.get("qrcode") or "")
            qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
            if not qrcode_value:
                raise RuntimeError("get_bot_qrcode 响应缺少 qrcode 字段")
            scan_data = qrcode_url if qrcode_url else qrcode_value
            self._render_qr(scan_data)
            print("等待扫码(超时 %d 秒)..." % int(deadline - time.monotonic()))

            while time.monotonic() < deadline:
                try:
                    status_resp = await self.ilink.get_qrcode_status(qrcode_value)
                except asyncio.TimeoutError:
                    await asyncio.sleep(self._qr_status_interval)
                    continue
                except Exception as exc:
                    log(f"二维码状态轮询异常(继续): {exc}")
                    await asyncio.sleep(self._qr_status_interval)
                    continue

                status = str(status_resp.get("status") or "wait")
                if status == "wait":
                    print(".", end="", flush=True)
                elif status == "scaned":
                    print("\n已扫码, 请在手机上确认登录...", flush=True)
                elif status == "scaned_but_redirect":
                    host = str(status_resp.get("redirect_host") or "")
                    if host:
                        current_base_url = f"https://{host}"
                        if self.ilink:
                            self.ilink.base_url = current_base_url
                elif status == "confirmed":
                    creds = {
                        "ilink_bot_id": str(status_resp.get("ilink_bot_id") or ""),
                        "bot_token": str(status_resp.get("bot_token") or ""),
                        "base_url": str(status_resp.get("base_url")
                                        or status_resp.get("baseurl")
                                        or current_base_url or ILINK_BASE_URL),
                        "ilink_user_id": str(status_resp.get("ilink_user_id") or ""),
                    }
                    if not creds["ilink_bot_id"] or not creds["bot_token"]:
                        raise RuntimeError("确认登录但凭据不完整: " + json.dumps(status_resp, ensure_ascii=False))
                    account = self.save_account(creds)
                    if self.ilink:
                        self.ilink.token = account["bot_token"]
                        self.ilink.base_url = account["base_url"]
                    print(f"\n微信登录成功! ilink_bot_id={account['ilink_bot_id']}")
                    return account
                elif status == "expired":
                    refresh_count += 1
                    if refresh_count > QR_MAX_REFRESH:
                        raise RuntimeError("二维码多次过期, 请重新执行登录")
                    print(f"\n二维码已过期, 刷新({refresh_count}/{QR_MAX_REFRESH})...", flush=True)
                    break
                await asyncio.sleep(self._qr_status_interval)
        raise TimeoutError(f"微信登录超时({timeout_seconds}s), 请重新运行")

    def login(self, bot_type: str = "", timeout_seconds: int = QR_LOGIN_TIMEOUT_SECONDS) -> dict:
        """同步包装: 输出二维码后阻塞等待扫码确认."""
        if not AIOHTTP_AVAILABLE:
            raise DependencyError(install_hint())
        return asyncio.run(self._login_async(bot_type=bot_type, timeout_seconds=timeout_seconds))

    # ---------- 入站处理 ----------
    def _extract_msg_text(self, msg: dict) -> str:
        return _extract_text(msg.get("item_list") or [])

    def _reply_target(self, msg: dict) -> tuple:
        """返回 (is_group, reply_target, sender). 群聊回给 room, 私聊回给 from_user_id."""
        room_id = str(msg.get("room_id") or msg.get("chat_room_id") or "").strip()
        from_user_id = str(msg.get("from_user_id") or "").strip()
        to_user_id = str(msg.get("to_user_id") or "").strip()
        is_group = bool(room_id) or (bool(to_user_id) and to_user_id != self.account_id
                                     and msg.get("msg_type") == MSG_TYPE_USER)
        target = room_id if is_group else from_user_id
        return is_group, target, from_user_id

    def _handle_inbound(self, msg: dict) -> dict | None:
        """处理一条 getupdates 消息: 去重 + token记录 + 路由进 State. 返回处理摘要."""
        from_user_id = str(msg.get("from_user_id") or "").strip()
        if not from_user_id:
            return None
        if from_user_id == self.account_id:
            return None  # 自己的消息回显

        message_id = str(msg.get("message_id") or "").strip()
        if message_id and self._dedup.is_duplicate(message_id):
            log(f"去重跳过 message_id={message_id}")
            return None

        text = self._extract_msg_text(msg)
        if text:
            content_key = f"content:{from_user_id}:{hashlib.md5(text.encode('utf-8')).hexdigest()}"
            if self._dedup.is_duplicate(content_key):
                log(f"去重跳过重复文本 from={from_user_id}")
                return None
        if not text:
            return None  # 本版只处理文本消息(媒体后续版本)

        is_group, target, sender = self._reply_target(msg)
        if not target:
            target = sender
        if not is_group:
            self._last_peer = sender
            # 自动认主: 第一个私聊联系人即主人(可在 State kv wechat/owner 覆盖)
            owner = self._owner or (self._state_client().kv_get(STATE_OWNER_KEY) or "")
            if not owner:
                try:
                    self._state_client().kv_set(STATE_OWNER_KEY, sender)
                    self._owner = sender
                    log(f"自动设置主人(wechat/owner): {sender}")
                except Exception as exc:
                    log(f"设置 wechat/owner 失败: {exc}")

        context_token = str(msg.get("context_token") or "").strip()
        if context_token:
            if self._tokens.get(target) != context_token:
                self._tokens[target] = context_token
                self._save_tokens()

        payload = {
            "text": text,
            "from_wechat": True,
            "sender": sender,
            "chat_type": "group" if is_group else "dm",
            "room": target if is_group else "",
            "message_id": message_id,
        }
        try:
            sig = self._router().route("master_message", payload, source="wechat_loop")
            log(f"消息入State: {sender[:12]} -> master_message(sig_id={sig.sig_id})")
            try:   # 票1: 进度互看(通道loops也在states/留痕)
                from immune.plugins import write_progress
                write_progress(self._state_client(), "wechat_loop", "收到:" + text[:30], 100)
            except Exception:
                pass
            return {"sig_id": sig.sig_id, "text": text[:80]}
        except Exception as exc:
            log(f"路由 master_message 失败: {exc}")
            return None

    def _process_msgs(self, msgs: list) -> int:
        n = 0
        for msg in msgs or []:
            if isinstance(msg, dict) and self._handle_inbound(msg):
                n += 1
        return n

    # ---------- 出站(sendmessage) ----------
    async def _send_text(self, to: str, text: str, context_token=None) -> list:
        """发一条文本(内部自动分片). 会话失效 -14 时去掉 token 重试一次. 返回已发片数."""
        if self.ilink is None:
            raise DependencyError(install_hint())
        chunks = _split_text(text)
        if not chunks:
            return []
        sent = 0
        ctx = context_token
        retried_without_token = False
        for chunk in chunks:
            for attempt in range(SEND_RETRIES + 1):
                resp = await self.ilink.send_message(to, chunk, context_token=ctx)
                ret, errcode = resp.get("ret"), resp.get("errcode")
                if ret not in {0, None} or errcode not in {0, None}:
                    msg_err = resp.get("errmsg") or resp.get("msg") or "unknown error"
                    if _is_session_expired(ret, errcode, msg_err):
                        if ctx and not retried_without_token:
                            log(f"发送遇会话失效(ret={ret} errcode={errcode}); 去掉 context_token 重试")
                            retried_without_token = True
                            self._tokens.pop(to, None)
                            self._save_tokens()
                            ctx = None
                            continue
                        raise ILinkError(f"sendmessage 会话失效: ret={ret} errcode={errcode} {msg_err}",
                                         ret=ret, errcode=errcode, session_expired=True)
                    if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE:
                        # 2026-09-21主人消息被丢事故: 固定9s退避扛不过iLink限流窗口,
                        # 改为逐轮翻倍(9→18→36→72→120封顶), 给限流窗口足够冷却时间
                        _rl_backoff = min(SEND_RATE_LIMIT_BACKOFF_SECONDS * (2 ** attempt), 120)
                        log(f"sendmessage 频率限制, backoff {_rl_backoff}s")
                        await asyncio.sleep(_rl_backoff)
                        continue
                    raise ILinkError(f"sendmessage 错误: ret={ret} errcode={errcode} {msg_err}",
                                     ret=ret, errcode=errcode)
                sent += 1
                break
            else:
                raise ILinkError(f"sendmessage 重试耗尽({SEND_RETRIES + 1}次), 最后错误见日志")
        return sent

    def _default_target(self) -> str:
        if self._owner:
            return self._owner
        try:
            owner = self._state_client().kv_get(STATE_OWNER_KEY) or ""
            if owner:
                self._owner = owner
                return owner
        except Exception:
            pass
        return self._last_peer or ""

    async def _drain_outbox(self) -> dict:
        """轮询 wechat_outbox 并发信(快照消费 + 失败项/并发新增保留)."""
        client = self._state_client()
        try:
            items = client.kv_get(STATE_OUTBOX_KEY) or []
        except Exception as exc:
            log(f"读取 {STATE_OUTBOX_KEY} 失败: {exc}")
            # P1修复(2026-09-20): 读取失败重建StateClient连接(9/19同款修复漏了这条路:
            # "timed out object"异常后客户端连接半包错位, 常驻循环复用坏连接→每秒报错永不自愈)
            try:
                client.close()
            except Exception:
                pass
            self.client = None
            return {"sent": 0, "failed": 0, "error": str(exc)}
        if not isinstance(items, list):
            log(f"{STATE_OUTBOX_KEY} 不是数组, 忽略")
            return {"sent": 0, "failed": 0}
        if not items:
            return {"sent": 0, "failed": 0}
        snapshot_len = len(items)
        sent, failed, skipped, dropped = 0, [], 0, 0
        for item in items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                skipped += 1
                continue
            # 9/29主人拍板: 只发"对话口/大成果报喜"(from_organ=dialogue),
            # 后台独白/反思日志(无标记或标reflection的)不发主人微信, 直接丢弃
            if not _should_send_outbox_item(item):
                _org = item.get("from_organ", "dialogue")
                log(f"outbox后台独白/日志拦截(不发主人): [{_org}] {text[:40]}")
                dropped += 1
                continue
            to = str(item.get("to") or "").strip()
            if not to or to in ("owner", "master", "主人"):
                to = self._default_target()
            if not to:
                log("outbox 消息无发送目标且未设置主人, 保留待发")
                failed.append(item)
                continue
            try:
                ctx = self._tokens.get(to)
                n = await self._send_text(to, text, context_token=ctx)
                sent += n
                log(f"已发出({to[:12]}): {text[:60]}" + (f" [echo context_token]" if ctx else ""))
            except Exception as exc:
                log(f"发送失败({to[:12]}): {exc}")
                # P1-3: 失败项带retry计数写回, 连续失败超MAX_OUTBOX_RETRIES次则放弃并log
                retries = int(item.get("retry") or 0) + 1
                if retries > MAX_OUTBOX_RETRIES:
                    log(f"outbox放弃: {to[:12]}的条目连续失败{retries}次仍未发出, 丢弃: {text[:60]}")
                    dropped += 1
                else:
                    kept = dict(item)
                    kept["retry"] = retries
                    failed.append(kept)

        # 并发新追加(消费期间写入者又 append 的尾部)不能被覆盖丢掉
        tail = []
        try:
            cur = client.kv_get(STATE_OUTBOX_KEY) or []
            if isinstance(cur, list) and len(cur) > snapshot_len:
                tail = cur[snapshot_len:]
        except Exception:
            pass
        try:
            client.kv_set(STATE_OUTBOX_KEY, failed + tail)
        except Exception as exc:
            log(f"outbox 消费后回写失败(消息仍保留): {exc}")
            # P1修复(2026-09-20): 回写失败同样重建连接(坏连接不复用)
            try:
                client.close()
            except Exception:
                pass
            self.client = None
        return {"sent": sent, "failed": len(failed), "dropped": dropped,
                "skipped": skipped, "kept_tail": len(tail)}

    # ---------- 长轮询 / outbox 两个循环 ----------
    async def _poll_inbox_once(self) -> dict:
        """单轮 getupdates. 抛异常由 _inbox_loop 计数退避."""
        if self.ilink is None or not self.token:
            raise RuntimeError("未登录(缺 token), 无法 getupdates")
        resp = await self.ilink.get_updates(self._sync_buf, timeout_ms=self._long_poll_timeout_ms)

        suggested = resp.get("longpolling_timeout_ms")
        if isinstance(suggested, int) and 2000 <= suggested <= 120000:
            self._long_poll_timeout_ms = suggested

        ret, errcode = resp.get("ret", 0), resp.get("errcode", 0)
        errmsg = resp.get("errmsg") or resp.get("msg") or ""
        if ret not in {0, None} or errcode not in {0, None}:
            if _is_session_expired(ret, errcode, errmsg):
                log(f"会话失效(ret={ret} errcode={errcode}), 暂停 {SESSION_EXPIRED_PAUSE_SECONDS}s")
                self._failure_streak = 0
                raise ILinkError(f"session expired: ret={ret} errcode={errcode} {errmsg}",
                                 ret=ret, errcode=errcode, session_expired=True)
            raise ILinkError(f"getupdates 失败: ret={ret} errcode={errcode} {errmsg}", ret=ret, errcode=errcode)

        new_buf = str(resp.get("get_updates_buf") or "")
        n = self._process_msgs(resp.get("msgs") or [])   # P1-4: 先完整处理消息
        # P1-4: 处理成功后才推进并持久化sync_buf——崩溃时旧buf未落盘,
        # 重启后消息会重复收到(去重器兜底), 而不是被跳过的永久丢失
        if new_buf and new_buf != self._sync_buf:
            self._sync_buf = new_buf
            self._save_sync_buf()
        self._last_poll_ok = time.time()
        return {"handled": n, "sync_buf_len": len(self._sync_buf)}

    async def _inbox_loop(self) -> None:
        if not self.token:
            raise RuntimeError("账号未登录")
        log("inbox 长轮询循环启动")
        while self._running:
            try:
                r = await self._poll_inbox_once()
                self._failure_streak = 0
                if r["handled"]:
                    log(f"本轮处理 {r['handled']} 条消息")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failure_streak += 1
                is_session = (isinstance(exc, ILinkError) and exc.session_expired)
                if is_session:
                    log("会话失效, 暂停后恢复轮询(等主人重新扫码/官方恢复会话)")
                    await asyncio.sleep(SESSION_EXPIRED_PAUSE_SECONDS)
                    self._failure_streak = 0
                    continue
                wait = BACKOFF_DELAY_SECONDS if self._failure_streak >= MAX_CONSECUTIVE_FAILURES \
                    else RETRY_DELAY_SECONDS
                log(f"getupdates 失败({self._failure_streak}/{MAX_CONSECUTIVE_FAILURES}): "
                    f"{exc} -> {wait}s 后重试")
                await asyncio.sleep(wait)
                if self._failure_streak >= MAX_CONSECUTIVE_FAILURES:
                    self._failure_streak = 0
                    self._recycle_transport()

    async def _outbox_loop(self) -> None:
        log("outbox 轮询循环启动")
        while self._running:
            try:
                r = await self._drain_outbox()
                if r["sent"] or r["failed"]:
                    log(f"outbox 本轮: {r}")
                    # 2026-09-22凌晨限流风暴: 有失败项时冷却4分钟再碰sendmessage,
                    # 否则1秒一轮的高频重试会不断续期iLink风控窗口, 永远出不去
                    if r["failed"]:
                        await asyncio.sleep(OUTBOX_RETRY_COOLDOWN)
            except Exception as exc:
                log(f"outbox 轮询异常(不退出): {exc}")
            await asyncio.sleep(self._outbox_poll_seconds)

    def _recycle_transport(self) -> None:
        """连续失败后重建 transport(释放被卡住的 socket)."""
        try:
            old = self.transport
            if old is not None and hasattr(old, "close"):
                asyncio.get_event_loop().create_task(self._close_transport(old))
            self.transport = AiohttpTransport() if AIOHTTP_AVAILABLE else None
            self.ilink = ILinkClient(self.transport, base_url=self.ilink.base_url,
                                     token=self.token) if self.transport else None
            log("transport 已重建")
        except Exception as exc:
            log(f"transport 重建失败: {exc}")

    @staticmethod
    async def _close_transport(transport) -> None:
        try:
            if hasattr(transport, "close"):
                await transport.close()
        except Exception:
            pass

    # ---------- 主循环 ----------
    async def run_async(self) -> None:
        if self.transport is None:
            if not AIOHTTP_AVAILABLE:
                raise DependencyError(install_hint())
            self.transport = AiohttpTransport()
        if self.ilink is None:
            self.ilink = ILinkClient(self.transport, base_url=ILINK_BASE_URL, token=self.token)
        if not self.account or not self.token:
            if self.account:
                log("账号文件存在但 token 缺失, 重新扫码登录")
            else:
                log("未找到登录凭据, 进入扫码登录...")
            await self._login_async()
        else:
            self.ilink.token = self.token
            self.ilink.base_url = (self.account.get("base_url") or ILINK_BASE_URL)
            log(f"已加载登录凭据 ilink_bot_id={self.account_id}")

        self._running = True
        self._started_at = time.time()
        self._tasks = [asyncio.create_task(self._inbox_loop()),
                       asyncio.create_task(self._outbox_loop())]
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            log("收到取消信号, 器官退出")
            raise
        finally:
            self._running = False
            for t in self._tasks:
                if not t.done():
                    t.cancel()
            self._save_tokens()
            await self._close_transport(self.transport)
            if self.client is not None and hasattr(self.client, "close"):
                try:
                    self.client.close()
                except Exception:
                    pass

    def run(self) -> None:
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            log("手动停止")
        except DependencyError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)
        except Exception as exc:
            log(f"器官退出: {exc}")
            raise

    # ---------- 四件套(插件规格) ----------
    def init(self, client=None):
        if client is not None:
            self.client = client
        if not self._owner:
            try:
                self._owner = self._state_client().kv_get(STATE_OWNER_KEY) or ""
            except Exception:
                pass
        return self

    def tick(self, client=None) -> dict:
        """单轮动作(同步): 已登录就排空一次 outbox; 未登录返回状态."""
        if client is not None:
            self.client = client
        if not self.account:
            return {"ok": False, "reason": "not_logged_in",
                    "hint": "先运行 python3 organs/wechat_loop.py --login"}
        if not AIOHTTP_AVAILABLE:
            return {"ok": False, "reason": "missing_deps", "hint": install_hint()}
        try:
            asyncio.get_running_loop()
            return {"ok": False, "reason": "loop_running", "note": "continuous main loop already active"}
        except RuntimeError:
            pass
        return asyncio.run(self._drain_outbox())

    def on_event(self, event=None) -> None:
        """event 型接口: 器官常驻轮询, 事件无附加动作."""
        if isinstance(event, dict):
            log(f"on_event 收到 {event.get('type', event)} (无动作)")

    def health(self) -> bool:
        missing = check_requirements()
        if missing:
            log(f"health: 缺依赖 {missing}")
            return False
        if not self.account or not self.token:
            return False
        if self._started_at is not None:
            # 常驻运行中: 最近成功长轮询需在健康宽限内(>2 个长轮询周期)
            grace = max(self._long_poll_timeout_ms * 2 / 1000 + 20, 90)
            if self._last_poll_ok and time.time() - self._last_poll_ok > grace:
                return False
        return True


# --------------------------------------------------------------------------
# 模块级四件套(便于免疫系统/工具按 organs 规格接入)
# --------------------------------------------------------------------------
_ORGAN: WeChatLoopOrgan | None = None


def _get_organ() -> WeChatLoopOrgan:
    global _ORGAN
    if _ORGAN is None:
        _ORGAN = WeChatLoopOrgan()
        _ORGAN.init()
    return _ORGAN


def init(state=None) -> WeChatLoopOrgan:
    global _ORGAN
    _ORGAN = WeChatLoopOrgan(client=state)
    _ORGAN.init(state)
    log("wechat_loop organ 已初始化")
    return _ORGAN


def tick(state=None) -> dict:
    return _get_organ().tick(state)


def on_event(event=None) -> None:
    _get_organ().on_event(event)


def health() -> bool:
    try:
        return _get_organ().health()
    except Exception as exc:
        log(f"health 异常: {exc}")
        return False


def handle(sig_type: str, payload=None, client=None) -> None:
    """与 dialogue/reflection 器官一致的 handle 入口(意识核若直接调度)."""
    _get_organ().init(client)
    if sig_type in ("wechat_outbox_flush", "master_message"):
        _get_organ().on_event({"type": sig_type})
    else:
        log(f"handle({sig_type}) 忽略")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _cli(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="诺诺微信器官(wechat_loop)")
    ap.add_argument("--dir", default=DEFAULT_DATA_DIR, help="weixin 数据目录")
    ap.add_argument("--login", action="store_true", help="只执行扫码登录并退出")
    ap.add_argument("--bot-type", default="3", help="iLink bot_type(默认 3)")
    ap.add_argument("--owner", default="", help="设置主人 wxid(写入 State wechat/owner)")
    ap.add_argument("--check", action="store_true", help="依赖/健康自检并退出")
    ap.add_argument("--timeout", type=int, default=QR_LOGIN_TIMEOUT_SECONDS,
                    help="登录等待扫码超时(秒)")
    args = ap.parse_args(argv)

    if args.check:
        missing = check_requirements()
        print("依赖检查:", "OK" if not missing else f"缺: {missing}")
        if missing:
            print(install_hint())
            return 0 if not args.login else 1
        organ = WeChatLoopOrgan(data_dir=args.dir)
        print("登录态:", "已登录" if organ.account else "未登录")
        print("健康:", "OK" if organ.health() else "不健康(未登录/依赖缺)")
        return 0

    if args.owner:
        from state.server import StateClient
        c = StateClient()
        c.kv_set(STATE_OWNER_KEY, args.owner)
        c.close()
        print(f"主人已设为: {args.owner}")

    organ = WeChatLoopOrgan(data_dir=args.dir, bot_type=args.bot_type)
    if args.login:
        try:
            organ.login(bot_type=args.bot_type, timeout_seconds=args.timeout)
            return 0
        except Exception as exc:
            print(f"登录失败: {exc}", file=sys.stderr)
            return 1

    if args.owner:
        organ._owner = args.owner
    try:
        organ.run()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"wechat_loop 退出: {exc}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    if not AIOHTTP_AVAILABLE and "--check" not in (argv or sys.argv[1:]):
        print(install_hint(), file=sys.stderr)
        return 1
    return _cli(argv)


if __name__ == "__main__":
    sys.exit(main())
