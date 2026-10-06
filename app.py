# -*- coding: utf-8 -*-
"""
Ponte de pagamento do curso "Vende em Tudo".
Pix via Cobrix (HMAC) · Cartão via Changelly Pay (RSA) · matrícula automática no Frappe LMS.
Lê TODOS os segredos de variáveis de ambiente (.env) — nada hard-coded.
"""
import os, re, sqlite3, json, time, hmac, hashlib, base64, secrets, contextlib, asyncio
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature

# ------------------------------------------------------------------ config
def env(k, d=None, req=False):
    v = os.environ.get(k, d)
    if req and not v:
        raise RuntimeError(f"Falta a variável de ambiente {k}")
    return v

COBRIX_API_URL        = env("COBRIX_API_URL", "https://cobrix.liquix.tech").rstrip("/")
COBRIX_API_KEY        = env("COBRIX_API_KEY")            # cbx_...
COBRIX_WEBHOOK_SECRET = env("COBRIX_WEBHOOK_SECRET")     # whs_...

CHANGELLY_API_URL     = env("CHANGELLY_API_URL", "https://api.pay.changelly.com").rstrip("/")
CHANGELLY_API_KEY     = env("CHANGELLY_API_KEY")                 # public key (SPKI b64) — vai no X-Api-Key
CHANGELLY_PRIV_B64    = env("CHANGELLY_PRIVATE_KEY_B64")         # private RSA PKCS8 em base64 (DER)
CHANGELLY_CB_PUB_B64  = env("CHANGELLY_CALLBACK_PUBKEY_B64", "") # callback public key SPKI base64 (pega no painel)

FRAPPE_URL            = env("FRAPPE_URL", "https://cursos.liquix.tech").rstrip("/")
FRAPPE_API_KEY        = env("FRAPPE_API_KEY")
FRAPPE_API_SECRET     = env("FRAPPE_API_SECRET")

PUBLIC_BASE           = env("PUBLIC_BASE", "https://pagamentos.liquix.tech").rstrip("/")
DB_PATH               = env("DB_PATH", "/data/ponte.db")
REPLAY_WINDOW_S       = int(env("REPLAY_WINDOW_S", "300"))
ADMIN_KEY             = env("ADMIN_KEY", "")   # chave do painel admin de afiliados
BTC_SEND_URL          = env("BTC_SEND_URL", "")   # endpoint do enviador de BTC do dono (a plataforma chama)
BTC_SEND_KEY          = env("BTC_SEND_KEY", "")   # autenticação do enviador (X-Api-Key)
PAYOUT_MAX_CENTS      = int(env("PAYOUT_MAX_CENTS", "100000"))  # teto por pagamento (trava anti-erro)
OVERRIDE_PCT          = float(env("OVERRIDE_PCT", "5"))   # (legado) fallback antigo de override
DEFAULT_SUB_PCT       = float(env("DEFAULT_SUB_PCT", "15"))  # MLM: quanto o indicado ganha por venda quando o mestre não definiu

# plano -> valor (centavos, já com taxas embutidas) + curso no LMS + valor USDT p/ cartão
PLANS = {
    "starter": {"label": "Starter", "amount_cents": 5400,  "course": "vende-em-tudo",         "usdt": "11.00", "commission_pct": 20},
    "pro":     {"label": "Pro",     "amount_cents": 11200, "course": "vende-em-tudo",         "usdt": "22.50", "commission_pct": 20},
    "premium": {"label": "Premium", "amount_cents": 22700, "course": "vende-em-tudo-premium", "usdt": "45.50", "commission_pct": 20},
    "afiliado": {"label": "Afiliado Express", "amount_cents": 4990,  "course": "afiliado-express", "usdt": "9.90",  "commission_pct": 20},
    "ponto":    {"label": "Ponto de Coleta",  "amount_cents": 29990, "course": "ponto-de-coleta",  "usdt": "59.00", "commission_pct": 40},
}

BUMP_PLAN = "afiliado"  # order bump oferecido no checkout do curso

PIX_DISCOUNT = 0.90  # 10% off no Pix (cartão = preço cheio)
RECOVERY_COUPON = env("RECOVERY_COUPON", "VOLTA15").upper()      # cupom de recuperação de carrinho
RECOVERY_DISCOUNT = float(env("RECOVERY_DISCOUNT", "0.85"))      # 15% off (os 10% + 5% a mais)
RESEND_API_KEY = env("RESEND_API_KEY", "")                       # e-mail transacional (Resend)
RESEND_FROM = env("RESEND_FROM", "Liquix <noreply@liquix.tech>")
OWNER_EMAIL = env("OWNER_EMAIL", "digitalsoberano05@gmail.com")   # avisos pro dono
META_PIXEL_ID = env("META_PIXEL_ID", "")                          # Meta Pixel (tráfego pago)
META_CAPI_TOKEN = env("META_CAPI_TOKEN", "")                      # Conversions API (server-side)

PIXEL_SNIPPET = ("<script>!function(f,b,e,v,n,t,s){if(f.fbq)return;n=f.fbq=function(){"
                 "n.callMethod?n.callMethod.apply(n,arguments):n.queue.push(arguments)};"
                 "if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version='2.0';n.queue=[];"
                 "t=b.createElement(e);t.async=!0;t.src=v;s=b.getElementsByTagName(e)[0];"
                 "s.parentNode.insertBefore(t,s)}(window,document,'script',"
                 "'https://connect.facebook.net/en_US/fbevents.js');"
                 "fbq('init','%s');fbq('track','PageView');</script>")

# Pacotes de tráfego pago gerenciado (Turbine) — sua margem embutida no preço
TURBINE = {
    "impulso": {"label": "Impulso", "valor": "R$ 97",  "desc": "Teste: ~3 dias de anúncio no seu link."},
    "turbo":   {"label": "Turbo",   "valor": "R$ 297", "desc": "~10 dias de anúncio + criativo pronto."},
    "escala":  {"label": "Escala",  "valor": "R$ 597", "desc": "Campanha completa + otimização."},
}

app = FastAPI(title="Ponte Vende em Tudo")

# ------------------------------------------------------------------ banco
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with contextlib.closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS orders(
            order_id TEXT PRIMARY KEY, method TEXT, plan TEXT, email TEXT,
            name TEXT, amount_cents INTEGER, provider_ref TEXT,
            status TEXT DEFAULT 'pending', enrolled INTEGER DEFAULT 0,
            created_at TEXT, updated_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_ref ON orders(provider_ref);
        CREATE TABLE IF NOT EXISTS seen(event_id TEXT PRIMARY KEY, at TEXT);
        CREATE TABLE IF NOT EXISTS affiliates(
            code TEXT PRIMARY KEY, name TEXT, email TEXT UNIQUE, pass_hash TEXT, salt TEXT,
            liquid_wallet TEXT DEFAULT '', usdt_wallet TEXT DEFAULT '', pct REAL DEFAULT 20,
            token TEXT, created_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_af_token ON affiliates(token);
        CREATE TABLE IF NOT EXISTS payouts(
            idem_key TEXT PRIMARY KEY, affiliate_code TEXT, month TEXT, method TEXT,
            destino TEXT, cents INTEGER, status TEXT, txid TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS commissions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, affiliate_code TEXT,
            level INTEGER DEFAULT 1, cents INTEGER, status TEXT DEFAULT 'pending',
            created_at TEXT, updated_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_comm_af ON commissions(affiliate_code);
        CREATE INDEX IF NOT EXISTS idx_comm_order ON commissions(order_id);
        CREATE TABLE IF NOT EXISTS goals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tipo TEXT DEFAULT 'vendas_mes',
            titulo TEXT, vendas INTEGER, bonus_cents INTEGER,
            active INTEGER DEFAULT 1, created_at TEXT);
        CREATE TABLE IF NOT EXISTS turbine_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, affiliate_code TEXT, pacote TEXT,
            status TEXT DEFAULT 'novo', created_at TEXT);
        """)
        # colunas de afiliado nos pedidos (idempotente)
        for col, ddl in (("affiliate_code", "TEXT"),
                         ("commission_cents", "INTEGER DEFAULT 0"),
                         ("commission_status", "TEXT DEFAULT ''"),
                         ("bump", "TEXT DEFAULT ''")):
            try:
                con.execute(f"ALTER TABLE orders ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass
        for acol, addl in (("coupon", "TEXT"),
                           ("payout_method", "TEXT DEFAULT 'pix'"),
                           ("pix_key", "TEXT DEFAULT ''"),
                           ("btc_address", "TEXT DEFAULT ''"),
                           ("ln_address", "TEXT DEFAULT ''"),
                           ("parent_code", "TEXT"),
                           ("give_pct", "REAL")):
            try:
                con.execute(f"ALTER TABLE affiliates ADD COLUMN {acol} {addl}")
            except sqlite3.OperationalError:
                pass
        try:
            con.execute("ALTER TABLE commissions ADD COLUMN note TEXT")   # identifica o bônus de meta (idempotência)
        except sqlite3.OperationalError:
            pass
        con.execute("CREATE INDEX IF NOT EXISTS idx_af_code ON orders(affiliate_code)")
        if not con.execute("SELECT 1 FROM goals LIMIT 1").fetchone():      # metas/missões de exemplo (o dono edita)
            seed = (("vendas_mes", "5 vendas no mês", 5, 5000),
                    ("vendas_mes", "10 vendas no mês", 10, 15000),
                    ("vendas_mes", "25 vendas no mês", 25, 50000),
                    ("vendas_total", "Primeira venda 🎉", 1, 2000),
                    ("indicados", "Recrute 3 afiliados", 3, 10000))
            for tipo, titulo, v, b in seed:
                con.execute("INSERT INTO goals(tipo,titulo,vendas,bonus_cents,active,created_at) VALUES(?,?,?,?,1,?)",
                            (tipo, titulo, v, b, now_iso()))
        con.commit()

def now_iso(): return datetime.now(timezone.utc).isoformat()

# ------------------------------------------------------------------ afiliados (helpers)
def _hash_pw(password, salt=None):
    salt = salt or secrets.token_hex(8)
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex(), salt

def get_affiliate(code=None, email=None, token=None, coupon=None):
    with contextlib.closing(db()) as con:
        if code:
            r = con.execute("SELECT * FROM affiliates WHERE code=?", (code,)).fetchone()
        elif email:
            r = con.execute("SELECT * FROM affiliates WHERE email=?", (email,)).fetchone()
        elif coupon:
            r = con.execute("SELECT * FROM affiliates WHERE coupon=?", (coupon,)).fetchone()
        else:
            r = con.execute("SELECT * FROM affiliates WHERE token=?", (token,)).fetchone()
        return dict(r) if r else None

def gen_coupon(name, code, wanted=None):
    base = re.sub(r"[^A-Z0-9]", "", (wanted or name or "").upper())[:12]
    if not base or base == "NETO10":
        base = code.upper()
    cand, n = base, 1
    while get_affiliate(coupon=cand):
        n += 1
        cand = (base + str(n))[:14]
    return cand

def af_from_req(req):
    auth = req.headers.get("Authorization", "")
    tok = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    af = get_affiliate(token=tok) if tok else None
    if not af:
        raise HTTPException(401, "não autenticado")
    return af

def gen_code(email):
    base = "".join(ch for ch in email.split("@")[0].lower() if ch.isalnum())[:8] or "af"
    for _ in range(6):
        code = base + secrets.token_hex(2)
        if not get_affiliate(code=code):
            return code
    return "af" + secrets.token_hex(4)

def aff_link(code):
    return f"https://curso.liquix.tech/?ref={code}"

def recruit_link(code):
    return PUBLIC_BASE + "/afiliados?ref_af=" + code

def _fee_cents(method, amount_cents):
    """Taxa de pagamento — descontada da comissão do afiliado."""
    if method == "pix":
        return round(amount_cents * 0.05) + 200   # Cobrix: 5% + R$2
    return round(amount_cents * 0.09)              # cartão (Changelly ~9%)

def _insert_bonus(con, code, cents, note, now):
    """Credita um bônus (level 3) uma única vez por 'note' (idempotente)."""
    if not cents or cents <= 0:
        return
    if con.execute("SELECT 1 FROM commissions WHERE affiliate_code=? AND level=3 AND note=?", (code, note)).fetchone():
        return
    con.execute("""INSERT INTO commissions(order_id,affiliate_code,level,cents,status,note,created_at,updated_at)
                   VALUES('',?,3,?,'pending',?,?,?)""", (code, cents, note, now, now))

def _credit_goal_bonuses(con, code, now):
    """Credita bônus das metas/missões que o afiliado bateu (level 3). Idempotente."""
    if not code:
        return
    ym = now[:7]
    goals = con.execute("SELECT id,tipo,vendas,bonus_cents FROM goals WHERE active=1").fetchall()
    cache = {}
    def n(key, sql, *a):
        if key not in cache:
            cache[key] = con.execute(sql, a).fetchone()[0]
        return cache[key]
    for g in goals:
        tipo = g["tipo"] or "vendas_mes"; alvo = g["vendas"] or 0; bonus = g["bonus_cents"] or 0
        if alvo <= 0 or bonus <= 0:
            continue
        if tipo == "vendas_mes":
            if n("vm", "SELECT COUNT(*) FROM orders WHERE enrolled=1 AND method!='cortesia' AND affiliate_code=? AND substr(COALESCE(updated_at,created_at),1,7)=?", code, ym) >= alvo:
                _insert_bonus(con, code, bonus, "g%d:%s" % (g["id"], ym), now)
        elif tipo == "vendas_total":
            if n("vt", "SELECT COUNT(*) FROM orders WHERE enrolled=1 AND method!='cortesia' AND affiliate_code=?", code) >= alvo:
                _insert_bonus(con, code, bonus, "g%d" % g["id"], now)
        elif tipo == "indicados":
            if n("ind", "SELECT COUNT(*) FROM affiliates WHERE parent_code=?", code) >= alvo:
                _insert_bonus(con, code, bonus, "g%d" % g["id"], now)
        # 'manual' = o dono credita pelo painel, não automático

def credit_commission(order_id):
    o = find_order(order_id=order_id)
    if not o or not o.get("affiliate_code"):
        return
    with contextlib.closing(db()) as con:
        if con.execute("SELECT 1 FROM commissions WHERE order_id=?", (order_id,)).fetchone():
            return  # já creditado (idempotente)
    af = get_affiliate(code=o["affiliate_code"])
    if not af:
        return
    gross = o["amount_cents"] or 0
    net = max(0, gross - _fee_cents(o["method"], gross))   # líquido após taxa
    plan = PLANS.get(o["plan"], {})
    pct = plan.get("commission_pct") or af.get("pct") or 20
    pool = round(net * pct / 100.0)                         # bolo total que a casa paga nessa venda
    parent = af.get("parent_code")                          # MLM: quem indicou esse afiliado
    paf = get_affiliate(code=parent) if parent else None
    if paf:
        # o mestre (quem indicou) escolhe quanto o indicado ganha; ele fica com o resto do bolo
        give = paf.get("give_pct")
        give = DEFAULT_SUB_PCT if give is None else float(give)
        direct = round(net * give / 100.0)
        if direct > pool:
            direct = pool                                  # nunca paga mais que o bolo do plano
        over = pool - direct                               # override do mestre = sobra do bolo
        entries = [(o["affiliate_code"], 1, direct)]
        if over > 0:
            entries.append((parent, 2, over))
    else:
        direct = pool                                      # venda direta: afiliado leva o bolo inteiro
        entries = [(o["affiliate_code"], 1, direct)]
    with contextlib.closing(db()) as con:
        for (code, lvl, cents) in entries:
            con.execute("""INSERT INTO commissions(order_id,affiliate_code,level,cents,status,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?)""", (order_id, code, lvl, cents, "pending", now_iso(), now_iso()))
        con.execute("UPDATE orders SET commission_cents=?, commission_status='pending' WHERE order_id=?", (direct, order_id))
        _credit_goal_bonuses(con, o["affiliate_code"], now_iso())   # metas de venda do afiliado
        con.commit()

def save_order(o):
    with contextlib.closing(db()) as con:
        con.execute("""INSERT INTO orders(order_id,method,plan,email,name,amount_cents,provider_ref,status,affiliate_code,bump,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (o["order_id"],o["method"],o["plan"],o["email"],o.get("name"),o["amount_cents"],
                     o.get("provider_ref"),"pending",o.get("affiliate_code"),o.get("bump"),now_iso(),now_iso()))
        con.commit()

def find_order(order_id=None, provider_ref=None):
    with contextlib.closing(db()) as con:
        if order_id:
            r = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        else:
            r = con.execute("SELECT * FROM orders WHERE provider_ref=?", (provider_ref,)).fetchone()
        return dict(r) if r else None

def mark_order(order_id, **f):
    f["updated_at"] = now_iso()
    sets = ",".join(f"{k}=?" for k in f)
    with contextlib.closing(db()) as con:
        con.execute(f"UPDATE orders SET {sets} WHERE order_id=?", (*f.values(), order_id))
        con.commit()

def seen_before(event_id):
    with contextlib.closing(db()) as con:
        try:
            con.execute("INSERT INTO seen(event_id,at) VALUES(?,?)", (event_id, now_iso()))
            con.commit(); return False
        except sqlite3.IntegrityError:
            return True

# ------------------------------------------------------------------ Frappe LMS (matrícula)
async def frappe_enroll(email, name, course):
    """Cria o usuário (se preciso) e matricula no curso do LMS."""
    headers = {"Authorization": f"token {FRAPPE_API_KEY}:{FRAPPE_API_SECRET}",
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as c:
        # 1) garante o usuário
        r = await c.get(f"{FRAPPE_URL}/api/resource/User/{email}", headers=headers)
        if r.status_code == 404:
            parts = (name or email.split("@")[0]).split(" ", 1)
            await c.post(f"{FRAPPE_URL}/api/resource/User", headers=headers, json={
                "email": email, "first_name": parts[0],
                "last_name": parts[1] if len(parts) > 1 else "",
                "send_welcome_email": 1, "roles": [{"role": "LMS Student"}]})
        # 2) matrícula (idempotente: ignora se já existe)
        er = await c.post(f"{FRAPPE_URL}/api/resource/LMS Enrollment", headers=headers,
                          json={"member": email, "course": course})
        if er.status_code not in (200, 201, 409, 417):
            raise RuntimeError(f"Frappe enroll falhou {er.status_code}: {er.text[:300]}")

async def fb_capi(order):
    """Envia o Purchase pro Meta Conversions API (server-side). No-op se não configurado."""
    if not (META_PIXEL_ID and META_CAPI_TOKEN) or not order:
        return
    import hashlib, time
    email = (order.get("email") or "").strip().lower()
    ev = {"event_name": "Purchase", "event_time": int(time.time()),
          "event_id": order.get("order_id"), "action_source": "website",
          "event_source_url": PUBLIC_BASE + "/comprar",
          "user_data": {"em": [hashlib.sha256(email.encode()).hexdigest()]} if email else {},
          "custom_data": {"currency": "BRL",
                          "value": round((order.get("amount_cents") or 0) / 100.0, 2),
                          "content_name": order.get("plan") or "",
                          "ref": order.get("affiliate_code") or ""}}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post("https://graph.facebook.com/v19.0/%s/events" % META_PIXEL_ID,
                         params={"access_token": META_CAPI_TOKEN}, json={"data": [ev]})
    except Exception:
        pass

async def do_enroll(order):
    plan = PLANS.get(order["plan"], {})
    course = plan.get("course")
    if not course:
        raise RuntimeError(f"Plano sem curso: {order['plan']}")
    await frappe_enroll(order["email"], order.get("name"), course)
    if order.get("bump"):
        bp = PLANS.get(order["bump"], {})
        if bp.get("course"):
            await frappe_enroll(order["email"], order.get("name"), bp["course"])
    mark_order(order["order_id"], enrolled=1, status="paid")
    credit_commission(order["order_id"])
    await fb_capi(order)   # conversão server-side (Meta CAPI)

# ------------------------------------------------------------------ Cobrix (Pix)
async def cobrix_create_charge(name, cpf, amount_cents, description):
    h = {"X-Api-Key": COBRIX_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=40) as c:
        cl = await c.post(f"{COBRIX_API_URL}/clients", headers=h,
                          json={"name": name, "document": cpf, "whatsapp": ""})
        if cl.status_code == 201:
            client_id = cl.json()["id"]
        elif cl.status_code == 409:
            # CPF já cadastrado: reaproveita o cliente existente (a API devolve o id na mensagem)
            try:
                detail = (cl.json() or {}).get("detail", "")
            except Exception:
                detail = cl.text or ""
            m = re.search(r"\(cliente\s+([^)]+)\)", detail)
            client_id = m.group(1).strip() if m else None
            if not client_id:
                raise HTTPException(409, f"CPF já cadastrado e não consegui reaproveitar: {detail[:160]}")
        else:
            raise HTTPException(502, f"Cobrix /clients {cl.status_code}: {cl.text[:200]}")
        ch = await c.post(f"{COBRIX_API_URL}/charges", headers=h, json={
            "client_id": client_id, "amount_cents": amount_cents,
            "description": description, "fee_mode": "repassar", "send_whatsapp": False})
        if ch.status_code != 201:
            raise HTTPException(502, f"Cobrix /charges {ch.status_code}: {ch.text[:200]}")
        return ch.json()

def cobrix_verify(raw: bytes, sig_header: str, ts_header: str) -> bool:
    if not sig_header or not ts_header:
        return False
    try:
        if abs(time.time() - int(ts_header)) > REPLAY_WINDOW_S:
            return False
    except ValueError:
        return False
    expected = "sha256=" + hmac.new(COBRIX_WEBHOOK_SECRET.encode(),
                                    f"{ts_header}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)

# ------------------------------------------------------------------ Changelly (cartão)
def _priv_key():
    return serialization.load_der_private_key(base64.b64decode(CHANGELLY_PRIV_B64), password=None)

def changelly_sign(method, path, body: bytes):
    ts = str(int(time.time()) + 3600)
    payload = f"{method}:{path}:{base64.b64encode(body).decode() if body else ''}:{ts}"
    sig = _priv_key().sign(payload.encode(), padding.PKCS1v15(), hashes.SHA256())
    header = base64.b64encode((base64.b64encode(sig).decode() + ":" + ts).encode()).decode()
    return header

async def changelly_create_payment(order_id, usdt_amount, email, ip, title):
    path = "/api/payment/v1/payments"
    body = json.dumps({
        "type": "INVOICE", "nominal_currency": "USDT", "nominal_amount": usdt_amount,
        "order_id": order_id, "customer_email": email,
        "customer_ip_address": ip or "0.0.0.0", "customer_referer_domain": "cursos.liquix.tech",
        "title": title, "fees_payer": "CUSTOMER",
        "success_redirect_url": f"{FRAPPE_URL}/obrigado",
    }, separators=(",", ":")).encode()
    headers = {"X-Api-Key": CHANGELLY_API_KEY, "X-Signature": changelly_sign("POST", path, body),
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(CHANGELLY_API_URL + path, headers=headers, content=body)
        if r.status_code not in (200, 201):
            raise HTTPException(502, f"Changelly {r.status_code}: {r.text[:200]}")
        return r.json()

async def changelly_get_payment(payment_id):
    path = "/api/payment/v1/payments/" + payment_id
    headers = {"X-Api-Key": CHANGELLY_API_KEY, "X-Signature": changelly_sign("GET", path, b"")}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(CHANGELLY_API_URL + path, headers=headers)
        r.raise_for_status()
        return r.json()

def changelly_verify(raw: bytes, sig_header: str) -> bool:
    if not sig_header:
        return False
    try:
        inner = base64.b64decode(sig_header).decode()
        sig_b64, ts = inner.rsplit(":", 1)
        if not CHANGELLY_CB_PUB_B64:
            return False  # callback public key ainda não configurada (pega no painel Changelly)
        sig = base64.b64decode(sig_b64)
        pub = serialization.load_der_public_key(base64.b64decode(CHANGELLY_CB_PUB_B64))
        pub.verify(sig, (raw.decode() + ":" + ts).encode(), padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError, Exception):
        return False

# ------------------------------------------------------------------ rotas
@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/comprar")
async def comprar():
    html = open("/app/checkout.html", encoding="utf-8").read()
    if META_PIXEL_ID:
        html = html.replace("</head>", (PIXEL_SNIPPET % META_PIXEL_ID) + "</head>", 1)
    return HTMLResponse(html)

@app.get("/order-status/{order_id}")
async def order_status(order_id: str):
    o = find_order(order_id=order_id)
    if not o:
        raise HTTPException(404, "pedido não encontrado")
    return {"order_id": order_id, "status": o["status"], "enrolled": bool(o["enrolled"]),
            "plan": o["plan"], "amount_cents": o["amount_cents"]}

@app.post("/checkout")
async def checkout(req: Request):
    d = await req.json()
    plan = d.get("plan")
    method = d.get("method")   # "pix" | "card"
    email = (d.get("email") or "").strip().lower()
    name = d.get("name") or ""
    cpf = d.get("cpf") or ""
    raw_coupon = (d.get("coupon") or "").strip()
    ref = (d.get("ref") or "").strip()
    ref_af = get_affiliate(code=ref) if ref else None
    coupon_af = None
    if raw_coupon and raw_coupon.upper() not in ("NETO10", RECOVERY_COUPON):
        coupon_af = get_affiliate(coupon=raw_coupon.upper()) or get_affiliate(code=raw_coupon)
    the_af = ref_af or coupon_af
    affiliate_code = the_af["code"] if the_af else None
    coupon_ok = (raw_coupon.upper() == "NETO10") or bool(coupon_af)  # 10% off: NETO10 ou cupom de influencer
    is_recovery = raw_coupon.upper() == RECOVERY_COUPON             # cupom de recuperação: 15% (10% + 5%)
    bump = BUMP_PLAN if (d.get("bump") and plan in ("starter", "pro", "premium") and BUMP_PLAN in PLANS) else None
    if plan not in PLANS or method not in ("pix", "card") or "@" not in email:
        raise HTTPException(400, "dados inválidos (plan/method/email)")
    p = PLANS[plan]
    bump_cents = PLANS[BUMP_PLAN]["amount_cents"] if bump else 0  # bump flat (sem desconto)
    order_id = "VET-" + secrets.token_hex(6)
    desc = f"Curso Vende em Tudo - {p['label']}" + (" + Afiliado" if bump else "") + f" - {order_id}"
    o = {"order_id": order_id, "method": method, "plan": plan, "email": email,
         "name": name, "amount_cents": p["amount_cents"], "affiliate_code": affiliate_code, "bump": bump}

    if method == "pix":
        if len(cpf) < 11:
            raise HTTPException(400, "CPF obrigatório no Pix")
        disc = RECOVERY_DISCOUNT if is_recovery else PIX_DISCOUNT
        pix_cents = round(p["amount_cents"] * disc) + bump_cents  # 10% (ou 15% recuperação) off no Pix (+ bump flat)
        o["amount_cents"] = pix_cents
        ch = await cobrix_create_charge(name or email, cpf, pix_cents, desc)
        o["provider_ref"] = ch.get("id")
        save_order(o)
        return {"order_id": order_id, "method": "pix",
                "qr_copy_paste": ch.get("qr_copy_paste"), "qr_image_url": ch.get("qr_image_url"),
                "amount_cents": pix_cents, "charge_id": ch.get("id")}
    else:
        ip = req.headers.get("x-forwarded-for", "").split(",")[0].strip() or (req.client.host if req.client else "")
        cfac = RECOVERY_DISCOUNT if is_recovery else (PIX_DISCOUNT if coupon_ok else 1.0)
        card_usdt = round(float(p["usdt"]) * cfac
                          + (float(PLANS[BUMP_PLAN]["usdt"]) if bump else 0.0), 2)
        o["amount_cents"] = round(p["amount_cents"] * cfac) + bump_cents
        o["provider_ref"] = order_id  # Changelly usa o order_id
        save_order(o)
        pay = await changelly_create_payment(order_id, ("%.2f" % card_usdt), email, ip, desc)
        mark_order(order_id, provider_ref=pay.get("id") or order_id)
        return {"order_id": order_id, "method": "card",
                "payment_url": pay.get("payment_url"), "payment_id": pay.get("id")}

@app.post("/webhook/cobrix")
async def wh_cobrix(req: Request):
    raw = await req.body()
    if not cobrix_verify(raw, req.headers.get("X-Cobrix-Signature", ""),
                         req.headers.get("X-Cobrix-Timestamp", "")):
        raise HTTPException(401, "assinatura inválida")
    ev = json.loads(raw)
    if seen_before(ev.get("event_id", secrets.token_hex(8))):
        return JSONResponse({"ok": True, "dup": True})
    cob = ev.get("cobranca", {})
    if cob.get("status") == "liquidado":
        order = find_order(provider_ref=cob.get("id"))
        if order and not order["enrolled"]:
            if cob.get("valor_cobrado_cents") != order["amount_cents"]:
                mark_order(order["order_id"], status="valor_divergente")
                raise HTTPException(409, "valor divergente")
            await do_enroll(order)
    return JSONResponse({"ok": True})

@app.post("/webhook/changelly")
async def wh_changelly(req: Request):
    raw = await req.body()
    if not changelly_verify(raw, req.headers.get("X-Signature", "")):
        raise HTTPException(401, "assinatura inválida")
    ev = json.loads(raw)
    if seen_before("chg_" + str(ev.get("payment_id", secrets.token_hex(8))) + ":" + str(ev.get("state"))):
        return JSONResponse({"ok": True, "dup": True})
    if ev.get("state") == "COMPLETED":
        order = find_order(order_id=ev.get("order_id"))
        if order and not order["enrolled"]:
            await do_enroll(order)
    return JSONResponse({"ok": True})

# ------------------------------------------------------------------ afiliados (rotas)
@app.get("/afiliados")
async def afiliados_page():
    return FileResponse("/app/afiliados.html")

@app.get("/afiliados/painel")
async def afiliados_painel():
    return FileResponse("/app/painel.html")

@app.get("/a/{code}")
async def bio_page(code: str):
    af = get_affiliate(code=code)
    if not af:
        raise HTTPException(404, "afiliado não encontrado")
    html = open("/app/bio.html", encoding="utf-8").read()
    nome = (af.get("name") or "").strip()
    html = (html.replace("__NOME__", nome.split(" ")[0] if nome else "")
                .replace("__REF__", code)
                .replace("__CUPOM__", af.get("coupon") or ""))
    if META_PIXEL_ID:
        html = html.replace("</head>", (PIXEL_SNIPPET % META_PIXEL_ID) + "</head>", 1)
    return HTMLResponse(html)

@app.post("/api/af/turbine")
async def af_turbine(req: Request):
    af = af_from_req(req)
    d = await req.json()
    pac = (d.get("pacote") or "").strip().lower()
    if pac not in TURBINE:
        raise HTTPException(400, "pacote inválido")
    t = TURBINE[pac]
    with contextlib.closing(db()) as con:
        con.execute("INSERT INTO turbine_requests(affiliate_code,pacote,status,created_at) VALUES(?,?,?,?)",
                    (af["code"], pac, "novo", now_iso()))
        con.commit()
    corpo = ("<p>O afiliado <b>%s</b> (código %s) pediu o pacote de tráfego pago <b>%s · %s</b>.</p>"
             "<p>Link de venda dele: %s</p><p>Combine o pagamento e rode a campanha.</p>"
             % (af.get("name") or "", af["code"], t["label"], t["valor"], aff_link(af["code"])))
    await send_email(OWNER_EMAIL, "🚀 Turbine: %s quer o pacote %s" % (af.get("name") or af["code"], t["label"]), corpo)
    return {"ok": True, "pacote": t["label"]}

@app.post("/api/af/signup")
async def af_signup(req: Request):
    d = await req.json()
    name = (d.get("name") or "").strip()
    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""
    if not name or "@" not in email or len(password) < 6:
        raise HTTPException(400, "nome, e-mail e senha (mínimo 6) são obrigatórios")
    if get_affiliate(email=email):
        raise HTTPException(409, "já existe cadastro com esse e-mail — faça login")
    code = gen_code(email)
    coupon = gen_coupon(name, code, d.get("coupon"))
    parent = (d.get("parent") or d.get("ref_af") or "").strip()
    parent_code = parent if (parent and parent != code and get_affiliate(code=parent)) else None
    ph, salt = _hash_pw(password)
    token = secrets.token_urlsafe(24)
    with contextlib.closing(db()) as con:
        con.execute("""INSERT INTO affiliates(code,name,email,pass_hash,salt,liquid_wallet,usdt_wallet,pct,coupon,parent_code,token,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (code, name, email, ph, salt,
                     (d.get("liquid_wallet") or "").strip(),
                     (d.get("usdt_wallet") or "").strip(), 20, coupon, parent_code, token, now_iso()))
        if parent_code:
            _credit_goal_bonuses(con, parent_code, now_iso())       # missão "recrute N afiliados" do mestre
        con.commit()
    return {"code": code, "token": token, "link": aff_link(code), "coupon": coupon}

@app.post("/api/af/login")
async def af_login(req: Request):
    d = await req.json()
    email = (d.get("email") or "").strip().lower()
    af = get_affiliate(email=email)
    ph = _hash_pw(d.get("password") or "", af["salt"])[0] if af else ""
    if not af or not hmac.compare_digest(ph, af["pass_hash"]):
        raise HTTPException(401, "e-mail ou senha incorretos")
    return {"code": af["code"], "token": af["token"], "link": aff_link(af["code"]), "coupon": af.get("coupon")}

@app.get("/api/af/me")
async def af_me(req: Request):
    af = af_from_req(req)
    ym = datetime.now(timezone.utc).strftime("%Y-%m")
    with contextlib.closing(db()) as con:
        rows = [dict(r) for r in con.execute(
            """SELECT c.created_at, c.level, c.cents, c.status, c.note, o.plan
               FROM commissions c LEFT JOIN orders o ON o.order_id=c.order_id
               WHERE c.affiliate_code=? ORDER BY c.created_at DESC""", (af["code"],)).fetchall()]
        subs = con.execute("SELECT COUNT(*) n FROM affiliates WHERE parent_code=?", (af["code"],)).fetchone()["n"]
        vm = con.execute("SELECT COUNT(*) n FROM orders WHERE enrolled=1 AND method!='cortesia' AND affiliate_code=? AND substr(COALESCE(updated_at,created_at),1,7)=?", (af["code"], ym)).fetchone()["n"]
        vt = con.execute("SELECT COUNT(*) n FROM orders WHERE enrolled=1 AND method!='cortesia' AND affiliate_code=?", (af["code"],)).fetchone()["n"]
        goals = [dict(r) for r in con.execute("SELECT id,tipo,titulo,vendas,bonus_cents FROM goals WHERE active=1 ORDER BY tipo DESC, vendas").fetchall()]
    done = set(r["note"] for r in rows if r["level"] == 3 and r.get("note"))
    pend = sum(r["cents"] for r in rows if r["status"] == "pending")
    paid = sum(r["cents"] for r in rows if r["status"] == "paid")
    over = sum(r["cents"] for r in rows if r["level"] == 2)
    bonus_mes = sum(r["cents"] for r in rows if r["level"] == 3 and (r["created_at"] or "")[:7] == ym)
    def _niv(l): return "bônus" if l == 3 else ("indicação" if l == 2 else "direta")
    sales = [{"created_at": r["created_at"],
              "plan": (r["plan"] or ("🎁 Bônus de meta" if r["level"] == 3 else "")),
              "commission_cents": r["cents"], "commission_status": r["status"], "nivel": _niv(r["level"])} for r in rows]
    missoes = []
    for g in goals:
        tipo = g["tipo"] or "vendas_mes"; alvo = g["vendas"] or 0
        if tipo == "vendas_mes": prog = vm; note = "g%d:%s" % (g["id"], ym)
        elif tipo == "vendas_total": prog = vt; note = "g%d" % g["id"]
        elif tipo == "indicados": prog = subs; note = "g%d" % g["id"]
        else: prog = 0; note = "g%d" % g["id"]
        missoes.append({"titulo": g["titulo"] or "", "tipo": tipo, "alvo": alvo,
                        "bonus_cents": g["bonus_cents"], "progresso": prog, "atingida": note in done})
    return {"code": af["code"], "name": af["name"], "pct": af["pct"],
            "link": aff_link(af["code"]), "coupon": af.get("coupon"),
            "recruit_link": recruit_link(af["code"]), "subs": subs,
            "bio_link": PUBLIC_BASE + "/a/" + af["code"],
            "give_pct": af.get("give_pct") if af.get("give_pct") is not None else DEFAULT_SUB_PCT,
            "liquid_wallet": af["liquid_wallet"], "usdt_wallet": af["usdt_wallet"],
            "pix_key": af.get("pix_key") or "", "payout_method": af.get("payout_method") or "pix",
            "btc_address": af.get("btc_address") or "", "ln_address": af.get("ln_address") or "",
            "sales": sales,
            "metas": {"vendas_mes": vm, "vendas_total": vt, "bonus_mes_cents": bonus_mes, "missoes": missoes},
            "totals": {"count": len(rows), "pending_cents": pend, "paid_cents": paid, "override_cents": over}}

@app.post("/api/af/wallet")
async def af_wallet(req: Request):
    af = af_from_req(req)
    d = await req.json()
    sets, vals = [], []                                    # atualiza só os campos enviados (não apaga o resto)
    if "payout_method" in d:
        m = (d.get("payout_method") or "pix").strip().lower()
        if m not in ("pix", "liquid", "usdt", "btc", "lightning"):
            m = "pix"
        sets.append("payout_method=?"); vals.append(m)
    for key in ("liquid_wallet", "usdt_wallet", "pix_key", "btc_address", "ln_address"):
        if key in d:
            sets.append(key + "=?"); vals.append((d.get(key) or "").strip())
    if "give_pct" in d and str(d.get("give_pct")).strip() != "":
        try:
            sets.append("give_pct=?"); vals.append(max(0.0, min(40.0, float(d.get("give_pct")))))
        except (TypeError, ValueError):
            pass
    if sets:
        with contextlib.closing(db()) as con:
            con.execute("UPDATE affiliates SET " + ", ".join(sets) + " WHERE code=?", vals + [af["code"]])
            con.commit()
    return {"ok": True}

# ------------------------------------------------------------------ admin (dono)
def require_admin(req):
    key = req.headers.get("X-Admin-Key", "") or req.query_params.get("key", "")
    if not ADMIN_KEY or not hmac.compare_digest(key, ADMIN_KEY):
        raise HTTPException(401, "admin não autorizado")

def _dest(af):
    m = af.get("payout_method") or "pix"
    return {"pix": af.get("pix_key") or "", "liquid": af.get("liquid_wallet") or "",
            "usdt": af.get("usdt_wallet") or "", "btc": af.get("btc_address") or "",
            "lightning": af.get("ln_address") or ""}.get(m, "")

@app.get("/afiliados/admin")
async def afiliados_admin_page():
    return FileResponse("/app/admin.html")

@app.get("/afiliados/ferramentas")
async def afiliados_tools_page():
    return FileResponse("/app/ferramentas.html")

@app.get("/afiliados/widget.js")
async def afiliados_widget():
    return FileResponse("/app/widget.js", media_type="application/javascript")

@app.get("/api/af/admin/overview")
async def af_admin_overview(req: Request):
    require_admin(req)
    with contextlib.closing(db()) as con:
        affs = [dict(r) for r in con.execute("SELECT * FROM affiliates ORDER BY created_at DESC").fetchall()]
        out, tp, tpaid, ts = [], 0, 0, 0
        for a in affs:
            rows = con.execute("SELECT cents, status FROM commissions WHERE affiliate_code=?", (a["code"],)).fetchall()
            pend = sum((r["cents"] or 0) for r in rows if r["status"] == "pending")
            paid = sum((r["cents"] or 0) for r in rows if r["status"] == "paid")
            subs = con.execute("SELECT COUNT(*) n FROM affiliates WHERE parent_code=?", (a["code"],)).fetchone()["n"]
            tp += pend; tpaid += paid; ts += len(rows)
            out.append({"code": a["code"], "name": a["name"], "email": a["email"], "coupon": a.get("coupon"),
                        "payout_method": a.get("payout_method") or "pix", "pix_key": a.get("pix_key") or "",
                        "liquid_wallet": a.get("liquid_wallet") or "", "usdt_wallet": a.get("usdt_wallet") or "",
                        "parent_code": a.get("parent_code") or "", "subs": subs,
                        "give_pct": a.get("give_pct") if a.get("give_pct") is not None else DEFAULT_SUB_PCT,
                        "sales_count": len(rows), "pending_cents": pend, "paid_cents": paid})
    return {"totals": {"affiliates": len(affs), "sales": ts, "pending_cents": tp, "paid_cents": tpaid}, "affiliates": out}

@app.get("/api/af/admin/analytics")
async def af_admin_analytics(req: Request):
    require_admin(req)
    ym = datetime.now(timezone.utc).strftime("%Y-%m")        # mês corrente (UTC)
    with contextlib.closing(db()) as con:
        def scalar(q, *a):
            r = con.execute(q, a).fetchone()
            return (r[0] if r else 0) or 0
        PAID = "enrolled=1 AND method!='cortesia'"            # venda paga de verdade (exclui cortesia)
        fat_total = scalar("SELECT SUM(amount_cents) FROM orders WHERE " + PAID)
        fat_mes   = scalar("SELECT SUM(amount_cents) FROM orders WHERE " + PAID + " AND substr(COALESCE(updated_at,created_at),1,7)=?", ym)
        vendas    = scalar("SELECT COUNT(*) FROM orders WHERE " + PAID)
        vendas_mes= scalar("SELECT COUNT(*) FROM orders WHERE " + PAID + " AND substr(COALESCE(updated_at,created_at),1,7)=?", ym)
        checkouts = scalar("SELECT COUNT(*) FROM orders WHERE method!='cortesia'")
        abn       = scalar("SELECT COUNT(*) FROM orders WHERE enrolled=0 AND status!='paid'")
        abn_cents = scalar("SELECT SUM(amount_cents) FROM orders WHERE enrolled=0 AND status!='paid'")
        por_plano = [dict(r) for r in con.execute(
            "SELECT plan, COUNT(*) vendas, COALESCE(SUM(amount_cents),0) cents FROM orders WHERE " + PAID + " GROUP BY plan ORDER BY cents DESC")]
        por_metodo = [dict(r) for r in con.execute(
            """SELECT method,
                      SUM(CASE WHEN enrolled=1 THEN 1 ELSE 0 END) pagos,
                      SUM(CASE WHEN enrolled=0 AND status!='paid' THEN 1 ELSE 0 END) abandonados
               FROM orders WHERE method!='cortesia' GROUP BY method""")]
        serie = [dict(r) for r in con.execute(
            "SELECT substr(COALESCE(updated_at,created_at),1,10) dia, COUNT(*) vendas, COALESCE(SUM(amount_cents),0) cents FROM orders WHERE " + PAID + " GROUP BY dia ORDER BY dia DESC LIMIT 14")]
        aff_rows = con.execute(
            "SELECT CASE WHEN affiliate_code IS NOT NULL AND affiliate_code!='' THEN 1 ELSE 0 END viaaff, "
            "COUNT(*) v, COALESCE(SUM(amount_cents),0) c FROM orders WHERE " + PAID + " GROUP BY viaaff").fetchall()
        top_aff = [dict(r) for r in con.execute(
            "SELECT o.affiliate_code code, COALESCE(a.name,o.affiliate_code) name, COUNT(*) vendas, COALESCE(SUM(o.amount_cents),0) cents "
            "FROM orders o LEFT JOIN affiliates a ON a.code=o.affiliate_code "
            "WHERE " + PAID + " AND o.affiliate_code IS NOT NULL AND o.affiliate_code!='' "
            "GROUP BY o.affiliate_code ORDER BY cents DESC LIMIT 8")]
        cpend = scalar("SELECT SUM(cents) FROM commissions WHERE status='pending'")
        cpaid = scalar("SELECT SUM(cents) FROM commissions WHERE status='paid'")
    afiliado = {"aff_vendas": 0, "aff_cents": 0, "direto_vendas": 0, "direto_cents": 0}
    for r in aff_rows:
        if r["viaaff"] == 1:
            afiliado["aff_vendas"] = r["v"]; afiliado["aff_cents"] = r["c"]
        else:
            afiliado["direto_vendas"] = r["v"]; afiliado["direto_cents"] = r["c"]
    conv = round(vendas * 100.0 / checkouts, 1) if checkouts else 0.0
    ticket = round(fat_total / vendas) if vendas else 0
    return {"mes": ym,
            "kpi": {"fat_total_cents": fat_total, "fat_mes_cents": fat_mes,
                    "vendas": vendas, "vendas_mes": vendas_mes, "checkouts": checkouts,
                    "conversao": conv, "ticket_cents": ticket,
                    "abandonados": abn, "abandonados_cents": abn_cents},
            "por_plano": por_plano, "por_metodo": por_metodo,
            "serie": list(reversed(serie)),
            "afiliado": afiliado, "top_afiliados": top_aff,
            "comissoes": {"pending_cents": cpend, "paid_cents": cpaid}}

@app.get("/api/af/admin/carts")
async def af_admin_carts(req: Request):
    require_admin(req)
    with contextlib.closing(db()) as con:
        rows = [dict(r) for r in con.execute(
            """SELECT order_id, plan, email, name, amount_cents, method, status, affiliate_code,
                      created_at, COALESCE(updated_at,created_at) quando
               FROM orders WHERE enrolled=0 AND status!='paid'
               ORDER BY created_at DESC LIMIT 200""")]
    carrinhos = []
    for r in rows:
        plan = r["plan"] or ""
        label = PLANS.get(plan, {}).get("label", plan)
        link = PUBLIC_BASE + "/comprar?plano=" + plan + "&cupom=" + RECOVERY_COUPON
        if r.get("affiliate_code"):
            link += "&ref=" + r["affiliate_code"]
        nome1 = (r["name"] or "").strip().split(" ")[0]
        ola = ("Oi " + nome1) if nome1 else "Oi"
        msg = (ola + "! Vi que você começou a garantir o curso *" + label + "* e não finalizou. "
               "Ainda dá tempo — e com o cupom *" + RECOVERY_COUPON + "* você ganha 15% de desconto. "
               "É só terminar por aqui: " + link)
        carrinhos.append({"order_id": r["order_id"], "email": r["email"], "name": r["name"] or "",
                          "plan": plan, "plan_label": label, "amount_cents": r["amount_cents"],
                          "method": r["method"], "status": r["status"],
                          "created_at": r["created_at"], "quando": r["quando"],
                          "affiliate_code": r.get("affiliate_code") or "",
                          "recovery_link": link, "whatsapp_msg": msg})
    total_cents = sum((x["amount_cents"] or 0) for x in carrinhos)
    return {"total": len(carrinhos), "total_cents": total_cents, "carrinhos": carrinhos}

@app.get("/api/af/admin/students")
async def af_admin_students(req: Request):
    require_admin(req)
    q = (req.query_params.get("q") or "").strip().lower()
    with contextlib.closing(db()) as con:
        rows = [dict(r) for r in con.execute(
            """SELECT order_id, plan, email, name, amount_cents, method, affiliate_code, bump,
                      COALESCE(updated_at,created_at) quando
               FROM orders WHERE enrolled=1 ORDER BY quando DESC LIMIT 500""")]
    out = []
    for r in rows:
        if q and q not in (r["email"] or "").lower() and q not in (r["name"] or "").lower() and q not in (r["plan"] or "").lower():
            continue
        out.append({"order_id": r["order_id"], "email": r["email"], "name": r["name"] or "",
                    "plan": r["plan"], "plan_label": PLANS.get(r["plan"], {}).get("label", r["plan"]),
                    "amount_cents": r["amount_cents"], "method": r["method"],
                    "affiliate_code": r.get("affiliate_code") or "", "bump": r.get("bump") or "",
                    "quando": r["quando"]})
    return {"total": len(out), "alunos": out}

@app.post("/api/af/admin/reenroll")
async def af_admin_reenroll(req: Request):
    require_admin(req)
    d = await req.json()
    o = find_order(order_id=(d.get("order_id") or "").strip())
    if not o:
        raise HTTPException(404, "pedido não encontrado")
    try:
        await do_enroll(o)
    except Exception as e:
        raise HTTPException(502, "falha ao matricular: " + str(e)[:160])
    return {"ok": True, "email": o["email"], "plan": o["plan"]}

@app.post("/api/af/admin/grant")
async def af_admin_grant(req: Request):
    require_admin(req)
    d = await req.json()
    email = (d.get("email") or "").strip().lower()
    plan = (d.get("plan") or "").strip()
    name = (d.get("name") or "").strip()
    if "@" not in email or plan not in PLANS:
        raise HTTPException(400, "informe um e-mail válido e um plano existente")
    oid = "CORT-" + secrets.token_hex(6)
    save_order({"order_id": oid, "method": "cortesia", "plan": plan, "email": email,
                "name": name, "amount_cents": 0, "provider_ref": None,
                "affiliate_code": None, "bump": ""})
    try:
        await do_enroll(find_order(order_id=oid))
    except Exception as e:
        raise HTTPException(502, "acesso registrado, mas a matrícula falhou: " + str(e)[:160])
    return {"ok": True, "email": email, "plan": plan, "course": PLANS[plan].get("course")}

@app.get("/api/af/admin/export")
async def af_admin_export(req: Request):
    require_admin(req)
    from starlette.responses import PlainTextResponse
    import csv, io
    what = (req.query_params.get("what") or "sales").strip()
    buf = io.StringIO()
    w = csv.writer(buf)
    with contextlib.closing(db()) as con:
        if what == "affiliates":
            w.writerow(["codigo", "nome", "email", "cupom", "indicado_por", "metodo", "destino", "criado"])
            for a in con.execute("SELECT * FROM affiliates ORDER BY created_at"):
                a = dict(a)
                dest = a.get("pix_key") or a.get("liquid_wallet") or a.get("usdt_wallet") or a.get("btc_address") or a.get("ln_address") or ""
                w.writerow([a["code"], a["name"], a["email"], a.get("coupon") or "", a.get("parent_code") or "",
                            a.get("payout_method") or "pix", dest, a.get("created_at") or ""])
            fname = "afiliados.csv"
        elif what == "carts":
            w.writerow(["quando", "nome", "email", "plano", "valor_reais", "metodo", "status"])
            for r in con.execute("SELECT * FROM orders WHERE enrolled=0 AND status!='paid' ORDER BY created_at DESC"):
                r = dict(r)
                w.writerow([(r.get("created_at") or "")[:16], r.get("name") or "", r["email"], r["plan"],
                            "%.2f" % ((r["amount_cents"] or 0) / 100), r["method"], r["status"]])
            fname = "carrinhos.csv"
        else:
            w.writerow(["quando", "nome", "email", "plano", "valor_reais", "metodo", "afiliado", "order_id"])
            for r in con.execute("SELECT * FROM orders WHERE enrolled=1 AND method!='cortesia' ORDER BY COALESCE(updated_at,created_at) DESC"):
                r = dict(r)
                w.writerow([(r.get("updated_at") or r.get("created_at") or "")[:16], r.get("name") or "", r["email"], r["plan"],
                            "%.2f" % ((r["amount_cents"] or 0) / 100), r["method"], r.get("affiliate_code") or "", r["order_id"]])
            fname = "vendas.csv"
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="%s"' % fname})

@app.post("/api/af/admin/cart/email")
async def af_admin_cart_email(req: Request):
    require_admin(req)
    d = await req.json()
    with contextlib.closing(db()) as con:
        if d.get("all"):
            oids = [r[0] for r in con.execute(
                "SELECT order_id FROM orders WHERE enrolled=0 AND status!='paid' ORDER BY created_at DESC LIMIT 200")]
        else:
            oid = (d.get("order_id") or "").strip()
            oids = [oid] if oid else []
    disc_pct = round((1.0 - RECOVERY_DISCOUNT) * 100)
    results, sent = [], 0
    for oid in oids:
        o = find_order(order_id=oid)
        if not o or o.get("enrolled") or o.get("status") == "paid":
            results.append({"order_id": oid, "ok": False, "error": "não elegível"}); continue
        email = (o.get("email") or "").strip()
        if "@" not in email:
            results.append({"order_id": oid, "ok": False, "error": "sem e-mail"}); continue
        plan = o["plan"]; label = PLANS.get(plan, {}).get("label", plan)
        link = PUBLIC_BASE + "/comprar?plano=" + plan + "&cupom=" + RECOVERY_COUPON
        if o.get("affiliate_code"):
            link += "&ref=" + o["affiliate_code"]
        html = recovery_email_html(o.get("name"), label, link, RECOVERY_COUPON, disc_pct)
        res = await send_email(email, "Faltou pouco pro seu curso 🚀 (cupom de %d%%)" % disc_pct, html)
        if res.get("ok"):
            sent += 1
        results.append({"order_id": oid, "email": email, "ok": res.get("ok"), "error": res.get("error")})
    return {"enviados": sent, "total": len(oids), "configurado": bool(RESEND_API_KEY), "resultados": results}

@app.get("/api/af/admin/goals")
async def af_admin_goals(req: Request):
    require_admin(req)
    with contextlib.closing(db()) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id,tipo,titulo,vendas,bonus_cents,active FROM goals ORDER BY active DESC, tipo DESC, vendas").fetchall()]
    return {"metas": rows}

@app.post("/api/af/admin/goals")
async def af_admin_goals_save(req: Request):
    require_admin(req)
    d = await req.json()
    tipo = (d.get("tipo") or "vendas_mes").strip()
    if tipo not in ("vendas_mes", "vendas_total", "indicados", "manual"):
        tipo = "vendas_mes"
    titulo = (d.get("titulo") or "").strip()
    try:
        alvo = int(d.get("vendas") or 0)
    except (TypeError, ValueError):
        alvo = 0
    try:
        if d.get("bonus_reais") is not None:
            bonus = int(round(float(d.get("bonus_reais")) * 100))
        else:
            bonus = int(d.get("bonus_cents") or 0)
    except (TypeError, ValueError):
        bonus = 0
    active = 1 if d.get("active", True) else 0
    gid = d.get("id")
    with contextlib.closing(db()) as con:
        if gid:
            con.execute("UPDATE goals SET tipo=?, titulo=?, vendas=?, bonus_cents=?, active=? WHERE id=?",
                        (tipo, titulo, alvo, bonus, active, gid))
        else:
            con.execute("INSERT INTO goals(tipo,titulo,vendas,bonus_cents,active,created_at) VALUES(?,?,?,?,?,?)",
                        (tipo, titulo, alvo, bonus, active, now_iso()))
        con.commit()
    return {"ok": True}

@app.post("/api/af/admin/goals/delete")
async def af_admin_goals_delete(req: Request):
    require_admin(req)
    d = await req.json()
    gid = d.get("id")
    if not gid:
        raise HTTPException(400, "id obrigatório")
    with contextlib.closing(db()) as con:
        con.execute("DELETE FROM goals WHERE id=?", (gid,))
        con.commit()
    return {"ok": True}

@app.get("/api/af/admin/payouts")
async def af_admin_payouts(req: Request):
    require_admin(req)
    month = req.query_params.get("month") or ""
    with contextlib.closing(db()) as con:
        rows = con.execute("""SELECT affiliate_code, SUM(cents) cents, COUNT(*) n FROM commissions
                              WHERE status='pending' AND (?='' OR substr(created_at,1,7)=?)
                              GROUP BY affiliate_code""", (month, month)).fetchall()
    itens, total = [], 0
    for r in rows:
        a = get_affiliate(code=r["affiliate_code"])
        if not a:
            continue
        total += r["cents"]
        itens.append({"code": a["code"], "nome": a["name"], "email": a["email"],
                      "metodo": a.get("payout_method") or "pix", "destino": _dest(a),
                      "comissao_cents": r["cents"], "vendas": r["n"]})
    itens.sort(key=lambda x: -x["comissao_cents"])
    return {"mes": month or "todos", "total_cents": total, "itens": itens}

@app.post("/api/af/admin/close")
async def af_admin_close(req: Request):
    require_admin(req)
    d = await req.json()
    month = (d.get("month") or "").strip()
    with contextlib.closing(db()) as con:
        if month:
            con.execute("UPDATE commissions SET status='paid', updated_at=? WHERE status='pending' AND substr(created_at,1,7)=?", (now_iso(), month))
        else:
            con.execute("UPDATE commissions SET status='paid', updated_at=? WHERE status='pending'", (now_iso(),))
        changes = con.total_changes
        con.commit()
    return {"ok": True, "pagos": changes}

async def crypto_send(method, address, cents, idem, name):
    """Chama o enviador externo do dono (ex.: BTC). Nunca move nada sem URL+chave configuradas."""
    if method == "btc":
        if not BTC_SEND_URL:
            return {"ok": False, "error": "BTC_SEND_URL não configurada"}
        body = {"method": "btc", "address": address, "amount_brl": round(cents / 100, 2),
                "idempotency_key": idem, "affiliate": name, "reference": idem}
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(BTC_SEND_URL, headers={"X-Api-Key": BTC_SEND_KEY, "Content-Type": "application/json"}, json=body)
        except Exception as e:
            return {"ok": False, "error": "conexão: " + str(e)[:120]}
        if r.status_code in (200, 201):
            try:
                j = r.json()
            except Exception:
                j = {}
            ok = bool(j.get("ok") or j.get("txid") or str(j.get("status", "")).lower() in ("sent", "ok", "paid"))
            return {"ok": ok, "txid": j.get("txid") or j.get("tx"), "raw": j}
        return {"ok": False, "error": "%s: %s" % (r.status_code, r.text[:140])}
    return {"ok": False, "error": "método sem envio automático: " + method}

async def send_email(to, subject, html):
    """E-mail transacional via Resend (só envia se RESEND_API_KEY estiver configurada)."""
    if not RESEND_API_KEY:
        return {"ok": False, "error": "e-mail não configurado (RESEND_API_KEY)"}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.resend.com/emails",
                             headers={"Authorization": "Bearer " + RESEND_API_KEY, "Content-Type": "application/json"},
                             json={"from": RESEND_FROM, "to": [to], "subject": subject, "html": html})
        if r.status_code in (200, 201):
            return {"ok": True, "id": (r.json() or {}).get("id")}
        return {"ok": False, "error": "%s: %s" % (r.status_code, r.text[:160])}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}

def recovery_email_html(name, label, link, coupon, disc_pct):
    nome1 = (name or "").strip().split(" ")[0]
    ola = ("Oi " + nome1) if nome1 else "Oi"
    return (
        '<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:520px;margin:auto;color:#0b1f1a">'
        '<h2 style="color:#0e8c7f;margin:0 0 8px">' + ola + '! Faltou pouco 🚀</h2>'
        '<p>Você começou a garantir o curso <b>' + label + '</b> e não finalizou — guardei sua vaga.</p>'
        '<p>Pra te dar um empurrãozinho, use o cupom '
        '<b style="background:#e9fbf5;padding:3px 10px;border-radius:6px;letter-spacing:1px">' + coupon + '</b> '
        'e ganhe <b>' + str(disc_pct) + '% de desconto</b>.</p>'
        '<p style="text-align:center;margin:26px 0">'
        '<a href="' + link + '" style="background:#15d6b6;color:#04211d;font-weight:800;text-decoration:none;padding:14px 26px;border-radius:12px;display:inline-block">Terminar minha compra</a>'
        '</p>'
        '<p style="color:#6b8c84;font-size:13px">Oferta por tempo limitado. Dúvida? É só responder este e-mail.</p>'
        '<p style="color:#9fb8b0;font-size:12px;margin-top:18px">Destravando as Vendas · Liquix</p>'
        '</div>')

@app.post("/api/af/admin/pay")
async def af_admin_pay(req: Request):
    require_admin(req)
    d = await req.json()
    month = (d.get("month") or "").strip()
    confirm = bool(d.get("confirm"))
    with contextlib.closing(db()) as con:
        rows = con.execute("""SELECT affiliate_code, SUM(cents) cents, COUNT(*) n FROM commissions
                              WHERE status='pending' AND (?='' OR substr(created_at,1,7)=?)
                              GROUP BY affiliate_code""", (month, month)).fetchall()
    itens = []
    for r in rows:
        a = get_affiliate(code=r["affiliate_code"])
        if not a:
            continue
        method = a.get("payout_method") or "pix"
        destino = _dest(a)
        cents = r["cents"]
        idem = "pay:%s:%s" % (month or "all", a["code"])
        it = {"code": a["code"], "nome": a["name"], "metodo": method, "destino": destino,
              "comissao_cents": cents, "vendas": r["n"]}
        if not destino:
            it["status"] = "sem_carteira"; itens.append(it); continue
        if cents > PAYOUT_MAX_CENTS:
            it["status"] = "acima_do_teto"; itens.append(it); continue
        if not confirm:
            it["status"] = "pronto"; itens.append(it); continue
        if method == "btc":
            with contextlib.closing(db()) as con:
                prev = con.execute("SELECT status, txid FROM payouts WHERE idem_key=?", (idem,)).fetchone()
            if prev and prev["status"] == "sent":
                it["status"] = "ja_enviado"; it["txid"] = prev["txid"]; itens.append(it); continue
            res = await crypto_send("btc", destino, cents, idem, a["name"])
            st = "sent" if res.get("ok") else "falhou"
            with contextlib.closing(db()) as con:
                con.execute("""INSERT OR REPLACE INTO payouts(idem_key,affiliate_code,month,method,destino,cents,status,txid,created_at)
                               VALUES(?,?,?,?,?,?,?,?,?)""",
                            (idem, a["code"], month, method, destino, cents, st, res.get("txid"), now_iso()))
                if st == "sent":
                    con.execute("""UPDATE commissions SET status='paid', updated_at=? WHERE affiliate_code=?
                                   AND status='pending' AND (?='' OR substr(created_at,1,7)=?)""",
                                (now_iso(), a["code"], month, month))
                con.commit()
            it["status"] = st; it["txid"] = res.get("txid"); it["erro"] = res.get("error")
        else:
            it["status"] = "manual"
        itens.append(it)
    return {"mes": month or "todos", "confirm": confirm, "itens": itens}

# ---- Poller do cartão (confirma via Changelly sem depender da callback) ----
async def card_poller():
    while True:
        try:
            with contextlib.closing(db()) as con:
                rows = [dict(r) for r in con.execute(
                    "SELECT * FROM orders WHERE method='card' AND status='pending' AND enrolled=0").fetchall()]
            for o in rows:
                try:
                    created = datetime.fromisoformat(o["created_at"])
                    if (datetime.now(timezone.utc) - created).total_seconds() > 3 * 3600:
                        mark_order(o["order_id"], status="expired"); continue
                except Exception:
                    pass
                pid = o.get("provider_ref")
                if not pid:
                    continue
                try:
                    p = await changelly_get_payment(pid)
                    st = p.get("state")
                    if st == "COMPLETED":
                        await do_enroll(o)
                    elif st in ("FAILED", "CANCELED"):
                        mark_order(o["order_id"], status=st.lower())
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(30)

@app.on_event("startup")
async def _startup():
    asyncio.create_task(card_poller())

init_db()
