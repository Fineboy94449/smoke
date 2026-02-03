import os
import json
import hashlib
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from datetime import datetime, timezone, timedelta
from functools import wraps

# Initialize Firebase
if os.path.exists("serviceAccountKey.json"):
    cred = credentials.Certificate("serviceAccountKey.json")
elif os.environ.get("FIREBASE_CRED"):
    cred = credentials.Certificate(json.loads(os.environ["FIREBASE_CRED"]))
else:
    cred = credentials.Default()

firebase_admin.initialize_app(cred)
db = firestore.client()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "smoketrack-secret-key-change-in-prod")

# Business Constants
BUNDLE_COST = 145.00
STICKS_PER_BUNDLE = 200
COST_PER_STICK = BUNDLE_COST / STICKS_PER_BUNDLE  # R0.725

# Session timeout — 30 minutes
SESSION_TIMEOUT = timedelta(minutes=30)

# ---------- AUTH HELPERS ----------

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        # Check session timeout
        last_active = session.get('last_active')
        if last_active and datetime.now() > datetime.fromisoformat(last_active) + SESSION_TIMEOUT:
            session.clear()
            return redirect(url_for('login'))
        session['last_active'] = datetime.now().isoformat()
        return f(*args, **kwargs)
    return decorated

# ---------- AUTH ROUTES ----------

@app.route('/login', methods=['GET'])
def login():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def do_login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')

    # Fetch user from Firestore
    doc = db.collection('users').document(username).get()
    if not doc.exists:
        return jsonify({"status": "error", "message": "Invalid username or password"})

    user = doc.to_dict()
    if user['password'] != hash_password(password):
        return jsonify({"status": "error", "message": "Invalid username or password"})

    session['user'] = username
    session['role'] = user.get('role', 'seller')
    session['last_active'] = datetime.now().isoformat()
    return jsonify({"status": "success"})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ---------- SETUP FIRST ADMIN (run once) ----------

@app.route('/setup-admin', methods=['POST'])
def setup_admin():
    # Only allow if no users exist
    users = db.collection('users').get()
    if len(users) > 0:
        return jsonify({"status": "error", "message": "Users already exist. Use login."})

    data = request.json
    username = data.get('username', 'admin').strip()
    password = data.get('password', '')

    if len(password) < 4:
        return jsonify({"status": "error", "message": "Password must be at least 4 characters"})

    db.collection('users').document(username).set({
        'password': hash_password(password),
        'role': 'admin',
        'created': datetime.now(timezone.utc)
    })

    return jsonify({"status": "success", "message": "Admin created. You can now log in."})

# ---------- DASHBOARD ----------

@app.route('/')
@login_required
def dashboard():
    sales = list(db.collection('sales').stream())
    debtors = list(db.collection('debtors').stream())

    cash_total = 0
    credit_total = 0
    total_sticks_sold = 0
    today = datetime.now(timezone.utc).date()
    daily_sales = 0

    # --- Chart data structures ---
    # Line chart: last 30 days
    last_30 = {}
    for i in range(30):
        d = (datetime.now(timezone.utc) - timedelta(days=i)).date()
        last_30[d.isoformat()] = {'cash': 0, 'credit': 0, 'sticks': 0}

    # Pie chart counters
    loose_total = 0
    pack_total = 0
    cash_pie = 0
    credit_pie = 0

    for s in sales:
        data = s.to_dict()
        price = data.get('price', 0)
        qty = data.get('qty', 0)
        method = data.get('method', 'cash')
        item_type = data.get('item_type', 'pack')

        if method == 'cash':
            cash_total += price
        else:
            credit_total += price
        total_sticks_sold += qty

        # Today's sales
        ts = data.get('timestamp')
        if ts:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts.date() == today:
                daily_sales += price

            # Line chart data (last 30 days)
            sale_date = ts.date().isoformat()
            if sale_date in last_30:
                last_30[sale_date]['cash' if method == 'cash' else 'credit'] += price
                last_30[sale_date]['sticks'] += qty

        # Pie: loose vs pack
        if item_type == 'loose':
            loose_total += price
        else:
            pack_total += price

        # Pie: cash vs credit
        if method == 'cash':
            cash_pie += price
        else:
            credit_pie += price

    # Profit calculation
    total_cost = total_sticks_sold * COST_PER_STICK
    net_profit = (cash_total + credit_total) - total_cost

    # Risk
    if cash_total > 0:
        credit_ratio = credit_total / cash_total
        risk_level = "HIGH" if credit_ratio > 0.6 else "MEDIUM" if credit_ratio > 0.3 else "SAFE"
    else:
        risk_level = "HIGH" if credit_total > 0 else "SAFE"

    # Debtor list
    debtor_list = []
    for d in debtors:
        dd = d.to_dict()
        dd['name'] = d.id
        debtor_list.append(dd)
    debtor_list.sort(key=lambda x: x['balance'], reverse=True)

    # Format line chart: sorted by date ascending
    line_labels = sorted(last_30.keys())
    line_cash = [last_30[d]['cash'] for d in line_labels]
    line_credit = [last_30[d]['credit'] for d in line_labels]
    line_sticks = [last_30[d]['sticks'] for d in line_labels]
    # Shorten labels to "DD MMM"
    line_labels_display = [datetime.fromisoformat(d).strftime('%d %b') for d in line_labels]

    # Profit vs Cost pie
    total_revenue = cash_total + credit_total
    profit_pie = max(0, total_revenue - total_cost)
    cost_pie = total_cost

    return render_template('dashboard.html',
                           cash=round(cash_total, 2),
                           credit=round(credit_total, 2),
                           profit=round(net_profit, 2),
                           daily=round(daily_sales, 2),
                           risk=risk_level,
                           debtors=debtor_list,
                           sticks_sold=total_sticks_sold,
                           # Chart data
                           line_labels=line_labels_display,
                           line_cash=line_cash,
                           line_credit=line_credit,
                           line_sticks=line_sticks,
                           loose_total=round(loose_total, 2),
                           pack_total=round(pack_total, 2),
                           cash_pie=round(cash_pie, 2),
                           credit_pie=round(credit_pie, 2),
                           profit_pie=round(profit_pie, 2),
                           cost_pie=round(cost_pie, 2),
                           user=session.get('user'),
                           role=session.get('role'))

# ---------- SELL ----------

@app.route('/sell', methods=['POST'])
@login_required
def process_sale():
    data = request.json
    item = data['item']
    method = data['method']
    qty = int(data.get('qty', 1))

    if item == 'loose':
        price = 1.50 * qty
    else:
        unit_price = 40.00 if method == 'credit' else 30.00
        price = unit_price * qty

    customer_name = data.get('name', 'Cash Customer')

    db.collection('sales').add({
        'qty': qty if item == 'loose' else qty * 20,
        'price': price,
        'method': method,
        'customer': customer_name,
        'timestamp': datetime.now(timezone.utc),
        'item_type': item
    })

    if method == 'credit':
        debtor_ref = db.collection('debtors').document(customer_name)
        doc = debtor_ref.get()
        if doc.exists:
            debtor_ref.update({
                'balance': firestore.Increment(price),
                'last_purchase': datetime.now(timezone.utc)
            })
        else:
            debtor_ref.set({
                'balance': price,
                'trust_score': 50,
                'created': datetime.now(timezone.utc),
                'last_purchase': datetime.now(timezone.utc)
            })

    sticks = qty if item == 'loose' else qty * 20
    profit_made = price - (sticks * COST_PER_STICK)

    return jsonify({
        "status": "success",
        "profit_made": round(profit_made, 2),
        "price": round(price, 2)
    })

# ---------- PAYMENT ----------

@app.route('/payment', methods=['POST'])
@login_required
def record_payment():
    data = request.json
    name = data['name']
    amount = float(data['amount'])

    debtor_ref = db.collection('debtors').document(name)
    doc = debtor_ref.get()

    if not doc.exists:
        return jsonify({"status": "error", "message": "Debtor not found"}), 404

    current_balance = doc.to_dict()['balance']
    new_balance = max(0, current_balance - amount)

    if new_balance == 0:
        debtor_ref.delete()
    else:
        debtor_ref.update({
            'balance': new_balance,
            'last_payment': datetime.now(timezone.utc)
        })

    db.collection('payments').add({
        'customer': name,
        'amount': amount,
        'timestamp': datetime.now(timezone.utc),
        'previous_balance': current_balance,
        'new_balance': new_balance
    })

    return jsonify({
        "status": "success",
        "new_balance": round(new_balance, 2),
        "paid_in_full": new_balance == 0
    })

# ---------- DEBTORS PAGE ----------

@app.route('/debtors')
@login_required
def view_debtors():
    debtors = db.collection('debtors').stream()
    debtor_list = []
    total_owed = 0
    for d in debtors:
        data = d.to_dict()
        data['name'] = d.id
        debtor_list.append(data)
        total_owed += data['balance']
    debtor_list.sort(key=lambda x: x['balance'], reverse=True)
    return render_template('debtors.html',
                           debtors=debtor_list,
                           total_owed=round(total_owed, 2),
                           user=session.get('user'),
                           role=session.get('role'))

# ---------- HISTORY ----------

@app.route('/history')
@login_required
def view_history():
    sales = db.collection('sales').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(50).stream()
    sales_list = []
    for s in sales:
        data = s.to_dict()
        data['id'] = s.id
        sales_list.append(data)
    return render_template('history.html',
                           sales=sales_list,
                           user=session.get('user'),
                           role=session.get('role'))

# ---------- STOCK ----------

@app.route('/stock')
@login_required
def view_stock():
    stock_docs = db.collection('stock').order_by('date', direction=firestore.Query.DESCENDING).stream()
    stock_list = []
    total_bundles = 0
    total_spent = 0
    total_sticks_from_stock = 0

    for doc in stock_docs:
        data = doc.to_dict()
        data['id'] = doc.id
        stock_list.append(data)
        total_bundles += data['bundles']
        total_spent += data['cost']
        total_sticks_from_stock += data['bundles'] * STICKS_PER_BUNDLE

    monthly = {}
    for s in stock_list:
        month_key = s['date'].strftime('%B %Y')
        if month_key not in monthly:
            monthly[month_key] = {'bundles': 0, 'cost': 0.0, 'entries': []}
        monthly[month_key]['bundles'] += s['bundles']
        monthly[month_key]['cost'] += s['cost']
        monthly[month_key]['entries'].append(s)

    sales = db.collection('sales').stream()
    total_sticks_sold = sum(s.to_dict()['qty'] for s in sales)
    sticks_remaining = total_sticks_from_stock - total_sticks_sold

    return render_template('stock.html',
                           stock_list=stock_list,
                           monthly=monthly,
                           total_bundles=total_bundles,
                           total_spent=total_spent,
                           total_sticks_from_stock=total_sticks_from_stock,
                           total_sticks_sold=total_sticks_sold,
                           sticks_remaining=sticks_remaining,
                           bundle_cost=BUNDLE_COST,
                           sticks_per_bundle=STICKS_PER_BUNDLE,
                           user=session.get('user'),
                           role=session.get('role'))

@app.route('/stock/add', methods=['POST'])
@login_required
def add_stock():
    data = request.json
    bundles = int(data['bundles'])
    cost = bundles * BUNDLE_COST
    if data.get('date'):
        purchase_date = datetime.strptime(data['date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
    else:
        purchase_date = datetime.now(timezone.utc)

    db.collection('stock').add({
        'bundles': bundles,
        'sticks': bundles * STICKS_PER_BUNDLE,
        'cost': cost,
        'date': purchase_date,
        'note': data.get('note', '')
    })

    return jsonify({
        "status": "success",
        "bundles": bundles,
        "sticks": bundles * STICKS_PER_BUNDLE,
        "cost": cost
    })

@app.route('/stock/delete', methods=['POST'])
@login_required
def delete_stock():
    data = request.json
    try:
        db.collection('stock').document(data['stock_id']).delete()
        return jsonify({"status": "success", "message": "Stock entry deleted"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ---------- RECENT TRANSACTIONS ----------

@app.route('/recent-transactions')
@login_required
def recent_transactions():
    try:
        sales = db.collection('sales').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(10).stream()
        transactions = []
        now = datetime.now(timezone.utc)

        for s in sales:
            data = s.to_dict()
            if 'price' not in data or 'qty' not in data:
                continue

            ts = data.get('timestamp')
            if ts is None:
                time_ago = "Unknown"
            else:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                total_seconds = int((now - ts).total_seconds())
                if total_seconds < 60:
                    time_ago = "Just now"
                elif total_seconds < 3600:
                    time_ago = f"{total_seconds // 60} min ago"
                elif total_seconds < 86400:
                    h = total_seconds // 3600
                    time_ago = f"{h} hour{'s' if h > 1 else ''} ago"
                else:
                    d = (now - ts).days
                    time_ago = f"{d} day{'s' if d > 1 else ''} ago"

            profit = data['price'] - (data['qty'] * COST_PER_STICK)
            transactions.append({
                'id': s.id,
                'customer': data.get('customer', 'Unknown'),
                'method': data.get('method', 'cash'),
                'item_type': data.get('item_type', 'pack'),
                'price': data['price'],
                'qty': data['qty'],
                'profit': round(profit, 2),
                'time_ago': time_ago
            })

        return jsonify({'transactions': transactions})
    except Exception as e:
        print(f"ERROR in recent_transactions: {e}")
        return jsonify({'transactions': [], 'error': str(e)}), 500

# ---------- DELETE TRANSACTION ----------

@app.route('/delete-transaction', methods=['POST'])
@login_required
def delete_transaction():
    data = request.json
    try:
        doc = db.collection('sales').document(data['transaction_id']).get()
        if not doc.exists:
            return jsonify({"status": "error", "message": "Transaction not found"}), 404

        trans_data = doc.to_dict()

        if trans_data['method'] == 'credit':
            customer_name = trans_data['customer']
            amount = trans_data['price']
            debtor_ref = db.collection('debtors').document(customer_name)
            debtor_doc = debtor_ref.get()
            if debtor_doc.exists:
                new_balance = max(0, debtor_doc.to_dict()['balance'] - amount)
                if new_balance == 0:
                    debtor_ref.delete()
                else:
                    debtor_ref.update({'balance': new_balance})

        db.collection('sales').document(data['transaction_id']).delete()
        return jsonify({"status": "success", "message": "Transaction deleted"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)