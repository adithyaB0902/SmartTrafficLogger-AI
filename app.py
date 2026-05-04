import os
import csv
import io
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import qrcode

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///traffic.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

VIOLATION_TYPES = [
    "Signal Jump",
    "Speeding",
    "No Helmet",
    "No Seatbelt",
    "Wrong Way Driving",
    "Drunk Driving",
    "No License",
    "Illegal Parking",
    "Mobile Phone Use",
    "Overloading",
    "No Insurance",
    "Other"
]

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True)
    password = db.Column(db.String(200))

class Violation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    vehicle_number = db.Column(db.String(50), nullable=False)
    violation_type = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(100), nullable=False)
    date = db.Column(db.Date, nullable=False)
    fine_amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default="Unpaid")
    notes = db.Column(db.Text, default="")
    qr_code = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def date_str(self):
        return self.date.strftime('%d %b %Y') if self.date else ''

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

@app.route('/')
def home():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and check_password_hash(user.password, request.form['password']):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash("Invalid username or password.", "error")
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    total = Violation.query.count()
    unpaid = Violation.query.filter_by(status='Unpaid').count()
    paid = Violation.query.filter_by(status='Paid').count()
    total_fines = db.session.query(db.func.sum(Violation.fine_amount)).scalar() or 0
    collected = db.session.query(db.func.sum(Violation.fine_amount)).filter_by(status='Paid').scalar() or 0
    pending_amount = db.session.query(db.func.sum(Violation.fine_amount)).filter_by(status='Unpaid').scalar() or 0

    # Top violation types
    from sqlalchemy import func
    top_types = db.session.query(
        Violation.violation_type,
        func.count(Violation.id).label('count')
    ).group_by(Violation.violation_type).order_by(func.count(Violation.id).desc()).limit(5).all()

    # Recent violations
    recent = Violation.query.order_by(Violation.created_at.desc()).limit(5).all()

    return render_template('dashboard.html',
        total=total, unpaid=unpaid, paid=paid,
        total_fines=total_fines, collected=collected,
        pending_amount=pending_amount,
        top_types=top_types, recent=recent
    )

@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_violation():
    if request.method == 'POST':
        try:
            fine = float(request.form['fine_amount'])
            if fine <= 0:
                flash("Fine amount must be greater than 0.", "error")
                return render_template('add_violation.html', violation_types=VIOLATION_TYPES)

            date_obj = datetime.strptime(request.form['date'], '%Y-%m-%d').date()

            violation = Violation(
                vehicle_number=request.form['vehicle_number'].strip().upper(),
                violation_type=request.form['violation_type'],
                location=request.form['location'].strip(),
                date=date_obj,
                fine_amount=fine,
                notes=request.form.get('notes', '').strip()
            )
            db.session.add(violation)
            db.session.commit()

            # Generate QR code
            qr_data = url_for('public_status', violation_id=violation.id, _external=True)
            img = qrcode.make(qr_data)
            qr_dir = os.path.join(app.root_path, 'static', 'qr_codes')
            os.makedirs(qr_dir, exist_ok=True)
            qr_filename = f'{violation.id}.png'
            img.save(os.path.join(qr_dir, qr_filename))
            violation.qr_code = qr_filename
            db.session.commit()

            flash(f"Violation added for {violation.vehicle_number}.", "success")
            return redirect(url_for('view_violations'))
        except ValueError as e:
            flash("Invalid date or amount format.", "error")

    return render_template('add_violation.html', violation_types=VIOLATION_TYPES)

@app.route('/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_violation(id):
    violation = db.session.get(Violation, id)
    if not violation:
        flash("Violation not found.", "error")
        return redirect(url_for('view_violations'))

    if request.method == 'POST':
        try:
            fine = float(request.form['fine_amount'])
            if fine <= 0:
                flash("Fine amount must be greater than 0.", "error")
                return render_template('edit_violation.html', v=violation, violation_types=VIOLATION_TYPES)

            violation.vehicle_number = request.form['vehicle_number'].strip().upper()
            violation.violation_type = request.form['violation_type']
            violation.location = request.form['location'].strip()
            violation.date = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
            violation.fine_amount = fine
            violation.notes = request.form.get('notes', '').strip()
            db.session.commit()
            flash("Violation updated successfully.", "success")
            return redirect(url_for('view_violations'))
        except ValueError:
            flash("Invalid data.", "error")

    return render_template('edit_violation.html', v=violation, violation_types=VIOLATION_TYPES)

@app.route('/violations')
@login_required
def view_violations():
    search = request.args.get('vehicle', '').strip()
    status_filter = request.args.get('status', '')
    type_filter = request.args.get('type', '')
    page = request.args.get('page', 1, type=int)

    query = Violation.query
    if search:
        query = query.filter(Violation.vehicle_number.ilike(f'%{search}%'))
    if status_filter:
        query = query.filter_by(status=status_filter)
    if type_filter:
        query = query.filter_by(violation_type=type_filter)

    violations = query.order_by(Violation.created_at.desc()).paginate(page=page, per_page=15, error_out=False)

    return render_template('view_violations.html',
        violations=violations, search=search,
        status_filter=status_filter, type_filter=type_filter,
        violation_types=VIOLATION_TYPES
    )

@app.route('/update/<int:id>', methods=['POST'])
@login_required
def update_status(id):
    violation = db.session.get(Violation, id)
    if violation:
        violation.status = "Paid"
        db.session.commit()
        flash(f"Marked as Paid: {violation.vehicle_number}", "success")
    return redirect(url_for('view_violations'))

@app.route('/delete/<int:id>', methods=['POST'])
@login_required
def delete_violation(id):
    violation = db.session.get(Violation, id)
    if violation:
        # Remove QR image
        if violation.qr_code:
            qr_path = os.path.join(app.root_path, 'static', 'qr_codes', violation.qr_code)
            if os.path.exists(qr_path):
                os.remove(qr_path)
        db.session.delete(violation)
        db.session.commit()
        flash("Violation deleted.", "success")
    return redirect(url_for('view_violations'))

@app.route('/export/csv')
@login_required
def export_csv():
    violations = Violation.query.order_by(Violation.date.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Vehicle Number', 'Violation Type', 'Location', 'Date', 'Fine Amount (₹)', 'Status', 'Notes', 'Created At'])
    for v in violations:
        writer.writerow([v.id, v.vehicle_number, v.violation_type, v.location,
                         v.date_str, v.fine_amount, v.status, v.notes,
                         v.created_at.strftime('%d %b %Y %H:%M') if v.created_at else ''])
    output.seek(0)
    return Response(
        output,
        mimetype='text/csv',
        headers={"Content-Disposition": "attachment;filename=violations_export.csv"}
    )

@app.route('/status/<int:violation_id>')
def public_status(violation_id):
    violation = db.session.get(Violation, violation_id)
    if not violation:
        return render_template('404.html'), 404
    return render_template('public_status.html', violation=violation)

@app.route('/api/stats')
@login_required
def api_stats():
    from sqlalchemy import func
    monthly = db.session.query(
        db.func.strftime('%Y-%m', Violation.created_at).label('month'),
        func.count(Violation.id).label('count'),
        func.sum(Violation.fine_amount).label('total')
    ).group_by('month').order_by('month').limit(12).all()
    return jsonify([{'month': m.month, 'count': m.count, 'total': float(m.total or 0)} for m in monthly])

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username="admin").first():
            admin = User(username="admin", password=generate_password_hash("admin123"))
            db.session.add(admin)
            db.session.commit()
    app.run(debug=True)
