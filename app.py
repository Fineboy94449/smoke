import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, render_template, request, jsonify
from datetime import datetime, timezone

# Initialize Firebase
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

app = Flask(__name__)

# Business Constants
BUNDLE_COST = 145.00
STICKS_PER_BUNDLE = 200
COST_PER_STICK = BUNDLE_COST / STICKS_PER_BUNDLE  # R0.72

@app.route('/')
def dashboard():
    """Main dashboard with financial overview"""
    sales = db.collection('sales').stream()
    debtors = db.collection('debtors').stream()
    
    cash_total = 0
    credit_total = 0
    total_sticks_sold = 0
    daily_sales = 0
    
    today = datetime.now().date()
    
    for s in sales:
        data = s.to_dict()
        if data['method'] == 'cash':
            cash_total += data['price']
        else:
            credit_total += data['price']
        total_sticks_sold += data['qty']
        
        # Calculate today's sales
        sale_date = data.get('timestamp', datetime.now()).date()
        if sale_date == today:
            daily_sales += data['price']
    
    # Calculate metrics
    total_cost = total_sticks_sold * COST_PER_STICK
    net_profit = (cash_total + credit_total) - total_cost
    
    # Credit risk assessment
    if cash_total > 0:
        credit_ratio = credit_total / cash_total
        if credit_ratio > 0.6:
            risk_level = "HIGH"
        elif credit_ratio > 0.3:
            risk_level = "MEDIUM"
        else:
            risk_level = "SAFE"
    else:
        risk_level = "HIGH" if credit_total > 0 else "SAFE"
    
    # Prepare debtor list
    debtor_list = []
    for d in debtors:
        debtor_data = d.to_dict()
        debtor_data['name'] = d.id
        debtor_list.append(debtor_data)
    
    # Sort debtors by balance (highest first)
    debtor_list.sort(key=lambda x: x['balance'], reverse=True)
    
    return render_template('dashboard.html',
                         cash=round(cash_total, 2),
                         credit=round(credit_total, 2),
                         profit=round(net_profit, 2),
                         daily=round(daily_sales, 2),
                         risk=risk_level,
                         debtors=debtor_list,
                         sticks_sold=total_sticks_sold)

@app.route('/sell', methods=['POST'])
def process_sale():
    """Process a cigarette sale"""
    data = request.json
    
    # Determine quantity
    qty = 1 if data['item'] == 'loose' else 20
    
    # Pricing logic
    if data['item'] == 'loose':
        price = 1.50
    else:
        price = 40.00 if data['method'] == 'credit' else 30.00
    
    customer_name = data.get('name', 'Cash Customer')
    
    # Log transaction
    db.collection('sales').add({
        'qty': qty,
        'price': price,
        'method': data['method'],
        'customer': customer_name,
        'timestamp': datetime.now(),
        'item_type': data['item']
    })
    
    # Update debtor if credit sale
    if data['method'] == 'credit':
        debtor_ref = db.collection('debtors').document(customer_name)
        doc = debtor_ref.get()
        
        if doc.exists:
            current_balance = doc.to_dict().get('balance', 0)
            debtor_ref.update({
                'balance': firestore.Increment(price),
                'last_purchase': datetime.now()
            })
        else:
            debtor_ref.set({
                'balance': price,
                'trust_score': 50,
                'created': datetime.now(),
                'last_purchase': datetime.now()
            })
    
    profit_made = price - (qty * COST_PER_STICK)
    
    return jsonify({
        "status": "success",
        "profit_made": round(profit_made, 2),
        "new_balance": price
    })

@app.route('/payment', methods=['POST'])
def record_payment():
    """Record a debt payment"""
    data = request.json
    name = data['name']
    amount = float(data['amount'])
    
    debtor_ref = db.collection('debtors').document(name)
    doc = debtor_ref.get()
    
    if not doc.exists:
        return jsonify({"status": "error", "message": "Debtor not found"}), 404
    
    current_balance = doc.to_dict()['balance']
    new_balance = max(0, current_balance - amount)
    
    # Update debtor balance
    if new_balance == 0:
        debtor_ref.delete()
    else:
        debtor_ref.update({
            'balance': new_balance,
            'last_payment': datetime.now()
        })
    
    # Log payment
    db.collection('payments').add({
        'customer': name,
        'amount': amount,
        'timestamp': datetime.now(),
        'previous_balance': current_balance,
        'new_balance': new_balance
    })
    
    return jsonify({
        "status": "success",
        "new_balance": round(new_balance, 2),
        "paid_in_full": new_balance == 0
    })

@app.route('/debtors')
def view_debtors():
    """View detailed debtor information"""
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
                         total_owed=round(total_owed, 2))

@app.route('/history')
def view_history():
    """View sales history"""
    sales = db.collection('sales').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(50).stream()
    
    sales_list = []
    for s in sales:
        data = s.to_dict()
        data['id'] = s.id
        sales_list.append(data)
    
    return render_template('history.html', sales=sales_list)

@app.route('/stock')
def view_stock():
    """View stock/bundle management page"""
    # Fetch all stock entries
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
    
    # Group by month
    monthly = {}
    for s in stock_list:
        month_key = s['date'].strftime('%B %Y')
        if month_key not in monthly:
            monthly[month_key] = {'bundles': 0, 'cost': 0.0, 'entries': []}
        monthly[month_key]['bundles'] += s['bundles']
        monthly[month_key]['cost'] += s['cost']
        monthly[month_key]['entries'].append(s)
    
    # Calculate total sticks sold from sales
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
                         sticks_per_bundle=STICKS_PER_BUNDLE)

@app.route('/stock/add', methods=['POST'])
def add_stock():
    """Add a stock/bundle purchase"""
    data = request.json
    bundles = int(data['bundles'])
    cost = bundles * BUNDLE_COST
    
    # Parse date - use provided date or today
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
def delete_stock():
    """Delete a stock entry"""
    data = request.json
    stock_id = data['stock_id']
    
    try:
        db.collection('stock').document(stock_id).delete()
        return jsonify({"status": "success", "message": "Stock entry deleted"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
def recent_transactions():
    """Get recent transactions for dashboard"""
    sales = db.collection('sales').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(10).stream()
    
    transactions = []
    for s in sales:
        data = s.to_dict()
        
        # Calculate time ago
        time_diff = datetime.now(timezone.utc) - data['timestamp']
        if time_diff.seconds < 60:
            time_ago = "Just now"
        elif time_diff.seconds < 3600:
            minutes = time_diff.seconds // 60
            time_ago = f"{minutes} min ago"
        elif time_diff.days == 0:
            hours = time_diff.seconds // 3600
            time_ago = f"{hours} hour{'s' if hours > 1 else ''} ago"
        else:
            time_ago = data['timestamp'].strftime('%d %b, %I:%M %p')
        
        profit = data['price'] - (data['qty'] * COST_PER_STICK)
        
        transactions.append({
            'id': s.id,
            'customer': data['customer'],
            'method': data['method'],
            'item_type': data.get('item_type', 'pack'),
            'price': data['price'],
            'qty': data['qty'],
            'profit': profit,
            'time_ago': time_ago
        })
    
    return jsonify({'transactions': transactions})

@app.route('/delete-transaction', methods=['POST'])
def delete_transaction():
    """Delete a transaction and reverse any credit"""
    data = request.json
    transaction_id = data['transaction_id']
    
    try:
        # Get transaction details
        doc = db.collection('sales').document(transaction_id).get()
        
        if not doc.exists:
            return jsonify({"status": "error", "message": "Transaction not found"}), 404
        
        trans_data = doc.to_dict()
        
        # If it was a credit sale, reverse the credit
        if trans_data['method'] == 'credit':
            customer_name = trans_data['customer']
            amount = trans_data['price']
            
            debtor_ref = db.collection('debtors').document(customer_name)
            debtor_doc = debtor_ref.get()
            
            if debtor_doc.exists:
                current_balance = debtor_doc.to_dict()['balance']
                new_balance = max(0, current_balance - amount)
                
                if new_balance == 0:
                    debtor_ref.delete()
                else:
                    debtor_ref.update({'balance': new_balance})
        
        # Delete the transaction
        db.collection('sales').document(transaction_id).delete()
        
        return jsonify({
            "status": "success",
            "message": "Transaction deleted successfully"
        })
    
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)