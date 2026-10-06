# -*- coding: utf-8 -*-
"""Comissões de afiliados a pagar (todo dia 05, referente ao mês anterior).

Uso:
  python payout.py            -> lote do mês ANTERIOR (JSON) para pagamento
  python payout.py 2026-10    -> lote de um mês específico (YYYY-MM)
  python payout.py close 2026-10 -> marca as comissões 'pending' daquele mês como 'paid'

O ENVIO do dinheiro é humano (revisão): este script só CALCULA e LISTA quem receber,
quanto, e em qual carteira (Liquid / USDT). Nunca movimenta fundos sozinho.
"""
import os, sys, json, sqlite3
from datetime import datetime, timezone

DB = os.environ.get("DB_PATH", "/data/ponte.db")


def prev_month():
    now = datetime.now(timezone.utc)
    y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return f"{y:04d}-{m:02d}"


def rows_for(con, month):
    return con.execute(
        """SELECT o.affiliate_code, a.name, a.email, a.liquid_wallet, a.usdt_wallet,
                  SUM(o.commission_cents) AS cents, COUNT(*) AS vendas
           FROM orders o JOIN affiliates a ON a.code = o.affiliate_code
           WHERE o.commission_status='pending' AND substr(o.updated_at,1,7)=?
           GROUP BY o.affiliate_code ORDER BY cents DESC""", (month,)).fetchall()


def main():
    args = sys.argv[1:]
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    if args and args[0] == "close":
        month = args[1] if len(args) > 1 else prev_month()
        con.execute("""UPDATE orders SET commission_status='paid'
                       WHERE commission_status='pending' AND substr(updated_at,1,7)=?""", (month,))
        con.commit()
        print(json.dumps({"fechado": month, "alteracoes": con.total_changes}, ensure_ascii=False))
        return
    month = args[0] if args else prev_month()
    rows = rows_for(con, month)
    out = {
        "mes": month,
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "total_reais": round(sum(r["cents"] for r in rows) / 100.0, 2),
        "afiliados": [
            {"code": r["affiliate_code"], "nome": r["name"], "email": r["email"],
             "vendas": r["vendas"], "comissao_reais": round(r["cents"] / 100.0, 2),
             "carteira_liquid": r["liquid_wallet"], "carteira_usdt": r["usdt_wallet"]}
            for r in rows
        ],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
