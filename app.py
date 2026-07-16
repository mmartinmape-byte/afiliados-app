# ── ReferidosApp ──────────────────────────────────────────────────────────────
# Programa de afiliados/influencers multi-tienda para Tienda Nube.
# Cada tienda que instala la app obtiene su propio espacio: influencers con
# cupón y link de tracking, atribución automática de ventas al pagarse el
# pedido (webhook order/paid), comisiones y liquidaciones.
#
# Historia: nació como app single-tenant para Cleantech (somoscleantech.com.ar);
# la migración de arranque convierte esos datos en la tienda #1.

from flask import Flask, render_template, request, jsonify, redirect
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
import os, re, uuid, hmac, hashlib, json
import requests as req_lib
from urllib.parse import urlparse, parse_qs

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SUPERADMIN_KEY   = os.environ.get('ADMIN_KEY', 'admin123')  # panel del dueño de la app
TN_CLIENT_ID     = os.environ.get('TN_CLIENT_ID', '')
TN_CLIENT_SECRET = os.environ.get('TN_CLIENT_SECRET', '')
APP_URL          = os.environ.get('APP_URL', '').rstrip('/')
COMISION_DEFAULT = float(os.environ.get('COMISION_DEFAULT', '10'))
# Ojo: paréntesis/espacios en el User-Agent disparan el WAF de Cloudflare de TN
TN_UA            = 'ReferidosApp/1.0'
PLAN_FREE_MAX_INFLUENCERS = 1
# Cobro del plan Pro (Fase 3 MVP): link de suscripción de Mercado Pago.
# Cuando TN habilite su Billing API, esto migra a cobro nativo del App Store.
PRO_PRECIO_TXT   = os.environ.get('PRO_PRECIO_TXT', 'USD 12/mes')
PRO_LINK_PAGO    = os.environ.get('PRO_LINK_PAGO', '')  # link de suscripción MP
SOPORTE_EMAIL    = os.environ.get('SOPORTE_EMAIL', 'mmartinmape@gmail.com')

# URL de la tienda Cleantech para la migración inicial (con www: la redirección
# sin www descarta los parámetros utm y rompe la atribución por link)
CLEANTECH_URL = os.environ.get('TIENDA_URL', 'https://www.somoscleantech.com.ar').rstrip('/')

DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
IS_PG = DATABASE_URL.startswith('postgresql')
engine = create_engine(DATABASE_URL if IS_PG else 'sqlite:///afiliados.db',
                       pool_pre_ping=True)

AUTOINC = 'SERIAL PRIMARY KEY' if IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'


# ── Esquema y migración ───────────────────────────────────────────────────────

def _crear_esquema():
    with engine.begin() as conn:
        conn.execute(text(f'''
            CREATE TABLE IF NOT EXISTS tiendas (
                id           {AUTOINC},
                store_id     TEXT NOT NULL UNIQUE,
                nombre       TEXT NOT NULL DEFAULT '',
                url          TEXT NOT NULL DEFAULT '',
                email        TEXT NOT NULL DEFAULT '',
                access_token TEXT NOT NULL DEFAULT '',
                clave_admin  TEXT NOT NULL UNIQUE,
                plan         TEXT NOT NULL DEFAULT 'free',
                activa       INTEGER NOT NULL DEFAULT 1,
                creado       TEXT NOT NULL
            )'''))
        conn.execute(text(f'''
            CREATE TABLE IF NOT EXISTS influencers (
                id            {AUTOINC},
                tienda_id     INTEGER NOT NULL DEFAULT 0,
                nombre        TEXT NOT NULL,
                slug          TEXT NOT NULL,
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
                tienda_id      INTEGER NOT NULL DEFAULT 0,
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
                tienda_id     INTEGER NOT NULL DEFAULT 0,
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


def _migrar_multitienda():
    """Migración single-tenant → multi-tienda. Idempotente.
    Convierte la conexión original (tn_config) en la tienda #1 y le asigna
    todos los datos existentes. La clave de admin histórica sigue vigente."""
    try:
        # 1) Columnas tienda_id en tablas preexistentes
        if IS_PG:
            with engine.begin() as conn:
                for t in ('influencers', 'ventas', 'liquidaciones'):
                    conn.execute(text(
                        f'ALTER TABLE {t} ADD COLUMN IF NOT EXISTS tienda_id INTEGER NOT NULL DEFAULT 0'))
                # El slug pasa a ser único POR TIENDA (era único global)
                conn.execute(text(
                    'ALTER TABLE influencers DROP CONSTRAINT IF EXISTS influencers_slug_key'))
                conn.execute(text(
                    'CREATE UNIQUE INDEX IF NOT EXISTS idx_influencers_tienda_slug '
                    'ON influencers (tienda_id, slug)'))
        else:
            with engine.connect() as conn:
                for t in ('influencers', 'ventas', 'liquidaciones'):
                    cols = [r[1] for r in conn.execute(text(f'PRAGMA table_info({t})')).fetchall()]
                    if 'tienda_id' not in cols:
                        with engine.begin() as c2:
                            c2.execute(text(
                                f'ALTER TABLE {t} ADD COLUMN tienda_id INTEGER NOT NULL DEFAULT 0'))

        # 2) La conexión original (tn_config) se convierte en la tienda #1
        with engine.begin() as conn:
            cfg = {r[0]: r[1] for r in conn.execute(
                text('SELECT key, value FROM tn_config')).fetchall()}
            hay_tiendas = conn.execute(text('SELECT COUNT(*) FROM tiendas')).fetchone()[0]
            if cfg.get('access_token') and cfg.get('store_id') and not hay_tiendas:
                conn.execute(text('''
                    INSERT INTO tiendas (store_id, nombre, url, access_token,
                                         clave_admin, plan, activa, creado)
                    VALUES (:sid, 'Cleantech', :url, :tok, :clave, 'pro', 1, :cr)
                '''), {'sid': cfg['store_id'], 'url': CLEANTECH_URL,
                       'tok': cfg['access_token'], 'clave': SUPERADMIN_KEY,
                       'cr': _now()})
                print('  Migración: Cleantech dada de alta como tienda #1.')

            # 3) Adoptar datos huérfanos (tienda_id=0) a la tienda #1
            primera = conn.execute(text(
                'SELECT id FROM tiendas ORDER BY id LIMIT 1')).fetchone()
            if primera:
                for t in ('influencers', 'ventas', 'liquidaciones'):
                    n = conn.execute(text(
                        f'UPDATE {t} SET tienda_id=:tid WHERE tienda_id=0'),
                        {'tid': primera[0]}).rowcount
                    if n:
                        print(f'  Migración: {n} filas de {t} asignadas a la tienda #1.')
    except Exception as ex:
        print(f'  Aviso migración multi-tienda: {ex}')


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

_crear_esquema()
_migrar_multitienda()


@app.errorhandler(Exception)
def _error_detalle(e):
    import traceback
    print(f'ERROR: {traceback.format_exc()}')
    return jsonify({'error': str(e)}), 500


def _row(r):
    return dict(r._mapping)


# ── Resolución de tienda y permisos ──────────────────────────────────────────

def _clave_request():
    return (request.args.get('clave') or request.headers.get('X-Admin-Key') or '').strip()

def _es_superadmin():
    return _clave_request() == SUPERADMIN_KEY

def tienda_actual():
    """Resuelve la tienda del comerciante por su clave de admin."""
    clave = _clave_request()
    if not clave:
        return None
    with engine.connect() as conn:
        row = conn.execute(text(
            'SELECT * FROM tiendas WHERE clave_admin=:c AND activa=1'),
            {'c': clave}).fetchone()
    return row

def tienda_por_store_id(store_id):
    with engine.connect() as conn:
        return conn.execute(text(
            'SELECT * FROM tiendas WHERE store_id=:s'),
            {'s': str(store_id)}).fetchone()


# ── Tienda Nube API (por tienda) ──────────────────────────────────────────────

def tn_headers(tienda):
    return {
        'Authentication': f"bearer {tienda._mapping['access_token']}",
        'User-Agent': TN_UA,
        'Content-Type': 'application/json',
    }

def tn_base(tienda):
    return f"https://api.tiendanube.com/v1/{tienda._mapping['store_id']}"

def tn_base_billing(tienda):
    # Los endpoints de Billing existen solo desde la versión 2025-03 de la API
    return f"https://api.tiendanube.com/2025-03/{tienda._mapping['store_id']}"


@app.route('/entrar')
def entrar():
    """Re-ingreso al admin: manda a autorizar en TN (instantáneo si la app ya
    está instalada) y el callback redirige al admin de la tienda logueada.
    Es la URL para poner como "Página de la aplicación" en el Portal de Socios."""
    if not TN_CLIENT_ID:
        return 'App no configurada.', 500
    return redirect(f'https://www.tiendanube.com/apps/{TN_CLIENT_ID}/authorize')


@app.route('/tn/callback')
def tn_callback():
    """Alta/reconexión automática: cualquier tienda que instala la app."""
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
    store_id = str(data.get('user_id', ''))
    token = data['access_token']

    existente = tienda_por_store_id(store_id)
    if existente:
        with engine.begin() as conn:
            conn.execute(text(
                'UPDATE tiendas SET access_token=:t, activa=1 WHERE store_id=:s'),
                {'t': token, 's': store_id})
        tienda = tienda_por_store_id(store_id)
    else:
        with engine.begin() as conn:
            conn.execute(text('''
                INSERT INTO tiendas (store_id, access_token, clave_admin, plan, activa, creado)
                VALUES (:s, :t, :clave, 'free', 1, :cr)
            '''), {'s': store_id, 't': token, 'clave': uuid.uuid4().hex, 'cr': _now()})
        tienda = tienda_por_store_id(store_id)

    tienda = _refrescar_datos_tienda(tienda)
    _registrar_webhook(tienda)
    return redirect(f"/admin?clave={tienda._mapping['clave_admin']}&bienvenida=1")


def _refrescar_datos_tienda(tienda):
    """Completa nombre/URL/email de la tienda desde la API de TN, con varios
    campos de respaldo para el dominio (las tiendas demo no siempre traen
    url_with_brand). Devuelve la fila actualizada."""
    store_id = tienda._mapping['store_id']
    try:
        rs = req_lib.get(f'{tn_base(tienda)}/store', headers=tn_headers(tienda), timeout=20)
        if rs.status_code != 200:
            return tienda
        s = rs.json()
        nombres = s.get('name') or {}
        nombre = (nombres.get('es') or nombres.get('pt') or nombres.get('en') or '').strip()
        url = (s.get('url_with_brand') or '').strip().rstrip('/')
        if not url:
            dominio = s.get('original_domain') or ''
            if not dominio:
                doms = s.get('domains') or []
                dominio = doms[0] if doms and isinstance(doms[0], str) else ''
            if dominio:
                url = f'https://{dominio}'.rstrip('/')
        email = (s.get('email') or '').strip()
        with engine.begin() as conn:
            conn.execute(text(
                'UPDATE tiendas SET nombre=COALESCE(NULLIF(:n, \'\'), nombre), '
                'url=COALESCE(NULLIF(:u, \'\'), url), '
                'email=COALESCE(NULLIF(:e, \'\'), email) WHERE store_id=:s'),
                {'n': nombre, 'u': url, 'e': email, 's': store_id})
        return tienda_por_store_id(store_id)
    except Exception as ex:
        print(f'  Aviso: no se pudo refrescar datos de la tienda {store_id} ({ex})')
        return tienda


def _registrar_webhook(tienda):
    """Registra (una vez) el webhook order/paid de esta tienda."""
    url_base = APP_URL or request.url_root.rstrip('/')
    destino = f'{url_base}/webhooks/tn'
    try:
        r = req_lib.get(f'{tn_base(tienda)}/webhooks', headers=tn_headers(tienda))
        existentes = r.json() if r.status_code == 200 else []
        for w in existentes:
            if w.get('event') == 'order/paid' and w.get('url') == destino:
                return 'ya-existia'
        r = req_lib.post(f'{tn_base(tienda)}/webhooks', headers=tn_headers(tienda),
                         json={'event': 'order/paid', 'url': destino})
        return 'creado' if r.status_code in (200, 201) else f'error-{r.status_code}'
    except Exception as ex:
        return f'error-{ex}'


# ── Atribución y registro de ventas ──────────────────────────────────────────

def _atribuir_orden(tienda_id, order):
    """Devuelve (influencer_row, 'cupon'|'link') o (None, None)."""
    with engine.connect() as conn:
        infs = conn.execute(text(
            'SELECT * FROM influencers WHERE activo=1 AND tienda_id=:t'),
            {'t': tienda_id}).fetchall()
    if not infs:
        return None, None

    cup = order.get('coupon')
    if isinstance(cup, dict):
        cup = [cup]
    cupones = [(c.get('code') or '').strip().upper()
               for c in (cup or []) if isinstance(c, dict)]
    for inf in infs:
        cod = (inf._mapping['cupon_codigo'] or '').strip().upper()
        if cod and cod in cupones:
            return inf, 'cupon'

    marcas = []
    visita = order.get('customer_visit') or {}
    utm = (visita.get('utm_parameters') or {})
    if utm.get('utm_source'):
        marcas.append(str(utm['utm_source']).strip().lower())
    landing = order.get('landing_url') or visita.get('landing_page') or ''
    try:
        qs = parse_qs(urlparse(landing).query)
        marcas += [v.strip().lower() for k in ('utm_source', 'ref')
                   for v in qs.get(k, [])]
    except Exception:
        pass
    for inf in infs:
        if inf._mapping['slug'].lower() in marcas:
            return inf, 'link'

    return None, None


def _registrar_venta(tienda, order):
    """Guarda la venta con su comisión si corresponde a un influencer de la tienda."""
    tienda_id = tienda._mapping['id']
    order_id = str(order.get('id', ''))
    if not order_id:
        return None
    with engine.connect() as conn:
        ya = conn.execute(text('SELECT 1 FROM ventas WHERE order_id=:oid'),
                          {'oid': order_id}).fetchone()
    if ya:
        return 'duplicada'

    inf, via = _atribuir_orden(tienda_id, order)
    if not inf:
        return 'sin-influencer'

    m = inf._mapping
    total = float(order.get('total') or 0)
    pct = float(m['comision_pct'] or 0)
    comision = round(total * pct / 100, 2)
    cliente = (order.get('contact_name') or '')
    with engine.begin() as conn:
        conn.execute(text('''
            INSERT INTO ventas (tienda_id, order_id, numero, fecha, cliente, total,
                                influencer_id, atribucion, comision_pct,
                                comision, estado, creado)
            VALUES (:tid, :oid, :num, :f, :cli, :tot, :inf, :via, :pct, :com,
                    'pendiente', :creado)
        '''), {'tid': tienda_id, 'oid': order_id,
               'num': str(order.get('number') or ''),
               'f': (order.get('paid_at') or order.get('created_at') or _now())[:19].replace('T', ' '),
               'cli': cliente, 'tot': total, 'inf': m['id'], 'via': via,
               'pct': pct, 'com': comision, 'creado': _now()})
    return 'registrada'


@app.route('/webhooks/tn', methods=['POST'])
def webhook_tn():
    raw = request.get_data()
    firma = request.headers.get('x-linkedstore-hmac-sha256', '')
    if TN_CLIENT_SECRET and firma:
        esperada = hmac.new(TN_CLIENT_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(esperada, firma):
            return jsonify({'error': 'firma inválida'}), 401
    try:
        payload = json.loads(raw.decode() or '{}')
    except Exception:
        return jsonify({'error': 'payload inválido'}), 400

    evento = payload.get('event', '')
    # Webhooks de privacidad (LGPD): responder OK; store/redact desactiva la tienda
    if evento in ('store/redact', 'customers/redact', 'customers/data_request'):
        if evento == 'store/redact' and payload.get('store_id'):
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE tiendas SET activa=0, access_token='' WHERE store_id=:s"),
                    {'s': str(payload['store_id'])})
        return jsonify({'ok': True})

    if evento != 'order/paid':
        return jsonify({'ok': True, 'ignorado': evento})

    tienda = tienda_por_store_id(payload.get('store_id', ''))
    if not tienda or not tienda._mapping['activa']:
        return jsonify({'ok': True, 'ignorado': 'tienda-desconocida'})

    order_id = payload.get('id')
    r = req_lib.get(f'{tn_base(tienda)}/orders/{order_id}', headers=tn_headers(tienda))
    if r.status_code != 200:
        return jsonify({'error': f'no se pudo leer la orden ({r.status_code})'}), 500
    resultado = _registrar_venta(tienda, r.json())
    return jsonify({'ok': True, 'resultado': resultado})


@app.route('/api/sync', methods=['POST'])
def sync_manual():
    """Respaldo por si algún webhook se perdió: repasa pedidos pagos recientes."""
    tienda = tienda_actual()
    if not tienda:
        return jsonify({'error': 'No autorizado'}), 401
    dias = int(request.args.get('dias', 30))
    desde = (datetime.now() - timedelta(days=dias)).strftime('%Y-%m-%dT00:00:00')
    nuevos, page = 0, 1
    while True:
        r = req_lib.get(f'{tn_base(tienda)}/orders', headers=tn_headers(tienda), params={
            'payment_status': 'paid', 'created_at_min': desde,
            'per_page': 50, 'page': page})
        if r.status_code != 200:
            break
        ordenes = r.json()
        if not ordenes:
            break
        for o in ordenes:
            if _registrar_venta(tienda, o) == 'registrada':
                nuevos += 1
        if len(ordenes) < 50:
            break
        page += 1
    return jsonify({'ok': True, 'nuevas': nuevos})


# ── Influencers ───────────────────────────────────────────────────────────────

def _slug(nombre):
    s = re.sub(r'[^a-z0-9]+', '-', nombre.lower().strip()).strip('-')
    return s or uuid.uuid4().hex[:8]

def _link_influencer(tienda, slug):
    url = (tienda._mapping['url'] or '').rstrip('/')
    return f'{url}/?utm_source={slug}&utm_medium=afiliado'


@app.route('/api/influencers', methods=['POST'])
def crear_influencer():
    tienda = tienda_actual()
    if not tienda:
        return jsonify({'error': 'No autorizado'}), 401
    tid = tienda._mapping['id']
    d = request.json or {}
    nombre = (d.get('nombre') or '').strip()
    if not nombre:
        return jsonify({'error': 'Falta el nombre'}), 400

    # Límite del plan gratuito
    if (tienda._mapping['plan'] or 'free') == 'free':
        with engine.connect() as conn:
            n = conn.execute(text(
                'SELECT COUNT(*) FROM influencers WHERE tienda_id=:t'),
                {'t': tid}).fetchone()[0]
        if n >= PLAN_FREE_MAX_INFLUENCERS:
            return jsonify({'error': f'El plan gratuito incluye {PLAN_FREE_MAX_INFLUENCERS} influencer. '
                                     'Pasate al plan Pro para agregar más.'}), 402

    slug = _slug(d.get('slug') or nombre)
    try:
        comision = float(d.get('comision_pct', COMISION_DEFAULT))
        descuento = float(d.get('descuento_pct', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Comisión o descuento inválido'}), 400

    with engine.connect() as conn:
        if conn.execute(text(
                'SELECT 1 FROM influencers WHERE slug=:s AND tienda_id=:t'),
                {'s': slug, 't': tid}).fetchone():
            return jsonify({'error': f'Ya existe un influencer con el código "{slug}"'}), 400

    cupon = ''
    aviso_cupon = ''
    if descuento > 0:
        cupon = f"{re.sub(r'[^A-Z0-9]', '', slug.upper())[:12]}{int(descuento)}"
        r = req_lib.post(f'{tn_base(tienda)}/coupons', headers=tn_headers(tienda), json={
            'code': cupon, 'type': 'percentage',
            'value': str(descuento), 'valid': True})
        if r.status_code not in (200, 201):
            aviso_cupon = f'No se pudo crear el cupón en Tienda Nube ({r.status_code}): {r.text[:200]}'

    token = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text('''
            INSERT INTO influencers (tienda_id, nombre, slug, instagram, email, alias_mp,
                                     comision_pct, descuento_pct, cupon_codigo,
                                     token, activo, creado)
            VALUES (:t, :n, :s, :ig, :em, :mp, :c, :d, :cup, :tok, 1, :creado)
        '''), {'t': tid, 'n': nombre, 's': slug, 'ig': (d.get('instagram') or '').strip(),
               'em': (d.get('email') or '').strip(), 'mp': (d.get('alias_mp') or '').strip(),
               'c': comision, 'd': descuento, 'cup': cupon, 'tok': token,
               'creado': _now()})
    return jsonify({'ok': True, 'slug': slug, 'cupon': cupon,
                    'aviso': aviso_cupon,
                    'link': _link_influencer(tienda, slug)})


@app.route('/api/influencers')
def listar_influencers():
    tienda = tienda_actual()
    if not tienda:
        return jsonify({'error': 'No autorizado'}), 401
    if not (tienda._mapping['url'] or '').strip():
        tienda = _refrescar_datos_tienda(tienda)
    with engine.connect() as conn:
        rows = conn.execute(text('''
            SELECT i.*,
                   COALESCE(SUM(CASE WHEN v.estado='pendiente' THEN v.comision END), 0) AS comision_pendiente,
                   COALESCE(SUM(v.comision), 0) AS comision_total,
                   COUNT(v.id) AS ventas_count
            FROM influencers i
            LEFT JOIN ventas v ON v.influencer_id = i.id
            WHERE i.tienda_id = :t
            GROUP BY i.id
            ORDER BY i.nombre
        '''), {'t': tienda._mapping['id']}).fetchall()
    out = []
    for r in rows:
        d = _row(r)
        d['link'] = _link_influencer(tienda, d['slug'])
        d['panel'] = f"/i/{d['token']}"
        out.append(d)
    return jsonify(out)


def _influencer_de_tienda(conn, iid, tienda_id):
    return conn.execute(text(
        'SELECT 1 FROM influencers WHERE id=:i AND tienda_id=:t'),
        {'i': iid, 't': tienda_id}).fetchone()


@app.route('/api/influencers/<int:iid>', methods=['PATCH'])
def editar_influencer(iid):
    tienda = tienda_actual()
    if not tienda:
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
    campos['tid'] = tienda._mapping['id']
    with engine.begin() as conn:
        if not _influencer_de_tienda(conn, iid, campos['tid']):
            return jsonify({'error': 'Influencer inexistente'}), 404
        conn.execute(text(f'UPDATE influencers SET {sets} WHERE id=:id AND tienda_id=:tid'), campos)
    return jsonify({'ok': True})


@app.route('/api/influencers/<int:iid>/liquidar', methods=['POST'])
def liquidar(iid):
    tienda = tienda_actual()
    if not tienda:
        return jsonify({'error': 'No autorizado'}), 401
    tid = tienda._mapping['id']
    notas = (request.json or {}).get('notas', '')
    with engine.begin() as conn:
        if not _influencer_de_tienda(conn, iid, tid):
            return jsonify({'error': 'Influencer inexistente'}), 404
        row = conn.execute(text('''
            SELECT COALESCE(SUM(comision), 0) FROM ventas
            WHERE influencer_id=:i AND tienda_id=:t AND estado='pendiente'
        '''), {'i': iid, 't': tid}).fetchone()
        monto = float(row[0] or 0)
        if monto <= 0:
            return jsonify({'error': 'No hay comisiones pendientes'}), 400
        res = conn.execute(text(
            'INSERT INTO liquidaciones (tienda_id, influencer_id, monto, fecha, notas) '
            'VALUES (:t, :i, :m, :f, :n)' + (' RETURNING id' if IS_PG else '')),
            {'t': tid, 'i': iid, 'm': monto, 'f': _now(), 'n': notas})
        lid = res.fetchone()[0] if IS_PG else res.lastrowid
        conn.execute(text('''
            UPDATE ventas SET estado='liquidada', liquidacion_id=:l
            WHERE influencer_id=:i AND tienda_id=:t AND estado='pendiente'
        '''), {'l': lid, 'i': iid, 't': tid})
    return jsonify({'ok': True, 'monto': monto})


@app.route('/api/ventas')
def listar_ventas():
    tienda = tienda_actual()
    if not tienda:
        return jsonify({'error': 'No autorizado'}), 401
    with engine.connect() as conn:
        rows = conn.execute(text('''
            SELECT v.*, i.nombre AS influencer, i.slug
            FROM ventas v JOIN influencers i ON i.id = v.influencer_id
            WHERE v.tienda_id = :t
            ORDER BY v.fecha DESC LIMIT 300
        '''), {'t': tienda._mapping['id']}).fetchall()
    return jsonify([_row(r) for r in rows])


@app.route('/api/ventas/<int:vid>', methods=['DELETE'])
def borrar_venta(vid):
    tienda = tienda_actual()
    if not tienda:
        return jsonify({'error': 'No autorizado'}), 401
    with engine.begin() as conn:
        row = conn.execute(text(
            'SELECT numero, estado FROM ventas WHERE id=:id AND tienda_id=:t'),
            {'id': vid, 't': tienda._mapping['id']}).fetchone()
        if not row:
            return jsonify({'error': 'Venta inexistente'}), 404
        if row[1] == 'liquidada':
            return jsonify({'error': 'Ya fue liquidada: borrarla desbalancearía las liquidaciones'}), 400
        conn.execute(text('DELETE FROM ventas WHERE id=:id AND tienda_id=:t'),
                     {'id': vid, 't': tienda._mapping['id']})
    return jsonify({'ok': True})


# ── Ventas de prueba (para testear el circuito) ───────────────────────────────

@app.route('/api/test/venta', methods=['POST'])
def crear_venta_prueba():
    tienda = tienda_actual()
    if not tienda:
        return jsonify({'error': 'No autorizado'}), 401
    tid = tienda._mapping['id']
    d = request.json or {}
    iid = d.get('influencer_id')
    try:
        total = float(d.get('total', 50000))
    except (TypeError, ValueError):
        return jsonify({'error': 'Total inválido'}), 400
    with engine.connect() as conn:
        inf = conn.execute(text(
            'SELECT * FROM influencers WHERE id=:i AND tienda_id=:t'),
            {'i': iid, 't': tid}).fetchone()
    if not inf:
        return jsonify({'error': 'Influencer inexistente'}), 400
    m = inf._mapping
    pct = float(m['comision_pct'] or 0)
    oid = f'TEST-{uuid.uuid4().hex[:8]}'
    with engine.begin() as conn:
        conn.execute(text('''
            INSERT INTO ventas (tienda_id, order_id, numero, fecha, cliente, total,
                                influencer_id, atribucion, comision_pct,
                                comision, estado, creado)
            VALUES (:tid, :oid, :num, :f, 'Cliente de prueba', :tot, :inf, 'cupon',
                    :pct, :com, 'pendiente', :creado)
        '''), {'tid': tid, 'oid': oid, 'num': oid, 'f': _now(), 'tot': total,
               'inf': m['id'], 'pct': pct,
               'com': round(total * pct / 100, 2), 'creado': _now()})
    return jsonify({'ok': True, 'order_id': oid})


@app.route('/api/test/ventas', methods=['DELETE'])
def borrar_ventas_prueba():
    tienda = tienda_actual()
    if not tienda:
        return jsonify({'error': 'No autorizado'}), 401
    with engine.begin() as conn:
        n = conn.execute(text(
            "DELETE FROM ventas WHERE order_id LIKE 'TEST-%' AND tienda_id=:t"),
            {'t': tienda._mapping['id']}).rowcount
    return jsonify({'ok': True, 'borradas': n})


# ── Superadmin (dueño de ReferidosApp) ────────────────────────────────────────

@app.route('/superadmin')
def superadmin():
    if not _es_superadmin():
        return 'No autorizado.', 401
    return render_template('superadmin.html', clave=SUPERADMIN_KEY)


@app.route('/api/superadmin/tiendas')
def superadmin_tiendas():
    if not _es_superadmin():
        return jsonify({'error': 'No autorizado'}), 401
    with engine.connect() as conn:
        rows = conn.execute(text('''
            SELECT t.id, t.store_id, t.nombre, t.url, t.email, t.plan, t.activa,
                   t.clave_admin, t.creado,
                   (SELECT COUNT(*) FROM influencers i WHERE i.tienda_id = t.id) AS influencers,
                   (SELECT COUNT(*) FROM ventas v WHERE v.tienda_id = t.id) AS ventas
            FROM tiendas t ORDER BY t.id
        ''')).fetchall()
    return jsonify([_row(r) for r in rows])


@app.route('/api/superadmin/tiendas/<int:tid>/plan', methods=['PATCH'])
def superadmin_cambiar_plan(tid):
    if not _es_superadmin():
        return jsonify({'error': 'No autorizado'}), 401
    plan = (request.json or {}).get('plan', '')
    if plan not in ('free', 'pro'):
        return jsonify({'error': 'Plan inválido (free|pro)'}), 400
    with engine.begin() as conn:
        conn.execute(text('UPDATE tiendas SET plan=:p WHERE id=:id'),
                     {'p': plan, 'id': tid})
    return jsonify({'ok': True})


# ── Billing API (sondeo para descubrir la forma exacta de los endpoints) ─────

@app.route('/api/superadmin/billing-debug')
def billing_debug():
    """Prueba los endpoints de Billing contra una tienda para confirmar
    concept_code, auth y formas de request antes de integrar en serio."""
    if not _es_superadmin():
        return jsonify({'error': 'No autorizado'}), 401
    store = request.args.get('tienda', '')
    tienda = tienda_por_store_id(store)
    if not tienda:
        return jsonify({'error': f'No hay tienda con store_id {store}'}), 404
    out = {'store_id': store, 'app_id': TN_CLIENT_ID, 'probes': {}}
    # Ruta arbitraria: ?path=/apps/36482/plans (relativa a la base 2025-03)
    extra = request.args.get('path', '')
    if extra:
        rutas = [('custom', f'{tn_base_billing(tienda)}{extra}')]
    else:
        rutas = [
            ('subs_app_cost', f'{tn_base_billing(tienda)}/concepts/app-cost/services/{TN_CLIENT_ID}/subscriptions'),
            ('plans_con_app_id', f'{tn_base_billing(tienda)}/apps/{TN_CLIENT_ID}/plans'),
        ]
    for nombre, url in rutas:
        try:
            r = req_lib.get(url, headers=tn_headers(tienda), timeout=20)
            es_html = 'html' in r.headers.get('content-type', '')
            out['probes'][nombre] = {
                'url': url, 'status': r.status_code,
                'body': '(html/bloqueado)' if es_html else r.text[:400],
            }
        except Exception as ex:
            out['probes'][nombre] = {'url': url, 'error': str(ex)}
    return jsonify(out)


# ── Vistas ────────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return render_template('index.html')


@app.route('/admin')
def admin():
    tienda = tienda_actual()
    if not tienda:
        return 'No autorizado. Ingresá con el link de admin de tu tienda.', 401
    if not (tienda._mapping['url'] or '').strip():
        tienda = _refrescar_datos_tienda(tienda)  # auto-reparación del dominio
    m = tienda._mapping
    return render_template('admin.html',
                           clave=m['clave_admin'],
                           tienda_nombre=m['nombre'] or m['url'] or 'tu tienda',
                           tienda_url=m['url'],
                           plan=m['plan'],
                           max_free=PLAN_FREE_MAX_INFLUENCERS,
                           comision_default=COMISION_DEFAULT)


@app.route('/privacidad')
def privacidad():
    contenido = f'''
<h1>Política de privacidad</h1>
<p>ReferidosApp es una aplicación para tiendas de la plataforma Tienda Nube que permite gestionar programas de afiliados e influencers. Esta política describe qué datos tratamos y cómo.</p>
<h2>Datos que tratamos</h2>
<ul>
<li><strong>Datos de la tienda:</strong> al instalar la app recibimos el identificador de la tienda, su nombre, dominio y email de contacto, junto con un token de acceso otorgado por Tienda Nube.</li>
<li><strong>Datos de influencers:</strong> los que el comerciante carga voluntariamente (nombre, Instagram, email, alias de pago, comisión).</li>
<li><strong>Datos de pedidos:</strong> para atribuir ventas leemos de cada pedido pago su número, total, cupón utilizado, origen de la visita y nombre del comprador. No almacenamos direcciones, teléfonos ni datos de pago de los compradores.</li>
</ul>
<h2>Uso de los datos</h2>
<p>Los datos se usan exclusivamente para el funcionamiento de la app: atribuir ventas a influencers, calcular comisiones y mostrar paneles a la tienda y a sus influencers. No vendemos ni compartimos datos con terceros.</p>
<h2>Almacenamiento y seguridad</h2>
<p>Los datos se almacenan en infraestructura cloud con acceso restringido y cifrado en tránsito. Cada tienda accede únicamente a sus propios datos.</p>
<h2>Baja y eliminación</h2>
<p>Al desinstalar la app, la tienda queda desactivada y dejamos de recibir sus datos. Ante una solicitud de eliminación (propia de la tienda o vía los webhooks de privacidad de Tienda Nube), los datos asociados se eliminan de forma permanente.</p>
<h2>Contacto</h2>
<p>Por consultas sobre esta política: <a href="mailto:{SOPORTE_EMAIL}">{SOPORTE_EMAIL}</a></p>
'''
    return render_template('legal.html', titulo='Política de privacidad', contenido=contenido)


@app.route('/soporte')
def soporte():
    contenido = f'''
<h1>Soporte de ReferidosApp</h1>
<p>¿Necesitás ayuda con la app? Estamos para ayudarte.</p>
<h2>Canales de contacto</h2>
<ul>
<li><strong>Email:</strong> <a href="mailto:{SOPORTE_EMAIL}">{SOPORTE_EMAIL}</a> — respondemos dentro de las 24 hs hábiles.</li>
</ul>
<h2>Preguntas frecuentes</h2>
<ul>
<li><strong>No encuentro mi link de acceso al panel:</strong> entrá a tu admin de Tienda Nube → Mis aplicaciones → ReferidosApp, y volvés a tu panel automáticamente.</li>
<li><strong>Una venta no se atribuyó:</strong> verificá que el pedido figure como pagado y que se haya usado el cupón o el link del influencer. La sincronización manual del panel repasa los últimos 30 días.</li>
<li><strong>Quiero cambiar de plan:</strong> desde tu panel, en el botón del plan (arriba a la derecha).</li>
</ul>
'''
    return render_template('legal.html', titulo='Soporte', contenido=contenido)


@app.route('/upgrade')
def upgrade():
    tienda = tienda_actual()
    if not tienda:
        return 'No autorizado.', 401
    m = tienda._mapping
    return render_template('upgrade.html',
                           clave=m['clave_admin'],
                           tienda_nombre=m['nombre'] or 'tu tienda',
                           plan=m['plan'],
                           precio=PRO_PRECIO_TXT,
                           link_pago=PRO_LINK_PAGO,
                           soporte=SOPORTE_EMAIL,
                           max_free=PLAN_FREE_MAX_INFLUENCERS)


@app.route('/i/<token>')
def panel_influencer(token):
    with engine.connect() as conn:
        inf = conn.execute(text('SELECT * FROM influencers WHERE token=:t'),
                           {'t': token}).fetchone()
        if not inf:
            return 'Link inválido.', 404
        tienda = conn.execute(text('SELECT * FROM tiendas WHERE id=:t'),
                              {'t': inf._mapping['tienda_id']}).fetchone()
        ventas = conn.execute(text('''
            SELECT fecha, numero, total, comision, estado FROM ventas
            WHERE influencer_id=:i ORDER BY fecha DESC LIMIT 200
        '''), {'i': inf._mapping['id']}).fetchall()
        liqs = conn.execute(text('''
            SELECT fecha, monto, notas FROM liquidaciones
            WHERE influencer_id=:i ORDER BY fecha DESC
        '''), {'i': inf._mapping['id']}).fetchall()
    m = inf._mapping
    t = tienda._mapping if tienda else {}
    pendiente = sum(v._mapping['comision'] for v in ventas
                    if v._mapping['estado'] == 'pendiente')
    return render_template('influencer.html',
                           inf=dict(m), ventas=[_row(v) for v in ventas],
                           liquidaciones=[_row(l) for l in liqs],
                           pendiente=pendiente,
                           tienda_nombre=t.get('nombre') or 'la tienda',
                           tienda=t.get('url') or '',
                           link=_link_influencer(tienda, m['slug']) if tienda else '')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5003))
    print(f'\n  ReferidosApp corriendo en: http://localhost:{port}\n')
    app.run(debug=False, host='0.0.0.0', port=port)
