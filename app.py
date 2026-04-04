# app.py
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import enum
import json
import pandas as pd
import io
import secrets
import os
import logging
from logging.handlers import RotatingFileHandler

# ИМПОРТЫ ДЛЯ РАСШИРЕННЫХ ФУНКЦИЙ
from flask_mail import Mail, Message
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
# from flask_babelex import Babel, gettext
from flask_migrate import Migrate
from marshmallow import Schema, fields, validate
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from werkzeug.utils import secure_filename
from PIL import Image
from celery import Celery
import redis
import qrcode
import pyotp
import barcode 
from barcode.writer import ImageWriter

# Инициализация приложения
app = Flask(__name__)

# Конфигурация приложения
app.config['SECRET_KEY'] = 'your-secret-key-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///warehouse_advanced.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Конфигурация для Flask-Mail
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'your-email@gmail.com'
app.config['MAIL_PASSWORD'] = 'your-app-password'
app.config['MAIL_DEFAULT_SENDER'] = 'your-email@gmail.com'

# Конфигурация для JWT
app.config['JWT_SECRET_KEY'] = 'jwt-secret-key-change-in-production'
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=24)

# Конфигурация для Redis и Celery
app.config['REDIS_URL'] = 'redis://localhost:6379/0'
app.config['SOCKETIO_MESSAGE_QUEUE'] = 'redis://'

# Настройки загрузки файлов
app.config['UPLOAD_FOLDER'] = 'static/uploads/products'
app.config['BARCODE_FOLDER'] = 'static/barcodes'
app.config['QRCODE_FOLDER'] = 'static/qrcodes'
app.config['DOCUMENTS_FOLDER'] = 'static/documents'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf'}

# ИНИЦИАЛИЗАЦИЯ РАСШИРЕНИЙ
db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
mail = Mail(app)
jwt = JWTManager(app)
CORS(app)
#babel = Babel(app)
socketio = SocketIO(app, cors_allowed_origins="*", message_queue=app.config['SOCKETIO_MESSAGE_QUEUE'])

# Инициализация Redis
redis_client = redis.from_url(app.config['REDIS_URL'])

# Инициализация Celery
celery = Celery(app.name, broker=app.config['REDIS_URL'])
celery.conf.update(app.config)

# Создаем необходимые директории
for folder in [app.config['UPLOAD_FOLDER'], app.config['BARCODE_FOLDER'], 
               app.config['QRCODE_FOLDER'], app.config['DOCUMENTS_FOLDER'], 
               'logs', 'ml_models']:
    os.makedirs(folder, exist_ok=True)

login_manager.login_view = 'login'
login_manager.login_message = 'Пожалуйста, войдите в систему.'

# НАСТРОЙКА ЛОГГИРОВАНИЯ
if not app.debug:
    if not os.path.exists('logs'):
        os.mkdir('logs')
    file_handler = RotatingFileHandler('logs/warehouse.log', maxBytes=10240, backupCount=10)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
    ))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info('Warehouse system startup with advanced features')

# Локализация
#@babel.localeselector
# def get_locale():
    # if current_user.is_authenticated and hasattr(current_user, 'language'):
        # return current_user.language
    # return request.accept_languages.best_match(['ru', 'en', 'kk']) or 'ru'

# Модели данных (Enum классы)
class UserRole(enum.Enum):
    ADMIN = "admin"
    STOREKEEPER = "storekeeper"
    EMPLOYEE = "employee"
    EXTERNAL = "external"

class InventoryStatus(enum.Enum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class OperationType(enum.Enum):
    RECEIPT = "receipt"
    SHIPMENT = "shipment"
    ADJUSTMENT = "adjustment"

class OrderStatus(enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class SupplyStatus(enum.Enum):
    PLANNED = "planned"
    ORDERED = "ordered"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"

# Модели данных (существующие)
class Inventory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    status = db.Column(db.Enum(InventoryStatus), default=InventoryStatus.PLANNED)
    start_date = db.Column(db.DateTime)
    end_date = db.Column(db.DateTime)
    notes = db.Column(db.Text)
    total_difference = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    inventory_items = db.relationship('InventoryItem', backref='inventory', lazy=True, cascade="all, delete-orphan")

class InventoryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actual_quantity = db.Column(db.Integer, nullable=False)
    system_quantity = db.Column(db.Integer, nullable=False)
    difference = db.Column(db.Integer)
    
    inventory_id = db.Column(db.Integer, db.ForeignKey('inventory.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    product = db.relationship('Product', backref='inventory_items')

# РАСШИРЕННАЯ МОДЕЛЬ USER
class User(UserMixin, db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(128))
    role = db.Column(db.Enum(UserRole), default=UserRole.EMPLOYEE)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Новые поля для расширенных функций
    language = db.Column(db.String(10), default='ru')
    telegram_chat_id = db.Column(db.String(100), nullable=True)
    notification_preferences = db.Column(db.Text, default=json.dumps({
        'email': True,
        'telegram': False,
        'socket': True,
        'low_stock': True,
        'new_orders': True,
        'returns': True
    }))
    two_factor_enabled = db.Column(db.Boolean, default=False)
    
    
    def set_password(self, password):
        self.password = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password, password)
    
    def is_admin(self):
        return self.role == UserRole.ADMIN
    
    def is_storekeeper(self):
        return self.role == UserRole.STOREKEEPER
    
    def is_employee(self):
        return self.role == UserRole.EMPLOYEE
    
    def is_external(self):
        return self.role == UserRole.EXTERNAL

# РАСШИРЕННАЯ МОДЕЛЬ PRODUCT
class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    sku = db.Column(db.String(50), unique=True)
    barcode = db.Column(db.String(100))
    unit = db.Column(db.String(20), nullable=False)
    price = db.Column(db.Float, default=0.0)
    cost_price = db.Column(db.Float, default=0.0)
    min_stock = db.Column(db.Integer, default=5)
    max_stock = db.Column(db.Integer, default=1000)
    location = db.Column(db.String(100))
    is_active = db.Column(db.Boolean, default=True)
    current_stock = db.Column(db.Integer, default=0)
    image_filename = db.Column(db.String(255))
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'))
    
    # Новые поля для расширенных функций
    weight = db.Column(db.Float, nullable=True)
    volume = db.Column(db.Float, nullable=True)
    tax_rate_id = db.Column(db.Integer, db.ForeignKey('tax_rate.id'), nullable=True)
    currency_code = db.Column(db.String(3), db.ForeignKey('currency.code'), nullable=True)
    barcode_image = db.Column(db.String(255), nullable=True)
    qr_code = db.Column(db.String(255), nullable=True)
    warehouse_zone_id = db.Column(db.Integer, db.ForeignKey('warehouse_zone.id'), nullable=True)
    storage_cell_id = db.Column(db.Integer, db.ForeignKey('storage_cell.id'), nullable=True)
    batch_number = db.Column(db.String(100), nullable=True)
    expiry_date = db.Column(db.DateTime, nullable=True)
    


# Модели для расширенных функций

class Currency(db.Model):
    """Поддержка валют"""
    code = db.Column(db.String(3), primary_key=True)  # USD, EUR, RUB
    symbol = db.Column(db.String(5))
    name = db.Column(db.String(50))
    rate_to_rub = db.Column(db.Float)  # Курс к рублю
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

class TaxRate(db.Model):
    """Налоговые ставки"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50))
    rate = db.Column(db.Float)
    applies_to = db.Column(db.String(50))
    is_active = db.Column(db.Boolean, default=True)

class WarehouseZone(db.Model):
    """Зоны склада"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    code = db.Column(db.String(20), unique=True)
    type = db.Column(db.String(50))  # receiving, storage, shipping, quarantine
    capacity = db.Column(db.Integer)
    temperature = db.Column(db.Float)
    humidity = db.Column(db.Float)
    is_active = db.Column(db.Boolean, default=True)
    
    cells = db.relationship('StorageCell', backref='zone', lazy=True)

class StorageCell(db.Model):
    """Ячейки хранения"""
    id = db.Column(db.Integer, primary_key=True)
    zone_id = db.Column(db.Integer, db.ForeignKey('warehouse_zone.id'))
    code = db.Column(db.String(20))
    barcode = db.Column(db.String(50), unique=True)
    max_weight = db.Column(db.Float)
    max_volume = db.Column(db.Float)
    current_product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=True)
    current_quantity = db.Column(db.Integer, default=0)
    is_occupied = db.Column(db.Boolean, default=False)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)

class ReturnRequest(db.Model):
    """Система возвратов"""
    __tablename__ = 'return_request'
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    reason = db.Column(db.Text)
    condition = db.Column(db.String(50))  # new, used, damaged
    status = db.Column(db.String(50), default='pending')  # pending, approved, rejected, completed
    refund_amount = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    processed_at = db.Column(db.DateTime)
    processed_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    
    order = db.relationship('Order', foreign_keys=[order_id], backref='returns')
    user = db.relationship('User', foreign_keys=[user_id], backref='return_requests')
    product = db.relationship('Product', foreign_keys=[product_id], backref='return_requests')
    processor = db.relationship('User', foreign_keys=[processed_by], backref='processed_returns')

class Document(db.Model):
    """Документооборот"""
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50))  # invoice, waybill, receipt, contract
    number = db.Column(db.String(50), unique=True)
    file_path = db.Column(db.String(255))
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=True)
    supply_id = db.Column(db.Integer, db.ForeignKey('supply_order.id'), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    
    total_amount = db.Column(db.Float)
    tax_amount = db.Column(db.Float)
    signed_by = db.Column(db.String(100))
    signed_at = db.Column(db.DateTime)
    
    user = db.relationship('User', backref='documents')

class MarketplaceIntegration(db.Model):
    """Интеграция с маркетплейсами"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50))
    marketplace_type = db.Column(db.String(50))  # ozon, wildberries, yandex
    api_key = db.Column(db.String(255))
    api_secret = db.Column(db.String(255))
    seller_id = db.Column(db.String(100))
    
    sync_products = db.Column(db.Boolean, default=True)
    sync_orders = db.Column(db.Boolean, default=True)
    sync_stock = db.Column(db.Boolean, default=True)
    sync_prices = db.Column(db.Boolean, default=True)
    
    last_sync = db.Column(db.DateTime)
    sync_status = db.Column(db.String(50), default='pending')
    error_message = db.Column(db.Text)
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='marketplace_integrations')

class LoyaltyProgram(db.Model):
    """Программа лояльности"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    description = db.Column(db.Text)
    discount_percent = db.Column(db.Float)
    min_purchases = db.Column(db.Integer)
    min_amount = db.Column(db.Float)
    valid_from = db.Column(db.DateTime)
    valid_until = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)
    
    customers = db.relationship('Customer', backref='loyalty_program')

class Customer(db.Model):
    """Расширенная информация о клиентах"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True)
    loyalty_program_id = db.Column(db.Integer, db.ForeignKey('loyalty_program.id'), nullable=True)
    bonus_points = db.Column(db.Integer, default=0)
    total_purchases = db.Column(db.Float, default=0)
    total_orders = db.Column(db.Integer, default=0)
    last_purchase_date = db.Column(db.DateTime)
    preferred_categories = db.Column(db.Text)  # JSON с категориями
    communication_preferences = db.Column(db.Text)  # email, sms, push
    
    user = db.relationship('User', backref='customer_profile')

class TwoFactorAuth(db.Model):
    """Двухфакторная аутентификация"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True)
    secret_key = db.Column(db.String(32))
    backup_codes = db.Column(db.Text)  # JSON с кодами
    is_enabled = db.Column(db.Boolean, default=False)
    last_used = db.Column(db.DateTime)
    failed_attempts = db.Column(db.Integer, default=0)
    
    user = db.relationship('User', backref='two_factor_auth')

class DemandForecast(db.Model):
    """Прогнозирование спроса"""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    forecast_date = db.Column(db.DateTime)
    predicted_demand = db.Column(db.Integer)
    confidence_level = db.Column(db.Float)
    factors_considered = db.Column(db.Text)  # JSON с факторами
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    product = db.relationship('Product', foreign_keys=[product_id], backref='forecasts')

class PurchaseOrderSuggestion(db.Model):
    """Автоматические предложения по закупкам"""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    suggested_quantity = db.Column(db.Integer)
    suggested_supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=True)
    expected_price = db.Column(db.Float)
    urgency = db.Column(db.String(20))  # critical, high, medium, low
    based_on = db.Column(db.Text)  # JSON с данными для расчета
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_approved = db.Column(db.Boolean, default=False)
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    
    product = db.relationship('Product', foreign_keys=[product_id], backref='purchase_suggestions')
    supplier = db.relationship('Supplier', backref='purchase_suggestions')
    approver = db.relationship('User', foreign_keys=[approved_by])

class Notification(db.Model):
    """Уведомления"""
    __tablename__ = 'notification'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    title = db.Column(db.String(200))
    message = db.Column(db.Text)
    type = db.Column(db.String(50))  # low_stock, new_order, return_request, system, security
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=True)
    return_id = db.Column(db.Integer, nullable=True)
    
    role = db.Column(db.String(50), nullable=True)  # admin, storekeeper, all
    
    user = db.relationship('User', foreign_keys=[user_id], backref='notifications')
    product = db.relationship('Product')
    order = db.relationship('Order')

# Существующие модели (Category, Supplier, Operation, Order, SupplyOrder, SupplyItem, OrderItem, AuditLog)
class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    products = db.relationship('Product', backref='category', lazy=True)

class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    contact_person = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    email = db.Column(db.String(120))
    address = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    products = db.relationship('Product', backref='supplier', lazy=True)
    supply_orders = db.relationship('SupplyOrder', backref='supplier', lazy=True)

class Operation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.Enum(OperationType), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    previous_stock = db.Column(db.Integer)
    new_stock = db.Column(db.Integer)
    document_number = db.Column(db.String(50))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', foreign_keys=[user_id], backref='operations')
    product = db.relationship('Product', foreign_keys=[product_id], backref='operations')

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(50), unique=True)
    status = db.Column(db.Enum(OrderStatus), default=OrderStatus.PENDING)
    customer_name = db.Column(db.String(200))
    customer_email = db.Column(db.String(120))
    customer_phone = db.Column(db.String(20))
    total_amount = db.Column(db.Float, default=0.0)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    
    # Новые поля
    marketplace_integration_id = db.Column(db.Integer, db.ForeignKey('marketplace_integration.id'), nullable=True)
    external_order_id = db.Column(db.String(100), nullable=True)
    delivery_service = db.Column(db.String(100), nullable=True)
    tracking_number = db.Column(db.String(100), nullable=True)
    document_id = db.Column(db.Integer, db.ForeignKey('document.id'), nullable=True)
    
    order_items = db.relationship('OrderItem', backref='order', lazy=True, cascade="all, delete-orphan")
    user = db.relationship('User', foreign_keys=[user_id], backref='orders')

class SupplyOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(50), unique=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'))
    status = db.Column(db.Enum(SupplyStatus), default=SupplyStatus.PLANNED)
    order_date = db.Column(db.DateTime, default=datetime.utcnow)
    expected_date = db.Column(db.DateTime)
    delivery_date = db.Column(db.DateTime)
    total_amount = db.Column(db.Float, default=0.0)
    notes = db.Column(db.Text)
    
    supply_items = db.relationship('SupplyItem', backref='supply_order', lazy=True, cascade="all, delete-orphan")

class SupplyItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    
    supply_order_id = db.Column(db.Integer, db.ForeignKey('supply_order.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    product = db.relationship('Product', foreign_keys=[product_id], backref='supply_items')

class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    product = db.relationship('Product', foreign_keys=[product_id], backref='order_items')

class AuditLog(db.Model):
    __tablename__ = 'audit_log'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    action = db.Column(db.String(200), nullable=False)
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', foreign_keys=[user_id], backref='audit_logs')

# Загрузчик пользователя
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# СХЕМЫ MARSHMALLOW ДЛЯ API
class ProductSchema(Schema):
    id = fields.Int(dump_only=True)
    name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    description = fields.Str()
    sku = fields.Str(dump_only=True)
    barcode = fields.Str()
    unit = fields.Str(required=True)
    price = fields.Float(validate=validate.Range(min=0))
    cost_price = fields.Float(validate=validate.Range(min=0))
    min_stock = fields.Int(validate=validate.Range(min=0))
    max_stock = fields.Int(validate=validate.Range(min=1))
    location = fields.Str()
    current_stock = fields.Int(dump_only=True)
    is_active = fields.Bool()
    category_id = fields.Int()
    supplier_id = fields.Int()
    image_filename = fields.Str(dump_only=True)
    image_url = fields.Method("get_image_url")
    
    # Новые поля
    weight = fields.Float()
    volume = fields.Float()
    tax_rate_id = fields.Int()
    currency_code = fields.Str()
    warehouse_zone_id = fields.Int()
    storage_cell_id = fields.Int()
    batch_number = fields.Str()
    expiry_date = fields.DateTime()

    def get_image_url(self, obj):
        return get_product_image_url(obj)

class OperationSchema(Schema):
    id = fields.Int(dump_only=True)
    type = fields.Str(required=True)
    quantity = fields.Int(required=True)
    product_id = fields.Int(required=True)
    document_number = fields.Str()
    notes = fields.Str()
    created_at = fields.DateTime(dump_only=True)

class UserSchema(Schema):
    id = fields.Int(dump_only=True)
    username = fields.Str(required=True)
    email = fields.Str(required=True)
    role = fields.Str()
    is_active = fields.Bool()
    language = fields.Str()
    two_factor_enabled = fields.Bool()

product_schema = ProductSchema()
products_schema = ProductSchema(many=True)
operation_schema = OperationSchema()
operations_schema = OperationSchema(many=True)
user_schema = UserSchema()

# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ

def allowed_file(filename):
    """Проверяем допустимые расширения файлов"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def save_product_image(file, product_id):
    """Сохраняем изображение товара с оптимизацией"""
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file_ext = filename.rsplit('.', 1)[1].lower()
        new_filename = f"product_{product_id}_{secrets.token_hex(8)}.{file_ext}"
        
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], new_filename)
        
        try:
            image = Image.open(file.stream)
            if image.mode in ('RGBA', 'LA', 'P'):
                image = image.convert('RGB')
            max_size = (800, 800)
            image.thumbnail(max_size, Image.Resampling.LANCZOS)
            image.save(filepath, 'JPEG', quality=85, optimize=True)
            return new_filename
        except Exception as e:
            app.logger.error(f"Error processing image: {str(e)}")
            return None
    return None

def delete_product_image(filename):
    """Удаляем изображение товара"""
    if filename:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                return True
        except Exception as e:
            app.logger.error(f"Error deleting image: {str(e)}")
    return False

def get_product_image_url(product):
    """Получаем URL изображения товара"""
    if product.image_filename:
        return url_for('static', filename=f'uploads/products/{product.image_filename}')
    else:
        return url_for('static', filename='images/product-placeholder.jpg')

def log_audit(action, details=None):
    """Функция логирования"""
    audit = AuditLog(
        user_id=current_user.id if current_user.is_authenticated else None,
        action=action,
        details=details,
        ip_address=request.remote_addr
    )
    db.session.add(audit)
    db.session.commit()
    
    # Также отправляем в Redis для real-time мониторинга
    try:
        redis_client.lpush('audit_log', json.dumps({
            'user': current_user.username if current_user.is_authenticated else 'guest',
            'action': action,
            'time': datetime.utcnow().isoformat()
        }))
        redis_client.ltrim('audit_log', 0, 999)
    except:
        pass

def generate_sku():
    return f"SKU{datetime.now().strftime('%Y%m%d%H%M%S')}{secrets.randbelow(1000):03d}"

def generate_document_number(prefix):
    today = datetime.now().strftime('%Y%m%d')
    return f"{prefix}-{today}-{secrets.randbelow(10000):04d}"

def send_email(subject, recipients, text_body, html_body=None):
    """Отправка email уведомлений"""
    try:
        msg = Message(
            subject=subject,
            recipients=recipients if isinstance(recipients, list) else [recipients],
            body=text_body,
            html=html_body
        )
        mail.send(msg)
        app.logger.info(f"Email sent to {recipients}")
        return True
    except Exception as e:
        app.logger.error(f"Failed to send email: {str(e)}")
        return False

def send_low_stock_notification(product):
    """Отправка уведомления о низком запасе"""
    admins = User.query.filter_by(role=UserRole.ADMIN, is_active=True).all()
    storekeepers = User.query.filter_by(role=UserRole.STOREKEEPER, is_active=True).all()
    recipients = [admin.email for admin in admins] + [sk.email for sk in storekeepers]
    
    subject = f"⚠️ Низкий запас товара: {product.name}"
    text_body = f"""
    Товар: {product.name}
    SKU: {product.sku}
    Текущий остаток: {product.current_stock} {product.unit}
    Минимальный запас: {product.min_stock} {product.unit}
    
    Требуется пополнение запасов.
    """
    
    html_body = f"""
    <h3>⚠️ Низкий запас товара: {product.name}</h3>
    <p><strong>Товар:</strong> {product.name}</p>
    <p><strong>SKU:</strong> {product.sku}</p>
    <p><strong>Текущий остаток:</strong> {product.current_stock} {product.unit}</p>
    <p><strong>Минимальный запас:</strong> {product.min_stock} {product.unit}</p>
    <p><em>Требуется пополнение запасов.</em></p>
    """
    
    return send_email(subject, recipients, text_body, html_body)

# ДЕКОРАТОРЫ ДОСТУПА
def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin():
            flash('Доступ запрещен. Требуются права администратора.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def storekeeper_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not (current_user.is_storekeeper() or current_user.is_admin()):
            flash('Доступ запрещен. Требуются права кладовщика.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def employee_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.is_external():
            flash('Доступ запрещен. Требуются права сотрудника.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# КЛАССЫ ДЛЯ РАСШИРЕННЫХ ФУНКЦИЙ

class NotificationManager:
    """Менеджер уведомлений"""
    
    @staticmethod
    def send_notification(user_id, title, message, type='system', product_id=None, order_id=None):
        """Отправка уведомления конкретному пользователю"""
        notification = Notification(
            user_id=user_id,
            title=title,
            message=message,
            type=type,
            product_id=product_id,
            order_id=order_id
        )
        db.session.add(notification)
        db.session.commit()
        
        # Отправляем через WebSocket
        socketio.emit('notification', {
            'id': notification.id,
            'title': title,
            'message': message,
            'type': type,
            'created_at': datetime.utcnow().isoformat()
        }, room=f'user_{user_id}')
        
        return notification
    
    @staticmethod
    def send_low_stock_alert(product):
        """Отправка уведомления о низком запасе"""
        message = f"Товар '{product.name}' заканчивается. Осталось: {product.current_stock} {product.unit}"
        
        # Отправляем всем кладовщикам и админам
        storekeepers = User.query.filter_by(role=UserRole.STOREKEEPER, is_active=True).all()
        admins = User.query.filter_by(role=UserRole.ADMIN, is_active=True).all()
        
        for user in storekeepers + admins:
            NotificationManager.send_notification(
                user_id=user.id,
                title="⚠️ Низкий запас",
                message=message,
                type='low_stock',
                product_id=product.id
            )
    
    @staticmethod
    def send_new_order_alert(order):
        """Уведомление о новом заказе"""
        message = f"Новый заказ #{order.order_number} от {order.customer_name}"
        
        storekeepers = User.query.filter_by(role=UserRole.STOREKEEPER, is_active=True).all()
        
        for user in storekeepers:
            NotificationManager.send_notification(
                user_id=user.id,
                title="📦 Новый заказ",
                message=message,
                type='new_order',
                order_id=order.id
            )
    
    @staticmethod
    def send_return_request_alert(return_request):
        """Уведомление о запросе на возврат"""
        message = f"Запрос на возврат товара #{return_request.product_id}"
        
        admins = User.query.filter_by(role=UserRole.ADMIN, is_active=True).all()
        
        for user in admins:
            NotificationManager.send_notification(
                user_id=user.id,
                title="↩️ Запрос на возврат",
                message=message,
                type='return_request',
                return_id=return_request.id
            )

class DemandForecaster:
    """Класс для прогнозирования спроса"""
    
    def predict_demand(self, product_id, days_ahead=30):
        """Прогнозирование спроса (упрощенная версия)"""
        # В реальном приложении здесь будет ML модель
        # Пока возвращаем случайные значения для демонстрации
        import random
        predictions = []
        base = random.randint(5, 20)
        for i in range(days_ahead):
            # Добавляем сезонность и случайность
            seasonal = 1 + 0.3 * (i % 7 == 5 or i % 7 == 6)  # больше в выходные
            prediction = int(base * seasonal * (0.8 + 0.4 * random.random()))
            predictions.append(prediction)
            
            # Сохраняем прогноз
            forecast = DemandForecast(
                product_id=product_id,
                forecast_date=datetime.utcnow() + timedelta(days=i+1),
                predicted_demand=prediction,
                confidence_level=0.85,
                factors_considered=json.dumps({
                    'seasonal': True,
                    'trend': 'stable'
                })
            )
            db.session.add(forecast)
        
        db.session.commit()
        return predictions
    
    def generate_purchase_suggestions(self):
        """Генерация предложений по закупкам"""
        products = Product.query.filter_by(is_active=True).all()
        suggestions = []
        
        for product in products:
            # Получаем последние прогнозы
            current_date = datetime.utcnow().date()
            forecasts = DemandForecast.query.filter(
                DemandForecast.product_id == product.id,
                DemandForecast.forecast_date >= current_date
            ).all()
            
            if forecasts:
                total_demand = sum(f.predicted_demand for f in forecasts)
                
                if total_demand > product.current_stock:
                    suggested_quantity = total_demand - product.current_stock
                    
                    # Определяем срочность
                    if product.current_stock <= product.min_stock:
                        urgency = 'critical'
                    elif product.current_stock <= product.min_stock * 2:
                        urgency = 'high'
                    elif product.current_stock <= product.min_stock * 3:
                        urgency = 'medium'
                    else:
                        urgency = 'low'
                    
                    # Сохраняем предложение в базу
                    suggestion = PurchaseOrderSuggestion(
                        product_id=product.id,
                        suggested_quantity=suggested_quantity,
                        urgency=urgency,
                        based_on=json.dumps({
                            'forecast_demand': total_demand,
                            'current_stock': product.current_stock,
                            'min_stock': product.min_stock
                        })
                    )
                    db.session.add(suggestion)
                    suggestions.append(suggestion)
        
        db.session.commit()
        return suggestions

class BarcodeGenerator:
    """Генерация штрих-кодов и QR-кодов"""
    
    def generate_product_barcode(self, product):
        """Генерация штрих-кода для товара"""
        try:
            code = barcode.get('code128', product.sku or str(product.id), writer=ImageWriter())
            filename = f"{app.config['BARCODE_FOLDER']}/{product.sku or product.id}"
            code.save(filename)
            return f"{filename}.png"
        except Exception as e:
            app.logger.error(f"Barcode generation error: {str(e)}")
            return None
    
    def generate_product_qr(self, product):
        """Генерация QR-кода для товара"""
        try:
            data = {
                'id': product.id,
                'sku': product.sku,
                'name': product.name,
                'price': product.price,
                'stock': product.current_stock,
                'location': product.location
            }
            
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(json.dumps(data, ensure_ascii=False))
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            filename = f"{app.config['QRCODE_FOLDER']}/product_{product.id}.png"
            img.save(filename)
            
            return filename
        except Exception as e:
            app.logger.error(f"QR generation error: {str(e)}")
            return None

class TwoFactorAuthManager:
    """Управление двухфакторной аутентификацией"""
    
    @staticmethod
    def setup_2fa(user_id):
        """Настройка 2FA"""
        secret = pyotp.random_base32()
        
        two_factor = TwoFactorAuth.query.filter_by(user_id=user_id).first()
        if not two_factor:
            two_factor = TwoFactorAuth(
                user_id=user_id,
                secret_key=secret,
                backup_codes=json.dumps(TwoFactorAuthManager.generate_backup_codes())
            )
            db.session.add(two_factor)
        else:
            two_factor.secret_key = secret
            two_factor.backup_codes = json.dumps(TwoFactorAuthManager.generate_backup_codes())
            two_factor.is_enabled = False
        
        db.session.commit()
        return secret
    
    @staticmethod
    def generate_backup_codes(count=10):
        """Генерация резервных кодов"""
        codes = []
        for _ in range(count):
            code = ''.join(secrets.choice('0123456789') for _ in range(8))
            codes.append(code)
        return codes
    
    @staticmethod
    def get_qr_code(secret, username):
        """Получение QR-кода для настройки"""
        totp = pyotp.TOTP(secret)
        uri = totp.provisioning_uri(
            name=username,
            issuer_name="Warehouse System"
        )
        
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(uri)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        import base64
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        
        img_base64 = base64.b64encode(buffer.getvalue()).decode()
        
        return f"data:image/png;base64,{img_base64}"
    
    @staticmethod
    def verify_code(user_id, code):
        """Проверка кода 2FA"""
        two_factor = TwoFactorAuth.query.filter_by(user_id=user_id).first()
        
        if not two_factor or not two_factor.is_enabled:
            return False
        
        totp = pyotp.TOTP(two_factor.secret_key)
        if totp.verify(code):
            two_factor.failed_attempts = 0
            two_factor.last_used = datetime.utcnow()
            db.session.commit()
            return True
        
        backup_codes = json.loads(two_factor.backup_codes)
        if code in backup_codes:
            backup_codes.remove(code)
            two_factor.backup_codes = json.dumps(backup_codes)
            two_factor.failed_attempts = 0
            two_factor.last_used = datetime.utcnow()
            db.session.commit()
            return True
        
        two_factor.failed_attempts += 1
        db.session.commit()
        
        return False

def login_with_2fa(email, password, code=None):
    """Вход с поддержкой 2FA"""
    user = User.query.filter_by(email=email).first()
    
    if not user or not user.check_password(password):
        return {'success': False, 'message': 'Неверный email или пароль'}
    
    if not user.is_active:
        return {'success': False, 'message': 'Аккаунт деактивирован'}
    
    two_factor = TwoFactorAuth.query.filter_by(user_id=user.id).first()
    
    if two_factor and two_factor.is_enabled:
        if not code:
            return {
                'success': False,
                'requires_2fa': True,
                'user_id': user.id,
                'message': 'Требуется код двухфакторной аутентификации'
            }
        
        if not TwoFactorAuthManager.verify_code(user.id, code):
            return {'success': False, 'message': 'Неверный код 2FA'}
    
    return {'success': True, 'user': user}

class DocumentGenerator:
    """Генерация документов"""
    
    def generate_invoice(self, order_id):
        """Генерация счета"""
        order = Order.query.get(order_id)
        if not order:
            return None
        
        filename = f"invoice_{order.order_number}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        filepath = os.path.join(app.config['DOCUMENTS_FOLDER'], filename)
        
        doc = SimpleDocTemplate(filepath, pagesize=A4)
        elements = []
        
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=16, alignment=1)
        
        # Заголовок
        elements.append(Paragraph(f"СЧЕТ № {order.order_number}", title_style))
        elements.append(Spacer(1, 20))
        
        # Информация о клиенте
        elements.append(Paragraph(f"Клиент: {order.customer_name}", styles["Normal"]))
        elements.append(Paragraph(f"Email: {order.customer_email}", styles["Normal"]))
        elements.append(Paragraph(f"Телефон: {order.customer_phone}", styles["Normal"]))
        elements.append(Spacer(1, 20))
        
        # Таблица товаров
        data = [['№', 'Товар', 'Кол-во', 'Цена', 'Сумма']]
        for i, item in enumerate(order.order_items, 1):
            data.append([
                str(i),
                item.product.name,
                f"{item.quantity} {item.product.unit}",
                f"{item.unit_price} ₽",
                f"{item.quantity * item.unit_price} ₽"
            ])
        
        table = Table(data)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        
        elements.append(table)
        elements.append(Spacer(1, 20))
        
        # Итого
        elements.append(Paragraph(f"ИТОГО: {order.total_amount} ₽", styles["Heading2"]))
        elements.append(Spacer(1, 20))
        elements.append(Paragraph(f"Дата: {datetime.now().strftime('%d.%m.%Y')}", styles["Normal"]))
        
        doc.build(elements)
        
        # Сохраняем в базу
        document = Document(
            type='invoice',
            number=order.order_number,
            file_path=filename,
            order_id=order.id,
            user_id=order.user_id,
            total_amount=order.total_amount
        )
        db.session.add(document)
        db.session.commit()
        
        return filepath
    
    def generate_waybill(self, order_id):
        """Генерация накладной"""
        # Аналогично generate_invoice
        pass
    
    def generate_receipt(self, order_id):
        """Генерация чека"""
        # Аналогично generate_invoice
        pass

# ФОНОВЫЕ ЗАДАЧИ CELERY

@celery.task
def sync_marketplaces_task():
    """Фоновая синхронизация с маркетплейсами"""
    integrations = MarketplaceIntegration.query.all()
    for integration in integrations:
        try:
            # Здесь будет логика синхронизации
            integration.last_sync = datetime.utcnow()
            integration.sync_status = 'success'
        except Exception as e:
            integration.sync_status = 'error'
            integration.error_message = str(e)
        db.session.commit()
    
    app.logger.info("Marketplace sync completed")
    return len(integrations)

@celery.task
def check_low_stock_task():
    """Проверка низкого запаса"""
    low_stock_products = Product.query.filter(
        Product.current_stock <= Product.min_stock,
        Product.is_active == True
    ).all()
    
    for product in low_stock_products:
        NotificationManager.send_low_stock_alert(product)
        
        if product.current_stock <= 3:
            send_low_stock_notification(product)
    
    return len(low_stock_products)

@celery.task
def generate_forecasts_task():
    """Генерация прогнозов"""
    forecaster = DemandForecaster()
    products = Product.query.filter_by(is_active=True).limit(10).all()  # Ограничиваем для демо
    
    for product in products:
        forecaster.predict_demand(product.id)
    
    app.logger.info("Forecasts generated")
    return len(products)

@celery.task
def generate_purchase_suggestions_task():
    """Генерация предложений по закупкам"""
    forecaster = DemandForecaster()
    suggestions = forecaster.generate_purchase_suggestions()
    return len(suggestions)

# WebSocket ОБРАБОТЧИКИ

@socketio.on('connect')
def handle_connect():
    if current_user.is_authenticated:
        join_room(f'user_{current_user.id}')
        if current_user.is_admin():
            join_room('admins')
        if current_user.is_storekeeper():
            join_room('storekeepers')
        
        # Отправляем непрочитанные уведомления
        notifications = Notification.query.filter_by(
            user_id=current_user.id,
            is_read=False
        ).all()
        
        for notif in notifications:
            emit('notification', {
                'id': notif.id,
                'title': notif.title,
                'message': notif.message,
                'type': notif.type,
                'created_at': notif.created_at.isoformat()
            })

@socketio.on('disconnect')
def handle_disconnect():
    if current_user.is_authenticated:
        leave_room(f'user_{current_user.id}')

@socketio.on('mark_notification_read')
def handle_mark_read(data):
    notification_id = data.get('notification_id')
    notification = Notification.query.get(notification_id)
    if notification and notification.user_id == current_user.id:
        notification.is_read = True
        db.session.commit()
        emit('notification_read', {'id': notification_id})

# КОНТЕКСТНЫЙ ПРОЦЕССОР
@app.context_processor
def inject_global_vars():
    # Безопасно получаем количество непрочитанных уведомлений
    unread_count = 0
    if current_user.is_authenticated:
        try:
            unread_count = Notification.query.filter_by(
                user_id=current_user.id,
                is_read=False
            ).count()
        except:
            unread_count = 0
    
    return {
        'now': datetime.now(),
        'current_year': datetime.now().year,
        'is_guest': not current_user.is_authenticated,
        'unread_notifications': unread_count
    }

# ФИЛЬТРЫ
@app.template_filter('low_stock_count')
def low_stock_count_filter(products):
    return sum(1 for product in products 
               if product.is_active and product.current_stock <= product.min_stock)

@app.template_filter('out_of_stock_count')
def out_of_stock_count_filter(products):
    return sum(1 for product in products 
               if product.is_active and product.current_stock == 0)

# ГЛАВНАЯ СТРАНИЦА
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('public_catalog'))

# ПУБЛИЧНЫЕ МАРШРУТЫ
@app.route('/catalog')
def public_catalog():
    """Публичный каталог товаров"""
    search = request.args.get('search', '')
    category_id = request.args.get('category_id', type=int)
    
    query = Product.query.filter_by(is_active=True)
    
    if search:
        query = query.filter(
            db.or_(
                Product.name.ilike(f'%{search}%'),
                Product.description.ilike(f'%{search}%')
            )
        )
    
    if category_id:
        query = query.filter_by(category_id=category_id)
    
    products = query.all()
    categories = Category.query.all()
    
    return render_template('public_catalog.html', 
                         products=products,
                         categories=categories,
                         search=search,
                         category_id=category_id)

@app.route('/product/<int:product_id>')
def public_product_detail(product_id):
    """Публичная страница товара"""
    product = Product.query.get_or_404(product_id)
    if not product.is_active:
        flash('Товар недоступен', 'error')
        return redirect(url_for('public_catalog'))
    
    return render_template('public_product_detail.html', product=product)

@app.route('/guest/order', methods=['GET', 'POST'])
def guest_order():
    """Создание заказа без регистрации"""
    if request.method == 'POST':
        customer_name = request.form.get('customer_name')
        customer_email = request.form.get('customer_email')
        customer_phone = request.form.get('customer_phone')
        notes = request.form.get('notes')
        
        if not customer_name or not customer_email:
            flash('Пожалуйста, заполните обязательные поля: ФИО и Email', 'error')
            return redirect(url_for('guest_order'))
        
        order = Order(
            order_number=generate_document_number('GUEST'),
            customer_name=customer_name,
            customer_email=customer_email,
            customer_phone=customer_phone,
            notes=notes,
            status=OrderStatus.PENDING
        )
        
        db.session.add(order)
        db.session.flush()
        
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        
        total_amount = 0
        has_products = False
        
        for i, product_id in enumerate(product_ids):
            quantity = int(quantities[i])
            if quantity > 0:
                product = Product.query.get(product_id)
                if product and product.is_active and product.current_stock >= quantity:
                    order_item = OrderItem(
                        order_id=order.id,
                        product_id=product_id,
                        quantity=quantity,
                        unit_price=product.price
                    )
                    total_amount += quantity * product.price
                    db.session.add(order_item)
                    has_products = True
        
        if not has_products:
            db.session.rollback()
            flash('Добавьте хотя бы один товар в заказ', 'error')
            return redirect(url_for('guest_order'))
        
        order.total_amount = total_amount
        db.session.commit()
        
        NotificationManager.send_new_order_alert(order)
        log_audit(f"Создан гостевой заказ: {order.order_number}")
        
        flash(f'Заказ #{order.order_number} успешно создан!', 'success')
        return redirect(url_for('guest_order_success', order_number=order.order_number))
    
    products = Product.query.filter_by(is_active=True).all()
    return render_template('guest_order.html', products=products)

@app.route('/guest/order/success/<order_number>')
def guest_order_success(order_number):
    order = Order.query.filter_by(order_number=order_number).first_or_404()
    return render_template('guest_order_success.html', order=order)

@app.route('/guest/cart')
def guest_cart():
    """Корзина гостя"""
    # Здесь будет логика корзины
    # Пока возвращаем заглушку
    return render_template('guest_cart.html')
# МАРШРУТЫ АУТЕНТИФИКАЦИИ

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        twofa_code = request.form.get('twofa_code')
        remember = bool(request.form.get('remember'))
        
        result = login_with_2fa(email, password, twofa_code)
        
        if result['success']:
            user = result['user']
            login_user(user, remember=remember)
            log_audit(f"Вход в систему пользователем {user.username}")
            
            NotificationManager.send_notification(
                user_id=user.id,
                title="Новый вход в систему",
                message=f"Вход выполнен с IP: {request.remote_addr}",
                type='security'
            )
            
            flash('Вы успешно вошли в систему!', 'success')
            return redirect(url_for('dashboard'))
        elif result.get('requires_2fa'):
            return render_template('login_2fa.html', user_id=result['user_id'], email=email)
        else:
            flash(result['message'], 'error')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if password != confirm_password:
            flash('Пароли не совпадают', 'error')
            return render_template('register.html')
        
        if User.query.filter_by(username=username).first():
            flash('Имя пользователя уже занято', 'error')
            return render_template('register.html')
        
        if User.query.filter_by(email=email).first():
            flash('Email уже зарегистрирован', 'error')
            return render_template('register.html')
        
        user_count = User.query.count()
        role = UserRole.ADMIN if user_count == 0 else UserRole.EXTERNAL
        
        user = User(username=username, email=email, role=role)
        user.set_password(password)
        
        db.session.add(user)
        db.session.commit()
        
        # Создаем профиль клиента
        customer = Customer(user_id=user.id)
        db.session.add(customer)
        db.session.commit()
        
        log_audit(f"Зарегистрирован новый пользователь: {username}")
        flash('Регистрация успешна!', 'success')
        return redirect(url_for('login'))
    
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    log_audit(f"Выход из системы пользователем {current_user.username}")
    logout_user()
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('public_catalog'))

# ДАШБОРДЫ

@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.is_admin():
        return admin_dashboard()
    elif current_user.is_storekeeper():
        return warehouse_dashboard()
    elif current_user.is_employee():
        return employee_dashboard()
    else:
        return public_dashboard()

def admin_dashboard():
    stats = {
        'users_count': User.query.count(),
        'products_count': Product.query.count(),
        'categories_count': Category.query.count(),
        'suppliers_count': Supplier.query.count(),
        'operations_today': Operation.query.filter(
            db.func.date(Operation.created_at) == db.func.current_date()
        ).count(),
        'low_stock_count': Product.query.filter(Product.current_stock <= Product.min_stock).count(),
        'pending_orders': Order.query.filter_by(status=OrderStatus.PENDING).count(),
        'pending_returns': ReturnRequest.query.filter_by(status='pending').count(),
        'active_integrations': MarketplaceIntegration.query.count()
    }
    
    low_stock_products = Product.query.filter(Product.current_stock <= Product.min_stock).limit(10).all()
    recent_activities = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(10).all()
    recent_returns = ReturnRequest.query.order_by(ReturnRequest.created_at.desc()).limit(5).all()
    
    return render_template('admin_dashboard.html', 
                         stats=stats, 
                         low_stock_products=low_stock_products,
                         recent_activities=recent_activities,
                         recent_returns=recent_returns)

def warehouse_dashboard():
    low_stock_products = Product.query.filter(Product.current_stock <= Product.min_stock).limit(10).all()
    recent_operations = Operation.query.order_by(Operation.created_at.desc()).limit(10).all()
    pending_orders = Order.query.filter_by(status=OrderStatus.PENDING).count()
    new_orders = Order.query.filter_by(status=OrderStatus.PENDING).order_by(
        Order.created_at.desc()
    ).limit(5).all()
    
    # Данные для расширенных функций
    zones = WarehouseZone.query.all()
    suggestions = PurchaseOrderSuggestion.query.filter_by(is_approved=False).count()
    
    return render_template('warehouse_dashboard.html',
                         low_stock_products=low_stock_products,
                         recent_operations=recent_operations,
                         pending_orders=pending_orders,
                         new_orders=new_orders,
                         zones=zones,
                         suggestions=suggestions)

def employee_dashboard():
    products = Product.query.filter_by(is_active=True).limit(10).all()
    my_orders = Order.query.filter_by(user_id=current_user.id).order_by(
        Order.created_at.desc()
    ).limit(5).all()
    my_returns = ReturnRequest.query.filter_by(user_id=current_user.id).order_by(
        ReturnRequest.created_at.desc()
    ).limit(5).all()
    
    return render_template('employee_dashboard.html',
                         products=products,
                         orders=my_orders,
                         returns=my_returns)

def public_dashboard():
    products = Product.query.filter_by(is_active=True).limit(12).all()
    my_orders = Order.query.filter_by(user_id=current_user.id).order_by(
        Order.created_at.desc()
    ).limit(5).all()
    
    # Информация о программе лояльности
    customer = Customer.query.filter_by(user_id=current_user.id).first()
    
    return render_template('public_dashboard.html',
                         products=products,
                         orders=my_orders,
                         customer=customer)

# УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ

@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    users = User.query.all()
    return render_template('admin_users.html', users=users)

@app.route('/admin/users/update_role', methods=['POST'])
@login_required
@admin_required
def update_user_role():
    user_id = request.form.get('user_id')
    new_role = request.form.get('role')
    
    user = User.query.get_or_404(user_id)
    old_role = user.role.value
    
    if new_role in [role.value for role in UserRole]:
        user.role = UserRole(new_role)
        db.session.commit()
        
        log_audit(f"Изменена роль пользователя {user.username} с {old_role} на {new_role}")
        flash(f'Роль пользователя {user.username} изменена на {new_role}', 'success')
    
    return redirect(url_for('admin_users'))

# УПРАВЛЕНИЕ ТОВАРАМИ

@app.route('/products')
@login_required
def products():
    search = request.args.get('search', '')
    category_id = request.args.get('category_id', type=int)
    supplier_id = request.args.get('supplier_id', type=int)
    low_stock = request.args.get('low_stock', type=bool)
    
    query = Product.query.filter_by(is_active=True)
    
    if search:
        query = query.filter(
            db.or_(
                Product.name.ilike(f'%{search}%'),
                Product.sku.ilike(f'%{search}%'),
                Product.barcode.ilike(f'%{search}%')
            )
        )
    
    if category_id:
        query = query.filter_by(category_id=category_id)
    
    if supplier_id:
        query = query.filter_by(supplier_id=supplier_id)
    
    if low_stock:
        query = query.filter(Product.current_stock <= Product.min_stock)
    
    products = query.all()
    categories = Category.query.all()
    suppliers = Supplier.query.all()
    
    # Данные для расширенных функций
    zones = WarehouseZone.query.all()
    currencies = Currency.query.all()
    
    template = 'products.html'
    if current_user.is_admin():
        template = 'admin_products.html'
    elif current_user.is_storekeeper():
        template = 'warehouse_products.html'
    
    return render_template(template,
                         products=products,
                         categories=categories,
                         suppliers=suppliers,
                         zones=zones,
                         currencies=currencies,
                         search=search,
                         category_id=category_id,
                         supplier_id=supplier_id,
                         low_stock=low_stock)

@app.route('/products/create', methods=['POST'])
@login_required
@admin_required
def create_product():
    name = request.form.get('name')
    description = request.form.get('description')
    category_id = request.form.get('category_id')
    supplier_id = request.form.get('supplier_id')
    unit = request.form.get('unit')
    price = float(request.form.get('price', 0))
    cost_price = float(request.form.get('cost_price', 0))
    min_stock = int(request.form.get('min_stock', 0))
    max_stock = int(request.form.get('max_stock', 1000))
    barcode = request.form.get('barcode')
    location = request.form.get('location')
    current_stock = int(request.form.get('current_stock', 0))
    
    # Новые поля
    weight = request.form.get('weight', type=float)
    volume = request.form.get('volume', type=float)
    tax_rate_id = request.form.get('tax_rate_id', type=int)
    currency_code = request.form.get('currency_code')
    warehouse_zone_id = request.form.get('warehouse_zone_id', type=int)
    batch_number = request.form.get('batch_number')
    expiry_date_str = request.form.get('expiry_date')
    expiry_date = datetime.strptime(expiry_date_str, '%Y-%m-%d') if expiry_date_str else None
    
    product = Product(
        name=name,
        description=description,
        sku=generate_sku(),
        barcode=barcode,
        category_id=category_id,
        supplier_id=supplier_id,
        unit=unit,
        price=price,
        cost_price=cost_price,
        min_stock=min_stock,
        max_stock=max_stock,
        location=location,
        current_stock=current_stock,
        weight=weight,
        volume=volume,
        tax_rate_id=tax_rate_id,
        currency_code=currency_code,
        warehouse_zone_id=warehouse_zone_id,
        batch_number=batch_number,
        expiry_date=expiry_date
    )
    
    db.session.add(product)
    db.session.flush()
    
    if 'image' in request.files:
        image_file = request.files['image']
        if image_file and image_file.filename:
            filename = save_product_image(image_file, product.id)
            if filename:
                product.image_filename = filename
    
    # Генерируем штрих-код и QR-код
    barcode_gen = BarcodeGenerator()
    barcode_path = barcode_gen.generate_product_barcode(product)
    if barcode_path:
        product.barcode_image = barcode_path
    
    qr_path = barcode_gen.generate_product_qr(product)
    if qr_path:
        product.qr_code = qr_path
    
    db.session.commit()
    
    log_audit(f"Создан товар: {name}")
    flash('Товар создан успешно', 'success')
    return redirect(url_for('products'))

@app.route('/products/<int:product_id>/edit')
@login_required
@admin_required
def edit_product_form(product_id):
    product = Product.query.get_or_404(product_id)
    categories = Category.query.all()
    suppliers = Supplier.query.all()
    zones = WarehouseZone.query.all()
    currencies = Currency.query.all()
    tax_rates = TaxRate.query.all()
    return render_template('edit_product.html', 
                         product=product, 
                         categories=categories, 
                         suppliers=suppliers,
                         zones=zones,
                         currencies=currencies,
                         tax_rates=tax_rates)

@app.route('/products/<int:product_id>/update', methods=['POST'])
@login_required
@admin_required
def update_product(product_id):
    product = Product.query.get_or_404(product_id)
    
    product.name = request.form.get('name')
    product.description = request.form.get('description')
    product.category_id = request.form.get('category_id') or None
    product.supplier_id = request.form.get('supplier_id') or None
    product.unit = request.form.get('unit')
    product.price = float(request.form.get('price', 0))
    product.cost_price = float(request.form.get('cost_price', 0))
    product.min_stock = int(request.form.get('min_stock', 0))
    product.max_stock = int(request.form.get('max_stock', 1000))
    product.barcode = request.form.get('barcode')
    product.location = request.form.get('location')
    product.is_active = bool(request.form.get('is_active'))
    
    # Новые поля
    product.weight = request.form.get('weight', type=float)
    product.volume = request.form.get('volume', type=float)
    product.tax_rate_id = request.form.get('tax_rate_id', type=int)
    product.currency_code = request.form.get('currency_code')
    product.warehouse_zone_id = request.form.get('warehouse_zone_id', type=int)
    product.batch_number = request.form.get('batch_number')
    expiry_date_str = request.form.get('expiry_date')
    product.expiry_date = datetime.strptime(expiry_date_str, '%Y-%m-%d') if expiry_date_str else None
    
    if 'image' in request.files:
        image_file = request.files['image']
        if image_file and image_file.filename:
            if product.image_filename:
                delete_product_image(product.image_filename)
            filename = save_product_image(image_file, product.id)
            if filename:
                product.image_filename = filename
    
    if 'delete_image' in request.form:
        if product.image_filename:
            delete_product_image(product.image_filename)
            product.image_filename = None
    
    db.session.commit()
    
    log_audit(f"Обновлен товар: {product.name}")
    flash('Товар успешно обновлен', 'success')
    return redirect(url_for('products'))

@app.route('/products/<int:product_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_product(product_id):
    product = Product.query.get_or_404(product_id)
    product_name = product.name
    product.is_active = False
    db.session.commit()
    
    log_audit(f"Деактивирован товар: {product_name}")
    flash(f'Товар "{product_name}" деактивирован', 'success')
    return redirect(url_for('products'))

@app.route('/products/<int:product_id>/activate', methods=['POST'])
@login_required
@admin_required
def activate_product(product_id):
    product = Product.query.get_or_404(product_id)
    product.is_active = True
    db.session.commit()
    
    log_audit(f"Активирован товар: {product.name}")
    flash(f'Товар "{product.name}" активирован', 'success')
    return redirect(url_for('products'))

# ЗАКАЗЫ

@app.route('/orders')
@login_required
def orders():
    if current_user.is_admin() or current_user.is_storekeeper():
        orders_list = Order.query.order_by(Order.created_at.desc()).all()
    else:
        orders_list = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    
    return render_template('orders.html', orders=orders_list)

@app.route('/orders/create', methods=['GET', 'POST'])
@login_required
def create_order():
    if request.method == 'POST':
        customer_name = request.form.get('customer_name')
        customer_email = request.form.get('customer_email')
        customer_phone = request.form.get('customer_phone')
        notes = request.form.get('notes')
        
        order = Order(
            order_number=generate_document_number('ORDER'),
            customer_name=customer_name,
            customer_email=customer_email,
            customer_phone=customer_phone,
            notes=notes,
            user_id=current_user.id,
            status=OrderStatus.PENDING
        )
        
        db.session.add(order)
        db.session.flush()
        
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        
        total_amount = 0
        for i, product_id in enumerate(product_ids):
            quantity = int(quantities[i])
            if quantity > 0:
                product = Product.query.get(product_id)
                order_item = OrderItem(
                    order_id=order.id,
                    product_id=product_id,
                    quantity=quantity,
                    unit_price=product.price
                )
                total_amount += quantity * product.price
                db.session.add(order_item)
        
        order.total_amount = total_amount
        db.session.commit()
        
        NotificationManager.send_new_order_alert(order)
        log_audit(f"Создан заказ: {order.order_number}")
        flash('Заказ создан успешно', 'success')
        return redirect(url_for('orders'))
    
    products = Product.query.filter_by(is_active=True).all()
    return render_template('create_order.html', products=products)

@app.route('/orders/<int:order_id>/approve', methods=['POST'])
@login_required
@storekeeper_required
def approve_order(order_id):
    order = Order.query.get_or_404(order_id)
    order.status = OrderStatus.APPROVED
    db.session.commit()
    
    log_audit(f"Заказ подтвержден: {order.order_number}")
    flash('Заказ подтвержден', 'success')
    return redirect(url_for('orders'))

@app.route('/orders/<int:order_id>/complete', methods=['POST'])
@login_required
@storekeeper_required
def complete_order(order_id):
    order = Order.query.get_or_404(order_id)
    
    for item in order.order_items:
        product = item.product
        if product.current_stock < item.quantity:
            flash(f'Недостаточно товара: {product.name}', 'error')
            return redirect(url_for('orders'))
        
        operation = Operation(
            type=OperationType.SHIPMENT,
            product_id=product.id,
            quantity=-item.quantity,
            user_id=current_user.id,
            document_number=f"ORDER-{order.order_number}",
            notes=f"Отгрузка по заказу {order.order_number}",
            previous_stock=product.current_stock,
            new_stock=product.current_stock - item.quantity
        )
        
        product.current_stock -= item.quantity
        db.session.add(operation)
    
    order.status = OrderStatus.COMPLETED
    order.completed_at = datetime.utcnow()
    db.session.commit()
    
    log_audit(f"Заказ выполнен: {order.order_number}")
    flash('Заказ выполнен и товары отгружены', 'success')
    return redirect(url_for('orders'))

@app.route('/orders/<int:order_id>/cancel', methods=['POST'])
@login_required
def cancel_order(order_id):
    order = Order.query.get_or_404(order_id)
    
    if not (current_user.is_admin() or current_user.is_storekeeper() or order.user_id == current_user.id):
        flash('Недостаточно прав для отмены заказа', 'error')
        return redirect(url_for('orders'))
    
    order.status = OrderStatus.CANCELLED
    db.session.commit()
    
    log_audit(f"Заказ отменен: {order.order_number}")
    flash('Заказ отменен', 'success')
    return redirect(url_for('orders'))

# ВОЗВРАТЫ

@app.route('/returns')
@login_required
def returns_list():
    """Список возвратов"""
    if current_user.is_admin():
        returns = ReturnRequest.query.order_by(ReturnRequest.created_at.desc()).all()
    else:
        returns = ReturnRequest.query.filter_by(user_id=current_user.id).order_by(ReturnRequest.created_at.desc()).all()
    return render_template('returns.html', returns=returns)

@app.route('/returns/create', methods=['GET', 'POST'])
@login_required
def create_return():
    """Создание запроса на возврат"""
    if request.method == 'POST':
        return_request = ReturnRequest(
            order_id=request.form.get('order_id'),
            product_id=request.form.get('product_id'),
            reason=request.form.get('reason'),
            condition=request.form.get('condition'),
            user_id=current_user.id
        )
        db.session.add(return_request)
        db.session.commit()
        
        NotificationManager.send_return_request_alert(return_request)
        log_audit(f"Создан запрос на возврат #{return_request.id}")
        
        flash('Запрос на возврат создан', 'success')
        return redirect(url_for('returns_list'))
    
    orders = Order.query.filter_by(user_id=current_user.id).all()
    products = Product.query.all()
    return render_template('create_return.html', orders=orders, products=products)

@app.route('/returns/<int:return_id>/process', methods=['POST'])
@login_required
@admin_required
def process_return(return_id):
    """Обработка возврата"""
    return_request = ReturnRequest.query.get_or_404(return_id)
    status = request.form.get('status')
    refund_amount = request.form.get('refund_amount', type=float)
    
    return_request.status = status
    return_request.processed_at = datetime.utcnow()
    return_request.processed_by = current_user.id
    if refund_amount:
        return_request.refund_amount = refund_amount
    
    db.session.commit()
    
    # Уведомляем пользователя
    NotificationManager.send_notification(
        user_id=return_request.user_id,
        title="Статус возврата обновлен",
        message=f"Ваш запрос на возврат #{return_request.id} {status}",
        type='return_request'
    )
    
    log_audit(f"Обработан возврат #{return_id}: {status}")
    flash('Возврат обработан', 'success')
    return redirect(url_for('returns_list'))

# УПРАВЛЕНИЕ СКЛАДСКИМИ ЗОНАМИ

@app.route('/warehouse/zones')
@login_required
@storekeeper_required
def warehouse_zones():
    """Визуализация склада по зонам"""
    zones = WarehouseZone.query.all()
    return render_template('warehouse_zones.html', zones=zones)

@app.route('/warehouse/cells/<int:zone_id>')
@login_required
@storekeeper_required
def warehouse_cells(zone_id):
    """Ячейки в зоне склада"""
    zone = WarehouseZone.query.get_or_404(zone_id)
    cells = StorageCell.query.filter_by(zone_id=zone_id).all()
    return render_template('warehouse_cells.html', zone=zone, cells=cells)

@app.route('/warehouse/assign_cell', methods=['POST'])
@login_required
@storekeeper_required
def assign_cell():
    """Назначение ячейки для товара"""
    product_id = request.form.get('product_id')
    cell_id = request.form.get('cell_id')
    quantity = request.form.get('quantity', type=int)
    
    product = Product.query.get(product_id)
    cell = StorageCell.query.get(cell_id)
    
    if product and cell and not cell.is_occupied:
        cell.current_product_id = product_id
        cell.current_quantity = quantity
        cell.is_occupied = True
        cell.last_updated = datetime.utcnow()
        
        product.storage_cell_id = cell_id
        product.warehouse_zone_id = cell.zone_id
        
        db.session.commit()
        
        log_audit(f"Товар {product.name} размещен в ячейке {cell.code}")
        flash(f'Товар размещен в ячейке {cell.code}', 'success')
    
    return redirect(url_for('warehouse_cells', zone_id=cell.zone_id if cell else None))

# ИНТЕГРАЦИЯ С МАРКЕТПЛЕЙСАМИ

@app.route('/marketplace/integrations')
@login_required
def marketplace_integrations():
    """Управление интеграциями с маркетплейсами"""
    if current_user.is_admin():
        integrations = MarketplaceIntegration.query.all()
    else:
        integrations = MarketplaceIntegration.query.filter_by(user_id=current_user.id).all()
    return render_template('marketplace_integrations.html', integrations=integrations)

@app.route('/marketplace/create', methods=['POST'])
@login_required
def create_marketplace_integration():
    """Создание интеграции с маркетплейсом"""
    integration = MarketplaceIntegration(
        name=request.form.get('name'),
        marketplace_type=request.form.get('marketplace_type'),
        api_key=request.form.get('api_key'),
        api_secret=request.form.get('api_secret'),
        seller_id=request.form.get('seller_id'),
        user_id=current_user.id,
        sync_products=bool(request.form.get('sync_products')),
        sync_orders=bool(request.form.get('sync_orders')),
        sync_stock=bool(request.form.get('sync_stock')),
        sync_prices=bool(request.form.get('sync_prices'))
    )
    db.session.add(integration)
    db.session.commit()
    
    log_audit(f"Создана интеграция с {integration.name}")
    flash('Интеграция создана', 'success')
    return redirect(url_for('marketplace_integrations'))

@app.route('/marketplace/sync/<int:integration_id>')
@login_required
def sync_marketplace(integration_id):
    """Запуск синхронизации с маркетплейсом"""
    integration = MarketplaceIntegration.query.get_or_404(integration_id)
    
    if integration.user_id != current_user.id and not current_user.is_admin():
        flash('Нет прав для выполнения синхронизации', 'error')
        return redirect(url_for('marketplace_integrations'))
    
    # Запускаем фоновую задачу
    sync_marketplaces_task.delay()
    
    flash('Синхронизация запущена в фоновом режиме', 'info')
    return redirect(url_for('marketplace_integrations'))

# ПРОГРАММА ЛОЯЛЬНОСТИ

@app.route('/loyalty')
@login_required
def loyalty():
    """Программа лояльности"""
    customer = Customer.query.filter_by(user_id=current_user.id).first()
    if not customer:
        customer = Customer(user_id=current_user.id)
        db.session.add(customer)
        db.session.commit()
    
    programs = LoyaltyProgram.query.filter_by(is_active=True).all()
    return render_template('loyalty.html', customer=customer, programs=programs)

@app.route('/loyalty/enroll/<int:program_id>')
@login_required
def enroll_loyalty(program_id):
    """Запись в программу лояльности"""
    customer = Customer.query.filter_by(user_id=current_user.id).first()
    program = LoyaltyProgram.query.get_or_404(program_id)
    
    if customer:
        customer.loyalty_program_id = program_id
        db.session.commit()
        flash(f'Вы записаны в программу {program.name}', 'success')
    
    return redirect(url_for('loyalty'))

# 2FA НАСТРОЙКИ

@app.route('/settings/2fa')
@login_required
def settings_2fa():
    """Настройки двухфакторной аутентификации"""
    two_factor = TwoFactorAuth.query.filter_by(user_id=current_user.id).first()
    return render_template('settings_2fa.html', 
                         two_factor_enabled=two_factor.is_enabled if two_factor else False)

@app.route('/api/auth/2fa/setup', methods=['POST'])
@login_required
def api_setup_2fa():
    """Настройка двухфакторной аутентификации"""
    secret = TwoFactorAuthManager.setup_2fa(current_user.id)
    qr_code = TwoFactorAuthManager.get_qr_code(secret, current_user.username)
    
    return jsonify({
        'success': True,
        'secret': secret,
        'qr_code': qr_code
    })

@app.route('/api/auth/2fa/verify', methods=['POST'])
@login_required
def api_verify_2fa():
    """Проверка и включение 2FA"""
    data = request.json
    code = data.get('code')
    
    if TwoFactorAuthManager.verify_code(current_user.id, code):
        two_factor = TwoFactorAuth.query.filter_by(user_id=current_user.id).first()
        if two_factor:
            two_factor.is_enabled = True
            db.session.commit()
            return jsonify({'success': True, 'message': '2FA успешно включена'})
    
    return jsonify({'success': False, 'message': 'Неверный код'}), 400

# ШТРИХ-КОДЫ И СКАНИРОВАНИЕ

@app.route('/barcode/scan')
@login_required
def scan_barcode():
    """Страница сканирования штрих-кодов"""
    return render_template('scan_barcode.html')

@app.route('/api/products/barcode/<string:barcode>')
@login_required
def get_product_by_barcode(barcode):
    """Поиск товара по штрих-коду"""
    product = Product.query.filter_by(barcode=barcode).first()
    if product:
        return jsonify({
            'id': product.id,
            'name': product.name,
            'sku': product.sku,
            'current_stock': product.current_stock,
            'price': product.price,
            'unit': product.unit,
            'location': product.location,
            'image_url': get_product_image_url(product)
        })
    return jsonify({'error': 'Product not found'}), 404

@app.route('/api/scan/history')
@login_required
def get_scan_history():
    """История сканирований"""
    # Получаем из Redis
    try:
        history = redis_client.lrange('scan_history', 0, 49)
        return jsonify([json.loads(h) for h in history])
    except:
        return jsonify([])

# ПРОГНОЗИРОВАНИЕ

@app.route('/analytics/forecast/<int:product_id>')
@login_required
@storekeeper_required
def product_forecast(product_id):
    """Страница с прогнозом для товара"""
    product = Product.query.get_or_404(product_id)
    
    # ИСПРАВЛЕНО: используем filter() вместо filter_by() для сравнения
    current_date = datetime.utcnow().date()
    forecasts = DemandForecast.query.filter(
        DemandForecast.product_id == product_id,
        DemandForecast.forecast_date >= current_date
    ).order_by(DemandForecast.forecast_date).all()
    
    return render_template('product_forecast.html', 
                         product=product, 
                         forecasts=forecasts)

@app.route('/analytics/purchase-suggestions')
@login_required
@storekeeper_required
def purchase_suggestions():
    """Предложения по закупкам"""
    suggestions = PurchaseOrderSuggestion.query.filter_by(is_approved=False).order_by(
        db.case(
            (PurchaseOrderSuggestion.urgency == 'critical', 1),
            (PurchaseOrderSuggestion.urgency == 'high', 2),
            (PurchaseOrderSuggestion.urgency == 'medium', 3),
            (PurchaseOrderSuggestion.urgency == 'low', 4)
        )
    ).all()
    return render_template('purchase_suggestions.html', suggestions=suggestions)

@app.route('/analytics/purchase-suggestions/<int:suggestion_id>/approve', methods=['POST'])
@login_required
@storekeeper_required
def approve_purchase_suggestion(suggestion_id):
    """Утверждение предложения по закупке"""
    suggestion = PurchaseOrderSuggestion.query.get_or_404(suggestion_id)
    suggestion.is_approved = True
    suggestion.approved_by = current_user.id
    suggestion.approved_at = datetime.utcnow()
    db.session.commit()
    
    log_audit(f"Утверждено предложение по закупке #{suggestion_id}")
    flash('Предложение утверждено', 'success')
    return redirect(url_for('purchase_suggestions'))

# ДОКУМЕНТЫ

@app.route('/documents')
@login_required
def documents():
    """Список документов"""
    if current_user.is_admin():
        docs = Document.query.order_by(Document.generated_at.desc()).all()
    else:
        docs = Document.query.filter_by(user_id=current_user.id).order_by(Document.generated_at.desc()).all()
    return render_template('documents.html', documents=docs)

@app.route('/documents/generate/<string:type>/<int:id>')
@login_required
def generate_document(type, id):
    """Генерация документа"""
    generator = DocumentGenerator()
    
    filepath = None
    if type == 'invoice':
        filepath = generator.generate_invoice(id)
    elif type == 'waybill':
        filepath = generator.generate_waybill(id)
    elif type == 'receipt':
        filepath = generator.generate_receipt(id)
    
    if filepath and os.path.exists(filepath):
        return send_file(filepath, as_attachment=True)
    
    flash('Ошибка генерации документа', 'error')
    return redirect(url_for('documents'))

# УВЕДОМЛЕНИЯ

@app.route('/notifications')
@login_required
def notifications():
    """Страница уведомлений"""
    notifications = Notification.query.filter_by(
        user_id=current_user.id
    ).order_by(Notification.created_at.desc()).all()
    return render_template('notifications.html', notifications=notifications)

@app.route('/api/notifications/mark_all_read', methods=['POST'])
@login_required
def mark_all_notifications_read():
    """Отметить все уведомления как прочитанные"""
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({'success': True})

# АНАЛИТИКА

@app.route('/analytics')
@login_required
@admin_required
def analytics():
    """Страница аналитики"""
    return render_template('analytics.html')

@app.route('/api/analytics/stock_movement')
@login_required
@admin_required
def api_stock_movement():
    """Анализ движения товаров"""
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=30)
    
    operations = Operation.query.filter(
        Operation.created_at.between(start_date, end_date)
    ).all()
    
    daily_data = {}
    for op in operations:
        date_str = op.created_at.strftime('%Y-%m-%d')
        if date_str not in daily_data:
            daily_data[date_str] = {'receipt': 0, 'shipment': 0, 'adjustment': 0}
        
        if op.type == OperationType.RECEIPT:
            daily_data[date_str]['receipt'] += op.quantity
        elif op.type == OperationType.SHIPMENT:
            daily_data[date_str]['shipment'] += abs(op.quantity)
        else:
            daily_data[date_str]['adjustment'] += op.quantity
    
    dates = sorted(daily_data.keys())
    receipt_data = [daily_data[date]['receipt'] for date in dates]
    shipment_data = [daily_data[date]['shipment'] for date in dates]
    
    return jsonify({
        'dates': dates,
        'receipts': receipt_data,
        'shipments': shipment_data
    })

@app.route('/api/analytics/product_popularity')
@login_required
@admin_required
def api_product_popularity():
    """Анализ популярности товаров"""
    products = Product.query.filter_by(is_active=True).all()
    
    product_data = []
    for product in products:
        shipment_count = Operation.query.filter(
            Operation.product_id == product.id,
            Operation.type == OperationType.SHIPMENT
        ).count()
        
        total_shipped = db.session.query(db.func.sum(Operation.quantity)).filter(
            Operation.product_id == product.id,
            Operation.type == OperationType.SHIPMENT
        ).scalar() or 0
        
        product_data.append({
            'name': product.name,
            'shipment_count': shipment_count,
            'total_shipped': abs(total_shipped),
            'current_stock': product.current_stock
        })
    
    product_data.sort(key=lambda x: x['shipment_count'], reverse=True)
    return jsonify(product_data[:10])

@app.route('/api/analytics/low_stock_alert')
@login_required
@admin_required
def api_low_stock_alert():
    """Товары с низким запасом"""
    critical_products = Product.query.filter(
        Product.current_stock <= Product.min_stock,
        Product.is_active == True
    ).all()
    
    alert_data = []
    for product in critical_products:
        alert_data.append({
            'name': product.name,
            'current_stock': product.current_stock,
            'min_stock': product.min_stock,
            'unit': product.unit,
            'urgency': 'critical' if product.current_stock == 0 else 'low'
        })
    
    return jsonify(alert_data)

@app.route('/api/analytics/returns_stats')
@login_required
@admin_required
def api_returns_stats():
    """Статистика по возвратам"""
    returns = ReturnRequest.query.all()
    
    stats = {
        'total': len(returns),
        'pending': sum(1 for r in returns if r.status == 'pending'),
        'approved': sum(1 for r in returns if r.status == 'approved'),
        'rejected': sum(1 for r in returns if r.status == 'rejected'),
        'completed': sum(1 for r in returns if r.status == 'completed'),
        'total_refund': sum(r.refund_amount or 0 for r in returns)
    }
    
    return jsonify(stats)

# ОТЧЕТЫ

@app.route('/reports/stock')
@login_required
def stock_report():
    """Excel отчет по остаткам"""
    products = Product.query.filter_by(is_active=True).all()
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df = pd.DataFrame([{
            'SKU': p.sku,
            'Наименование': p.name,
            'Категория': p.category.name if p.category else '',
            'Текущий остаток': p.current_stock,
            'Мин. запас': p.min_stock,
            'Единица': p.unit,
            'Цена': p.price,
            'Место хранения': p.location,
            'Вес': p.weight,
            'Объем': p.volume,
            'Партия': p.batch_number,
            'Срок годности': p.expiry_date.strftime('%d.%m.%Y') if p.expiry_date else ''
        } for p in products])
        df.to_excel(writer, sheet_name='Остатки товаров', index=False)
    
    output.seek(0)
    return send_file(output, 
                    download_name=f'stock_report_{datetime.now().strftime("%Y%m%d")}.xlsx',
                    as_attachment=True)

@app.route('/reports/pdf/stock')
@login_required
@admin_required
def pdf_stock_report():
    """PDF отчет по остаткам"""
    products = Product.query.filter_by(is_active=True).all()
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    elements = []
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=16, alignment=1)
    
    title = Paragraph("ОТЧЕТ ПО ОСТАТКАМ ТОВАРОВ", title_style)
    elements.append(title)
    elements.append(Spacer(1, 20))
    
    data = [['SKU', 'Наименование', 'Категория', 'Остаток', 'Мин. запас', 'Статус', 'Цена']]
    
    for product in products:
        if product.current_stock <= 0:
            status = "❌ Нет в наличии"
        elif product.current_stock <= product.min_stock:
            status = "⚠️ Низкий запас"
        else:
            status = "✅ В наличии"
        
        data.append([
            product.sku,
            product.name,
            product.category.name if product.category else '-',
            f"{product.current_stock} {product.unit}",
            f"{product.min_stock} {product.unit}",
            status,
            f"{product.price:.2f} ₽"
        ])
    
    table = Table(data, colWidths=[80, 150, 80, 60, 60, 80, 60])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    
    elements.append(table)
    elements.append(Spacer(1, 20))
    
    total_products = len(products)
    low_stock = len([p for p in products if p.current_stock <= p.min_stock and p.current_stock > 0])
    out_of_stock = len([p for p in products if p.current_stock == 0])
    
    stats_text = f"""
    <b>Статистика:</b><br/>
    Всего товаров: {total_products}<br/>
    Товаров с низким запасом: {low_stock}<br/>
    Товаров нет в наличии: {out_of_stock}<br/>
    Дата формирования: {datetime.now().strftime('%d.%m.%Y %H:%M')}
    """
    
    stats = Paragraph(stats_text, styles["Normal"])
    elements.append(stats)
    
    doc.build(elements)
    buffer.seek(0)
    
    return send_file(
        buffer,
        download_name=f'stock_report_{datetime.now().strftime("%Y%m%d_%H%M")}.pdf',
        as_attachment=True,
        mimetype='application/pdf'
    )

# API МАРШРУТЫ

@app.route('/api/v1/auth/login', methods=['POST'])
def api_login():
    """API аутентификация"""
    try:
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password) and user.is_active:
            access_token = create_access_token(
                identity=user.id,
                additional_claims={'role': user.role.value}
            )
            return jsonify({
                'access_token': access_token,
                'user': {
                    'id': user.id,
                    'username': user.username,
                    'email': user.email,
                    'role': user.role.value
                }
            }), 200
        else:
            return jsonify({'error': 'Invalid credentials'}), 401
    except Exception as e:
        app.logger.error(f"API login error: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/v1/products', methods=['GET'])
@jwt_required()
def api_products():
    """API получение товаров"""
    products = Product.query.filter_by(is_active=True).all()
    return jsonify(products_schema.dump(products)), 200

# API V2 МАРШРУТЫ

@app.route('/api/v2/returns', methods=['GET', 'POST'])
@jwt_required()
def api_v2_returns():
    """API v2 для работы с возвратами"""
    if request.method == 'GET':
        user_id = get_jwt_identity()
        returns = ReturnRequest.query.filter_by(user_id=user_id).all()
        return jsonify([{
            'id': r.id,
            'order_id': r.order_id,
            'product_id': r.product_id,
            'status': r.status,
            'created_at': r.created_at.isoformat()
        } for r in returns])
    
    elif request.method == 'POST':
        data = request.json
        return_request = ReturnRequest(
            order_id=data['order_id'],
            product_id=data['product_id'],
            reason=data['reason'],
            condition=data.get('condition', 'new'),
            user_id=get_jwt_identity()
        )
        db.session.add(return_request)
        db.session.commit()
        
        NotificationManager.send_return_request_alert(return_request)
        
        return jsonify({'success': True, 'id': return_request.id}), 201

@app.route('/api/v2/warehouse/zones', methods=['GET'])
@jwt_required()
def api_v2_warehouse_zones():
    """API v2 для получения зон склада"""
    zones = WarehouseZone.query.all()
    return jsonify([{
        'id': z.id,
        'name': z.name,
        'code': z.code,
        'type': z.type,
        'cells': [{
            'id': c.id,
            'code': c.code,
            'is_occupied': c.is_occupied,
            'current_product': c.current_product_id,
            'current_quantity': c.current_quantity
        } for c in z.cells]
    } for z in zones])

@app.route('/api/v2/forecast/<int:product_id>', methods=['GET'])
@jwt_required()
def api_v2_forecast(product_id):
    """API v2 для получения прогноза"""
    days = request.args.get('days', 30, type=int)
    forecaster = DemandForecaster()
    predictions = forecaster.predict_demand(product_id, days)
    
    if predictions:
        return jsonify({
            'product_id': product_id,
            'predictions': predictions,
            'dates': [(datetime.utcnow() + timedelta(days=i)).date().isoformat() 
                     for i in range(days)]
        })
    return jsonify({'error': 'Недостаточно данных'}), 400

@app.route('/api/v2/notifications', methods=['GET'])
@jwt_required()
def api_v2_notifications():
    """API v2 для получения уведомлений"""
    user_id = get_jwt_identity()
    notifications = Notification.query.filter_by(
        user_id=user_id,
        is_read=False
    ).order_by(Notification.created_at.desc()).limit(50).all()
    
    return jsonify([{
        'id': n.id,
        'title': n.title,
        'message': n.message,
        'type': n.type,
        'created_at': n.created_at.isoformat()
    } for n in notifications])

@app.route('/api/v2/loyalty/info', methods=['GET'])
@jwt_required()
def api_v2_loyalty_info():
    """API v2 для получения информации о программе лояльности"""
    user_id = get_jwt_identity()
    customer = Customer.query.filter_by(user_id=user_id).first()
    
    if not customer:
        customer = Customer(user_id=user_id)
        db.session.add(customer)
        db.session.commit()
    
    return jsonify({
        'bonus_points': customer.bonus_points,
        'total_purchases': customer.total_purchases,
        'total_orders': customer.total_orders,
        'loyalty_program': customer.loyalty_program.name if customer.loyalty_program else None,
        'discount_percent': customer.loyalty_program.discount_percent if customer.loyalty_program else 0
    })

@app.route('/api/v2/marketplace/integrations', methods=['GET'])
@jwt_required()
def api_v2_marketplace_integrations():
    """API v2 для получения списка интеграций"""
    user_id = get_jwt_identity()
    integrations = MarketplaceIntegration.query.filter_by(user_id=user_id).all()
    
    return jsonify([{
        'id': i.id,
        'name': i.name,
        'marketplace_type': i.marketplace_type,
        'last_sync': i.last_sync.isoformat() if i.last_sync else None,
        'sync_status': i.sync_status
    } for i in integrations])

# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ

def init_db():
    """Инициализация базы данных с начальными данными"""
    with app.app_context():
        db.create_all()
        
        # Создаем администратора если нет пользователей
        if User.query.count() == 0:
            admin = User(
                username='admin',
                email='admin@warehouse.com',
                role=UserRole.ADMIN
            )
            admin.set_password('admin123')
            db.session.add(admin)
            
            # Создаем тестового кладовщика
            storekeeper = User(
                username='storekeeper',
                email='storekeeper@warehouse.com',
                role=UserRole.STOREKEEPER
            )
            storekeeper.set_password('store123')
            db.session.add(storekeeper)
            
            # Создаем тестового сотрудника
            employee = User(
                username='employee',
                email='employee@warehouse.com',
                role=UserRole.EMPLOYEE
            )
            employee.set_password('emp123')
            db.session.add(employee)
            
            db.session.commit()
        
        # Создаем валюты
        currencies = [
            Currency(code='RUB', symbol='₽', name='Российский рубль', rate_to_rub=1.0),
            Currency(code='USD', symbol='$', name='Доллар США', rate_to_rub=90.0),
            Currency(code='EUR', symbol='€', name='Евро', rate_to_rub=100.0)
        ]
        
        for currency in currencies:
            if not Currency.query.get(currency.code):
                db.session.add(currency)
        
        # Создаем налоговые ставки
        tax_rates = [
            TaxRate(name='НДС 20%', rate=0.20, applies_to='all'),
            TaxRate(name='НДС 10%', rate=0.10, applies_to='food'),
            TaxRate(name='Без НДС', rate=0.0, applies_to='special')
        ]
        
        for tax in tax_rates:
            if not TaxRate.query.filter_by(name=tax.name).first():
                db.session.add(tax)
        
        # Создаем зоны склада
        zones = [
            WarehouseZone(name='Зона приемки', code='REC', type='receiving', capacity=50),
            WarehouseZone(name='Основное хранение', code='STO', type='storage', capacity=500),
            WarehouseZone(name='Зона отгрузки', code='SHIP', type='shipping', capacity=30)
        ]
        
        for zone in zones:
            if not WarehouseZone.query.filter_by(code=zone.code).first():
                db.session.add(zone)
        
        db.session.commit()
        
        # Создаем ячейки для каждой зоны
        for zone in WarehouseZone.query.all():
            if not zone.cells:
                for row in range(1, 6):
                    for shelf in range(1, 11):
                        cell_code = f"{zone.code}-{row:02d}-{shelf:02d}"
                        cell = StorageCell(
                            zone_id=zone.id,
                            code=cell_code,
                            barcode=f"CELL{cell_code}",
                            max_weight=100.0,
                            max_volume=1.0
                        )
                        db.session.add(cell)
        
        # Создаем программы лояльности
        programs = [
            LoyaltyProgram(name='Базовый', description='Базовая программа', discount_percent=0, min_purchases=0),
            LoyaltyProgram(name='Серебряный', description='5% скидка', discount_percent=5, min_purchases=5, min_amount=10000),
            LoyaltyProgram(name='Золотой', description='10% скидка', discount_percent=10, min_purchases=10, min_amount=50000)
        ]
        
        for program in programs:
            if not LoyaltyProgram.query.filter_by(name=program.name).first():
                db.session.add(program)
        
        # Создаем тестовые категории
        categories = [
            Category(name='Электроника', description='Электронные товары'),
            Category(name='Офисные принадлежности', description='Канцелярские товары'),
            Category(name='Хозяйственные товары', description='Товары для уборки')
        ]
        
        for category in categories:
            if not Category.query.filter_by(name=category.name).first():
                db.session.add(category)
        
        # Создаем тестовые товары
        if Product.query.count() == 0:
            rub = Currency.query.get('RUB')
            tax20 = TaxRate.query.filter_by(name='НДС 20%').first()
            
            products = [
                Product(
                    name='Ноутбук',
                    description='Мощный ноутбук для работы',
                    sku='LAP001',
                    unit='шт',
                    price=50000,
                    cost_price=40000,
                    min_stock=2,
                    current_stock=5,
                    category_id=1,
                    currency_code=rub.code if rub else 'RUB',
                    tax_rate_id=tax20.id if tax20 else None,
                    weight=2.5,
                    volume=0.02
                ),
                Product(
                    name='Монитор',
                    description='27-дюймовый монитор',
                    sku='MON001',
                    unit='шт',
                    price=25000,
                    cost_price=20000,
                    min_stock=2,
                    current_stock=3,
                    category_id=1,
                    currency_code=rub.code if rub else 'RUB',
                    tax_rate_id=tax20.id if tax20 else None,
                    weight=4.0,
                    volume=0.03
                )
            ]
            
            for product in products:
                db.session.add(product)
        
        db.session.commit()
        
        # Генерируем штрих-коды для товаров
        barcode_gen = BarcodeGenerator()
        for product in Product.query.all():
            if not product.barcode_image:
                barcode_path = barcode_gen.generate_product_barcode(product)
                if barcode_path:
                    product.barcode_image = barcode_path
            
            if not product.qr_code:
                qr_path = barcode_gen.generate_product_qr(product)
                if qr_path:
                    product.qr_code = qr_path
        
        db.session.commit()
        
        app.logger.info("Database initialized with advanced features")

# ЗАПУСК ПРИЛОЖЕНИЯ

if __name__ == '__main__':
    init_db()
    
    # Запускаем фоновые задачи при старте
    with app.app_context():
        check_low_stock_task.delay()
        generate_forecasts_task.delay()
        
        # Отправляем тестовое email
        try:
            admins = User.query.filter_by(role=UserRole.ADMIN).all()
            if admins:
                send_email(
                    subject='🚀 Warehouse System Started with Advanced Features',
                    recipients=[admin.email for admin in admins],
                    text_body='Система управления складом с расширенными функциями успешно запущена.',
                    html_body='<h1>Система с расширенными функциями запущена</h1>'
                )
        except Exception as e:
            app.logger.warning(f"Could not send startup email: {str(e)}")
    
    # Запускаем с поддержкой WebSocket
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True)