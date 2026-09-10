from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from urllib.parse import urlparse, urljoin
from functools import wraps
import json
import os
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from werkzeug.utils import secure_filename

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
app.secret_key = 'your-secret-key-here'

# 管理者認証情報
ADMIN_CREDENTIALS = {
    'admin': '123'
}

# ────────────────────────────────
# 気象警報・注意報設定
PREFECTURE_CODE = "020000"  # 青森県
AREA_NAME = "青森市"

# ワークショップ課題：青森市の市区町村コードに変更する
AREA_CODE = "0220100"  # 青森市

WARNING_URL = (
    f"https://www.jma.go.jp/bosai/warning/data/r8/{PREFECTURE_CODE}.json"
)

JST = timezone(timedelta(hours=9))

# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "レベル3大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "レベル3高潮警報",
    "09": "レベル3土砂災害警報",
    "10": "レベル2大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "レベル2高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "29": "レベル2土砂災害注意報",
    "32": "暴風雪特別警報",
    "33": "レベル5大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "レベル5高潮特別警報",
    "39": "レベル5土砂災害特別警報",
    "43": "レベル4大雨危険警報",
    "48": "レベル4高潮危険警報",
    "49": "レベル4土砂災害危険警報"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
INSTRUCTIONS_FILE = os.path.join(APP_DIR, 'data', 'instructions.json')
REGISTER_LOG_FILE = os.path.join(APP_DIR, 'data', 'shelter_register_logs.json')
BOARD_DRAFT_FILE = os.path.join(APP_DIR, 'data', 'board_drafts.json')
UPLOAD_DIR = os.path.join(APP_DIR, 'static', 'uploads')
ALLOWED_PHOTO_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def record_id(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def atomic_save_json(path, value):
    """同一ディレクトリへ一時保存してから置換し、途中書き込みを防ぐ。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix='.tmp-', suffix='.json', dir=directory
    )
    try:
        with os.fdopen(file_descriptor, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, path)
    except Exception:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
        raise

def normalize_shelter(record, fallback_id):
    """旧形式を残しながら、施設の共通項目をそろえる。"""
    if not isinstance(record, dict):
        return None
    normalized = dict(record)
    normalized['id'] = record_id(record.get('id'), fallback_id)
    normalized['name'] = str(record.get('name', '')).strip()
    normalized['address1'] = str(record.get('address1') or record.get('address') or '').strip()
    normalized['address2'] = str(record.get('address2') or '').strip()
    normalized['phone'] = str(record.get('phone') or record.get('contact') or '').strip()
    normalized['address'] = normalized['address1']
    normalized['contact'] = normalized['phone']
    photos = record.get('photos')
    if not isinstance(photos, list):
        photos = [record.get('photo')] if record.get('photo') else []
    normalized['photos'] = [photo for photo in photos if isinstance(photo, str) and photo]
    normalized.setdefault('capacity', '')
    normalized.setdefault('disaster_type', '')
    normalized.setdefault('information', record.get('info', ''))
    return normalized


def normalize_instruction(record, fallback_id):
    """旧形式の通知を発信履歴の共通項目へ読み替える。"""
    if not isinstance(record, dict):
        return None
    normalized = dict(record)
    normalized['id'] = record_id(record.get('id'), fallback_id)
    normalized['kind'] = record.get('kind') or '災害時の指示'
    if normalized['kind'] == '災害tips':
        normalized['kind'] = '防災tips'
    normalized['title'] = record.get('title') or str(record.get('content', ''))[:30]
    normalized['target'] = record.get('target') or ''
    normalized['content'] = str(record.get('content', ''))
    normalized['location'] = record.get('location') or ''
    normalized['occurred_at'] = record.get('occurred_at') or ''
    normalized['status'] = record.get('status') or '発信中'
    normalized['created_at'] = (
        record.get('created_at')
        or record.get('timestamp')
        or datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")
    )
    normalized['updated_at'] = record.get('updated_at') or normalized['created_at']
    return normalized


raw_shelters = load_json(DATA_FILE, [])
raw_instructions = load_json(INSTRUCTIONS_FILE, [])
shelters = [normalized for index, record in enumerate(raw_shelters, 1) if (normalized := normalize_shelter(record, index))]
instructions = [normalized for index, record in enumerate(raw_instructions, 1) if (normalized := normalize_instruction(record, index))]
register_logs = load_json(REGISTER_LOG_FILE, [])
board_drafts = load_json(BOARD_DRAFT_FILE, [])


def shelter_photos(shelter):
    """旧 photo 項目を含む施設データを写真配列へ正規化する。"""
    photos = shelter.get('photos')
    if isinstance(photos, list):
        return [photo for photo in photos if isinstance(photo, str) and photo]
    photo = shelter.get('photo')
    return [photo] if isinstance(photo, str) and photo else []


def save_register_logs():
    atomic_save_json(REGISTER_LOG_FILE, register_logs)


def save_board_drafts():
    atomic_save_json(BOARD_DRAFT_FILE, board_drafts)


def save_board_instructions():
    atomic_save_json(INSTRUCTIONS_FILE, instructions)


def next_record_id(records):
    return max((record.get('id', 0) for record in records), default=0) + 1


def add_register_log(action):
    register_logs.insert(0, {
        'operator': session.get('username', 'admin'),
        'created_at': get_japan_time(),
        'action': action,
    })
    save_register_logs()


def is_allowed_photo(filename):
    return (
        filename and '.' in filename
        and filename.rsplit('.', 1)[1].lower() in ALLOWED_PHOTO_EXTENSIONS
    )


def save_uploaded_photos(files):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    saved = []
    for uploaded in files:
        if not uploaded or not uploaded.filename:
            continue
        if not is_allowed_photo(uploaded.filename):
            raise ValueError('JPG、JPEG、PNG、GIF、WebP のみ登録できます。')
        safe_name = secure_filename(uploaded.filename)
        if not safe_name:
            raise ValueError('写真ファイル名が正しくありません。')
        stem, extension = os.path.splitext(safe_name)
        candidate = safe_name
        counter = 1
        while os.path.exists(os.path.join(UPLOAD_DIR, candidate)):
            candidate = f'{stem}-{counter}{extension}'
            counter += 1
        uploaded.save(os.path.join(UPLOAD_DIR, candidate))
        saved.append(url_for('static', filename=f'uploads/{candidate}'))
    return saved


def delete_photo_file(photo_url):
    prefix = url_for('static', filename='')
    if not isinstance(photo_url, str) or not photo_url.startswith(prefix):
        return
    relative_path = photo_url[len(prefix):].replace('/', os.sep)
    file_path = os.path.abspath(os.path.join(app.static_folder, relative_path))
    if file_path.startswith(os.path.abspath(UPLOAD_DIR) + os.sep) and os.path.isfile(file_path):
        os.remove(file_path)

def save_instructions():
    """指示ボードのデータをファイルに保存する"""
    try:
        atomic_save_json(INSTRUCTIONS_FILE, instructions)
    except Exception:
        pass

def save_shelters():
    """避難所データをファイルに保存する"""
    atomic_save_json(DATA_FILE, shelters)
# ────────────────────────────────

# ────────────────────────────────
# 認証関連の設定とヘルパー関数
def is_safe_url(target):
    """リダイレクト先URLが安全かどうかチェック"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

def login_required(f):
    """認証が必要なページに付けるデコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 現在のURLをnextパラメータとしてログイン画面にリダイレクト
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_japan_time():
    """日本時間（JST）の現在時刻を取得する"""
    return datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")


def format_report_time(iso_str):
    """気象庁の発表時刻（ISO形式）をJSTの表示用文字列に変換する"""
    if not iso_str:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        return parsed.strftime("%Y年%m月%d日 %H:%M")
    except ValueError:
        return iso_str


def shelter_distance(shelter):
    """避難所までの距離をメートルで返す。未登録の場合は並び替え末尾にする。"""
    value = shelter.get('distance_m', shelter.get('distance'))
    try:
        return float(value)
    except (TypeError, ValueError):
        return float('inf')


def is_enabled(shelter, *keys):
    """互換性のある項目名から真偽値を取得する。"""
    return any(shelter.get(key) is True for key in keys)


def filter_shelters(district=None, query=None, max_distance=None,
                    tsunami=False, pets=False, barrier_free=False,
                    supplies=False, sort='name'):
    """検索条件を適用した避難所一覧を返す。"""
    normalized_query = (query or '').strip().casefold()
    results = []
    for shelter in shelters:
        name = str(shelter.get('name', ''))
        if district and shelter.get('district') != district:
            continue
        if normalized_query and normalized_query not in name.casefold():
            continue
        if max_distance is not None and shelter_distance(shelter) > max_distance:
            continue
        if tsunami and not is_enabled(shelter, 'tsunami', 'tsunami_ready'):
            continue
        if pets and not is_enabled(shelter, 'pets', 'pet', 'pets_allowed'):
            continue
        if barrier_free and not is_enabled(shelter, 'barrier_free'):
            continue
        if supplies and not is_enabled(shelter, 'emergency_supplies', 'supplies'):
            continue
        results.append(shelter)

    if sort == 'distance':
        results.sort(key=lambda shelter: (shelter_distance(shelter), shelter.get('name', '')))
    elif sort == 'capacity':
        results.sort(key=lambda shelter: (-int(shelter.get('capacity', 0) or 0), shelter.get('name', '')))
    else:
        results.sort(key=lambda shelter: shelter.get('name', ''))
    return results


def parse_area_warnings(warning_data):
    """気象庁の新形式JSONから対象市区町村の発表・継続中の情報を抽出する"""
    if not isinstance(warning_data, list):
        raise ValueError("気象庁の警報・注意報データが新形式の配列ではありません")

    reports = [
        report for report in warning_data
        if isinstance(report, dict)
        and isinstance(report.get("reportDatetime"), str)
        and report.get("reportDatetime")
    ]
    latest_report = max(
        reports,
        key=lambda report: report["reportDatetime"],
        default={}
    )
    warning = latest_report.get("warning", {})
    class20_items = warning.get("class20Items", [])
    area = next(
        (
            item for item in class20_items
            if isinstance(item, dict) and item.get("areaCode") == AREA_CODE
        ),
        None
    )

    warnings = []
    if area:
        for kind in area.get("kinds", []):
            if not isinstance(kind, dict):
                continue
            status = kind.get("status", "")
            code = kind.get("code", "")
            if status in ("発表", "継続") and code:
                warnings.append({
                    "name": WARNING_CODES.get(
                        code,
                        f"不明な警報・注意報 (コード: {code})"
                    ),
                    "code": code,
                    "status": status
                })

    latest_report_datetime = latest_report.get("reportDatetime", "")
    return warnings, latest_report_datetime


def get_weather_warnings():
    """対象市区町村の警報・注意報を取得する"""
    try:
        # 青森県の新形式（令和8年～）警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        warnings, report_datetime = parse_area_warnings(warning_data)

        return {
            "area_name": AREA_NAME,
            "warnings": warnings,
            "report_time": format_report_time(report_datetime),
            "last_fetch_time": get_japan_time()
        }

    except Exception:
        return {
            "area_name": AREA_NAME,
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "error": True
        }


# トップページ：templates/index.html を返す（住民向け指示も表示する）
@app.route('/')
def index():
    resident_notices = [
        i for i in instructions
        if i.get('status') != '取消'
        and (i.get('target') == '住民' or i.get('kind') in ('防災tips', '獣害情報'))
    ]
    resident_tips = list(reversed([
        i for i in instructions
        if i.get('status') != '取消' and i.get('kind') == '防災tips'
    ]))
    resident_instructions = list(reversed([
        i for i in instructions
        if i.get('status') != '取消'
        and i.get('kind') == '災害時の指示'
        and i.get('target') == '住民'
    ]))
    staff_notices = list(reversed([
        i for i in instructions
        if i.get('status') != '取消'
        and i.get('kind') == '災害時の指示'
        and i.get('target')
        and i.get('target') != '住民'
    ]))
    return render_template(
        'index.html',
        resident_notices=resident_notices,
        resident_tips=resident_tips,
        resident_instructions=resident_instructions,
        staff_notices=staff_notices
    )

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトは避難所登録画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('shelter_register')

    if request.method == 'POST':
        password = request.form.get('password', '').strip()

        # 認証チェック
        username = next(
            (name for name, registered_password in ADMIN_CREDENTIALS.items()
             if registered_password == password),
            None
        )
        if username:
            session['logged_in'] = True
            session['username'] = username
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        return render_template('login.html', error=True, message="パスワードが正しくありません。", next=next_url)

    # ログイン済みの場合は指定されたページにリダイレクト
    if session.get('logged_in'):
        return redirect(next_url)

    return render_template('login.html', next=next_url)

# ログアウト
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# 避難所登録ページ
@app.route('/shelter_register', methods=['GET', 'POST'])
@login_required
def shelter_register():
    selected_id = request.args.get('selected', type=int)
    mode = request.args.get('mode', 'detail')
    message = request.args.get('message')
    error = None

    if request.method == 'POST':
        action = request.form.get('action', 'create')
        if action == 'delete':
            selected_id = request.form.get('shelter_id', type=int)
            shelter = next((item for item in shelters if item.get('id') == selected_id), None)
            if shelter is None:
                error = '削除する避難所が見つかりません。'
            else:
                shelters.remove(shelter)
                for photo in shelter_photos(shelter):
                    delete_photo_file(photo)
                save_shelters()
                add_register_log(f"{shelter.get('name', '避難所')}について削除しました")
                selected_id = None
                mode = 'detail'
                message = '避難所を削除しました。'
        else:
            mode = 'edit' if action == 'update' else 'create'
            name = request.form.get('name', '').strip()
            address1 = request.form.get('address1', '').strip()
            phone = request.form.get('phone', '').strip()
            capacity = request.form.get('capacity', '').strip()
            if not name or not address1 or not phone or not capacity:
                error = '名称、住所1、電話番号、最大収容人数は必須です。'
            elif not capacity.isdigit() or int(capacity) < 1:
                error = '最大収容人数は1以上の数値を入力してください。'
            else:
                try:
                    new_photos = save_uploaded_photos(request.files.getlist('photos'))
                    fields = {
                        'name': name,
                        'address1': address1,
                        'address2': request.form.get('address2', '').strip(),
                        'phone': phone,
                        'capacity': capacity,
                        'disaster_type': request.form.get('disaster_type', '').strip(),
                        'information': request.form.get('information', '').strip(),
                    }
                    if action == 'update':
                        selected_id = request.form.get('shelter_id', type=int)
                        shelter = next((item for item in shelters if item.get('id') == selected_id), None)
                        if shelter is None:
                            error = '更新する避難所が見つかりません。'
                        else:
                            existing_photos = shelter_photos(shelter)
                            deleted_photos = set(request.form.getlist('delete_photos'))
                            kept_photos = [photo for photo in existing_photos if photo not in deleted_photos]
                            for photo in deleted_photos:
                                delete_photo_file(photo)
                            shelter.update(fields)
                            shelter['photos'] = kept_photos + new_photos
                            save_shelters()
                            add_register_log(f"{name}について更新しました")
                            mode = 'detail'
                            message = '避難所を更新しました。'
                    else:
                        next_id = max((shelter.get('id', 0) for shelter in shelters), default=0) + 1
                        shelter = {'id': next_id, **fields, 'photos': new_photos}
                        shelters.append(shelter)
                        save_shelters()
                        add_register_log(f"{name}について登録しました")
                        selected_id = next_id
                        mode = 'detail'
                        message = '避難所を登録しました。'
                except ValueError as exc:
                    error = str(exc)

    selected = next((item for item in shelters if item.get('id') == selected_id), None)
    if selected is not None:
        selected = dict(selected)
        selected['photos'] = shelter_photos(selected)
    return render_template(
        'shelter_register.html',
        shelters=shelters,
        selected=selected,
        selected_id=selected_id,
        mode=mode,
        logs=register_logs,
        success=bool(message) and not error,
        message=message,
        error=bool(error),
        error_message=error,
    )

# 避難所検索ページ
@app.route('/shelter_search')
def shelter_search():
    return render_template(
        'shelter_search.html',
        shelter_names=sorted({s.get('name', '') for s in shelters if s.get('name')}),
        shelters=shelters
    )

# 避難所詳細ページ
@app.route('/shelter/<int:shelter_id>')
def shelter_detail(shelter_id):
    shelter = next((item for item in shelters if item.get('id') == shelter_id), None)
    if shelter is None:
        return '避難所が見つかりませんでした', 404
    return render_template('shelter_detail.html', shelter=shelter)

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    return render_template('search_results.html', results=shelters)


# 指示・発信ボード
@app.route('/board')
@login_required
def board():
    return render_template(
        'board.html',
        instructions=list(reversed(instructions)),
        drafts=list(reversed(board_drafts)),
        form_data={},
        message=request.args.get('message'),
        error=None,
    )


@app.route('/board', methods=['POST'])
@login_required
def board_action():
    action = request.form.get('action', 'send')
    allowed_types = {'災害時の指示', '防災tips', '獣害情報'}
    allowed_targets = {'住民', '消防団', '防災課', '道路管理課', 'A避難所', 'B避難所'}
    form_data = request.form.to_dict()
    message = None
    error = None

    if action == 'cancel':
        instruction_id = request.form.get('instruction_id', type=int)
        instruction = next((item for item in instructions if item.get('id') == instruction_id), None)
        if instruction is None:
            error = '取り消す発信が見つかりません。'
        else:
            instruction['status'] = '取消'
            instruction['updated_at'] = get_japan_time()
            save_board_instructions()
            message = '発信を取り消しました。'
    elif action == 'load_draft':
        draft_id = request.form.get('draft_id', type=int)
        draft = next((item for item in board_drafts if item.get('id') == draft_id), None)
        if draft is None:
            error = '呼び出す下書きを選択してください。'
        else:
            form_data = dict(draft)
            message = '下書きを呼び出しました。'
    elif action == 'save_draft':
        if len(board_drafts) >= 50:
            error = '下書きは最大50件まで保存できます。'
        else:
            draft = {
                'id': next_record_id(board_drafts),
                'kind': request.form.get('kind', ''),
                'title': request.form.get('title', '').strip(),
                'target': request.form.get('target', ''),
                'content': request.form.get('content', '').strip(),
                'location': request.form.get('location', '').strip(),
                'occurred_at': request.form.get('occurred_at', ''),
                'saved_at': get_japan_time(),
            }
            board_drafts.append(draft)
            save_board_drafts()
            form_data = dict(draft)
            message = '下書きを保存しました。'
    else:
        kind = request.form.get('kind', '').strip()
        title = request.form.get('title', '').strip()
        target = request.form.get('target', '').strip()
        content = request.form.get('content', '').strip()
        location = request.form.get('location', '').strip()
        occurred_at = request.form.get('occurred_at', '').strip()
        if not kind:
            error = '発信の種類を選択してください。'
        elif kind not in allowed_types:
            error = '発信の種類が正しくありません。'
        elif not title:
            error = 'タイトルを入力してください。'
        elif not content:
            error = '送信内容を入力してください。'
        elif kind == '災害時の指示' and target not in allowed_targets:
            error = '送信先を選択してください。'
        elif kind == '獣害情報' and (not location or not occurred_at):
            error = '発生場所と発生日時を入力してください。'
        else:
            record = {
                'id': next_record_id(instructions),
                'kind': kind,
                'title': title,
                'target': target if kind == '災害時の指示' else '',
                'content': content,
                'location': location if kind == '獣害情報' else '',
                'occurred_at': occurred_at if kind == '獣害情報' else '',
                'status': '発信中',
                'created_at': get_japan_time(),
                'updated_at': get_japan_time(),
            }
            instructions.append(record)
            save_board_instructions()
            message = '発信を保存しました。'

    return render_template(
        'board.html',
        instructions=list(reversed(instructions)),
        drafts=list(reversed(board_drafts)),
        form_data=form_data,
        message=message,
        error=error,
    )

# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    max_distance = request.args.get('max_distance', type=float)
    results = filter_shelters(
        district=request.args.get('district'),
        query=request.args.get('q'),
        max_distance=max_distance,
        tsunami=request.args.get('tsunami') == '1',
        pets=request.args.get('pets') == '1',
        barrier_free=request.args.get('barrier_free') == '1',
        supplies=request.args.get('supplies') == '1',
        sort=request.args.get('sort', 'name')
    )
    return render_template(
        'search_results.html',
        results=results,
        search_params=request.args
    )


@app.route('/checkin', methods=['POST'])
def checkin():
    shelter_id = request.form.get('shelter_id', type=int)
    shelter = next((item for item in shelters if item.get('id') == shelter_id), None)
    if shelter is None:
        return '避難所が見つかりませんでした', 404

    adults = request.form.get('adults', type=int) or 0
    children = request.form.get('children', type=int) or 0
    if not 0 <= adults <= 9 or not 0 <= children <= 9:
        return '人数の指定が正しくありません', 400

    session['last_checkin'] = {
        'shelter_id': shelter_id,
        'adults': adults,
        'children': children,
    }
    capacity = int(shelter.get('capacity', 0) or 0)
    remaining = capacity - (adults + children * 0.5)
    if not capacity:
        availability = '-'
    elif remaining <= 0:
        availability = '満席'
    elif remaining < capacity / 3:
        availability = '残り僅か'
    else:
        availability = '空きあり'
    if request.accept_mimetypes.best == 'application/json':
        return jsonify({
            'success': True,
            'shelter_id': shelter_id,
            'availability': availability,
            'remaining': remaining,
        })
    return redirect(url_for('shelter_detail', shelter_id=shelter_id))

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    results = filter_shelters(request.args.get('district'))

    if not results:
        # 見つからなければエラー JSON を返す
        return jsonify({'error': 'No shelters found'}), 404

    # 見つかったらリストを JSON で返す
    return jsonify(results)

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    return jsonify(get_weather_warnings())

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
