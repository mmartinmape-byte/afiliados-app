# ── Afiliados Cleantech ───────────────────────────────────────────────────────
# App de afiliados para la Tienda Nube de Cleantech (somoscleantech.com.ar).
# Cada influencer tiene un cupón y un link con tracking; cuando un pedido se
# paga, el webhook de Tienda Nube acredita la comisión automáticamente.

from flask import Flask, render_template, request, jsonify, redirect
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
import os, re, uuid, hmac, hashlib, json
import requests as req_lib
from urllib.parse import urlparse, parse_qs

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
ADMIN_KEY        = os.environ.get('ADMIN_KEY', 'admin123')
TN_CLIENT_ID     = os.environ.get('TN_CLIENT_ID', '')
TN_CLIENT_SECRET = os.environ.get('TN_CLIENT_SECRET', '')
APP_URL          = os.environ.get('APP_URL', '').rstrip('/')  # ej: https://afiliados-cleantech.up.railway.app
TIENDA_URL       = os.environ.get('TIENDA_URL', 'https://somoscleantech.com.ar').rstrip('/')
COMISION_DEFAULT = float(os.environ.get('COMISION_DEFAULT', '10'))
# Ojo: paréntesis/espacios en el User-Agent disparan el WAF de Cloudflare de TN
TN_UA            = 'CleantechAfiliados/1.0'

DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
IS_PG = DATABASE_URL.startswith('postgresql')
engine = create_engine(DATABASE_URL if IS_PG else 'sqlite:///afiliados.db',
                       pool_pre_ping=True)

AUTOINC = 'SERIAL PRIMARY KEY' if IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'

with engine.begin() as conn:
    conn.execute(text(f'''
        CREATE TABLE IF NOT EXISTS influencers (
            id            {AUTOINC},
            nombre        TEXT NOT NULL,
            slug          TEXT NOT NULL UNIQUE,
            instagram     TEXT NOT NULL DEFAULT '',
            email         TEXT NOT NULL DEFAULT '',
            alias_mp      TEXT NOT NULL DEFAULT '',
            comision_pct  REAL NOT NULL DEFAULT 10,
            descuento_pct REAL NOT NULL DEFAULT 0,
            cupon_codigo  TEXT NOT NULL DEFAULT '',
            token         TEXT NOT NULL UNIQUE,
            activo        INTEGER NOT NULL DEFAULT 1,
            creado        TEXT NOT NULL
        )'''))
    conn.execute(text(f'''
        CREATE TABLE IF NOT EXISTS ventas (
            id             {AUTOINC},
            order_id       TEXT NOT NULL UNIQUE,
            numero         TEXT NOT NULL DEFAULT '',
            fecha          TEXT NOT NULL,
            cliente        TEXT NOT NULL DEFAULT '',
            total          REAL NOT NULL DEFAULT 0,
            influencer_id  INTEGER NOT NULL,
            atribucion     TEXT NOT NULL DEFAULT 'cupon',
            comision_pct   REAL NOT NULL DEFAULT 10,
            comision       REAL NOT NULL DEFAULT 0,
            estado         TEXT NOT NULL DEFAULT 'pendiente',
            liquidacion_id INTEGER,
            creado         TEXT NOT NULL
        )'''))
    conn.execute(text(f'''
        CREATE TABLE IF NOT EXISTS liquidaciones (
            id            {AUTOINC},
            influencer_id INTEGER NOT NULL,
            monto         REAL NOT NULL,
            fecha         TEXT NOT NULL,
            notas         TEXT NOT NULL DEFAULT ''
        )'''))
    conn.execute(text('''
        CREATE TABLE IF NOT EXISTS tn_config (
            key   TEXT PRIMARY KEY,
            value TEXT
        )'''))


@app.errorhandler(Exception)
def _error_detalle(e):
    import traceback
    print(f'ERROR: {traceback.format_exc()}')
    return jsonify({'error': str(e)}), 500


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def _row(r):
    return dict(r._mapping)

def _es_admin():
    clave = request.args.get('clave') or request.headers.get('X-Admin-Key') or ''
    return clave == ADMIN_KEY


# ── Tienda Nube API ───────────────────────────────────────────────────────────

def tn_config():
    with engine.connect() as conn:
        rows = conn.execute(text('SELECT key, value FROM tn_config')).fetchall()
    return {r[0]: r[1] for r in rows}

def tn_guardar(key, value):
    with engine.begin() as conn:
        if IS_PG:
            conn.execute(text(
                'INSERT INTO tn_config (key, value) VALUES (:k, :v) '
                'ON CONFLICT (key) DO UPDATE SET value=:v'), {'k': key, 'v': value})
        else:
            conn.execute(text(
                'INSERT OR REPLACE INTO tn_config (key, value) VALUES (:k, :v)'),
                {'k': key, 'v': value})

def tn_headers():
    cfg = tn_config()
    return {
        'Authentication': f"bearer {cfg.get('access_token', '')}",
        'User-Agent': TN_UA,
        'Content-Type': 'application/json',
    }

def tn_base():
    cfg = tn_config()
    return f"https://api.tiendanube.com/v1/{cfg.get('store_id', '')}"

def tn_conectada():
    cfg = tn_config()
    return bool(cfg.get('access_token') and cfg.get('store_id'))


@app.route('/tn/conectar')
def tn_conectar():
    if not _es_admin():
        return 'No autorizado', 401
    if not TN_CLIENT_ID:
        return 'Falta configurar TN_CLIENT_ID en las variables de entorno.', 500
    return redirect(f'https://www.tiendanube.com/apps/{TN_CLIENT_ID}/authorize')


@app.route('/tn/callback')
def tn_callback():
    code = request.args.get('code')
    if not code:
        return 'Error: Tienda Nube no devolvió el código de autorización.', 400
    r = req_lib.post('https://www.tiendanube.com/apps/authorize/token', json={
        'client_id': TN_CLIENT_ID,
        'client_secret': TN_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
    }, headers={'User-Agent': TN_UA})
    data = r.json()
    if 'access_token' not in data:
        return f'Error al obtener token: {data}', 500
    tn_guardar('access_token', data['access_token'])
    tn_guardar('store_id', str(data.get('user_id', '')))
    tn_guardar('scope', str(data.get('scope', '')))
    aviso = _registrar_webhook()
    return redirect(f'/admin?clave={ADMIN_KEY}&conectada=1&webhook={aviso}')


def _registrar_webhook():
    """Registra (una vez) el webhook order/paid apuntando a esta app."""
    url_base = APP_URL or request.url_root.rstrip('/')
    destino = f'{url_base}/webhooks/tn'
    try:
        r = req_lib.get(f'{tn_base()}/webhooks', headers=tn_headers())
        existentes = r.json() if r.status_code == 200 else []
        for w in existentes:
            if w.get('event') == 'order/paid' and w.get('url') == destino:
                return 'ya-existia'
        r = req_lib.post(f'{tn_base()}/webhooks', headers=tn_headers(),
                         json={'event': 'order/paid', 'url': destino})
        return 'creado' if r.status_code in (200, 201) else f'error-{r.status_code}'
    except Exception as ex:
        return f'error-{ex}'


# ── Atribución y registro de ventas ──────────────────────────────────────────

def _atribuir_orden(order):
    """Devuelve (influencer_row, 'cupon'|'link') o (None, None)."""
    with engine.connect() as conn:
        infs = conn.execute(text(
            'SELECT * FROM influencers WHERE activo=1')).fetchall()
    if not infs:
        return None, None

    # 1) Por cupón usado en el pedido (TN lo devuelve como objeto único,
    #    pero por las dudas soportamos también lista)
    cup = order.get('coupon')
    if isinstance(cup, dict):
        cup = [cup]
    cupones = [(c.get('code') or '').strip().upper()
               for c in (cup or []) if isinstance(c, dict)]
    for inf in infs:
        cod = (inf._mapping['cupon_codigo'] or '').strip().upper()
        if cod and cod in cupones:
            return inf, 'cupon'

    # 2) Por link de entrada (utm_source=slug o ?ref=slug)
    landing = order.get('landing_url') or order.get('landing_site') or ''
    try:
        qs = parse_qs(urlparse(landing).query)
        marcas = [v.strip().lower() for k in ('utm_source', 'ref')
                  for v in qs.get(k, [])]
    except Exception:
        marcas = []
    for inf in infs:
        if inf._mapping['slug'].lower() in marcas:
            return inf, 'link'

    return None, None


def _registrar_venta(order):
    """Guarda la venta con su comisión si corresponde a un influencer."""
    order_id = str(order.get('id', ''))
    if not order_id:
        return None
    with engine.connect() as conn:
        ya = conn.execute(text('SELECT 1 FROM ventas WHERE order_id=:oid'),
                          {'oid': order_id}).fetchone()
    if ya:
        return 'duplicada'

    inf, via = _atribuir_orden(order)
    if not inf:
        return 'sin-influencer'

    m = inf._mapping
    total = float(order.get('total') or 0)
    pct = float(m['comision_pct'] or 0)
    comision = round(total * pct / 100, 2)
    cliente = (order.get('contact_name') or order.get('customer', {}).get('name')
               if isinstance(order.get('customer'), dict) else order.get('contact_name')) or ''
    with engine.begin() as conn:
        conn.execute(text('''
            INSERT INTO ventas (order_id, numero, fecha, cliente, total,
                                influencer_id, atribucion, comision_pct,
                                comision, estado, creado)
            VALUES (:oid, :num, :f, :cli, :tot, :inf, :via, :pct, :com,
                    'pendiente', :creado)
        '''), {'oid': order_id, 'num': str(order.get('number') or ''),
               'f': (order.get('paid_at') or order.get('created_at') or _now())[:19].replace('T', ' '),
               'cli': cliente, 'tot': total, 'inf': m['id'], 'via': via,
               'pct': pct, 'com': comision, 'creado': _now()})
    return 'registrada'


@app.route('/webhooks/tn', methods=['POST'])
def webhook_tn():
    raw = request.get_data()
    # Verificar firma HMAC si tenemos el secret
    firma = request.headers.get('x-linkedstore-hmac-sha256', '')
    if TN_CLIENT_SECRET and firma:
        esperada = hmac.new(TN_CLIENT_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(esperada, firma):
            return jsonify({'error': 'firma inválida'}), 401
    try:
        payload = json.loads(raw.decode() or '{}')
    except Exception:
        return jsonify({'error': 'payload inválido'}), 400
    if payload.get('event') != 'order/paid':
        return jsonify({'ok': True, 'ignorado': payload.get('event')})
    order_id = payload.get('id')
    r = req_lib.get(f'{tn_base()}/orders/{order_id}', headers=tn_headers())
    if r.status_code != 200:
        return jsonify({'error': f'no se pudo leer la orden ({r.status_code})'}), 500
    resultado = _registrar_venta(r.json())
    return jsonify({'ok': True, 'resultado': resultado})


@app.route('/api/sync', methods=['POST'])
def sync_manual():
    """Respaldo por si algún webhook se perdió: repasa pedidos pagos recientes."""
    if not _es_admin():
        return jsonify({'error': 'No autorizado'}), 401
    if not tn_conectada():
        return jsonify({'error': 'Tienda Nube no está conectada'}), 400
    dias = int(request.args.get('dias', 30))
    desde = (datetime.now() - timedelta(days=dias)).strftime('%Y-%m-%dT00:00:00')
    nuevos, page = 0, 1
    while True:
        r = req_lib.get(f'{tn_base()}/orders', headers=tn_headers(), params={
            'payment_status': 'paid', 'created_at_min': desde,
            'per_page': 50, 'page': page})
        if r.status_code != 200:
            break
        ordenes = r.json()
        if not ordenes:
            break
        for o in ordenes:
            if _registrar_venta(o) == 'registrada':
                nuevos += 1
        if len(ordenes) < 50:
            break
        page += 1
    return jsonify({'ok': True, 'nuevas': nuevos})


@app.route('/api/debug/ordenes')
def debug_ordenes():
    """Muestra los pedidos crudos de TN para diagnosticar atribución."""
    if not _es_admin():
        return jsonify({'error': 'No autorizado'}), 401
    dias = int(request.args.get('dias', 2))
    desde = (datetime.now() - timedelta(days=dias)).strftime('%Y-%m-%dT00:00:00')
    r = req_lib.get(f'{tn_base()}/orders', headers=tn_headers(),
                    params={'created_at_min': desde, 'per_page': 20})
    cfg = tn_config()
    out = {'status': r.status_code, 'ordenes': [],
           'store_id': cfg.get('store_id', ''),
           'scopes_del_token': cfg.get('scope', '(desconocido: reconectar para capturarlo)')}
    if r.status_code != 200:
        out['detalle_error'] = r.text[:300]
    # Sondas: probar GET /store con distintos User-Agent para aislar el bloqueo
    out['probes'] = {}
    cfg2 = tn_config()
    uas = {
        'ua_actual': TN_UA,
        'ua_navegador': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
        'ua_simple': 'CleantechAfiliados/1.0',
    }
    for nombre, ua in uas.items():
        try:
            rr = req_lib.get(f'{tn_base()}/store', headers={
                'Authentication': f"bearer {cfg2.get('access_token', '')}",
                'User-Agent': ua,
                'Content-Type': 'application/json',
            }, timeout=20)
            es_html = 'html' in rr.headers.get('content-type', '')
            out['probes'][nombre] = {
                'status': rr.status_code,
                'bloqueado_cf': es_html,
                'body': '' if es_html else rr.text[:150],
            }
        except Exception as ex:
            out['probes'][nombre] = {'error': str(ex)}
    try:
        data = r.json() if r.status_code == 200 else []
        for o in (data if isinstance(data, list) else []):
            out['ordenes'].append({
                'id': o.get('id'), 'number': o.get('number'),
                'payment_status': o.get('payment_status'),
                'status': o.get('status'),
                'total': o.get('total'),
                'coupon': o.get('coupon'),
                'promotional_discount': (o.get('promotional_discount') or {}).get('promotions_applied'),
                'discount_coupon': o.get('discount_coupon'),
                'landing_url': o.get('landing_url') or o.get('landing_site'),
                'created_at': o.get('created_at'), 'paid_at': o.get('paid_at'),
                'contact_name': o.get('contact_name'),
            })
        if not isinstance(data, list):
            out['respuesta'] = data
    except Exception as ex:
        out['error'] = str(ex)
    return jsonify(out)


@app.route('/api/debug/limpiar-tienda', methods=['POST'])
def limpiar_tienda():
    """Borra de la tienda conectada SOLO lo que creó esta app:
    su webhook order/paid y los cupones de prueba PRUEBACUPON*."""
    if not _es_admin():
        return jsonify({'error': 'No autorizado'}), 401
    borrado = {'webhooks': [], 'cupones': [], 'tienda': ''}
    r = req_lib.get(f'{tn_base()}/store', headers=tn_headers())
    if r.status_code == 200:
        borrado['tienda'] = (r.json().get('name') or {}).get('es', '')
    destino = f'{APP_URL}/webhooks/tn'
    r = req_lib.get(f'{tn_base()}/webhooks', headers=tn_headers())
    if r.status_code == 200:
        for w in r.json():
            if w.get('url') == destino:
                rr = req_lib.delete(f"{tn_base()}/webhooks/{w['id']}", headers=tn_headers())
                borrado['webhooks'].append({'id': w['id'], 'status': rr.status_code})
    r = req_lib.get(f'{tn_base()}/coupons', headers=tn_headers())
    if r.status_code == 200:
        for c in r.json():
            if (c.get('code') or '').upper().startswith('PRUEBACUPON'):
                rr = req_lib.delete(f"{tn_base()}/coupons/{c['id']}", headers=tn_headers())
                borrado['cupones'].append({'code': c.get('code'), 'status': rr.status_code})
    return jsonify(borrado)


# ── Influencers ───────────────────────────────────────────────────────────────

def _slug(nombre):
    s = re.sub(r'[^a-z0-9]+', '-', nombre.lower().strip()).strip('-')
    return s or uuid.uuid4().hex[:8]


@app.route('/api/influencers', methods=['POST'])
def crear_influencer():
    if not _es_admin():
        return jsonify({'error': 'No autorizado'}), 401
    d = request.json or {}
    nombre = (d.get('nombre') or '').strip()
    if not nombre:
        return jsonify({'error': 'Falta el nombre'}), 400
    slug = _slug(d.get('slug') or nombre)
    try:
        comision = float(d.get('comision_pct', COMISION_DEFAULT))
        descuento = float(d.get('descuento_pct', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Comisión o descuento inválido'}), 400

    with engine.connect() as conn:
        if conn.execute(text('SELECT 1 FROM influencers WHERE slug=:s'), {'s': slug}).fetchone():
            return jsonify({'error': f'Ya existe un influencer con el código "{slug}"'}), 400

    # Cupón en Tienda Nube (solo si el comprador recibe descuento)
    cupon = ''
    aviso_cupon = ''
    if descuento > 0:
        cupon = f"{re.sub(r'[^A-Z0-9]', '', slug.upper())[:12]}{int(descuento)}"
        if tn_conectada():
            r = req_lib.post(f'{tn_base()}/coupons', headers=tn_headers(), json={
                'code': cupon, 'type': 'percentage',
                'value': str(descuento), 'valid': True})
            if r.status_code not in (200, 201):
                aviso_cupon = f'No se pudo crear el cupón en Tienda Nube ({r.status_code}): {r.text[:200]}'
        else:
            aviso_cupon = 'Tienda Nube no está conectada: el cupón se definió pero hay que crearlo a mano.'

    token = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text('''
            INSERT INTO influencers (nombre, slug, instagram, email, alias_mp,
                                     comision_pct, descuento_pct, cupon_codigo,
                                     token, activo, creado)
            VALUES (:n, :s, :ig, :em, :mp, :c, :d, :cup, :tok, 1, :creado)
        '''), {'n': nombre, 's': slug, 'ig': (d.get('instagram') or '').strip(),
               'em': (d.get('email') or '').strip(), 'mp': (d.get('alias_mp') or '').strip(),
               'c': comision, 'd': descuento, 'cup': cupon, 'tok': token,
               'creado': _now()})
    return jsonify({'ok': True, 'slug': slug, 'cupon': cupon,
                    'aviso': aviso_cupon,
                    'link': f'{TIENDA_URL}/?utm_source={slug}&utm_medium=afiliado'})


@app.route('/api/influencers')
def listar_influencers():
    if not _es_admin():
        return jsonify({'error': 'No autorizado'}), 401
    with engine.connect() as conn:
        rows = conn.execute(text('''
            SELECT i.*,
                   COALESCE(SUM(CASE WHEN v.estado='pendiente' THEN v.comision END), 0) AS comision_pendiente,
                   COALESCE(SUM(v.comision), 0) AS comision_total,
                   COUNT(v.id) AS ventas_count
            FROM influencers i
            LEFT JOIN ventas v ON v.influencer_id = i.id
            GROUP BY i.id
            ORDER BY i.nombre
        ''')).fetchall()
    out = []
    for r in rows:
        d = _row(r)
        d['link'] = f"{TIENDA_URL}/?utm_source={d['slug']}&utm_medium=afiliado"
        d['panel'] = f"/i/{d['token']}"
        out.append(d)
    return jsonify(out)


@app.route('/api/influencers/<int:iid>', methods=['PATCH'])
def editar_influencer(iid):
    if not _es_admin():
        return jsonify({'error': 'No autorizado'}), 401
    d = request.json or {}
    campos = {}
    for c in ('nombre', 'instagram', 'email', 'alias_mp'):
        if c in d:
            campos[c] = (d[c] or '').strip()
    for c in ('comision_pct', 'descuento_pct'):
        if c in d:
            try:
                campos[c] = float(d[c])
            except (TypeError, ValueError):
                return jsonify({'error': f'{c} inválido'}), 400
    if 'activo' in d:
        campos['activo'] = 1 if d['activo'] else 0
    if not campos:
        return jsonify({'error': 'Nada para actualizar'}), 400
    sets = ', '.join(f'{k}=:{k}' for k in campos)
    campos['id'] = iid
    with engine.begin() as conn:
        conn.execute(text(f'UPDATE influencers SET {sets} WHERE id=:id'), campos)
    return jsonify({'ok': True})


@app.route('/api/influencers/<int:iid>/liquidar', methods=['POST'])
def liquidar(iid):
    if not _es_admin():
        return jsonify({'error': 'No autorizado'}), 401
    notas = (request.json or {}).get('notas', '')
    with engine.begin() as conn:
        row = conn.execute(text('''
            SELECT COALESCE(SUM(comision), 0) FROM ventas
            WHERE influencer_id=:i AND estado='pendiente'
        '''), {'i': iid}).fetchone()
        monto = float(row[0] or 0)
        if monto <= 0:
            return jsonify({'error': 'No hay comisiones pendientes'}), 400
        res = conn.execute(text(
            'INSERT INTO liquidaciones (influencer_id, monto, fecha, notas) '
            'VALUES (:i, :m, :f, :n)' + (' RETURNING id' if IS_PG else '')),
            {'i': iid, 'm': monto, 'f': _now(), 'n': notas})
        lid = res.fetchone()[0] if IS_PG else res.lastrowid
        conn.execute(text('''
            UPDATE ventas SET estado='liquidada', liquidacion_id=:l
            WHERE influencer_id=:i AND estado='pendiente'
        '''), {'l': lid, 'i': iid})
    return jsonify({'ok': True, 'monto': monto})


@app.route('/api/ventas')
def listar_ventas():
    if not _es_admin():
        return jsonify({'error': 'No autorizado'}), 401
    with engine.connect() as conn:
        rows = conn.execute(text('''
            SELECT v.*, i.nombre AS influencer, i.slug
            FROM ventas v JOIN influencers i ON i.id = v.influencer_id
            ORDER BY v.fecha DESC LIMIT 300
        ''')).fetchall()
    return jsonify([_row(r) for r in rows])


# ── Ventas de prueba (para testear el circuito sin Tienda Nube) ──────────────

@app.route('/api/test/venta', methods=['POST'])
def crear_venta_prueba():
    if not _es_admin():
        return jsonify({'error': 'No autorizado'}), 401
    d = request.json or {}
    iid = d.get('influencer_id')
    try:
        total = float(d.get('total', 50000))
    except (TypeError, ValueError):
        return jsonify({'error': 'Total inválido'}), 400
    with engine.connect() as conn:
        inf = conn.execute(text('SELECT * FROM influencers WHERE id=:i'), {'i': iid}).fetchone()
    if not inf:
        return jsonify({'error': 'Influencer inexistente'}), 400
    m = inf._mapping
    pct = float(m['comision_pct'] or 0)
    oid = f'TEST-{uuid.uuid4().hex[:8]}'
    with engine.begin() as conn:
        conn.execute(text('''
            INSERT INTO ventas (order_id, numero, fecha, cliente, total,
                                influencer_id, atribucion, comision_pct,
                                comision, estado, creado)
            VALUES (:oid, :num, :f, 'Cliente de prueba', :tot, :inf, 'cupon',
                    :pct, :com, 'pendiente', :creado)
        '''), {'oid': oid, 'num': oid, 'f': _now(), 'tot': total,
               'inf': m['id'], 'pct': pct,
               'com': round(total * pct / 100, 2), 'creado': _now()})
    return jsonify({'ok': True, 'order_id': oid})


@app.route('/api/test/ventas', methods=['DELETE'])
def borrar_ventas_prueba():
    if not _es_admin():
        return jsonify({'error': 'No autorizado'}), 401
    with engine.begin() as conn:
        n = conn.execute(text("DELETE FROM ventas WHERE order_id LIKE 'TEST-%'")).rowcount
    return jsonify({'ok': True, 'borradas': n})


# ── Vistas ────────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return render_template('index.html', tienda=TIENDA_URL)


@app.route('/admin')
def admin():
    if not _es_admin():
        return 'No autorizado. Agregá ?clave=... a la URL.', 401
    return render_template('admin.html', clave=ADMIN_KEY,
                           tn_conectada=tn_conectada(), tienda=TIENDA_URL,
                           comision_default=COMISION_DEFAULT)


@app.route('/i/<token>')
def panel_influencer(token):
    with engine.connect() as conn:
        inf = conn.execute(text('SELECT * FROM influencers WHERE token=:t'),
                           {'t': token}).fetchone()
        if not inf:
            return 'Link inválido.', 404
        ventas = conn.execute(text('''
            SELECT fecha, numero, total, comision, estado FROM ventas
            WHERE influencer_id=:i ORDER BY fecha DESC LIMIT 200
        '''), {'i': inf._mapping['id']}).fetchall()
        liqs = conn.execute(text('''
            SELECT fecha, monto, notas FROM liquidaciones
            WHERE influencer_id=:i ORDER BY fecha DESC
        '''), {'i': inf._mapping['id']}).fetchall()
    m = inf._mapping
    pendiente = sum(v._mapping['comision'] for v in ventas
                    if v._mapping['estado'] == 'pendiente')
    return render_template('influencer.html',
                           inf=dict(m), ventas=[_row(v) for v in ventas],
                           liquidaciones=[_row(l) for l in liqs],
                           pendiente=pendiente, tienda=TIENDA_URL,
                           link=f"{TIENDA_URL}/?utm_source={m['slug']}&utm_medium=afiliado")


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5003))
    print(f'\n  Afiliados Cleantech corriendo en: http://localhost:{port}\n')
    app.run(debug=False, host='0.0.0.0', port=port)
