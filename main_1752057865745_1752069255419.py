import logging
import os
import sqlite3
import hashlib
import uuid
import json
import base64
import qrcode
import asyncio
import aiohttp
import time
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image
import threading
from flask import Flask, render_template, request, jsonify, send_file
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
)

# Configuration du logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# États de conversation
CHOOSING, SERVICE_DETAIL, AMOUNT_DETAIL, PHONE_INPUT, CONFIRMATION, PAYMENT_PROOF, ADMIN_PANEL, PAYOUT_INFO, SUPPORT_CHAT = range(9)

# Configuration base de données
DB_PATH = "bot_database.db"

# Configuration Flask pour interface web
app = Flask(__name__)
app.secret_key = os.urandom(24)

# Système de tarification dynamique (géré par admin)
DYNAMIC_RATES = {
    'usdt_buy': {'rate': 280, 'cashback': 0.02, 'vip_cashback': 0.05},
    'usdt_sell': {'rate': 270, 'cashback': 0.02, 'vip_cashback': 0.05},
    'flexy': {'multiplier': 1.2, 'cashback': 0.02, 'vip_cashback': 0.05},
    'mobilis': {'multiplier': 1.15, 'cashback': 0.02, 'vip_cashback': 0.05},
    'ooredoo': {'multiplier': 1.15, 'cashback': 0.02, 'vip_cashback': 0.05}
}

# Adresse USDT pour les ventes (géré par admin)
USDT_RECEIVING_ADDRESS = "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"

# Minuteur des transactions (10 minutes)
TRANSACTION_TIMEOUT = 600  # 10 minutes en secondes

# Codes promo actifs
PROMO_CODES = {
    'WELCOME10': {'discount': 0.10, 'expires': '2025-12-31', 'max_uses': 100, 'used': 0},
    'VIP20': {'discount': 0.20, 'expires': '2025-12-31', 'max_uses': 50, 'used': 0, 'vip_only': True},
    'RAMADAN15': {'discount': 0.15, 'expires': '2025-04-30', 'max_uses': 200, 'used': 0}
}

# Méthodes de paiement
PAYMENT_METHODS = {
    'ccp': {'name': 'CCP', 'fee': 0.01, 'min_amount': 100},
    'baridimob': {'name': 'BaridiMob', 'fee': 0.015, 'min_amount': 50},
    'crypto': {'name': 'Crypto (USDT)', 'fee': 0.02, 'min_amount': 10},
    'western': {'name': 'Western Union', 'fee': 0.025, 'min_amount': 500}
}

class DatabaseManager:
    def __init__(self):
        self.init_database()

    def init_database(self):
        """Initialise la base de données avec toutes les tables"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Table des utilisateurs avec profils VIP
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER UNIQUE,
                username TEXT,
                first_name TEXT,
                phone TEXT,
                email TEXT,
                is_vip BOOLEAN DEFAULT FALSE,
                vip_expires DATE,
                loyalty_points INTEGER DEFAULT 0,
                cashback_balance REAL DEFAULT 0.0,
                total_spent REAL DEFAULT 0.0,
                referral_code TEXT UNIQUE,
                referred_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Table des transactions détaillées
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                transaction_id TEXT UNIQUE,
                service_type TEXT,
                amount REAL,
                fee REAL,
                cashback REAL,
                status TEXT DEFAULT 'pending',
                phone_number TEXT,
                operator TEXT,
                payment_method TEXT,
                payment_reference TEXT,
                promo_code TEXT,
                discount_applied REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        # Table des codes promo utilisés
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS promo_usage (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                promo_code TEXT,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        # Table des paiements
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY,
                transaction_id TEXT,
                payment_method TEXT,
                amount REAL,
                fee REAL,
                reference TEXT,
                qr_code TEXT,
                status TEXT DEFAULT 'pending',
                proof_url TEXT,
                verified_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (transaction_id) REFERENCES transactions (transaction_id)
            )
        ''')

        # Table des preuves de paiement
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payment_proofs (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                telegram_id INTEGER,
                transaction_id TEXT,
                service_type TEXT,
                amount REAL,
                payment_method TEXT,
                proof_file_id TEXT,
                proof_type TEXT,
                status TEXT DEFAULT 'pending',
                admin_response TEXT,
                admin_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        # Table des admins
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER UNIQUE,
                username TEXT,
                role TEXT DEFAULT 'admin',
                permissions TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Table des logs d'activité
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                action TEXT,
                details TEXT,
                ip_address TEXT,
                user_agent TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Table des paramètres admin
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_settings (
                id INTEGER PRIMARY KEY,
                setting_name TEXT UNIQUE,
                setting_value TEXT,
                updated_by INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Table des ventes USDT
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS usdt_sales (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                telegram_id INTEGER,
                amount_usdt REAL,
                rate_dzd REAL,
                total_dzd REAL,
                usdt_address TEXT,
                transaction_hash TEXT,
                status TEXT DEFAULT 'waiting_send',
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        # Table des informations de paiement utilisateur
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_payout_info (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                payout_method TEXT,
                account_details TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        # Table des messages de support
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                message TEXT,
                message_type TEXT DEFAULT 'user',
                status TEXT DEFAULT 'pending',
                admin_response TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        # Table des réponses automatiques du chat
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_responses (
                id INTEGER PRIMARY KEY,
                keyword TEXT,
                response TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Insérer les paramètres par défaut
        cursor.execute('''
            INSERT OR IGNORE INTO admin_settings (setting_name, setting_value) VALUES
            ('usdt_buy_rate', '280'),
            ('usdt_sell_rate', '270'),
            ('usdt_address', 'TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE'),
            ('transaction_timeout', '600')
        ''')

        conn.commit()
        conn.close()

    def get_user(self, telegram_id):
        """Récupère un utilisateur par son ID Telegram"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
        user = cursor.fetchone()
        conn.close()
        return user

    def create_user(self, telegram_id, username, first_name):
        """Crée un nouvel utilisateur"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        referral_code = self.generate_referral_code()

        cursor.execute('''
            INSERT INTO users (telegram_id, username, first_name, referral_code)
            VALUES (?, ?, ?, ?)
        ''', (telegram_id, username, first_name, referral_code))

        conn.commit()
        conn.close()
        return referral_code

    def generate_referral_code(self):
        """Génère un code de parrainage unique"""
        return f"REF{uuid.uuid4().hex[:8].upper()}"

    def create_transaction(self, user_id, service_type, amount, phone_number=None, operator=None):
        """Crée une nouvelle transaction"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        transaction_id = f"TXN{uuid.uuid4().hex[:12].upper()}"

        cursor.execute('''
            INSERT INTO transactions (user_id, transaction_id, service_type, amount, phone_number, operator)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, transaction_id, service_type, amount, phone_number, operator))

        conn.commit()
        conn.close()
        return transaction_id

    def get_user_transactions(self, user_id, limit=10):
        """Récupère les transactions d'un utilisateur"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM transactions 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT ?
        ''', (user_id, limit))
        transactions = cursor.fetchall()
        conn.close()
        return transactions

    def update_user_activity(self, telegram_id):
        """Met à jour la dernière activité de l'utilisateur"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users 
            SET last_activity = CURRENT_TIMESTAMP 
            WHERE telegram_id = ?
        ''', (telegram_id,))
        conn.commit()
        conn.close()

    def add_loyalty_points(self, telegram_id, points):
        """Ajoute des points de fidélité"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users 
            SET loyalty_points = loyalty_points + ? 
            WHERE telegram_id = ?
        ''', (points, telegram_id))
        conn.commit()
        conn.close()

    def convert_points_to_cashback(self, telegram_id, points_to_convert):
        """Convertit les points en cashback"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Vérifier les points disponibles
        cursor.execute('SELECT loyalty_points FROM users WHERE telegram_id = ?', (telegram_id,))
        current_points = cursor.fetchone()[0]

        if current_points >= points_to_convert:
            # Calculer le cashback (100 pts = 10 DZD)
            cashback_amount = points_to_convert / 10

            cursor.execute('''
                UPDATE users 
                SET loyalty_points = loyalty_points - ?, 
                    cashback_balance = cashback_balance + ?
                WHERE telegram_id = ?
            ''', (points_to_convert, cashback_amount, telegram_id))
            conn.commit()
            conn.close()
            return True, cashback_amount

        conn.close()
        return False, 0

    def save_payment_proof(self, user_id, telegram_id, transaction_id, service_type, amount, payment_method, file_id, proof_type):
        """Enregistre une preuve de paiement"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO payment_proofs (user_id, telegram_id, transaction_id, service_type, amount, payment_method, proof_file_id, proof_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, telegram_id, transaction_id, service_type, amount, payment_method, file_id, proof_type))
        
        proof_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return proof_id

    def get_pending_proofs(self):
        """Récupère toutes les preuves en attente"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT pp.id, pp.telegram_id, u.username, u.first_name, pp.service_type, 
                   pp.amount, pp.payment_method, pp.created_at, pp.proof_file_id
            FROM payment_proofs pp
            JOIN users u ON pp.user_id = u.id
            WHERE pp.status = 'pending'
            ORDER BY pp.created_at DESC
        ''')
        proofs = cursor.fetchall()
        conn.close()
        return proofs

    def approve_proof(self, proof_id, admin_id):
        """Approuve une preuve de paiement"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE payment_proofs 
            SET status = 'approved', admin_id = ?, admin_response = 'Paiement validé', processed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (admin_id, proof_id))
        conn.commit()
        conn.close()

    def reject_proof(self, proof_id, admin_id, reason):
        """Rejette une preuve de paiement"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE payment_proofs 
            SET status = 'rejected', admin_id = ?, admin_response = ?, processed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (admin_id, reason, proof_id))
        conn.commit()
        conn.close()

    def get_proof_by_id(self, proof_id):
        """Récupère une preuve par son ID"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT pp.*, u.username, u.first_name
            FROM payment_proofs pp
            JOIN users u ON pp.user_id = u.id
            WHERE pp.id = ?
        ''', (proof_id,))
        proof = cursor.fetchone()
        conn.close()
        return proof

    def get_admin_setting(self, setting_name):
        """Récupère un paramètre admin"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT setting_value FROM admin_settings WHERE setting_name = ?', (setting_name,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else None

    def update_admin_setting(self, setting_name, setting_value, admin_id=1):
        """Met à jour un paramètre admin"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO admin_settings (setting_name, setting_value, updated_by, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ''', (setting_name, setting_value, admin_id))
        conn.commit()
        conn.close()

    def create_usdt_sale(self, user_id, telegram_id, amount_usdt, rate_dzd):
        """Crée une vente USDT"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        total_dzd = amount_usdt * rate_dzd
        usdt_address = self.get_admin_setting('usdt_address') or USDT_RECEIVING_ADDRESS
        timeout = int(self.get_admin_setting('transaction_timeout') or TRANSACTION_TIMEOUT)
        expires_at = datetime.now() + timedelta(seconds=timeout)
        
        cursor.execute('''
            INSERT INTO usdt_sales (user_id, telegram_id, amount_usdt, rate_dzd, total_dzd, usdt_address, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, telegram_id, amount_usdt, rate_dzd, total_dzd, usdt_address, expires_at))
        
        sale_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return sale_id

    def create_user_payout_info(self, user_id, payout_method, account_details):
        """Enregistre les informations de paiement utilisateur"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO user_payout_info 
            (user_id, payout_method, account_details, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, payout_method, json.dumps(account_details)))
        
        conn.commit()
        conn.close()

    def get_user_payout_info(self, user_id):
        """Récupère les informations de paiement utilisateur"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM user_payout_info WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return {
                'method': result[2],
                'details': json.loads(result[3]) if result[3] else {}
            }
        return None

    def save_support_message(self, user_id, message, message_type='user'):
        """Enregistre un message de support"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO support_messages (user_id, message, message_type, created_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, message, message_type))
        
        message_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return message_id

    def get_support_conversation(self, user_id, limit=20):
        """Récupère la conversation de support"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT message, message_type, created_at 
            FROM support_messages 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT ?
        ''', (user_id, limit))
        
        messages = cursor.fetchall()
        conn.close()
        return list(reversed(messages))

    def get_usdt_sale(self, sale_id):
        """Récupère une vente USDT"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM usdt_sales WHERE id = ?', (sale_id,))
        sale = cursor.fetchone()
        conn.close()
        return sale

    def update_usdt_sale_hash(self, sale_id, transaction_hash):
        """Met à jour le hash de transaction USDT"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE usdt_sales 
            SET transaction_hash = ?, status = 'hash_provided'
            WHERE id = ?
        ''', (transaction_hash, sale_id))
        conn.commit()
        conn.close()

    def get_pending_usdt_sales(self):
        """Récupère les ventes USDT en attente"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT us.*, u.username, u.first_name
            FROM usdt_sales us
            JOIN users u ON us.user_id = u.id
            WHERE us.status IN ('waiting_send', 'hash_provided')
            ORDER BY us.created_at DESC
        ''')
        sales = cursor.fetchall()
        conn.close()
        return sales

    def approve_usdt_sale(self, sale_id, admin_id):
        """Approuve une vente USDT"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE usdt_sales 
            SET status = 'completed', completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (sale_id,))
        conn.commit()
        conn.close()

    def reject_usdt_sale(self, sale_id, admin_id):
        """Rejette une vente USDT"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE usdt_sales 
            SET status = 'rejected'
            WHERE id = ?
        ''', (sale_id,))
        conn.commit()
        conn.close()

# Instance du gestionnaire de base de données
db = DatabaseManager()

class PaymentProcessor:
    def __init__(self):
        self.payment_methods = PAYMENT_METHODS

    def generate_qr_code(self, payment_data):
        """Génère un QR code pour le paiement"""
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(json.dumps(payment_data))
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)

        return base64.b64encode(buf.getvalue()).decode()

    def create_payment_reference(self, transaction_id, method):
        """Crée une référence de paiement unique"""
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        hash_input = f"{transaction_id}{method}{timestamp}"
        hash_object = hashlib.md5(hash_input.encode())
        return f"{method.upper()}{hash_object.hexdigest()[:8].upper()}"

    def calculate_fees(self, amount, method):
        """Calcule les frais de transaction"""
        if method in self.payment_methods:
            fee_rate = self.payment_methods[method]['fee']
            return amount * fee_rate
        return 0

    def process_payment(self, transaction_id, method, amount):
        """Traite un paiement"""
        reference = self.create_payment_reference(transaction_id, method)
        fee = self.calculate_fees(amount, method)

        payment_data = {
            'transaction_id': transaction_id,
            'method': method,
            'amount': amount,
            'fee': fee,
            'reference': reference,
            'timestamp': datetime.now().isoformat()
        }

        qr_code = self.generate_qr_code(payment_data)

        # Enregistrer dans la base de données
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO payments (transaction_id, payment_method, amount, fee, reference, qr_code)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (transaction_id, method, amount, fee, reference, qr_code))
        conn.commit()
        conn.close()

        return {
            'reference': reference,
            'qr_code': qr_code,
            'fee': fee,
            'total': amount + fee
        }

# Instance du processeur de paiement
payment_processor = PaymentProcessor()

class LoyaltySystem:
    def __init__(self):
        self.point_rates = {
            'usdt': 1,      # 1 point per DZD
            'flexy': 0.5,   # 0.5 points per DZD
            'mobilis': 0.5,
            'ooredoo': 0.5
        }

    def calculate_cashback(self, user_id, amount, service_type):
        """Calcule le cashback pour une transaction"""
        user = db.get_user(user_id)
        if not user:
            return 0

        is_vip = user[5]  # is_vip column
        rate_info = DYNAMIC_RATES.get(service_type, {})

        if is_vip:
            cashback_rate = rate_info.get('vip_cashback', 0.05)
        else:
            cashback_rate = rate_info.get('cashback', 0.02)

        return amount * cashback_rate

    def calculate_loyalty_points(self, amount, service_type):
        """Calcule les points de fidélité"""
        rate = self.point_rates.get(service_type, 0.5)
        return int(amount * rate)

    def apply_promo_code(self, user_id, promo_code, amount):
        """Applique un code promo"""
        if promo_code not in PROMO_CODES:
            return 0, "Code promo invalide"

        promo = PROMO_CODES[promo_code]

        # Vérifier l'expiration
        if datetime.now() > datetime.strptime(promo['expires'], '%Y-%m-%d'):
            return 0, "Code promo expiré"

        # Vérifier les utilisations
        if promo['used'] >= promo['max_uses']:
            return 0, "Code promo épuisé"

        # Vérifier si VIP uniquement
        if promo.get('vip_only', False):
            user = db.get_user(user_id)
            if not user or not user[5]:  # is_vip
                return 0, "Code promo réservé aux VIP"

        # Vérifier si déjà utilisé
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM promo_usage WHERE user_id = ? AND promo_code = ?', 
                      (user_id, promo_code))
        used = cursor.fetchone()[0]
        conn.close()

        if used > 0:
            return 0, "Code promo déjà utilisé"

        discount = amount * promo['discount']
        return discount, "Code promo appliqué avec succès"

# Instance du système de fidélité
loyalty_system = LoyaltySystem()

# Interface Web Flask
@app.route('/')
def dashboard():
    """Dashboard principal"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Statistiques générales
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM users WHERE is_vip = 1')
    vip_users = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM transactions WHERE status = "completed"')
    completed_transactions = cursor.fetchone()[0]

    cursor.execute('SELECT SUM(amount) FROM transactions WHERE status = "completed"')
    total_revenue = cursor.fetchone()[0] or 0

    # Transactions récentes
    cursor.execute('''
        SELECT t.transaction_id, u.username, t.service_type, t.amount, t.status, t.created_at
        FROM transactions t
        JOIN users u ON t.user_id = u.id
        ORDER BY t.created_at DESC
        LIMIT 10
    ''')
    recent_transactions = cursor.fetchall()

    conn.close()

    stats = {
        'total_users': total_users,
        'vip_users': vip_users,
        'completed_transactions': completed_transactions,
        'total_revenue': total_revenue,
        'recent_transactions': recent_transactions
    }

    return render_template('dashboard.html', stats=stats)

@app.route('/api/stats')
def api_stats():
    """API pour les statistiques en temps réel"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Statistiques par heure pour les graphiques
    cursor.execute('''
        SELECT strftime('%H', created_at) as hour, COUNT(*) as count
        FROM transactions 
        WHERE DATE(created_at) = DATE('now')
        GROUP BY hour
        ORDER BY hour
    ''')
    hourly_stats = cursor.fetchall()

    # Répartition par service
    cursor.execute('''
        SELECT service_type, COUNT(*) as count
        FROM transactions
        WHERE status = "completed"
        GROUP BY service_type
    ''')
    service_stats = cursor.fetchall()

    conn.close()

    return jsonify({
        'hourly_stats': hourly_stats,
        'service_stats': service_stats
    })

@app.route('/admin/proofs')
def admin_proofs():
    """Page d'administration des preuves"""
    pending_proofs = db.get_pending_proofs()
    return render_template('admin_proofs.html', proofs=pending_proofs)

@app.route('/api/admin/approve_proof/<int:proof_id>', methods=['POST'])
def approve_proof_api(proof_id):
    """API pour approuver une preuve"""
    try:
        admin_id = 1  # ID admin par défaut
        proof = db.get_proof_by_id(proof_id)
        
        if proof:
            db.approve_proof(proof_id, admin_id)
            
            # Message personnalisé selon le service
            service_messages = {
                'usdt_buy': 'Votre achat USDT a été validé ! Les USDT seront transférés vers votre portefeuille.',
                'usdt_sell': 'Votre vente USDT a été validée ! Le paiement sera effectué sous peu.',
                'vip_purchase': 'Votre abonnement VIP a été activé ! Profitez de tous les avantages premium.',
                'mobile': 'Votre recharge mobile a été effectuée avec succès !',
                'djezzy': 'Votre recharge Djezzy a été effectuée avec succès !',
                'mobilis': 'Votre recharge Mobilis a été effectuée avec succès !',
                'ooredoo': 'Votre recharge Ooredoo a été effectuée avec succès !'
            }
            
            service_type = proof[4] if proof[4] else 'service'
            custom_message = service_messages.get(service_type, 'Votre paiement a été validé avec succès !')
            
            # Envoyer notification à l'utilisateur
            send_admin_notification(proof[2], "approved", custom_message)
            
            return jsonify({'status': 'success', 'message': 'Preuve approuvée et notification envoyée'})
        else:
            return jsonify({'status': 'error', 'message': 'Preuve non trouvée'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/admin/reject_proof/<int:proof_id>', methods=['POST'])
def reject_proof_api(proof_id):
    """API pour rejeter une preuve"""
    try:
        data = request.get_json()
        reason = data.get('reason', 'Preuve non conforme')
        admin_id = 1  # ID admin par défaut
        
        proof = db.get_proof_by_id(proof_id)
        
        if proof:
            db.reject_proof(proof_id, admin_id, reason)
            
            # Envoyer notification à l'utilisateur
            send_admin_notification(proof[2], "rejected", reason)
            
            return jsonify({'status': 'success', 'message': 'Preuve rejetée et notification envoyée'})
        else:
            return jsonify({'status': 'error', 'message': 'Preuve non trouvée'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/admin/test_notification/<int:telegram_id>')
def test_notification(telegram_id):
    """Test d'envoi de notification"""
    try:
        send_admin_notification(telegram_id, "approved", "Test de notification depuis l'admin")
        return jsonify({'status': 'success', 'message': f'Notification de test envoyée à {telegram_id}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/admin/settings')
def admin_settings():
    """Page des paramètres admin"""
    buy_rate = db.get_admin_setting('usdt_buy_rate') or '280'
    sell_rate = db.get_admin_setting('usdt_sell_rate') or '270'
    usdt_address = db.get_admin_setting('usdt_address') or USDT_RECEIVING_ADDRESS
    timeout = db.get_admin_setting('transaction_timeout') or '600'
    
    settings = {
        'usdt_buy_rate': buy_rate,
        'usdt_sell_rate': sell_rate,
        'usdt_address': usdt_address,
        'transaction_timeout': timeout
    }
    
    return render_template('admin_settings.html', settings=settings)

@app.route('/api/admin/update_setting', methods=['POST'])
def update_setting():
    """API pour mettre à jour un paramètre"""
    try:
        data = request.get_json()
        setting_name = data.get('setting_name')
        setting_value = data.get('setting_value')
        
        if not setting_name or not setting_value:
            return jsonify({'status': 'error', 'message': 'Paramètres manquants'})
        
        db.update_admin_setting(setting_name, setting_value)
        return jsonify({'status': 'success', 'message': f'Paramètre {setting_name} mis à jour'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/admin/usdt_sales')
def admin_usdt_sales():
    """Page de gestion des ventes USDT"""
    pending_sales = db.get_pending_usdt_sales()
    return render_template('admin_usdt_sales.html', sales=pending_sales)

@app.route('/api/admin/approve_usdt_sale/<int:sale_id>', methods=['POST'])
def approve_usdt_sale_api(sale_id):
    """API pour approuver une vente USDT"""
    try:
        admin_id = 1
        sale = db.get_usdt_sale(sale_id)
        
        if sale:
            db.approve_usdt_sale(sale_id, admin_id)
            
            # Envoyer notification à l'utilisateur
            message = f"Votre vente de {sale[2]:.4f} USDT a été validée ! Vous recevrez {sale[4]:.2f} DZD via BaridiMob/CCP sous peu."
            send_admin_notification(sale[1], "approved", message)
            
            return jsonify({'status': 'success', 'message': 'Vente USDT approuvée et notification envoyée'})
        else:
            return jsonify({'status': 'error', 'message': 'Vente non trouvée'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/admin/reject_usdt_sale/<int:sale_id>', methods=['POST'])
def reject_usdt_sale_api(sale_id):
    """API pour rejeter une vente USDT"""
    try:
        data = request.get_json()
        reason = data.get('reason', 'Transaction USDT non valide')
        admin_id = 1
        
        sale = db.get_usdt_sale(sale_id)
        
        if sale:
            db.reject_usdt_sale(sale_id, admin_id)
            
            # Envoyer notification à l'utilisateur
            send_admin_notification(sale[1], "rejected", reason)
            
            return jsonify({'status': 'success', 'message': 'Vente USDT rejetée et notification envoyée'})
        else:
            return jsonify({'status': 'error', 'message': 'Vente non trouvée'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

def validate_algerian_phone(phone):
    """Valide un numéro de téléphone algérien"""
    import re
    phone = re.sub(r'[^\d]', '', phone)

    patterns = [
        r'^0[567]\d{8}$',
        r'^213[567]\d{8}$',
        r'^[567]\d{8}$'
    ]

    for pattern in patterns:
        if re.match(pattern, phone):
            return True
    return False

def format_phone_number(phone):
    """Formate un numéro de téléphone algérien"""
    import re
    phone = re.sub(r'[^\d]', '', phone)

    if phone.startswith('213'):
        return f"+213 {phone[3:5]} {phone[5:7]} {phone[7:9]} {phone[9:11]}"
    elif phone.startswith('0'):
        return f"{phone[:3]} {phone[3:5]} {phone[5:7]} {phone[7:9]}"
    else:
        return f"0{phone[0]} {phone[1:3]} {phone[3:5]} {phone[5:7]}"

# Variable globale pour l'application Telegram
telegram_app = None

def send_admin_notification(telegram_id, status, message):
    """Envoie une notification à l'utilisateur depuis l'admin"""
    global telegram_app
    if telegram_app:
        try:
            # Créer une nouvelle boucle d'événements pour l'exécution asynchrone
            import threading
            def run_async():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(send_notification_async(telegram_id, status, message))
                loop.close()
            
            # Exécuter dans un thread séparé
            thread = threading.Thread(target=run_async)
            thread.daemon = True
            thread.start()
        except Exception as e:
            print(f"Erreur notification: {e}")

async def send_notification_async(telegram_id, status, message):
    """Fonction asynchrone pour envoyer les notifications"""
    global telegram_app
    try:
        if status == "approved":
            notification = f"""
✅ **Paiement Validé !**

🎉 Félicitations ! Votre preuve de paiement a été approuvée par notre équipe administrative.

💬 **Message de l'admin :** {message}

🚀 **Votre service est maintenant activé !**
⚡ Traitement effectué avec succès
🔔 Transaction confirmée

💡 **Votre transaction :**
• ✅ Statut : Validé et traité
• 🕐 Heure de validation : {datetime.now().strftime('%H:%M')}
• 📅 Date : {datetime.now().strftime('%d/%m/%Y')}

🎯 Points de fidélité et cashback ajoutés automatiquement !

Merci pour votre confiance ! 💫
"""
        else:
            notification = f"""
❌ **Paiement Rejeté**

😔 Désolé, votre preuve de paiement a été refusée par notre équipe.

📋 **Raison du rejet :** {message}

💡 **Que faire maintenant ?**
• 🔍 Vérifiez que votre capture est complète et lisible
• 💰 Assurez-vous que le montant correspond exactement
• 📱 Vérifiez le bon RIP/compte de destination
• 📸 Prenez une nouvelle capture si nécessaire

🔄 **Comment recommencer :**
1. Retournez au menu principal
2. Refaites votre transaction
3. Envoyez une nouvelle preuve claire

📞 **Besoin d'aide ?**
• Support Telegram : @support_bot
• Disponible 24/7
• Réponse rapide garantie

Nous restons à votre disposition ! 🤝
"""

        await telegram_app.bot.send_message(
            chat_id=telegram_id,
            text=notification,
            parse_mode='Markdown'
        )
        
        print(f"✅ Notification envoyée à l'utilisateur {telegram_id} - Statut: {status}")
        
    except Exception as e:
        print(f"❌ Erreur envoi notification: {e}")
        
        # Essayer un message de secours plus simple en cas d'erreur Markdown
        try:
            simple_message = f"🔔 Mise à jour de votre paiement: {status.upper()}\n\n{message}"
            await telegram_app.bot.send_message(
                chat_id=telegram_id,
                text=simple_message
            )
            print(f"✅ Message de secours envoyé à {telegram_id}")
        except Exception as e2:
            print(f"❌ Erreur critique notification: {e2}")

def get_main_menu():
    """Retourne le menu principal"""
    return [
        [KeyboardButton('💳 Acheter USDT'), KeyboardButton('💰 Vendre USDT')],
        [KeyboardButton('📱 Recharger Mobile'), KeyboardButton('🎯 Cashback & Points')],
        [KeyboardButton('💼 Mes Transactions'), KeyboardButton('👑 Devenir VIP')],
        [KeyboardButton('🏆 Parrainage'), KeyboardButton('ℹ️ Support')]
    ]

# Handlers Telegram améliorés
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Commande /start avec gestion VIP"""
    user = update.effective_user
    telegram_id = user.id

    # Vérifier si l'utilisateur existe
    db_user = db.get_user(telegram_id)
    if not db_user:
        referral_code = db.create_user(telegram_id, user.username, user.first_name)
        welcome_msg = f"""
🎉 **Bienvenue sur Bot Multi-Services Premium !**

✅ Votre compte a été créé avec succès !
🎁 Code de parrainage : `{referral_code}`

🌟 **Fonctionnalités Premium :**
• 💳 Achat USDT (Taux dynamique)
• 📱 Recharge multi-opérateurs
• 🎯 Système de fidélité & cashback
• 💰 Codes promo exclusifs
• 🔒 Paiements sécurisés
• 👑 Upgrade VIP disponible

💳 **Méthodes de paiement :**
• CCP, BaridiMob, Crypto, Western Union
• QR codes automatiques
• Vérification en temps réel

Commencez par choisir un service ! 🚀
"""
    else:
        is_vip = db_user[6]
        vip_status = "👑 VIP" if is_vip else "⭐ Standard"
        loyalty_points = db_user[8]
        cashback_balance = db_user[9]

        welcome_msg = f"""
🇩🇿 **Ahlan wa sahlan, {user.first_name}!**

📊 **Votre Profil :**
• Statut : {vip_status}
• Points fidélité : {loyalty_points} pts
• Cashback : {cashback_balance:.2f} DZD

🎯 **Services Premium Disponibles :**
• 💳 Achat USDT avec cashback
• 📱 Recharge instantanée
• 🎁 Codes promo exclusifs
• 💰 Paiements multi-méthodes

Que souhaitez-vous faire aujourd'hui ? 👇
"""

    reply_markup = ReplyKeyboardMarkup(get_main_menu(), resize_keyboard=True)

    await update.message.reply_text(welcome_msg, reply_markup=reply_markup, parse_mode='Markdown')

    # Mettre à jour l'activité
    db.update_user_activity(telegram_id)

    return CHOOSING

async def get_usdt_buy_price():
    """Récupère le prix USDT d'achat (admin ou Binance)"""
    # Récupérer le prix depuis l'admin
    admin_price = db.get_admin_setting('usdt_buy_rate')
    if admin_price:
        return float(admin_price)
    
    # Fallback sur Binance P2P
    try:
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        payload = {
            "asset": "USDT",
            "fiat": "DZD",
            "merchantCheck": True,
            "page": 1,
            "payTypes": [],
            "publisherType": None,
            "rows": 10,
            "tradeType": "BUY"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('data') and len(data['data']) > 0:
                        prices = [float(ad['adv']['price']) for ad in data['data'][:3]]
                        return sum(prices) / len(prices)
                
        return DYNAMIC_RATES['usdt_buy']['rate']
    except:
        return DYNAMIC_RATES['usdt_buy']['rate']

async def get_usdt_sell_price():
    """Récupère le prix USDT de vente (admin ou Binance)"""
    # Récupérer le prix depuis l'admin
    admin_price = db.get_admin_setting('usdt_sell_rate')
    if admin_price:
        return float(admin_price)
    
    # Fallback sur Binance P2P
    try:
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        payload = {
            "asset": "USDT",
            "fiat": "DZD",
            "merchantCheck": True,
            "page": 1,
            "payTypes": [],
            "publisherType": None,
            "rows": 10,
            "tradeType": "SELL"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('data') and len(data['data']) > 0:
                        prices = [float(ad['adv']['price']) for ad in data['data'][:3]]
                        avg_price = sum(prices) / len(prices)
                        return avg_price * 0.97
                
        return DYNAMIC_RATES['usdt_sell']['rate']
    except:
        return DYNAMIC_RATES['usdt_sell']['rate']

async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gestionnaire de choix amélioré avec gestion complète"""
    text = update.message.text
    user_id = update.effective_user.id

    # Acheter USDT
    if '💳' in text and 'acheter' in text.lower():
        # Récupérer le prix d'achat (admin ou Binance)
        buy_price = await get_usdt_buy_price()
        await update.message.reply_text(
            f"💳 **Achat USDT Premium**\n\n"
            f"💰 Taux d'achat : 1 USDT = {buy_price:.2f} DZD\n"
            f"🎯 Cashback : 2% (Standard) | 5% (VIP)\n"
            f"💳 Méthodes : CCP, BaridiMob, Crypto, Western Union\n\n"
            f"Entrez le montant en USDT (minimum 5 USDT) :",
            parse_mode='Markdown'
        )
        context.user_data['service'] = 'usdt_buy'
        context.user_data['usdt_rate'] = buy_price
        return AMOUNT_DETAIL

    # Vendre USDT
    elif '💰' in text and 'vendre' in text.lower():
        # Vérifier les informations de paiement existantes
        user = db.get_user(user_id)
        payout_info = db.get_user_payout_info(user[0]) if user else None
        
        # Récupérer le prix de vente (admin ou Binance)
        sell_price = await get_usdt_sell_price()
        
        if payout_info:
            method_name = {
                'baridimob': 'BaridiMob',
                'ccp': 'CCP',
                'bank': 'Virement Bancaire'
            }.get(payout_info['method'], payout_info['method'])
            
            keyboard = [
                [InlineKeyboardButton(f"✅ Utiliser {method_name}", callback_data="use_existing_payout")],
                [InlineKeyboardButton("🔄 Modifier coordonnées", callback_data="update_payout")],
                [InlineKeyboardButton("📋 Voir détails", callback_data="view_payout")]
            ]
            payout_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"💰 **Vente USDT Premium**\n\n"
                f"💸 Taux de rachat : 1 USDT = {sell_price:.2f} DZD\n"
                f"⏰ Temps limite : 10 minutes chrono\n\n"
                f"📋 **Coordonnées enregistrées :**\n"
                f"💳 Méthode : {method_name}\n\n"
                f"Voulez-vous utiliser ces coordonnées ?",
                reply_markup=payout_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                f"💰 **Vente USDT Premium**\n\n"
                f"💸 Taux de rachat : 1 USDT = {sell_price:.2f} DZD\n"
                f"⏰ Temps limite : 10 minutes chrono\n"
                f"🔹 Paiement rapide via BaridiMob/CCP\n\n"
                f"💡 **Première vente ?**\n"
                f"Nous devons d'abord enregistrer vos coordonnées de paiement.\n\n"
                f"Cliquez ci-dessous pour commencer :",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 Configurer mes coordonnées", callback_data="setup_payout")]
                ]),
                parse_mode='Markdown'
            )
        
        context.user_data['service'] = 'usdt_sell'
        context.user_data['usdt_rate'] = sell_price
        return CHOOSING

    # Recharger Mobile
    elif '📱' in text or 'recharger' in text.lower():
        operators_menu = [
            [KeyboardButton('📱 Djezzy (06)'), KeyboardButton('📞 Mobilis (05)')],
            [KeyboardButton('🌐 Ooredoo (07)'), KeyboardButton('🔙 Retour')]
        ]
        operators_markup = ReplyKeyboardMarkup(operators_menu, resize_keyboard=True)

        await update.message.reply_text(
            "📱 **Recharge Mobile Premium**\n\n"
            "🎯 Cashback automatique sur toutes les recharges\n"
            "⚡ Traitement instantané\n"
            "🔒 Sécurisé et fiable\n\n"
            "Choisissez votre opérateur :",
            reply_markup=operators_markup,
            parse_mode='Markdown'
        )
        context.user_data['service'] = 'mobile'
        return SERVICE_DETAIL

    # Opérateurs Mobile
    elif 'djezzy' in text.lower() or '06' in text:
        context.user_data['operator'] = 'djezzy'
        await update.message.reply_text(
            "📱 **Recharge Djezzy**\n\n"
            "Entrez votre numéro de téléphone Djezzy (06XXXXXXXX) :"
        )
        return PHONE_INPUT

    elif 'mobilis' in text.lower() or '05' in text:
        context.user_data['operator'] = 'mobilis'
        await update.message.reply_text(
            "📞 **Recharge Mobilis**\n\n"
            "Entrez votre numéro de téléphone Mobilis (05XXXXXXXX) :"
        )
        return PHONE_INPUT

    elif 'ooredoo' in text.lower() or '07' in text:
        context.user_data['operator'] = 'ooredoo'
        await update.message.reply_text(
            "🌐 **Recharge Ooredoo**\n\n"
            "Entrez votre numéro de téléphone Ooredoo (07XXXXXXXX) :"
        )
        return PHONE_INPUT

    # Cashback & Points
    elif '🎯' in text or 'cashback' in text.lower() or 'points' in text.lower():
        user = db.get_user(user_id)
        if user:
            loyalty_points = user[8]
            cashback_balance = user[9]
            total_spent = user[10]

            cashback_msg = f"""
🎯 **Votre Système de Fidélité**

💰 **Cashback disponible :** {cashback_balance:.2f} DZD
🏆 **Points de fidélité :** {loyalty_points} pts
💳 **Total dépensé :** {total_spent:.2f} DZD

🎁 **Conversion points :**
• 100 pts = 10 DZD de cashback
• 500 pts = 60 DZD de cashback
• 1000 pts = 150 DZD de cashback

⭐ **Avantages VIP :**
• Cashback x2.5 (5% au lieu de 2%)
• Codes promo exclusifs
• Support prioritaire
• Tarifs préférentiels
"""

            keyboard = []
            if loyalty_points >= 100:
                keyboard.append([InlineKeyboardButton("💰 Convertir 100 pts → 10 DZD", callback_data="convert_100")])
            if loyalty_points >= 500:
                keyboard.append([InlineKeyboardButton("💰 Convertir 500 pts → 60 DZD", callback_data="convert_500")])
            if loyalty_points >= 1000:
                keyboard.append([InlineKeyboardButton("💰 Convertir 1000 pts → 150 DZD", callback_data="convert_1000")])

            keyboard.append([InlineKeyboardButton("🔙 Retour", callback_data="back_main")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(cashback_msg, reply_markup=reply_markup, parse_mode='Markdown')

        return CHOOSING

    # Codes Promo
    elif '💰' in text or 'promo' in text.lower():
        promo_msg = """
🎁 **Codes Promo Actifs**

🎉 **WELCOME10** - 10% de réduction
   • Valable jusqu'au 31/12/2025
   • Tous utilisateurs

👑 **VIP20** - 20% de réduction
   • Réservé aux membres VIP
   • Valable jusqu'au 31/12/2025

🌙 **RAMADAN15** - 15% de réduction
   • Valable jusqu'au 30/04/2025
   • Tous utilisateurs

💡 **Comment utiliser :**
Lors de votre prochaine transaction, entrez le code promo pour bénéficier de la réduction !
"""
        await update.message.reply_text(promo_msg, parse_mode='Markdown')
        return CHOOSING

    # Devenir VIP
    elif '👑' in text or 'vip' in text.lower():
        vip_msg = """
👑 **Membership VIP Premium**

🌟 **Avantages VIP :**
• 🎯 Cashback 5% (au lieu de 2%)
• 🎁 Codes promo exclusifs -20%
• ⚡ Traitement prioritaire
• 💎 Tarifs préférentiels
• 🎪 Accès aux offres spéciales
• 📞 Support VIP 24/7

💰 **Tarifs :**
• 1 mois : 500 DZD
• 3 mois : 1200 DZD (économie 300 DZD)
• 6 mois : 2000 DZD (économie 1000 DZD)
• 1 an : 3500 DZD (économie 2500 DZD)

🎁 **Offre spéciale :** Premier mois à 299 DZD !
"""

        keyboard = [
            [InlineKeyboardButton("👑 Devenir VIP 1 mois", callback_data="vip_1m")],
            [InlineKeyboardButton("💎 Devenir VIP 3 mois", callback_data="vip_3m")],
            [InlineKeyboardButton("🌟 Devenir VIP 6 mois", callback_data="vip_6m")],
            [InlineKeyboardButton("🔥 Devenir VIP 1 an", callback_data="vip_1y")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(vip_msg, reply_markup=reply_markup, parse_mode='Markdown')
        return CHOOSING

    # Parrainage
    elif '🏆' in text or 'parrainage' in text.lower():
        user = db.get_user(user_id)
        if user:
            referral_code = user[11]
            referral_msg = f"""
🏆 **Programme de Parrainage**

🎁 **Votre code :** `{referral_code}`

💰 **Récompenses :**
• 50 DZD pour chaque filleul
• 100 points de fidélité bonus
• 5% de commission sur leurs transactions

📊 **Votre Performance :**
• Filleuls actifs : 0
• Commissions gagnées : 0 DZD
• Bonus reçus : 0 DZD

🚀 **Comment ça marche :**
1. Partagez votre code avec vos amis
2. Ils s'inscrivent avec /start {referral_code}
3. Vous gagnez des récompenses automatiquement !
"""
            await update.message.reply_text(referral_msg, parse_mode='Markdown')

        return CHOOSING

    # Mes Transactions
    elif '💼' in text or 'transactions' in text.lower():
        user = db.get_user(user_id)
        if user:
            transactions = db.get_user_transactions(user[0], 5)
            if transactions:
                history_text = "💼 **Historique des Transactions**\n\n"
                for tx in transactions:
                    status_emoji = "✅" if tx[7] == "completed" else "⏳" if tx[7] == "pending" else "❌"
                    history_text += f"{status_emoji} **{tx[3]}** - {tx[4]:.2f} DZD\n"
                    history_text += f"   📅 {tx[14]}\n"
                    if tx[8]:
                        history_text += f"   📱 {tx[8]}\n"
                    history_text += "\n"
            else:
                history_text = "💼 Aucune transaction trouvée.\n\nCommencez par faire votre première transaction ! 🚀"
        else:
            history_text = "❌ Erreur lors de la récupération de vos données."

        await update.message.reply_text(history_text, parse_mode='Markdown')
        return CHOOSING

    # Support
    elif 'ℹ️' in text or 'support' in text.lower():
        support_keyboard = [
            [InlineKeyboardButton("🤖 Chat Intelligent", callback_data="smart_chat")],
            [InlineKeyboardButton("📞 Support Humain", callback_data="human_support")],
            [InlineKeyboardButton("❓ FAQ", callback_data="faq")],
            [InlineKeyboardButton("🔙 Retour", callback_data="back_main")]
        ]
        support_markup = InlineKeyboardMarkup(support_keyboard)
        
        support_msg = """
ℹ️ **Centre d'Aide Intelligent**

🤖 **Chat Intelligent IA**
• Réponses instantanées 24/7
• Solutions automatiques
• Aide contextuelle personnalisée

📞 **Support Humain Premium**
• Agents spécialisés
• Résolution de problèmes complexes
• Support VIP prioritaire

❓ **Questions Fréquentes**
• Solutions aux problèmes courants
• Guides détaillés
• Procédures pas-à-pas

Choisissez votre type d'assistance :
"""
        
        await update.message.reply_text(
            support_msg, 
            reply_markup=support_markup, 
            parse_mode='Markdown'
        )
        return CHOOSING

    # Retour au menu principal
    elif '🔙' in text or 'retour' in text.lower():
        reply_markup = ReplyKeyboardMarkup(get_main_menu(), resize_keyboard=True)
        await update.message.reply_text(
            "🏠 **Menu Principal**\n\nChoisissez une option :",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return CHOOSING

    # Si aucune option reconnue
    else:
        reply_markup = ReplyKeyboardMarkup(get_main_menu(), resize_keyboard=True)
        await update.message.reply_text(
            "❓ **Option non reconnue**\n\n"
            "Veuillez utiliser les boutons du menu ci-dessous pour naviguer :",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return CHOOSING

async def handle_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère la saisie du numéro de téléphone"""
    phone = update.message.text.strip()

    if validate_algerian_phone(phone):
        context.user_data['phone'] = phone
        formatted_phone = format_phone_number(phone)

        amounts_menu = [
            [KeyboardButton('100 DZD'), KeyboardButton('200 DZD'), KeyboardButton('500 DZD')],
            [KeyboardButton('1000 DZD'), KeyboardButton('200 DZD'), KeyboardButton('Autre montant')],
            [KeyboardButton('🔙 Retour')]
        ]
        amounts_markup = ReplyKeyboardMarkup(amounts_menu, resize_keyboard=True)

        await update.message.reply_text(
            f"✅ **Numéro validé :** {formatted_phone}\n"
            f"📱 **Opérateur :** {context.user_data.get('operator', 'N/A').title()}\n\n"
            f"💰 Choisissez le montant de recharge :",
            reply_markup=amounts_markup,
            parse_mode='Markdown'
        )
        return AMOUNT_DETAIL
    else:
        await update.message.reply_text(
            "❌ **Numéro invalide**\n\n"
            "Veuillez entrer un numéro algérien valide :\n"
            "• Format : 05XXXXXXXX, 06XXXXXXXX, 07XXXXXXXX\n"
            "• Ou avec indicatif : +213XXXXXXXXX\n\n"
            "Essayez à nouveau :"
        )
        return PHONE_INPUT

async def handle_amount_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère les détails du montant"""
    text = update.message.text.strip()
    service = context.user_data.get('service', 'unknown')

    # Retour au menu
    if '🔙' in text or 'retour' in text.lower():
        return await handle_choice(update, context)

    # Extraction du montant
    amount = None

    # Si c'est un bouton prédéfini
    if 'DZD' in text:
        try:
            amount = float(text.split()[0])
        except:
            pass

    # Si c'est "Autre montant"
    elif 'autre' in text.lower():
        if service == 'usdt_buy':
            await update.message.reply_text(
                "💰 **Montant personnalisé**\n\n"
                "Entrez le montant en USDT (minimum 5 USDT) :"
            )
        elif service == 'usdt_sell':
            await update.message.reply_text(
                "💰 **Montant personnalisé**\n\n"
                "Entrez le montant en USDT à vendre (minimum 10 USDT) :"
            )
        else:
            await update.message.reply_text(
                "💰 **Montant personnalisé**\n\n"
                "Entrez le montant souhaité en DZD (minimum 50 DZD) :"
            )
        return AMOUNT_DETAIL

    # Si c'est un nombre saisi directement
    else:
        try:
            amount = float(text)
        except:
            await update.message.reply_text(
                "❌ **Montant invalide**\n\n"
                "Veuillez entrer un montant valide ou utiliser les boutons :"
            )
            return AMOUNT_DETAIL

    # Validation du montant selon le service
    min_amount = 50
    amount_valid = False

    if service == 'usdt_buy':
        min_amount = 5
        amount_valid = amount >= 5
    elif service == 'usdt_sell':
        min_amount = 10
        amount_valid = amount >= 10
    else:
        amount_valid = amount >= 50

    if amount and amount_valid:
        context.user_data['amount'] = amount

        # Calculer les détails selon le service
        if service == 'usdt_buy':
            rate = context.user_data.get('usdt_rate', DYNAMIC_RATES['usdt']['rate'])
            total_dzd = amount * rate
            cashback = loyalty_system.calculate_cashback(update.effective_user.id, total_dzd, 'usdt')

            summary = f"""
💳 **Récapitulatif Achat USDT**

💎 **Montant USDT :** {amount:.4f} USDT
🔄 **Taux Binance P2P :** 1 USDT = {rate:.2f} DZD
💰 **Total à payer :** {total_dzd:.2f} DZD
🎯 **Cashback :** {cashback:.2f} DZD
🏆 **Points fidélité :** +{int(total_dzd)} pts

💳 **Méthodes de paiement disponibles :**
"""
            context.user_data['total_amount'] = total_dzd

        elif service == 'usdt_sell':
            rate = context.user_data.get('usdt_rate', DYNAMIC_RATES['usdt_sell']['rate'])
            total_dzd = amount * rate
            usdt_address = db.get_admin_setting('usdt_address') or USDT_RECEIVING_ADDRESS

            summary = f"""
💰 **Récapitulatif Vente USDT**

💎 **Montant à vendre :** {amount:.4f} USDT
🔄 **Taux de rachat :** 1 USDT = {rate:.2f} DZD
💸 **Vous recevrez :** {total_dzd:.2f} DZD
🏆 **Points fidélité :** +{int(total_dzd * 0.5)} pts

📍 **Adresse de réception USDT :**
`{usdt_address}`

⏰ **Important : Temps limite de 10 minutes !**
🔸 Envoyez vos USDT à l'adresse ci-dessus
🔸 Fournissez le hash de transaction
🔸 Recevez votre paiement DZD rapidement

💳 **Vous recevrez le paiement via :**
• 📱 BaridiMob (instantané)
• 🏦 CCP (1-2h)
"""
            context.user_data['total_amount'] = total_dzd

        else:
            operator = context.user_data.get('operator', 'unknown')
            phone = context.user_data.get('phone', 'N/A')
            cashback = loyalty_system.calculate_cashback(update.effective_user.id, amount, operator)

            summary = f"""
📱 **Récapitulatif Recharge Mobile**

📞 **Numéro :** {format_phone_number(phone)}
📱 **Opérateur :** {operator.title()}
💰 **Montant :** {amount:.2f} DZD
🎯 **Cashback :** {cashback:.2f} DZD
🏆 **Points fidélité :** +{int(amount * 0.5)} pts

💳 **Méthodes de paiement disponibles :**
"""
            context.user_data['total_amount'] = amount

        # Boutons de paiement
        payment_keyboard = [
            [InlineKeyboardButton("🏦 CCP", callback_data="pay_ccp")],
            [InlineKeyboardButton("📱 BaridiMob", callback_data="pay_baridimob")],
            [InlineKeyboardButton("💎 Crypto (USDT)", callback_data="pay_crypto")],
            [InlineKeyboardButton("🌍 Western Union", callback_data="pay_western")],
            [InlineKeyboardButton("🎁 Utiliser code promo", callback_data="use_promo")],
            [InlineKeyboardButton("✅ Confirmer", callback_data="confirm_payment")],
            [InlineKeyboardButton("❌ Annuler", callback_data="cancel_payment")]
        ]
        payment_markup = InlineKeyboardMarkup(payment_keyboard)

        await update.message.reply_text(
            summary,
            reply_markup=payment_markup,
            parse_mode='Markdown'
        )
        return CONFIRMATION

    else:
        if service == 'usdt_buy':
            error_msg = f"❌ **Montant trop faible**\n\nLe montant minimum est de {min_amount} USDT.\nVeuillez entrer un montant valide :"
        elif service == 'usdt_sell':
            error_msg = f"❌ **Montant trop faible**\n\nLe montant minimum est de {min_amount} USDT.\nVeuillez entrer un montant valide :"
        else:
            error_msg = f"❌ **Montant trop faible**\n\nLe montant minimum est de {min_amount} DZD.\nVeuillez entrer un montant valide :"

        await update.message.reply_text(error_msg)
        return AMOUNT_DETAIL

async def start_countdown_timer(context: ContextTypes.DEFAULT_TYPE, sale_id: int, telegram_id: int, duration: int = 600):
    """Démarre un compte à rebours pour une vente USDT"""
    try:
        # Messages de compte à rebours à intervalles spécifiques
        countdown_intervals = [300, 120, 60, 30]  # 5min, 2min, 1min, 30sec
        
        for interval in countdown_intervals:
            if duration > interval:
                await asyncio.sleep(duration - interval)
                duration = interval
                
                minutes = interval // 60
                seconds = interval % 60
                
                if minutes > 0:
                    time_text = f"{minutes} minute{'s' if minutes > 1 else ''}"
                    if seconds > 0:
                        time_text += f" et {seconds} seconde{'s' if seconds > 1 else ''}"
                else:
                    time_text = f"{seconds} seconde{'s' if seconds > 1 else ''}"
                
                warning_msg = f"""
⏰ **ATTENTION - Temps limité !**

🚨 Il vous reste seulement **{time_text}** pour compléter votre vente USDT #{sale_id}

📤 **Actions requises :**
• Envoyez vos USDT à l'adresse fournie
• Fournissez le hash de transaction
• Respectez le délai pour éviter l'annulation

⚡ **Dépêchez-vous pour sécuriser votre transaction !**
"""
                
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text=warning_msg,
                    parse_mode='Markdown'
                )
        
        # Message final d'expiration
        await asyncio.sleep(duration)
        
        # Vérifier si la transaction est toujours en attente
        sale = db.get_usdt_sale(sale_id)
        if sale and sale[8] in ['waiting_send', 'hash_provided']:
            # Marquer comme expiré
            db.reject_usdt_sale(sale_id, 0)
            
            expiry_msg = f"""
❌ **Transaction Expirée !**

🕐 Votre vente USDT #{sale_id} a expiré après 10 minutes.

💔 **Transaction annulée automatiquement**
💰 Montant : {sale[2]:.4f} USDT
💸 Valeur : {sale[4]:.2f} DZD

🔄 **Pour recommencer :**
• Retournez au menu principal
• Sélectionnez "Vendre USDT"
• Suivez les étapes plus rapidement

💡 **Conseil :** Préparez vos USDT avant de commencer la transaction !
"""
            
            await context.bot.send_message(
                chat_id=telegram_id,
                text=expiry_msg,
                parse_mode='Markdown'
            )
            
    except Exception as e:
        print(f"Erreur countdown timer: {e}")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère les callbacks des boutons inline"""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data

    # Conversion de points
    if data.startswith('convert_'):
        points = int(data.split('_')[1])
        success, cashback_amount = db.convert_points_to_cashback(user_id, points)

        if success:
            await query.edit_message_text(
                f"✅ **Conversion réussie !**\n\n"
                f"🏆 {points} points convertis\n"
                f"💰 +{cashback_amount:.2f} DZD ajoutés à votre cashback\n\n"
                f"Votre cashback est maintenant utilisable pour vos prochaines transactions ! 🎉",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                "❌ **Conversion échouée**\n\n"
                "Points insuffisants pour cette conversion.",
                parse_mode='Markdown'
            )

        return CHOOSING

    # Chat intelligent
    elif data == 'smart_chat':
        await query.edit_message_text(
            "🤖 **Assistant IA Intelligent**\n\n"
            "Bonjour ! Je suis votre assistant intelligent.\n"
            "Décrivez votre problème ou votre question et je vous aiderai immédiatement !\n\n"
            "💡 **Exemples de questions :**\n"
            "• Comment acheter des USDT ?\n"
            "• Ma transaction est bloquée\n"
            "• Comment devenir VIP ?\n"
            "• Problème avec BaridiMob\n\n"
            "Tapez votre question :",
            parse_mode='Markdown'
        )
        
        # Enregistrer que l'utilisateur est en chat intelligent
        context.user_data['smart_chat_active'] = True
        return SUPPORT_CHAT

    # Support humain
    elif data == 'human_support':
        await query.edit_message_text(
            "📞 **Support Humain Premium**\n\n"
            "Vous allez être mis en relation avec un agent spécialisé.\n\n"
            "📝 **Décrivez votre problème :**\n"
            "Soyez le plus précis possible pour une résolution rapide.\n\n"
            "⏰ **Délai de réponse :**\n"
            "• Standard : 15-30 minutes\n"
            "• VIP : 5-10 minutes\n\n"
            "Tapez votre message :",
            parse_mode='Markdown'
        )
        
        # Enregistrer que l'utilisateur veut un support humain
        context.user_data['human_support_active'] = True
        return SUPPORT_CHAT

    # Configuration des coordonnées de paiement
    elif data == 'setup_payout' or data == 'update_payout':
        keyboard = [
            [InlineKeyboardButton("📱 BaridiMob", callback_data="payout_baridimob")],
            [InlineKeyboardButton("🏦 CCP", callback_data="payout_ccp")],
            [InlineKeyboardButton("🏛️ Virement Bancaire", callback_data="payout_bank")],
            [InlineKeyboardButton("🔙 Retour", callback_data="back_main")]
        ]
        payout_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "💳 **Configuration Coordonnées de Paiement**\n\n"
            "Choisissez votre méthode de réception préférée :\n\n"
            "📱 **BaridiMob** - Instantané\n"
            "🏦 **CCP** - 1-2 heures\n"
            "🏛️ **Virement Bancaire** - 24-48h\n\n"
            "⚡ **Recommandé :** BaridiMob pour des paiements instantanés",
            reply_markup=payout_markup,
            parse_mode='Markdown'
        )
        return PAYOUT_INFO

    # Méthodes de paiement spécifiques
    elif data.startswith('payout_'):
        method = data.split('_')[1]
        context.user_data['payout_method'] = method
        
        if method == 'baridimob':
            await query.edit_message_text(
                "📱 **Configuration BaridiMob**\n\n"
                "Veuillez fournir vos informations BaridiMob :\n\n"
                "📋 **Format attendu :**\n"
                "Nom complet: [Votre nom]\n"
                "Numéro: [0XXXXXXXXX]\n"
                "RIP: [Votre RIP si disponible]\n\n"
                "💡 **Exemple :**\n"
                "Nom complet: Ahmed Benali\n"
                "Numéro: 0555123456\n"
                "RIP: 0012345678901234567890\n\n"
                "Tapez vos informations :",
                parse_mode='Markdown'
            )
        elif method == 'ccp':
            await query.edit_message_text(
                "🏦 **Configuration CCP**\n\n"
                "Veuillez fournir vos informations CCP :\n\n"
                "📋 **Format attendu :**\n"
                "Nom complet: [Votre nom]\n"
                "Numéro CCP: [Votre numéro]\n"
                "Clé: [Votre clé]\n"
                "Wilaya: [Votre wilaya]\n\n"
                "💡 **Exemple :**\n"
                "Nom complet: Ahmed Benali\n"
                "Numéro CCP: 1234567890\n"
                "Clé: 12\n"
                "Wilaya: Alger\n\n"
                "Tapez vos informations :",
                parse_mode='Markdown'
            )
        elif method == 'bank':
            await query.edit_message_text(
                "🏛️ **Configuration Virement Bancaire**\n\n"
                "Veuillez fournir vos informations bancaires :\n\n"
                "📋 **Format attendu :**\n"
                "Nom complet: [Votre nom]\n"
                "Banque: [Nom de la banque]\n"
                "RIB: [Votre RIB complet]\n"
                "Agence: [Agence]\n\n"
                "💡 **Exemple :**\n"
                "Nom complet: Ahmed Benali\n"
                "Banque: BNA\n"
                "RIB: 0123456789012345678901234\n"
                "Agence: Alger Centre\n\n"
                "Tapez vos informations :",
                parse_mode='Markdown'
            )
        
        return PAYOUT_INFO

    # Utiliser coordonnées existantes
    elif data == 'use_existing_payout':
        await query.edit_message_text(
            "✅ **Coordonnées confirmées !**\n\n"
            "💰 Entrez maintenant le montant en USDT que vous souhaitez vendre :\n"
            "(Minimum 10 USDT)",
            parse_mode='Markdown'
        )
        return AMOUNT_DETAIL

    # Retour au menu principal
    elif data == 'back_main':
        reply_markup = ReplyKeyboardMarkup(get_main_menu(), resize_keyboard=True)
        await query.edit_message_text(
            "🏠 **Menu Principal**\n\nChoisissez une option :",
            parse_mode='Markdown'
        )
        return CHOOSING

    # Méthodes de paiement
    elif data.startswith('pay_'):
        method = data.split('_')[1]
        context.user_data['payment_method'] = method

        method_info = PAYMENT_METHODS.get(method, {})
        method_name = method_info.get('name', method.upper())
        fee_rate = method_info.get('fee', 0) * 100

        # Instructions spécifiques pour BaridiMob
        if method == 'baridimob':
            payment_instructions = f"""
💳 **Méthode sélectionnée : {method_name}**

📱 **RIP BaridiMob :** `0799999002264673222`

📋 **Instructions :**
1. Ouvrez votre application BaridiMob
2. Choisissez "Transfert vers RIP"
3. Saisissez le RIP : 0799999002264673222
4. Entrez le montant et validez
5. Envoyez-nous la preuve de paiement

💰 Frais de transaction : {fee_rate}%
⚡ Traitement : Instantané

Confirmez-vous cette méthode de paiement ?
"""
        else:
            payment_instructions = f"""
💳 **Méthode sélectionnée : {method_name}**

💰 Frais de transaction : {fee_rate}%
⚡ Traitement : Instantané

Confirmez-vous cette méthode de paiement ?
"""

        await query.edit_message_text(
            payment_instructions,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmer", callback_data="confirm_method")],
                [InlineKeyboardButton("🔙 Changer", callback_data="back_payment")]
            ]),
            parse_mode='Markdown'
        )
        return CONFIRMATION

    # Confirmation de méthode
    elif data == 'confirm_method':
        method = context.user_data.get('payment_method')
        amount = context.user_data.get('amount', 0)
        total_amount = context.user_data.get('total_amount', amount)
        service = context.user_data.get('service', 'unknown')

        # Créer la transaction
        user = db.get_user(user_id)
        if user:
            # Gestion spéciale pour la vente USDT
            if service == 'usdt_sell':
                rate = context.user_data.get('usdt_rate', DYNAMIC_RATES['usdt_sell']['rate'])
                sale_id = db.create_usdt_sale(user[0], user_id, amount, rate)
                sale = db.get_usdt_sale(sale_id)
                
                usdt_address = sale[7]  # usdt_address
                expires_at = sale[9]    # expires_at
                
                # Calculer le temps restant
                expire_time = datetime.fromisoformat(expires_at.replace('Z', '+00:00')) if isinstance(expires_at, str) else expires_at
                time_left = int((expire_time - datetime.now()).total_seconds())
                minutes_left = time_left // 60
                
                payment_text = f"""
💰 **Vente USDT Initiée !**

🆔 **ID Vente :** `{sale_id}`
💎 **Montant USDT :** {amount:.4f} USDT
💸 **Vous recevrez :** {total_amount:.2f} DZD

📍 **Adresse USDT (TRC20) :**
`{usdt_address}`

⏰ **URGENT - Temps restant : {minutes_left} minutes**

📋 **Étapes à suivre :**
1. 📱 Ouvrez votre wallet USDT
2. 📤 Envoyez {amount:.4f} USDT à l'adresse ci-dessus
3. ⚠️ Réseau obligatoire : TRC20 (Tron)
4. 🧾 Copiez le hash de transaction
5. 📝 Envoyez le hash via le bouton ci-dessous

🚨 **ATTENTION :**
• ⏰ Temps limite : 10 minutes maximum
• 🔗 Utilisez uniquement le réseau TRC20
• 💰 Paiement DZD après vérification

Envoyez votre hash de transaction :
"""
                
                keyboard = [
                    [InlineKeyboardButton("📤 Envoyer Hash Transaction", callback_data=f"send_hash_{sale_id}")],
                    [InlineKeyboardButton("📋 Copier Adresse", callback_data=f"copy_address_{sale_id}")],
                    [InlineKeyboardButton("❓ Aide", callback_data="help_usdt_sale")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    payment_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                
                context.user_data['sale_id'] = sale_id
                
                # Démarrer le compte à rebours de 10 minutes
                timeout = int(db.get_admin_setting('transaction_timeout') or TRANSACTION_TIMEOUT)
                asyncio.create_task(start_countdown_timer(context, sale_id, user_id, timeout))
                
                return PAYMENT_PROOF
            
            else:
                # Gestion normale pour achat USDT et autres services
                transaction_id = db.create_transaction(
                    user[0], 
                    service,
                    total_amount,
                    context.user_data.get('phone'),
                    context.user_data.get('operator')
                )

                # Traiter le paiement
                payment_info = payment_processor.process_payment(transaction_id, method, total_amount)

                # Instructions spécifiques selon la méthode
                if method == 'baridimob':
                    payment_text = f"""
✅ **Transaction créée avec succès !**

🆔 **ID Transaction :** `{transaction_id}`
💳 **RIP BaridiMob :** `0799999002264673222`
💰 **Montant à transférer :** {payment_info['total']:.2f} DZD

📋 **Instructions BaridiMob :**
1. Ouvrez votre app BaridiMob
2. "Transfert" → "Vers RIP"
3. RIP : 0799999002264673222
4. Montant : {payment_info['total']:.2f} DZD
5. Validez et prenez une capture

⏰ **Délai de traitement :** 5-15 minutes
🔔 **Confirmation automatique après vérification**

Envoyez votre preuve de paiement (capture d'écran) :
"""
                else:
                    payment_text = f"""
✅ **Transaction créée avec succès !**

🆔 **ID Transaction :** `{transaction_id}`
💳 **Référence de paiement :** `{payment_info['reference']}`
💰 **Montant :** {amount:.2f} DZD
📊 **Frais :** {payment_info['fee']:.2f} DZD
💎 **Total à payer :** {payment_info['total']:.2f} DZD

📋 **Instructions de paiement :**
Utilisez la référence ci-dessus pour effectuer votre paiement.

⏰ **Délai de traitement :** 5-15 minutes
🔔 **Vous recevrez une confirmation automatique**

Envoyez votre preuve de paiement (capture d'écran) :
"""

                keyboard = [
                    [InlineKeyboardButton("📸 Envoyer preuve", callback_data="send_proof")],
                    [InlineKeyboardButton("❓ Aide", callback_data="help_payment")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.edit_message_text(
                    payment_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )

                # Ajouter des points de fidélité
                points = loyalty_system.calculate_loyalty_points(amount, service)
                db.add_loyalty_points(user_id, points)

                return PAYMENT_PROOF

    # Bouton "Envoyer preuve"
    elif data == 'send_proof' or data.startswith('send_proof_vip'):
        await query.edit_message_text(
            "📸 **Envoi de preuve de paiement**\n\n"
            "Veuillez envoyer votre capture d'écran de paiement :\n\n"
            "📋 **Formats acceptés :**\n"
            "• JPEG (.jpg)\n"
            "• PNG (.png)\n\n"
            "📱 **Comment faire :**\n"
            "1. Prenez une capture d'écran de votre confirmation de paiement\n"
            "2. Cliquez sur l'icône 📎 (trombone) dans Telegram\n"
            "3. Sélectionnez 'Galerie' ou 'Fichiers'\n"
            "4. Choisissez votre capture et envoyez\n\n"
            "⚡ **La vérification se fera automatiquement !**",
            parse_mode='Markdown'
        )
        
        # Marquer qu'on attend une preuve
        context.user_data['waiting_for_proof'] = True
        if data.startswith('send_proof_vip'):
            vip_duration = data.split('_')[-1]  # 1m, 3m, 6m, 1y
            context.user_data['vip_purchase'] = vip_duration
        
        return PAYMENT_PROOF

    # Envoyer hash de transaction USDT
    elif data.startswith('send_hash_'):
        sale_id = int(data.split('_')[2])
        context.user_data['waiting_for_hash'] = True
        context.user_data['sale_id'] = sale_id
        
        await query.edit_message_text(
            "📤 **Envoi du Hash de Transaction**\n\n"
            "Envoyez maintenant le hash de votre transaction USDT :\n\n"
            "📋 **Format attendu :**\n"
            "• Hash de transaction complet\n"
            "• Réseau TRC20 uniquement\n"
            "• Exemple : a1b2c3d4e5f6g7h8i9j0...\n\n"
            "💡 **Où trouver le hash :**\n"
            "• Dans votre wallet après envoi\n"
            "• Dans l'historique des transactions\n"
            "• Sur l'explorateur blockchain\n\n"
            "⏰ **Temps restant limité !**",
            parse_mode='Markdown'
        )
        return PAYMENT_PROOF

    # Copier adresse USDT
    elif data.startswith('copy_address_'):
        sale_id = int(data.split('_')[2])
        sale = db.get_usdt_sale(sale_id)
        
        if sale:
            await query.answer(f"Adresse copiée : {sale[7]}", show_alert=True)
        else:
            await query.answer("Erreur lors de la récupération de l'adresse", show_alert=True)
        return PAYMENT_PROOF

    # Aide pour la vente USDT
    elif data == 'help_usdt_sale':
        help_text = """
❓ **Aide Vente USDT**

📋 **Étapes détaillées :**
1. 📱 Ouvrez votre wallet USDT (TronLink, Trust Wallet, etc.)
2. 📤 Choisissez "Envoyer" ou "Send"
3. 🔗 Sélectionnez le réseau TRC20 (Tron)
4. 📍 Collez l'adresse de destination fournie
5. 💎 Entrez le montant exact en USDT
6. ✅ Validez et confirmez la transaction
7. 📋 Copiez le hash de transaction
8. 📤 Envoyez le hash via le bouton correspondant

⚠️ **IMPORTANT :**
• Utilisez uniquement le réseau TRC20
• Vérifiez l'adresse avant d'envoyer
• Gardez le hash de transaction
• Respectez le délai de 10 minutes

🆘 **En cas de problème :**
• Contactez le support : @support_bot
• Vérifiez le réseau (TRC20)
• Vérifiez l'adresse de destination
"""

        await query.edit_message_text(
            help_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Retour", callback_data="back_transaction")]
            ]),
            parse_mode='Markdown'
        )
        return PAYMENT_PROOF

    # Aide pour le paiement
    elif data == 'help_payment':
        method = context.user_data.get('payment_method', 'baridimob')
        
        if method == 'baridimob':
            help_text = """
❓ **Aide Paiement BaridiMob**

📋 **Étapes détaillées :**
1. Ouvrez l'app BaridiMob sur votre téléphone
2. Connectez-vous avec vos identifiants
3. Sélectionnez "Transfert"
4. Choisissez "Vers RIP"
5. Saisissez le RIP : 0799999002264673222
6. Entrez le montant exact
7. Validez la transaction
8. Prenez une capture de la confirmation
9. Envoyez la capture via le bouton "Envoyer preuve"

💡 **Important :**
• Vérifiez bien le RIP avant de valider
• Le montant doit être exact
• La capture doit être lisible et complète
"""
        else:
            help_text = """
❓ **Aide Paiement**

📋 **Étapes à suivre :**
1. Effectuez le paiement avec la référence fournie
2. Prenez une capture d'écran de la confirmation
3. Envoyez la capture via le bouton 'Envoyer preuve'
4. Attendez la validation (5-15 min)

💡 **Conseils :**
• Vérifiez bien la référence de paiement
• La capture doit être lisible
• Contactez le support en cas de problème
"""

        await query.edit_message_text(
            help_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Retour", callback_data="back_transaction")]
            ]),
            parse_mode='Markdown'
        )
        return PAYMENT_PROOF

    # Annulation
    elif data == 'cancel_payment':
        reply_markup = ReplyKeyboardMarkup(get_main_menu(), resize_keyboard=True)
        await query.edit_message_text(
            "❌ **Transaction annulée**\n\n"
            "Aucune charge n'a été effectuée.\n"
            "Retour au menu principal.",
            parse_mode='Markdown'
        )
        return CHOOSING

    # Options VIP
    elif data == "vip_1m":
        context.user_data['vip_duration'] = '1m'
        context.user_data['vip_amount'] = 500
        await query.edit_message_text(
            "👑 **VIP 1 mois - 500 DZD**\n\n"
            "💳 **Paiement via BaridiMob :**\n"
            "📱 **RIP :** `0799999002264673222`\n"
            "💰 **Montant :** 500 DZD\n\n"
            "Confirmez-vous cet achat VIP ?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmer et Payer", callback_data="confirm_vip_1m")],
                [InlineKeyboardButton("❌ Annuler", callback_data="cancel_vip")]
            ]),
            parse_mode='Markdown'
        )
        return CHOOSING

    elif data == "vip_3m":
        context.user_data['vip_duration'] = '3m'
        context.user_data['vip_amount'] = 1200
        await query.edit_message_text(
            "💎 **VIP 3 mois - 1200 DZD**\n\n"
            "💳 **Paiement via BaridiMob :**\n"
            "📱 **RIP :** `0799999002264673222`\n"
            "💰 **Montant :** 1200 DZD\n\n"
            "Confirmez-vous cet achat VIP ?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmer et Payer", callback_data="confirm_vip_3m")],
                [InlineKeyboardButton("❌ Annuler", callback_data="cancel_vip")]
            ]),
            parse_mode='Markdown'
        )
        return CHOOSING

    elif data == "vip_6m":
        context.user_data['vip_duration'] = '6m'
        context.user_data['vip_amount'] = 2000
        await query.edit_message_text(
            "🌟 **VIP 6 mois - 2000 DZD**\n\n"
            "💳 **Paiement via BaridiMob :**\n"
            "📱 **RIP :** `0799999002264673222`\n"
            "💰 **Montant :** 2000 DZD\n\n"
            "Confirmez-vous cet achat VIP ?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmer et Payer", callback_data="confirm_vip_6m")],
                [InlineKeyboardButton("❌ Annuler", callback_data="cancel_vip")]
            ]),
            parse_mode='Markdown'
        )
        return CHOOSING

    elif data == "vip_1y":
        context.user_data['vip_duration'] = '1y'
        context.user_data['vip_amount'] = 3500
        await query.edit_message_text(
            "🔥 **VIP 1 an - 3500 DZD**\n\n"
            "💳 **Paiement via BaridiMob :**\n"
            "📱 **RIP :** `0799999002264673222`\n"
            "💰 **Montant :** 3500 DZD\n\n"
            "Confirmez-vous cet achat VIP ?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmer et Payer", callback_data="confirm_vip_1y")],
                [InlineKeyboardButton("❌ Annuler", callback_data="cancel_vip")]
            ]),
            parse_mode='Markdown'
        )
        return CHOOSING
        
    elif data == "cancel_vip":
        reply_markup = ReplyKeyboardMarkup(get_main_menu(), resize_keyboard=True)
        await query.edit_message_text(
            "❌ **Achat VIP annulé.**\n\n"
            "Retour au menu principal.",
            parse_mode='Markdown'
        )
        return CHOOSING

    # Confirmations VIP avec instructions BaridiMob
    elif data.startswith("confirm_vip_"):
        duration = data.split('_')[-1]
        amount = context.user_data.get('vip_amount', 0)
        
        await query.edit_message_text(
            f"✅ **Achat VIP confirmé !**\n\n"
            f"💳 **Instructions de paiement BaridiMob :**\n\n"
            f"📱 **RIP à utiliser :** `0799999002264673222`\n"
            f"💰 **Montant exact :** {amount} DZD\n\n"
            f"📋 **Étapes :**\n"
            f"1. Ouvrez BaridiMob\n"
            f"2. Transfert → Vers RIP\n"
            f"3. RIP : 0799999002264673222\n"
            f"4. Montant : {amount} DZD\n"
            f"5. Validez et prenez une capture\n\n"
            f"Envoyez ensuite votre preuve de paiement :",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📸 Envoyer preuve", callback_data=f"send_proof_vip_{duration}")],
                [InlineKeyboardButton("❓ Aide", callback_data="help_payment")]
            ]),
            parse_mode='Markdown'
        )
        return PAYMENT_PROOF

    return CHOOSING

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère la saisie de texte intelligente selon le contexte"""
    text = update.message.text.strip()
    
    # Gestion du hash de transaction USDT
    if context.user_data.get('waiting_for_hash', False):
        if len(text) >= 20:  # Hash minimum
            sale_id = context.user_data.get('sale_id')
            if sale_id:
                # Enregistrer le hash
                db.update_usdt_sale_hash(sale_id, text)
                
                confirmation_msg = f"""
✅ **Hash de Transaction Reçu !**

🆔 **ID Vente :** #{sale_id}
🔗 **Hash :** `{text[:20]}...`
📤 **Statut :** Hash fourni, en cours de vérification

🔍 **Vérification en cours :**
• Notre équipe vérifie votre transaction USDT
• Délai de traitement : 5-30 minutes
• Vous recevrez une notification automatique

💰 **Prochaines étapes :**
• Vérification de la transaction sur la blockchain
• Validation du montant et du réseau
• Transfert de votre paiement DZD

📞 **Support :** @support_bot disponible 24/7

Merci pour votre transaction ! 🎉
"""
                
                await update.message.reply_text(confirmation_msg, parse_mode='Markdown')
                
                # Nettoyer les données temporaires
                context.user_data.clear()
                
                # Retour au menu principal
                reply_markup = ReplyKeyboardMarkup(get_main_menu(), resize_keyboard=True)
                await update.message.reply_text(
                    "🏠 **Menu Principal**\n\n"
                    "Vous pouvez effectuer une nouvelle transaction si vous le souhaitez.",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                
                return CHOOSING
        else:
            await update.message.reply_text(
                "❌ **Hash invalide**\n\n"
                "Le hash de transaction doit contenir au moins 20 caractères.\n"
                "Veuillez envoyer un hash valide :",
                parse_mode='Markdown'
            )
            return PAYMENT_PROOF
    
    # Vérifier si c'est un montant numérique pour USDT
    try:
        amount = float(text)
        service = context.user_data.get('service', '')
        
        # Si c'est un montant pour USDT, traiter comme amount_detail
        if service in ['usdt_buy', 'usdt_sell'] and amount > 0:
            context.user_data['amount'] = amount
            return await handle_amount_detail(update, context)
        
        # Si c'est un montant pour mobile et qu'on a un phone
        elif service == 'mobile' and context.user_data.get('phone') and amount >= 50:
            context.user_data['amount'] = amount
            return await handle_amount_detail(update, context)
            
    except ValueError:
        # Ce n'est pas un nombre, continuer avec le traitement normal
        pass
    
    # Si aucune action spéciale, retourner au gestionnaire de choix
    return await handle_choice(update, context)

async def handle_photo_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère la réception des preuves de paiement en photo"""
    if not context.user_data.get('waiting_for_proof', False):
        await update.message.reply_text(
            "❓ **Photo reçue**\n\n"
            "Je n'attends pas de preuve de paiement actuellement.\n"
            "Utilisez le menu pour effectuer une transaction.",
            reply_markup=ReplyKeyboardMarkup(get_main_menu(), resize_keyboard=True),
            parse_mode='Markdown'
        )
        return CHOOSING

    # Vérifier le format de l'image
    if update.message.photo:
        user = update.effective_user
        user_db = db.get_user(user.id)
        
        if user_db:
            # Récupérer l'ID du fichier de la plus grande photo
            file_id = update.message.photo[-1].file_id
            
            # Traitement pour les achats VIP
            if context.user_data.get('vip_purchase'):
                vip_duration = context.user_data.get('vip_duration', '1m')
                amount = context.user_data.get('vip_amount', 0)
                
                # Enregistrer la preuve dans la base de données
                proof_id = db.save_payment_proof(
                    user_db[0], user.id, f"VIP_{vip_duration}_{user.id}_{int(datetime.now().timestamp())}", 
                    "vip_purchase", amount, "baridimob", file_id, "photo"
                )
                
                confirmation_msg = f"""
✅ **Preuve de paiement VIP reçue !**

🆔 **Numéro de confirmation :** #{proof_id}
📸 **Votre capture a été enregistrée avec succès**
💎 **Achat VIP {vip_duration} - {amount} DZD**

🔍 **Statut :** En cours de vérification
⏰ **Délai de traitement :** 5-30 minutes maximum
🔔 **Vous recevrez une notification automatique**

👑 **Votre statut VIP sera activé après validation !**

📋 **Prochaines étapes :**
• Notre équipe vérifie votre paiement
• Vous recevrez une confirmation par message
• En cas de problème, contactez le support

Merci pour votre confiance ! 🎉
"""
                
            # Traitement pour les autres transactions
            else:
                service = context.user_data.get('service', 'service')
                amount = context.user_data.get('amount', 0)
                payment_method = context.user_data.get('payment_method', 'baridimob')
                transaction_id = context.user_data.get('transaction_id', f"{service}_{user.id}_{int(datetime.now().timestamp())}")
                
                # Enregistrer la preuve dans la base de données
                proof_id = db.save_payment_proof(
                    user_db[0], user.id, transaction_id, service, amount, payment_method, file_id, "photo"
                )
                
                confirmation_msg = f"""
✅ **Preuve de paiement reçue !**

🆔 **Numéro de confirmation :** #{proof_id}
📸 **Votre capture a été enregistrée avec succès**
🔄 **Service :** {service.upper()}
💰 **Montant :** {amount} DZD

🔍 **Statut :** En cours de vérification
⏰ **Délai de traitement :** 5-30 minutes maximum
🔔 **Vous recevrez une notification automatique**

📋 **Prochaines étapes :**
• Notre équipe vérifie votre paiement
• Votre service sera activé après validation
• En cas de problème, contactez le support

📞 **Support :** @support_bot disponible 24/7

Merci pour votre confiance ! 🎉
"""

            await update.message.reply_text(confirmation_msg, parse_mode='Markdown')

        # Nettoyer les données temporaires
        context.user_data.clear()
        
        # Retour au menu principal
        reply_markup = ReplyKeyboardMarkup(get_main_menu(), resize_keyboard=True)
        await update.message.reply_text(
            "🏠 **Menu Principal**\n\n"
            "Vous pouvez effectuer une nouvelle transaction si vous le souhaitez.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
        return CHOOSING
    
    else:
        await update.message.reply_text(
            "❌ **Format non supporté**\n\n"
            "Veuillez envoyer une image au format :\n"
            "• JPEG (.jpg)\n"
            "• PNG (.png)\n\n"
            "📱 Utilisez l'icône 📎 pour sélectionner votre capture d'écran.",
            parse_mode='Markdown'
        )
        return PAYMENT_PROOF

async def handle_support_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère le chat de support intelligent et humain"""
    user_id = update.effective_user.id
    message = update.message.text.strip()
    
    if context.user_data.get('smart_chat_active'):
        # Chat intelligent - réponses automatiques
        response = await generate_smart_response(message)
        
        await update.message.reply_text(
            f"🤖 **Assistant IA :**\n\n{response}\n\n"
            f"💡 **Besoin d'aide supplémentaire ?**\n"
            f"Tapez votre prochaine question ou /menu pour retourner au menu principal.",
            parse_mode='Markdown'
        )
        
        # Enregistrer la conversation
        db.save_support_message(user_id, message, 'user')
        db.save_support_message(user_id, response, 'bot')
        
    elif context.user_data.get('human_support_active'):
        # Support humain - transférer à l'admin
        user = db.get_user(user_id)
        username = user[2] if user else "Utilisateur"
        
        message_id = db.save_support_message(user_id, message, 'user')
        
        await update.message.reply_text(
            "📨 **Message envoyé au support !**\n\n"
            f"🆔 Ticket: #{message_id}\n"
            f"📝 Votre message: {message[:100]}{'...' if len(message) > 100 else ''}\n\n"
            f"⏰ **Délai de réponse estimé :**\n"
            f"• Standard: 15-30 minutes\n"
            f"• VIP: 5-10 minutes\n\n"
            f"🔔 Vous recevrez une notification dès qu'un agent vous répondra.\n\n"
            f"Tapez /menu pour retourner au menu principal.",
            parse_mode='Markdown'
        )
        
        # Notifier l'admin (optionnel - peut être implémenté plus tard)
        context.user_data['human_support_active'] = False
    
    return SUPPORT_CHAT

async def handle_payout_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère la configuration des informations de paiement"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    method = context.user_data.get('payout_method')
    
    if not method:
        await update.message.reply_text(
            "❌ Erreur: Méthode de paiement non sélectionnée.\n"
            "Retournez au menu principal.",
            reply_markup=ReplyKeyboardMarkup(get_main_menu(), resize_keyboard=True)
        )
        return CHOOSING
    
    # Parser les informations selon la méthode
    try:
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        details = {}
        
        for line in lines:
            if ':' in line:
                key, value = line.split(':', 1)
                details[key.strip().lower()] = value.strip()
        
        # Validation selon la méthode
        if method == 'baridimob':
            required = ['nom complet', 'numéro']
            if not all(key in details for key in required):
                raise ValueError("Informations manquantes")
                
        elif method == 'ccp':
            required = ['nom complet', 'numéro ccp', 'clé']
            if not all(key in details for key in required):
                raise ValueError("Informations manquantes")
                
        elif method == 'bank':
            required = ['nom complet', 'banque', 'rib']
            if not all(key in details for key in required):
                raise ValueError("Informations manquantes")
        
        # Sauvegarder les informations
        user = db.get_user(user_id)
        if user:
            db.create_user_payout_info(user[0], method, details)
            
            method_names = {
                'baridimob': 'BaridiMob',
                'ccp': 'CCP',
                'bank': 'Virement Bancaire'
            }
            
            await update.message.reply_text(
                f"✅ **Coordonnées enregistrées avec succès !**\n\n"
                f"💳 Méthode: {method_names[method]}\n"
                f"👤 Nom: {details.get('nom complet', 'N/A')}\n\n"
                f"🔒 **Sécurité :** Vos informations sont chiffrées et sécurisées.\n\n"
                f"💰 **Prêt pour la vente !**\n"
                f"Entrez maintenant le montant en USDT que vous souhaitez vendre :\n"
                f"(Minimum 10 USDT)",
                parse_mode='Markdown'
            )
            
            context.user_data['payout_configured'] = True
            return AMOUNT_DETAIL
            
    except Exception as e:
        method_instructions = {
            'baridimob': "Nom complet: [Votre nom]\nNuméro: [0XXXXXXXXX]",
            'ccp': "Nom complet: [Votre nom]\nNuméro CCP: [Numéro]\nClé: [Clé]",
            'bank': "Nom complet: [Votre nom]\nBanque: [Nom banque]\nRIB: [RIB complet]"
        }
        
        await update.message.reply_text(
            f"❌ **Format incorrect**\n\n"
            f"Veuillez respecter le format suivant :\n\n"
            f"{method_instructions.get(method, 'Format non défini')}\n\n"
            f"Essayez à nouveau :",
            parse_mode='Markdown'
        )
        return PAYOUT_INFO

async def generate_smart_response(message: str) -> str:
    """Génère une réponse intelligente basée sur le message"""
    message_lower = message.lower()
    
    # Réponses prédéfinies pour les questions courantes
    responses = {
        'usdt': "💎 **À propos des USDT :**\n• Achat minimum: 5 USDT\n• Vente minimum: 10 USDT\n• Paiement BaridiMob/CCP\n• Cashback automatique\n\nQue voulez-vous faire avec les USDT ?",
        
        'baridi': "📱 **BaridiMob :**\n• RIP: 0799999002264673222\n• Transfert instantané\n• Capture d'écran requise\n• Traitement en 5-15 min\n\nProblème spécifique avec BaridiMob ?",
        
        'vip': "👑 **Avantages VIP :**\n• Cashback 5% (vs 2%)\n• Codes promo -20%\n• Support prioritaire\n• Tarifs préférentiels\n\nTarifs: 500 DZD/mois, 1200/3mois, 2000/6mois",
        
        'transaction': "💼 **Transactions :**\n• Délai: 5-30 minutes\n• Statut visible dans 'Mes Transactions'\n• Notification automatique\n• Support si problème\n\nProblème avec quelle transaction ?",
        
        'paiement': "💳 **Paiements :**\n• BaridiMob: Instantané\n• CCP: 1-2h\n• Crypto: 10-30 min\n• Preuve obligatoire\n\nQuel mode de paiement vous pose problème ?",
        
        'bloqué': "🔧 **Transaction bloquée ?**\n• Vérifiez votre preuve de paiement\n• Délai normal: 5-30 min\n• Contactez support si +1h\n• ID transaction requis\n\nDepuis combien de temps ?",
        
        'erreur': "❌ **Erreurs courantes :**\n• Montant incorrect\n• Mauvais RIP/compte\n• Photo illisible\n• Réseau différent (USDT)\n\nQuelle erreur exactement ?",
        
        'aide': "🆘 **Centre d'aide :**\n• Chat IA: Réponses immédiates\n• Support humain: Problèmes complexes\n• FAQ: Solutions courantes\n• Guide: Procédures détaillées\n\nQue puis-je vous expliquer ?"
    }
    
    # Recherche de mots-clés
    for keyword, response in responses.items():
        if keyword in message_lower:
            return response
    
    # Réponse générale si aucun mot-clé trouvé
    return """
🤖 **Je suis là pour vous aider !**

Voici ce que je peux vous expliquer :
• 💎 Achat/Vente USDT
• 📱 Paiements BaridiMob/CCP
• 👑 Avantages VIP
• 💼 Suivi des transactions
• 🔧 Résolution de problèmes

**Reformulez votre question** avec des mots-clés comme :
"USDT", "BaridiMob", "VIP", "transaction bloquée", etc.

Ou tapez **"aide"** pour voir toutes les options !
"""

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Annule la conversation"""
    reply_markup = ReplyKeyboardMarkup(get_main_menu(), resize_keyboard=True)
    await update.message.reply_text(
        "❌ **Opération annulée**\n\nRetour au menu principal.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return CHOOSING

# Fonction pour démarrer Flask dans un thread séparé
def run_flask():
    app.run(host='0.0.0.0', port=5000, debug=False)

def main():
    """Fonction principale avec toutes les fonctionnalités avancées"""
    global telegram_app
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

    if not TOKEN:
        TOKEN = "7965004321:AAEjt1sIQc8XbqK1HoDNIbo7hvn2qxj6ljI"
        print("⚠️ Token récupéré directement")
    else:
        print("✅ Token récupéré depuis les Secrets")

    # Démarrer Flask dans un thread séparé
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Configuration optimisée pour éviter les conflits d'instances
    app_telegram = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()
    telegram_app = app_telegram  # Référence globale pour les notifications admin

    # Gestionnaire de conversation principal avec tous les états
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CHOOSING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_choice),
                CallbackQueryHandler(handle_callback_query),
                MessageHandler(filters.PHOTO, handle_photo_proof)
            ],
            SERVICE_DETAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_choice)
            ],
            PHONE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone_input)
            ],
            AMOUNT_DETAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_detail)
            ],
            CONFIRMATION: [
                CallbackQueryHandler(handle_callback_query),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)
            ],
            PAYMENT_PROOF: [
                CallbackQueryHandler(handle_callback_query),
                MessageHandler(filters.PHOTO, handle_photo_proof),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)
            ],
            PAYOUT_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payout_info),
                CallbackQueryHandler(handle_callback_query)
            ],
            SUPPORT_CHAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_support_chat),
                CallbackQueryHandler(handle_callback_query)
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CommandHandler('start', start)
        ],
        allow_reentry=True,
        per_message=False,
        per_chat=True
    )

    # Gestionnaire d'erreur global
    async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Gestionnaire d'erreurs global pour éviter les crashes"""
        print(f"❌ Erreur dans l'update {update}: {context.error}")
        
        # Si c'est un conflit d'instance
        if "terminated by other getUpdates request" in str(context.error):
            print("⚠️ Conflit d'instance détecté - Tentative de récupération...")
            await asyncio.sleep(5)  # Pause avant redémarrage
            return
        
        # Autres erreurs
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "❌ Une erreur technique s'est produite. Veuillez réessayer ou contactez le support.\n\n"
                    "Retour au menu principal :",
                    reply_markup=ReplyKeyboardMarkup(get_main_menu(), resize_keyboard=True)
                )
            except:
                pass  # Ignore si impossible d'envoyer le message

    app_telegram.add_error_handler(error_handler)
    app_telegram.add_handler(conv_handler)

    print("🚀 Bot Multi-Services Premium démarré avec succès!")
    print("✅ Fonctionnalités Premium activées:")
    print("   📊 Base de données SQLite complète")
    print("   👑 Système VIP et cashback")
    print("   💳 Processeur de paiement avancé")
    print("   🎯 Système de fidélité")
    print("   🌐 Interface web d'administration")
    print("   🔒 Sécurité renforcée")
    print("   📱 Support multi-opérateurs")
    print("   🎁 Codes promo et parrainage")
    print("   📊 Analytics en temps réel")
    print("   ✅ Gestion complète des erreurs")
    print("")
    print("🌐 Interface web disponible sur : http://0.0.0.0:5000")
    print("🔍 En attente de messages Telegram...")

    # Démarrage avec gestion d'erreurs et retry automatique
    try:
        app_telegram.run_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
            timeout=30,
            read_timeout=30,
            write_timeout=30,
            connect_timeout=30
        )
    except Exception as e:
        print(f"❌ Erreur critique: {e}")
        print("🔄 Redémarrage du bot en cours...")
        time.sleep(10)
        main()  # Redémarrage automatique

if __name__ == '__main__':
    main()