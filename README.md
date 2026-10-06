# liquix-affiliates

Plataforma de afiliados **própria da Liquix** para o curso **Destravando as Vendas**. Backend em FastAPI (Python) com rastreio por link, cupom e cookie, cálculo de comissão sobre o líquido e **repasse em múltiplos métodos: Pix, Liquid/DePix e USDT**.

> Stack: FastAPI · Uvicorn · SQL (SQLite/Postgres) · HTML estático · JS vanilla · Docker.

---

## O que é

Sistema completo de afiliados, sem depender de plataforma de terceiros:

- O afiliado se cadastra, recebe um **código** (link `?ref` + cupom) e divulga.
- Cada clique grava um **cookie de 30 dias** (atribuição last-click).
- Quando o gateway confirma a venda, a plataforma **atribui ao afiliado** e **calcula a comissão sobre o valor líquido** (já descontada a taxa).
- As comissões fecham por mês e são **pagas todo dia 05 do mês seguinte**, com **conferência e envio manual** (nunca automático).

Inclui painel do afiliado, painel admin, página de ferramentas e um widget embutível.

---

## Principais features

- **Cadastro e login de afiliado** (JWT), com cadastro de destino de pagamento.
- **Rastreio triplo:** link `?ref`, **cupom** e **cookie de 30 dias**.
- **Atribuição por prioridade:** `cupom informado` > `?ref da URL` > `cookie (30 dias)`.
- **Cálculo de comissão** em centavos (inteiros), `% sobre o líquido` — taxa do gateway descontada antes.
- **🔥 Multi-pagamento cripto + Pix:** repasse em **Pix (via Cobrix)**, **Liquid/DePix** ou **USDT** — escolha do afiliado.
- **Fechamento mensal** com lote de repasses no **dia 05**.
- **Painel do afiliado:** métricas (cliques, conversões, saldo), extrato de comissões, histórico de repasses, gerador de links/cupom.
- **Painel admin:** lista de afiliados, métricas, fila de pagamentos e **aprovação manual** de cada repasse.
- **Página de ferramentas:** links prontos, cupom, banners e snippet do widget.
- **Widget embutível** (`widget.js`): grava o `ref`, seta o cookie e reescreve os CTAs de compra.
- **Webhook assinado** do gateway, com **idempotência** (sem comissão duplicada).

---

## Métodos de pagamento (multi-pagamento)

O afiliado define um método em `PUT /api/afiliados/me/pagamento`. Todos passam por **conferência humana** antes do envio.

| Método | Rede | Campo de destino | Integração |
|---|---|---|---|
| **Pix** | BR | Chave Pix (e-mail, CPF, aleatória...) | Gateway **Cobrix** |
| **DePix / Liquid** | Liquid Network | Endereço Liquid | **DePix** |
| **USDT** | Definida em `USDT_REDE` (ex: Tron/Polygon/Liquid) | Endereço da carteira | Hot wallet própria |

O valor em BRL é convertido para o ativo no momento do fechamento (cotação registrada no repasse). Mínimo de repasse configurável; abaixo do mínimo, a comissão acumula para o mês seguinte.

---

## Modelo de comissão e repasse

**Comissão** (valores sempre em centavos, inteiros — sem float):

```
liquido  = bruto - taxa_gateway
comissao = liquido * COMISSAO_PADRAO   # ex.: 30%
```

Exemplo: venda de **R$ 497,00**, taxa de **R$ 7,00** → líquido **R$ 490,00** → comissão a 30% = **R$ 147,00**.

**Repasse:**

1. A comissão nasce com status `pendente` ao confirmar a venda.
2. No fechamento (`POST /api/admin/pagamentos/gerar`), as comissões do mês viram um **lote de repasse** por afiliado/método.
3. **Todo dia 05 do mês seguinte** o admin revisa a fila (`/api/admin/pagamentos/pendentes`).
4. Cada repasse exige **aprovação humana** (`.../aprovar`) — só então o envio é disparado.

---

## Estrutura do repositório

```
liquix-affiliates/
├── backend/
│   └── app.py            # API FastAPI (auth, rastreio, comissão, webhook, admin)
├── web/
│   ├── afiliados.html    # landing + cadastro/login
│   ├── painel.html       # painel do afiliado (métricas, extrato, links)
│   ├── ferramentas.html  # links, cupom, banners, snippet do widget
│   └── admin.html        # painel admin (afiliados + fila de pagamentos)
├── widget/
│   └── widget.js         # widget embutível (ref + cookie + reescrita de CTA)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Como rodar (Docker)

```bash
# 1. configure o ambiente
cp .env.example .env
#    edite .env com as chaves do Cobrix, DePix e USDT e o APP_SECRET

# 2. suba com docker compose
docker compose up -d --build
```

- API / painel do afiliado: `http://localhost:8080/`
- Página de ferramentas: `http://localhost:8080/ferramentas.html`
- Painel admin: `http://localhost:8080/admin.html`
- Widget: `http://localhost:8080/widget/widget.js`

Sem compose:

```bash
docker build -t liquix-affiliates .
docker run -d --env-file .env -p 8080:8080 liquix-affiliates
```

Desenvolvimento local (sem Docker):

```bash
pip install -r requirements.txt
uvicorn backend.app:app --reload --port 8080
```

**Widget** — para embutir em qualquer página de vendas:

```html
<script src="https://afiliados.liquix.tech/widget/widget.js"
        data-ref="SEUCODIGO" defer></script>
```

---

## Rotas da API

### Público / rastreio

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/r/{codigo}` | Registra o clique, grava cookie `lqx_ref` (30 dias) e redireciona para `CHECKOUT_URL`. |
| `GET` | `/api/track?ref={codigo}` | Mesma atribuição via query (para links/embutidos). |
| `GET` | `/api/widget/config` | Config pública consumida por `widget.js`. |

### Afiliado (requer token)

| Método | Rota | Descrição |
|---|---|---|
| `POST` | `/api/afiliados/registrar` | Cadastro (nome, e-mail, senha, método de pagamento). |
| `POST` | `/api/afiliados/login` | Autentica e devolve JWT. |
| `GET` | `/api/afiliados/me` | Dados do afiliado logado. |
| `PUT` | `/api/afiliados/me/pagamento` | Define método e destino (Pix / DePix-Liquid / USDT). |
| `GET` | `/api/afiliados/me/metricas` | Cliques, conversões, comissão acumulada, saldo a pagar. |
| `GET` | `/api/afiliados/me/comissoes` | Extrato de comissões. |
| `GET` | `/api/afiliados/me/pagamentos` | Histórico de repasses. |
| `GET` | `/api/afiliados/me/links` | Gera link `?ref` e cupom de divulgação. |

### Conversão / webhook

| Método | Rota | Descrição |
|---|---|---|
| `POST` | `/api/webhooks/cobrix` | Recebe confirmação de venda do gateway. **Assinatura verificada + idempotente.** Atribui ao afiliado (cupom > ref > cookie) e gera comissão sobre o líquido. |

### Admin (requer token admin)

| Método | Rota | Descrição |
|---|---|---|
| `POST` | `/api/admin/login` | Login do admin. |
| `GET` | `/api/admin/afiliados` | Lista afiliados + métricas. |
| `GET` | `/api/admin/afiliados/{id}` | Detalhe de um afiliado. |
| `GET` | `/api/admin/pagamentos/pendentes` | Fila de repasses do mês. |
| `POST` | `/api/admin/pagamentos/gerar` | Fecha o mês e monta o lote de repasses (`pendente`). |
| `POST` | `/api/admin/pagamentos/{id}/aprovar` | **Conferência humana**; só então o envio é disparado. |

Estáticos: `/`, `/painel.html`, `/ferramentas.html`, `/admin.html`, `/widget/widget.js`.

---

## Variáveis de ambiente

```env
# App
APP_SECRET=            # segredo p/ assinar JWT (obrigatório)
DATABASE_URL=          # sqlite:///data/afiliados.db  ou  postgresql://...
BASE_URL=              # URL pública da plataforma
CHECKOUT_URL=          # destino do redirect (página de vendas do curso)
COOKIE_DIAS=30
DIA_PAGAMENTO=5
COMISSAO_PADRAO=0.30   # 30% sobre o líquido
REPASSE_MINIMO=5000    # em centavos (abaixo disso, acumula)
CORS_ORIGINS=https://liquix.tech,https://cursos.liquix.tech

# Admin
ADMIN_EMAIL=
ADMIN_SENHA_HASH=      # hash da senha (nunca em texto puro)

# Pix (gateway Cobrix)
COBRIX_API_URL=
COBRIX_API_KEY=
COBRIX_WEBHOOK_SECRET= # verificação HMAC do webhook

# Liquid / DePix
DEPIX_API_URL=
DEPIX_API_KEY=

# USDT
USDT_REDE=             # ex: tron | polygon | liquid
USDT_RPC_URL=
USDT_WALLET=           # carteira de origem dos repasses
```

Os segredos são lidos **apenas** de variáveis de ambiente e validados no startup — a aplicação não sobe sem `APP_SECRET`, `DATABASE_URL` e `COBRIX_WEBHOOK_SECRET`.

---

## ⚠️ Aviso de segurança

**Nenhum envio de dinheiro é automático.** A plataforma apenas **calcula** e **enfileira** os repasses.

- Todo repasse (Pix, DePix/Liquid ou USDT) exige **aprovação humana** — conferência de **valor, método e destino** — antes de qualquer envio. O dia 05 dispara a fila, não o pagamento.
- O webhook de vendas é **assinado (HMAC)** e **idempotente**: a comissão só é creditada após validar a assinatura, e uma mesma venda nunca gera comissão duplicada.
- Valores monetários sempre em **inteiros (centavos)** — sem `float`.
- Chaves de API, segredos e carteiras ficam **somente em variáveis de ambiente**, nunca no código ou no repositório.
- Entradas validadas por schema (Pydantic) em todas as rotas; endpoints admin e de afiliado exigem token.
- Endereços de carteira e chaves Pix são revalidados no momento da aprovação do repasse.