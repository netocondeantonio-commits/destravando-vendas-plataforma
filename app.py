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
from fastapi.responses import JSONResponse, FileResponse
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
        con.execute("CREATE INDEX IF NOT EXISTS idx_af_code ON orders(affiliate_code)")
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
    return FileResponse("/app/checkout.html")

@app.get("/order-status/{order_id}")
async def order_status(order_id: str):
    o = find_order(order_id=order_id)
    if not o:
        raise HTTPException(404, "pedido não encontrado")
    return {"order_id": order_id, "status": o["status"], "enrolled": bool(o["enrolled"]), "plan": o["plan"]}

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
    if raw_coupon and raw_coupon.upper() != "NETO10":
        coupon_af = get_affiliate(coupon=raw_coupon.upper()) or get_affiliate(code=raw_coupon)
    the_af = ref_af or coupon_af
    affiliate_code = the_af["code"] if the_af else None
    coupon_ok = (raw_coupon.upper() == "NETO10") or bool(coupon_af)  # 10% off: NETO10 ou cupom de influencer
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
        pix_cents = round(p["amount_cents"] * PIX_DISCOUNT) + bump_cents  # 10% off no Pix (+ bump flat)
        o["amount_cents"] = pix_cents
        ch = await cobrix_create_charge(name or email, cpf, pix_cents, desc)
        o["provider_ref"] = ch.get("id")
        save_order(o)
        return {"order_id": order_id, "method": "pix",
                "qr_copy_paste": ch.get("qr_copy_paste"), "qr_image_url": ch.get("qr_image_url"),
                "amount_cents": pix_cents, "charge_id": ch.get("id")}
    else:
        ip = req.headers.get("x-forwarded-for", "").split(",")[0].strip() or (req.client.host if req.client else "")
        card_usdt = round(float(p["usdt"]) * (PIX_DISCOUNT if coupon_ok else 1.0)
                          + (float(PLANS[BUMP_PLAN]["usdt"]) if bump else 0.0), 2)
        o["amount_cents"] = round(p["amount_cents"] * (PIX_DISCOUNT if coupon_ok else 1.0)) + bump_cents
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
    with contextlib.closing(db()) as con:
        rows = [dict(r) for r in con.execute(
            """SELECT c.created_at, c.level, c.cents, c.status, o.plan
               FROM commissions c JOIN orders o ON o.order_id=c.order_id
               WHERE c.affiliate_code=? ORDER BY c.created_at DESC""", (af["code"],)).fetchall()]
        subs = con.execute("SELECT COUNT(*) n FROM affiliates WHERE parent_code=?", (af["code"],)).fetchone()["n"]
    pend = sum(r["cents"] for r in rows if r["status"] == "pending")
    paid = sum(r["cents"] for r in rows if r["status"] == "paid")
    over = sum(r["cents"] for r in rows if r["level"] == 2)
    sales = [{"created_at": r["created_at"], "plan": r["plan"], "commission_cents": r["cents"],
              "commission_status": r["status"], "nivel": "indicação" if r["level"] == 2 else "direta"} for r in rows]
    return {"code": af["code"], "name": af["name"], "pct": af["pct"],
            "link": aff_link(af["code"]), "coupon": af.get("coupon"),
            "recruit_link": recruit_link(af["code"]), "subs": subs,
            "give_pct": af.get("give_pct") if af.get("give_pct") is not None else DEFAULT_SUB_PCT,
            "liquid_wallet": af["liquid_wallet"], "usdt_wallet": af["usdt_wallet"],
            "pix_key": af.get("pix_key") or "", "payout_method": af.get("payout_method") or "pix",
            "btc_address": af.get("btc_address") or "", "ln_address": af.get("ln_address") or "",
            "sales": sales,
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
