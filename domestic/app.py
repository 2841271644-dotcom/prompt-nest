"""Private prompt & media collection, backed by one transactional SQLite database."""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__, static_folder=None)
MAX_FILE = 25 * 1024 * 1024
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE + 512 * 1024
app.config['MAX_FORM_PARTS'] = 10
DATA = Path(os.getenv('DATA_DIR', Path(__file__).parent / 'data')).resolve()
DATA.mkdir(exist_ok=True, parents=True)
DB = DATA / 'sucang.sqlite3'
INDEX = Path(__file__).resolve().parents[1] / 'dist' / 'index.html'
COOKIE = 'sucang_session'
SECURE = os.getenv('COOKIE_SECURE', '1') == '1'
LIFETIME = 7 * 86400
DUMMY = generate_password_hash(secrets.token_hex(20))

def connect():
    conn = sqlite3.connect(DB, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

with connect() as init:
    init.execute('PRAGMA journal_mode=WAL')
    init.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY, uid INTEGER NOT NULL REFERENCES users, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts(
            bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS media(id INTEGER PRIMARY KEY, mime TEXT NOT NULL, data BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS items(
            id TEXT PRIMARY KEY, uid INTEGER NOT NULL REFERENCES users, title TEXT NOT NULL,
            prompt TEXT NOT NULL, tags TEXT NOT NULL, mid INTEGER REFERENCES media, modified INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS item_owner ON items(uid, modified);
        CREATE INDEX IF NOT EXISTS item_media ON items(mid);
        CREATE TABLE IF NOT EXISTS history(
            uid INTEGER PRIMARY KEY REFERENCES users, item TEXT NOT NULL, snapshot TEXT, mid INTEGER REFERENCES media);
        CREATE INDEX IF NOT EXISTS undo_media ON history(mid);
    """)
init.close()

def db():
    if 'db' not in g:
        g.db = connect()
    return g.db

@app.teardown_appcontext
def close(_):
    if 'db' in g:
        g.db.close()

@contextmanager
def write():
    c = db()
    c.execute('BEGIN IMMEDIATE')
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise

class Problem(Exception):
    def __init__(self, message, code):
        self.message, self.code = message, code

@app.errorhandler(Problem)
def problem(exc):
    return jsonify(error=exc.message), exc.code

@app.errorhandler(HTTPException)
def bad_http(exc):
    message = {400:'请求内容无效',404:'未找到内容',405:'请求方式不支持',413:'文件或表单超出大小限制'}.get(exc.code, '请求未完成')
    return jsonify(error=message), exc.code

@app.after_request
def headers(r):
    r.headers['Cache-Control'] = 'no-store'
    r.headers['X-Content-Type-Options'] = 'nosniff'
    r.headers['X-Frame-Options'] = 'DENY'
    r.headers['Referrer-Policy'] = 'same-origin'
    return r

@app.before_request
def same_origin():
    if request.method in ('POST','PUT','DELETE'):
        origin = request.headers.get('Origin')
        if request.headers.get('Sec-Fetch-Site') == 'cross-site' or (origin and urlsplit(origin).netloc.lower() != request.host.lower()):
            raise Problem('请从本站发起操作', 403)

def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()

def user():
    token = request.cookies.get(COOKIE, '')
    row = db().execute('SELECT users.id,users.name FROM sessions JOIN users ON users.id=sessions.uid WHERE token=? AND expires>?',
                       (digest(token), int(time.time()))).fetchone()
    if not row:
        raise Problem('请先登录', 401)
    return row

def credentials():
    obj = request.get_json(silent=True)
    if not isinstance(obj, dict) or not all(isinstance(obj.get(k), str) for k in ('name','password')):
        raise Problem('请输入账号和密码', 400)
    name, password = obj['name'].strip(), obj['password']
    if not 2 <= len(name) <= 40 or any(not ch.isprintable() for ch in name) or not 8 <= len(password) <= 128:
        raise Problem('账号须为 2–40 个字符，密码须为 8–128 个字符', 400)
    return name,password

def throttle(action, name):
    now = int(time.time())
    remote = request.remote_addr or ''
    if os.getenv('TRUST_PROXY_IP') == '1':
        remote = request.headers.get('X-Real-IP') or remote
    with write() as c:
        c.execute('DELETE FROM attempts WHERE expires<=?', (now,))
        for value,limit in [(action+'-ip:'+remote,60 if action=='login' else 12),
                            (action+'-account:'+name.casefold(),12)]:
            key = digest(value)
            c.execute('INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1', (key,now+600))
            count = c.execute('SELECT count FROM attempts WHERE bucket=?',(key,)).fetchone()[0]
            if count > limit:
                raise Problem('操作过于频繁，请稍后重试',429)

def signin(c, uid, name):
    raw = secrets.token_urlsafe(32)
    now = int(time.time())
    c.execute('DELETE FROM sessions WHERE expires<=? OR token=?',(now,digest(request.cookies.get(COOKIE,''))))
    c.execute('INSERT INTO sessions VALUES(?,?,?)',(digest(raw),uid,now+LIFETIME))
    response = jsonify(name=name)
    response.set_cookie(COOKIE,raw,httponly=True,secure=SECURE,samesite='Lax',max_age=LIFETIME)
    return response

@app.post('/api/logout')
def logout():
    db().execute('DELETE FROM sessions WHERE token=?',(digest(request.cookies.get(COOKIE,'')),))
    response = jsonify(ok=True)
    response.delete_cookie(COOKIE, secure=SECURE,samesite='Lax',httponly=True)
    return response

@app.get('/api/me')
def me():
    return jsonify(name=user()['name'])

@app.get('/api/items')
def listing():
    uid = user()['id']
    rows = db().execute('SELECT items.id,title,prompt,tags,mime FROM items LEFT JOIN media ON mid=media.id WHERE uid=? ORDER BY modified DESC,items.id DESC',(uid,))
    return jsonify(items=[dict(dict(row),media=bool(row['mime'])) for row in rows],
                   canUndo=bool(db().execute('SELECT 1 FROM history WHERE uid=?',(uid,)).fetchone()))

def sniff(header):
    if header.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
    if header.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
    if header.startswith((b'GIF87a',b'GIF89a')): return 'image/gif'
    if header.startswith(b'RIFF') and header[8:12]==b'WEBP': return 'image/webp'
    if header[4:8]==b'ftyp' and header[8:12] in (b'isom',b'iso2',b'mp41',b'mp42',b'M4V ',b'avc1',b'dash',b'mp71',b'iso5',b'iso6'): return 'video/mp4'
    raise Problem('请选择 PNG、JPEG、GIF、WebP 图片或 MP4 视频',400)

def form_data():
    form = request.get_json(silent=True) if request.is_json else request.form
    if form is None or not hasattr(form,'get'):
        raise Problem('表单内容无效',400)
    fields = {}
    for key,limit in [('title',160),('prompt',60000),('tags',2048)]:
        if key in form:
            value = form.get(key)
            if not isinstance(value,str) or len(value)>limit:
                raise Problem('输入内容过长或格式无效',400)
            fields[key]=value.strip()
    file=request.files.get('file')
    upload=None
    if file and file.filename:
        stream=file.stream
        stream.seek(0,2)
        size=stream.tell()
        stream.seek(0)
        if size>MAX_FILE: raise Problem('文件超过 25 MiB',413)
        mime=sniff(stream.read(32))
        stream.seek(0)
        upload=(mime,size,stream)
    return fields,upload

def keep_history(c,uid,item,old):
    c.execute('INSERT OR REPLACE INTO history VALUES(?,?,?,?)',
              (uid,item,json.dumps(dict(old)) if old else None,old['mid'] if old else None))

def cleanup(c,ids):
    for mid in set(ids)-{None}:
        c.execute('DELETE FROM media WHERE id=? AND NOT EXISTS(SELECT 1 FROM items WHERE mid=?) AND NOT EXISTS(SELECT 1 FROM history WHERE mid=?)',(mid,mid,mid))

def previous_media(c,uid):
    row=c.execute('SELECT mid FROM history WHERE uid=?',(uid,)).fetchone()
    return row['mid'] if row else None

@app.post('/api/items')
@app.route('/api/items/<item>',methods=['PUT','DELETE'])
def mutate(item=None):
    uid=user()['id']
    delete=request.method=='DELETE'
    fields,upload=({},None) if delete else form_data()
    with write() as c:
        old=None
        if item is not None:
            old=c.execute('SELECT * FROM items WHERE id=? AND uid=?',(item,uid)).fetchone()
            if not old: raise Problem('收藏不存在',404)
        else:
            item=secrets.token_hex(16)
        prior_mid=previous_media(c,uid)
        keep_history(c,uid,item,old)
        if delete:
            c.execute('DELETE FROM items WHERE id=? AND uid=?',(item,uid))
        else:
            values=dict(old) if old else dict(title='',prompt='',tags='',mid=None)
            values.update(fields)
            if not values['title']: raise Problem('请填写收藏名称',400)
            if upload:
                mime,size,stream=upload
                values['mid']=c.execute('INSERT INTO media VALUES(NULL,?,zeroblob(?))',(mime,size)).lastrowid
                with c.blobopen('media','data',values['mid']) as blob:
                    while chunk:=stream.read(256*1024): blob.write(chunk)
            c.execute('INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?)',
                      (item,uid,values['title'],values['prompt'],values['tags'],values['mid'],time.time_ns()))
        cleanup(c,[prior_mid])
    return jsonify(ok=True,id=item)

@app.post('/api/undo')
def undo():
    uid=user()['id']
    with write() as c:
        h=c.execute('SELECT * FROM history WHERE uid=?',(uid,)).fetchone()
        if not h: raise Problem('没有可撤回的操作',409)
        item=h['item']
        current=c.execute('SELECT mid FROM items WHERE uid=? AND id=?',(uid,item)).fetchone()
        c.execute('DELETE FROM items WHERE uid=? AND id=?',(uid,item))
        if h['snapshot']:
            row=json.loads(h['snapshot'])
            c.execute('INSERT INTO items VALUES(:id,:uid,:title,:prompt,:tags,:mid,:modified)',row)
        c.execute('DELETE FROM history WHERE uid=?',(uid,))
        cleanup(c,[h['mid'],current['mid'] if current else None])
    return jsonify(ok=True)

@app.get('/api/media/<item>')
def media(item):
    uid=user()['id']
    # Hold this read snapshot through streaming so concurrent replace/delete cannot change the bytes.
    c=connect()
    c.execute('BEGIN')
    try:
        row=c.execute('SELECT mid,mime,length(data) AS size FROM items JOIN media ON mid=media.id WHERE items.id=? AND uid=?',(item,uid)).fetchone()
        if not row: raise Problem('素材不存在',404)
        size=row['size']
        start,end=0,size-1
        status=200
        if request.headers.get('Range'):
            parsed=request.range
            bounds=parsed.range_for_length(size) if parsed and parsed.units=='bytes' else None
            if not bounds:
                c.close()
                return Response(status=416,headers={'Content-Range':f'bytes */{size}'})
            start,stop=bounds
            end,status=stop-1,206
        blob=c.blobopen('media','data',row['mid'],readonly=True)
        blob.seek(start)
    except Exception:
        c.close()
        raise
    def release():
        blob.close()
        c.close()
    def chunks():
        remaining=end-start+1
        while remaining:
            chunk=blob.read(min(remaining,256*1024))
            if not chunk: break
            remaining-=len(chunk)
            yield chunk
    response=Response(chunks(),status=status,mimetype=row['mime'])
    response.call_on_close(release)
    response.headers['Content-Length']=str(end-start+1)
    response.headers['Accept-Ranges']='bytes'
    if status==206: response.headers['Content-Range']=f'bytes {start}-{end}/{size}'
    return response

@app.get('/')
def index():
    return send_file(INDEX)
