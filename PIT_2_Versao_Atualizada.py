


import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


VERSION = "2"
DEFAULT_PORT = 8001
DATABASE = Path(__file__).with_name("pit_v2.sqlite3")
PRODUCTS = (
    (1, "Chocolate intenso", "Chocolate", "Cacau, ganache e raspas de chocolate.", 1400, "chocolate"),
    (2, "Baunilha e morango", "Frutados", "Baunilha com cobertura de morango.", 1300, "morango"),
    (3, "Red velvet", "Especiais", "Massa de cacau e cobertura de cream cheese.", 1600, "velvet"),
    (4, "Limão siciliano", "Frutados", "Massa cítrica, creme e raspas de limão.", 1400, "limao"),
    (5, "Caramelo salgado", "Especiais", "Caramelo cremoso com um toque de flor de sal.", 1700, "caramelo"),
    (6, "Duplo chocolate", "Chocolate", "Massa de chocolate e cobertura de cacau.", 1600, "duplo"),
)
PAYMENT_METHODS = ("Pix simulado", "Cartão simulado")


# MODELO — catálogo, regras do pedido e persistência em SQLite
def connect():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database():
    with closing(connect()) as db, db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                price INTEGER NOT NULL CHECK (price > 0),
                decoration TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                customer TEXT NOT NULL,
                address TEXT NOT NULL,
                cep TEXT NOT NULL,
                payment TEXT NOT NULL,
                subtotal INTEGER NOT NULL CHECK (subtotal > 0),
                shipping INTEGER NOT NULL CHECK (shipping >= 0),
                total INTEGER NOT NULL CHECK (total = subtotal + shipping),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS order_items (
                order_id INTEGER NOT NULL REFERENCES orders(id),
                product_id INTEGER NOT NULL REFERENCES products(id),
                quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 20),
                unit_price INTEGER NOT NULL CHECK (unit_price > 0),
                PRIMARY KEY (order_id, product_id)
            );
        """)
        db.executemany("INSERT OR IGNORE INTO products VALUES (?, ?, ?, ?, ?, ?)", PRODUCTS)


def get_catalog():
    with closing(connect()) as db:
        return [dict(row) for row in db.execute("SELECT * FROM products ORDER BY id")]


def required_text(data, field, label, minimum, maximum):
    value = data.get(field)
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ValueError(f"Informe {label} com {minimum} a {maximum} caracteres.")
    return value.strip()


def normalize_cep(value):
    if not re.fullmatch(r"[0-9]{5}-?[0-9]{3}", value):
        raise ValueError("Informe um CEP com 8 dígitos, como 01310-100.")
    return value.replace("-", "")


def shipping_cost(cep, subtotal):
    """Área e tarifas fictícias, definidas para a demonstração do projeto."""
    prefix = int(cep[:5])
    if 1000 <= prefix <= 1999:
        tariff = 600
    elif 2000 <= prefix <= 5999:
        tariff = 1000
    else:
        raise ValueError("Este CEP está fora da área de entrega da loja demonstrativa.")
    return 0 if subtotal >= 6000 else tariff


def quote_shipping(data):
    cep = normalize_cep(required_text(data, "cep", "um CEP", 8, 9))
    with closing(connect()) as db:
        lines = build_order_items(db, data.get("items"))
    subtotal = sum(quantity * price for _, quantity, price in lines)
    shipping = shipping_cost(cep, subtotal)
    return {"subtotal": subtotal, "shipping": shipping, "total": subtotal + shipping}


def build_order_items(db, items):
    if not isinstance(items, list) or not 1 <= len(items) <= len(PRODUCTS):
        raise ValueError("Adicione pelo menos um produto ao carrinho.")
    lines = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Há um item inválido no carrinho.")
        product_id = item.get("product_id")
        quantity = item.get("quantity")
        if type(product_id) is not int or not 1 <= product_id <= len(PRODUCTS):
            raise ValueError("Produto não encontrado no catálogo.")
        if type(quantity) is not int or not 1 <= quantity <= 20:
            raise ValueError("A quantidade por sabor deve ficar entre 1 e 20.")
        if product_id in seen:
            raise ValueError("Há um sabor repetido no carrinho.")
        seen.add(product_id)
        product = db.execute("SELECT price FROM products WHERE id = ?", (product_id,)).fetchone()
        if product is None:
            raise ValueError("Produto não encontrado no catálogo.")
        lines.append((product_id, quantity, product["price"]))
    return lines


def create_order(data):
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("Identificador da solicitação inválido.")
    request_hash = sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    customer = required_text(data, "customer", "um nome", 2, 80)
    address = required_text(data, "address", "um endereço com número", 5, 160)
    cep = normalize_cep(required_text(data, "cep", "um CEP", 8, 9))
    payment = data.get("payment")
    if payment not in PAYMENT_METHODS:
        raise ValueError("Selecione uma forma de pagamento de demonstração.")

    with closing(connect()) as db, db:
        # A transação impede que duas tentativas simultâneas gravem o mesmo pedido.
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            """SELECT id AS order_id, request_hash, subtotal, shipping, total
               FROM orders WHERE request_id = ?""",
            (request_id,),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise ValueError("Esta solicitação já foi usada com outros dados.")
            return {key: existing[key] for key in ("order_id", "subtotal", "shipping", "total")}

        lines = build_order_items(db, data.get("items"))
        subtotal = sum(quantity * price for _, quantity, price in lines)
        shipping = shipping_cost(cep, subtotal)
        total = subtotal + shipping
        order = db.execute(
            """INSERT INTO orders
               (request_id, request_hash, customer, address, cep, payment, subtotal, shipping, total)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (request_id, request_hash, customer, address, cep, payment, subtotal, shipping, total),
        )
        order_id = order.lastrowid
        db.executemany(
            "INSERT INTO order_items VALUES (?, ?, ?, ?)",
            [(order_id, product_id, quantity, price) for product_id, quantity, price in lines],
        )
    return {"order_id": order_id, "subtotal": subtotal, "shipping": shipping, "total": total}


# CONTROLADOR — rotas usadas pela interface
class StoreHandler(BaseHTTPRequestHandler):
    def respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def respond_json(self, status, data):
        self.respond(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            self.respond(200, PAGE.replace("{{VERSION}}", VERSION).encode(), "text/html; charset=utf-8")
        elif path == "/api/catalogo":
            self.respond_json(200, get_catalog())
        else:
            self.respond_json(404, {"error": "Página não encontrada."})

    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Envie os dados no formato JSON.")
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 8192:
                raise ValueError
            data = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError("Dados da solicitação inválidos.") from exc
        if not isinstance(data, dict):
            raise ValueError("Dados da solicitação inválidos.")
        return data

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/api/pedidos", "/api/frete"):
            self.respond_json(404, {"error": "Página não encontrada."})
            return
        try:
            data = self.read_json()
            if path == "/api/frete":
                self.respond_json(200, quote_shipping(data))
            else:
                self.respond_json(201, create_order(data))
        except ValueError as exc:
            self.respond_json(400, {"error": str(exc)})
        except sqlite3.Error:
            self.log_error("Não foi possível acessar o banco de dados.")
            self.respond_json(503, {"error": "Serviço temporariamente indisponível. Tente novamente."})


# VISÃO — interface responsiva e ações do navegador
PAGE = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Açúcar &amp; Afeto | Loja de cupcakes</title>
<style>
:root { color-scheme: light; --ink: #143447; --muted: #405e6c; --paper: #f2f9fb;
        --accent: #09665d; --line: #c9dfe4; --white: #fcfefe; }
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.5 Arial, Helvetica, sans-serif; }
h1, h2, h3, .brand { font-weight: 700; margin: 0; line-height: 1.12; }
button, input, select { font: inherit; }
button { cursor: pointer; }
button:disabled { opacity: .58; cursor: default; }
:focus-visible { outline: 3px solid #1f6594; outline-offset: 3px; }
a { color: inherit; }
header { border-bottom: 1px solid var(--line); }
.bar, main, footer { width: min(1120px, calc(100% - 48px)); margin: auto; }
.bar { height: 70px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.brand { font-size: 23px; text-decoration: none; }
.cart-link { font-weight: 700; color: var(--accent); text-decoration: none; }
.hero { margin: 30px 0 40px; min-height: 230px; display: grid; grid-template-columns: 1.45fr 1fr;
        overflow: hidden; border-radius: 24px; background: #d9edf5; }
.hero-copy { padding: 38px 38px 36px; }
.hero h1 { font-size: clamp(38px, 5.7vw, 70px); letter-spacing: -.035em; }
.hero p { margin: 14px 0 0; max-width: 33ch; color: #315466; }
.hero-art { display: grid; place-items: center; background: radial-gradient(ellipse at 50% 45%, #edfcf7, #9bd4cd); }
.hero-art .cupcake { transform: scale(1.55) rotate(-12deg); }
.layout { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 32px; align-items: start; }
h2 { font-size: 32px; margin-bottom: 16px; }
.filters { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, .75fr); gap: 10px; margin-bottom: 14px; }
.products { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.product, .basket { border: 1px solid var(--line); border-radius: 18px; background: var(--white); }
.product { overflow: hidden; display: flex; flex-direction: column; }
.product-art { display: grid; place-items: center; height: 112px; background: var(--art, #e0f2ee); }
.cupcake { position: relative; width: 74px; height: 74px; }
.cupcake::before { content: ""; position: absolute; inset: 36px 9px 0; border-radius: 2px 2px 12px 12px;
                   clip-path: polygon(0 0, 100% 0, 81% 100%, 19% 100%);
                   background: repeating-linear-gradient(100deg,#315a74 0 5px,#43738a 5px 9px); }
.cupcake::after { content: ""; position: absolute; width: 75px; height: 43px; top: 5px; left: 0;
                  border-radius: 62% 60% 29% 28% / 82% 85% 24% 25%;
                  background: radial-gradient(circle at 30% 30%, #fff8 0 2px, transparent 3px),
                  repeating-linear-gradient(175deg, var(--icing, #58a99b) 0 10px, var(--shadow, #368d80) 11px 14px); }
.chocolate { --art: #e0edf7; --icing: #5a87ad; --shadow: #3e6b92; }
.morango { --art: #ddf4f1; --icing: #78cabd; --shadow: #4ba99b; }
.velvet { --art: #e3edf8; --icing: #b7d6f2; --shadow: #8ab5db; }
.limao { --art: #e6f2e9; --icing: #9bcea7; --shadow: #6cb385; }
.caramelo { --art: #e3f3f7; --icing: #85c4db; --shadow: #54a6c3; }
.duplo { --art: #e2edee; --icing: #48888e; --shadow: #2f6b72; }
.product-body { display: flex; flex: 1; flex-direction: column; padding: 16px; }
.product h3 { font-size: 22px; }
.product p { font-size: 14px; color: var(--muted); margin: 6px 0 14px; }
.product-action { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: auto; }
button { border: 1px solid var(--accent); border-radius: 9px; padding: 7px 11px; background: var(--white); color: var(--accent); font-weight: 700; }
button:hover:not(:disabled) { background: #e4f4f0; }
.basket { padding: 21px; scroll-margin-top: 20px; }
.basket h2 { font-size: 28px; }
.basket h3 { font-size: 22px; margin: 18px 0 11px; }
.cart-list { margin: 0; padding: 0; list-style: none; }
.cart-row { padding: 10px 0; display: grid; gap: 7px; border-bottom: 1px solid var(--line); }
.item-top, .quantity, .totals div { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.quantity { justify-content: flex-start; }
.quantity button { min-width: 37px; min-height: 34px; padding: 3px 9px; }
.totals { margin: 14px 0; display: grid; gap: 4px; color: var(--muted); }
.totals dd { margin: 0; }
.totals .grand { border-top: 1px solid var(--line); margin-top: 5px; padding-top: 9px; font-weight: 800; color: var(--ink); }
form { display: grid; gap: 9px; }
form h3 { margin-bottom: 0 !important; }
label { display: grid; gap: 3px; font-size: 14px; font-weight: 650; }
input, select { width: 100%; min-width: 0; border: 1px solid #7495a2; border-radius: 8px; padding: 8px 9px; background: #fff; color: var(--ink); }
.primary, .primary:hover:not(:disabled) { background: var(--accent); color: #fff; padding: 10px; }
.primary:hover:not(:disabled) { background: #074e4a; }
.note { color: var(--muted); font-size: 14px; margin: 0; }
.status { color: var(--accent); font-size: 14px; font-weight: 700; }
.status:empty { display: none; }
.receipt { margin: 14px 0 0; padding: 11px 12px; border: 1px solid #8bbdad; background: #e6f6ee;
           border-radius: 9px; color: #204f43; font-weight: 650; }
footer { border-top: 1px solid var(--line); margin-top: 40px; padding: 18px 0 25px; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
@media (max-width: 820px) { .layout { grid-template-columns: minmax(0, 1fr) 290px; gap: 16px; } }
@media (max-width: 660px) {
  .bar, main, footer { width: calc(100% - 28px); }
  .hero { grid-template-columns: 1fr .48fr; min-height: 175px; margin: 18px 0 25px; }
  .hero-copy { padding: 22px 15px; }
  .hero-art .cupcake { transform: scale(.85) rotate(-12deg); }
  .layout { grid-template-columns: 1fr; }
  .product-body { padding: 12px; }
  .product h3 { font-size: 19px; }
}
@media (max-width: 335px) { .products { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <div class="bar">
    <a class="brand" href="/">Açúcar &amp; Afeto</a>
    <a class="cart-link" href="#carrinho">Carrinho (<span id="cart-count">0</span>)</a>
  </div>
</header>
<main>
  <section class="hero">
    <div class="hero-copy">
      <h1>Cupcakes gourmet</h1>
      <p>Escolha seus sabores e monte seu pedido.</p>
    </div>
    <div class="hero-art" aria-hidden="true"><span class="cupcake"></span></div>
  </section>
  <div class="layout">
    <section aria-labelledby="catalog-title">
      <h2 id="catalog-title">Cardápio</h2>
      <div class="filters" aria-label="Filtros do cardápio">
        <label>Buscar sabor <input type="search" id="search" placeholder="Ex.: chocolate" maxlength="50"></label>
        <label>Categoria <select id="category"><option value="">Todas</option></select></label>
      </div>
      <p class="status" id="catalog-status" role="status">Carregando cardápio…</p>
      <div class="products" id="products"></div>
    </section>
    <aside class="basket" id="carrinho" aria-labelledby="cart-title">
      <h2 id="cart-title" tabindex="-1">Seu carrinho</h2>
      <p class="note" id="empty-cart">Seu carrinho está vazio.</p>
      <ul class="cart-list" id="cart-items"></ul>
      <dl class="totals">
        <div><dt>Subtotal</dt><dd id="subtotal">R$ 0,00</dd></div>
        <div><dt>Frete</dt><dd id="shipping">—</dd></div>
        <div class="grand"><dt>Total</dt><dd id="total">R$ 0,00</dd></div>
      </dl>
      <form id="checkout">
        <h3>Entrega e pagamento</h3>
        <label>Nome <input name="customer" placeholder="Cliente Teste" minlength="2" maxlength="80" required></label>
        <label>Endereço com número <input name="address" placeholder="Rua Exemplo, 100" minlength="5" maxlength="160" required></label>
        <label>CEP <input name="cep" placeholder="01310-100" inputmode="numeric" pattern="[0-9]{5}-?[0-9]{3}" maxlength="9" aria-describedby="shipping-status" required></label>
        <p class="note" id="shipping-status" role="status">Informe o CEP para calcular o frete.</p>
        <button type="button" id="retry-shipping" hidden>Recalcular frete</button>
        <label>Pagamento
          <select name="payment"><option>Pix simulado</option><option>Cartão simulado</option></select>
        </label>
        <p class="note">Demonstração acadêmica. Use dados fictícios. Nenhuma cobrança é realizada.</p>
        <button class="primary" id="submit-order" type="submit" disabled>Confirmar pedido</button>
        <p class="status" id="checkout-status" role="alert"></p>
      </form>
      <p class="receipt" id="receipt" tabindex="-1" role="status" hidden></p>
      <span class="sr-only" id="cart-status" role="status"></span>
    </aside>
  </div>
</main>
<footer class="note">Açúcar &amp; Afeto · Projeto acadêmico · Versão {{VERSION}}</footer>
<script>
const byId = (id) => document.getElementById(id);
const currency = new Intl.NumberFormat("pt-BR", {style: "currency", currency: "BRL"});
const money = (cents) => currency.format(cents / 100);
const checkout = byId("checkout");
const cart = new Map();
const CART_KEY = "acucar-afeto-carrinho-v2";
let catalog = [];
let sending = false;
let lastSubmission;
let quote = null;
let quoteRevision = 0;
let quoteTimer;

function saveCart() {
  try {
    localStorage.setItem(CART_KEY, JSON.stringify([...cart]));
  } catch {
    // A compra segue normalmente quando o navegador bloqueia o armazenamento.
  }
}

function restoreCart() {
  try {
    const saved = JSON.parse(localStorage.getItem(CART_KEY) || "[]");
    if (!Array.isArray(saved)) return;
    for (const entry of saved) {
      if (!Array.isArray(entry) || entry.length !== 2) continue;
      const [id, quantity] = entry;
      if (catalog.some((product) => product.id === id)
          && Number.isInteger(quantity) && quantity >= 1 && quantity <= 20) {
        cart.set(id, quantity);
      }
    }
  } catch {
    // Valores antigos ou ilegíveis deixam o carrinho vazio.
  }
}

async function requestJson(url, data) {
  const response = await fetch(url, data === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || "Falha na comunicação com o servidor.");
    error.status = response.status;
    throw error;
  }
  return result;
}

function searchable(value) {
  return value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

function renderCatalog() {
  if (!catalog.length) return;
  const search = searchable(byId("search").value.trim());
  const category = byId("category").value;
  const shown = catalog.filter((product) =>
    (!category || product.category === category)
    && searchable(product.name + " " + product.description).includes(search)
  );
  byId("catalog-status").textContent = shown.length ? "" : "Nenhum sabor encontrado.";
  byId("products").innerHTML = shown.map((product) => `
    <article class="product">
      <div class="product-art ${product.decoration}" aria-hidden="true"><span class="cupcake"></span></div>
      <div class="product-body">
        <h3>${product.name}</h3>
        <p>${product.description}</p>
        <div class="product-action">
          <strong>${money(product.price)}</strong>
          <button type="button" data-add="${product.id}" aria-label="Adicionar ${product.name}">Adicionar</button>
        </div>
      </div>
    </article>`).join("");
}

function selectedItems() {
  return catalog.filter((product) => cart.has(product.id)).map((product) => ({
    ...product, quantity: cart.get(product.id)
  }));
}

function orderPayload() {
  return {
    ...Object.fromEntries(new FormData(checkout)),
    items: selectedItems().map((item) => ({product_id: item.id, quantity: item.quantity}))
  };
}

function renderTotals(items) {
  const subtotal = items.reduce((sum, item) => sum + item.quantity * item.price, 0);
  byId("subtotal").textContent = money(subtotal);
  byId("shipping").textContent = quote ? (quote.shipping ? money(quote.shipping) : "Grátis") : "—";
  byId("total").textContent = quote ? money(quote.total) : (subtotal ? "—" : money(0));
  byId("submit-order").disabled = !quote || sending;
}

function scheduleShippingQuote() {
  quote = null;
  clearTimeout(quoteTimer);
  const revision = ++quoteRevision;
  const items = selectedItems();
  const cep = checkout.elements.cep.value.trim();
  const status = byId("shipping-status");
  byId("retry-shipping").hidden = true;
  renderTotals(items);
  if (!items.length || !/^[0-9]{5}-?[0-9]{3}$/.test(cep)) {
    status.textContent = items.length ? "Informe um CEP com 8 dígitos." : "Adicione um sabor para calcular o frete.";
    return;
  }
  status.textContent = "Calculando frete…";
  quoteTimer = setTimeout(async () => {
    try {
      const result = await requestJson("/api/frete", {
        cep,
        items: items.map((item) => ({product_id: item.id, quantity: item.quantity}))
      });
      if (revision !== quoteRevision) return;
      quote = result;
      status.textContent = quote.shipping ? "Frete calculado. Frete grátis a partir de R$ 60,00." : "Seu pedido tem frete grátis.";
    } catch (error) {
      if (revision !== quoteRevision) return;
      status.textContent = error.message;
      byId("retry-shipping").hidden = error.status === 400;
    }
    renderTotals(selectedItems());
  }, 180);
}

function renderCart() {
  const items = selectedItems();
  byId("cart-count").textContent = items.reduce((sum, item) => sum + item.quantity, 0);
  byId("empty-cart").hidden = items.length > 0;
  byId("cart-items").innerHTML = items.map((item) => `
    <li class="cart-row">
      <div class="item-top"><strong>${item.name}</strong><span>${money(item.price * item.quantity)}</span></div>
      <div class="quantity">
        <button type="button" data-id="${item.id}" data-delta="-1" aria-label="Diminuir ${item.name}">−</button>
        <span>${item.quantity}</span>
        <button type="button" data-id="${item.id}" data-delta="1" aria-label="Aumentar ${item.name}">+</button>
      </div>
    </li>`).join("");
  renderTotals(items);
}

function changeQuantity(id, delta) {
  if (sending || !catalog.some((product) => product.id === id)) return;
  const next = (cart.get(id) || 0) + delta;
  if (next > 20) {
    byId("cart-status").textContent = "Limite de 20 unidades por sabor.";
    byId("checkout-status").textContent = "Limite de 20 unidades por sabor.";
    return;
  }
  if (next > 0) cart.set(id, next);
  else cart.delete(id);
  saveCart();
  byId("receipt").hidden = true;
  byId("checkout-status").textContent = "";
  byId("cart-status").textContent = next ? "Quantidade no carrinho: " + next : "Item removido do carrinho.";
  renderCart();
  scheduleShippingQuote();
}

byId("products").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-add]");
  if (button) changeQuantity(Number(button.dataset.add), 1);
});

byId("cart-items").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-delta]");
  if (!button) return;
  const {id, delta} = button.dataset;
  changeQuantity(Number(id), Number(delta));
  const retained = byId("cart-items").querySelector(`button[data-id="${id}"][data-delta="${delta}"]`);
  (retained || document.querySelector(`button[data-add="${id}"]`) || byId("cart-title")).focus();
});

byId("search").addEventListener("input", renderCatalog);
byId("category").addEventListener("change", renderCatalog);
checkout.elements.cep.addEventListener("input", scheduleShippingQuote);
byId("retry-shipping").addEventListener("click", scheduleShippingQuote);

checkout.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (sending || !cart.size || !quote) return;
  sending = true;
  byId("submit-order").disabled = true;
  byId("checkout-status").textContent = "";
  try {
    const payload = orderPayload();
    const signature = JSON.stringify(payload);
    if (lastSubmission?.signature !== signature) {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      const requestId = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
      lastSubmission = {signature, requestId};
    }
    const order = await requestJson("/api/pedidos", {...payload, request_id: lastSubmission.requestId});
    lastSubmission = null;
    cart.clear();
    saveCart();
    checkout.reset();
    scheduleShippingQuote();
    const receipt = byId("receipt");
    receipt.textContent = `Pedido #${order.order_id} registrado. Total: ${money(order.total)}. Pagamento simulado.`;
    receipt.hidden = false;
    receipt.focus();
  } catch (error) {
    byId("checkout-status").textContent = error.message;
  } finally {
    sending = false;
    renderCart();
  }
});

async function start() {
  try {
    catalog = await requestJson("/api/catalogo");
    const categories = [...new Set(catalog.map((product) => product.category))].sort();
    for (const category of categories) byId("category").add(new Option(category, category));
    restoreCart();
    byId("catalog-status").textContent = "";
    renderCatalog();
    renderCart();
    scheduleShippingQuote();
  } catch {
    byId("catalog-status").textContent = "Não foi possível carregar o cardápio. Atualize a página.";
  }
}

start();
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Loja demonstrativa de cupcakes.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    initialize_database()
    with ThreadingHTTPServer(("127.0.0.1", args.port), StoreHandler) as server:
        port = server.server_address[1]
        print(f"Açúcar & Afeto | versão {VERSION} | http://127.0.0.1:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServidor encerrado.")


if __name__ == "__main__":
    main()
