import os
import json
import hashlib
import secrets
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
COST_PER_STICK = BUNDLE_COST / STICKS_PER_BUNDLE

# Credit Tiers
TIER_NEW = {'name': 'new', 'limit': 80, 'min_points': 0}
TIER_REGULAR = {'name': 'regular', 'limit': 100, 'min_points': 50}
TIER_TRUSTED = {'name': 'trusted', 'limit': 120, 'min_points': 100}

SESSION_TIMEOUT = timedelta(minutes=30)

# ---------- HELPERS ----------

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def calculate_tier(points):
    if points >= 100: return TIER_TRUSTED
    elif points >= 50: return TIER_REGULAR
    else: return TIER_NEW

def update_customer_tier(customer_ref, points):
    tier_info = calculate_tier(points)
    customer_ref.update({
        'loyalty_points': points,
        'tier': tier_info['name'],
        'credit_limit': tier_info['limit']
    })

def check_overdue_penalties():
    """Check all customers for 4-week overdue and 10% debt increase"""
    customers = db.collection('customers').where('approved', '==', True).stream()
    now = datetime.now(timezone.utc)
    
    for c in customers:
        data = c.to_dict()
        phone = c.id
        
        # Skip if no debt
        current_debt = data.get('current_debt', 0)
        if current_debt == 0:
            continue
            
        # Check 4-week overdue
        last_check = data.get('last_debt_check')
        if last_check:
            if last_check.tzinfo is None:
                last_check = last_check.replace(tzinfo=timezone.utc)
            weeks_overdue = (now - last_check).days / 7
            if weeks_overdue >= 4:
                # Deduct 2 points
                new_points = max(0, data.get('loyalty_points', 0) - 2)
                update_customer_tier(db.collection('customers').document(phone), new_points)
                # Log point change
                db.collection('point_history').add({
                    'customer_phone': phone,
                    'change': -2,
                    'reason': '4 weeks overdue',
                    'timestamp': now
                })
                # Reset check date
                db.collection('customers').document(phone).update({'last_debt_check': now})
        
        # Check 10% debt increase
        debt_at_check = data.get('debt_at_last_check', 0)
        if debt_at_check > 0:
            increase_pct = ((current_debt - debt_at_check) / debt_at_check) * 100
            if increase_pct >= 10:
                # Deduct 2 points
                new_points = max(0, data.get('loyalty_points', 0) - 2)
                update_customer_tier(db.collection('customers').document(phone), new_points)
                # Log point change
                db.collection('point_history').add({
                    'customer_phone': phone,
                    'change': -2,
                    'reason': f'Debt increased {increase_pct:.1f}%',
                    'timestamp': now
                })
                # Update baseline
                db.collection('customers').document(phone).update({'debt_at_last_check': current_debt})

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        last_active = session.get('last_active')
        if last_active and datetime.now() > datetime.fromisoformat(last_active) + SESSION_TIMEOUT:
            session.clear()
            return redirect(url_for('login'))
        session['last_active'] = datetime.now().isoformat()
        return f(*args, **kwargs)
    return decorated

def customer_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'customer_phone' not in session:
            return redirect(url_for('customer_login'))
        return f(*args, **kwargs)
    return decorated

# ---------- ADMIN AUTH ----------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if 'user' in session:
            return redirect(url_for('dashboard'))
        return render_template('login.html')
    
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    doc = db.collection('users').document(username).get()
    
    if not doc.exists or doc.to_dict()['password'] != hash_password(password):
        return jsonify({"status": "error", "message": "Invalid credentials"})
    
    session['user'] = username
    session['role'] = doc.to_dict().get('role', 'seller')
    session['last_active'] = datetime.now().isoformat()
    return jsonify({"status": "success"})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/setup-admin', methods=['POST'])
def setup_admin():
    users = list(db.collection('users').stream())
    if len(users) > 0:
        return jsonify({"status": "error", "message": "Users already exist"})
    
    data = request.json
    username = data.get('username', 'admin').strip()
    password = data.get('password', '')
    
    if len(password) < 4:
        return jsonify({"status": "error", "message": "Password must be 4+ characters"})
    
    db.collection('users').document(username).set({
        'password': hash_password(password),
        'role': 'admin',
        'created': datetime.now(timezone.utc)
    })
    return jsonify({"status": "success"})

# ---------- CUSTOMER AUTH ----------

@app.route('/customer/register', methods=['GET', 'POST'])
def customer_register():
    if request.method == 'GET':
        return render_template('customer_register.html')
    
    data = request.json
    name = data.get('name', '').strip()
    phone = data.get('phone', '').strip()
    house = data.get('house_number', '').strip()
    password = data.get('password', '')
    
    if not all([name, phone, house, password]) or len(password) < 4:
        return jsonify({"status": "error", "message": "All fields required, password 4+ chars"})
    
    # Check if phone exists
    if db.collection('customers').document(phone).get().exists:
        return jsonify({"status": "error", "message": "Phone number already registered"})
    
    db.collection('customers').document(phone).set({
        'name': name,
        'phone': phone,
        'house_number': house,
        'password_hash': hash_password(password),
        'approved': False,
        'credit_enabled': False,
        'credit_limit': 80,
        'loyalty_points': 0,
        'tier': 'new',
        'tier_override': False,
        'cash_on_hand': 0,
        'current_debt': 0,
        'last_debt_check': datetime.now(timezone.utc),
        'debt_at_last_check': 0,
        'created': datetime.now(timezone.utc)
    })
    return jsonify({"status": "success", "message": "Registration submitted. Wait for approval."})

@app.route('/customer/login', methods=['GET', 'POST'])
def customer_login():
    if request.method == 'GET':
        if 'customer_phone' in session:
            return redirect(url_for('customer_dashboard'))
        return render_template('customer_login.html')
    
    data = request.json
    phone = data.get('phone', '').strip()
    password = data.get('password', '')
    
    doc = db.collection('customers').document(phone).get()
    if not doc.exists:
        return jsonify({"status": "error", "message": "Invalid phone or password"})
    
    cust = doc.to_dict()
    if cust['password_hash'] != hash_password(password):
        return jsonify({"status": "error", "message": "Invalid phone or password"})
    
    if not cust.get('approved', False):
        return jsonify({"status": "error", "message": "Account pending approval"})
    
    session['customer_phone'] = phone
    session['customer_name'] = cust['name']
    return jsonify({"status": "success"})

@app.route('/customer/logout')
def customer_logout():
    session.pop('customer_phone', None)
    session.pop('customer_name', None)
    return redirect(url_for('customer_login'))

# ---------- CUSTOMER DASHBOARD ----------

@app.route('/customer/dashboard')
@customer_login_required
def customer_dashboard():
    phone = session['customer_phone']
    cust = db.collection('customers').document(phone).get().to_dict()
    
    # Get purchase history
    sales = db.collection('sales').where('customer', '==', cust['name']).order_by('timestamp', direction=firestore.Query.DESCENDING).limit(20).stream()
    purchases = [s.to_dict() for s in sales]
    
    # Get payment history
    payments = db.collection('payments').where('customer', '==', cust['name']).order_by('timestamp', direction=firestore.Query.DESCENDING).limit(20).stream()
    payment_list = [p.to_dict() for p in payments]
    
    # Get orders
    orders = db.collection('orders').where('customer_phone', '==', phone).order_by('timestamp', direction=firestore.Query.DESCENDING).limit(10).stream()
    order_list = []
    for o in orders:
        od = o.to_dict()
        od['id'] = o.id
        order_list.append(od)
    
    # Point history
    points_hist = db.collection('point_history').where('customer_phone', '==', phone).order_by('timestamp', direction=firestore.Query.DESCENDING).limit(10).stream()
    point_list = [p.to_dict() for p in points_hist]
    
    # Tier progress
    tier_info = calculate_tier(cust['loyalty_points'])
    next_tier = None
    points_to_next = 0
    if tier_info['name'] == 'new':
        next_tier = TIER_REGULAR
        points_to_next = TIER_REGULAR['min_points'] - cust['loyalty_points']
    elif tier_info['name'] == 'regular':
        next_tier = TIER_TRUSTED
        points_to_next = TIER_TRUSTED['min_points'] - cust['loyalty_points']
    
    return render_template('customer_dashboard.html',
                           customer=cust,
                           purchases=purchases,
                           payments=payment_list,
                           orders=order_list,
                           point_history=point_list,
                           next_tier=next_tier,
                           points_to_next=points_to_next)

@app.route('/customer/order', methods=['POST'])
@customer_login_required
def customer_create_order():
    phone = session['customer_phone']
    cust = db.collection('customers').document(phone).get().to_dict()
    
    data = request.json
    items = data.get('items', [])  # [{'type': 'loose', 'qty': 5}, {'type': 'pack', 'qty': 2}]
    payment_method = data.get('payment_method', 'cash')
    
    if not items:
        return jsonify({"status": "error", "message": "No items selected"})
    
    # Calculate total
    total = 0
    total_sticks = 0
    for item in items:
        if item['type'] == 'loose':
            total += 1.50 * item['qty']
            total_sticks += item['qty']
        else:  # pack
            price_per = 40 if payment_method == 'credit' else 30
            total += price_per * item['qty']
            total_sticks += item['qty'] * 20
    
    # Check credit limit if credit
    if payment_method == 'credit':
        if not cust.get('credit_enabled', False):
            return jsonify({"status": "error", "message": "Credit not enabled for your account"})
        
        available_credit = cust['credit_limit'] - cust['current_debt']
        if total > available_credit:
            return jsonify({"status": "error", "message": f"Exceeds credit limit. Available: R{available_credit:.2f}"})
    
    db.collection('orders').add({
        'customer_phone': phone,
        'customer_name': cust['name'],
        'house_number': cust['house_number'],
        'items': items,
        'total': total,
        'total_sticks': total_sticks,
        'payment_method': payment_method,
        'status': 'pending',
        'timestamp': datetime.now(timezone.utc)
    })
    
    return jsonify({"status": "success", "message": "Order submitted!"})

@app.route('/customer/update-cash', methods=['POST'])
@customer_login_required
def customer_update_cash():
    phone = session['customer_phone']
    data = request.json
    amount = float(data.get('amount', 0))
    
    db.collection('customers').document(phone).update({'cash_on_hand': amount})
    return jsonify({"status": "success"})

# ---------- ADMIN DASHBOARD ----------

@app.route('/')
@login_required
def dashboard():
    # Run penalty checks
    check_overdue_penalties()
    
    sales = list(db.collection('sales').stream())
    debtors = list(db.collection('debtors').stream())
    
    cash_total = 0
    credit_total = 0
    total_sticks_sold = 0
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    daily_sales = 0
    yesterday_sales = 0
    yesterday_profit = 0
    
    # Line chart last 30 days
    last_30 = {}
    for i in range(30):
        d = (datetime.now(timezone.utc) - timedelta(days=i)).date()
        last_30[d.isoformat()] = {'cash': 0, 'credit': 0, 'sticks': 0}
    
    # Pie counters
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
        
        ts = data.get('timestamp')
        if ts:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            sale_date = ts.date()
            
            if sale_date == today:
                daily_sales += price
            elif sale_date == yesterday:
                yesterday_sales += price
                yesterday_profit += price - (qty * COST_PER_STICK)
            
            sale_date_iso = sale_date.isoformat()
            if sale_date_iso in last_30:
                last_30[sale_date_iso]['cash' if method == 'cash' else 'credit'] += price
                last_30[sale_date_iso]['sticks'] += qty
        
        if item_type == 'loose':
            loose_total += price
        else:
            pack_total += price
        
        if method == 'cash':
            cash_pie += price
        else:
            credit_pie += price
    
    total_cost = total_sticks_sold * COST_PER_STICK
    net_profit = (cash_total + credit_total) - total_cost
    
    if cash_total > 0:
        credit_ratio = credit_total / cash_total
        risk_level = "HIGH" if credit_ratio > 0.6 else "MEDIUM" if credit_ratio > 0.3 else "SAFE"
    else:
        risk_level = "HIGH" if credit_total > 0 else "SAFE"
    
    debtor_list = []
    for d in debtors:
        dd = d.to_dict()
        dd['name'] = d.id
        debtor_list.append(dd)
    debtor_list.sort(key=lambda x: x['balance'], reverse=True)
    
    # Stock calculations
    stock_docs = list(db.collection('stock').stream())
    total_sticks_from_stock = sum(s.to_dict()['sticks'] for s in stock_docs)
    sticks_remaining = total_sticks_from_stock - total_sticks_sold
    
    # Stock alert level
    stock_alert = "out" if sticks_remaining <= 0 else "low" if sticks_remaining < 200 else "safe"
    
    # Goals
    goals_doc = db.collection('settings').document('goals').get()
    if goals_doc.exists:
        goals = goals_doc.to_dict()
    else:
        goals = {'daily': 500, 'monthly': 15000}
    
    # Calculate monthly sales
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_sales = sum(s.to_dict()['price'] for s in sales if s.to_dict().get('timestamp', datetime.now(timezone.utc)) >= month_start)
    
    # Cash flow forecast
    if daily_sales > 0 and sticks_remaining > 0:
        avg_sticks_per_day = total_sticks_sold / max(1, (datetime.now(timezone.utc) - month_start).days)
        days_until_out = int(sticks_remaining / max(1, avg_sticks_per_day))
        bundles_needed = max(1, int((avg_sticks_per_day * 7) / STICKS_PER_BUNDLE))
    else:
        days_until_out = 0
        bundles_needed = 1
    
    # Pending orders & customers
    pending_orders = db.collection('orders').where('status', '==', 'pending').stream()
    pending_order_count = len(list(pending_orders))
    
    pending_customers = db.collection('customers').where('approved', '==', False).stream()
    pending_customer_count = len(list(pending_customers))
    
    # Chart data
    line_labels = sorted(last_30.keys())
    line_cash = [last_30[d]['cash'] for d in line_labels]
    line_credit = [last_30[d]['credit'] for d in line_labels]
    line_sticks = [last_30[d]['sticks'] for d in line_labels]
    line_labels_display = [datetime.fromisoformat(d).strftime('%d %b') for d in line_labels]
    
    profit_pie = max(0, (cash_total + credit_total) - total_cost)
    cost_pie = total_cost
    
    return render_template('dashboard.html',
                           cash=round(cash_total, 2),
                           credit=round(credit_total, 2),
                           profit=round(net_profit, 2),
                           daily=round(daily_sales, 2),
                           risk=risk_level,
                           debtors=debtor_list,
                           sticks_sold=total_sticks_sold,
                           sticks_remaining=sticks_remaining,
                           stock_alert=stock_alert,
                           yesterday_sales=round(yesterday_sales, 2),
                           yesterday_profit=round(yesterday_profit, 2),
                           daily_goal=goals['daily'],
                           monthly_goal=goals['monthly'],
                           monthly_sales=round(monthly_sales, 2),
                           days_until_out=days_until_out,
                           bundles_needed=bundles_needed,
                           pending_orders=pending_order_count,
                           pending_customers=pending_customer_count,
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

@app.route('/sell', methods=['POST'])
@login_required
def process_sale():
    data = request.json
    item = data['item']
    method = data['method']
    qty = int(data.get('qty', 1))
    
    if item == 'loose':
        price = 1.50 * qty
        sticks = qty
    else:
        unit_price = 40.00 if method == 'credit' else 30.00
        price = unit_price * qty
        sticks = qty * 20
    
    customer_name = data.get('name', 'Cash Customer')
    
    db.collection('sales').add({
        'qty': sticks,
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
        
        # Update customer debt and points
        cust = db.collection('customers').where('name', '==', customer_name).limit(1).stream()
        for c in cust:
            phone = c.id
            cust_data = c.to_dict()
            new_debt = cust_data.get('current_debt', 0) + price
            new_points = cust_data.get('loyalty_points', 0) + (sticks // 20)
            
            db.collection('customers').document(phone).update({
                'current_debt': new_debt,
                'loyalty_points': new_points
            })
            
            # Log points
            if sticks >= 20:
                db.collection('point_history').add({
                    'customer_phone': phone,
                    'change': sticks // 20,
                    'reason': f'Purchase: {sticks} sticks',
                    'timestamp': datetime.now(timezone.utc)
                })
            
            update_customer_tier(db.collection('customers').document(phone), new_points)
    
    profit_made = price - (sticks * COST_PER_STICK)
    return jsonify({
        "status": "success",
        "profit_made": round(profit_made, 2),
        "price": round(price, 2)
    })

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
    
    # Update customer debt and give bonus points
    cust = db.collection('customers').where('name', '==', name).limit(1).stream()
    for c in cust:
        phone = c.id
        cust_data = c.to_dict()
        new_debt = max(0, cust_data.get('current_debt', 0) - amount)
        new_points = cust_data.get('loyalty_points', 0) + 5  # Bonus
        
        db.collection('customers').document(phone).update({
            'current_debt': new_debt,
            'loyalty_points': new_points,
            'last_debt_check': datetime.now(timezone.utc),
            'debt_at_last_check': new_debt
        })
        
        db.collection('point_history').add({
            'customer_phone': phone,
            'change': 5,
            'reason': f'Payment: R{amount:.2f}',
            'timestamp': datetime.now(timezone.utc)
        })
        
        update_customer_tier(db.collection('customers').document(phone), new_points)
    
    return jsonify({
        "status": "success",
        "new_balance": round(new_balance, 2),
        "paid_in_full": new_balance == 0
    })

@app.route('/update-goals', methods=['POST'])
@login_required
def update_goals():
    data = request.json
    db.collection('settings').document('goals').set({
        'daily': float(data.get('daily', 500)),
        'monthly': float(data.get('monthly', 15000))
    })
    return jsonify({"status": "success"})

# Orders management
@app.route('/orders')
@login_required
def view_orders():
    orders = db.collection('orders').order_by('timestamp', direction=firestore.Query.DESCENDING).stream()
    order_list = []
    for o in orders:
        od = o.to_dict()
        od['id'] = o.id
        order_list.append(od)
    
    return render_template('orders.html', orders=order_list, user=session.get('user'), role=session.get('role'))

@app.route('/orders/update', methods=['POST'])
@login_required
def update_order_status():
    data = request.json
    order_id = data['order_id']
    status = data['status']  # approved, completed, rejected
    
    order_ref = db.collection('orders').document(order_id)
    order = order_ref.get()
    if not order.exists:
        return jsonify({"status": "error", "message": "Order not found"}), 404
    
    order_data = order.to_dict()
    order_ref.update({'status': status})
    
    # If completed, create sale
    if status == 'completed':
        total_sticks = order_data['total_sticks']
        total_price = order_data['total']
        method = order_data['payment_method']
        customer_name = order_data['customer_name']
        
        db.collection('sales').add({
            'qty': total_sticks,
            'price': total_price,
            'method': method,
            'customer': customer_name,
            'timestamp': datetime.now(timezone.utc),
            'item_type': 'pack',
            'from_order': order_id
        })
        
        if method == 'credit':
            debtor_ref = db.collection('debtors').document(customer_name)
            if debtor_ref.get().exists:
                debtor_ref.update({'balance': firestore.Increment(total_price)})
            else:
                debtor_ref.set({'balance': total_price, 'trust_score': 50, 'created': datetime.now(timezone.utc)})
            
            # Update customer
            phone = order_data['customer_phone']
            cust_ref = db.collection('customers').document(phone)
            cust = cust_ref.get().to_dict()
            new_debt = cust.get('current_debt', 0) + total_price
            new_points = cust.get('loyalty_points', 0) + (total_sticks // 20)
            
            cust_ref.update({'current_debt': new_debt, 'loyalty_points': new_points})
            update_customer_tier(cust_ref, new_points)
    
    return jsonify({"status": "success"})

# Customer management
@app.route('/customers')
@login_required
def view_customers():
    customers = db.collection('customers').stream()
    cust_list = []
    for c in customers:
        cd = c.to_dict()
        cd['phone'] = c.id
        cust_list.append(cd)
    
    cust_list.sort(key=lambda x: x['loyalty_points'], reverse=True)
    return render_template('customers.html', customers=cust_list, user=session.get('user'), role=session.get('role'))

@app.route('/customers/approve', methods=['POST'])
@login_required
def approve_customer():
    data = request.json
    phone = data['phone']
    db.collection('customers').document(phone).update({'approved': True})
    return jsonify({"status": "success"})

@app.route('/customers/toggle-credit', methods=['POST'])
@login_required
def toggle_credit():
    data = request.json
    phone = data['phone']
    enabled = data['enabled']
    db.collection('customers').document(phone).update({'credit_enabled': enabled})
    return jsonify({"status": "success"})

@app.route('/customers/update-limit', methods=['POST'])
@login_required
def update_credit_limit():
    data = request.json
    phone = data['phone']
    limit = float(data['limit'])
    db.collection('customers').document(phone).update({'credit_limit': limit, 'tier_override': True})
    return jsonify({"status": "success"})

@app.route('/customers/blacklist', methods=['POST'])
@login_required
def blacklist_customer():
    data = request.json
    phone = data['phone']
    blacklisted = data['blacklisted']
    db.collection('customers').document(phone).update({'credit_enabled': not blacklisted})
    return jsonify({"status": "success"})

# Insights page
@app.route('/insights')
@login_required
def view_insights():
    sales = list(db.collection('sales').stream())
    customers_col = list(db.collection('customers').where('approved', '==', True).stream())
    
    # Top customers by spending
    customer_spending = {}
    for s in sales:
        data = s.to_dict()
        cust = data.get('customer', 'Cash Customer')
        if cust not in customer_spending:
            customer_spending[cust] = 0
        customer_spending[cust] += data.get('price', 0)
    
    top_customers = sorted(customer_spending.items(), key=lambda x: x[1], reverse=True)[:5]
    
    # Most reliable payers (lowest debt ratio)
    reliable_payers = []
    for c in customers_col:
        cd = c.to_dict()
        cd['phone'] = c.id
        if cd.get('loyalty_points', 0) > 0:
            debt_ratio = cd.get('current_debt', 0) / max(1, cd.get('loyalty_points', 1))
            cd['debt_ratio'] = debt_ratio
            reliable_payers.append(cd)
    
    reliable_payers.sort(key=lambda x: x['debt_ratio'])
    reliable_payers = reliable_payers[:5]
    
    # Worst debtors
    worst_debtors = sorted(customers_col, key=lambda x: x.to_dict().get('current_debt', 0), reverse=True)[:5]
    worst_debtor_list = []
    for w in worst_debtors:
        wd = w.to_dict()
        wd['phone'] = w.id
        if wd.get('current_debt', 0) > 0:
            worst_debtor_list.append(wd)
    
    return render_template('insights.html',
                           top_customers=top_customers,
                           reliable_payers=reliable_payers,
                           worst_debtors=worst_debtor_list,
                           user=session.get('user'),
                           role=session.get('role'))

# Other existing routes (debtors, history, stock, etc.)
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
    return render_template('debtors.html', debtors=debtor_list, total_owed=round(total_owed, 2), user=session.get('user'), role=session.get('role'))

@app.route('/history')
@login_required
def view_history():
    sales = db.collection('sales').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(50).stream()
    sales_list = []
    for s in sales:
        data = s.to_dict()
        data['id'] = s.id
        sales_list.append(data)
    return render_template('history.html', sales=sales_list, user=session.get('user'), role=session.get('role'))

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
    return render_template('stock.html', stock_list=stock_list, monthly=monthly, total_bundles=total_bundles, total_spent=total_spent, total_sticks_from_stock=total_sticks_from_stock, total_sticks_sold=total_sticks_sold, sticks_remaining=sticks_remaining, bundle_cost=BUNDLE_COST, sticks_per_bundle=STICKS_PER_BUNDLE, user=session.get('user'), role=session.get('role'))

@app.route('/stock/add', methods=['POST'])
@login_required
def add_stock():
    data = request.json
    bundles = int(data['bundles'])
    stock_cost = bundles * BUNDLE_COST
    transport_cost = float(data.get('transport_cost', 0))
    total_cost = stock_cost + transport_cost
    
    payment_source = data.get('payment_source', 'personal')  # 'business' or 'personal'
    transport_source = data.get('transport_source', 'personal')  # who paid transport
    
    if data.get('date'):
        purchase_date = datetime.strptime(data['date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
    else:
        purchase_date = datetime.now(timezone.utc)
    
    # Create individual bundle records
    bundle_ids = []
    for i in range(bundles):
        bundle_doc = db.collection('bundles').add({
            'bundle_number': i + 1,
            'purchase_date': purchase_date,
            'cost': BUNDLE_COST,
            'sticks_total': STICKS_PER_BUNDLE,
            'sticks_sold': 0,
            'cash_revenue': 0,
            'credit_revenue': 0,
            'status': 'active',
            'note': data.get('note', '')
        })
        bundle_ids.append(bundle_doc[1].id)
    
    # Add to stock collection (for backwards compatibility)
    db.collection('stock').add({
        'bundles': bundles,
        'sticks': bundles * STICKS_PER_BUNDLE,
        'cost': stock_cost,
        'date': purchase_date,
        'note': data.get('note', ''),
        'payment_source': payment_source,
        'transport_cost': transport_cost,
        'transport_source': transport_source,
        'bundle_ids': bundle_ids
    })
    
    # Record expenses if paid from business cash
    if payment_source == 'business':
        db.collection('expenses').add({
            'type': 'stock',
            'amount': stock_cost,
            'description': f'{bundles} bundles purchased',
            'date': purchase_date,
            'paid_from': 'business_cash'
        })
    
    if transport_cost > 0 and transport_source == 'business':
        db.collection('expenses').add({
            'type': 'transport',
            'amount': transport_cost,
            'description': f'Transport for {bundles} bundles',
            'date': purchase_date,
            'paid_from': 'business_cash'
        })
    
    # Record personal injection if paid from personal money
    if payment_source == 'personal':
        db.collection('personal_injections').add({
            'amount': stock_cost,
            'description': f'Personal money for {bundles} bundles',
            'date': purchase_date
        })
    
    if transport_cost > 0 and transport_source == 'personal':
        db.collection('personal_injections').add({
            'amount': transport_cost,
            'description': f'Personal money for transport',
            'date': purchase_date
        })
    
    return jsonify({
        "status": "success",
        "bundles": bundles,
        "sticks": bundles * STICKS_PER_BUNDLE,
        "cost": total_cost,
        "payment_source": payment_source
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
            transactions.append({'id': s.id, 'customer': data.get('customer', 'Unknown'), 'method': data.get('method', 'cash'), 'item_type': data.get('item_type', 'pack'), 'price': data['price'], 'qty': data['qty'], 'profit': round(profit, 2), 'time_ago': time_ago})
        return jsonify({'transactions': transactions})
    except Exception as e:
        print(f"ERROR in recent_transactions: {e}")
        return jsonify({'transactions': [], 'error': str(e)}), 500

@app.route('/reports')
@login_required
def view_reports():
    """Summary reports page with bundles tab"""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=today_start.weekday())
    last_week_start = week_start - timedelta(days=7)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 1:
        last_month_start = month_start.replace(year=month_start.year - 1, month=12)
    else:
        last_month_start = month_start.replace(month=month_start.month - 1)
    
    def get_period_data(start, end):
        sales = db.collection('sales').where('timestamp', '>=', start).where('timestamp', '<', end).stream()
        payments = db.collection('payments').where('timestamp', '>=', start).where('timestamp', '<', end).stream()
        
        total_sales = 0
        total_sticks = 0
        cash_sales = 0
        credit_sales = 0
        customer_sales = {}
        
        for s in sales:
            data = s.to_dict()
            price = data.get('price', 0)
            qty = data.get('qty', 0)
            method = data.get('method', 'cash')
            customer = data.get('customer', 'Cash Customer')
            
            total_sales += price
            total_sticks += qty
            
            if method == 'cash':
                cash_sales += price
            else:
                credit_sales += price
            
            if customer not in customer_sales:
                customer_sales[customer] = 0
            customer_sales[customer] += price
        
        total_payments = sum(p.to_dict().get('amount', 0) for p in payments)
        profit = total_sales - (total_sticks * COST_PER_STICK)
        top_customer = max(customer_sales.items(), key=lambda x: x[1]) if customer_sales else ('None', 0)
        
        return {
            'total_sales': round(total_sales, 2),
            'cash_sales': round(cash_sales, 2),
            'credit_sales': round(credit_sales, 2),
            'profit': round(profit, 2),
            'sticks_sold': total_sticks,
            'payments_received': round(total_payments, 2),
            'top_customer': top_customer[0],
            'top_customer_amount': round(top_customer[1], 2)
        }
    
    today = get_period_data(today_start, now)
    yesterday = get_period_data(yesterday_start, today_start)
    this_week = get_period_data(week_start, now)
    last_week = get_period_data(last_week_start, week_start)
    this_month = get_period_data(month_start, now)
    last_month = get_period_data(last_month_start, month_start)
    
    def calc_change(current, previous):
        if previous == 0:
            return 0
        return round(((current - previous) / previous) * 100, 1)
    
    week_change = calc_change(this_week['total_sales'], last_week['total_sales'])
    month_change = calc_change(this_month['total_sales'], last_month['total_sales'])
    
    # Bundle data
    bundles = db.collection('bundles').order_by('purchase_date', direction=firestore.Query.DESCENDING).stream()
    bundle_list = []
    bundle_chart_labels = []
    bundle_chart_cash = []
    bundle_chart_credit = []
    
    for idx, b in enumerate(bundles):
        bd = b.to_dict()
        bd['id'] = b.id
        
        total_revenue = bd['cash_revenue'] + bd['credit_revenue']
        profit = total_revenue - bd['cost']
        progress_pct = (bd['sticks_sold'] / bd['sticks_total']) * 100
        
        bd['total_revenue'] = round(total_revenue, 2)
        bd['profit'] = round(profit, 2)
        bd['progress_pct'] = round(progress_pct, 1)
        
        bundle_list.append(bd)
        
        # Chart data (last 20 bundles)
        if idx < 20:
            bundle_chart_labels.insert(0, f"Bundle {idx + 1}")
            bundle_chart_cash.insert(0, round(bd['cash_revenue'], 2))
            bundle_chart_credit.insert(0, round(bd['credit_revenue'], 2))
    
    # Calculate cash flow
    sales_all = db.collection('sales').stream()
    total_cash_sales = sum(s.to_dict()['price'] for s in sales_all if s.to_dict().get('method') == 'cash')
    
    expenses_all = db.collection('expenses').stream()
    total_expenses = sum(e.to_dict().get('amount', 0) for e in expenses_all)
    
    injections_all = db.collection('personal_injections').stream()
    total_injections = sum(i.to_dict().get('amount', 0) for i in injections_all)
    
    net_cash = total_cash_sales + total_injections - total_expenses
    
    return render_template('reports.html',
                           today=today,
                           yesterday=yesterday,
                           this_week=this_week,
                           last_week=last_week,
                           this_month=this_month,
                           last_month=last_month,
                           week_change=week_change,
                           month_change=month_change,
                           bundles=bundle_list,
                           bundle_chart_labels=bundle_chart_labels,
                           bundle_chart_cash=bundle_chart_cash,
                           bundle_chart_credit=bundle_chart_credit,
                           total_cash_sales=round(total_cash_sales, 2),
                           total_expenses=round(total_expenses, 2),
                           total_injections=round(total_injections, 2),
                           net_cash=round(net_cash, 2),
                           user=session.get('user'),
                           role=session.get('role'))

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