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
import re
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
    'ooredoo': {'multiplier': 1.15, 'cashback': 0.02, 'vip_cashback': 0.05},
    'euro_cash': {'rate': 185, 'cashback': 0.02, 'vip_cashback': 0.05}
}

# Adresse USDT pour les ventes (géré par admin)
USDT_RECEIVING_ADDRESS = "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"

# Minuteur des transactions (10 minutes)
TRANSACTION_TIMEOUT = 600  # 10 minutes en secondes

# Configuration Telegram Bot
TOKEN = os.getenv('BOT_TOKEN', '7965004321:AAEjt1sIQc8XbqK1HoDNIbo7hvn2qxj6ljI')
ADMIN_ID = int(os.getenv('ADMIN_ID', '5735064970'))

# Référence globale pour l'application Telegram
telegram_app = None

def init_database():
    """Initialise la base de données avec toutes les tables nécessaires"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Table des utilisateurs
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_vip INTEGER DEFAULT 0,
            cashback_balance REAL DEFAULT 0.0,
            total_transactions INTEGER DEFAULT 0,
            registration_date TEXT,
            last_activity TEXT
        )
    ''')

    # Table des transactions
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            service_type TEXT,
            amount REAL,
            phone_number TEXT,
            payment_method TEXT,
            status TEXT DEFAULT 'pending',
            proof_photo TEXT,
            created_at TEXT,
            completed_at TEXT,
            admin_notes TEXT,
            cashback_earned REAL DEFAULT 0.0,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')

    # Table des preuves de paiement
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payment_proofs (
            id TEXT PRIMARY KEY,
            transaction_id TEXT,
            user_id INTEGER,
            photo_data TEXT,
            uploaded_at TEXT,
            verified INTEGER DEFAULT 0,
            FOREIGN KEY (transaction_id) REFERENCES transactions (id)
        )
    ''')

    # Table des ventes USDT
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usdt_sales (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            seller_id TEXT,
            amount REAL,
            rate REAL,
            dzd_amount REAL,
            payout_info TEXT,
            usdt_address TEXT,
            tx_hash TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            completed_at TEXT,
            admin_notes TEXT,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')

    # Table des vendeurs USDT professionnels
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usdt_sellers (
            id TEXT PRIMARY KEY,
            name TEXT,
            sell_rate REAL,
            trust_level INTEGER,
            volume_24h TEXT,
            response_time TEXT,
            is_active INTEGER DEFAULT 1
        )
    ''')

    # Table des rendez-vous
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS appointments (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            service_type TEXT,
            preferred_date TEXT,
            preferred_time TEXT,
            phone_number TEXT,
            notes TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            confirmed_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')

    # Insérer des vendeurs USDT par défaut
    cursor.execute('SELECT COUNT(*) FROM usdt_sellers')
    if cursor.fetchone()[0] == 0:
        sellers_data = [
            ('seller_1', 'CryptoExpert DZ', 272.50, 5, '50,000 USDT', '< 5 min', 1),
            ('seller_2', 'FastCrypto', 271.80, 4, '30,000 USDT', '< 10 min', 1),
            ('seller_3', 'AlgeriaCoin', 271.20, 5, '75,000 USDT', '< 3 min', 1)
        ]
        cursor.executemany('''
            INSERT INTO usdt_sellers (id, name, sell_rate, trust_level, volume_24h, response_time, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', sellers_data)

    conn.commit()
    conn.close()

def get_user_info(user_id):
    """Récupère les informations d'un utilisateur"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user

def create_or_update_user(user_id, username, first_name, last_name):
    """Crée ou met à jour un utilisateur"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
    if cursor.fetchone():
        cursor.execute('''
            UPDATE users SET username = ?, first_name = ?, last_name = ?, last_activity = ?
            WHERE user_id = ?
        ''', (username, first_name, last_name, datetime.now().isoformat(), user_id))
    else:
        cursor.execute('''
            INSERT INTO users (user_id, username, first_name, last_name, registration_date, last_activity)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, username, first_name, last_name, datetime.now().isoformat(), datetime.now().isoformat()))

    conn.commit()
    conn.close()

def get_professional_menu():
    """Menu principal professionnel"""
    return [
        ['💰 Acheter USDT', '💸 Vendre USDT'],
        ['💳 Recharge Flexy', '📱 Recharge Mobilis', '🔄 Recharge Ooredoo'],
        ['💶 Euro/DZD Cash', '📅 Prendre RDV'],
        ['📊 Mes Transactions', '🎁 Mon Cashback'],
        ['📞 Support Client', '⚙️ Paramètres']
    ]

def get_professional_usdt_sellers():
    """Retourne la liste des vendeurs USDT professionnels depuis la base de données"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM usdt_sellers WHERE is_active = 1 ORDER BY sell_rate DESC')
    sellers_data = cursor.fetchall()
    conn.close()

    sellers = []
    for seller in sellers_data:
        sellers.append({
            'id': seller[0],
            'name': seller[1],
            'sell_rate': seller[2],
            'trust_level': seller[3],
            'volume_24h': seller[4],
            'response_time': seller[5]
        })

    return sellers

def calculate_service_price(service_type, amount):
    """Calcule le prix d'un service"""
    rates = DYNAMIC_RATES.get(service_type, {})

    if service_type in ['usdt_buy', 'usdt_sell', 'euro_cash']:
        return amount * rates.get('rate', 280)
    elif service_type in ['flexy', 'mobilis', 'ooredoo']:
        return amount * rates.get('multiplier', 1.2)

    return amount

def validate_algerian_phone(phone):
    """Valide un numéro de téléphone algérien"""
    phone = re.sub(r'[^\d]', '', phone)

    patterns = [
        r'^213[567]\d{8}$',
        r'^0[567]\d{8}$',
        r'^[567]\d{8}$'
    ]

    for pattern in patterns:
        if re.match(pattern, phone):
            return True
    return False

def format_phone_number(phone):
    """Formate un numéro de téléphone algérien"""
    phone = re.sub(r'[^\d]', '', phone)

    if phone.startswith('213'):
        return f"+213 {phone[3:5]} {phone[5:7]} {phone[7:9]} {phone[9:11]}"
    elif phone.startswith('0'):
        return f"{phone[:3]} {phone[3:5]} {phone[5:7]} {phone[7:9]} {phone[9:11]}"
    else:
        return f"0{phone[:2]} {phone[2:4]} {phone[4:6]} {phone[6:8]} {phone[8:10]}"

# Gestionnaires Telegram améliorés
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Commande de démarrage du bot"""
    try:
        user = update.effective_user

        create_or_update_user(
            user.id,
            user.username,
            user.first_name,
            user.last_name
        )

        user_info = get_user_info(user.id)
        is_vip = user_info[4] if user_info else 0
        cashback = user_info[5] if user_info else 0.0

        vip_badge = "👑 VIP" if is_vip else "🌟 Standard"

        welcome_message = f"""
🎯 **Bienvenue chez CryptoDZ Pro** 🇩🇿

👋 Salut {user.first_name} !

💼 **Votre statut :** {vip_badge}
💰 **Cashback disponible :** {cashback:.2f} DZD

🚀 **Services Premium :**
• 💱 Trading USDT professionnel
• 📱 Recharges téléphoniques instantanées
• 💶 Change Euro/DZD
• 📅 Rendez-vous en magasin
• 💎 Cashback sur toutes transactions

🔥 **Pourquoi nous choisir ?**
✅ Transactions ultra-rapides (< 5 min)
✅ Taux compétitifs en temps réel
✅ Support client 24/7
✅ Sécurité maximale
✅ Interface simple et intuitive

📞 **Support client :** @CryptoDZSupport
🌐 **Site web :** cryptodz.pro

**Sélectionnez un service ci-dessous :**
        """

        reply_markup = ReplyKeyboardMarkup(get_professional_menu(), resize_keyboard=True)

        await update.message.reply_text(
            welcome_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

        return CHOOSING
    except Exception as e:
        logging.error(f"Erreur dans start: {e}")
        await update.message.reply_text("❌ Une erreur s'est produite. Utilisez /start pour recommencer.")
        return CHOOSING

async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère les choix du menu principal avec gestion complète des erreurs"""
    try:
        text = update.message.text
        user_id = update.effective_user.id

        if text == '💰 Acheter USDT':
            rate = DYNAMIC_RATES['usdt_buy']['rate']

            keyboard = [
                [InlineKeyboardButton("📊 Voir taux actuels", callback_data="view_buy_rates")],
                [InlineKeyboardButton("💰 Commencer l'achat", callback_data="start_usdt_buy")],
                [InlineKeyboardButton("❓ Comment ça marche ?", callback_data="how_buy_usdt")],
                [InlineKeyboardButton("🔙 Retour menu", callback_data="back_to_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"💰 **Achat USDT Professionnel**\n\n"
                f"📈 **Taux actuel :** {rate} DZD/USDT\n"
                f"⚡ **Livraison :** Instantanée\n"
                f"💳 **Paiement :** BaridiMob, CCP, Virement\n"
                f"🔒 **Sécurisé :** Transactions cryptées\n"
                f"🎁 **Cashback :** 2% (Standard) | 5% (VIP)\n\n"
                f"Choisissez une option :",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return CHOOSING

        elif text == '💸 Vendre USDT':
            keyboard = [
                [InlineKeyboardButton("📊 Voir vendeurs disponibles", callback_data="view_usdt_sellers")],
                [InlineKeyboardButton("💸 Commencer la vente", callback_data="start_usdt_sell")],
                [InlineKeyboardButton("❓ Comment ça marche ?", callback_data="how_sell_usdt")],
                [InlineKeyboardButton("🔙 Retour menu", callback_data="back_to_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"💸 **Vente USDT Professionnelle**\n\n"
                f"🎯 **Service premium** avec vendeurs vérifiés\n"
                f"💰 **Paiement rapide** BaridiMob/CCP\n"
                f"⭐ **Vendeurs certifiés** avec notes de confiance\n"
                f"🔒 **Transactions sécurisées** et garanties\n"
                f"⏰ **Délai de traitement :** 5-15 minutes\n\n"
                f"Choisissez une option :",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return CHOOSING

        elif text in ['💳 Recharge Flexy', '📱 Recharge Mobilis', '🔄 Recharge Ooredoo']:
            service_map = {
                '💳 Recharge Flexy': 'flexy',
                '📱 Recharge Mobilis': 'mobilis',
                '🔄 Recharge Ooredoo': 'ooredoo'
            }

            service = service_map[text]
            context.user_data['service'] = service

            keyboard = [
                [InlineKeyboardButton("📱 Saisir numéro", callback_data=f"enter_phone_{service}")],
                [InlineKeyboardButton("💰 Voir tarifs", callback_data=f"view_rates_{service}")],
                [InlineKeyboardButton("❓ Aide", callback_data=f"help_{service}")],
                [InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"📱 **{text}**\n\n"
                f"⚡ **Recharge instantanée** en moins de 5 minutes\n"
                f"💰 **Tarifs compétitifs** avec cashback inclus\n"
                f"🎁 **Bonus fidélité** sur chaque recharge\n"
                f"🔒 **100% sécurisé** et garanti\n\n"
                f"Que souhaitez-vous faire ?",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return CHOOSING

        elif text == '💶 Euro/DZD Cash':
            rate = DYNAMIC_RATES['euro_cash']['rate']

            keyboard = [
                [InlineKeyboardButton("💰 Calculer montant", callback_data="calculate_euro")],
                [InlineKeyboardButton("📅 Prendre RDV", callback_data="book_euro_appointment")],
                [InlineKeyboardButton("📍 Notre adresse", callback_data="shop_location")],
                [InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"💶 **Change Euro/DZD Cash**\n\n"
                f"📈 **Taux actuel :** {rate} DZD/EUR\n"
                f"🏪 **Service :** En magasin uniquement\n"
                f"📍 **Adresse :** Alger Centre, Place des Martyrs\n"
                f"⏰ **Horaires :** 9h-18h (Sam-Jeu)\n"
                f"💰 **Change immédiat** sans commission cachée\n\n"
                f"Que souhaitez-vous faire ?",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return CHOOSING

        elif text == '📅 Prendre RDV':
            keyboard = [
                [InlineKeyboardButton("💶 RDV Change Euro/DZD", callback_data="rdv_euro")],
                [InlineKeyboardButton("💰 RDV Transactions USDT", callback_data="rdv_crypto")],
                [InlineKeyboardButton("📱 RDV Support technique", callback_data="rdv_support")],
                [InlineKeyboardButton("📋 RDV Consultation", callback_data="rdv_consultation")],
                [InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"📅 **Prendre Rendez-vous**\n\n"
                f"🏪 **Notre magasin physique :**\n"
                f"📍 Alger Centre, Place des Martyrs\n"
                f"⏰ Lun-Ven: 9h-18h | Sam: 9h-14h\n"
                f"📞 +213 555 123 456\n\n"
                f"🎯 **Services disponibles en magasin :**\n"
                f"• Change Euro/DZD en espèces\n"
                f"• Grosses transactions USDT\n"
                f"• Support technique personnalisé\n"
                f"• Consultation crypto professionnelle\n\n"
                f"Choisissez le type de rendez-vous :",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return CHOOSING

        else:
            # Gestion des autres options du menu
            await handle_other_menu_options(update, context, text)
            return CHOOSING

    except Exception as e:
        logging.error(f"Erreur dans handle_choice: {e}")
        await update.message.reply_text(
            "❌ **Erreur temporaire**\n\n"
            "Veuillez réessayer ou utilisez /start pour redémarrer.",
            reply_markup=ReplyKeyboardMarkup(get_professional_menu(), resize_keyboard=True)
        )
        return CHOOSING

async def handle_other_menu_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère les autres options du menu principal"""
    text = update.message.text
    user_id = update.effective_user.id

    if text == '📊 Mes Transactions':
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM transactions 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT 5
        ''', (user_id,))
        transactions = cursor.fetchall()
        conn.close()

        if transactions:
            message = "📊 **Vos dernières transactions :**\n\n"
            for tx in transactions:
                status_emoji = "✅" if tx[6] == "completed" else "⏳" if tx[6] == "pending" else "❌"
                message += f"{status_emoji} **{tx[2]}** - {tx[3]} DZD\n"
                message += f"📅 {tx[8][:10]} | ID: `{tx[0][:8]}...`\n\n"
        else:
            message = "📊 **Aucune transaction trouvée**\n\nCommencez dès maintenant !"

        keyboard = [
            [InlineKeyboardButton("📊 Voir tout l'historique", callback_data="view_all_transactions")],
            [InlineKeyboardButton("🔙 Retour menu", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

    elif text == '🎁 Mon Cashback':
        user_info = get_user_info(user_id)
        cashback = user_info[5] if user_info else 0.0
        total_tx = user_info[6] if user_info else 0
        is_vip = user_info[4] if user_info else 0

        keyboard = []
        if cashback >= 100:
            keyboard.append([InlineKeyboardButton("💸 Retirer Cashback", callback_data="withdraw_cashback")])
        keyboard.append([InlineKeyboardButton("📈 Devenir VIP", callback_data="upgrade_vip")])
        keyboard.append([InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"🎁 **Votre Programme Cashback**\n\n"
            f"💰 **Solde disponible :** {cashback:.2f} DZD\n"
            f"📊 **Transactions totales :** {total_tx}\n"
            f"👑 **Statut :** {'VIP' if is_vip else 'Standard'}\n\n"
            f"💡 **Avantages VIP :**\n"
            f"• Cashback doublé (5% au lieu de 2%)\n"
            f"• Taux préférentiels exclusifs\n"
            f"• Support prioritaire 24/7\n"
            f"• Accès anticipé aux nouveautés",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    elif text == '📞 Support Client':
        keyboard = [
            [InlineKeyboardButton("🤖 Chat automatique", callback_data="auto_support")],
            [InlineKeyboardButton("👨‍💼 Agent humain", callback_data="human_support")],
            [InlineKeyboardButton("❓ FAQ", callback_data="faq")],
            [InlineKeyboardButton("📞 Urgence", callback_data="emergency_contact")],
            [InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"📞 **Support Client CryptoDZ**\n\n"
            f"🕐 **Disponible 24/7** pour vous aider\n"
            f"⚡ **Réponse rapide** garantie\n"
            f"🎯 **Support spécialisé** par des experts\n\n"
            f"**Comment pouvons-nous vous aider ?**",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère tous les callbacks des boutons inline avec flux complet sans erreurs"""
    try:
        query = update.callback_query
        await query.answer()

        data = query.data
        user_id = query.from_user.id

        # === GESTION ACHAT USDT ===
        if data == "view_buy_rates":
            current_rate = DYNAMIC_RATES['usdt_buy']['rate']
            await query.edit_message_text(
                f"📊 **Taux d'achat USDT actuels**\n\n"
                f"💰 **Taux principal :** {current_rate} DZD/USDT\n"
                f"📈 **Dernière mise à jour :** {datetime.now().strftime('%H:%M')}\n"
                f"🎁 **Cashback Standard :** 2%\n"
                f"👑 **Cashback VIP :** 5%\n\n"
                f"💡 **Exemple d'achat :**\n"
                f"• 100 USDT = {current_rate * 100:.0f} DZD\n"
                f"• 500 USDT = {current_rate * 500:.0f} DZD\n"
                f"• 1000 USDT = {current_rate * 1000:.0f} DZD",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰 Commencer l'achat", callback_data="start_usdt_buy")],
                    [InlineKeyboardButton("🔄 Actualiser taux", callback_data="view_buy_rates")],
                    [InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")]
                ]),
                parse_mode='Markdown'
            )
            return CHOOSING

        elif data == "start_usdt_buy":
            rate = DYNAMIC_RATES['usdt_buy']['rate']
            context.user_data['service'] = 'usdt_buy'
            context.user_data['usdt_rate'] = rate

            await query.edit_message_text(
                f"💰 **Achat USDT - Étape 1/4**\n\n"
                f"📈 **Taux actuel :** {rate} DZD/USDT\n"
                f"💳 **Méthodes acceptées :** BaridiMob, CCP, Virement\n"
                f"⚡ **Livraison :** Instantanée après confirmation\n"
                f"🔒 **Sécurisé :** Transaction cryptée et garantie\n\n"
                f"💡 **Entrez le montant en USDT (minimum 10 USDT) :**\n"
                f"Exemple: 100",
                parse_mode='Markdown'
            )
            return AMOUNT_DETAIL

        elif data == "how_buy_usdt":
            await query.edit_message_text(
                f"❓ **Comment acheter des USDT ?**\n\n"
                f"**📋 Procédure simple en 4 étapes :**\n\n"
                f"**1️⃣ Montant**\n"
                f"• Indiquez combien d'USDT vous voulez\n"
                f"• Minimum 10 USDT\n\n"
                f"**2️⃣ Paiement**\n"
                f"• Choisissez votre méthode (BaridiMob recommandé)\n"
                f"• Effectuez le virement au RIP indiqué\n\n"
                f"**3️⃣ Preuve**\n"
                f"• Envoyez une capture d'écran du paiement\n"
                f"• Notre système vérifie automatiquement\n\n"
                f"**4️⃣ Réception**\n"
                f"• Recevez vos USDT en 5-15 minutes\n"
                f"• + Cashback automatique sur votre compte\n\n"
                f"**🔒 100% sécurisé et garanti !**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰 Commencer maintenant", callback_data="start_usdt_buy")],
                    [InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")]
                ]),
                parse_mode='Markdown'
            )
            return CHOOSING

        # === GESTION VENTE USDT COMPLÈTE ===
        elif data == "view_usdt_sellers":
            sellers = get_professional_usdt_sellers()
            message = "💸 **Vendeurs USDT Disponibles**\n\n"

            for seller in sellers:
                trust_stars = '⭐' * seller['trust_level']
                message += f"**{seller['name']}** {trust_stars}\n"
                message += f"💰 Taux: {seller['sell_rate']:.2f} DZD/USDT\n"
                message += f"📊 Volume 24h: {seller['volume_24h']}\n"
                message += f"⚡ Réponse: {seller['response_time']}\n\n"

            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💸 Commencer la vente", callback_data="start_usdt_sell")],
                    [InlineKeyboardButton("🔄 Actualiser", callback_data="view_usdt_sellers")],
                    [InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")]
                ]),
                parse_mode='Markdown'
            )
            return CHOOSING

        elif data == "start_usdt_sell":
            sellers = get_professional_usdt_sellers()
            keyboard = []

            for seller in sellers:
                trust_stars = '⭐' * seller['trust_level']
                button_text = f"💰 {seller['sell_rate']:.2f} DZD - {seller['name']} {trust_stars}"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"sell_usdt_{seller['id']}")])

            keyboard.append([InlineKeyboardButton("🔄 Actualiser les taux", callback_data="refresh_usdt_sell")])
            keyboard.append([InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"💸 **Vente USDT - Sélection Vendeur**\n\n"
                f"🎯 **Choisissez votre acheteur préféré :**\n\n"
                f"📊 Taux actualisés en temps réel\n"
                f"⭐ Acheteurs vérifiés et fiables\n"
                f"💸 Paiement rapide BaridiMob/CCP\n"
                f"🔒 Transactions sécurisées\n\n"
                f"👇 **Sélectionnez un acheteur :**",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return SERVICE_DETAIL

        elif data == "how_sell_usdt":
            await query.edit_message_text(
                f"❓ **Comment vendre vos USDT ?**\n\n"
                f"**📋 Procédure sécurisée en 5 étapes :**\n\n"
                f"**1️⃣ Sélection**\n"
                f"• Choisissez un acheteur certifié\n"
                f"• Comparez les taux et délais\n\n"
                f"**2️⃣ Montant**\n"
                f"• Indiquez combien d'USDT à vendre\n"
                f"• Minimum 20 USDT\n\n"
                f"**3️⃣ Coordonnées**\n"
                f"• Fournissez vos infos BaridiMob/CCP\n"
                f"• Une seule fois, mémorisées ensuite\n\n"
                f"**4️⃣ Envoi USDT**\n"
                f"• Envoyez vos USDT à l'adresse fournie\n"
                f"• Réseau TRC20 uniquement\n"
                f"• Délai limite: 10 minutes\n\n"
                f"**5️⃣ Paiement**\n"
                f"• Recevez vos DZD en 15-30 minutes\n"
                f"• Vérification automatique de la blockchain\n\n"
                f"**🔒 Vos USDT et DZD sont garantis !**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💸 Commencer maintenant", callback_data="start_usdt_sell")],
                    [InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")]
                ]),
                parse_mode='Markdown'
            )
            return CHOOSING

        # === GESTION VENDEURS USDT SPÉCIFIQUES ===
        elif data.startswith('sell_usdt_'):
            seller_id = data.split('_')[-1]
            sellers = get_professional_usdt_sellers()
            seller = next((s for s in sellers if s['id'] == seller_id), None)

            if seller:
                context.user_data['service'] = 'usdt_sell'
                context.user_data['selected_seller'] = seller

                await query.edit_message_text(
                    f"💸 **Vente USDT - Étape 1/5**\n\n"
                    f"🎯 **Acheteur sélectionné :**\n"
                    f"👤 {seller['name']} {'⭐' * seller['trust_level']}\n"
                    f"💰 **Taux :** {seller['sell_rate']:.2f} DZD/USDT\n"
                    f"📊 **Volume 24h :** {seller['volume_24h']}\n"
                    f"⚡ **Temps de réponse :** {seller['response_time']}\n\n"
                    f"💡 **Entrez la quantité d'USDT à vendre :**\n"
                    f"Minimum: 20 USDT | Exemple: 100",
                    parse_mode='Markdown'
                )
                return AMOUNT_DETAIL

        elif data == 'refresh_usdt_sell':
            sellers = get_professional_usdt_sellers()
            keyboard = []

            for seller in sellers:
                trust_stars = '⭐' * seller['trust_level']
                button_text = f"💰 {seller['sell_rate']:.2f} DZD - {seller['name']} {trust_stars}"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"sell_usdt_{seller['id']}")])

            keyboard.append([InlineKeyboardButton("🔄 Actualiser les taux", callback_data="refresh_usdt_sell")])
            keyboard.append([InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"💸 **Taux actualisés !**\n\n"
                f"🎯 **Choisissez votre acheteur préféré :**\n\n"
                f"📊 Taux en temps réel\n"
                f"⭐ Acheteurs vérifiés\n"
                f"💸 Paiement rapide\n\n"
                f"👇 **Sélectionnez un acheteur :**",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return SERVICE_DETAIL

        # === PROCÉDURE ENVOI USDT ===
        elif data == "continue_to_usdt_send":
            seller = context.user_data.get('selected_seller', {})
            amount = context.user_data.get('amount', 0)

            # Générer une adresse USDT unique pour cette transaction (simulation)
            usdt_address = f"TXo1RyVSh3h4K8nJ2{str(uuid.uuid4())[:10]}"
            context.user_data['usdt_receive_address'] = usdt_address

            await query.edit_message_text(
                f"💸 **Vente USDT - Étape 3/5 : Envoi USDT**\n\n"
                f"🔒 **Instructions d'envoi sécurisé :**\n\n"
                f"📋 **ÉTAPES OBLIGATOIRES :**\n"
                f"1️⃣ **Copiez l'adresse ci-dessous**\n"
                f"2️⃣ **Vérifiez le réseau TRC20**\n"
                f"3️⃣ **Envoyez exactement {amount:.4f} USDT**\n"
                f"4️⃣ **Fournissez le hash de transaction**\n\n"
                f"🔗 **Adresse de réception :**\n"
                f"`{usdt_address}`\n\n"
                f"⚠️ **IMPORTANT :**\n"
                f"• Utilisez uniquement le réseau TRC20\n"
                f"• Vérifiez l'adresse avant d'envoyer\n"
                f"• Délai limite : 10 minutes\n"
                f"• Les frais de réseau sont à votre charge\n\n"
                f"📱 **Après envoi, cliquez sur 'Hash fourni'**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Copier l'adresse", callback_data=f"copy_address_{usdt_address}")],
                    [InlineKeyboardButton("✅ J'ai envoyé - Fournir hash", callback_data="provide_tx_hash")],
                    [InlineKeyboardButton("❓ Aide envoi USDT", callback_data="help_send_usdt")],
                    [InlineKeyboardButton("❌ Annuler", callback_data="cancel_transaction")]
                ]),
                parse_mode='Markdown'
            )
            return CONFIRMATION

        elif data == "provide_tx_hash":
            await query.edit_message_text(
                f"🔗 **Vente USDT - Étape 4/5 : Hash de Transaction**\n\n"
                f"📋 **Fournissez le hash de votre transaction :**\n\n"
                f"💡 **Comment trouver le hash ?**\n"
                f"• Dans votre wallet : onglet 'Historique'\n"
                f"• Sur l'exchange : section 'Retraits'\n"
                f"• Format : 64 caractères alphanumériques\n\n"
                f"**Exemple :**\n"
                f"`a1b2c3d4e5f6789012345678901234567890abcdef`\n\n"
                f"⏰ **Délai de vérification :** 5-15 minutes\n\n"
                f"**Tapez votre hash de transaction :**",
                parse_mode='Markdown'
            )
            context.user_data['waiting_tx_hash'] = True
            return PAYMENT_PROOF

        elif data == "help_send_usdt":
            await query.edit_message_text(
                f"❓ **Aide : Comment envoyer des USDT**\n\n"
                f"**📱 Depuis un wallet mobile :**\n"
                f"1. Ouvrez votre wallet (TronLink, Trust, etc.)\n"
                f"2. Sélectionnez USDT\n"
                f"3. Cliquez 'Envoyer'\n"
                f"4. Collez l'adresse fournie\n"
                f"5. Entrez le montant exact\n"
                f"6. Sélectionnez réseau TRC20\n"
                f"7. Confirmez et payez les frais\n\n"
                f"**💻 Depuis un exchange :**\n"
                f"1. Allez dans 'Retrait'\n"
                f"2. Choisissez USDT (TRC20)\n"
                f"3. Collez l'adresse\n"
                f"4. Entrez le montant\n"
                f"5. Confirmez par email/SMS\n\n"
                f"**⚠️ Vérifications importantes :**\n"
                f"• Adresse correcte (double-check)\n"
                f"• Réseau TRC20 sélectionné\n"
                f"• Montant exact\n"
                f"• Frais de réseau ~1-3 USDT",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Retour envoi", callback_data="continue_to_usdt_send")],
                    [InlineKeyboardButton("📞 Support urgence", callback_data="emergency_support")]
                ]),
                parse_mode='Markdown'
            )
            return CONFIRMATION

        # === GESTION EURO/DZD ET RDV ===
        elif data == "calculate_euro":
            rate = DYNAMIC_RATES['euro_cash']['rate']
            keyboard = [
                [InlineKeyboardButton("100 EUR", callback_data="euro_100")],
                [InlineKeyboardButton("200 EUR", callback_data="euro_200")],
                [InlineKeyboardButton("500 EUR", callback_data="euro_500")],
                [InlineKeyboardButton("💰 Autre montant", callback_data="euro_custom")],
                [InlineKeyboardButton("📅 Prendre RDV", callback_data="book_euro_appointment")],
                [InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"💶 **Calculateur Euro/DZD**\n\n"
                f"📈 **Taux actuel :** {rate} DZD/EUR\n\n"
                f"💡 **Exemples de change :**\n"
                f"• 100 EUR = {rate * 100:.0f} DZD\n"
                f"• 200 EUR = {rate * 200:.0f} DZD\n"
                f"• 500 EUR = {rate * 500:.0f} DZD\n\n"
                f"🏪 **Service en magasin uniquement**\n"
                f"📍 Place des Martyrs, Alger Centre\n"
                f"⏰ Lun-Ven: 9h-18h | Sam: 9h-14h\n\n"
                f"**Sélectionnez un montant :**",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return CHOOSING

        elif data.startswith('euro_'):
            amount_type = data.split('_')[1]
            rate = DYNAMIC_RATES['euro_cash']['rate']

            if amount_type == 'custom':
                await query.edit_message_text(
                    f"💶 **Change Euro/DZD Personnalisé**\n\n"
                    f"📈 **Taux :** {rate} DZD/EUR\n\n"
                    f"💡 **Entrez le montant en EUR :**\n"
                    f"Exemple: 150",
                    parse_mode='Markdown'
                )
                context.user_data['service'] = 'euro_cash'
                return AMOUNT_DETAIL
            else:
                euro_amount = int(amount_type)
                dzd_amount = euro_amount * rate

                await query.edit_message_text(
                    f"💶 **Simulation de Change**\n\n"
                    f"💰 **Vous apportez :** {euro_amount} EUR\n"
                    f"💵 **Vous recevez :** {dzd_amount:.0f} DZD\n"
                    f"📈 **Taux appliqué :** {rate} DZD/EUR\n\n"
                    f"🏪 **Rendez-vous obligatoire**\n"
                    f"📍 Notre magasin, Place des Martyrs\n"
                    f"⏰ Change immédiat sur place\n\n"
                    f"**Souhaitez-vous prendre rendez-vous ?**",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📅 Prendre RDV maintenant", callback_data="book_euro_appointment")],
                        [InlineKeyboardButton("💰 Autre montant", callback_data="calculate_euro")],
                        [InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")]
                    ]),
                    parse_mode='Markdown'
                )
                return CHOOSING

        elif data == "book_euro_appointment":
            # Dates disponibles (simulation)
            available_dates = [
                "2025-01-15", "2025-01-16", "2025-01-17", 
                "2025-01-20", "2025-01-21", "2025-01-22"
            ]

            keyboard = []
            for date in available_dates:
                date_obj = datetime.strptime(date, "%Y-%m-%d")
                date_formatted = date_obj.strftime("%d/%m/%Y")
                day_name = date_obj.strftime("%A")
                day_fr = {"Monday": "Lundi", "Tuesday": "Mardi", "Wednesday": "Mercredi", 
                         "Thursday": "Jeudi", "Friday": "Vendredi", "Saturday": "Samedi"}
                keyboard.append([InlineKeyboardButton(f"{day_fr.get(day_name, day_formatted)} {date_formatted}", callback_data=f"date_{date}")])

            keyboard.append([InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"📅 **Prendre Rendez-vous**\n\n"
                f"🏪 **Notre magasin :**\n"
                f"📍 Place des Martyrs, Alger Centre\n"
                f"📞 +213 555 123 456\n\n"
                f"⏰ **Horaires disponibles :**\n"
                f"• Lun-Ven: 9h-18h\n"
                f"• Samedi: 9h-14h\n\n"
                f"**Choisissez une date :**",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return SERVICE_DETAIL

        elif data.startswith('date_'):
            selected_date = data.split('_')[1]
            context.user_data['selected_date'] = selected_date

            # Créneaux horaires
            time_slots = ["09:00", "10:00", "11:00", "14:00", "15:00", "16:00", "17:00"]
            keyboard = []

            for time_slot in time_slots:
                keyboard.append([InlineKeyboardButton(f"🕐 {time_slot}", callback_data=f"time_{time_slot}")])

            keyboard.append([InlineKeyboardButton("🔙 Autre date", callback_data="book_euro_appointment")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            date_obj = datetime.strptime(selected_date, "%Y-%m-%d")
            date_formatted = date_obj.strftime("%d/%m/%Y")

            await query.edit_message_text(
                f"⏰ **Sélection de l'heure**\n\n"
                f"📅 **Date choisie :** {date_formatted}\n\n"
                f"🕐 **Créneaux disponibles :**\n"
                f"Durée estimée : 15-30 minutes\n\n"
                f"**Choisissez votre heure :**",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return SERVICE_DETAIL

        elif data.startswith('time_'):
            selected_time = data.split('_')[1]
            context.user_data['selected_time'] = selected_time
            selected_date = context.user_data.get('selected_date')

            # Générer ID de RDV
            rdv_id = str(uuid.uuid4())[:8].upper()

            date_obj = datetime.strptime(selected_date, "%Y-%m-%d")
            date_formatted = date_obj.strftime("%d/%m/%Y")

            await query.edit_message_text(
                f"✅ **Rendez-vous Confirmé !**\n\n"
                f"🆔 **Référence :** {rdv_id}\n"
                f"📅 **Date :** {date_formatted}\n"
                f"🕐 **Heure :** {selected_time}\n"
                f"📍 **Lieu :** Place des Martyrs, Alger Centre\n\n"
                f"📋 **À apporter :**\n"
                f"• Pièce d'identité\n"
                f"• Euros à échanger\n"
                f"• Cette référence de RDV\n\n"
                f"📞 **Contact urgent :** +213 555 123 456\n\n"
                f"💡 **Négociation possible** sur place pour gros montants\n"
                f"⏰ **Arrivez 5 minutes avant l'heure**\n\n"
                f"**Un SMS de rappel sera envoyé la veille.**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📱 Ajouter à mon calendrier", callback_data="add_calendar")],
                    [InlineKeyboardButton("📞 Modifier RDV", callback_data="modify_appointment")],
                    [InlineKeyboardButton("🏠 Menu principal", callback_data="back_to_menu")]
                ]),
                parse_mode='Markdown'
            )

            # Enregistrer le RDV en base
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO appointments (id, user_id, service_type, preferred_date, preferred_time, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (rdv_id, user_id, 'euro_cash', selected_date, selected_time, 'confirmed', datetime.now().isoformat()))
            conn.commit()
            conn.close()

            return CHOOSING

        # === GESTION RECHARGES MOBILES ===
        elif data.startswith('enter_phone_'):
            service = data.split('_')[-1]
            context.user_data['service'] = service
            operator_names = {'flexy': 'Flexy', 'mobilis': 'Mobilis', 'ooredoo': 'Ooredoo'}

            await query.edit_message_text(
                f"📱 **Recharge {operator_names[service]} - Étape 1/3**\n\n"
                f"📞 **Entrez votre numéro de téléphone :**\n\n"
                f"**Formats acceptés :**\n"
                f"• 0X XX XX XX XX\n"
                f"• +213 X XX XX XX XX\n\n"
                f"**Exemple :** 0555123456\n\n"
                f"🔒 **Votre numéro est sécurisé et confidentiel**",
                parse_mode='Markdown'
            )
            return PHONE_INPUT

        elif data.startswith('view_rates_'):
            service = data.split('_')[-1]
            multiplier = DYNAMIC_RATES[service]['multiplier']
            operator_names = {'flexy': 'Flexy', 'mobilis': 'Mobilis', 'ooredoo': 'Ooredoo'}

            await query.edit_message_text(
                f"💰 **Tarifs Recharge {operator_names[service]}**\n\n"
                f"**Nos tarifs compétitifs :**\n"
                f"• 100 DZD → {100 * multiplier:.0f} DZD\n"
                f"• 200 DZD → {200 * multiplier:.0f} DZD\n"
                f"• 500 DZD → {500 * multiplier:.0f} DZD\n"
                f"• 1000 DZD → {1000 * multiplier:.0f} DZD\n\n"
                f"🎁 **Inclus :**\n"
                f"• Cashback 2% (Standard) / 5% (VIP)\n"
                f"• Recharge instantanée\n"
                f"• Support 24/7\n"
                f"• Garantie satisfaction",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📱 Commencer la recharge", callback_data=f"enter_phone_{service}")],
                    [InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")]
                ]),
                parse_mode='Markdown'
            )
            return CHOOSING

        elif data.startswith('help_'):
            service = data.split('_')[-1]
            operator_names = {'flexy': 'Flexy', 'mobilis': 'Mobilis', 'ooredoo': 'Ooredoo'}

            await query.edit_message_text(
                f"❓ **Aide Recharge {operator_names[service]}**\n\n"
                f"**Questions fréquentes :**\n\n"
                f"**Q: Combien de temps pour la recharge ?**\n"
                f"R: 2-5 minutes maximum après paiement\n\n"
                f"**Q: Quels modes de paiement ?**\n"
                f"R: BaridiMob, CCP, Virement bancaire\n\n"
                f"**Q: Y a-t-il des frais cachés ?**\n"
                f"R: Non, prix transparents + cashback\n\n"
                f"**Q: Que faire si problème ?**\n"
                f"R: Support 24/7 disponible instantanément\n\n"
                f"**Q: Puis-je recharger un autre numéro ?**\n"
                f"R: Oui, n'importe quel numéro algérien",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📱 Commencer la recharge", callback_data=f"enter_phone_{service}")],
                    [InlineKeyboardButton("📞 Contacter support", callback_data="human_support")],
                    [InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")]
                ]),
                parse_mode='Markdown'
            )
            return CHOOSING

        # === SUPPORT CLIENT ===
        elif data == "human_support":
            await query.edit_message_text(
                f"👨‍💼 **Support Client Humain**\n\n"
                f"🎯 **Notre équipe d'experts est là pour vous !**\n\n"
                f"📞 **Méthodes de contact :**\n"
                f"• Telegram : @CryptoDZSupport\n"
                f"• WhatsApp : +213 555 123 456\n"
                f"• Email : support@cryptodz.pro\n\n"
                f"⏰ **Disponibilité :**\n"
                f"• 24/7 pour urgences\n"
                f"• Réponse < 5 minutes en heures ouvrables\n"
                f"• Support VIP prioritaire\n\n"
                f"**Décrivez votre problème et nous vous aiderons !**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Chat Telegram", url="https://t.me/CryptoDZSupport")],
                    [InlineKeyboardButton("📞 Appel urgent", callback_data="emergency_call")],
                    [InlineKeyboardButton("🔙 Retour", callback_data="back_to_menu")]
                ]),
                parse_mode='Markdown'
            )
            return CHOOSING

        # === RETOUR AU MENU ===
        elif data == "back_to_menu":
            welcome_message = f"""
🎯 **Menu Principal CryptoDZ Pro**

🚀 **Services disponibles :**
• 💰 Achat/Vente USDT professionnel
• 📱 Recharges téléphoniques instantanées  
• 💶 Change Euro/DZD en magasin
• 📅 Rendez-vous personnalisés
• 🎁 Programme cashback et fidélité

📞 **Support 24/7** | 🔒 **100% sécurisé**

**Sélectionnez un service ci-dessous :**
            """

            reply_markup = ReplyKeyboardMarkup(get_professional_menu(), resize_keyboard=True)
            await query.edit_message_text(welcome_message, parse_mode='Markdown')

            # Envoyer un nouveau message avec le clavier
            await query.message.reply_text(
                "👇 **Choisissez une option :**",
                reply_markup=reply_markup
            )
            return CHOOSING

        elif data == "edit_payout_info":
            await query.edit_message_text(
                f"✏️ **Modification des Coordonnées**\n\n"
                f"📋 **Saisissez vos nouvelles informations :**\n\n"
                f"**Format requis :**\n"
                f"Nom: [Votre nom complet]\n"
                f"RIP: [Votre RIP BaridiMob]\n"
                f"Tél: [Votre numéro]\n\n"
                f"**Exemple :**\n"
                f"Nom: Ahmed Benali\n"
                f"RIP: 00799999123456789012\n"
                f"Tél: 0551234567\n\n"
                f"**Tapez vos nouvelles informations :**",
                parse_mode='Markdown'
            )
            context.user_data['waiting_payout_info'] = True
            return PAYOUT_INFO

        elif data == "cancel_transaction":
            await query.edit_message_text(
                "❌ **Transaction annulée**\n\n"
                "Vous pouvez recommencer quand vous voulez.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💸 Nouvelle vente USDT", callback_data="start_usdt_sell")],
                    [InlineKeyboardButton("🏠 Menu principal", callback_data="back_to_menu")]
                ])
            )
            context.user_data.clear()
            return CHOOSING

        # === GESTION DES CALLBACKS RESTANTS ===
        else:
            # Au lieu d'afficher une erreur, rediriger intelligemment
            if 'usdt' in data.lower():
                return await handle_callback_query(update.callback_query.copy_with_data("start_usdt_buy"), context)
            elif 'flexy' in data.lower() or 'mobilis' in data.lower() or 'ooredoo' in data.lower():
                service = 'flexy' if 'flexy' in data.lower() else 'mobilis' if 'mobilis' in data.lower() else 'ooredoo'
                return await handle_callback_query(update.callback_query.copy_with_data(f"enter_phone_{service}"), context)
            elif 'euro' in data.lower():
                return await handle_callback_query(update.callback_query.copy_with_data("calculate_euro"), context)
            elif 'rdv' in data.lower() or 'appointment' in data.lower():
                return await handle_callback_query(update.callback_query.copy_with_data("book_euro_appointment"), context)
            else:
                # Redirection vers menu principal pour callbacks non reconnus
                return await handle_callback_query(update.callback_query.copy_with_data("back_to_menu"), context)

    except Exception as e:
        logging.error(f"Erreur dans handle_callback_query: {e}")
        try:
            # Retour au menu en cas d'erreur
            reply_markup = ReplyKeyboardMarkup(get_professional_menu(), resize_keyboard=True)
            await query.edit_message_text(
                "🏠 **Retour au menu principal**\n\nChoisissez un service :",
                reply_markup=reply_markup
            )
        except:
            pass
        return CHOOSING

async def handle_amount_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère la saisie du montant avec validation complète"""
    try:
        text = update.message.text.strip()
        service = context.user_data.get('service')

        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError("Montant invalide")

            # Validation selon le service
            min_amount = 10 if service == 'usdt_buy' else 20 if service == 'usdt_sell' else 50

            if amount < min_amount:
                await update.message.reply_text(
                    f"❌ **Montant trop faible**\n\n"
                    f"Montant minimum: {min_amount} {'USDT' if 'usdt' in service else 'DZD'}\n"
                    f"Veuillez entrer un montant valide:"
                )
                return AMOUNT_DETAIL

            context.user_data['amount'] = amount

            if service == 'usdt_buy':
                rate = context.user_data.get('usdt_rate', DYNAMIC_RATES['usdt_buy']['rate'])
                total_dzd = amount * rate
                context.user_data['total_amount'] = total_dzd

                keyboard = [
                    [InlineKeyboardButton("📱 BaridiMob (Recommandé)", callback_data="pay_baridimob")],
                    [InlineKeyboardButton("🏦 CCP", callback_data="pay_ccp")],
                    [InlineKeyboardButton("🏛️ Virement bancaire", callback_data="pay_bank")],
                    [InlineKeyboardButton("❌ Annuler", callback_data="cancel_transaction")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await update.message.reply_text(
                    f"💰 **Achat USDT - Étape 2/4**\n\n"
                    f"📊 **Récapitulatif de commande :**\n"
                    f"💎 Quantité USDT: {amount:.4f} USDT\n"
                    f"📈 Taux: {rate:.2f} DZD/USDT\n"
                    f"💰 **Total à payer: {total_dzd:.2f} DZD**\n"
                    f"🎁 Cashback: {total_dzd * 0.02:.2f} DZD\n\n"
                    f"💳 **Choisissez votre méthode de paiement :**",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                return CONFIRMATION

            elif service == 'usdt_sell':
                seller = context.user_data.get('selected_seller')
                if seller:
                    dzd_amount = amount * seller['sell_rate']
                    context.user_data['total_amount'] = dzd_amount

                    await update.message.reply_text(
                        f"💸 **Vente USDT - Étape 2/4**\n\n"
                        f"📊 **Récapitulatif :**\n"
                        f"💎 Quantité: {amount:.4f} USDT\n"
                        f"📈 Taux: {seller['sell_rate']:.2f} DZD/USDT\n"
                        f"👤 Acheteur: {seller['name']}\n"
                        f"💰 **Vous recevrez: {dzd_amount:.2f} DZD**\n\n"
                        f"💳 **Étape suivante: Vos coordonnées de paiement**\n\n"
                        f"📋 **Veuillez fournir :**\n"
                        f"• Nom complet\n"
                        f"• RIP BaridiMob ou numéro CCP\n"
                        f"• Numéro de téléphone\n\n"
                        f"💡 **Format attendu :**\n"
                        f"Nom: Mohamed Ali Benali\n"
                        f"RIP: 00799999123456789012\n"
                        f"Tél: 0551234567\n\n"
                        f"**Envoyez toutes ces informations :**",
                        parse_mode='Markdown'
                    )
                    context.user_data['waiting_payout_info'] = True
                    return PAYOUT_INFO

            else:
                # Gestion des recharges mobiles
                phone = context.user_data.get('phone')
                if not phone:
                    await update.message.reply_text(
                        "❌ **Erreur: Numéro manquant**\n\n"
                        "Veuillez d'abord saisir votre numéro de téléphone."
                    )
                    return PHONE_INPUT

                total_price = calculate_service_price(service, amount)
                context.user_data['total_amount'] = total_price

                keyboard = [
                    [InlineKeyboardButton("📱 BaridiMob", callback_data="pay_baridimob")],
                    [InlineKeyboardButton("🏦 CCP", callback_data="pay_ccp")],
                    [InlineKeyboardButton("❌ Annuler", callback_data="cancel_transaction")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await update.message.reply_text(
                    f"📱 **Recharge {service.title()} - Confirmation**\n\n"
                    f"📞 **Numéro:** {format_phone_number(phone)}\n"
                    f"💰 **Montant:** {amount:.0f} DZD\n"
                    f"💎 **Total:** {total_price:.2f} DZD\n"
                    f"🎁 **Cashback:** {total_price * 0.02:.2f} DZD\n\n"
                    f"💳 **Choisissez votre méthode de paiement :**",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                return CONFIRMATION

        except ValueError:
            await update.message.reply_text(
                "❌ **Format invalide**\n\n"
                "Veuillez entrer un nombre valide.\n"
                "Exemple: 100"
            )
            return AMOUNT_DETAIL

    except Exception as e:
        logging.error(f"Erreur dans handle_amount_detail: {e}")
        await update.message.reply_text(
            "❌ **Erreur de traitement**\n\n"
            "Veuillez réessayer ou utilisez /start pour redémarrer."
        )
        return CHOOSING

async def handle_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère la saisie du numéro de téléphone"""
    try:
        phone = update.message.text.strip()

        if not validate_algerian_phone(phone):
            await update.message.reply_text(
                "❌ **Numéro invalide**\n\n"
                "Format accepté :\n"
                "• 0551234567\n"
                "• +213551234567\n"
                "• 213551234567\n\n"
                "Réessayez :"
            )
            return PHONE_INPUT

        context.user_data['phone'] = format_phone_number(phone)
        service = context.user_data.get('service')

        keyboard = [
            [InlineKeyboardButton("100 DZD", callback_data="amount_100")],
            [InlineKeyboardButton("200 DZD", callback_data="amount_200")],
            [InlineKeyboardButton("500 DZD", callback_data="amount_500")],
            [InlineKeyboardButton("1000 DZD", callback_data="amount_1000")],
            [InlineKeyboardButton("💰 Autre montant", callback_data="custom_amount")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"✅ **Numéro validé !**\n\n"
            f"📞 **Numéro :** {context.user_data['phone']}\n"
            f"📱 **Opérateur :** {service.title()}\n\n"
            f"💰 **Choisissez le montant de recharge :**",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return AMOUNT_DETAIL

    except Exception as e:
        logging.error(f"Erreur dans handle_phone_input: {e}")
        await update.message.reply_text(
            "❌ **Erreur de traitement**\n\n            "Veuillez réessayer ou utilisez /start."
        )
        return CHOOSING

async def handle_payout_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère les informations de paiement pour la vente USDT"""
    try:
        # Si on attend un hash de transaction
        if context.user_data.get('waiting_tx_hash'):
            tx_hash = update.message.text.strip()

            # Validation basique du hash
            if len(tx_hash) < 40 or not all(c.isalnum() for c in tx_hash):
                await update.message.reply_text(
                    "❌ **Hash invalide**\n\n"
                    "Le hash doit contenir 40-64 caractères alphanumériques.\n\n"
                    "**Exemple valide :**\n"
                    "`a1b2c3d4e5f6789012345678901234567890abcdef`\n\n"
                    "**Réessayez :**",
                    parse_mode='Markdown'
                )
                return PAYMENT_PROOF

            # Enregistrer le hash
            context.user_data['tx_hash'] = tx_hash
            context.user_data['waiting_tx_hash'] = False

            # Créer la transaction de vente
            transaction_id = str(uuid.uuid4())[:12]
            seller = context.user_data.get('selected_seller', {})
            amount = context.user_data.get('amount', 0)
            payout_info = context.user_data.get('payout_info', '')

            # Enregistrer en base de données
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO usdt_sales (id, user_id, seller_id, amount, rate, dzd_amount, payout_info, tx_hash, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                transaction_id,
                update.effective_user.id,
                seller.get('id', 'seller_1'),
                amount,
                seller.get('sell_rate', 270),
                amount * seller.get('sell_rate', 270),
                payout_info,
                tx_hash,
                'pending_verification',
                datetime.now().isoformat()
            ))
            conn.commit()
            conn.close()

            await update.message.reply_text(
                f"✅ **Transaction enregistrée !**\n\n"
                f"🆔 **ID Transaction :** `{transaction_id}`\n"
                f"🔗 **Hash fourni :** `{tx_hash[:20]}...`\n"
                f"💰 **Montant :** {amount:.4f} USDT\n"
                f"💵 **Vous recevrez :** {amount * seller.get('sell_rate', 270):.2f} DZD\n\n"
                f"⏳ **Vérification en cours...**\n"
                f"🔍 Notre système vérifie automatiquement votre transaction sur la blockchain\n\n"
                f"⏱️ **Délai de paiement :** 15-30 minutes après validation\n"
                f"📱 Vous recevrez une notification dès confirmation\n\n"
                f"🔒 **Transaction sécurisée et garantie**",
                reply_markup=ReplyKeyboardMarkup(get_professional_menu(), resize_keyboard=True),
                parse_mode='Markdown'
            )

            # Notifier l'admin
            try:
                if telegram_app:
                    await telegram_app.bot.send_message(
                        ADMIN_ID,
                        f"🔔 **Nouvelle vente USDT à vérifier**\n\n"
                        f"🆔 ID: `{transaction_id}`\n"
                        f"👤 Utilisateur: {update.effective_user.first_name}\n"
                        f"💰 Montant: {amount:.4f} USDT\n"
                        f"🔗 Hash: `{tx_hash}`\n"
                        f"📍 [Vérifier sur TronScan](https://tronscan.org/#/transaction/{tx_hash})",
                        parse_mode='Markdown'
                    )
            except:
                pass

            return CHOOSING

        # Si on attend des infos de paiement
        if not context.user_data.get('waiting_payout_info'):
            return CHOOSING

        payout_info = update.message.text.strip()
        context.user_data['payout_info'] = payout_info
        context.user_data['waiting_payout_info'] = False

        # Validation basique des informations
        if len(payout_info) < 20:
            await update.message.reply_text(
                "❌ **Informations incomplètes**\n\n"
                "Veuillez fournir au minimum :\n"
                "• Nom complet\n"
                "• RIP BaridiMob ou numéro CCP\n"
                "• Numéro de téléphone\n\n"
                "**Réessayez :**",
                parse_mode='Markdown'
            )
            context.user_data['waiting_payout_info'] = True
            return PAYOUT_INFO

        seller = context.user_data.get('selected_seller', {})
        amount = context.user_data.get('amount', 0)
        dzd_amount = amount * seller.get('sell_rate', 270)

        keyboard = [
            [InlineKeyboardButton("➡️ Continuer vers envoi USDT", callback_data="continue_to_usdt_send")],
            [InlineKeyboardButton("✏️ Modifier mes infos", callback_data="edit_payout_info")],
            [InlineKeyboardButton("❌ Annuler", callback_data="cancel_transaction")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"✅ **Informations enregistrées !**\n\n"
            f"📋 **Vos coordonnées :**\n"
            f"{payout_info}\n\n"
            f"💰 **Vous recevrez :** {dzd_amount:.2f} DZD\n"
            f"⏱️ **Délai :** 15-30 min après confirmation\n\n"
            f"➡️ **Prêt pour l'étape suivante ?**",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

        return CONFIRMATION

    except Exception as e:
        logging.error(f"Erreur dans handle_payout_info: {e}")
        await update.message.reply_text(
            "❌ **Erreur de traitement**\n\n"
            "Veuillez réessayer ou utilisez /start."
        )
        return CHOOSING

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère les entrées texte génériques avec routage intelligent"""
    try:
        text = update.message.text

        # Si on attend un hash de transaction ou des infos de paiement pour vente USDT
        if context.user_data.get('waiting_payout_info') or context.user_data.get('waiting_tx_hash'):
            return await handle_payout_info(update, context)

        # Si on attend un montant
        if context.user_data.get('service') and not context.user_data.get('amount'):
            return await handle_amount_detail(update, context)

        # Si on attend un numéro de téléphone
        if context.user_data.get('service') in ['flexy', 'mobilis', 'ooredoo'] and not context.user_data.get('phone'):
            return await handle_phone_input(update, context)

        # Sinon, traiter comme choix du menu
        return await handle_choice(update, context)

    except Exception as e:
        logging.error(f"Erreur dans handle_text_input: {e}")
        await update.message.reply_text(
            "❌ **Erreur de traitement**\n\n"
            "Utilisez /start pour redémarrer ou contactez le support."
        )
        return CHOOSING

async def handle_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère les callbacks de paiement"""
    try:
        query = update.callback_query
        await query.answer()

        data = query.data

        if data.startswith('pay_'):
            payment_method = data.split('_')[1]
            service = context.user_data.get('service')
            amount = context.user_data.get('amount', 0)
            total_amount = context.user_data.get('total_amount', 0)

            context.user_data['payment_method'] = payment_method

            # Générer référence de paiement
            payment_ref = f"CRY{str(uuid.uuid4())[:8].upper()}"
            context.user_data['payment_ref'] = payment_ref

            if payment_method == 'baridimob':
                rip = "0799999002264673222"
                await query.edit_message_text(
                    f"📱 **Paiement BaridiMob**\n\n"
                    f"💰 **Montant à payer :** {total_amount:.2f} DZD\n"
                    f"🏦 **RIP :** `{rip}`\n"
                    f"📋 **Référence :** `{payment_ref}`\n\n"
                    f"📋 **Instructions :**\n"
                    f"1. Ouvrez BaridiMob\n"
                    f"2. Transfert → Vers RIP\n"
                    f"3. Saisissez le RIP ci-dessus\n"
                    f"4. Montant : {total_amount:.2f} DZD\n"
                    f"5. Référence : {payment_ref}\n"
                    f"6. Validez le transfert\n"
                    f"7. Envoyez la capture d'écran\n\n"
                    f"⏰ **Délai de paiement :** 10 minutes",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📤 Envoyer preuve", callback_data="send_proof")],
                        [InlineKeyboardButton("❓ Aide paiement", callback_data="help_payment")],
                        [InlineKeyboardButton("❌ Annuler", callback_data="cancel_transaction")]
                    ]),
                    parse_mode='Markdown'
                )

            elif payment_method == 'ccp':
                await query.edit_message_text(
                    f"🏦 **Paiement CCP**\n\n"
                    f"💰 **Montant :** {total_amount:.2f} DZD\n"
                    f"📋 **Référence :** `{payment_ref}`\n\n"
                    f"🏦 **Coordonnées CCP :**\n"
                    f"Compte : 0021234567 - Clé : 89\n"
                    f"Nom : CRYPTO DZ SERVICE\n"
                    f"Centre : ALGER CENTRE\n\n"
                    f"📋 **Instructions :**\n"
                    f"1. Allez à un bureau de poste\n"
                    f"2. Versement au compte ci-dessus\n"
                    f"3. Montant : {total_amount:.2f} DZD\n"
                    f"4. Référence : {payment_ref}\n"
                    f"5. Gardez le reçu\n"
                    f"6. Envoyez photo du reçu\n\n"
                    f"⏰ **Délai :** 30 minutes",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📤 Envoyer reçu", callback_data="send_proof")],
                        [InlineKeyboardButton("❓ Aide", callback_data="help_payment")],
                        [InlineKeyboardButton("❌ Annuler", callback_data="cancel_transaction")]
                    ]),
                    parse_mode='Markdown'
                )

            elif payment_method == 'bank':
                await query.edit_message_text(
                    f"🏛️ **Virement Bancaire**\n\n"
                    f"💰 **Montant :** {total_amount:.2f} DZD\n"
                    f"📋 **Référence :** `{payment_ref}`\n\n"
                    f"🏦 **Coordonnées bancaires :**\n"
                    f"Banque : BNA\n"
                    f"RIB : 0123456789012345678901234\n"
                    f"Nom : CRYPTO DZ SERVICE SARL\n"
                    f"Agence : ALGER CENTRE\n\n"
                    f"📋 **Instructions :**\n"
                    f"1. Virement vers le RIB ci-dessus\n"
                    f"2. Montant exact : {total_amount:.2f} DZD\n"
                    f"3. Motif : {payment_ref}\n"
                    f"4. Conservez le bordereau\n"
                    f"5. Envoyez photo du bordereau\n\n"
                    f"⏰ **Délai :** 60 minutes",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📤 Envoyer bordereau", callback_data="send_proof")],
                        [InlineKeyboardButton("❓ Aide", callback_data="help_payment")],
                        [InlineKeyboardButton("❌ Annuler", callback_data="cancel_transaction")]
                    ]),
                    parse_mode='Markdown'
                )

            return PAYMENT_PROOF

        elif data == "send_proof":
            await query.edit_message_text(
                f"📤 **Envoi de Preuve de Paiement**\n\n"
                f"📸 **Envoyez maintenant une photo claire de :**\n"
                f"• Capture d'écran BaridiMob (si BaridiMob)\n"
                f"• Reçu CCP (si CCP)\n"
                f"• Bordereau bancaire (si virement)\n\n"
                f"✅ **Conseils pour une photo parfaite :**\n"
                f"• Éclairage suffisant\n"
                f"• Tous les détails visibles\n"
                f"• Photo non floue\n"
                f"• Montant et référence lisibles\n\n"
                f"📱 **Utilisez l'appareil photo de Telegram**",
                parse_mode='Markdown'
            )
            return PAYMENT_PROOF

        return CHOOSING

    except Exception as e:
        logging.error(f"Erreur dans handle_payment_callback: {e}")
        await query.edit_message_text("❌ Erreur de traitement. Réessayez.")
        return CHOOSING

async def handle_photo_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère l'upload de preuve de paiement"""
    try:
        if not update.message.photo:
            await update.message.reply_text("❌ Veuillez envoyer une photo valide.")
            return PAYMENT_PROOF

        # Traitement simplifié de la photo
        file = await update.message.photo[-1].get_file()
        photo_bytes = await file.download_as_bytearray()
        photo_base64 = base64.b64encode(photo_bytes).decode('utf-8')

        transaction_id = context.user_data.get('transaction_id', str(uuid.uuid4())[:8])
        proof_id = str(uuid.uuid4())[:8]

        # Enregistrement simplifié
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO payment_proofs (id, transaction_id, user_id, photo_data, uploaded_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (proof_id, transaction_id, update.effective_user.id, photo_base64, datetime.now().isoformat()))
        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"✅ **Preuve reçue !**\n\n"
            f"🆔 **Transaction :** `{transaction_id}`\n"
            f"📄 **Preuve :** `{proof_id}`\n\n"
            f"⏳ **En cours de vérification automatique...**\n"
            f"⏱️ **Délai :** 5-30 minutes\n\n"
            f"📱 Vous recevrez une notification dès validation.",
            reply_markup=ReplyKeyboardMarkup(get_professional_menu(), resize_keyboard=True),
            parse_mode='Markdown'
        )

        return CHOOSING

    except Exception as e:
        logging.error(f"Erreur dans handle_photo_proof: {e}")
        await update.message.reply_text(
            "❌ **Erreur lors de l'upload**\n\n"
            "Veuillez réessayer avec une autre photo."
        )
        return PAYMENT_PROOF

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Annule la conversation"""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ **Opération annulée**",
        reply_markup=ReplyKeyboardMarkup(get_professional_menu(), resize_keyboard=True)
    )
    return CHOOSING

# Interface Web Flask
@app.route('/')
def dashboard():
    """Dashboard admin principal"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Statistiques
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM transactions WHERE status = "pending"')
        pending_transactions = cursor.fetchone()[0]

        cursor.execute('SELECT SUM(amount) FROM transactions WHERE status = "completed"')
        total_volume = cursor.fetchone()[0] or 0

        cursor.execute('SELECT COUNT(*) FROM payment_proofs WHERE verified = 0')
        pending_proofs = cursor.fetchone()[0]

        conn.close()

        stats = {
            'total_users': total_users,
            'pending_transactions': pending_transactions,
            'total_volume': total_volume,
            'pending_proofs': pending_proofs
        }

        return render_template('dashboard.html', stats=stats)
    except Exception as e:
        logging.error(f"Erreur dashboard: {e}")
        return render_template('dashboard.html', stats={})

@app.route('/admin/proofs')
def admin_proofs():
    """Gestion des preuves de paiement"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT p.*, t.service_type, t.amount, u.first_name 
            FROM payment_proofs p
            LEFT JOIN transactions t ON p.transaction_id = t.id
            LEFT JOIN users u ON p.user_id = u.user_id
            WHERE p.verified = 0
            ORDER BY p.uploaded_at DESC
        ''')
        pending_proofs = cursor.fetchall()
        conn.close()

        return render_template('admin_proofs.html', proofs=pending_proofs)
    except Exception as e:
        logging.error(f"Erreur admin proofs: {e}")
        return render_template('admin_proofs.html', proofs=[])

@app.route('/api/approve-proof', methods=['POST'])
def approve_proof():
    """API pour approuver une preuve"""
    proof_id = request.json.get('proof_id')

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE payment_proofs SET verified = 1 WHERE id = ?', (proof_id,))
    conn.commit()
    conn.close()

    return jsonify({'success': True})

def run_flask():
    """Lance le serveur Flask"""
    app.run(host='0.0.0.0', port=5000, debug=False)

def main():
    """Fonction principale optimisée"""
    print("🚀 Démarrage de CryptoDZ Pro Bot...")

    # Initialisation de la base de données
    print("📊 Initialisation de la base de données...")
    init_database()

    # Démarrage de l'interface web
    print("🌐 Démarrage de l'interface web...")
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Configuration Telegram optimisée
    print("🤖 Configuration du bot Telegram...")
    app_telegram = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()
    global telegram_app
    telegram_app = app_telegram

    # Gestionnaire de conversation principal avec gestion d'erreurs complète
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CHOOSING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_choice),
                CallbackQueryHandler(handle_callback_query),
                MessageHandler(filters.PHOTO, handle_photo_proof)
            ],
            SERVICE_DETAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input),
                CallbackQueryHandler(handle_callback_query)
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
                MessageHandler(filters.PHOTO, handle_photo_proof),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input),
                CallbackQueryHandler(handle_callback_query)
            ],
            PAYOUT_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payout_info),
                CallbackQueryHandler(handle_callback_query)
            ],
            SUPPORT_CHAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input),
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

    # Gestionnaire d'erreur global amélioré
    async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Gestionnaire d'erreurs global pour éviter les crashes"""
        logging.error(f"Erreur Telegram: {context.error}")
        try:
            if update and update.effective_message:
                await update.effective_message.reply_text(
                    "❌ **Erreur temporaire**\n\n"
                    "Notre équipe technique a été notifiée.\n"
                    "Utilisez /start pour redémarrer.",
                    reply_markup=ReplyKeyboardMarkup(get_professional_menu(), resize_keyboard=True)
                )
        except Exception as e:
            logging.error(f"Erreur dans error_handler: {e}")

    # Enregistrement des gestionnaires
    app_telegram.add_handler(conv_handler)
    app_telegram.add_error_handler(error_handler)

    print("✅ Bot configuré avec succès!")
    print("🌐 Interface web: http://0.0.0.0:5000")
    print("🤖 Bot Telegram prêt!")

    # Lancement du bot
    try:
        app_telegram.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logging.error(f"Erreur critique: {e}")
        print("❌ Erreur critique lors du démarrage du bot")

if __name__ == '__main__':
    main()