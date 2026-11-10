import os, csv, io, secrets, re, hmac, hashlib, json, urllib.request, urllib.error
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, jsonify, make_response, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import func, or_

try:
    import razorpay
except ImportError:
    razorpay = None

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY') or secrets.token_hex(32),
    # Neon PostgreSQL can close idle SSL connections; validate pooled connections before reuse.
    SQLALCHEMY_ENGINE_OPTIONS={
        'pool_pre_ping': True,
        'pool_recycle': 300,
        'pool_timeout': 30,
    },
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=50 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE','1') == '1',
    SESSION_COOKIE_NAME='avi_classes_session',
    PERMANENT_SESSION_LIFETIME=60*60*4,
)
raw_db = os.environ.get('DATABASE_URL','').strip()
if raw_db:
    if raw_db.startswith('postgres://'): raw_db = raw_db.replace('postgres://','postgresql://',1)
    if raw_db.startswith('postgresql://') and '+psycopg' not in raw_db: raw_db = raw_db.replace('postgresql://','postgresql+psycopg://',1)
    app.config['SQLALCHEMY_DATABASE_URI'] = raw_db
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE,'avi_class.db')

db = SQLAlchemy(app)
csrf = CSRFProtect(app)

@app.after_request
def add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    if request.is_secure or request.headers.get('X-Forwarded-Proto', '').lower() == 'https':
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return response

CLASSES = list(range(1,11))
SUBJECTS = ['Hindi','English','Mathematics','Science','Social Science','EVS','General Knowledge']

class User(db.Model):
    id=db.Column(db.Integer,primary_key=True); name=db.Column(db.String(120),nullable=False)
    email=db.Column(db.String(255),unique=True,nullable=False,index=True); password=db.Column(db.String(255),nullable=False)
    role=db.Column(db.String(20),nullable=False,default='student'); class_no=db.Column(db.Integer)
    created_at=db.Column(db.DateTime,default=datetime.utcnow)
class LoginThrottle(db.Model):
    # Additive security table; existing users, questions and attempts are untouched.
    id = db.Column(db.Integer, primary_key=True)
    throttle_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    failures = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

class UserPhone(db.Model):
    # Additive table: does not alter existing User rows or student/question records.
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True, index=True)
    phone = db.Column(db.String(16), nullable=False, unique=True, index=True)
    verified_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class PendingRegistration(db.Model):
    # Temporary server-side storage; password hashes are never put in the browser session.
    id = db.Column(db.Integer, primary_key=True)
    nonce = db.Column(db.String(64), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    class_no = db.Column(db.Integer, nullable=False)
    phone = db.Column(db.String(16), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)


class Question(db.Model):
    id=db.Column(db.Integer,primary_key=True); class_no=db.Column(db.Integer,nullable=False,index=True)
    subject=db.Column(db.String(80),nullable=False,index=True); chapter=db.Column(db.String(160),default='')
    question=db.Column(db.Text,nullable=False); a=db.Column(db.Text,nullable=False); b=db.Column(db.Text,nullable=False)
    c=db.Column(db.Text,nullable=False); d=db.Column(db.Text,nullable=False); answer=db.Column(db.String(1),nullable=False)
    explanation=db.Column(db.Text,default=''); published=db.Column(db.Boolean,default=False,index=True)
    created_at=db.Column(db.DateTime,default=datetime.utcnow)
class Attempt(db.Model):
    id=db.Column(db.Integer,primary_key=True); user_id=db.Column(db.Integer,db.ForeignKey('user.id'),nullable=False,index=True)
    class_no=db.Column(db.Integer,nullable=False); subject=db.Column(db.String(80),nullable=False)
    score=db.Column(db.Integer,nullable=False); total=db.Column(db.Integer,nullable=False); created_at=db.Column(db.DateTime,default=datetime.utcnow)

CONTENT_CATEGORIES = {
    'test-series': 'Test Series', 'current-affairs': 'Current Affairs',
    'live-batches': 'Live Batches', 'recorded-batches': 'Recorded Batches',
    'daily-quiz': 'Daily Quiz', 'downloads': 'Downloads',
    'free-videos': 'Free Videos', 'courses': 'My Courses',
    'materials-store': 'Materials Store'
}
ALLOWED_UPLOADS = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.zip', '.jpg', '.jpeg', '.png', '.mp4', '.mp3'}
UPLOAD_DIR = os.path.join(BASE, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

class LearningItem(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    category=db.Column(db.String(40),nullable=False,index=True)
    title=db.Column(db.String(180),nullable=False)
    description=db.Column(db.Text,default='')
    class_no=db.Column(db.Integer,nullable=True,index=True)
    subject=db.Column(db.String(80),default='')
    file_name=db.Column(db.String(255),default='')
    stored_name=db.Column(db.String(255),default='')
    external_url=db.Column(db.String(1000),default='')
    schedule_text=db.Column(db.String(255),default='')
    price=db.Column(db.String(50),default='Free')
    published=db.Column(db.Boolean,default=True,index=True)
    created_at=db.Column(db.DateTime,default=datetime.utcnow)

class CourseEnrollment(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    user_id=db.Column(db.Integer,db.ForeignKey('user.id'),nullable=False,index=True)
    item_id=db.Column(db.Integer,db.ForeignKey('learning_item.id'),nullable=False,index=True)
    created_at=db.Column(db.DateTime,default=datetime.utcnow)
    __table_args__=(db.UniqueConstraint('user_id','item_id',name='uq_course_enrollment'),)

class BillingSubscription(db.Model):
    # Separate table: adding it does not alter or delete existing student/question records.
    id=db.Column(db.Integer,primary_key=True)
    user_id=db.Column(db.Integer,db.ForeignKey('user.id'),nullable=False,unique=True,index=True)
    razorpay_subscription_id=db.Column(db.String(80),nullable=False,unique=True,index=True)
    status=db.Column(db.String(30),nullable=False,default='created',index=True)
    current_start=db.Column(db.Integer)
    current_end=db.Column(db.Integer)
    created_at=db.Column(db.DateTime,default=datetime.utcnow)
    updated_at=db.Column(db.DateTime,default=datetime.utcnow,onupdate=datetime.utcnow)

with app.app_context():
    db.create_all()
    admin_email=os.environ.get('ADMIN_EMAIL','admin@aviclasstr04.in').lower().strip()
    admin_password=os.environ.get('ADMIN_PASSWORD','')
    admin_user = User.query.filter_by(email=admin_email).first()

    if not admin_user:
        if not admin_password:
            admin_password = secrets.token_urlsafe(18)
            print('[AVI CLASS TR-04] One-time generated admin password:', admin_password)

        admin_user = User(
            name='AVI CLASS TR-04 Admin',
            email=admin_email,
            password=generate_password_hash(admin_password),
            role='admin'
        )
        db.session.add(admin_user)

    elif admin_password:
        admin_user.password = generate_password_hash(admin_password)

    db.session.commit()
RAZORPAY_PLAN_ID = os.environ.get('RAZORPAY_PLAN_ID', 'plan_TwkTkdtUQrO23M').strip()
RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID', '').strip()
RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', '').strip()
RAZORPAY_WEBHOOK_SECRET = os.environ.get('RAZORPAY_WEBHOOK_SECRET', '').strip()

MSG91_WIDGET_ID = os.environ.get('MSG91_WIDGET_ID', '').strip()
MSG91_WIDGET_TOKEN = os.environ.get('MSG91_WIDGET_TOKEN', '').strip()
MSG91_AUTHKEY = os.environ.get('MSG91_AUTHKEY', '').strip()
ADMIN_PHONE = re.sub(r'\D', '', os.environ.get('ADMIN_PHONE', ''))
if len(ADMIN_PHONE) == 10: ADMIN_PHONE = '91' + ADMIN_PHONE
MSG91_VERIFY_URL = 'https://control.msg91.com/api/v5/widget/verifyAccessToken'

def msg91_verify_access_token(access_token, expected_phone):
    """Verify MSG91's widget-issued access token server-side; never trust browser success alone."""
    if not (MSG91_AUTHKEY and access_token and expected_phone):
        return False
    payload = json.dumps({'authkey': MSG91_AUTHKEY, 'access-token': access_token}).encode('utf-8')
    req = urllib.request.Request(
        MSG91_VERIFY_URL, data=payload,
        headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            result = json.loads(response.read().decode('utf-8'))
        # MSG91 returns verified user details along with a success status.
        if not isinstance(result, dict):
            return False
        status = str(result.get('type', result.get('status', ''))).lower()
        if status not in ('success', 'verified', 'true', '200'):
            return False
        # Match the verified identifier to the phone being enrolled/authenticated.
        candidate = result.get('message') or result.get('data') or result
        if isinstance(candidate, dict):
            found = candidate.get('identifier') or candidate.get('mobile') or candidate.get('phone') or candidate.get('user')
        else:
            found = candidate if isinstance(candidate, str) else None
        if found:
            return re.sub(r'\D', '', str(found)).endswith(expected_phone[-10:])
        # Some MSG91 responses confirm token validity without echoing the identifier.
        # In that case the widget token is valid; server-side pending phone binding still applies.
        return True
    except Exception:
        app.logger.exception('MSG91 access-token verification failed')
        return False

def normalize_indian_phone(raw):
    digits = re.sub(r'\D', '', raw or '')
    if len(digits) == 10:
        digits = '91' + digits
    if len(digits) != 12 or not digits.startswith('91'):
        return ''
    return digits

def msg91_template_config():
    return {'widget_id': MSG91_WIDGET_ID, 'widget_token': MSG91_WIDGET_TOKEN}



def razorpay_client():
    if not razorpay or not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET or not RAZORPAY_PLAN_ID:
        raise RuntimeError('Razorpay configuration is incomplete.')
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


def billing_is_active(user_id):
    record = BillingSubscription.query.filter_by(user_id=user_id).first()
    return record is None or record.status == 'active'  # Existing students are grandfathered.


def set_subscription_state(subscription_id, status, entity=None):
    record = BillingSubscription.query.filter_by(razorpay_subscription_id=subscription_id).first()
    if not record:
        return False
    record.status = status or record.status
    entity = entity or {}
    if entity.get('current_start'):
        record.current_start = int(entity['current_start'])
    if entity.get('current_end'):
        record.current_end = int(entity['current_end'])
    db.session.commit()
    return True


@app.context_processor
def globals_for_templates(): return {'classes':CLASSES,'subjects':SUBJECTS,'content_categories':CONTENT_CATEGORIES, 'otp_config': msg91_template_config() if 'msg91_template_config' in globals() else {'widget_id': '', 'widget_token': ''}}

def _login_throttle_key(email):
    # Hash email + client address; do not store raw IP addresses in the throttle table.
    raw = f"{email.lower().strip()}|{request.remote_addr or 'unknown'}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def _login_is_locked(key):
    row = LoginThrottle.query.filter_by(throttle_key=key).first()
    now = datetime.utcnow()
    if row and row.locked_until and row.locked_until > now:
        return True
    if row and row.locked_until and row.locked_until <= now:
        row.failures = 0
        row.locked_until = None
        db.session.commit()
    return False

def _login_failed(key):
    now = datetime.utcnow()
    row = LoginThrottle.query.filter_by(throttle_key=key).first()
    if row is None:
        row = LoginThrottle(throttle_key=key, failures=0)
        db.session.add(row)
    row.failures += 1
    row.updated_at = now
    if row.failures >= 5:
        row.locked_until = now + __import__('datetime').timedelta(minutes=15)
    db.session.commit()

def _login_succeeded(key):
    LoginThrottle.query.filter_by(throttle_key=key).delete()
    db.session.commit()

def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            if not session.get('user_id'):
                flash('कृपया पहले लॉगिन करें।','error'); return redirect(url_for('login',next=request.path))
            current_user = db.session.get(User, session.get('user_id'))
            if not current_user or current_user.role != session.get('role'):
                session.clear()
                abort(403)
            if role and current_user.role != role: abort(403)
            now_ts = datetime.utcnow().timestamp()
            idle_limit = 30 * 60 if current_user.role == 'admin' else 4 * 60 * 60
            last_seen = float(session.get('last_seen', now_ts))
            if now_ts - last_seen > idle_limit:
                session.clear()
                flash('सुरक्षा के लिए आपका सत्र समाप्त हुआ। कृपया फिर लॉगिन करें।', 'error')
                return redirect(url_for('login'))
            session['last_seen'] = now_ts
            if session.get('role') == 'student' and request.endpoint not in {'payment','payment_verify','payment_status','logout'}:
                if not billing_is_active(session.get('user_id')):
                    return redirect(url_for('payment'))
            return fn(*args,**kwargs)
        return wrapped
    return deco

def valid_question_form(data):
    cn=data.get('class_no',''); sub=data.get('subject','')
    vals=[data.get(k,'').strip() for k in ('question','a','b','c','d')]
    ans=data.get('answer','').upper()
    return cn in [str(x) for x in CLASSES] and sub in SUBJECTS and all(vals) and ans in ['A','B','C','D']

@app.route('/')
def home():
    total=Question.query.filter_by(published=True).count()
    counts={r[0]:r[1] for r in db.session.query(Question.class_no,func.count(Question.id)).filter_by(published=True).group_by(Question.class_no).all()}
    recent_items = LearningItem.query.filter_by(published=True).order_by(LearningItem.created_at.desc()).limit(3).all()
    subjects=db.session.query(Question.subject,func.count(Question.id).label('n')).filter_by(published=True).group_by(Question.subject).order_by(Question.subject).all()
    attempts=[]
    courses=LearningItem.query.filter_by(category='courses',published=True).all()
    return render_template('home.html',total=total,counts=counts,recent_items=recent_items,subjects=subjects,attempts=attempts,courses=courses)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        pw = request.form.get('password', '')
        cn = request.form.get('class_no', '')
        phone = normalize_indian_phone(request.form.get('phone', ''))
        if not name or len(name) > 120 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email) or len(pw) < 8 or cn not in [str(x) for x in CLASSES] or not phone:
            flash('सही जानकारी भरें। मोबाइल नंबर 10 अंकों का और पासवर्ड कम से कम 8 अक्षरों का हो।', 'error')
        elif User.query.filter_by(email=email).first():
            flash('यह ईमेल पहले से रजिस्टर है।', 'error')
        elif UserPhone.query.filter_by(phone=phone).first():
            flash('यह मोबाइल नंबर पहले से किसी अकाउंट से जुड़ा है।', 'error')
        elif not (MSG91_WIDGET_ID and MSG91_WIDGET_TOKEN and MSG91_AUTHKEY):
            flash('OTP सेवा अभी सेटअप नहीं है। कृपया Admin से संपर्क करें।', 'error')
        else:
            nonce = secrets.token_urlsafe(32)
            db.session.add(PendingRegistration(
                nonce=nonce, name=name, email=email,
                password_hash=generate_password_hash(pw), class_no=int(cn),
                phone=phone, expires_at=datetime.utcnow() + __import__('datetime').timedelta(minutes=10)
            ))
            db.session.commit()
            session['otp_pending'] = {
                'purpose': 'register', 'nonce': nonce, 'phone': phone,
                'created_at': datetime.utcnow().timestamp()
            }
            return render_template('register.html', otp_config=msg91_template_config(), otp_pending=True)
    return render_template('register.html', otp_config=msg91_template_config(), otp_pending=False)


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Normal login uses email/password only; OTP is reserved for registration and password reset."""
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        key = _login_throttle_key(email)
        if _login_is_locked(key):
            flash('बहुत बार गलत प्रयास हुए हैं। सुरक्षा के लिए 15 मिनट बाद पुनः प्रयास करें।', 'error')
            return render_template('login.html'), 429
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, request.form.get('password', '')):
            _login_succeeded(key)
            session.pop('otp_pending', None)
            session.clear()
            session.permanent = True
            session.update(user_id=user.id, name=user.name, role=user.role,
                           class_no=user.class_no, last_seen=datetime.utcnow().timestamp())
            if user.role == 'student' and not billing_is_active(user.id):
                return redirect(url_for('payment'))
            return redirect(url_for('admin' if user.role == 'admin' else 'student'))
        _login_failed(key)
        flash('ईमेल या पासवर्ड सही नहीं है।', 'error')
    return render_template('login.html')


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """Start a password reset using the already-verified mobile number on a student account."""
    if request.method == 'POST':
        phone = normalize_indian_phone(request.form.get('phone', ''))
        if not phone:
            flash('कृपया सही 10 अंकों का मोबाइल नंबर डालें।', 'error')
            return render_template('forgot_password.html', otp_pending=False)
        phone_row = UserPhone.query.filter_by(phone=phone).first()
        user = db.session.get(User, phone_row.user_id) if phone_row else None
        # Do not disclose whether a phone number is registered.
        if not user or user.role != 'student':
            flash('यदि इस नंबर से विद्यार्थी अकाउंट जुड़ा है, तो OTP सत्यापन उपलब्ध होगा। सही रजिस्टर्ड नंबर जाँचें या Admin से संपर्क करें।', 'info')
            return render_template('forgot_password.html', otp_pending=False)
        session.pop('password_reset_verified', None)
        session['otp_pending'] = {
            'purpose': 'forgot_password', 'user_id': user.id, 'phone': phone,
            'created_at': datetime.utcnow().timestamp()
        }
        return render_template('forgot_password.html', otp_pending=True, pending_phone=phone)
    return render_template('forgot_password.html', otp_pending=False)


@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    grant = session.get('password_reset_verified') or {}
    if not grant or datetime.utcnow().timestamp() - float(grant.get('created_at', 0)) > 600:
        session.pop('password_reset_verified', None)
        flash('पासवर्ड रीसेट की समय-सीमा समाप्त हो गई। OTP से दोबारा सत्यापन करें।', 'error')
        return redirect(url_for('forgot_password'))
    user = db.session.get(User, int(grant.get('user_id', 0)))
    if not user or user.role != 'student':
        session.pop('password_reset_verified', None)
        flash('अकाउंट सत्यापित नहीं हो सका।', 'error')
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        if len(new_password) < 10:
            flash('नया पासवर्ड कम से कम 10 अक्षरों का रखें।', 'error')
        elif new_password != confirm_password:
            flash('दोनों पासवर्ड एक जैसे नहीं हैं।', 'error')
        else:
            user.password = generate_password_hash(new_password)
            db.session.commit()
            session.pop('password_reset_verified', None)
            session.pop('otp_pending', None)
            flash('आपका पासवर्ड सफलतापूर्वक बदल गया है। अब नए पासवर्ड से लॉगिन करें।', 'success')
            return redirect(url_for('login'))
    return render_template('reset_password.html')


@app.route('/otp/complete', methods=['POST'])
def otp_complete():
    pending = session.get('otp_pending') or {}
    if not pending or datetime.utcnow().timestamp() - float(pending.get('created_at', 0)) > 600:
        session.pop('otp_pending', None)
        return jsonify(ok=False, message='OTP प्रक्रिया समाप्त हो गई। दोबारा शुरू करें।'), 400
    data = request.get_json(silent=True) or {}
    access_token = str(data.get('access_token', '')).strip()
    phone = normalize_indian_phone(data.get('phone') or pending.get('phone') or '')
    if not access_token or not phone:
        return jsonify(ok=False, message='मोबाइल नंबर और OTP verification जरूरी है।'), 400
    if pending.get('phone') and pending['phone'] != phone:
        return jsonify(ok=False, message='मोबाइल नंबर मेल नहीं खाता।'), 400
    if not msg91_verify_access_token(access_token, phone):
        return jsonify(ok=False, message='MSG91 OTP सत्यापन नहीं हो सका। दोबारा प्रयास करें।'), 401

    purpose = pending.get('purpose')
    if purpose == 'register':
        staged = PendingRegistration.query.filter_by(nonce=pending.get('nonce')).first()
        if not staged or staged.expires_at < datetime.utcnow():
            if staged: db.session.delete(staged); db.session.commit()
            session.pop('otp_pending', None)
            return jsonify(ok=False, message='रजिस्ट्रेशन की समय-सीमा समाप्त। दोबारा शुरू करें।'), 400
        if User.query.filter_by(email=staged.email).first() or UserPhone.query.filter_by(phone=phone).first():
            db.session.delete(staged); db.session.commit()
            session.pop('otp_pending', None)
            return jsonify(ok=False, message='ईमेल या मोबाइल पहले से रजिस्टर है।'), 409
        try:
            user = User(name=staged.name, email=staged.email,
                        password=staged.password_hash, role='student',
                        class_no=int(staged.class_no))
            db.session.add(user)
            db.session.flush()
            db.session.add(UserPhone(user_id=user.id, phone=phone))
            db.session.delete(staged)
            db.session.commit()
            session.pop('otp_pending', None)
            session.clear()
            session.permanent = True
            session.update(user_id=user.id, name=user.name, role=user.role,
                           class_no=user.class_no, last_seen=datetime.utcnow().timestamp())
            try:
                client = razorpay_client()
                subscription = client.subscription.create(data={
                    'plan_id': RAZORPAY_PLAN_ID, 'total_count': 20, 'quantity': 1,
                    'customer_notify': True, 'notes': {'avi_user_id': str(user.id), 'course': 'AVI CLASSES'}
                })
                db.session.add(BillingSubscription(user_id=user.id,
                    razorpay_subscription_id=subscription['id'],
                    status=subscription.get('status', 'created')))
                db.session.commit()
                return jsonify(ok=True, redirect=url_for('payment'))
            except Exception:
                app.logger.exception('Razorpay subscription creation failed after verified registration')
                return jsonify(ok=True, redirect=url_for('payment'),
                               message='अकाउंट बन गया; भुगतान सेवा अभी उपलब्ध नहीं है।'), 200
        except Exception:
            db.session.rollback()
            app.logger.exception('Verified registration failed')
            return jsonify(ok=False, message='रजिस्ट्रेशन पूरा नहीं हो सका।'), 500

    user = db.session.get(User, int(pending.get('user_id', 0)))
    if not user:
        session.pop('otp_pending', None)
        return jsonify(ok=False, message='अकाउंट नहीं मिला।'), 404
    if purpose == 'forgot_password':
        row = UserPhone.query.filter_by(user_id=user.id).first()
        if user.role != 'student' or not row or row.phone != phone:
            return jsonify(ok=False, message='मोबाइल नंबर इस विद्यार्थी अकाउंट से मेल नहीं खाता।'), 403
        session.pop('otp_pending', None)
        session['password_reset_verified'] = {
            'user_id': user.id, 'created_at': datetime.utcnow().timestamp()
        }
        return jsonify(ok=True, redirect=url_for('reset_password'))
    if purpose == 'enroll_existing':
        if user.role != 'student' or UserPhone.query.filter_by(phone=phone).first():
            return jsonify(ok=False, message='यह मोबाइल नंबर किसी अन्य अकाउंट से जुड़ा है।'), 409
        db.session.add(UserPhone(user_id=user.id, phone=phone))
        db.session.commit()
    elif purpose == 'login' and user.role == 'admin':
        if not ADMIN_PHONE or phone != ADMIN_PHONE:
            return jsonify(ok=False, message='Admin मोबाइल नंबर सही नहीं है।'), 403
    elif purpose == 'login':
        row = UserPhone.query.filter_by(user_id=user.id).first()
        if not row or row.phone != phone:
            return jsonify(ok=False, message='मोबाइल नंबर मेल नहीं खाता।'), 403

    session.pop('otp_pending', None)
    session.clear()
    session.permanent = True
    session.update(user_id=user.id, name=user.name, role=user.role,
                   class_no=user.class_no, last_seen=datetime.utcnow().timestamp())
    if user.role == 'student' and not billing_is_active(user.id):
        return jsonify(ok=True, redirect=url_for('payment'))
    return jsonify(ok=True, redirect=url_for('admin' if user.role == 'admin' else 'student'))


@app.route('/payment')
@login_required('student')
def payment():
    billing = BillingSubscription.query.filter_by(user_id=session['user_id']).first()
    if not billing:
        return redirect(url_for('student'))
    if billing.status == 'active':
        return redirect(url_for('student'))
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET or not razorpay:
        flash('भुगतान सेटअप अधूरा है। कृपया AVI CLASSES Admin से संपर्क करें।','error')
        return render_template('payment.html', billing=billing, key_id='', checkout_ready=False, current_user_email=User.query.get(session['user_id']).email)
    return render_template('payment.html', billing=billing, key_id=RAZORPAY_KEY_ID, checkout_ready=True, current_user_email=User.query.get(session['user_id']).email)


@app.route('/payment/verify', methods=['POST'])
@login_required('student')
def payment_verify():
    data = request.get_json(silent=True) or request.form
    payment_id = str(data.get('razorpay_payment_id','')).strip()
    subscription_id = str(data.get('razorpay_subscription_id','')).strip()
    signature = str(data.get('razorpay_signature','')).strip()
    billing = BillingSubscription.query.filter_by(user_id=session['user_id'], razorpay_subscription_id=subscription_id).first()
    if not billing or not payment_id or not signature:
        return jsonify(ok=False, message='भुगतान की जानकारी सही नहीं है।'), 400
    try:
        # Razorpay Subscription Checkout signature: HMAC-SHA256(payment_id|subscription_id).
        expected = hmac.new(RAZORPAY_KEY_SECRET.encode(), f'{payment_id}|{subscription_id}'.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return jsonify(ok=False, message='भुगतान सत्यापित नहीं हो सका।'), 400
        remote = razorpay_client().subscription.fetch(subscription_id)
        set_subscription_state(subscription_id, remote.get('status','created'), remote)
        if remote.get('status') == 'active':
            return jsonify(ok=True, active=True, redirect=url_for('student'))
        return jsonify(ok=True, active=False, redirect=url_for('payment'), message='आपकी AutoPay अनुमति मिल गई है। भुगतान सक्रिय होने की पुष्टि का इंतजार है।')
    except Exception:
        app.logger.exception('Razorpay subscription verification failed')
        return jsonify(ok=False, message='भुगतान की पुष्टि अभी नहीं हो सकी। कृपया थोड़ी देर बाद फिर देखें।'), 503


@app.route('/payment/status')
@login_required('student')
def payment_status():
    billing = BillingSubscription.query.filter_by(user_id=session['user_id']).first()
    if not billing or billing.status == 'active':
        return redirect(url_for('student'))
    return redirect(url_for('payment'))


@app.route('/webhooks/razorpay', methods=['POST'])
@csrf.exempt
def razorpay_webhook():
    if not RAZORPAY_WEBHOOK_SECRET:
        return jsonify(ok=False, message='Webhook secret not configured'), 503
    raw_body = request.get_data(cache=True)
    received = request.headers.get('X-Razorpay-Signature','')
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    if not received or not hmac.compare_digest(expected, received):
        abort(400)
    payload = request.get_json(silent=True) or {}
    event = payload.get('event','')
    entity = ((payload.get('payload') or {}).get('subscription') or {}).get('entity') or {}
    subscription_id = entity.get('id')
    if subscription_id:
        status_map = {
            'subscription.activated': 'active',
            'subscription.charged': 'active',
            'subscription.pending': 'pending',
            'subscription.halted': 'halted',
            'subscription.cancelled': 'cancelled',
            'subscription.completed': 'completed',
        }
        if event in status_map:
            set_subscription_state(subscription_id, status_map[event], entity)
    return jsonify(ok=True)


@app.route('/logout',methods=['POST'])
@login_required()
def logout(): session.clear(); return redirect(url_for('home'))

@app.route('/admin')
@login_required('admin')
def admin():
    total=Question.query.count(); live=Question.query.filter_by(published=True).count()
    students=User.query.filter_by(role='student').count(); rows=Question.query.order_by(Question.id.desc()).limit(20).all()
    return render_template('admin.html',total=total,live=live,drafts=total-live,students=students,rows=rows)

@app.route('/admin/questions/new',methods=['GET','POST'])
@login_required('admin')
def new_question():
    if request.method=='POST':
        if not valid_question_form(request.form): flash('प्रश्न, चारों विकल्प, सही उत्तर, कक्षा और विषय सही भरें।','error')
        else:
            q=Question(class_no=int(request.form['class_no']),subject=request.form['subject'],chapter=request.form.get('chapter','').strip(),question=request.form['question'].strip(),a=request.form['a'].strip(),b=request.form['b'].strip(),c=request.form['c'].strip(),d=request.form['d'].strip(),answer=request.form['answer'].upper(),explanation=request.form.get('explanation','').strip(),published=request.form.get('published')=='on')
            db.session.add(q); db.session.commit(); flash('प्रश्न सेव हो गया।','success'); return redirect(url_for('admin'))
    return render_template('question_form.html',q=None)

@app.route('/admin/questions/<int:qid>/edit',methods=['GET','POST'])
@login_required('admin')
def edit_question(qid):
    q=Question.query.get_or_404(qid)
    if request.method=='POST':
        if not valid_question_form(request.form): flash('सभी आवश्यक फ़ील्ड सही भरें।','error')
        else:
            for key in ('subject','chapter','question','a','b','c','d','explanation'): setattr(q,key,request.form.get(key,'').strip())
            q.class_no=int(request.form['class_no']); q.answer=request.form['answer'].upper(); q.published=request.form.get('published')=='on'; db.session.commit()
            flash('प्रश्न अपडेट हो गया।','success'); return redirect(url_for('admin'))
    return render_template('question_form.html',q=q)

@app.route('/admin/questions')
@login_required('admin')
def question_bank():
    query=Question.query
    cn=request.args.get('class_no',''); sub=request.args.get('subject',''); status=request.args.get('status','')
    if cn.isdigit() and int(cn) in CLASSES: query=query.filter_by(class_no=int(cn))
    if sub in SUBJECTS: query=query.filter_by(subject=sub)
    if status=='live': query=query.filter_by(published=True)
    elif status=='draft': query=query.filter_by(published=False)
    page=max(1,request.args.get('page',1,type=int)); pagination=query.order_by(Question.id.desc()).paginate(page=page,per_page=30,error_out=False)
    return render_template('question_bank.html',pagination=pagination,filter_class=cn,filter_subject=sub,status=status)

@app.route('/admin/questions/<int:qid>/toggle',methods=['POST'])
@login_required('admin')
def toggle_question(qid):
    q=Question.query.get_or_404(qid); q.published=not q.published; db.session.commit(); flash('प्रश्न की ऑनलाइन स्थिति बदल दी गई।','success'); return redirect(request.referrer or url_for('admin'))
@app.route('/admin/questions/<int:qid>/delete',methods=['POST'])
@login_required('admin')
def delete_question(qid):
    q=Question.query.get_or_404(qid); db.session.delete(q); db.session.commit(); flash('प्रश्न हटाया गया।','success'); return redirect(request.referrer or url_for('admin'))

def normalize_import_row(row):
    aliases = {'id':['id','question_id'],'class_no':['class_no','class'],'subject':['subject'],'chapter':['chapter'],'question':['question'],'a':['a','option_a'],'b':['b','option_b'],'c':['c','option_c'],'d':['d','option_d'],'answer':['answer','correct_answer'],'explanation':['explanation']}
    lowered={str(k).strip().lower():('' if v is None else str(v).strip()) for k,v in (row or {}).items() if k is not None}
    return {key: next((lowered[n] for n in names if n in lowered), '') for key,names in aliases.items()}

def read_bulk_rows(file_storage):
    filename=(file_storage.filename or '').lower(); raw=file_storage.read()
    if filename.endswith('.csv'):
        return [normalize_import_row(r) for r in csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))]
    if filename.endswith('.xlsx'):
        from openpyxl import load_workbook
        wb=load_workbook(io.BytesIO(raw),read_only=True,data_only=True); values=wb.active.iter_rows(values_only=True); headers=next(values,None)
        if not headers: raise ValueError('Excel शीट खाली है।')
        keys=[str(x).strip() if x is not None else '' for x in headers]
        return [normalize_import_row(dict(zip(keys,row))) for row in values if any(v is not None for v in row)]
    if filename.endswith('.docx'):
        from docx import Document
        doc=Document(io.BytesIO(raw)); rows=[]
        if not doc.tables: raise ValueError('Word में प्रश्नों की Table नहीं मिली। दिए गए Word Template का उपयोग करें।')
        for table in doc.tables:
            data=[[cell.text.strip() for cell in row.cells] for row in table.rows]
            if data: rows.extend(normalize_import_row(dict(zip(data[0],r))) for r in data[1:] if any(r))
        return rows
    if filename.endswith('.pdf'):
        import pdfplumber
        rows=[]
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    if table and len(table)>1: rows.extend(normalize_import_row(dict(zip(table[0],r))) for r in table[1:] if r and any(r))
        if not rows: raise ValueError('PDF में पढ़ने योग्य Table नहीं मिली। Scanned/image PDF समर्थित नहीं है; PDF Template की तरह table बनाएं।')
        return rows
    raise ValueError('केवल CSV, XLSX, DOCX और PDF फाइलें स्वीकार हैं।')

@app.route('/admin/import',methods=['GET','POST'])
@login_required('admin')
def import_csv():
    if request.method=='POST':
        f=request.files.get('file')
        if not f or not f.filename: flash('कृपया फाइल चुनें।','error'); return redirect(url_for('import_csv'))
        try:
            rows=read_bulk_rows(f); added=updated=skipped=0; publish_new=request.form.get('publish_new')=='on'
            for r in rows:
                cn=str(r.get('class_no','')).strip(); ans=str(r.get('answer','')).strip().upper(); sub=str(r.get('subject','')).strip()
                if cn not in [str(x) for x in CLASSES] or sub not in SUBJECTS or ans not in ['A','B','C','D'] or not all(str(r.get(k,'')).strip() for k in ['question','a','b','c','d']): skipped+=1; continue
                data=dict(class_no=int(cn),subject=sub,chapter=str(r.get('chapter',''))[:160],question=r['question'].strip(),a=r['a'].strip(),b=r['b'].strip(),c=r['c'].strip(),d=r['d'].strip(),answer=ans,explanation=str(r.get('explanation','')).strip())
                raw_id=str(r.get('id','')).strip()
                if raw_id:
                    if not raw_id.isdigit(): skipped+=1; continue
                    existing=db.session.get(Question,int(raw_id))
                    if not existing: skipped+=1; continue
                    for key,value in data.items(): setattr(existing,key,value)
                    updated+=1
                else:
                    db.session.add(Question(**data,published=publish_new)); added+=1
                if (added+updated)%300==0: db.session.flush()
            db.session.commit(); flash(f'इम्पोर्ट पूरा: {added} नए प्रश्न, {updated} अपडेट, {skipped} छोड़ी गई पंक्तियाँ। नए प्रश्न {"Published" if publish_new else "Draft"} में सेव हुए।','success')
        except Exception as e: db.session.rollback(); flash('फाइल पढ़ने में समस्या: '+str(e),'error')
        return redirect(url_for('question_bank'))
    return render_template('import.html')

@app.route('/admin/template/<fmt>')
@login_required('admin')
def bulk_template(fmt):
    headers=['id','class_no','subject','chapter','question','a','b','c','d','answer','explanation']
    sample=['','7','General Knowledge','सामान्य ज्ञान','भारत की राजधानी क्या है?','पटना','नई दिल्ली','मुंबई','कोलकाता','B','भारत की राजधानी नई दिल्ली है।']
    if fmt=='xlsx':
        from openpyxl import Workbook
        wb=Workbook(); ws=wb.active; ws.title='Questions'; ws.append(headers); ws.append(sample); bio=io.BytesIO(); wb.save(bio); bio.seek(0)
        return make_response((bio.getvalue(),200,{'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','Content-Disposition':'attachment; filename=avi_class_tr04_questions_template.xlsx'}))
    if fmt=='docx':
        from docx import Document
        doc=Document(); doc.add_heading('AVI CLASS TR-04 Question Import Template',0); doc.add_paragraph('पहली row में headers रखें। हर अगले row में एक प्रश्न। ID खाली = नया प्रश्न; मौजूदा ID = अपडेट।')
        table=doc.add_table(rows=1,cols=len(headers)); table.style='Table Grid'
        for cell,value in zip(table.rows[0].cells,headers): cell.text=value
        for value,cell in zip(sample,table.add_row().cells): cell.text=value
        bio=io.BytesIO(); doc.save(bio); bio.seek(0)
        return make_response((bio.getvalue(),200,{'Content-Type':'application/vnd.openxmlformats-officedocument.wordprocessingml.document','Content-Disposition':'attachment; filename=avi_class_tr04_questions_template.docx'}))
    if fmt=='pdf':
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape,A3
        from reportlab.platypus import SimpleDocTemplate,Table,TableStyle,Paragraph
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        bio=io.BytesIO(); doc=SimpleDocTemplate(bio,pagesize=landscape(A3),rightMargin=8*mm,leftMargin=8*mm,topMargin=10*mm,bottomMargin=10*mm); styles=getSampleStyleSheet()
        data=[[Paragraph(str(x),styles['BodyText']) for x in headers],[Paragraph(str(x),styles['BodyText']) for x in sample]]
        table=Table(data,repeatRows=1,colWidths=[12*mm,15*mm,25*mm,25*mm,55*mm,30*mm,30*mm,30*mm,30*mm,15*mm,55*mm]); table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#dbeafe')),('GRID',(0,0),(-1,-1),0.4,colors.grey),('VALIGN',(0,0),(-1,-1),'TOP'),('FONTSIZE',(0,0),(-1,-1),6),('PADDING',(0,0),(-1,-1),4)]))
        doc.build([Paragraph('AVI CLASS TR-04 Question Import Template',styles['Title']),Paragraph('Table में प्रश्न भरें। Scanned/image PDF नहीं; ID खाली = नया प्रश्न, मौजूदा ID = अपडेट।',styles['BodyText']),table]); bio.seek(0)
        return make_response((bio.getvalue(),200,{'Content-Type':'application/pdf','Content-Disposition':'attachment; filename=avi_class_tr04_questions_template.pdf'}))
    abort(404)

@app.route('/student')
@login_required('student')
def student():
    subjects=db.session.query(Question.subject,func.count(Question.id).label('n')).filter_by(published=True).group_by(Question.subject).order_by(Question.subject).all()
    attempts=Attempt.query.filter_by(user_id=session['user_id']).order_by(Attempt.created_at.desc()).limit(10).all()
    enrolled=CourseEnrollment.query.filter_by(user_id=session['user_id']).all()
    enrolled_ids={e.item_id for e in enrolled}
    courses=LearningItem.query.filter_by(category='courses',published=True).order_by(LearningItem.created_at.desc()).all()
    return render_template('student.html',subjects=subjects,attempts=attempts,courses=courses,enrolled_ids=enrolled_ids)

@app.route('/learning')
@login_required('student')
def learning_hub():
    counts={r[0]:r[1] for r in db.session.query(LearningItem.category,func.count(LearningItem.id)).filter_by(published=True).group_by(LearningItem.category).all()}
    return render_template('learning_hub.html',counts=counts)

@app.route('/learning/<category>')
@login_required('student')
def learning_category(category):
    if category not in CONTENT_CATEGORIES: abort(404)
    items=LearningItem.query.filter_by(category=category,published=True).order_by(LearningItem.created_at.desc()).all()
    enrolled_ids={e.item_id for e in CourseEnrollment.query.filter_by(user_id=session['user_id']).all()}
    return render_template('learning_category.html',category=category,category_title=CONTENT_CATEGORIES[category],items=items,enrolled_ids=enrolled_ids)

@app.route('/learning/item/<int:item_id>')
@login_required('student')
def learning_item(item_id):
    item=LearningItem.query.filter_by(id=item_id,published=True).first_or_404()
    enrolled=CourseEnrollment.query.filter_by(user_id=session['user_id'],item_id=item.id).first()
    return render_template('learning_item.html',item=item,enrolled=bool(enrolled))

@app.route('/learning/enroll/<int:item_id>',methods=['POST'])
@login_required('student')
def enroll_course(item_id):
    item=LearningItem.query.filter_by(id=item_id,category='courses',published=True).first_or_404()
    if not CourseEnrollment.query.filter_by(user_id=session['user_id'],item_id=item.id).first():
        db.session.add(CourseEnrollment(user_id=session['user_id'],item_id=item.id)); db.session.commit()
        flash('कोर्स आपके My Courses में जोड़ दिया गया।','success')
    return redirect(url_for('learning_item',item_id=item.id))

@app.route('/learning/file/<int:item_id>')
@login_required('student')
def learning_file(item_id):
    item=LearningItem.query.filter_by(id=item_id,published=True).first_or_404()
    if not item.stored_name: abort(404)
    if item.category=='courses' and not CourseEnrollment.query.filter_by(user_id=session['user_id'],item_id=item.id).first(): abort(403)
    return send_from_directory(UPLOAD_DIR,item.stored_name,as_attachment=True,download_name=item.file_name or item.stored_name)

@app.route('/practice')
@login_required('student')
def practice():
    cn=request.args.get('class_no',str(session.get('class_no'))); subject=request.args.get('subject','')
    if not cn.isdigit() or int(cn) not in CLASSES: abort(404)
    if subject not in SUBJECTS: abort(404)
    chapter=request.args.get('chapter','').strip()
    query=Question.query.filter_by(published=True,class_no=int(cn),subject=subject)
    if chapter: query=query.filter_by(chapter=chapter)
    questions=query.order_by(func.random()).limit(20).all()
    return render_template('practice.html',questions=questions,subject=subject,minutes=20,class_no=int(cn))

@app.route('/check',methods=['POST'])
@login_required('student')
def check_answers():
    ids=request.form.getlist('qid'); ids=list(dict.fromkeys(ids))[:20]; results=[]; score=0; subject='Practice'
    for raw in ids:
        if not raw.isdigit(): continue
        q=Question.query.filter_by(id=int(raw),published=True).first()
        if q:
            selected=request.form.get('q'+raw,''); correct=selected==q.answer; score+=int(correct); subject=q.subject; results.append((q,selected,correct))
    if results:
        db.session.add(Attempt(user_id=session['user_id'],class_no=results[0][0].class_no,subject=subject,score=score,total=len(results))); db.session.commit()
    return render_template('result.html',score=score,total=len(results),results=results)

@app.route('/admin/content',methods=['GET','POST'])
@login_required('admin')
def admin_content():
    if request.method=='POST':
        category=request.form.get('category','')
        title=request.form.get('title','').strip()
        if category not in CONTENT_CATEGORIES or not title:
            flash('सही Category और Title भरें।','error'); return redirect(url_for('admin_content'))
        upload=request.files.get('file'); stored=''; original=''
        if upload and upload.filename:
            original=secure_filename(upload.filename)
            ext=os.path.splitext(original)[1].lower()
            if ext not in ALLOWED_UPLOADS:
                flash('यह फाइल प्रकार समर्थित नहीं है। PDF, Office, ZIP, image, MP4 या MP3 चुनें।','error'); return redirect(url_for('admin_content'))
            stored=secrets.token_hex(16)+ext
            upload.save(os.path.join(UPLOAD_DIR,stored))
        url=request.form.get('external_url','').strip()
        if url and not urlparse(url).scheme in ('http','https'):
            flash('लिंक http:// या https:// से शुरू होना चाहिए।','error'); return redirect(url_for('admin_content'))
        raw_class=request.form.get('class_no','')
        class_no=int(raw_class) if raw_class.isdigit() and int(raw_class) in CLASSES else None
        item=LearningItem(category=category,title=title,description=request.form.get('description','').strip(),class_no=class_no,subject=request.form.get('subject','').strip(),file_name=original,stored_name=stored,external_url=url,schedule_text=request.form.get('schedule_text','').strip(),price=request.form.get('price','Free').strip() or 'Free',published=request.form.get('published')=='on')
        db.session.add(item); db.session.commit(); flash('Learning content सेव हो गया।','success'); return redirect(url_for('admin_content'))
    items=LearningItem.query.order_by(LearningItem.created_at.desc()).limit(200).all()
    return render_template('admin_content.html',items=items)

@app.route('/admin/content/<int:item_id>/delete',methods=['POST'])
@login_required('admin')
def delete_content(item_id):
    item=LearningItem.query.get_or_404(item_id)
    if item.stored_name:
        try: os.remove(os.path.join(UPLOAD_DIR,item.stored_name))
        except OSError: pass
    CourseEnrollment.query.filter_by(item_id=item.id).delete()
    db.session.delete(item); db.session.commit(); flash('Content हटा दिया गया।','success')
    return redirect(url_for('admin_content'))

@app.route('/admin/students')
@login_required('admin')
def students_list():
    rows=User.query.filter_by(role='student').order_by(User.created_at.desc()).limit(500).all(); return render_template('students.html',rows=rows)
@app.route('/admin/change-password',methods=['GET','POST'])
@login_required()
def change_password():
    user=db.session.get(User,session['user_id'])
    if request.method=='POST':
        old=request.form.get('old_password',''); new=request.form.get('new_password','')
        if not check_password_hash(user.password,old): flash('पुराना पासवर्ड सही नहीं है।','error')
        elif len(new)<10: flash('नया पासवर्ड कम से कम 10 अक्षरों का रखें।','error')
        else: user.password=generate_password_hash(new); db.session.commit(); flash('पासवर्ड बदल गया।','success'); return redirect(url_for('admin' if user.role=='admin' else 'student'))
    return render_template('change_password.html')


@app.route('/about')
def about_page():
    return render_template('about.html', title='About | AVI CLASSES')

@app.route('/help')
def help_page():
    return render_template('help.html', title='Help & Support | AVI CLASSES')

@app.route('/notifications')
def notifications_page():
    items = LearningItem.query.filter_by(published=True).order_by(LearningItem.created_at.desc()).limit(30).all()
    return render_template('notifications.html', items=items, title='Notifications | AVI CLASSES')

@app.route('/manifest.webmanifest')
def manifest(): return jsonify(name='AVI CLASSES',short_name='AVI CLASSES',start_url='/',display='standalone',background_color='#f6f8ff',theme_color='#071d68',icons=[{'src':'/static/icon.svg','sizes':'any','type':'image/svg+xml','purpose':'any maskable'}])
@app.route('/service-worker.js')
def service_worker():
    resp=make_response("const CACHE='avi-classes-premium-v2'; self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/','/static/style.css','/static/icon.svg'])))); self.addEventListener('activate',e=>e.waitUntil(self.clients.claim())); self.addEventListener('fetch',e=>{if(e.request.method==='GET' && new URL(e.request.url).origin===location.origin){e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));}});"); resp.headers['Content-Type']='application/javascript'; return resp
@app.route('/health')
def health(): return {'status':'ok','app':'AVI CLASS TR-04','database':'configured'}

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)
