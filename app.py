# app.py
from flask import Flask, render_template, request, redirect, url_for, flash, session, g
import sqlite3
from datetime import datetime, timedelta
import random
import string
import smtplib
from email.mime.text import MIMEText
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import csv
import os

# --- Google Sheets Integration START ---
import gspread
from google.oauth2.service_account import Credentials
# --- Google Sheets Integration END ---

app = Flask(__name__)
app.secret_key = 'super_secret_key_anda' # Ganti dengan kunci rahasia yang kuat!

# --- Konfigurasi File Upload START ---
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'csv'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
# --- Konfigurasi File Upload END ---

DATABASE = 'perpustakaan.db'

# --- Konfigurasi Email ---
SMTP_SERVER = 'smtp.gmail.com'
SMTP_PORT = 587
SMTP_USERNAME = 'notifikasisistempinjambuku@gmail.com'
SMTP_PASSWORD = 'cqet ipux kllz ldjh'
PETUGAS_NOTIFICATION_EMAIL = 'alif.bomantara3@gmail.com'

# --- Google Sheets Configuration START ---
CREDENTIALS_FILE = 'credentials.json'
SPREADSHEET_NAME = 'Data Peminjam Buku Gramedia'
GOOGLE_SHEETS_WORKSHEET = None
# --- Google Sheets Configuration END ---

# --- Fungsi Database ---
def init_db():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS books (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Tersedia'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            email TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id TEXT NOT NULL,
            user_id INTEGER,
            borrower_name_manual TEXT,
            borrower_email TEXT,
            petugas_id INTEGER,
            petugas_name_manual TEXT,
            loan_date TEXT NOT NULL, -- Pastikan ini bisa menyimpan YYYY-MM-DD HH:MM:SS
            due_date TEXT NOT NULL,
            return_date TEXT, -- Pastikan ini bisa menyimpan YYYY-MM-DD HH:MM:SS
            FOREIGN KEY (book_id) REFERENCES books(id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (petugas_id) REFERENCES users(id)
        )
    ''')
    conn.commit()

    # --- Tambahkan kolom baru jika belum ada (untuk database yang sudah ada) ---
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")
        print("Kolom 'email' berhasil ditambahkan ke tabel 'users'.")
    except sqlite3.OperationalError as e:
        if "duplicate column name: email" not in str(e):
            print(f"Error menambahkan kolom email ke users: {e}")
        else:
            print("Kolom 'email' di tabel 'users' sudah ada.")
            
    try:
        cursor.execute("ALTER TABLE loans ADD COLUMN borrower_email TEXT")
        print("Kolom 'borrower_email' berhasil ditambahkan ke tabel 'loans'.")
    except sqlite3.OperationalError as e:
        if "duplicate column name: borrower_email" not in str(e):
            print(f"Error menambahkan kolom borrower_email: {e}")
        else:
            print("Kolom 'borrower_email' sudah ada.")

    try:
        cursor.execute("ALTER TABLE loans ADD COLUMN petugas_id INTEGER")
        print("Kolom 'petugas_id' berhasil ditambahkan ke tabel 'loans'.")
    except sqlite3.OperationalError as e:
        if "duplicate column name: petugas_id" not in str(e):
            print(f"Error menambahkan kolom petugas_id: {e}")
        else:
            print("Kolom 'petugas_id' sudah ada.")

    try:
        cursor.execute("ALTER TABLE loans ADD COLUMN petugas_name_manual TEXT")
        print("Kolom 'petugas_name_manual' berhasil ditambahkan ke tabel 'loans'.")
    except sqlite3.OperationalError as e:
        if "duplicate column name: petugas_name_manual" not in str(e):
            print(f"Error menambahkan kolom petugas_name_manual: {e}")
        else:
            print("Kolom 'petugas_name_manual' sudah ada.")
            
    conn.commit()
    conn.close()

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

# Menambahkan fungsi `now` sebagai global di Jinja environment
app.jinja_env.globals['now'] = datetime.now

app.jinja_env.filters['is_overdue'] = lambda date_str: datetime.now().date() > datetime.strptime(date_str, '%Y-%m-%d').date()

# Filter Jinja baru untuk format datetime (YYYY-MM-DD HH:MM:SS ke DD/MM/YYYY HH:MM)
def datetimeformat(value, format_string='%d/%m/%Y %H:%M'):
    if value:
        try:
            # Coba parsing sebagai datetime (jika ada jamnya)
            dt_obj = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            try:
                # Jika hanya tanggal (lama)
                dt_obj = datetime.strptime(value, '%Y-%m-%d')
            except ValueError:
                return value # Kembalikan nilai asli jika format tidak cocok
        return dt_obj.strftime(format_string)
    return "-" # Kembalikan '-' jika nilai kosong

app.jinja_env.filters['datetimeformat'] = datetimeformat


def generate_random_id(length=6):
    characters = string.ascii_uppercase + string.digits
    return ''.join(random.choice(characters) for i in range(length))

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- Fungsi Pengiriman Email ---
def send_email(recipient_email, subject, body):
    if not recipient_email or not SMTP_USERNAME or not SMTP_PASSWORD:
        print("Konfigurasi email pengirim tidak lengkap atau email penerima kosong. Pengiriman dibatalkan.")
        return False

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = SMTP_USERNAME
    msg['To'] = recipient_email

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        print(f"Email '{subject}' berhasil dikirim ke {recipient_email}")
        return True
    except Exception as e:
        print(f"Pengiriman email gagal ke {recipient_email}: {e}")
        return False

# --- Fungsi Penjadwalan Email Otomatis ---
def check_and_send_reminders():
    print("Menjalankan pengecekan dan pengiriman pengingat/notifikasi email...")
    conn = get_db_connection()
    today = datetime.now().date()

    loans = conn.execute('''
        SELECT
            l.id as loan_id,
            b.title,
            b.author,
            l.borrower_name_manual,
            l.borrower_email,
            l.due_date
        FROM loans l
        JOIN books b ON l.book_id = b.id
        WHERE l.return_date IS NULL
    ''').fetchall()
    
    for loan in loans:
        due_date_obj = datetime.strptime(loan['due_date'], '%Y-%m-%d').date()
        
        # Pengingat: 1 hari sebelum jatuh tempo
        if (due_date_obj - today).days == 1:
            if loan['borrower_email']:
                subject = f'Pengingat: Batas Waktu Pengembalian Buku "{loan["title"]}" Besok!'
                body = f"""
Halo {loan['borrower_name_manual']},

Kami ingin mengingatkan bahwa batas waktu pengembalian buku "{loan['title']}" oleh {loan['author']}" adalah besok, tanggal {loan['due_date']}.

Mohon segera kembalikan buku Anda, Jika terlambat atau tidak mengembalikan buku harus dibayar.

Terima kasih,
Sistem Pinjam Buku Gramedia
"""
                send_email(loan['borrower_email'], subject, body)
            else:
                print(f"Peminjam {loan['borrower_name_manual']} tidak memiliki email untuk pengingat buku {loan['title']}.")

        # Notifikasi Keterlambatan: Sudah melewati batas waktu
        elif today > due_date_obj:
            if loan['borrower_email']:
                subject = f'PENTING: Buku "{loan["title"]}" Anda Sudah Terlambat Dikembalikan!'
                body = f"""
Halo {loan['borrower_name_manual']},

Kami ingin memberitahukan bahwa buku "{loan['title']}" oleh {loan['author']}" yang Anda pinjam sudah melewati batas waktu pengembalian.

Batas waktu pengembalian adalah tanggal {loan['due_date']}.

Mohon segera kembalikan buku Anda untuk menghindari denda atau sanksi lainnya, yaitu membayar buku yang dipinjam.

Terima kasih,
Sistem Pinjam Buku Gramedia
"""
                send_email(loan['borrower_email'], subject, body)
            else:
                print(f"Peminjam {loan['borrower_name_manual']} tidak memiliki email untuk notifikasi keterlambatan buku {loan['title']}.")
    
    conn.close()
    print("Pengecekan dan pengiriman pengingat/notifikasi email selesai.")

# --- Google Sheets Functions START ---
def initialize_gspread_connection():
    global GOOGLE_SHEETS_WORKSHEET
    try:
        scope = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scope)
        gc = gspread.authorize(creds)
        spreadsheet = gc.open(SPREADSHEET_NAME)
        GOOGLE_SHEETS_WORKSHEET = spreadsheet.sheet1
        print(f"Berhasil terhubung ke Google Sheet: {SPREADSHEET_NAME}")
    except Exception as e:
        print(f"Error saat menginisialisasi Google Sheets: {e}")
        print("Pastikan:")
        print(f"1. File '{CREDENTIALS_FILE}' ada dan berisi kredensial yang valid.")
        print(f"2. Google Sheet dengan nama '{SPREADSHEET_NAME}' ada di akun Google Anda.")
        print("3. Anda telah berbagi Google Sheet tersebut dengan 'Client email' dari service account Anda (misal: your-service-account@your-project-id.iam.gserviceaccount.com).")
        GOOGLE_SHEETS_WORKSHEET = None # Set ke None jika gagal

def add_loan_to_gsheet(book_id, title, author, borrower_name, borrower_email, petugas_name, loan_datetime, due_date):
    if GOOGLE_SHEETS_WORKSHEET is None:
        print("Koneksi Google Sheet belum terinisialisasi. Data tidak dapat ditambahkan.")
        return False
    
    # Format tanggal/waktu sesuai permintaan user ("beserta jam")
    loan_date_str_with_time = loan_datetime.strftime('%d/%m/%Y %H:%M')
    
    data_row = [
        book_id,
        title,
        author,
        borrower_name,
        borrower_email,
        petugas_name,
        loan_date_str_with_time,
        due_date, # due_date sudah string YYYY-MM-DD
        '' # Kolom Tanggal Pengembalian kosong saat peminjaman
    ]
    try:
        GOOGLE_SHEETS_WORKSHEET.append_row(data_row)
        print(f"Data peminjaman buku '{title}' berhasil ditambahkan ke Google Sheet.")
        return True
    except Exception as e:
        print(f"Gagal menambahkan data peminjaman ke Google Sheet: {e}")
        return False

def update_return_in_gsheet(book_id, borrower_name, loan_date_str, return_datetime):
    if GOOGLE_SHEETS_WORKSHEET is None:
        print("Koneksi Google Sheet belum terinisialisasi. Data tidak dapat diupdate.")
        return False

    # Format return_datetime sesuai permintaan user ("beserta jam")
    return_date_str_with_time = return_datetime.strftime('%d/%m/%Y %H:%M')

    try:
        # Untuk mencari baris, kita ambil semua data dan cari manual
        all_data = GOOGLE_SHEETS_WORKSHEET.get_all_values()
        
        # Asumsi header ada di baris pertama
        header = all_data[0]
        data_rows = all_data[1:]

        # Temukan indeks kolom (ingat indeks dimulai dari 0 di Python)
        # Pastikan nama header di Google Sheet Anda SAMA PERSIS dengan ini
        id_buku_col = header.index('ID Buku')
        nama_peminjam_col = header.index('Nama Peminjam')
        tanggal_pinjam_col = header.index('Tanggal Pinjam (beserta jam)')
        tanggal_kembali_col = header.index('Tanggal Pengembalian (beserta jam)')

        row_index_to_update = -1
        
        # Iterate melalui baris data untuk menemukan peminjaman yang sesuai
        for i, row in enumerate(data_rows):
            # Periksa jika baris memiliki cukup kolom
            if (len(row) > max(id_buku_col, nama_peminjam_col, tanggal_pinjam_col, tanggal_kembali_col)):
                
                # Mengambil bagian tanggal dari "Tanggal Pinjam (beserta jam)" di Google Sheet (contoh: "03/07/2025")
                gsheet_loan_date_part = row[tanggal_pinjam_col].split(' ')[0] 
                # Mengubah format loan_date_str dari SQLite (YYYY-MM-DD) ke DD/MM/YYYY untuk perbandingan
                loan_date_for_comparison = datetime.strptime(loan_date_str, '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y')

                # Kriteria pencarian: ID Buku, Nama Peminjam, Bagian Tanggal Pinjam yang sama, dan Tanggal Pengembalian masih kosong
                if (row[id_buku_col] == book_id and
                    row[nama_peminjam_col] == borrower_name and
                    gsheet_loan_date_part == loan_date_for_comparison and
                    row[tanggal_kembali_col] == ''): # Hanya update yang belum dikembalikan
                    
                    row_index_to_update = i + 2 # +1 karena get_all_values() dimulai dari 0, +1 untuk baris header
                    break
        
        if row_index_to_update != -1:
            # Update sel Tanggal Pengembalian (ingat gspread.update_cell menggunakan indeks 1-based)
            GOOGLE_SHEETS_WORKSHEET.update_cell(row_index_to_update, tanggal_kembali_col + 1, return_date_str_with_time)
            print(f"Data pengembalian buku '{book_id}' oleh '{borrower_name}' berhasil diupdate di Google Sheet.")
            return True
        else:
            print(f"Peminjaman buku '{book_id}' oleh '{borrower_name}' pada tanggal '{loan_date_str}' tidak ditemukan di Google Sheet untuk diupdate.")
            return False
            
    except Exception as e:
        print(f"Gagal mengupdate data pengembalian di Google Sheet: {e}")
        return False
# --- Google Sheets Functions END ---


# --- Routes Aplikasi --- 

@app.before_request 
def before_request(): 
    init_db() 
    conn = get_db_connection() 
    try: 
        cursor = conn.cursor() 
        cursor.execute(''' 
            INSERT OR REPLACE INTO users (id, username, password, email) 
            VALUES ((SELECT id FROM users WHERE username = 'admin'), ?, ?, ?) 
        ''', ('admin', 'admin123', PETUGAS_NOTIFICATION_EMAIL)) 
        conn.commit() 
        print("User 'admin' dipastikan ada dan emailnya terupdate.") 
    except Exception as e: 
        print(f"Error saat memastikan user admin: {e}") 
    finally: 
        conn.close() 


@app.route('/') 
def index(): 
    if 'username' in session: 
        return redirect(url_for('dashboard')) 
    return redirect(url_for('login')) 

@app.route('/login', methods=['GET', 'POST']) 
def login(): 
    if request.method == 'POST': 
        username = request.form['username'] 
        password = request.form['password'] 
        conn = get_db_connection() 
        user = conn.execute('SELECT * FROM users WHERE username = ? AND password = ?', (username, password)).fetchone() 
        conn.close() 
        if user: 
            session['username'] = user['username'] 
            session['user_id'] = user['id'] 
            session['user_email'] = user['email'] if user['email'] else None  
            flash('Login berhasil!', 'success') 
            return redirect(url_for('dashboard')) 
        else: 
            flash('Username atau password salah.', 'danger') 
    return render_template('login.html') 

@app.route('/logout') 
def logout(): 
    session.pop('username', None) 
    session.pop('user_id', None) 
    session.pop('user_email', None) 
    flash('Anda telah logout.', 'info') 
    return redirect(url_for('login')) 

@app.route('/dashboard') 
def dashboard(): 
    if 'username' not in session: 
        return redirect(url_for('login')) 

    conn = get_db_connection() 
    
    books = conn.execute(''' 
        SELECT 
            b.id, 
            b.title, 
            b.author, 
            b.status, 
            COALESCE(u.username, l.borrower_name_manual) AS current_borrower_name 
        FROM books b 
        LEFT JOIN loans l ON b.id = l.book_id AND l.return_date IS NULL 
        LEFT JOIN users u ON l.user_id = u.id 
        ORDER BY b.title ASC 
    ''').fetchall() 
    
    loans = conn.execute(''' 
        SELECT 
            l.id as loan_id, 
            b.title, 
            b.author, 
            l.loan_date, 
            l.due_date, 
            l.return_date, 
            b.id as book_id, 
            COALESCE(u.username, l.borrower_name_manual) AS borrower_display_name, 
            l.borrower_email, 
            COALESCE(l.petugas_name_manual, p.username, 'Tidak Diketahui') AS petugas_display_name 
        FROM loans l 
        JOIN books b ON l.book_id = b.id 
        LEFT JOIN users u ON l.user_id = u.id 
        LEFT JOIN users p ON l.petugas_id = p.id 
        WHERE l.return_date IS NULL 
        ORDER BY l.due_date ASC 
    ''').fetchall() 
    conn.close() 

    today = datetime.now().date() 
    overdue_loans = [] 
    for loan in loans: 
        due_date_obj = datetime.strptime(loan['due_date'], '%Y-%m-%d').date() 
        if today > due_date_obj: 
            overdue_loans.append(loan) 

    return render_template('dashboard.html', books=books, loans=loans, overdue_loans=overdue_loans) 

@app.route('/add_book', methods=['POST']) 
def add_book(): 
    if 'username' not in session: 
        flash('Anda harus login untuk menambahkan buku.', 'danger') 
        return redirect(url_for('login')) 
    
    book_id = request.form['book_id'].strip() 
    title = request.form['title'].strip() 
    author = request.form['author'].strip() 

    conn = get_db_connection() 

    if not book_id: 
        book_id = generate_random_id() 
        while conn.execute('SELECT 1 FROM books WHERE id = ?', (book_id,)).fetchone(): 
            book_id = generate_random_id() 
    else: 
        existing_book = conn.execute('SELECT 1 FROM books WHERE id = ?', (book_id,)).fetchone() 
        if existing_book: 
            flash(f'ID buku "{book_id}" sudah ada. Mohon gunakan ID lain.', 'danger') 
            conn.close() 
            return redirect(url_for('dashboard')) 
            
    try: 
        # Simpan waktu saat ini bersama tanggal
        current_datetime_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S') 
        conn.execute('INSERT INTO books (id, title, author) VALUES (?, ?, ?)', (book_id, title, author)) 
        conn.commit() 
        flash(f'Buku "{title}" dengan ID "{book_id}" berhasil ditambahkan!', 'success') 
    except sqlite3.IntegrityError: 
        flash('Terjadi kesalahan saat menambahkan buku. Mungkin ID sudah ada.', 'danger') 
    except Exception as e: 
        flash(f'Terjadi kesalahan tak terduga: {e}', 'danger') 
    finally: 
        conn.close() 
    
    return redirect(url_for('dashboard'))

# --- Rute Baru: Impor Buku dari CSV START ---
@app.route('/import_books', methods=['GET', 'POST'])
def import_books():
    if 'username' not in session:
        flash('Anda harus login untuk mengimpor buku.', 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        # Periksa apakah request memiliki bagian file
        if 'file' not in request.files:
            flash('Tidak ada bagian file.', 'danger')
            return redirect(request.url)
        file = request.files['file']
        # Jika user tidak memilih file, browser juga mengirimkan bagian kosong tanpa nama file
        if file.filename == '':
            flash('Tidak ada file yang dipilih.', 'danger')
            return redirect(request.url)
        if file and allowed_file(file.filename):
            # Simpan file sementara ke folder UPLOAD_FOLDER
            filename = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
            file.save(filename)

            imported_count = 0
            skipped_count = 0
            conn = get_db_connection()
            try:
                with open(filename, 'r', encoding='utf-8') as csvfile:
                    reader = csv.reader(csvfile)
                    header = next(reader) # Baca baris header
                    
                    # Validasi header sederhana (opsional tapi disarankan)
                    # Pastikan header di CSV Anda adalah: id_buku,judul_buku,penulis
                    if header != ['id_buku', 'judul_buku', 'penulis']:
                        flash("Format header CSV tidak sesuai. Harusnya: id_buku,judul_buku,penulis", 'danger')
                        conn.close()
                        os.remove(filename) # Hapus file sementara
                        return redirect(request.url)

                    for row in reader:
                        if len(row) == 3:
                            book_id, title, author = [item.strip() for item in row]
                            
                            # Cek duplikasi ID sebelum insert
                            existing_book = conn.execute('SELECT 1 FROM books WHERE id = ?', (book_id,)).fetchone()
                            if existing_book:
                                skipped_count += 1
                                print(f"Melewatkan buku dengan ID '{book_id}' karena sudah ada.")
                                continue # Lanjut ke baris berikutnya
                            
                            try:
                                conn.execute('INSERT INTO books (id, title, author) VALUES (?, ?, ?)', (book_id, title, author))
                                imported_count += 1
                            except sqlite3.IntegrityError as e:
                                # Tangani error integritas jika ada (misal, ID sudah ada meskipun sudah dicek di atas)
                                skipped_count += 1
                                print(f"Error insert buku ID '{book_id}': {e}")
                            except Exception as e:
                                skipped_count += 1
                                print(f"Error tak terduga pada baris '{row}': {e}")
                        else:
                            skipped_count += 1
                            print(f"Melewatkan baris tidak valid: {row}")
                    conn.commit()
                    flash(f'Impor selesai! {imported_count} buku berhasil ditambahkan, {skipped_count} dilewati (ID duplikat/format salah).', 'success')
            except Exception as e:
                flash(f'Terjadi kesalahan saat membaca file CSV: {e}', 'danger')
            finally:
                conn.close()
                os.remove(filename) # Pastikan file sementara dihapus
            return redirect(url_for('dashboard'))
        else:
            flash('Jenis file tidak diizinkan. Hanya file CSV.', 'danger')
            return redirect(request.url)

    return render_template('import_books.html') # Render template form upload
# --- Rute Baru: Impor Buku dari CSV END ---


@app.route('/borrow_book/<string:book_id>', methods=['GET', 'POST']) 
def borrow_book(book_id): 
    if 'username' not in session: 
        flash('Anda harus login untuk melakukan peminjaman.', 'danger') 
        return redirect(url_for('login')) 

    conn = get_db_connection() 
    book = conn.execute('SELECT * FROM books WHERE id = ?', (book_id,)).fetchone() 

    if not book: 
        flash('Buku tidak ditemukan.', 'danger') 
        conn.close() 
        return redirect(url_for('dashboard')) 
    
    if book['status'] == 'Dipinjam': 
        flash('Buku ini sedang dipinjam.', 'danger') 
        conn.close() 
        return redirect(url_for('dashboard')) 

    if request.method == 'POST': 
        borrower_name = request.form.get('borrower_name', '').strip() 
        borrower_email = request.form.get('borrower_email', '').strip() 
        petugas_name_manual = request.form.get('petugas_name_manual', '').strip() 
        
        user_id = None 
        petugas_id = session.get('user_id') 
        petugas_email_for_notification = PETUGAS_NOTIFICATION_EMAIL 

        petugas_name_to_save = petugas_name_manual 
        if not petugas_name_to_save and 'username' in session: 
            petugas_name_to_save = session['username'] 
        elif not petugas_name_to_save: 
            petugas_name_to_save = "Tidak Diketahui" 

        if not borrower_name: 
            flash('Nama peminjam tidak boleh kosong. Silakan isi.', 'danger') 
            conn.close() 
            return redirect(url_for('dashboard')) 

        # --- Simpan Tanggal dan Waktu Saat Ini dalam format yang dapat diparsing
        current_datetime_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S') 
        due_date_obj = datetime.now() + timedelta(days=4)
        due_date_str = due_date_obj.strftime('%Y-%m-%d') # Batas waktu tetap hanya tanggal

        try: 
            conn.execute(''' 
                INSERT INTO loans (book_id, user_id, borrower_name_manual, borrower_email, petugas_id, petugas_name_manual, loan_date, due_date) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?) 
            ''', (book_id, user_id, borrower_name, borrower_email, petugas_id, petugas_name_to_save, current_datetime_str, due_date_str)) 
            
            conn.execute('UPDATE books SET status = "Dipinjam" WHERE id = ?', (book_id,)) 
            conn.commit() 
            flash(f'Buku "{book["title"]}" berhasil dipinjam oleh {borrower_name}!', 'success') 

            # --- Google Sheets Integration START (Panggil fungsi untuk menambahkan data) ---
            add_loan_to_gsheet(
                book_id, 
                book['title'], 
                book['author'], 
                borrower_name, 
                borrower_email, 
                petugas_name_to_save, 
                datetime.strptime(current_datetime_str, '%Y-%m-%d %H:%M:%S'), # Kirim objek datetime
                due_date_str 
            )
            # --- Google Sheets Integration END ---

            # --- Kirim email konfirmasi ke peminjam
            if borrower_email: 
                subject_peminjam = f'Konfirmasi Peminjaman Buku: {book["title"]}' 
                body_peminjam = f""" 
Halo {borrower_name}, 

Anda telah berhasil meminjam buku dari sistem Pinjam Buku kami. Berikut detail peminjaman Anda: 

ID Buku: {book_id} 
Judul Buku: {book['title']} 
Penulis: {book['author']} 
Tanggal Pinjam: {datetime.strptime(current_datetime_str, '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y %H:%M')} 
Batas Waktu Pengembalian: {due_date_str} 

Mohon kembalikan buku tepat waktu, Jika tidak dikembalikan sesuai waktu yang ditentukan, buku harus dibayar. 

Terima kasih, 
Sistem Sistem Pinjam Buku Gramedia 
""" 
                email_sent_peminjam = send_email(borrower_email, subject_peminjam, body_peminjam) 
                if email_sent_peminjam: 
                    flash('Email konfirmasi peminjaman berhasil dikirim ke peminjam.', 'info') 
                else: 
                    flash('Gagal mengirim email konfirmasi peminjaman ke peminjam. Cek konfigurasi SMTP.', 'warning') 
            else: 
                flash('Email peminjam tidak disediakan, tidak dapat mengirim konfirmasi email ke peminjam.', 'warning') 

            # --- Kirim email notifikasi ke petugas
            if PETUGAS_NOTIFICATION_EMAIL: 
                subject_petugas = f'Notifikasi Peminjaman Baru: {book["title"]}' 
                body_petugas = f""" 
Halo PIC Book, 

Sebuah buku baru saja dipinjam dari sistem Pinjam Buku Gramedia: 

ID Buku: {book_id} 
Judul Buku: {book['title']} 
Penulis: {book['author']} 
Dipinjam Oleh: {borrower_name} (Email: {borrower_email if borrower_email else 'Tidak Ada'}) 
Tanggal Pinjam: {datetime.strptime(current_datetime_str, '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y %H:%M')} 
Batas Waktu Pengembalian: {due_date_str} 
Dicatat oleh Petugas: {petugas_name_to_save} 

Ini adalah notifikasi otomatis. 

Terima kasih, 
Sistem Pinjam Buku Gramedia 
""" 
                email_sent_petugas = send_email(PETUGAS_NOTIFICATION_EMAIL, subject_petugas, body_petugas) 
                if email_sent_petugas: 
                    flash('Notifikasi peminjaman berhasil dikirim ke email petugas.', 'info') 
                else: 
                    flash('Gagal mengirim notifikasi peminjaman ke petugas. Cek konfigurasi SMTP.', 'warning') 
            else: 
                flash('Email notifikasi petugas tidak dikonfigurasi. Notifikasi tidak terkirim.', 'warning') 

        except Exception as e: 
            flash(f'Terjadi kesalahan saat memproses peminjaman: {e}', 'danger') 
        finally: 
            conn.close() 
        return redirect(url_for('dashboard')) 
    
    conn.close() 
    return render_template('borrow_confirm.html', book=book, default_borrower=session.get('username')) 


@app.route('/return_book/<int:loan_id>') 
def return_book(loan_id): 
    if 'username' not in session: 
        flash('Anda harus login untuk melakukan pengembalian buku.', 'danger') 
        return redirect(url_for('login')) 
    
    conn = get_db_connection() 
    
    loan_details = conn.execute(''' 
        SELECT 
            l.id AS loan_id, 
            b.title, 
            b.author, 
            l.borrower_name_manual, 
            l.borrower_email, 
            l.loan_date, 
            l.due_date, 
            COALESCE(l.petugas_name_manual, p.username) AS petugas_display_name, 
            b.id AS book_id 
        FROM loans l 
        JOIN books b ON l.book_id = b.id 
        LEFT JOIN users p ON l.petugas_id = p.id 
        WHERE l.id = ? AND l.return_date IS NULL 
    ''', (loan_id,)).fetchone() 

    if loan_details: 
        try: 
            book_id = loan_details['book_id'] 
            # --- Simpan Tanggal dan Waktu Saat Ini dalam format yang dapat diparsing
            current_return_datetime_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            conn.execute('UPDATE loans SET return_date = ? WHERE id = ?', (current_return_datetime_str, loan_id)) 
            conn.execute('UPDATE books SET status = "Tersedia" WHERE id = ?', (book_id,)) 
            conn.commit() 
            flash('Buku berhasil dikembalikan!', 'success') 

            # --- Google Sheets Integration START (Panggil fungsi untuk update data) ---
            update_return_in_gsheet(
                loan_details['book_id'], 
                loan_details['borrower_name_manual'], 
                loan_details['loan_date'], 
                datetime.strptime(current_return_datetime_str, '%Y-%m-%d %H:%M:%S') # Kirim objek datetime
            )
            # --- Google Sheets Integration END ---

            # --- Kirim email konfirmasi pengembalian ke peminjam 
            borrower_email = loan_details['borrower_email'] 
            if borrower_email: 
                subject_peminjam_kembali = f'Konfirmasi Pengembalian Buku: {loan_details["title"]}' 
                body_peminjam_kembali = f""" 
Halo {loan_details['borrower_name_manual']}, 

Buku "{loan_details['title']}" oleh {loan_details['author']} yang Anda pinjam telah berhasil dikembalikan. 

Detail Peminjaman: 
ID Buku: {book_id} 
Tanggal Pinjam: {loan_details['loan_date']} 
Tanggal Dikembalikan: {datetime.strptime(current_return_datetime_str, '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y %H:%M')} 

Terima kasih telah menggunakan layanan Sistem Pinjam kami. 

Hormat kami, 
Sistem Pinjam Buk Gramedia 
""" 
                email_sent_peminjam_kembali = send_email(borrower_email, subject_peminjam_kembali, body_peminjam_kembali) 
                if email_sent_peminjam_kembali: 
                    flash('Email konfirmasi pengembalian berhasil dikirim ke peminjam.', 'info') 
                else: 
                    flash('Gagal mengirim email konfirmasi pengembalian ke peminjam. Cek konfigurasi SMTP.', 'warning') 
            else: 
                flash('Email peminjam tidak tersedia untuk konfirmasi pengembalian.', 'warning') 

            # --- Kirim email notifikasi pengembalian ke petugas 
            petugas_email_for_notification = PETUGAS_NOTIFICATION_EMAIL 
            if petugas_email_for_notification: 
                subject_petugas_kembali = f'Notifikasi Pengembalian Buku: {loan_details["title"]}' 
                body_petugas_kembali = f""" 
Halo PIC BOOK, 

Buku berikut telah dikembalikan ke sistem Pinjam Buku Gramedia: 

ID Buku: {book_id} 
Judul Buku: {loan_details['title']} 
Penulis: {loan_details['author']} 
Dipinjam Oleh: {loan_details['borrower_name_manual']} (Email: {borrower_email if borrower_email else 'Tidak Ada'}) 
Tanggal Pinjam: {loan_details['loan_date']} 
Tanggal Dikembalikan: {datetime.strptime(current_return_datetime_str, '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y %H:%M')} 
Dicatat oleh Petugas (Peminjaman): {loan_details['petugas_display_name']} 
Dikembalikan oleh Petugas (Saat Ini): {session.get('username', 'Tidak Diketahui')} 

Ini adalah notifikasi otomatis. 

Terima kasih, 
Sistem Pinjam Buku Gramedia 
""" 
                email_sent_petugas_kembali = send_email(petugas_email_for_notification, subject_petugas_kembali, body_petugas_kembali) 
                if email_sent_petugas_kembali: 
                    flash('Notifikasi pengembalian buku berhasil dikirim ke email petugas.', 'info') 
                else: 
                    flash('Gagal mengirim notifikasi pengembalian buku ke petugas. Cek konfigurasi SMTP.', 'warning') 
            else: 
                flash('Email notifikasi petugas tidak dikonfigurasi. Notifikasi pengembalian tidak terkirim.', 'warning') 

        except Exception as e: 
            flash(f'Terjadi kesalahan saat memproses pengembalian: {e}', 'danger') 
        finally: 
            conn.close() 
    else: 
        flash('Peminjaman tidak ditemukan atau sudah dikembalikan.', 'danger') 
        conn.close() 
        
    return redirect(url_for('dashboard')) 

# --- START MODIFIKASI UNTUK FILTER RIWAYAT PEMINJAMAN ---
@app.route('/loan_history') 
def loan_history(): 
    if 'username' not in session:
        flash('Anda harus login untuk melihat riwayat peminjaman.', 'danger')
        return redirect(url_for('login'))

    conn = get_db_connection() 
    
    # Ambil parameter filter dari URL
    filter_book_id = request.args.get('book_id', '').strip()
    filter_book_title = request.args.get('book_title', '').strip()
    filter_book_author = request.args.get('book_author', '').strip()
    filter_borrower_name = request.args.get('borrower_name', '').strip()
    filter_borrower_email = request.args.get('borrower_email', '').strip()
    filter_petugas = request.args.get('petugas', '').strip()
    filter_status = request.args.get('status', '').strip()
    filter_loan_date_start = request.args.get('loan_date_start', '').strip()
    filter_loan_date_end = request.args.get('loan_date_end', '').strip()


    query = ''' 
        SELECT  
            l.id, 
            b.title AS book_title, 
            b.id AS book_id, 
            b.author AS book_author, 
            l.borrower_name_manual AS borrower_name, 
            l.borrower_email, 
            l.loan_date, 
            l.due_date, 
            l.return_date, 
            COALESCE(l.petugas_name_manual, u.username, 'Tidak Diketahui') AS petugas_name_display,
            CASE 
                WHEN l.return_date IS NOT NULL AND DATE(l.return_date) > DATE(l.due_date) THEN 'Dikembalikan (Terlambat)'
                WHEN l.return_date IS NOT NULL THEN 'Dikembalikan' 
                WHEN DATE(l.due_date) < DATE('now', 'localtime') AND l.return_date IS NULL THEN 'Terlambat' 
                ELSE 'Dipinjam' 
            END AS status_display
        FROM   
            loans l 
        JOIN   
            books b ON l.book_id = b.id 
        LEFT JOIN 
            users u ON l.petugas_id = u.id 
        WHERE 1=1
    '''
    params = []

    if filter_book_id:
        query += " AND b.id LIKE ?"
        params.append(f'%{filter_book_id}%')
    if filter_book_title:
        query += " AND b.title LIKE ?"
        params.append(f'%{filter_book_title}%')
    if filter_book_author:
        query += " AND b.author LIKE ?"
        params.append(f'%{filter_book_author}%')
    if filter_borrower_name:
        query += " AND l.borrower_name_manual LIKE ?"
        params.append(f'%{filter_borrower_name}%')
    if filter_borrower_email:
        query += " AND l.borrower_email LIKE ?"
        params.append(f'%{filter_borrower_email}%')
    if filter_petugas:
        query += " AND (l.petugas_name_manual LIKE ? OR u.username LIKE ?)"
        params.append(f'%{filter_petugas}%')
        params.append(f'%{filter_petugas}%')
    
    if filter_status:
        if filter_status == 'Dipinjam':
            query += " AND l.return_date IS NULL AND DATE(l.due_date) >= DATE('now', 'localtime')"
        elif filter_status == 'Dikembalikan':
            query += " AND l.return_date IS NOT NULL AND DATE(l.return_date) <= DATE(l.due_date)" # Dikembalikan tepat waktu
        elif filter_status == 'Terlambat':
            query += " AND (DATE(l.due_date) < DATE('now', 'localtime') AND l.return_date IS NULL) OR (l.return_date IS NOT NULL AND DATE(l.return_date) > DATE(l.due_date))"
        elif filter_status == 'Dikembalikan (Terlambat)':
             query += " AND l.return_date IS NOT NULL AND DATE(l.return_date) > DATE(l.due_date)"


    if filter_loan_date_start:
        query += " AND DATE(l.loan_date) >= DATE(?)"
        params.append(filter_loan_date_start)
    if filter_loan_date_end:
        query += " AND DATE(l.loan_date) <= DATE(?)"
        params.append(filter_loan_date_end)


    query += " ORDER BY l.loan_date DESC, l.id DESC"
    
    loans = conn.execute(query, params).fetchall() 
    conn.close() 

    # Simpan nilai filter yang digunakan untuk ditampilkan kembali di form
    applied_filters = {
        'book_id': filter_book_id,
        'book_title': filter_book_title,
        'book_author': filter_book_author,
        'borrower_name': filter_borrower_name,
        'borrower_email': filter_borrower_email,
        'petugas': filter_petugas,
        'status': filter_status,
        'loan_date_start': filter_loan_date_start,
        'loan_date_end': filter_loan_date_end
    }

    return render_template('loan_history.html', loans=loans, applied_filters=applied_filters) 
# --- END MODIFIKASI UNTUK FILTER RIWAYAT PEMINJAMAN ---

# Pastikan ini tetap di bagian paling bawah file app.py 

if __name__ == '__main__': 
    # --- Inisialisasi Scheduler ---
    scheduler = BackgroundScheduler()
    # Jadwalkan fungsi check_and_send_reminders untuk berjalan setiap hari pada waktu tertentu (misalnya, jam 8 pagi)
    scheduler.add_job(func=check_and_send_reminders, trigger='cron', hour=8, minute=0)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())

    # --- Google Sheets Integration START (Panggil fungsi inisialisasi) ---
    initialize_gspread_connection() 
    # --- Google Sheets Integration END ---

    with app.app_context(): 
        init_db() 
        conn = get_db_connection() 
        try: 
            cursor = conn.cursor() 
            cursor.execute(''' 
                INSERT OR REPLACE INTO users (id, username, password, email) 
                VALUES ((SELECT id FROM users WHERE username = 'admin'), ?, ?, ?) 
            ''', ('admin', 'admin123', PETUGAS_NOTIFICATION_EMAIL)) 
            conn.commit() 
            print("User 'admin' dipastikan ada dan emailnya terupdate.") 
        except Exception as e: 
            print(f"Error saat memastikan user admin: {e}") 
        finally: 
            conn.close() 
    app.run(debug=True)